# Kite Trading Sub-Agent — Specification

**Author:** Momo (drafted for Sachin's review)
**Date:** 12 Aug 2026
**Status:** Draft, awaiting review
**Mandate source:** Discord thread `1537102886033428581` (market-street), 12 Aug 2026
**Case file:** `/root/.hermes/cases/vibetrading/`

---

## 1. Goal

Long-term wealth building. Beat the Nifty50 total return by ≥ 10 percentage points over the 30-day evaluation window. CNC product only. No FnO, no intraday, no speculative trading.

The system runs **two parallel portfolios**:

| Portfolio | Capital | Position rules | Purpose |
|---|---|---|---|
| **Live** (real Kite account) | ₹5,000 (first window) | Single position, slow ramp | Proves the execution pipeline works. Protects downside. |
| **Paper** (virtual, on-disk) | ₹10,000–₹50,000 (configurable per paper run) | Full multi-position rules | Proves the strategy generates alpha. Statistically meaningful sample. |

Both portfolios run the **same Phase 1–4 pipeline** (screener → mandate filter → chart gate → your approval). Only Phase 5 differs:

- **Live Phase 5:** `mcp__kite__place_order` with real CNC buy.
- **Paper Phase 5:** writes a virtual fill to `/root/.hermes/cases/vibetrading/paper_state.json` and `paper_trades.csv`, using the live LTP at the moment of "execution" as the fill price (no slippage assumption — flag this as a caveat).

**Phase 6 (daily digest)** reports both portfolios side-by-side, with Nifty50 as the benchmark for both.

This is **not** an autonomous trading system. Every buy (live or paper) requires an explicit human go-ahead. The sub-agent's job is to do the screening, sizing, charting, and bookkeeping — *you* make the entry decision in both cases.

### 1.2 — Trade lifecycle (8-step human-in-the-loop flow)

Confirmed by Sachin on 12 Aug 2026 as the canonical operating procedure for both portfolios:

1. **MiniTrader runs the screener** and picks stocks based on the mandate (Phase 2).
2. **MiniTrader recommends a single best pick** for the current capital, sized per §3, with the technical verdict + chart attached.
3. **Sachin approves** by replying `go` / `skip` / `customize <qty|price>` to the Discord buy prompt.
4. **MiniTrader executes the trade** (live or paper), attaches the GTT if warranted, and starts monitoring.
5. **MiniTrader decides if the stock should be sold.** A sell is proposed when ANY of: (a) the +20% target hits, (b) the -7% stop hits, (c) the trailing-stop logic fires, (d) holding-period and target-progress math says exit is optimal for hitting the 30-day alpha target.
6. **MiniTrader tells Sachin** with a Discord sell prompt that includes the same indicators + chart + reason.
7. **Sachin approves** by replying `go` / `skip` / `hold <N more days>`.
8. **Both monitor the outcome daily** via the 09:00 IST digest and any triggered GTT notifications.

**Sells always require explicit approval** — same gate as buys. The sub-agent never fires a sell order on its own, even when a GTT trigger condition is met. (Pre-attached GTTs that *fire automatically* on Kite's side are different — those are exits you already approved at entry time.)

**The 3-day minimum holding period** (Sachin, 12 Aug 2026) is enforced before any sell proposal. If a stock is held <3 days, the sub-agent will refuse to propose a sell no matter what the indicators say.

---

## 2. Hard rules (non-negotiable)

| Rule | Source |
|---|---|
| Product type **CNC** (delivery) only | Sachin, 12 Aug 2026 |
| Minimum holding period **3 days** before any sell | Sachin, 12 Aug 2026 |
| **Don't touch existing holdings** — sub-agent never sells, modifies, or squares off anything already in the Kite portfolio | Sachin, 12 Aug 2026 |
| Capital is **pre-committed for 30 days** — once placed in Kite, do not withdraw mid-window | Sachin, 12 Aug 2026 |
| GTTs **at sub-agent's discretion** — attached when warranted, skipped when not | Sachin, 12 Aug 2026 |
| All buys **require human approval** before order placement | This spec |
| **No FnO, no MIS/BO/CO** product types, ever | Sachin, 12 Aug 2026 |

---

## 3. Capital & position sizing

**Investment goal:** beat the Nifty50 total return by ≥ 10 percentage points over the 30-day window. (Sachin, 12 Aug 2026.)

**Starting capital for the first test window: ₹5,000.** (Sachin, 12 Aug 2026.)

Because ₹5,000 is below the threshold where the original multi-position rules make sense, the **live** portfolio uses single-position sizing rules. The **paper** portfolio uses the full multi-position rules from day one.

**Live portfolio (₹5K window):**

| Parameter | Value | Override |
|---|---|---|
| Total 30-day capital | ₹5,000 | — |
| Max number of open positions | 1 | — |
| Max % of capital in single position | 100% (one pick, one shot) | Per-trade |
| Min order value | ₹1,500 (Kite's actual floor) | — |
| Reserve for fees/buffer | ₹200 uninvested | — |
| Sector cap | not enforced (single position) | — |
| Order type | MARKET | Per-trade |

**Paper portfolio (configurable, default ₹50,000):**

| Parameter | Value | Override |
|---|---|---|
| Total capital | ₹50,000 (configurable at start of each paper run) | per run |
| Max number of open positions | 8 | — |
| Max % of capital in single position | 20% | Per-trade |
| Min order value | ₹1,500 | — |
| Reserve for fees/buffer | ₹500 uninvested | — |
| Sector cap | 35% per sector | — |
| Order type | MARKET (LTP at moment of approval is fill price) | Per-trade |

**When does live capital scale up to multi-position rules?**

When the paper portfolio demonstrates alpha over a meaningful window:

- ≥ 6 months of paper trading, AND
- Average alpha vs Nifty50 ≥ +5 pp per 30-day window (consistent, not one-shot), AND
- Paper portfolio max drawdown < 15%

All three conditions. Then live capital ramps from ₹5K → ₹25K → ₹50K+ on your decision, with the same multi-position rules.

The sub-agent computes **exact share quantity** from current LTP × cap, rounded down to whole shares, and shows the math in the buy prompt so you can override.

---

## 4. Pipeline (end to end)

```
┌──────────────────────────────────────────────────────────────────┐
│  PHASE 1 — Universe (passive, monthly)                           │
│  Cron `03bdb4203ba9` (1st of month, 11:00 IST) already runs      │
│  `~/.hermes/scripts/stock-screener/run_combined_screener.sh`.    │
│  Output: /root/.hermes/cron/output/stock_screener_with_momentum  │
│          .json                                                   │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│  PHASE 2 — Mandate filter (on-demand, when budget is fresh)      │
│  Reads Phase 1 JSON. Applies:                                     │
│  • Drop any symbol already in Kite holdings                      │
│  • Drop any symbol where .NS/.BO fundamentals fail quality gates │
│  • Drop FnO-eligible names if the sub-agent can't verify CNC-only│
│    liquidity                                                      │
│  • Apply sector cap on combined (existing + proposed) portfolio  │
│  • Tiebreak: dividend yield ≥ X% as soft preference              │
│  Output: ranked shortlist, ~5-10 candidates                      │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│  PHASE 3 — Per-candidate technical workup (the chart gate)       │
│  For each candidate:                                              │
│  • Pull 1-year daily OHLC from Kite historical_data API          │
│  • Compute: 50 DMA, 200 DMA, RSI(14), 20-day avg volume          │
│  • Render PNG chart: 90-day daily candles + EMA overlays +       │
│    volume pane with 20-day average line + horizontal bands       │
│    (recent swing high / swing low)                                │
│  • Render interactive HTML version (lightweight-charts) for      │
│    on-demand inspection                                          │
│  • Generate a technical verdict:                                 │
│      PASS conditions (ALL must hold):                            │
│        - Price > 50 DMA AND Price > 200 DMA  (uptrend)           │
│        - RSI(14) in [40, 70]                                     │
│        - Today's volume ≥ 1.0× 20-day average                   │
│        - No earnings/blackout event in next 5 days               │
│      Conditions for GTT attachment:                              │
│        - If RSI(14) > 65 OR entered near swing-high resistance   │
│          → attach GTT (-7% stop, +20% target)                    │
│        - If clean uptrend, RSI healthy → skip GTT, let it run    │
│      Result: one of BUY-READY / BUY-SKIP / WAIT                  │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│  PHASE 4 — Human decision (the gate)                             │
│  Sub-agent presents in Discord:                                  │
│  • Candidate symbol + company name + sector                      │
│  • Suggested quantity, exact ₹ amount                            │
│  • Pass/fail on each technical condition                          │
│  • GTT recommendation: attached? at what levels?                 │
│  • Chart PNG attached                                            │
│  • Interactive HTML link (file:// path on this box)              │
│  • Single-line prompt: `go` / `skip` / `customize <qty/price>`   │
│  Sub-agent WAITS for response. No autonomous buy.                │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│  PHASE 5 — Execution (only after `go`)                           │
│  • Pre-trade guard (fail-closed):                                │
│      - Symbol in today's Phase-2 shortlist?                      │
│      - Quantity * price ≤ max-order-value?                       │
│      - Buying power sufficient (margin check)?                   │
│      - Not duplicating an existing position?                     │
│      - Kite session active? (else: stop and ask Sachin to        │
│        re-authenticate via mcp_kite_login)                       │
│  • Call `mcp__kite__place_order`:                                │
│      exchange=NSE, tradingsymbol=<sym>,                          │
│      transaction_type=BUY, quantity=<n>, product=CNC,            │
│      order_type=MARKET (or LIMIT with your override)             │
│  • Record order_id + fill price to local state file              │
│  • If GTT was approved: call `mcp__kite__place_gtt_order`        │
│      trigger values: stop < fill * 0.93, target > fill * 1.20    │
│  • Send Discord confirmation with fill details + GTT IDs         │
└──────────────────────────────────────────────────────────────────┘
                              ↓
┌──────────────────────────────────────────────────────────────────┐
│  PHASE 6 — Ongoing tracking (passive, daily)                     │
│  Cron: morning digest at 09:00 IST                               │
│  • Current positions (qty, avg price, LTP, unrealised P&L)       │
│  • Realised P&L since window start                               │
│  • Capital deployed vs available                                 │
│  • GTT status (active / triggered / expired)                     │
│  • Upcoming earnings / corporate actions in holdings             │
│  • Today's screen if budget is fresh and shortlist is empty      │
│  No buy prompts in the digest — those only come from Phase 4.    │
└──────────────────────────────────────────────────────────────────┘
```

---

## 5. State files & book-keeping

**Live portfolio state:** `/root/.hermes/cases/vibetrading/state.json`

**Paper portfolio state:** `/root/.hermes/cases/vibetrading/paper_state.json`

Schema (same for both, structurally):
```json
{
  "window_start": "2026-08-12",
  "window_end":   "2026-09-11",
  "capital_committed": 5000,
  "positions": {
    "INFY": {
      "entry_date": "2026-08-13",
      "quantity": 3,
      "avg_price": 1600.00,
      "order_id": "...",
      "gtt_ids": ["..."],
      "sells": []
    }
  },
  "realised_pnl": 0.0,
  "sells": [],
  "pending_buys": [],
  "last_screener_run": "2026-08-01T11:00:00+05:30"
}
```

**Trade journals:**

- Live:    `/root/.hermes/cases/vibetrading/trades.csv`     columns: `date, symbol, side, qty, price, order_id, gtt_id, realised_pnl_delta`
- Paper:   `/root/.hermes/cases/vibetrading/paper_trades.csv` columns: same

This is the source of truth for Phase 6 P&L. `get_holdings` / `get_positions` from Kite are the cross-check for the live side, not the primary.

**Paper-trade fill price:** LTP at the exact moment you reply `go` to the Phase 4 prompt. No slippage, no spread model. This is a *known optimistic assumption* — flagged as a caveat. If we ever add slippage, it goes into `paper_trades.csv` as a separate column (`assumed_slippage_bps`).

---

## 6. Charts (the gate)

**Storage:** Local on this Linux box. No Discord attachments. You fetch via Tailscale.

**Paths:**
- PNG digests:    `/root/.hermes/cases/vibetrading/charts/png/<symbol>_<YYYYMMDD>.png`
- Interactive:    `/root/.hermes/cases/vibetrading/charts/html/<symbol>_<YYYYMMDD>.html`

**PNG renderer:** `mplfinance` (matplotlib wrapper). Standard chart, ~90-day daily candles, EMA50 + EMA200, volume pane with 20-day average, horizontal swing bands, annotation panel.

**HTML renderer:** `lightweight-charts` (TradingView's MIT library, ~45KB gzipped). Single-file HTML, opens in any browser. 1D/1W/1M toggle, indicator picker (EMA, RSI pane, Bollinger, VWAP), drawing tools.

**Libraries to install:**
- `mplfinance` (already installed in most Python environments)
- `kiteconnect` (for historical_data API; Kite MCP doesn't expose history in all setups)

The interactive HTML will use `https://unpkg.com/lightweight-charts@4.1.0/dist/lightweight-charts.standalone.production.js` (CDN). If you'd rather pin it offline, I can vendor the JS into `/root/.hermes/cases/vibetrading/charts/static/`.

---

## 7. Discord integration

- **Buy prompts:** posted in this channel (`<#1517387649520762960>`). (Sachin, 12 Aug 2026.) Sub-agent waits for response in the same thread.
- **Daily digest delivery:** posted in this same channel (`<#1517387649520762960>`). (Sachin, 12 Aug 2026.)
- **TTS:** Never. Discord text only.

**Daily digest contents (must include Nifty50 benchmark for the goal). Reports both portfolios:**

```
Window: 2026-08-12 → 2026-09-11 (Day N of 30)
Nifty50 same period:  +1.85%

─── LIVE (real Kite, ₹5,000) ───
Capital deployed:  ₹4,800 of ₹5,000
Positions:         INFY · 3 @ avg ₹1,600 · LTP ₹1,650
Unrealised P&L:    +₹150 (+3.13%)
Realised P&L:      ₹0
Total return:      +3.13%
Alpha vs Nifty50:  +1.28 pp   (target: +10 pp by day 30)
GTTs:              active 1 (target +20% / stop -7%)
Earnings ≤5 days:  none

─── PAPER (virtual, ₹50,000) ───
Capital deployed:  ₹38,400 of ₹50,000
Positions:         5 active — INFY, HAL, TCS, ABB, POWERGRID
                   (top: HAL +4.2%; bottom: POWERGRID -1.1%)
Unrealised P&L:    +₹1,820 (+3.64%)
Realised P&L:      -₹420 (one closed position)
Total return:      +2.80%
Alpha vs Nifty50:  +0.95 pp   (target: +10 pp by day 30)
GTTs:              3 active, 1 triggered this week
Earnings ≤5 days:  TCS (day 4)

─── Pipeline status ───
Screener:          last run 2026-08-01 (Day 1 of 30-day cycle)
Mandate filter:    6 candidates identified; 1 not yet reviewed
Notes:             …
```

The "alpha vs Nifty50" lines are the headline numbers — one per portfolio. Everything else is supporting context.

---

## 8. Failure modes & safe behaviour

| Failure | Sub-agent behaviour |
|---|---|
| Kite session expired | Stop, ask Sachin to re-authenticate via `mcp_kite_login`, do not place any order until `get_holdings` returns 200 |
| Screener JSON missing or stale (>45 days) | Run screener on demand first; if that fails too, halt and surface to Sachin |
| Technical verdict rejects all candidates today | Send a Discord message: "No BUY-READY candidates today. Capital unchanged." Don't auto-retry. |
| Pre-trade guard fails on any check | Refuse the order, show which check failed, do not retry without Sachin override |
| Network/API error during `place_order` | Refuse to retry automatically. Surface exact error to Sachin. |
| `mcp__kite__historical_data` returns incomplete data | Render chart with whatever is available, mark the missing range in the annotation panel |

The sub-agent **never** retries a failed order without explicit instruction. **Never** widens a GTT. **Never** overrides its own mandate guard.

---

## 9. Files to be created (when you green-light)

```
/root/.hermes/cases/vibetrading/
├── SPEC.md                         (this file)
├── state.json                      (live portfolio, created at window start)
├── paper_state.json                (paper portfolio, created at first paper run)
├── trades.csv                      (live fills)
├── paper_trades.csv                (paper fills)
├── subagent.py                     (main loop + state mgmt + dual-portfolio dispatcher, ~250 lines)
├── screener_filter.py              (Phase 2 mandate filter, ~120 lines)
├── technical_workup.py             (Phase 3 OHLC + indicators + verdict + earnings-cal filter, ~210 lines)
├── chart_render.py                 (PNG + HTML renderers, ~150 lines)
├── kite_exec.py                    (Phase 5 live guard + order + GTT, ~150 lines)
├── paper_exec.py                   (Phase 5 paper fill writer, ~80 lines)
├── daily_digest.py                 (Phase 6 dual-portfolio digest, ~180 lines)
├── earnings_calendar.py            (StockeZee fetcher + 5-day window builder, ~60 lines)
├── charts/
│   ├── png/
│   └── html/
└── logs/
```

Total: ~1,200 lines of Python. Each module independently testable. The two execution modules (`kite_exec.py`, `paper_exec.py`) share a common guard interface defined in `subagent.py`.

Cron jobs to register after build:
- `daily_digest` at 09:00 IST weekdays (reports both portfolios)
- (Optional) weekend screener-refresh trigger if window is mid-cycle

---

## 10. What this spec deliberately does NOT include

- **No options strategies.** No MIS, BO, CO. CNC only.
- **No intraday signals.** Charts are daily EOD. No 1-minute / 5-minute decisions.
- **No machine-learning alpha.** Selection is screener-driven (Piotroski + momentum), not learned.
- **No autonomous sells.** GTTs are pre-attached exits. Anything else waits for you.
- **No cross-broker abstraction.** Kite-only. If you migrate brokers, this whole thing is rewritten.
- **No live shortlist refresh during market hours.** Phase 3 runs once per capital-deployment decision, not on a loop.

---

## 11. Resolved questions (from Sachin, 12 Aug 2026)

1. **Capital amount?** → **Live: ₹5,000** for the first window (single-position rules, slow ramp). **Paper: ₹50,000** default (configurable per paper run, full multi-position rules). Both portfolios run the same Phase 1–4 pipeline. See §1 and §3.
2. **Discord channel for buy prompts?** → This channel (`<#1517387649520762960>`). Sub-agent tags paper trades as `[PAPER]` in the prompt.
3. **Daily digest channel?** → Same channel (`<#1517387649520762960>`). Digest reports both portfolios side-by-side.
4. **Existing portfolio import.** → Confirmed: Phase 2 reads `get_holdings` to identify "do not touch" and "do not duplicate" lists (live only — paper portfolio starts from zero).
5. **Earnings calendar source.** → **StockeZee** primary (`https://www.stockezee.com/results-calendar`), **FreeScreener** fallback (`https://freescreener.in/calendar`). Both free, no login, server-rendered HTML, scrape-friendly. Implementation: ~30 lines in `earnings_calendar.py`, called from `technical_workup.py`.
6. **Screener universe size.** → 5–10 candidates from Phase 2. Confirmed.

**Investment goal (Sachin, 12 Aug 2026):**
> Beat the Nifty50 total return by ≥ 10 percentage points over the 30-day window.

Tracked separately for live and paper. Surfaced as the headline in the daily digest.

**Risk ramp logic (Sachin, 12 Aug 2026):**
> ₹5K is to ensure I don't lose too much. On the side, paper trading runs at ₹10K–₹50K to validate the strategy. Over time, increase live amount.

Encoded in §3: live stays at single-position rules until paper portfolio demonstrates alpha (≥ 6 months, ≥ +5 pp/window avg, max drawdown < 15%). Then live capital scales to multi-position rules on your call.

---

**All questions resolved. Green-light to build.**
