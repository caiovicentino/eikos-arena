# Eikos Arena

Two decision models trade 14 Hyperliquid perpetuals with $10,000 of paper money each: **Eikos-27B** (open weights,
MIT) against **Jev** (TypeSafe's decision API). Every 5 minutes both get the same market snapshot and the same 28
questions. Real prices, simulated money, no orders.

**Live:** [eikos-arena.vercel.app](https://eikos-arena.vercel.app) ·
**Data:** [datasets/caiovicentino1/eikos-arena](https://huggingface.co/datasets/caiovicentino1/eikos-arena) ·
**Model:** [caiovicentino1/Eikos-27B](https://huggingface.co/caiovicentino1/Eikos-27B) ·
**Model code:** [caiovicentino/eikos](https://github.com/caiovicentino/eikos)

> Eikos was built to apply stated rules to a case, not to predict prices, so this is a stress test, not a trading
> product. Not financial advice.

## Run your own arena

You need:

- Python 3.9 or newer. The engine uses only the standard library.
- An Eikos endpoint. Serve Eikos yourself with the model's own `serve.py`: Eikos-27B in bf16 fits one 96 GB GPU, and
  Eikos-4B runs on a smaller one. Any other TypeSafe-compatible `/v1/evaluate` endpoint works too.
- A key for Jev: a TypeSafe API key ([console.typesafe.ai](https://console.typesafe.ai)) or a Vercel AI Gateway key
  (model `typesafe-ai/jev`).

```bash
git clone https://github.com/caiovicentino/eikos-arena && cd eikos-arena

# 1. Serve Eikos, from a local copy of the model repo (as on its model card)
bash serve_vllm.sh <MODEL_DIR> 8001
python serve.py --model <MODEL_DIR> --vllm-url http://127.0.0.1:8001 --port 8000

# 2. Your Jev key, one of these
export TYPESAFE_API_KEY=...       # TypeSafe's API
export AI_GATEWAY_API_KEY=...     # or the Vercel AI Gateway

# 3. Check the setup, then start a 72-hour run
python3 scripts/check_setup.py
scripts/start_arena.sh 72
# dashboard: http://127.0.0.1:8340
```

`check_setup.py` sends one tiny typed request to Eikos and one to Jev, with the same URLs and keys the engine will use,
and says what to fix if something fails. `start_arena.sh` runs it first, copies the rules and the engine into a new
folder under `runs/`, hashes them into `PREREG.sha256` before the first decision, and starts the engine.

| Setting | Default | What it does |
|---|---|---|
| `TYPESAFE_API_KEY` | | Jev through TypeSafe's API: `https://api.typesafe.ai/v1/systemone`, model `jev-latest`. Used when set. |
| `AI_GATEWAY_API_KEY` | | Jev through the Vercel AI Gateway: `https://ai-gateway.vercel.sh/v1/evaluate`, model `typesafe-ai/jev`. |
| `JEV_KEY_FILE` | | A file holding either key, instead of the environment. Keep it `chmod 600` and outside the repo. |
| `JEV_URL`, `JEV_MODEL` | as above | Another Jev endpoint or model version. |
| `EIKOS_URL` | `http://127.0.0.1:8000/v1/evaluate` | Your Eikos endpoint. |
| `EIKOS_API_KEY` | | Only if your Eikos endpoint needs a key. |
| `PORT` | `8340` | Dashboard and API port, on 127.0.0.1. |
| `RUNS` | `runs/` | Where run folders go. |

If the engine stops, `cd` into its run folder and run `python3 arena.py --run-dir .` with the same settings: it resumes
from `arena_state.json`.

### Your own public page (optional)

```bash
pip install -r arena/requirements.txt && huggingface-cli login
python3 arena/publish_live.py runs/<run> --repo <user>/<dataset> --deny-file ~/.config/arena-deny.txt --dry-run
python3 arena/publish_live.py runs/<run> --repo <user>/<dataset> --deny-file ~/.config/arena-deny.txt
```

The publisher creates the dataset if needed and uploads `live/state.json` after every decision. Then set `STATE_URL`
at the top of `site/app.js` to `https://huggingface.co/datasets/<user>/<dataset>/resolve/main/live/state.json` and
deploy `site/` as a static site (for example with `vercel deploy`). The deny file is yours and private: one string per
line (your server's address, hostnames, folder names) that must never appear in a published snapshot.

## How a round works

1. **Snapshot** (`hl.py`). For each market: best bid and ask, the last 12 five-minute and 24 one-hour closes, price
   changes, funding, open interest, volume, spread and taker fee. The 14 markets are 5 crypto perps and 9 HIP-3 perps
   on the `xyz` dex (4 tokenized stocks, 2 indices, 3 commodities).
2. **Same request to both models** (`arena_core.py`). One state text and 28 typed questions: `pos_<market>` (long,
   flat or short, $2,000 per position) and `up_<market>` (will the price be higher in 5 minutes?). Only each
   player's own portfolio lines differ.
3. **Fills** at that snapshot's bid and ask with Hyperliquid's taker fee, so answering faster never helps. Open
   positions pay or receive Hyperliquid's hourly funding.
4. **Scoring.** Equity, and the Brier score of the `up_` probabilities (lower is better; always answering 50%
   scores 0.250).

Positions share one account (cross margin). With all 14 open, exposure is $28,000, 2.8x the starting $10,000.

## What is here

| Path | What it is |
|---|---|
| `arena/arena.py` | The engine: rounds, the requests to both models, fills, scoring, the local dashboard and its API. |
| `arena/arena_core.py` | The rules in code: state text, questions, paper accounting, Brier score. |
| `arena/hl.py` | Hyperliquid's public API: prices, candles, book, funding, fees. |
| `arena/RULES.md` | The rules of the official match. |
| `arena/arena.html` | The local dashboard. |
| `arena/publish_live.py` | Optional: publishes a sanitized snapshot to a public Hugging Face dataset. |
| `scripts/check_setup.py` | Checks Python, Hyperliquid, your Eikos endpoint and your Jev key. |
| `scripts/start_arena.sh` | Checks the setup, freezes and hashes the code into a run folder, starts the engine. |
| `site/` | The public page of the official match: static HTML, CSS and JavaScript. |

## The official match

The official match started on 2026-09-24 at 05:12:11 UTC and runs for 72 hours. `arena/RULES.md` is the rules file
hashed before its first decision: `shasum -a 256 arena/RULES.md` prints `943d01e0…`, the hash the live page shows.
`hl.py` and `arena_core.py` are byte-identical to the files running it. `arena.py` adds two things for your own
runs: keys and endpoints from the environment and, on TypeSafe's own API, the yes/no question under TypeSafe's name
(`noul`; the Vercel AI Gateway and Eikos call it `boolean`). When the match ends, on 2026-09-27 at 05:12 UTC, the
logs listed in `RULES.md`, including the hashes of the code, go to the dataset.

## Security

- **No secrets here.** Keys come from the environment or a key file when the engine starts, never from the code.
  Requests are logged without their headers. `.gitignore` excludes key files, `.env`, run folders and logs.
- **Local only.** The engine and its dashboard listen on 127.0.0.1.
- **Static public page.** No backend and no keys: it reads a public JSON snapshot from Hugging Face and prices from
  Hyperliquid's public API, under a strict Content-Security-Policy (`site/vercel.json`).
- **Filtered publishing.** The publisher uploads only whitelisted fields. It refuses any snapshot that contains a
  key format, an IP address, a local path, an error trace or a string from your deny file.
- **Evaluation only.** Jev's outputs are used for this evaluation, never for training.

## License

MIT, see [`LICENSE`](LICENSE). `site/lightweight-charts.js` is TradingView Lightweight Charts™ v4.2.3 under
Apache-2.0, see [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). Market data comes from Hyperliquid's public
API. Not financial advice.
