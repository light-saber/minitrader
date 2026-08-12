"""Phase 3 chart gate: OHLC indicators, technical verdict, and GTT sizing.

For each Phase 2 shortlist candidate this module pulls daily OHLC, computes
50/200 EMA + RSI(14) + 20-day average volume, and produces a BUY-READY /
BUY-SKIP / WAIT verdict against the PASS conditions in SPEC.md §4 (Phase 3):

    - Price > 50 DMA AND Price > 200 DMA (uptrend)
    - RSI(14) in [40, 70]
    - Today's volume >= 1.0x 20-day average
    - No earnings/blackout event in next 5 days (earnings_calendar.get_blackout_set)

Verdict mapping (this module's documented interpretation of the spec, since
SPEC.md names the three outcomes but doesn't enumerate every failure path):
    - All four conditions hold                          -> BUY-READY
    - Only the earnings blackout blocks an otherwise-     -> WAIT
      passing setup (temporary, revisit after results)
    - Downtrend, or RSI < 40 (momentum faded)             -> BUY-SKIP
    - Overbought (RSI > 70) or soft volume, trend intact  -> WAIT

`should_attach_gtt()` encodes the §4 GTT-attachment rule: attach (-7% stop,
+20% target) when RSI(14) > 65 or price is within 3% of its recent
(60-trading-day) swing high; otherwise let a clean uptrend run with no GTT.
"""

from __future__ import annotations

import logging
import zlib
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

import earnings_calendar

logger = logging.getLogger(__name__)

RSI_PERIOD = 14
EMA_FAST_SPAN = 50
EMA_SLOW_SPAN = 200
VOLUME_AVG_WINDOW = 20
EARNINGS_LOOKAHEAD_DAYS = 5

RSI_LOWER_BOUND = 40
RSI_UPPER_BOUND = 70
RSI_GTT_TRIGGER = 65
SWING_LOOKBACK_DAYS = 60
SWING_HIGH_PROXIMITY_PCT = 0.03
GTT_STOP_PCT = 0.07
GTT_TARGET_PCT = 0.20

VERDICT_BUY_READY = "BUY-READY"
VERDICT_BUY_SKIP = "BUY-SKIP"
VERDICT_WAIT = "WAIT"


def fetch_ohlc(symbol: str, days: int = 365) -> pd.DataFrame:
    """Return daily OHLCV data for `symbol` over the trailing `days` days.

    TODO(kite): this is a stub. Replace the body with a real call to
    `kite.historical_data()` (Kite Connect) once a live/authenticated Kite
    session is wired up — that session check belongs in subagent.py, not
    here. This function must stay import-safe and callable with zero live
    dependencies (constraint 2 of the build brief), so `chart_render.py` and
    this module's own indicator/verdict logic can be smoke-tested without a
    Kite session. The data below is a deterministic (seeded on `symbol`),
    realistic-looking synthetic random walk — it is NOT real market data.

    Args:
        symbol: NSE trading symbol, e.g. ``"INFY"``.
        days: Number of trailing calendar days to span (business days only
            are actually generated).

    Returns:
        A DataFrame indexed by date with columns
        ``open, high, low, close, volume``, oldest row first.
    """
    end = date.today()
    dates = pd.bdate_range(end=end, periods=days)

    seed = zlib.crc32(symbol.encode("utf-8"))
    rng = np.random.default_rng(seed)

    base_price = 100 + (seed % 2000)
    daily_returns = rng.normal(loc=0.0004, scale=0.018, size=len(dates))
    close = base_price * np.cumprod(1 + daily_returns)
    open_ = close * (1 + rng.normal(0, 0.004, size=len(dates)))
    high = np.maximum(open_, close) * (1 + np.abs(rng.normal(0, 0.006, size=len(dates))))
    low = np.minimum(open_, close) * (1 - np.abs(rng.normal(0, 0.006, size=len(dates))))
    volume = rng.integers(low=100_000, high=2_000_000, size=len(dates))

    return pd.DataFrame(
        {"open": open_, "high": high, "low": low, "close": close, "volume": volume},
        index=pd.Index(dates, name="date"),
    )


def _rsi(close: pd.Series, period: int = RSI_PERIOD) -> pd.Series:
    """Compute Wilder's RSI for a close-price series.

    Args:
        close: Close price series, oldest first.
        period: Lookback period (default 14).

    Returns:
        RSI series, same index as `close`. Neutral (50) where undefined.
    """
    delta = close.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    avg_loss = loss.ewm(alpha=1 / period, min_periods=period, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    rsi = 100 - (100 / (1 + rs))
    return rsi.fillna(50.0)


def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Add `ema_50`, `ema_200`, `rsi_14`, and `vol_avg_20` columns.

    Args:
        df: OHLCV DataFrame as returned by `fetch_ohlc` (needs `close` and
            `volume` columns).

    Returns:
        A copy of `df` with the four indicator columns added.
    """
    out = df.copy()
    out["ema_50"] = out["close"].ewm(span=EMA_FAST_SPAN, adjust=False).mean()
    out["ema_200"] = out["close"].ewm(span=EMA_SLOW_SPAN, adjust=False).mean()
    out["rsi_14"] = _rsi(out["close"], period=RSI_PERIOD)
    out["vol_avg_20"] = out["volume"].rolling(window=VOLUME_AVG_WINDOW, min_periods=1).mean()
    return out


def _is_blacked_out(symbol: str) -> bool:
    """Check the earnings blackout set for `symbol`, failing closed on error.

    Args:
        symbol: NSE trading symbol.

    Returns:
        True if `symbol` reports within the blackout window, or if the
        earnings calendar could not be fetched from any source (fail-closed:
        an unknown earnings date is treated as a reason to wait).
    """
    try:
        blackout_set = earnings_calendar.get_blackout_set(EARNINGS_LOOKAHEAD_DAYS)
    except RuntimeError as exc:
        logger.warning(
            "earnings blackout check unavailable (%s) — failing closed, treating %s as blacked out",
            exc,
            symbol,
        )
        return True
    return symbol.upper() in blackout_set


def verdict(symbol: str, df: pd.DataFrame) -> str:
    """Classify `symbol` as BUY-READY / BUY-SKIP / WAIT per SPEC.md §4 Phase 3.

    Expects `df` to already carry the indicator columns from
    `compute_indicators()`.

    Args:
        symbol: NSE trading symbol (used for the earnings blackout lookup).
        df: Indicator-enriched OHLCV DataFrame, oldest row first.

    Returns:
        One of `VERDICT_BUY_READY`, `VERDICT_BUY_SKIP`, `VERDICT_WAIT`.
    """
    latest = df.iloc[-1]
    price = latest["close"]

    uptrend = bool(price > latest["ema_50"] and price > latest["ema_200"])
    rsi = float(latest["rsi_14"])
    rsi_in_range = RSI_LOWER_BOUND <= rsi <= RSI_UPPER_BOUND
    vol_ok = bool(latest["volume"] >= latest["vol_avg_20"])

    blacked_out = _is_blacked_out(symbol)

    conditions = {
        "uptrend (price > 50DMA & 200DMA)": uptrend,
        f"RSI(14) in [{RSI_LOWER_BOUND}, {RSI_UPPER_BOUND}]": rsi_in_range,
        "volume >= 20d avg": vol_ok,
        "no earnings in next 5 trading days": not blacked_out,
    }
    logger.info("verdict conditions for %s: %s", symbol, conditions)

    if uptrend and rsi_in_range and vol_ok and not blacked_out:
        return VERDICT_BUY_READY
    if uptrend and rsi_in_range and vol_ok and blacked_out:
        return VERDICT_WAIT
    if not uptrend or rsi < RSI_LOWER_BOUND:
        return VERDICT_BUY_SKIP
    return VERDICT_WAIT


def should_attach_gtt(verdict_str: str, df: pd.DataFrame) -> tuple[bool, dict[str, Any]]:
    """Decide whether to attach a GTT and at what levels, per SPEC.md §4.

    Only relevant for a BUY-READY verdict. Attaches when RSI(14) > 65 or the
    latest close is within 3% of its 60-trading-day swing high; skips GTT
    (let the position run) for a clean uptrend with healthy RSI.

    Args:
        verdict_str: The result of `verdict()` for this candidate.
        df: Indicator-enriched OHLCV DataFrame, oldest row first.

    Returns:
        A ``(attach, levels)`` tuple. `levels` is empty when `attach` is
        False; otherwise it has ``stop``, ``target``, ``swing_high``,
        ``swing_low`` (all rounded to 2 decimals).
    """
    if verdict_str != VERDICT_BUY_READY:
        return False, {}

    latest = df.iloc[-1]
    price = float(latest["close"])
    rsi = float(latest["rsi_14"])

    lookback = df.iloc[-(SWING_LOOKBACK_DAYS + 1) : -1] if len(df) > 1 else df.iloc[:0]
    swing_high = float(lookback["high"].max()) if not lookback.empty else price
    swing_low = float(lookback["low"].min()) if not lookback.empty else price

    near_resistance = swing_high > 0 and (swing_high - price) / swing_high <= SWING_HIGH_PROXIMITY_PCT
    attach = rsi > RSI_GTT_TRIGGER or near_resistance

    if not attach:
        return False, {}

    levels = {
        "stop": round(price * (1 - GTT_STOP_PCT), 2),
        "target": round(price * (1 + GTT_TARGET_PCT), 2),
        "swing_high": round(swing_high, 2),
        "swing_low": round(swing_low, 2),
    }
    return True, levels


def _smoke_test() -> None:
    """Build a synthetic df, run the full pipeline, and print the verdict."""
    symbol = "INFY"
    df = fetch_ohlc(symbol, days=365)
    df = compute_indicators(df)
    v = verdict(symbol, df)
    attach, levels = should_attach_gtt(v, df)
    latest = df.iloc[-1]
    print(f"symbol={symbol} price={latest['close']:.2f} rsi14={latest['rsi_14']:.1f}")
    print(f"ema50={latest['ema_50']:.2f} ema200={latest['ema_200']:.2f} vol_avg_20={latest['vol_avg_20']:.0f}")
    print(f"verdict={v}")
    print(f"attach_gtt={attach} levels={levels}")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    _smoke_test()
