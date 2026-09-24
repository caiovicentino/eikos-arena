"""Checks a setup before a run: Python, Hyperliquid's public API, your Eikos endpoint and Jev. Each model gets one
tiny typed request with the same URL, model id and key the engine will use. Prints what is wrong and how to fix it.

usage: python3 scripts/check_setup.py [--jev-key-file FILE] [--skip-jev]
Reads EIKOS_URL, EIKOS_API_KEY, TYPESAFE_API_KEY or AI_GATEWAY_API_KEY, JEV_URL and JEV_MODEL from the environment."""
import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "arena"))

PROBE = {"state": "Setup check. A customer asks for a refund of USD 80. Policy: refunds up to USD 100 are approved "
                  "automatically.",
         "questions": {"approve": {"type": "boolean", "instructions": "Should the refund be approved automatically?",
                                   "criteria": {"true": "approve", "false": "do not approve"}},
                       "action": {"type": "choice", "instructions": "What should support do?",
                                  "criteria": {"refund": "refund now", "escalate": "ask a manager", "deny": "refuse"}}}}
REFUSED = "the key was refused (wrong, expired or without access)"
HINT = {401: REFUSED, 403: REFUSED, 404: "wrong URL (the path should end in /v1/evaluate or /v1/systemone)",
        422: "the server did not accept the request format", 429: "rate limited; wait a minute and retry",
        529: "the service is overloaded; retry later"}


def probe(name, url, key=None, extra=None, prep=None):
    body = json.dumps((prep or (lambda b: b))(PROBE) | (extra or {})).encode()
    headers = {"Content-Type": "application/json"} | ({"Authorization": f"Bearer {key}"} if key else {})
    t0 = time.time()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=body, headers=headers), timeout=120) as r:
            d = json.loads(r.read())
    except urllib.error.HTTPError as e:
        said = e.read()[:200].decode(errors="replace").strip()
        return False, (f"{name}: HTTP {e.code} from {url}: {HINT.get(e.code, e.reason)}"
                       + (f" · server said: {said}" if said else ""))
    except Exception as e:  # noqa: BLE001
        return False, f"{name}: cannot reach {url} ({type(e).__name__}: {e})"
    a = d.get("answers") or {}
    p = (a.get("approve") or {}).get("probability", (a.get("approve") or {}).get("noul"))
    choice = (a.get("action") or {}).get("choice")
    if p is None or choice is None:
        return False, f"{name}: {url} answered, but not with typed answers (answers.approve / answers.action)"
    return True, f"{name}: OK in {time.time() - t0:.2f} s (approve {float(p):.2f}, action {choice})"


def check_hyperliquid():
    import hl
    try:
        mids = hl.mids()
    except Exception as e:  # noqa: BLE001
        return False, f"Hyperliquid: cannot reach the public API ({e})"
    missing = [c for c in hl.COINS if c not in mids]
    if missing:
        return False, f"Hyperliquid: no price for {', '.join(missing)}"
    return True, f"Hyperliquid: OK ({len(mids)} markets priced)"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--jev-key-file", default=None, help="file holding the Jev key, instead of the environment")
    ap.add_argument("--skip-jev", action="store_true", help="check everything except Jev")
    a = ap.parse_args()
    py_ok = sys.version_info >= (3, 9)
    results = [(py_ok, f"Python {sys.version.split()[0]}" + ("" if py_ok else ": needs 3.9 or newer")),
               check_hyperliquid()]
    eikos_url = os.environ.get("EIKOS_URL", "http://127.0.0.1:8000/v1/evaluate")
    results.append(probe("Eikos", eikos_url, os.environ.get("EIKOS_API_KEY")))
    if not a.skip_jev:
        import arena  # the engine's own rule for Jev's URL, model id and key
        try:
            url, model, key = arena.jev_endpoint(a.jev_key_file)
            results.append(probe(f"Jev ({model})", url, key, {"model": model}, lambda b: arena.jev_body(b, url)))
        except SystemExit as e:
            results.append((False, f"Jev: {e}"))
    for ok, msg in results:
        print(("  ok    " if ok else "  FAIL  ") + msg)
    bad = sum(1 for ok, _ in results if not ok)
    print("Ready to start a run." if not bad else f"{bad} problem(s) to fix before a run.")
    sys.exit(1 if bad else 0)


if __name__ == "__main__":
    main()
