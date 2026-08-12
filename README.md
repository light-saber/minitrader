# MiniTrader

Long-term wealth building sub-agent for the Zerodha Kite account. Built on the Hermes Agent platform.

**Goal:** Beat the Nifty50 total return by ≥ 10 percentage points over each 30-day evaluation window. Long-term horizon. CNC product only. No FnO, no intraday, no speculative trading.

## How it works

Two parallel portfolios, both gated by the same pipeline:

| Portfolio | Capital | Purpose |
|---|---|---|
| **Live** (real Kite) | ₹5,000 first window | Proves the execution pipeline. Slow ramp. |
| **Paper** (virtual) | ₹50,000 default | Proves the strategy generates alpha. |

The pipeline runs in 6 phases:

1. **Universe** — read the monthly quality+momentum screener output
2. **Mandate filter** — apply your rules (CNC-only, no existing-holdings overlap, sector cap, dividend tiebreak)
3. **Chart gate** — for each candidate, compute 50/200 DMA + RSI(14) + volume, render PNG + interactive HTML charts
4. **Human decision** — present chart + verdict; you reply `go` / `skip` / `customize <qty/price>`
5. **Execution** — live: real `mcp__kite__place_order`; paper: write virtual fill to disk
6. **Daily digest** — 09:00 IST push to Discord with positions, P&L, alpha vs Nifty50

Every buy requires your explicit approval. The sub-agent never places a market order without `go`.

## Spec

See [`SPEC.md`](./SPEC.md) for the full specification — hard rules, sizing, pipeline details, state files, failure modes.

## Status

Under active development. See the GitHub issues for current work.

## License

MIT
