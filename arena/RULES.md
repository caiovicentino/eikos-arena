# Eikos vs Jev: live paper-trading arena on Hyperliquid (rules fixed before the start)

**Paper money, real prices.** No account and no orders. Market data comes from Hyperliquid's public info API. Not financial advice.

## Setup

- **Players:**
  - **Eikos-27B:** open weights, MIT. bf16, served by the released `serve.py` over vLLM 0.30 on one RTX PRO 6000. The only change to `serve.py` is HTTP keep-alive, which does not change any decision.
  - **Jev:** `typesafe-ai/jev` through the Vercel AI Gateway.
- **Money:** $10,000 of simulated money each.
- **Markets (14):**
  - Crypto: BTC, ETH, SOL, HYPE, XRP.
  - Tokenized stocks: NVDA, TSLA, AAPL, META.
  - Indices: SP500, XYZ100.
  - Commodities: GOLD, WTI oil (CL), SILVER.
  - Crypto trades on Hyperliquid's own perps; the other 9 markets are HIP-3 perps on the `xyz` dex. All trade 24/7; the state tells the models whether the US stock market is open.
- **Duration:** 72 hours from the start time in `arena_state.json`.
- **Decision times:** every 5 minutes, 2 seconds after each :00, :05, :10 … UTC.

## Each decision

1. **Snapshot.** For each market, one snapshot of:
   - top of book (best bid and ask);
   - the last 12 five-minute closes and the last 24 one-hour closes;
   - 5-minute, 1-hour and 24-hour change;
   - funding rate, open interest, 24-hour volume, spread and taker fee.
2. **Same request to both models.** One request per player (`POST /v1/evaluate`), built by `arena_core.py` from the same template. The only lines that differ are each player's own portfolio (equity, P&L, fees, open positions).
3. **Two questions per market (28 in total):**
   - `pos_<market>` (choice): long, flat or short for the next 5 minutes. Each position is $2,000 of notional and is held until the next decision.
   - `up_<market>` (boolean): will the price be higher at the next decision than now?
4. **Fills.** All fills use the snapshot's prices:
   - buys fill at the best ask and sells at the best bid;
   - fills happen at the same prices for both players, so answer speed never changes a fill;
   - no slippage beyond the top of book.
5. **Fees.** The taker fee is computed from live metadata, following Hyperliquid's docs:
   - 0.045% on the crypto perps;
   - 0.009% on the `xyz` markets (base fee × 2 for deployer fee scale 1.0, × 0.1 for growth mode).
6. **Funding.** At the first decision of each hour, open positions pay or receive the market's current hourly funding rate × oracle price × size.
7. **Missing answers.** If a player gives no answer after retries within the cycle:
   - its positions stay as they are;
   - its forecasts for that cycle are not scored;
   - the miss is counted and shown.

## Scoring

- **Equity:** balance plus unrealized P&L at the mid price. Positions still open at the end are valued at the mid, with no closing fee.
- **Forecasts:** the Brier score of the `up_` probabilities against the next decision's mid ("the same" counts as "not higher"). Lower is better; always answering 50% scores 0.250. The hit rate is also reported.
- **Noise:** over 72 hours, P&L is dominated by noise. It says little about trading skill. The forecast score gathers ~12,000 forecasts per player, so it is the more informative number.

## Logs (published after the run)

- `cycles.jsonl`: every snapshot, answer (all probabilities), fill, funding payment and equity.
- `requests.jsonl.gz`: every request sent to either model.
- `arena_state.json`: portfolios, scores, equity history.
- `PREREG.sha256`: hashes of these rules and of the code, taken when the run starts.

Nothing in these rules changes after the start. If the engine restarts, it continues from the saved state, and the downtime is logged. Jev's outputs are used for this evaluation only, never for training.
