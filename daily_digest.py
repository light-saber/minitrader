"""Phase 6 ongoing tracking: dual-portfolio daily digest, posted to Discord.

Reads `state.json` (live) and `paper_state.json` (paper), pulls current LTPs
and the Nifty50 same-period return from Kite, and formats the SPEC.md §7
digest template. Posts to Discord channel `1517387649520762960`, threaded
on `1537102886033428581` when possible.

MCP integration seam: mirrors `kite_exec.py` — `_call_mcp_tool()` raises
`NotImplementedError` until the Hermes runtime harness injects a real
dispatcher via `set_mcp_dispatcher()`, so import and `--dry-run` never
require a live Kite or Discord session (constraint 2). In `--dry-run` (or
whenever no dispatcher is wired up), live-data calls fall back to
`technical_workup.fetch_ohlc`'s deterministic synthetic OHLC generator
instead of failing, so the digest format can always be exercised end to end.

NOTE on SPEC.md §7's example numbers: the live and paper example blocks are
not mutually consistent (the live block's "+3.13%" only reconciles against
deployed capital; the paper block's "+3.64%" only reconciles against total
committed capital). This module picks one explicit, documented formula
(see `_unrealised_pct` / `_total_return_pct`) rather than chasing the
example's arithmetic.
"""

from __future__ import annotations

import argparse
import logging
from datetime import date, timedelta
from typing import Any, Callable, Iterable, Optional

import earnings_calendar
import screener_filter
import subagent
import technical_workup

logger = logging.getLogger(__name__)

DISCORD_CHANNEL_ID = "1517387649520762960"
DISCORD_THREAD_ID = "1537102886033428581"
NIFTY50_INSTRUMENT_TOKEN = 256265
EARNINGS_LOOKAHEAD_DAYS = 5

_mcp_dispatcher: Optional[Callable[..., Any]] = None


def set_mcp_dispatcher(dispatcher: Callable[..., Any]) -> None:
    """Inject the Hermes runtime's MCP tool-calling function.

    Args:
        dispatcher: Callable with signature ``dispatcher(tool_name: str, **kwargs) -> Any``.
    """
    global _mcp_dispatcher
    _mcp_dispatcher = dispatcher


def _call_mcp_tool(tool_name: str, **kwargs: Any) -> Any:
    """Invoke an MCP tool through the injected runtime dispatcher.

    Raises:
        NotImplementedError: If no dispatcher has been injected.
    """
    if _mcp_dispatcher is None:
        raise NotImplementedError(f"no MCP dispatcher wired up — cannot call {tool_name}")
    return _mcp_dispatcher(tool_name, **kwargs)


def fetch_ltps(symbols: Iterable[str], dry_run: bool) -> dict[str, float]:
    """Fetch last-traded prices for a list of NSE symbols.

    Args:
        symbols: NSE trading symbols.
        dry_run: If True (or no dispatcher is wired up), returns deterministic
            synthetic prices instead of calling `mcp__kite__get_ltp`.

    Returns:
        symbol -> LTP. Missing quotes are simply absent from the result.
    """
    symbols = list(dict.fromkeys(symbols))
    if not symbols:
        return {}

    if dry_run or _mcp_dispatcher is None:
        logger.info("using synthetic LTPs (dry-run or no MCP dispatcher) for %s", symbols)
        return {s: float(technical_workup.fetch_ohlc(s, days=5).iloc[-1]["close"]) for s in symbols}

    try:
        quotes = _call_mcp_tool("mcp__kite__get_ltp", instruments=[f"NSE:{s}" for s in symbols])
    except Exception as exc:
        logger.warning("failed to fetch live LTPs, falling back to synthetic: %s", exc)
        return {s: float(technical_workup.fetch_ohlc(s, days=5).iloc[-1]["close"]) for s in symbols}

    return {s: quotes[f"NSE:{s}"]["last_price"] for s in symbols if f"NSE:{s}" in quotes}


def fetch_nifty_return_pct(window_start: str, dry_run: bool) -> Optional[float]:
    """Compute Nifty50's % return from `window_start` to today.

    Args:
        window_start: ISO date string, the evaluation window's start date.
        dry_run: If True (or no dispatcher is wired up), returns a
            deterministic synthetic return instead of calling
            `mcp__kite__get_historical_data`.

    Returns:
        The percent return, or ``None`` if live data was requested but
        unavailable.
    """
    if dry_run or _mcp_dispatcher is None:
        logger.info("using synthetic Nifty50 return (dry-run or no MCP dispatcher)")
        df = technical_workup.fetch_ohlc("NIFTY50", days=90)
        start = date.fromisoformat(window_start)
        window_df = df[df.index.date >= start]
        if window_df.empty:
            window_df = df
        start_close = float(window_df.iloc[0]["close"])
        end_close = float(window_df.iloc[-1]["close"])
        return (end_close - start_close) / start_close * 100

    try:
        candles = _call_mcp_tool(
            "mcp__kite__get_historical_data",
            instrument_token=NIFTY50_INSTRUMENT_TOKEN,
            from_date=window_start,
            to_date=date.today().isoformat(),
            interval="day",
        )
    except Exception as exc:
        logger.warning("failed to fetch Nifty50 historical data: %s", exc)
        return None
    if not candles:
        return None
    start_close = candles[0]["close"]
    end_close = candles[-1]["close"]
    return (end_close - start_close) / start_close * 100


def _unrealised_pct(unrealised_pnl: float, capital_deployed: float) -> float:
    """Return on capital actually deployed (0 if nothing is deployed)."""
    return (unrealised_pnl / capital_deployed * 100) if capital_deployed else 0.0


def _total_return_pct(unrealised_pnl: float, realised_pnl: float, capital_committed: float) -> float:
    """Return on total capital committed for the window (idle cash included)."""
    return ((unrealised_pnl + realised_pnl) / capital_committed * 100) if capital_committed else 0.0


def _format_positions_line(positions: dict[str, Any], ltps: dict[str, float]) -> tuple[str, str]:
    """Format the "Positions:" line(s) for one portfolio.

    Args:
        positions: `state["positions"]`.
        ltps: symbol -> LTP.

    Returns:
        ``(positions_line, top_bottom_line)`` — `top_bottom_line` is empty
        unless there are 2+ positions.
    """
    if not positions:
        return "none", ""

    if len(positions) == 1:
        symbol, pos = next(iter(positions.items()))
        ltp = ltps.get(symbol)
        ltp_str = f"₹{ltp:,.2f}" if ltp is not None else "N/A"
        return f"{symbol} · {pos['quantity']} @ avg ₹{pos['avg_price']:,.2f} · LTP {ltp_str}", ""

    symbols = sorted(positions)
    line = f"{len(positions)} active — {', '.join(symbols)}"

    perf = []
    for symbol, pos in positions.items():
        ltp = ltps.get(symbol)
        if ltp is None or not pos.get("avg_price"):
            continue
        pct = (ltp - pos["avg_price"]) / pos["avg_price"] * 100
        perf.append((symbol, pct))
    top_bottom = ""
    if len(perf) >= 2:
        top = max(perf, key=lambda p: p[1])
        bottom = min(perf, key=lambda p: p[1])
        top_bottom = f"(top: {top[0]} {top[1]:+.1f}%; bottom: {bottom[0]} {bottom[1]:+.1f}%)"
    return line, top_bottom


def _format_portfolio_block(
    label: str, state: Optional[dict[str, Any]], rules: dict[str, Any], dry_run: bool
) -> tuple[str, Optional[float]]:
    """Format one portfolio's digest block (LIVE or PAPER).

    Args:
        label: ``"LIVE (real Kite, ...)"`` or ``"PAPER (virtual, ...)"`` header text.
        state: This portfolio's loaded state, or ``None`` if no window is open.
        rules: `subagent.LIVE_RULES` or `subagent.PAPER_RULES`.
        dry_run: Passed through to `fetch_ltps`.

    Returns:
        ``(block_text, total_return_pct)`` — `total_return_pct` is None when
        no window is open (nothing to compute alpha against).
    """
    header = f"─── {label} ───"
    if state is None:
        return f"{header}\nNo window open yet — capital uncommitted.", None

    positions = state.get("positions", {})
    ltps = fetch_ltps(positions.keys(), dry_run)

    capital_committed = state.get("capital_committed", rules["total_capital"])
    capital_deployed = sum(p["quantity"] * p["avg_price"] for p in positions.values())
    unrealised_pnl = sum(
        (ltps[sym] - p["avg_price"]) * p["quantity"] for sym, p in positions.items() if sym in ltps
    )
    realised_pnl = state.get("realised_pnl", 0.0)

    positions_line, top_bottom_line = _format_positions_line(positions, ltps)
    gtt_count = sum(1 for p in positions.values() if p.get("gtt_ids"))

    blackout = set()
    try:
        blackout = earnings_calendar.get_blackout_set(EARNINGS_LOOKAHEAD_DAYS)
    except RuntimeError as exc:
        logger.warning("earnings blackout check unavailable for digest: %s", exc)
    held_bare = {s.upper() for s in positions}
    earnings_soon = sorted(held_bare & blackout)

    total_return_pct = _total_return_pct(unrealised_pnl, realised_pnl, capital_committed)

    lines = [
        header,
        f"Capital deployed:  ₹{capital_deployed:,.0f} of ₹{capital_committed:,.0f}",
        f"Positions:         {positions_line}",
    ]
    if top_bottom_line:
        lines.append(f"                   {top_bottom_line}")
    lines.extend(
        [
            f"Unrealised P&L:    {unrealised_pnl:+,.0f} ({_unrealised_pct(unrealised_pnl, capital_deployed):+.2f}%)",
            f"Realised P&L:      ₹{realised_pnl:+,.0f}",
            f"Total return:      {total_return_pct:+.2f}%",
            f"GTTs:              active {gtt_count}",
            f"Earnings ≤{EARNINGS_LOOKAHEAD_DAYS} days:  {', '.join(earnings_soon) if earnings_soon else 'none'}",
        ]
    )
    return "\n".join(lines), total_return_pct


def _pipeline_status_block(live_state: Optional[dict[str, Any]], paper_state: Optional[dict[str, Any]]) -> str:
    """Format the "Pipeline status" footer: screener freshness and shortlist size.

    Args:
        live_state: Loaded live state, or None.
        paper_state: Loaded paper state, or None.

    Returns:
        The formatted block.
    """
    last_run = (live_state or paper_state or {}).get("last_screener_run") or "never"
    universe = screener_filter.load_screener_output()
    if universe is None:
        mandate_line = "screener output missing or stale (>45 days) — re-run needed"
    else:
        live_positions = (live_state or {}).get("positions", {})
        paper_positions = (paper_state or {}).get("positions", {})
        shortlist = screener_filter.apply_mandate_filter(universe, live_positions, paper_positions, subagent.PAPER_RULES)
        mandate_line = f"{len(shortlist)} candidates identified"

    return "\n".join(
        [
            "─── Pipeline status ───",
            f"Screener:          last run {last_run}",
            f"Mandate filter:    {mandate_line}",
        ]
    )


def build_digest(
    live_state: Optional[dict[str, Any]],
    paper_state: Optional[dict[str, Any]],
    dry_run: bool = False,
) -> str:
    """Build the full SPEC.md §7 digest text for both portfolios.

    Args:
        live_state: Loaded `state.json`, or None if no live window is open.
        paper_state: Loaded `paper_state.json`, or None if no paper run is open.
        dry_run: Passed through to the LTP/Nifty50 fetchers.

    Returns:
        The complete digest, ready to post to Discord.
    """
    reference_state = live_state or paper_state
    window_start = reference_state.get("window_start") if reference_state else date.today().isoformat()
    window_end = reference_state.get("window_end") if reference_state else (date.today() + timedelta(days=subagent.WINDOW_DAYS)).isoformat()
    day_n = (date.today() - date.fromisoformat(window_start)).days + 1 if reference_state else 0

    nifty_return = fetch_nifty_return_pct(window_start, dry_run)
    nifty_line = f"Nifty50 same period:  {nifty_return:+.2f}%" if nifty_return is not None else "Nifty50 same period:  N/A"

    header_lines = [
        f"Window: {window_start} -> {window_end} (Day {day_n} of {subagent.WINDOW_DAYS})",
        nifty_line,
        "",
    ]

    live_block, live_total_return = _format_portfolio_block("LIVE (real Kite, ₹5,000)", live_state, subagent.LIVE_RULES, dry_run)
    paper_block, paper_total_return = _format_portfolio_block("PAPER (virtual, ₹50,000)", paper_state, subagent.PAPER_RULES, dry_run)
    pipeline_block = _pipeline_status_block(live_state, paper_state)

    if nifty_return is not None and live_total_return is not None:
        live_block += f"\nAlpha vs Nifty50:  {live_total_return - nifty_return:+.2f} pp   (target: +10 pp by day {subagent.WINDOW_DAYS})"

    if nifty_return is not None and paper_total_return is not None:
        paper_block += f"\nAlpha vs Nifty50:  {paper_total_return - nifty_return:+.2f} pp   (target: +10 pp by day {subagent.WINDOW_DAYS})"

    return "\n".join(header_lines) + "\n" + live_block + "\n\n" + paper_block + "\n\n" + pipeline_block


def post_to_discord(digest_text: str) -> None:
    """Post the digest to the MiniTrader Discord channel, threaded when possible.

    Args:
        digest_text: The formatted digest from `build_digest()`.

    Raises:
        RuntimeError: If posting fails on both the threaded and channel-level attempt.
    """
    try:
        _call_mcp_tool("mcp__discord__send_message", channel_id=DISCORD_CHANNEL_ID, thread_id=DISCORD_THREAD_ID, content=digest_text)
        return
    except Exception as exc:
        logger.warning("threaded Discord post failed (%s), falling back to channel-level post", exc)

    try:
        _call_mcp_tool("mcp__discord__send_message", channel_id=DISCORD_CHANNEL_ID, content=digest_text)
    except Exception as exc:
        logger.error("failed to post digest to Discord: %s", exc)
        raise RuntimeError(f"failed to post digest to Discord: {exc}") from exc


def main() -> None:
    """CLI entry point for `daily_digest.py`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(prog="daily_digest.py", description="Phase 6 dual-portfolio daily digest")
    parser.add_argument("--dry-run", action="store_true", help="Print the digest instead of posting to Discord")
    args = parser.parse_args()

    live_state = subagent.load_state(subagent.STATE_PATH)
    paper_state = subagent.load_state(subagent.PAPER_STATE_PATH)

    digest = build_digest(live_state, paper_state, dry_run=args.dry_run)

    if args.dry_run:
        print(digest)
        return

    post_to_discord(digest)
    print(digest)


if __name__ == "__main__":
    main()
