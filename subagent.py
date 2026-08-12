"""MiniTrader main entrypoint: state management and dual-portfolio dispatcher.

This module owns the on-disk state schema (see SPEC.md §5) for MiniTrader's
two parallel portfolios — **live** (real Kite account, single-position rules,
₹5,000 first window) and **paper** (virtual, multi-position rules, ₹50,000
default). It does not place orders itself; it loads/saves portfolio state and
routes an approved buy `signal` to the correct execution module
(`kite_exec.py` for live, `paper_exec.py` for paper) via `dispatch()`.

Nothing in this module calls a Kite or Discord MCP tool at import time or
during `python subagent.py status` — execution modules are imported lazily
inside `dispatch()` so a missing Kite session never breaks state inspection.
"""

from __future__ import annotations

import argparse
import json
import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent

STATE_PATH = BASE_DIR / "state.json"
PAPER_STATE_PATH = BASE_DIR / "paper_state.json"
TRADES_CSV_PATH = BASE_DIR / "trades.csv"
PAPER_TRADES_CSV_PATH = BASE_DIR / "paper_trades.csv"

# Hard rules that apply to both portfolios (SPEC.md §2).
MIN_HOLDING_DAYS = 3
WINDOW_DAYS = 30
ALLOWED_PRODUCT = "CNC"
ALLOWED_ORDER_TYPES = ("MARKET", "LIMIT")

# Live portfolio sizing rules (SPEC.md §3, "Live portfolio (₹5K window)").
LIVE_RULES: dict[str, Any] = {
    "total_capital": 5000,
    "max_positions": 1,
    "max_position_pct": 1.0,
    "min_order_value": 1500,
    "reserve_buffer": 200,
    "sector_cap_pct": None,
    "default_order_type": "MARKET",
    "product": ALLOWED_PRODUCT,
}

# Paper portfolio sizing rules (SPEC.md §3, "Paper portfolio (configurable, default ₹50,000)").
PAPER_RULES: dict[str, Any] = {
    "total_capital": 50000,
    "max_positions": 8,
    "max_position_pct": 0.20,
    "min_order_value": 1500,
    "reserve_buffer": 500,
    "sector_cap_pct": 0.35,
    "default_order_type": "MARKET",
    "product": ALLOWED_PRODUCT,
}


def load_state(path: Path) -> Optional[dict[str, Any]]:
    """Load a portfolio state JSON file.

    Args:
        path: Absolute path to a `state.json`-shaped file.

    Returns:
        The parsed state dict, or ``None`` if the file does not exist yet
        (e.g. before the first window has been opened).

    Raises:
        json.JSONDecodeError: If the file exists but contains invalid JSON.
    """
    if not path.exists():
        return None
    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def save_state(path: Path, state: dict[str, Any]) -> None:
    """Persist a portfolio state dict to disk as indented JSON.

    Args:
        path: Absolute path to write the state file to.
        state: The full state dict (see SPEC.md §5 schema).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as f:
        json.dump(state, f, indent=2)
        f.write("\n")


@dataclass
class Portfolio:
    """One side of the dual-portfolio system (live or paper).

    Attributes:
        name: Either ``"live"`` or ``"paper"``.
        state_path: Path to this portfolio's state JSON file.
        trades_path: Path to this portfolio's trade journal CSV.
        rules: Position-sizing rules table (`LIVE_RULES` or `PAPER_RULES`).
        state: Cached in-memory state, or ``None`` until loaded.
    """

    name: str
    state_path: Path
    trades_path: Path
    rules: dict[str, Any]
    state: Optional[dict[str, Any]] = field(default=None)

    def load(self) -> Optional[dict[str, Any]]:
        """Load and cache this portfolio's state from disk.

        Returns:
            The loaded state dict, or ``None`` if no window has been opened.
        """
        self.state = load_state(self.state_path)
        return self.state

    def save(self) -> None:
        """Persist the currently cached state to disk.

        Raises:
            ValueError: If no state has been loaded or set yet.
        """
        if self.state is None:
            raise ValueError(f"no state to save for portfolio {self.name!r}")
        save_state(self.state_path, self.state)


def build_portfolios() -> tuple[Portfolio, Portfolio]:
    """Construct the two standing `Portfolio` instances used by the sub-agent.

    Returns:
        A ``(live, paper)`` tuple of `Portfolio` dataclass instances.
    """
    live = Portfolio(
        name="live",
        state_path=STATE_PATH,
        trades_path=TRADES_CSV_PATH,
        rules=LIVE_RULES,
    )
    paper = Portfolio(
        name="paper",
        state_path=PAPER_STATE_PATH,
        trades_path=PAPER_TRADES_CSV_PATH,
        rules=PAPER_RULES,
    )
    return live, paper


def dispatch(portfolio_name: str, signal: dict[str, Any]) -> dict[str, Any]:
    """Route an approved buy signal to the correct execution module.

    ``portfolio_name == "live"`` routes to `kite_exec.place_live_buy`
    (real `mcp__kite__place_order` call). ``portfolio_name == "paper"``
    routes to `paper_exec.place_paper_buy` (virtual fill, no Kite call).

    Execution modules are imported lazily, inside this function, so that
    importing `subagent` — and running `python subagent.py status` — never
    requires a Kite session or triggers any MCP call.

    Args:
        portfolio_name: Either ``"live"`` or ``"paper"``.
        signal: Approved buy instruction, e.g.
            ``{"symbol": "INFY", "quantity": 3, "ltp": 1600.0,
            "product": "CNC", "order_type": "MARKET"}``.

    Returns:
        The result dict returned by the underlying execution function.

    Raises:
        ValueError: If `portfolio_name` is not ``"live"`` or ``"paper"``.
    """
    if portfolio_name == "live":
        import kite_exec

        logger.info("dispatching live buy signal: %s", signal)
        return kite_exec.place_live_buy(
            symbol=signal["symbol"],
            qty=signal["quantity"],
            product=signal.get("product", ALLOWED_PRODUCT),
            order_type=signal.get("order_type", "MARKET"),
        )
    if portfolio_name == "paper":
        import paper_exec

        logger.info("dispatching paper buy signal: %s", signal)
        return paper_exec.place_paper_buy(
            symbol=signal["symbol"],
            qty=signal["quantity"],
            ltp=signal["ltp"],
        )
    raise ValueError(f"unknown portfolio_name {portfolio_name!r}, expected 'live' or 'paper'")


def _format_status(portfolio: Portfolio) -> str:
    """Build a human-readable status summary line for one portfolio.

    Args:
        portfolio: A `Portfolio` instance with `load()` already called.

    Returns:
        A multi-line summary string, or a "no state yet" message.
    """
    state = portfolio.state
    if state is None:
        return f"[{portfolio.name}] no state yet (window not opened — state file {portfolio.state_path.name} missing)"

    positions = state.get("positions", {})
    capital_committed = state.get("capital_committed", portfolio.rules["total_capital"])
    deployed = sum(
        pos.get("quantity", 0) * pos.get("avg_price", 0.0) for pos in positions.values()
    )
    lines = [
        f"[{portfolio.name}] window {state.get('window_start', '?')} -> {state.get('window_end', '?')}",
        f"[{portfolio.name}] capital deployed: {deployed:.2f} of {capital_committed}",
        f"[{portfolio.name}] positions: {len(positions)} ({', '.join(sorted(positions)) or 'none'})",
        f"[{portfolio.name}] realised P&L: {state.get('realised_pnl', 0.0)}",
    ]
    return "\n".join(lines)


def print_status() -> None:
    """Load both portfolios and print a combined status summary to stdout."""
    live, paper = build_portfolios()
    live.load()
    paper.load()
    print(_format_status(live))
    print(_format_status(paper))


def main() -> None:
    """CLI entry point for `subagent.py`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(prog="subagent.py", description="MiniTrader dual-portfolio sub-agent")
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("status", help="Print live + paper portfolio status")
    args = parser.parse_args()

    if args.command == "status":
        print_status()


if __name__ == "__main__":
    main()
