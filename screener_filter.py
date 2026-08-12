"""Phase 2 mandate filter: screener universe -> ranked buy shortlist.

`load_screener_output()` reads the monthly quality+momentum screener JSON
produced by `~/.hermes/scripts/stock-screener/run_combined_screener.sh`
(SPEC.md §4, Phase 1). `apply_mandate_filter()` then applies the Phase 2
rules from SPEC.md §4:

    - Drop any symbol already in Kite holdings (or an existing paper position)
    - Drop any symbol that isn't NSE/BSE-listed (Kite CNC can't trade it) or
      that fails the Piotroski F-Score quality gate
    - Drop FnO-eligible names the sub-agent can't verify CNC-only liquidity for
    - Apply the portfolio's sector cap on (existing + proposed) exposure
    - Tiebreak survivors on dividend yield as a soft preference

Both portfolios share the same screener universe; `rules` (`subagent.LIVE_RULES`
or `subagent.PAPER_RULES`) selects which existing-positions collection
(`kite_holdings` or `paper_positions`) anchors the sector-cap calculation via
its `"portfolio_name"` key, while `kite_holdings` and `paper_positions` are
both always used for the "already held" de-duplication check.

KNOWN LIMITATIONS (see inline comments): the live screener JSON
(`/root/.hermes/cron/output/stock_screener_with_momentum.json`) has no
`dividend_yield` field today, so the tiebreak is a no-op until the screener
is extended. FnO-eligibility can only be enforced if the caller supplies
`rules["fno_eligible_symbols"]` (no live Kite instrument-dump lookup is
wired up yet — same "Kite session belongs in subagent.py" boundary as
`technical_workup.fetch_ohlc`).
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional, Union

import subagent

logger = logging.getLogger(__name__)

DEFAULT_SCREENER_PATH = Path("/root/.hermes/cron/output/stock_screener_with_momentum.json")
MAX_SCREENER_AGE_DAYS = 45
MIN_F_SCORE = 7
SHORTLIST_MIN = 5
SHORTLIST_MAX = 10
NSE_BSE_SUFFIXES = (".NS", ".BO")


def load_screener_output(path: Union[str, Path] = DEFAULT_SCREENER_PATH) -> Optional[list[dict[str, Any]]]:
    """Load the Phase 1 screener JSON, treating a missing or stale file as absent.

    Args:
        path: Path to `stock_screener_with_momentum.json`.

    Returns:
        The parsed candidate list, or ``None`` if the file is missing or its
        mtime is older than `MAX_SCREENER_AGE_DAYS` — callers (subagent.py)
        are expected to trigger a fresh screener run in that case.
    """
    path = Path(path)
    if not path.exists():
        logger.warning("screener output not found at %s", path)
        return None

    mtime = datetime.fromtimestamp(path.stat().st_mtime)
    age_days = (datetime.now() - mtime).days
    if age_days > MAX_SCREENER_AGE_DAYS:
        logger.warning(
            "screener output at %s is %d days old (max %d) — treating as stale", path, age_days, MAX_SCREENER_AGE_DAYS
        )
        return None

    with path.open("r", encoding="utf-8") as f:
        return json.load(f)


def _bare_symbol(symbol: str) -> str:
    """Strip the `.NS`/`.BO` exchange suffix and uppercase, for cross-source matching.

    Screener entries use e.g. ``"TCS.NS"``; Kite's `tradingsymbol` is bare
    (``"TCS"``). Both are normalized to this form before comparison.

    Args:
        symbol: A raw symbol string, with or without an exchange suffix.

    Returns:
        The uppercase bare symbol.
    """
    bare = symbol.upper()
    for suffix in NSE_BSE_SUFFIXES:
        if bare.endswith(suffix):
            return bare[: -len(suffix)]
    return bare


def _position_symbols(positions: Any) -> set[str]:
    """Normalize a positions collection (dict-of-symbol or list-of-dict) to a bare-symbol set.

    Args:
        positions: `kite_holdings` (list of dicts, e.g.
            `mcp__kite__get_holdings` shape) or `paper_positions`
            (typically `paper_state["positions"]`, a symbol-keyed dict).

    Returns:
        The set of bare (suffix-stripped, uppercase) symbols held.
    """
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


def _position_items(positions: Any) -> Iterable[tuple[str, dict[str, Any]]]:
    """Yield (symbol, position-dict) pairs from either positions shape.

    Args:
        positions: See `_position_symbols`.

    Yields:
        ``(symbol, position_dict)`` tuples.
    """
    if not positions:
        return
    if isinstance(positions, dict):
        yield from positions.items()
        return
    for entry in positions:
        if isinstance(entry, dict):
            sym = entry.get("tradingsymbol") or entry.get("symbol") or entry.get("ticker")
            if sym:
                yield sym, entry


def _position_value_by_sector(positions: Any, sector_lookup: dict[str, str]) -> dict[str, float]:
    """Best-effort rupee exposure per sector for an existing-positions collection.

    Positions whose symbol isn't in `sector_lookup` (i.e. not in the current
    screener universe) are excluded from the total — a documented
    approximation, since neither `state.json` nor Kite holdings carry a
    sector field.

    Args:
        positions: `kite_holdings` or `paper_positions`.
        sector_lookup: bare-symbol -> sector, built from the screener universe.

    Returns:
        sector -> rupee value currently held.
    """
    values: dict[str, float] = {}
    for symbol, pos in _position_items(positions):
        sector = sector_lookup.get(_bare_symbol(symbol))
        if sector is None:
            continue
        qty = pos.get("quantity", 0)
        price = pos.get("average_price", pos.get("avg_price", 0))
        values[sector] = values.get(sector, 0.0) + qty * price
    return values


def _dividend_yield(candidate: dict[str, Any]) -> float:
    """Best-effort dividend yield extraction for the tiebreak sort.

    Args:
        candidate: A screener universe entry.

    Returns:
        The dividend yield if present under `dividend_yield` (top-level or
        nested in `financials`), else 0.0. The live screener JSON does not
        currently emit this field, so this is a no-op tiebreak today.
    """
    if "dividend_yield" in candidate:
        return float(candidate["dividend_yield"] or 0.0)
    return float(candidate.get("financials", {}).get("dividend_yield", 0.0) or 0.0)


def apply_mandate_filter(
    universe: list[dict[str, Any]],
    kite_holdings: Any,
    paper_positions: Any,
    rules: dict[str, Any],
) -> list[dict[str, Any]]:
    """Apply the SPEC.md §4 Phase 2 mandate rules and rank the survivors.

    Args:
        universe: Screener candidates, as returned by `load_screener_output()`.
        kite_holdings: Live Kite holdings (list of dicts with
            `tradingsymbol`/`quantity`/`average_price`, or an equivalent
            symbol-keyed dict).
        paper_positions: Paper portfolio positions, typically
            `paper_state["positions"]`.
        rules: `subagent.LIVE_RULES` or `subagent.PAPER_RULES` — must include
            `total_capital`, `max_position_pct`, `sector_cap_pct`, and
            `portfolio_name` (selects which of `kite_holdings`/
            `paper_positions` anchors the sector-cap calculation).
            `rules["fno_eligible_symbols"]` (optional set) drives the FnO
            liquidity-verification drop.

    Returns:
        A ranked shortlist of 5-10 candidate dicts (fewer if too few
        survive), ordered by F-Score, then momentum score, then dividend
        yield.
    """
    held_symbols = _position_symbols(kite_holdings) | _position_symbols(paper_positions)
    sector_lookup = {_bare_symbol(c["symbol"]): c.get("sector") for c in universe if c.get("symbol")}

    home_positions = paper_positions if rules.get("portfolio_name") == "paper" else kite_holdings
    sector_cap_pct = rules.get("sector_cap_pct")
    total_capital = rules.get("total_capital", 0)
    max_position_pct = rules.get("max_position_pct", 1.0)
    fno_eligible = rules.get("fno_eligible_symbols") or set()
    existing_sector_value = _position_value_by_sector(home_positions, sector_lookup)

    survivors: list[dict[str, Any]] = []
    for candidate in universe:
        symbol = candidate.get("symbol", "")
        bare = _bare_symbol(symbol)
        reasons: list[str] = []

        if not symbol.upper().endswith(NSE_BSE_SUFFIXES):
            reasons.append("not NSE/BSE-listed — Kite CNC cannot trade this symbol")
        if bare in held_symbols:
            reasons.append("already held (live or paper)")
        f_score = candidate.get("f_score")
        if f_score is not None and f_score < MIN_F_SCORE:
            reasons.append(f"f_score {f_score} below quality gate {MIN_F_SCORE}")
        if bare in fno_eligible:
            reasons.append("FnO-eligible — cannot verify CNC-only liquidity")

        sector = candidate.get("sector")
        if sector_cap_pct is not None and sector and total_capital:
            projected = existing_sector_value.get(sector, 0.0) + max_position_pct * total_capital
            projected_pct = projected / total_capital
            if projected_pct > sector_cap_pct:
                reasons.append(f"sector cap breach: {sector} would reach {projected_pct:.0%} > cap {sector_cap_pct:.0%}")

        if reasons:
            logger.info("dropping %s: %s", symbol, "; ".join(reasons))
            continue
        survivors.append(candidate)

    ranked = sorted(
        survivors,
        key=lambda c: (-(c.get("f_score") or 0), -(c.get("momentum_score") or 0), -_dividend_yield(c)),
    )
    shortlist = ranked[:SHORTLIST_MAX]
    if len(shortlist) < SHORTLIST_MIN:
        logger.warning(
            "mandate filter shortlist has only %d candidates (target %d-%d)", len(shortlist), SHORTLIST_MIN, SHORTLIST_MAX
        )
    return shortlist


def _demo() -> None:
    """Build a small mock screener JSON and run it through the full filter."""
    mock_universe = [
        {"symbol": "TCS.NS", "sector": "IT", "f_score": 9, "momentum_score": 44.0},
        {"symbol": "INFY.NS", "sector": "IT", "f_score": 9, "momentum_score": 60.0},
        {"symbol": "HAL.NS", "sector": "Defence", "f_score": 8, "momentum_score": 70.0},
        {"symbol": "ABB.NS", "sector": "Capital Goods", "f_score": 7, "momentum_score": 55.0},
        {"symbol": "POWERGRID.NS", "sector": "Power", "f_score": 8, "momentum_score": 40.0},
        {"symbol": "TATASTEEL.NS", "sector": "Metals", "f_score": 6, "momentum_score": 30.0},
        {"symbol": "GOOGL", "sector": "Tech", "f_score": 9, "momentum_score": 80.0},
        {"symbol": "WIPRO.NS", "sector": "IT", "f_score": 8, "momentum_score": 35.0},
    ]
    mock_path = Path("/tmp/.minitrader_demo_screener.json")
    mock_path.write_text(json.dumps(mock_universe), encoding="utf-8")

    universe = load_screener_output(mock_path)
    print(f"loaded {len(universe)} candidates from mock screener output at {mock_path}")

    kite_holdings = [{"tradingsymbol": "TCS", "quantity": 2, "average_price": 3200.0}]
    paper_positions = {"INFY": {"quantity": 3, "avg_price": 1600.0}}

    shortlist = apply_mandate_filter(universe, kite_holdings, paper_positions, subagent.PAPER_RULES)
    print(f"shortlist ({len(shortlist)} candidates), PAPER_RULES:")
    for c in shortlist:
        print(f"  {c['symbol']:12s} sector={c.get('sector', ''):15s} f_score={c.get('f_score')} momentum={c.get('momentum_score')}")


def main() -> None:
    """CLI entry point for `screener_filter.py`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(prog="screener_filter.py", description="Phase 2 mandate filter")
    parser.add_argument("--demo", action="store_true", help="Run the mandate filter against a mock screener file")
    args = parser.parse_args()

    if args.demo:
        _demo()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
