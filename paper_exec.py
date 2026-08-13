"""Phase 5 paper execution: virtual fill writer for the paper portfolio.

Mirror of `kite_exec.py` for the paper book — same pre-trade guard
interface (SPEC.md §9: "the two execution modules share a common guard
interface"; this module re-exports `kite_exec.pretrade_guard` rather than
duplicating it, since the guard is pure Python with no Kite dependency) and
the same `trades.csv` row shape, but it never calls any MCP tool.

Per SPEC.md §1/§5: a paper "fill" is a virtual position written to
`paper_state.json` / `paper_trades.csv`, using the live LTP at the moment
Sachin replies `go` to the Phase 4 prompt as the fill price — no slippage,
no spread model. This is a known optimistic assumption, flagged here and in
SPEC.md §5 as a caveat.
"""

from __future__ import annotations

import argparse
import csv
import logging
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import subagent
import kite_session
from kite_exec import TRADE_CSV_COLUMNS, pretrade_guard  # shared guard interface, SPEC.md §9

logger = logging.getLogger(__name__)

__all__ = ["pretrade_guard", "place_paper_buy", "place_paper_buy_at_ltp", "place_paper_gtt", "record_paper_fill"]

# Sanity band for an incoming LTP before we record a paper fill. A returned
# price that differs from the 5-day median close by more than this fraction
# is treated as bad dispatcher/test data rather than real market data and
# triggers `LTPPriceOutOfBandError` (see fix/paper-infy-entry-price — 2026-08-12
# entry was recorded at ₹1,500.25 vs actual ₹1,176.10 close, ~+27.5% drift).
LTP_SANITY_TOLERANCE = 0.15
LTP_SANITY_LOOKBACK_DAYS = 5


def _new_paper_state() -> dict[str, Any]:
    """Build a fresh paper `paper_state.json` payload for a new run.

    Returns:
        A state dict matching the SPEC.md §5 schema.
    """
    today = date.today()
    return {
        "window_start": today.isoformat(),
        "window_end": (today + timedelta(days=subagent.WINDOW_DAYS)).isoformat(),
        "capital_committed": subagent.PAPER_RULES["total_capital"],
        "positions": {},
        "realised_pnl": 0.0,
        "sells": [],
        "pending_buys": [],
        "last_screener_run": None,
    }


def place_paper_buy(symbol: str, qty: int, ltp: float, product: str = "CNC") -> dict[str, Any]:
    """"Place" a virtual paper buy — no real order, no MCP call.

    Generates a synthetic order id and returns a result dict shaped like
    `kite_exec.place_live_buy()`'s return value so callers (`subagent.dispatch`)
    can treat both portfolios uniformly. Fill price is exactly `ltp` — the
    caller (subagent, at the moment of human `go`) is responsible for
    supplying a fresh LTP; no slippage is modeled (SPEC.md §5 caveat).

    Args:
        symbol: NSE trading symbol.
        qty: Quantity to "buy".
        ltp: Last traded price at the moment of approval — used as the fill price.
        product: Must be ``"CNC"`` — MiniTrader never places FnO/MIS/BO/CO orders,
            paper included.

    Returns:
        ``{"order_id", "status", "average_price", "quantity", "symbol"}``.

    Raises:
        ValueError: If `product` isn't CNC.
    """
    if product != "CNC":
        raise ValueError(f"product must be CNC, got {product!r} — MiniTrader never places FnO/MIS/BO/CO orders")

    order_id = f"PAPER-{date.today():%Y%m%d}-{symbol}-{uuid.uuid4().hex[:8]}"
    result = {
        "order_id": order_id,
        "status": "COMPLETE",
        "average_price": ltp,
        "quantity": qty,
        "symbol": symbol,
    }
    logger.info("paper buy 'placed' (virtual, no slippage assumed): %s", result)
    return result


def _validate_ltp_against_history(symbol: str, ltp: float) -> None:
    """Cross-check a returned LTP against the recent real-market close range.

    Pulls the trailing `LTP_SANITY_LOOKBACK_DAYS` daily closes via
    `kite_session.historical_data()` and refuses the fill if `ltp` is more than
    `LTP_SANITY_TOLERANCE` away from the median of those closes.

    Args:
        symbol: NSE trading symbol.
        ltp: Candidate LTP to validate (typically from `kite_session.get_ltp`).

    Raises:
        kite_session.KiteSessionExpired: If the historical-data fetch is not
            available — propagated so the caller can surface a re-auth prompt
            rather than silently substituting.
        kite_session.LTPPriceOutOfBandError: If `ltp` is more than
            `LTP_SANITY_TOLERANCE` from the 5-day median close. This is the
            guard that catches dispatcher/test data leaking into the paper book.
    """
    end = date.today()
    start = end - timedelta(days=LTP_SANITY_LOOKBACK_DAYS)
    try:
        df = kite_session.historical_data(symbol, start, end)
    except kite_session.KiteSessionExpired:
        # No live reference data — refuse the fill rather than guess.
        raise
    closes = df["close"].astype(float).tolist()
    if not closes:
        raise kite_session.LTPPriceOutOfBandError(
            f"no recent closes available for {symbol} — cannot validate LTP ₹{ltp:.2f}"
        )
    sorted_closes = sorted(closes)
    median = sorted_closes[len(sorted_closes) // 2]
    if median <= 0:
        raise kite_session.LTPPriceOutOfBandError(
            f"5-day median close for {symbol} is non-positive ({median}); refusing to validate LTP ₹{ltp:.2f}"
        )
    drift = abs(ltp - median) / median
    if drift > LTP_SANITY_TOLERANCE:
        raise kite_session.LTPPriceOutOfBandError(
            f"LTP ₹{ltp:.2f} for {symbol} drifts {drift:.1%} from 5-day median close "
            f"₹{median:.2f} (band ±{LTP_SANITY_TOLERANCE:.0%}) — refusing to record fill. "
            f"Re-confirm after the next live quote; do not override without explicit Sachin approval."
        )


def place_paper_buy_at_ltp(symbol: str, quantity: int) -> dict[str, Any]:
    """Place a paper buy using live LTP from `kite_session`.

    Pulls live LTP via `kite_session.get_ltp`, then cross-checks it against
    the 5-day median close (`_validate_ltp_against_history`) to refuse any
    dispatcher/test data that drifts more than `LTP_SANITY_TOLERANCE` from
    the real-market band. Empty LTP responses or prices outside the band
    propagate as exceptions — never silently substituted.

    Args:
        symbol: NSE trading symbol.
        quantity: Quantity to buy in the paper portfolio.

    Returns:
        The virtual fill result returned by `place_paper_buy`.

    Raises:
        kite_session.KiteSessionExpired: If no current LTP is available,
            including when the Kite session has expired, OR if the
            historical-data cross-check cannot reach the live session.
        kite_session.LTPPriceOutOfBandError: If the returned LTP is more than
            `LTP_SANITY_TOLERANCE` away from the 5-day median close.
    """
    result = kite_session.get_ltp([symbol])
    if not result:
        raise kite_session.KiteSessionExpired(
            "Kite session expired or no live LTP available — re-authenticate before placing a paper fill."
        )
    ltp = float(result[symbol.upper()])
    _validate_ltp_against_history(symbol, ltp)
    return place_paper_buy(symbol, quantity, ltp)


def place_paper_gtt(symbol: str, qty: int, trigger_price: float) -> dict[str, Any]:
    """Track a paper GTT trigger without submitting or auto-resolving an order.

    Args:
        symbol: NSE trading symbol covered by the trigger.
        qty: Quantity covered by the paper GTT.
        trigger_price: Price that a future daily paper-GTT check should watch.

    Returns:
        A tracked paper-GTT record with a synthetic identifier.
    """
    if qty <= 0:
        raise ValueError("qty must be positive")
    if trigger_price <= 0:
        raise ValueError("trigger_price must be positive")
    gtt = {
        "gtt_id": f"PAPER_GTT_{uuid.uuid4().hex}",
        "symbol": symbol.upper(),
        "quantity": qty,
        "trigger_price": float(trigger_price),
        "status": "TRACKED",
        "created_at": date.today().isoformat(),
    }
    state = subagent.load_state(subagent.PAPER_STATE_PATH) or _new_paper_state()
    state.setdefault("paper_gtts", []).append(gtt)
    subagent.save_state(subagent.PAPER_STATE_PATH, state)
    logger.info("tracked paper GTT (no automatic resolution): %s", gtt)
    return gtt


def _append_trade_row(path: Path, row: dict[str, Any]) -> None:
    """Append one row to a trade journal CSV, writing the header if new.

    Args:
        path: Destination CSV path.
        row: Must contain exactly the keys in `TRADE_CSV_COLUMNS`.
    """
    is_new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_CSV_COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def record_paper_fill(order_id: str, fill_price: float, qty: int, symbol: str, gtt_ids: Optional[list[str]] = None) -> None:
    """Record a paper fill: append to `paper_trades.csv` and update `paper_state.json`.

    Symmetric to `kite_exec.record_fill()`.

    Args:
        order_id: Synthetic order id from `place_paper_buy()`.
        fill_price: Fill price (the LTP passed to `place_paper_buy()`).
        qty: Filled quantity.
        symbol: NSE trading symbol.
        gtt_ids: Simulated GTT id(s) attached to this position, if any.
    """
    trade_date = date.today().isoformat()
    _append_trade_row(
        subagent.PAPER_TRADES_CSV_PATH,
        {
            "date": trade_date,
            "symbol": symbol,
            "side": "BUY",
            "qty": qty,
            "price": fill_price,
            "order_id": order_id,
            "gtt_id": ";".join(gtt_ids) if gtt_ids else "",
            "realised_pnl_delta": 0.0,
            "status": "COMPLETE",
        },
    )

    state = subagent.load_state(subagent.PAPER_STATE_PATH) or _new_paper_state()
    state.setdefault("positions", {})[symbol] = {
        "entry_date": trade_date,
        "quantity": qty,
        "avg_price": fill_price,
        "order_id": order_id,
        "gtt_ids": gtt_ids or [],
        "sells": [],
    }
    subagent.save_state(subagent.PAPER_STATE_PATH, state)
    logger.info("recorded paper fill: %s qty=%d price=%.2f order_id=%s", symbol, qty, fill_price, order_id)


def main() -> None:
    """Run the safe live-LTP paper-buy smoke command from the CLI.

    Also exercises the LTP sanity guard via a self-test when `--self-test`
    is passed: a stubbed `kite_session.get_ltp` returning ``{}`` must
    propagate as `KiteSessionExpired` and never reach `record_paper_fill`.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="MiniTrader paper execution")
    parser.add_argument("--smoke", nargs=2, metavar=("SYM", "QTY"), help="Record a paper fill at live LTP")
    parser.add_argument(
        "--self-test",
        action="store_true",
        help="Run guard self-tests (no live Kite call) and exit",
    )
    args = parser.parse_args()
    if args.self_test:
        _run_self_tests()
        return
    if not args.smoke:
        parser.print_help()
        return
    symbol, quantity = args.smoke[0], int(args.smoke[1])
    fill = place_paper_buy_at_ltp(symbol, quantity)
    record_paper_fill(fill["order_id"], float(fill["average_price"]), quantity, symbol)
    print(fill)


def _run_self_tests() -> None:
    """In-process guards that catch silent LTP substitution.

    1. If `kite_session.get_ltp` returns ``{}``, `place_paper_buy_at_ltp`
       must raise `KiteSessionExpired` and `record_paper_fill` must never
       be called. This is the regression guard for the 2026-08-12 INFY
       fill being recorded at ₹1,500.25 when the live LTP path failed.
    """
    import kite_session as _ks

    global record_paper_fill  # noqa: PLW0603 - intentional rebinding for the duration of the test

    original_get_ltp = _ks.get_ltp
    original_record = record_paper_fill
    record_calls: list[dict[str, Any]] = []

    def _stub_record(*args, **kwargs):
        record_calls.append({"args": args, "kwargs": kwargs})
        return original_record(*args, **kwargs)

    _ks.get_ltp = lambda symbols: {}  # simulate MCP failure / empty LTP
    record_paper_fill = _stub_record  # type: ignore[assignment]
    try:
        try:
            place_paper_buy_at_ltp("INFY", 5)
        except _ks.KiteSessionExpired:
            assert not record_calls, (
                f"record_paper_fill was called even though get_ltp returned {{}}: {record_calls!r}"
            )
            print("self-test ok: get_ltp={} -> KiteSessionExpired, record_paper_fill not called")
            return
        raise AssertionError(
            "place_paper_buy_at_ltp did NOT raise KiteSessionExpired when get_ltp returned {}; "
            "the silent-substitution guard has regressed."
        )
    finally:
        _ks.get_ltp = original_get_ltp
        record_paper_fill = original_record  # type: ignore[assignment]


if __name__ == "__main__":
    main()
