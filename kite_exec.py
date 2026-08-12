"""Phase 5 live execution: pre-trade guard, order placement, GTT attach, fill recording.

CONSTRAINT (build brief, non-negotiable): this module defines `place_live_buy()`
but nothing in this codebase calls it. Every live buy requires an explicit
human `go` in Discord (SPEC.md §4, Phase 4) — that gate lives in the
sub-agent's Phase 4/5 orchestration (not yet built beyond the `dispatch()`
skeleton in subagent.py), which must call `pretrade_guard()` and only then
`place_live_buy()`. This module never retries a failed order and never
widens a GTT (SPEC.md §8).

MCP integration seam: `_call_mcp_tool()` is the boundary between this plain
Python module and the Hermes sub-agent's MCP tool-calling capability. A bare
`python kite_exec.py` (or `import kite_exec`) never touches a live Kite
session — `_call_mcp_tool` raises `NotImplementedError` until the Hermes
runtime harness injects a real dispatcher via `set_mcp_dispatcher()`. This
makes constraints 1 and 2 of the build brief (no live order placement ever;
no Kite session required for module import) hold structurally, not just by
convention. Tool names below follow SPEC.md's own naming
(`mcp__kite__place_order`, `mcp__kite__place_gtt_order`); in this dev
session the equivalent tools are connected under the `mcp__claude_ai_Kite__*`
prefix — the injected dispatcher owns whatever name mapping its deployment
needs.
"""

from __future__ import annotations

import csv
import inspect
import logging
from datetime import date, timedelta
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import subagent

logger = logging.getLogger(__name__)

TRADE_CSV_COLUMNS = ["date", "symbol", "side", "qty", "price", "order_id", "gtt_id", "realised_pnl_delta"]
NSE_BSE_SUFFIXES = (".NS", ".BO")

_mcp_dispatcher: Optional[Callable[..., dict[str, Any]]] = None


def set_mcp_dispatcher(dispatcher: Callable[..., dict[str, Any]]) -> None:
    """Inject the Hermes runtime's MCP tool-calling function.

    Args:
        dispatcher: Callable with signature ``dispatcher(tool_name: str, **kwargs) -> dict``,
            wired up by the live sub-agent runtime to actually invoke the
            named MCP tool and return its structured result.
    """
    global _mcp_dispatcher
    _mcp_dispatcher = dispatcher


def _call_mcp_tool(tool_name: str, **kwargs: Any) -> dict[str, Any]:
    """Invoke an MCP tool through the injected runtime dispatcher.

    Args:
        tool_name: MCP tool name, e.g. ``"mcp__kite__place_order"``.
        **kwargs: Tool arguments.

    Returns:
        The tool's structured result.

    Raises:
        NotImplementedError: If no dispatcher has been injected — expected
            for any invocation outside the live Hermes sub-agent runtime.
    """
    if _mcp_dispatcher is None:
        raise NotImplementedError(
            f"no MCP dispatcher wired up — cannot call {tool_name}. This is expected outside "
            "the live Hermes sub-agent runtime; see set_mcp_dispatcher()."
        )
    return _mcp_dispatcher(tool_name, **kwargs)


def _bare_symbol(symbol: str) -> str:
    """Strip the `.NS`/`.BO` exchange suffix and uppercase, for cross-source matching."""
    bare = symbol.upper()
    for suffix in NSE_BSE_SUFFIXES:
        if bare.endswith(suffix):
            return bare[: -len(suffix)]
    return bare


def _position_symbols(positions: Any) -> set[str]:
    """Normalize a positions collection (dict-of-symbol or list-of-dict) to a bare-symbol set."""
    if not positions:
        return set()
    if isinstance(positions, dict):
        return {_bare_symbol(k) for k in positions}
    symbols: set[str] = set()
    for entry in positions:
        if isinstance(entry, str):
            symbols.add(_bare_symbol(entry))
        elif isinstance(entry, dict):
            sym = entry.get("tradingsymbol") or entry.get("symbol") or entry.get("ticker")
            if sym:
                symbols.add(_bare_symbol(sym))
    return symbols


def pretrade_guard(
    symbol: str,
    qty: int,
    price: float,
    shortlist: Iterable[str],
    existing_positions: Any,
    available_margin: float,
    kite_session_active: bool,
    max_order_value: float,
) -> tuple[bool, str]:
    """Fail-closed pre-trade guard (SPEC.md §4, Phase 5).

    Checks, in order: Kite session active; symbol in today's Phase-2
    shortlist; order value within `max_order_value`; order value within
    available margin; not duplicating an existing position. Refuses on the
    first failing check — never partially proceeds.

    Args:
        symbol: NSE trading symbol to buy.
        qty: Proposed quantity.
        price: Proposed fill price (LTP or limit price).
        shortlist: Today's Phase-2 mandate-filter shortlist symbols.
        existing_positions: Current live holdings (kite_holdings shape).
        available_margin: Current buying power (₹) from `mcp__kite__get_margins`.
        kite_session_active: Whether the Kite session is currently authenticated.
        max_order_value: Maximum ₹ order value allowed by the portfolio's rules.

    Returns:
        ``(True, "")`` if every check passes, else ``(False, reason)`` for
        the first failing check.
    """
    if not kite_session_active:
        return False, "Kite session not active — re-authenticate via mcp_kite_login before retrying"

    bare_symbol = _bare_symbol(symbol)
    bare_shortlist = {_bare_symbol(s) for s in shortlist}
    if bare_symbol not in bare_shortlist:
        return False, f"{symbol} is not in today's Phase-2 shortlist"

    order_value = qty * price
    if order_value > max_order_value:
        return False, f"order value {order_value:.2f} exceeds max order value {max_order_value:.2f}"

    if order_value > available_margin:
        return False, f"order value {order_value:.2f} exceeds available margin {available_margin:.2f}"

    if bare_symbol in _position_symbols(existing_positions):
        return False, f"{symbol} already has an open position — never duplicate or modify an existing holding"

    return True, ""


def place_live_buy(
    symbol: str,
    qty: int,
    product: str = "CNC",
    order_type: str = "MARKET",
    price: Optional[float] = None,
) -> dict[str, Any]:
    """Place a real live CNC buy order via `mcp__kite__place_order`.

    Performs NO guard checks itself — callers must run `pretrade_guard()`
    and get an explicit human `go` first (SPEC.md §4, Phase 4/5). On any
    error this logs and re-raises; it never retries automatically
    (SPEC.md §8).

    Args:
        symbol: NSE trading symbol to buy.
        qty: Quantity to buy.
        product: Must be ``"CNC"`` — MiniTrader never places FnO/MIS/BO/CO orders.
        order_type: ``"MARKET"`` or ``"LIMIT"``.
        price: Required when `order_type` is ``"LIMIT"``.

    Returns:
        The order result dict from Kite (at least an `order_id`).

    Raises:
        ValueError: If `product` isn't CNC, `order_type` is unsupported, or
            a LIMIT order is missing `price`.
        RuntimeError: If order placement fails for any reason.
    """
    if product != "CNC":
        raise ValueError(f"product must be CNC, got {product!r} — MiniTrader never places FnO/MIS/BO/CO orders")
    if order_type not in ("MARKET", "LIMIT"):
        raise ValueError(f"unsupported order_type {order_type!r}")

    payload: dict[str, Any] = {
        "exchange": "NSE",
        "tradingsymbol": symbol,
        "transaction_type": "BUY",
        "quantity": qty,
        "product": product,
        "order_type": order_type,
    }
    if order_type == "LIMIT":
        if price is None:
            raise ValueError("price is required for LIMIT orders")
        payload["price"] = price

    logger.info("placing live CNC buy: %s", payload)
    try:
        result = _call_mcp_tool("mcp__kite__place_order", **payload)
    except Exception as exc:
        logger.error("live order placement failed for %s: %s", symbol, exc)
        raise RuntimeError(f"live order placement failed for {symbol}: {exc}") from exc

    logger.info("live order placed: %s", result)
    return result


def attach_gtt(symbol: str, trigger_values: dict[str, float], qty: int, last_price: float) -> dict[str, Any]:
    """Attach a two-leg GTT (stop-loss + target) via `mcp__kite__place_gtt_order`.

    Uses the levels from `technical_workup.should_attach_gtt()`. This
    function only ever places a GTT — it has no "modify" path, so it can
    never widen an existing GTT (SPEC.md §8). Never retried automatically
    on failure.

    Args:
        symbol: NSE trading symbol.
        trigger_values: ``{"stop": float, "target": float}`` from
            `technical_workup.should_attach_gtt()`.
        qty: Quantity covered by the GTT (matches the live fill quantity).
        last_price: Current LTP, required by Kite's GTT API as the trigger reference.

    Returns:
        The GTT result dict from Kite (at least a GTT id).

    Raises:
        RuntimeError: If GTT placement fails for any reason.
    """
    stop = trigger_values["stop"]
    target = trigger_values["target"]
    payload = {
        "exchange": "NSE",
        "tradingsymbol": symbol,
        "trigger_type": "two-leg",
        "last_price": last_price,
        "orders": [
            {
                "transaction_type": "SELL",
                "quantity": qty,
                "order_type": "LIMIT",
                "product": "CNC",
                "price": stop,
                "trigger_price": stop,
            },
            {
                "transaction_type": "SELL",
                "quantity": qty,
                "order_type": "LIMIT",
                "product": "CNC",
                "price": target,
                "trigger_price": target,
            },
        ],
    }

    logger.info("attaching GTT: %s", payload)
    try:
        result = _call_mcp_tool("mcp__kite__place_gtt_order", **payload)
    except Exception as exc:
        logger.error("GTT placement failed for %s: %s", symbol, exc)
        raise RuntimeError(f"GTT placement failed for {symbol}: {exc}") from exc

    logger.info("GTT placed: %s", result)
    return result


def _append_trade_row(path: Path, row: dict[str, Any]) -> None:
    """Append one row to a trade journal CSV, writing the header if new.

    Args:
        path: Destination CSV path (`trades.csv` or `paper_trades.csv`).
        row: Must contain exactly the keys in `TRADE_CSV_COLUMNS`.
    """
    is_new = not path.exists()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=TRADE_CSV_COLUMNS)
        if is_new:
            writer.writeheader()
        writer.writerow(row)


def _new_live_state() -> dict[str, Any]:
    """Build a fresh live `state.json` payload for a new 30-day window.

    Returns:
        A state dict matching the SPEC.md §5 schema.
    """
    today = date.today()
    return {
        "window_start": today.isoformat(),
        "window_end": (today + timedelta(days=subagent.WINDOW_DAYS)).isoformat(),
        "capital_committed": subagent.LIVE_RULES["total_capital"],
        "positions": {},
        "realised_pnl": 0.0,
        "sells": [],
        "pending_buys": [],
        "last_screener_run": None,
    }


def record_fill(order_id: str, fill_price: float, qty: int, symbol: str, gtt_ids: Optional[list[str]] = None) -> None:
    """Record a live fill: append to `trades.csv` and update `state.json`.

    Args:
        order_id: Kite order id returned by `place_live_buy()`.
        fill_price: Executed fill price.
        qty: Filled quantity.
        symbol: NSE trading symbol.
        gtt_ids: GTT id(s) attached to this position, if any.
    """
    trade_date = date.today().isoformat()
    _append_trade_row(
        subagent.TRADES_CSV_PATH,
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

    state = subagent.load_state(subagent.STATE_PATH) or _new_live_state()
    state.setdefault("positions", {})[symbol] = {
        "entry_date": trade_date,
        "quantity": qty,
        "avg_price": fill_price,
        "order_id": order_id,
        "gtt_ids": gtt_ids or [],
        "sells": [],
    }
    subagent.save_state(subagent.STATE_PATH, state)
    logger.info("recorded live fill: %s qty=%d price=%.2f order_id=%s", symbol, qty, fill_price, order_id)


def main() -> None:
    """CLI entry point — describes the module without executing any trading logic."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    print("kite_exec.py — Phase 5 live execution module (imports only; places no orders when run directly).")
    for name in ("pretrade_guard", "place_live_buy", "attach_gtt", "record_fill"):
        fn = globals()[name]
        print(f"  {name}{inspect.signature(fn)}")


if __name__ == "__main__":
    main()
