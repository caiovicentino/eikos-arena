"""Publishes a sanitized snapshot of a running arena to a public Hugging Face dataset, for the public page.

Read-only against the engine (GET <engine>/api/state and /api/history); the engine and its frozen code are not
touched. Every 10 s it checks the engine; when a new decision cycle has landed (or 5 minutes passed) it builds
live/state.json with only public fields, refuses to upload anything that looks like a secret or an internal detail,
and commits it to datasets/<repo>. Outbound HTTPS only; the Hugging Face token comes from huggingface_hub's usual
login (HF_TOKEN or `huggingface-cli login`) and is never written anywhere.

usage: python publish_live.py <run_dir> --repo <user>/<dataset> [--engine http://127.0.0.1:8340]
                              [--deny-file FILE] [--once] [--dry-run]

--deny-file: a private file (keep it out of git) with one literal string per line, such as your server's address,
SSH port, hostnames or folder names. A snapshot that contains any of them is not uploaded."""
import argparse
import json
import re
import time
import urllib.request
from pathlib import Path

FORBIDDEN = re.compile(
    r"vck_|hf_[A-Za-z0-9]{8}|\bsk-[A-Za-z0-9]|\bbk-[A-Za-z0-9]|gh[pousr]_[A-Za-z0-9]{8}|github_pat_|AKIA[0-9A-Z]{12}|"
    r"PRIVATE KEY|Bearer|Authorization|/root|/Users|/Volumes|/home/|127\.0\.0\.1|localhost|"
    r"\b\d{1,3}(?:\.\d{1,3}){3}\b|"  # any IPv4 address (prices never have three dots)
    r"providerMetadata|Traceback|RuntimeError|HTTPError|Errno", re.I)


def load_deny(path):
    if not path:
        return None
    words = [w.strip() for w in Path(path).read_text().splitlines() if w.strip() and not w.startswith("#")]
    return re.compile("|".join(re.escape(w) for w in words), re.I) if words else None


def get(engine, path):
    with urllib.request.urlopen(engine + path, timeout=20) as r:
        return json.loads(r.read())


def clean_text(t):
    if "no answer" in t:
        return "no answer this cycle (API error); positions kept"
    if "restarted" in t:
        return "engine restarted from its saved state"
    if "cycle skipped" in t:
        return "cycle skipped (market data unavailable)"
    t = re.sub(r"^(Eikos|Jev|eikos|jev): ", "", t)
    return re.sub(r"[^\w\s·→.,:;+\-%$/()]", "", t)[:160]


def read_fills(path, labels, keep=400):
    """Every simulated fill from the arena's cycle log (read-only): open/close, side, price, size, fee, closed P&L."""
    out = []
    if not path.exists():
        return out
    for line in path.open():
        try:
            r = json.loads(line)
        except ValueError:
            continue
        for who, pr in (r.get("players") or {}).items():
            for f in pr.get("fills") or []:
                rate = (r.get("market") or {}).get(f["coin"], {}).get("fee") or 0
                size = f["fee"] / (f["px"] * rate) if rate and f["px"] else None
                out.append({"t": round(r["t"]), "who": who, "coin": f["coin"], "label": labels.get(f["coin"], f["coin"]),
                            "action": f["action"], "side": f["side"], "px": f["px"],
                            "size": round(size, 8) if size else None, "fee": round(f["fee"], 4),
                            "pnl": round(f["pnl"], 4) if "pnl" in f else None})
    return out[-keep:]


def build(run_dir, engine):
    s, h = get(engine, "/api/state"), get(engine, "/api/history")
    rules_sha = ""
    pre = Path(run_dir) / "PREREG.sha256"
    if pre.exists():
        for line in pre.read_text().splitlines():
            if line.endswith("RULES.md"):
                rules_sha = line.split()[0]
    players = {}
    for p, v in s["players"].items():
        players[p] = {k: v.get(k) for k in ("equity", "balance", "fees", "funding", "realized", "trades", "open",
                                             "brier", "n_fc", "hit", "lat_last", "lat_med", "missed")}
        players[p]["last_changes"] = [clean_text(x) for x in (v.get("last") or [])]
        players[p]["last_missed"] = bool(v.get("last_error"))
    markets = []
    for m in s["markets"]:
        row = {k: m.get(k) for k in ("coin", "label", "cat", "desc", "mid", "chg24")}
        for p in ("eikos", "jev"):
            x = m[p]
            row[p] = {"side": x["side"], "entry": x["entry"], "upnl": x["upnl"],
                      "p_pos": x["p_pos"], "p_up": x["p_up"]}
        markets.append(row)
    feed = [{"t": e["t"], "who": e["who"], "text": clean_text(e["text"])} for e in s["feed"]]
    fills = read_fills(Path(run_dir) / "cycles.jsonl", {m["coin"]: m["label"] for m in s["markets"]})
    out = {
        "schema": 1, "published": time.time(),
        "arena": {k: s.get(k) for k in ("start", "end", "cycle", "cycles_total", "cycle_min", "next", "finished",
                                         "start_equity", "notional", "us_open")} | {"rules_sha256": rules_sha},
        "players": players, "markets": markets, "feed": feed[-60:], "fills": fills,
        "history": [[int(t), round(a, 2), round(b, 2)] for t, a, b in h["points"]],
    }
    return out


def blocked(raw, deny):
    """Why a snapshot must not be published, or None. Deny-list hits are not echoed (the list itself is private)."""
    bad = FORBIDDEN.search(raw)
    if bad:
        return repr(bad.group(0))
    if deny is not None and deny.search(raw):
        return "a string from the deny file"
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("run_dir")
    ap.add_argument("--repo", required=True, help="Hugging Face dataset, e.g. <user>/eikos-arena")
    ap.add_argument("--engine", default="http://127.0.0.1:8340")
    ap.add_argument("--deny-file", default=None)
    ap.add_argument("--once", action="store_true")
    ap.add_argument("--dry-run", action="store_true", help="build and check one snapshot, upload nothing")
    a = ap.parse_args()
    deny = load_deny(a.deny_file)
    api = None
    if not a.dry_run:
        from huggingface_hub import HfApi
        api = HfApi()
        api.create_repo(a.repo, repo_type="dataset", exist_ok=True)  # public, so the page can read it
    last_cycle, last_push = None, 0.0
    while True:
        try:
            snap = build(a.run_dir, a.engine)
            cyc = (snap["arena"]["cycle"], snap["arena"]["finished"])
            if cyc != last_cycle or time.time() - last_push >= 300 or a.once or a.dry_run:
                raw = json.dumps(snap, separators=(",", ":"))
                why = blocked(raw, deny)
                if why:  # never publish something that looks internal or secret
                    print(time.strftime("%H:%M:%S"), "BLOCKED: snapshot contains", why, flush=True)
                elif a.dry_run:
                    print(time.strftime("%H:%M:%S"), f"dry run: decision {snap['arena']['cycle']} passes the checks "
                          f"({len(raw)} bytes, {len(snap['fills'])} fills); nothing uploaded", flush=True)
                else:
                    api.upload_file(path_or_fileobj=raw.encode(), path_in_repo="live/state.json", repo_id=a.repo,
                                    repo_type="dataset", commit_message=f"live: decision {snap['arena']['cycle']}")
                    last_cycle, last_push = cyc, time.time()
                    print(time.strftime("%H:%M:%S"), f"published decision {snap['arena']['cycle']} ({len(raw)} bytes)",
                          flush=True)
        except Exception as e:  # noqa: BLE001
            print(time.strftime("%H:%M:%S"), "error:", type(e).__name__, str(e)[:120], flush=True)
        if a.once or a.dry_run:
            break
        time.sleep(10)


if __name__ == "__main__":
    main()
