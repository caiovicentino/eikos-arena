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
The full rules are in [`arena/RULES.md`](arena/RULES.md).

## What is here

| Path | What it is |
|---|---|
| `arena/RULES.md`, `arena/hl.py`, `arena/arena_core.py`, `arena/serve_used.py` | Byte-identical to the frozen copies that run the official match. Their hashes were taken at the start, in `arena/PREREG.sha256`. `serve_used.py` is the `serve.py` shipped with Eikos-27B. |
| `arena/arena.py` | The engine. Identical to the frozen copy except one line: the default path of the Jev key file. |
| `arena/arena.html` | The engine's own dashboard, served on 127.0.0.1. |
| `arena/publish_live.py` | Publishes a sanitized snapshot to a public Hugging Face dataset, for the public page. |
| `scripts/start_arena.sh` | Copies the rules and the engine into a run folder, hashes them, starts the engine. |
| `site/` | The public page: static HTML, CSS and JavaScript, deployed on Vercel. |

`arena.py`'s default ports are the official run's. `start_arena.sh` passes your own.

## Check the official run

```bash
cd arena && shasum -a 256 -c PREREG.sha256    # or: sha256sum -c PREREG.sha256
```

`RULES.md`, `hl.py`, `arena_core.py` and `serve_used.py` print `OK`. `arena.py` prints `FAILED` on purpose: its only
difference from the frozen copy is line 339, the default of `--jev-key-file`, which pointed to a folder on our
server and is `jev.key` here. The last line of the file is the start time (2026-09-24 05:12:11 UTC), not a hash.
The live page shows the rules hash (`943d01e0…`). When the run ends, on 2026-09-27 at 05:12 UTC, the logs listed in
`RULES.md` go to the dataset.

## Run your own

You need Python 3.9 or newer (the engine uses only the standard library), a GPU for Eikos, and a Vercel AI Gateway
key that can call `typesafe-ai/jev`. The official run serves Eikos-27B in bf16 on one 96 GB GPU; Eikos-4B runs with
the same commands on a smaller one.

```bash
# 1. Serve Eikos from a local copy of the model repo (see its model card)
bash serve_vllm.sh <MODEL_DIR> 8231 0.90
python serve.py --model <MODEL_DIR> --vllm-url http://127.0.0.1:8231 --port 8232

# 2. Start a 72-hour run. Keep the key file outside the repo, readable only by you (chmod 600)
JEV_KEY_FILE=~/.config/jev.key SERVE_PY=<MODEL_DIR>/serve.py scripts/start_arena.sh 72
# dashboard: http://127.0.0.1:8340

# 3. Optional: the public page
pip install -r arena/requirements.txt && huggingface-cli login
python arena/publish_live.py runs/<run> --repo <user>/<dataset> --deny-file ~/.config/arena-deny.txt --dry-run
python arena/publish_live.py runs/<run> --repo <user>/<dataset> --deny-file ~/.config/arena-deny.txt
# then set STATE_URL in site/app.js to your dataset and deploy site/ (for example with `vercel deploy`)
```

If the engine stops, run `python3 arena.py` again with the same `--run-dir` and flags: it resumes from
`arena_state.json`.

## Security

- **No secrets here.** The Jev key is read at runtime from the file you pass (`--jev-key-file`). The Hugging Face
  token comes from huggingface_hub's login. `.gitignore` excludes key files, `.env`, run folders and logs.
- **Local only.** The engine, its dashboard and the model servers listen on 127.0.0.1.
- **Static public page.** No backend and no keys: it reads a public JSON snapshot from Hugging Face and prices from
  Hyperliquid's public API, under a strict Content-Security-Policy (`site/vercel.json`).
- **Filtered publishing.** The publisher uploads only whitelisted fields. It refuses any snapshot that contains a
  key format, an IP address, a local path, an error trace or a string from your private deny file.
- **Evaluation only.** Jev's outputs are used for this evaluation, never for training.

## License

MIT, see [`LICENSE`](LICENSE). `site/lightweight-charts.js` is TradingView Lightweight Charts™ v4.2.3 under
Apache-2.0, see [`THIRD_PARTY_NOTICES.md`](THIRD_PARTY_NOTICES.md). Market data comes from Hyperliquid's public
API. Not financial advice.
