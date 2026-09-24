"""Live paper-trading arena, Eikos vs Jev, on real Hyperliquid prices. Runs on the GPU machine for the whole period.

Every CYCLE_MIN minutes, aligned to the clock: one market snapshot; each player gets the same request template
(arena_core) with its own portfolio lines; both are asked in parallel; positions change at that snapshot's
bid/ask, so latency never changes a fill. The previous cycle's "higher in 5 minutes?" forecasts are scored
(Brier) against the new mid. Mids are polled every 3 s for mark-to-market. Serves the dashboard and /api/state.
Paper money only: no account, no orders; market data comes from Hyperliquid's public info API.

usage: python arena.py --run-dir <dir> [--hours 72] [--port 8340] [--eikos-url ...] [--dry-cycles N]
Keys come from the environment: TYPESAFE_API_KEY (Jev through TypeSafe's API) or AI_GATEWAY_API_KEY (Jev through the
Vercel AI Gateway), and EIKOS_API_KEY if your Eikos endpoint needs one."""
import argparse
import datetime as dt
import gzip
import http.client
import json
import os
import threading
import time
import traceback
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import arena_core as C
import hl

HERE = Path(__file__).resolve().parent
PLAYERS = ("eikos", "jev")
TYPESAFE_URL = "https://api.typesafe.ai/v1/systemone"
GATEWAY_URL = "https://ai-gateway.vercel.sh/v1/evaluate"


def jev_endpoint(key_file=None, url=None, model=None):
    """Jev's URL, model id and key. With TYPESAFE_API_KEY set: TypeSafe's own API. Otherwise: the Vercel AI Gateway
    with AI_GATEWAY_API_KEY. A key file replaces the environment key; JEV_URL / JEV_MODEL (or the flags) replace the
    defaults."""
    direct = bool(os.environ.get("TYPESAFE_API_KEY"))
    key = os.environ.get("TYPESAFE_API_KEY" if direct else "AI_GATEWAY_API_KEY", "")
    if key_file:
        key = Path(key_file).expanduser().read_text()
    url = url or os.environ.get("JEV_URL") or (TYPESAFE_URL if direct else GATEWAY_URL)
    model = model or os.environ.get("JEV_MODEL") or ("jev-latest" if "typesafe.ai" in url else "typesafe-ai/jev")
    if not key.strip():
        raise SystemExit("Jev needs a key: set TYPESAFE_API_KEY (TypeSafe's API) or AI_GATEWAY_API_KEY "
                         "(Vercel AI Gateway), or pass --jev-key-file")
    return url, model, key.strip()


def jev_body(body, url):
    """TypeSafe's own API names a yes/no question "noul"; the Vercel AI Gateway (and Eikos) name it "boolean"."""
    if "typesafe.ai" not in url:
        return body
    qs = {k: (q | {"type": "noul"} if q.get("type") == "boolean" else q) for k, q in body["questions"].items()}
    return body | {"questions": qs}


class Upstream:
    """Keep-alive connection per player; retries transient errors (the gateway sometimes answers 503)."""

    def __init__(self, url, headers=None, extra=None, prep=None):
        u = urllib.parse.urlparse(url)
        self.prep = prep or (lambda body: body)
        self.tls, self.host, self.port, self.path = u.scheme == "https", u.hostname, u.port, u.path
        self.headers = {"Content-Type": "application/json"} | (headers or {})
        self.extra, self.conn, self.lock = extra or {}, None, threading.Lock()

    def call(self, body, tries=5):
        data = json.dumps(self.prep(body) | self.extra).encode()
        last = None
        with self.lock:
            for i in range(tries):
                try:
                    if self.conn is None:
                        cls = http.client.HTTPSConnection if self.tls else http.client.HTTPConnection
                        self.conn = cls(self.host, self.port, timeout=120)
                    t0 = time.perf_counter()
                    self.conn.request("POST", self.path, body=data, headers=self.headers)
                    r = self.conn.getresponse()
                    raw = r.read()
                    ms = (time.perf_counter() - t0) * 1000
                    if r.getheader("connection", "").lower() == "close" or r.version == 10:
                        self.conn.close()
                        self.conn = None
                    if r.status != 200:
                        raise RuntimeError(f"HTTP {r.status}: {raw[:160]!r}")
                    return json.loads(raw), ms
                except Exception as e:  # noqa: BLE001
                    last = e
                    if self.conn is not None:
                        self.conn.close()
                    self.conn = None
                    time.sleep(min(20, 2 * (i + 1)))
        raise RuntimeError(f"{type(last).__name__}: {last}")


class Arena:
    def __init__(self, a):
        self.a = a
        self.dir = Path(a.run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        self.state_path = self.dir / "arena_state.json"
        self.log_path = self.dir / "cycles.jsonl"
        self.req_path = self.dir / "requests.jsonl.gz"
        jev_url, jev_model, key = jev_endpoint(a.jev_key_file, a.jev_url, a.jev_model)
        ekey = os.environ.get("EIKOS_API_KEY")
        self.up = {"eikos": Upstream(a.eikos_url, {"Authorization": f"Bearer {ekey}"} if ekey else None),
                   "jev": Upstream(jev_url, {"Authorization": f"Bearer {key}"}, {"model": jev_model},
                                   lambda body: jev_body(body, jev_url))}
        self.lock = threading.Lock()
        self.mids, self.mids_t = {}, 0.0
        try:  # 24h reference prices for the dashboard before the first cycle
            ctx = hl.contexts()
            self.prev_day = {c: ctx[c]["prev_day"] for c in hl.COINS}
        except Exception:  # noqa: BLE001
            self.prev_day = {}
        self.running_cycle = False
        if self.state_path.exists():
            s = json.loads(self.state_path.read_text())
            self.S = s
            self.pf = {p: C.Portfolio(s["pf"][p]) for p in PLAYERS}
            self.feed("engine restarted; state reloaded", "sys")
        else:
            now = time.time()
            self.S = {"start": now, "end": now + a.hours * 3600, "cycle": 0, "history": [], "feed": [],
                      "score": {p: {"brier": 0.0, "n": 0, "hits": 0, "calls": 0, "cal": [[0, 0.0, 0] for _ in range(10)]}
                                for p in PLAYERS},
                      "pending": None, "last": {}, "lat": {p: [] for p in PLAYERS}, "missed": {p: 0 for p in PLAYERS},
                      "funding_hours": [], "config": {"hours": a.hours, "cycle_min": C.CYCLE_MIN, "notional": C.NOTIONAL,
                                                      "start_equity": C.START_EQUITY, "markets": hl.MARKETS,
                                                      "eikos_url": a.eikos_url, "jev_model": jev_model}}
            self.pf = {p: C.Portfolio() for p in PLAYERS}
            self.feed(f"arena started: {len(hl.MARKETS)} markets, {C.CYCLE_MIN}-minute decisions, {a.hours} h", "sys")

    # ---------- bookkeeping ----------
    def feed(self, text, who):
        self.S.setdefault("feed", []).append({"t": time.time(), "who": who, "text": text})
        self.S["feed"] = self.S["feed"][-300:]

    def save(self):
        self.S["pf"] = {p: self.pf[p].to_dict() for p in PLAYERS}
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.S))
        os.replace(tmp, self.state_path)

    def equities(self, mids=None):
        mids = mids or self.mids
        return {p: self.pf[p].equity(mids) for p in PLAYERS}

    # ---------- loops ----------
    def price_loop(self):
        last_hist = 0.0
        while True:
            try:
                m = hl.mids()
                with self.lock:
                    self.mids, self.mids_t = m, time.time()
                    if time.time() - last_hist >= 30 and len(m) == len(hl.COINS):
                        e = self.equities()
                        self.S["history"].append([round(time.time()), round(e["eikos"], 2), round(e["jev"], 2)])
                        last_hist = time.time()
            except Exception as e:  # noqa: BLE001
                print("price loop:", e, flush=True)
            time.sleep(3)

    def cycle_loop(self):
        n_dry = 0
        while True:
            now = time.time()
            if now >= self.S["end"]:
                if not self.S.get("finished"):
                    with self.lock:
                        self.S["finished"] = now
                        e = self.equities()
                        self.feed(f"arena finished: Eikos {C.fusd(e['eikos'])}, Jev {C.fusd(e['jev'])}", "sys")
                        self.save()
                time.sleep(30)
                continue
            step = C.CYCLE_MIN * 60
            nxt = (int(now) // step + 1) * step + 2  # 2 s after the boundary: the 5-minute candle has rolled
            if self.a.dry_cycles and n_dry < self.a.dry_cycles:
                nxt = now  # dry runs do not wait for the clock
            self.S["next"] = nxt
            time.sleep(max(0, nxt - time.time()))
            try:
                self.running_cycle = True
                self.cycle()
            except Exception:  # noqa: BLE001
                traceback.print_exc()
                with self.lock:
                    self.feed("cycle skipped (error fetching data)", "sys")
            finally:
                self.running_cycle = False
            n_dry += 1

    def cycle(self):
        snap = hl.snapshot()
        t = snap["t"]
        utc = dt.datetime.fromtimestamp(t, dt.timezone.utc)
        rec = {"cycle": self.S["cycle"] + 1, "t": t, "fetch_s": round(snap["fetch_s"], 2),
               "market": {c: {"mid": snap["mid"][c], "bid": snap["book"][c][0], "ask": snap["book"][c][1],
                              "funding": snap["ctx"][c]["funding"], "oracle": snap["ctx"][c]["oracle"],
                              "fee": snap["ctx"][c]["fee"]} for c in hl.COINS}}
        with self.lock:
            # hourly funding on positions held into the top of the hour
            hour_key = utc.strftime("%Y-%m-%d %H")
            if utc.minute < C.CYCLE_MIN and hour_key not in self.S["funding_hours"]:
                self.S["funding_hours"].append(hour_key)
                rec["funding"] = {p: self.pf[p].pay_funding(snap["ctx"]) for p in PLAYERS}
            # score the previous cycle's forecasts against this snapshot
            pend = self.S.get("pending")
            if pend:
                res = {}
                for p in PLAYERS:
                    sc = self.S["score"][p]
                    for coin, (pu, m0) in pend["p_up"].get(p, {}).items():
                        y = 1 if snap["mid"][coin] > m0 else 0
                        sc["brier"] += C.brier(pu, y)
                        sc["n"] += 1
                        sc["hits"] += int((pu > 0.5) == bool(y)) if pu != 0.5 else 0
                        b = min(9, int(pu * 10))
                        sc["cal"][b][0] += 1
                        sc["cal"][b][1] += pu
                        sc["cal"][b][2] += y
                        res.setdefault(p, {})[coin] = y
                rec["resolved"] = res
            reqs = {p: {"state": C.state_text(snap, self.pf[p]), "questions": C.questions(snap, self.pf[p])}
                    for p in PLAYERS}
        with gzip.open(self.req_path, "at") as f:
            f.write(json.dumps({"cycle": rec["cycle"], "t": t, "requests": reqs}) + "\n")

        def ask(p):
            try:
                d, ms = self.up[p].call(reqs[p])
                return p, d, ms, None
            except Exception as e:  # noqa: BLE001
                return p, None, None, str(e)[:200]

        with ThreadPoolExecutor(2) as ex:
            outs = list(ex.map(ask, PLAYERS))
        with self.lock:
            self.S["cycle"] = rec["cycle"]
            pending = {"p_up": {}}
            rec["players"] = {}
            last = {}
            for p, d, ms, err in outs:
                pr = {"ms": ms, "error": err}
                self.S["score"][p]["calls"] += 1
                if err:
                    self.S["missed"][p] += 1
                    self.feed(f"no answer this cycle, positions kept ({err[:60]})", p)
                    rec["players"][p] = pr
                    last[p] = {"error": err, "t": t}
                    continue
                self.S["lat"][p] = (self.S["lat"][p] + [round(ms)])[-2000:]
                P = C.parse_answers(d.get("answers") or {})
                fills, changes = [], []
                for coin, label, cat, desc in hl.MARKETS:
                    x = P[label]
                    if x["p_up"] is not None:
                        pending["p_up"].setdefault(p, {})[coin] = (x["p_up"], snap["mid"][coin])
                    if x["pos"] is None:
                        continue
                    before = self.pf[p].side(coin)
                    bid, ask_ = snap["book"][coin]
                    f = self.pf[p].set_side(coin, x["pos"], bid, ask_, snap["ctx"][coin]["fee"], t)
                    if f:
                        fills += f
                        changes.append(f"{label} {C.SIDE_NAME[before]}→{C.SIDE_NAME[x['pos']]}")
                eq = self.pf[p].equity(snap["mid"])
                pr.update({"answers": P, "fills": fills, "equity": eq, "usage": d.get("usage")})
                rec["players"][p] = pr
                last[p] = {"t": t, "ms": ms, "answers": P, "changes": changes}
                name = "Eikos" if p == "eikos" else "Jev"
                self.feed(f"{name}: " + (", ".join(changes) if changes else "no changes") + f" · {ms / 1000:.2f} s", p)
            self.S["pending"] = pending
            self.S["last"] = last
            self.S["last_cycle_t"] = t
            self.prev_day = {c: snap["ctx"][c]["prev_day"] for c in hl.COINS}
            self.mids = dict(snap["mid"])
            self.save()
        with open(self.log_path, "a") as f:
            f.write(json.dumps(rec) + "\n")
        print(f"[{utc:%H:%M:%S}] cycle {rec['cycle']}: " + " | ".join(
            (f"{p} {rec['players'][p]['ms']:.0f} ms eq {rec['players'][p]['equity']:,.2f}" if rec['players'][p].get('ms')
             else f"{p} ERR") for p in PLAYERS), flush=True)

    # ---------- dashboard ----------
    def api_state(self):
        with self.lock:
            mids = dict(self.mids)
            S = self.S
            e = self.equities(mids) if len(mids) == len(hl.COINS) else {p: None for p in PLAYERS}
            markets = []
            for coin, label, cat, desc in hl.MARKETS:
                row = {"coin": coin, "label": label, "cat": cat, "desc": desc, "mid": mids.get(coin),
                       "chg24": C.pct(mids[coin], self.prev_day[coin]) if coin in mids and self.prev_day.get(coin) else None}
                for p in PLAYERS:
                    pf, lp = self.pf[p], (S["last"].get(p) or {}).get("answers", {}).get(label)
                    row[p] = {"side": pf.side(coin), "upnl": pf.upnl(coin, mids[coin]) if coin in mids else 0.0,
                              "entry": (pf.pos.get(coin) or {}).get("entry"),
                              "p_pos": lp["p_pos"] if lp else None, "p_up": lp["p_up"] if lp else None}
                markets.append(row)
            players = {}
            for p in PLAYERS:
                pf, sc, lat = self.pf[p], S["score"][p], S["lat"][p]
                players[p] = {"equity": e[p], "balance": pf.balance, "fees": pf.fees, "funding": pf.funding,
                              "realized": pf.realized, "trades": pf.trades, "open": len(pf.pos),
                              "brier": sc["brier"] / sc["n"] if sc["n"] else None, "n_fc": sc["n"],
                              "hit": sc["hits"] / sc["n"] if sc["n"] else None, "cal": sc["cal"],
                              "lat_last": lat[-1] if lat else None,
                              "lat_med": sorted(lat)[len(lat) // 2] if lat else None,
                              "missed": S["missed"][p], "last": S["last"].get(p, {}).get("changes"),
                              "last_error": S["last"].get(p, {}).get("error")}
            return {"now": time.time(), "start": S["start"], "end": S["end"], "next": S.get("next"),
                    "cycle": S["cycle"], "cycles_total": int(S["config"]["hours"] * 60 / C.CYCLE_MIN),
                    "running": self.running_cycle, "finished": S.get("finished"), "mids_t": self.mids_t,
                    "us_open": C.us_market_open(time.time()), "markets": markets, "players": players,
                    "feed": S["feed"][-40:], "start_equity": C.START_EQUITY, "notional": C.NOTIONAL,
                    "cycle_min": C.CYCLE_MIN}

    def api_history(self):
        with self.lock:
            h = list(self.S["history"])
        step = max(1, len(h) // 1500)
        return {"start_equity": C.START_EQUITY, "points": h[::step] + ([h[-1]] if h and (len(h) - 1) % step else [])}

    def serve(self):
        arena = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *x):
                pass

            def _send(self, code, obj, ctype="application/json"):
                raw = obj if isinstance(obj, bytes) else json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(raw)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                path = self.path.split("?")[0]
                try:
                    if path in ("/", "/index.html"):
                        return self._send(200, Path(arena.a.html).read_bytes(), "text/html; charset=utf-8")
                    if path == "/api/state":
                        return self._send(200, arena.api_state())
                    if path == "/api/history":
                        return self._send(200, arena.api_history())
                    return self._send(404, {"error": "not found"})
                except Exception as e:  # noqa: BLE001
                    return self._send(500, {"error": f"{type(e).__name__}: {e}"})

        ThreadingHTTPServer(("127.0.0.1", self.a.port), H).serve_forever()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--run-dir", required=True)
    ap.add_argument("--hours", type=float, default=72)
    ap.add_argument("--port", type=int, default=8340)
    ap.add_argument("--eikos-url", default=os.environ.get("EIKOS_URL", "http://127.0.0.1:8000/v1/evaluate"),
                    help="any TypeSafe-compatible /v1/evaluate endpoint (EIKOS_URL); EIKOS_API_KEY if it needs a key")
    ap.add_argument("--jev-url", default=None,
                    help="default: TypeSafe's API with TYPESAFE_API_KEY, else the Vercel AI Gateway")
    ap.add_argument("--jev-model", default=None,
                    help="default: jev-latest on TypeSafe's API, typesafe-ai/jev on the gateway")
    ap.add_argument("--jev-key-file", default=None, help="file holding the Jev key, instead of the environment")
    ap.add_argument("--dry-cycles", type=int, default=0, help="run N cycles back to back first (testing only)")
    ap.add_argument("--html", default=str(HERE / "arena.html"), help="dashboard page (display only)")
    a = ap.parse_args()
    ar = Arena(a)
    ar.save()
    threading.Thread(target=ar.price_loop, daemon=True).start()
    threading.Thread(target=ar.cycle_loop, daemon=True).start()
    print(f"arena on http://127.0.0.1:{a.port}  run dir {a.run_dir}", flush=True)
    ar.serve()


if __name__ == "__main__":
    main()
