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


def place_paper_buy_at_ltp(symbol: str, quantity: int) -> dict[str, Any]:
    """Place a paper buy using live LTP from `kite_session`.

    Args:
        symbol: NSE trading symbol.
        quantity: Quantity to buy in the paper portfolio.

    Returns:
        The virtual fill result returned by `place_paper_buy`.

    Raises:
        kite_session.KiteSessionExpired: If no current quote is available,
            including when the Kite session has expired.
    """
    result = kite_session.get_ltp([symbol])
    if not result:
        raise kite_session.KiteSessionExpired(
            "Kite session expired or no live LTP available — re-authenticate before placing a paper fill."
        )
    ltp = float(result[symbol.upper()])
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
    """Run the safe live-LTP paper-buy smoke command from the CLI."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="MiniTrader paper execution")
    parser.add_argument("--smoke", nargs=2, metavar=("SYM", "QTY"), help="Record a paper fill at live LTP")
    args = parser.parse_args()
    if not args.smoke:
        parser.print_help()
        return
    symbol, quantity = args.smoke[0], int(args.smoke[1])
    fill = place_paper_buy_at_ltp(symbol, quantity)
    record_paper_fill(fill["order_id"], float(fill["average_price"]), quantity, symbol)
    print(fill)


if __name__ == "__main__":
    main()
