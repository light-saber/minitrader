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

import csv
import inspect
import logging
import uuid
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Optional

import subagent
from kite_exec import TRADE_CSV_COLUMNS, pretrade_guard  # shared guard interface, SPEC.md §9

logger = logging.getLogger(__name__)

__all__ = ["pretrade_guard", "place_paper_buy", "record_paper_fill"]


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
    """CLI entry point — describes the module without executing any trading logic."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print("paper_exec.py — Phase 5 paper execution module (imports only; no MCP calls, ever).")
    for name in ("pretrade_guard", "place_paper_buy", "record_paper_fill"):
        fn = globals()[name]
        print(f"  {name}{inspect.signature(fn)}")


if __name__ == "__main__":
    main()
