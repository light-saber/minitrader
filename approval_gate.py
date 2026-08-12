"""Discord human-approval gate for MiniTrader buy recommendations.

This module deliberately has no Discord dependency.  The production harness
injects its Discord MCP dispatcher with :func:`set_discord_dispatcher`; without
one, prompts receive an ``OFFLINE_`` id and decisions fail closed as a timeout.
"""

from __future__ import annotations

import argparse
import csv
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Optional, Union

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
APPROVAL_LOG_PATH = BASE_DIR / "logs" / "approval_log.csv"
DEFAULT_CHANNEL_ID = "1517387649520762960"

_discord_dispatcher: Optional[Callable[..., Any]] = None


class DiscordUnavailable(RuntimeError):
    """Raised when a Discord MCP dispatcher has not been configured."""


def set_discord_dispatcher(fn: Callable[..., Any]) -> None:
    """Inject the runtime's Discord MCP dispatcher.

    Args:
        fn: Callable accepting a tool name followed by keyword arguments.
    """
    global _discord_dispatcher
    _discord_dispatcher = fn


def _call_discord(tool: str, **kwargs: Any) -> Any:
    """Call a Discord tool through the injected dispatcher.

    Raises:
        DiscordUnavailable: If the module is running outside the dispatcher.
    """
    if _discord_dispatcher is None:
        raise DiscordUnavailable("no dispatcher wired; call set_discord_dispatcher() first")
    return _discord_dispatcher(tool, **kwargs)


def _condition(value: Any, passed: bool) -> str:
    """Format one visible technical condition."""
    return f"{'PASS' if passed else 'FAIL'}  {value}"


def build_buy_prompt(
    portfolio_name: str,
    symbol: str,
    quantity: int,
    ltp: float,
    verdict: str,
    indicators: dict[str, Any],
    chart_png_path: str | None,
    chart_html_path: str | None,
) -> str:
    """Build the SPEC Phase-4 Discord message body without posting it.

    Args:
        portfolio_name: ``"live"`` or ``"paper"``.
        symbol: NSE trading symbol.
        quantity: Suggested whole-share quantity.
        ltp: Last traded price used for the suggestion.
        verdict: Technical verdict.
        indicators: Technical-workup metadata and optional sector/GTT fields.
        chart_png_path: Local PNG chart path, if rendered.
        chart_html_path: Local interactive chart path, if rendered.

    Returns:
        The ready-to-send Discord message.
    """
    label = "LIVE" if portfolio_name.lower() == "live" else "PAPER"
    sector = indicators.get("sector", "Unknown")
    above_50 = bool(indicators.get("above_50dma", indicators.get("above_50_dma", False)))
    above_200 = bool(indicators.get("above_200dma", indicators.get("above_200_dma", False)))
    rsi = indicators.get("rsi14")
    rsi_pass = isinstance(rsi, (int, float)) and 40 <= rsi <= 70
    volume_ratio = indicators.get("volume_ratio", indicators.get("volume_vs_20d"))
    volume_pass = bool(indicators.get("volume_ok", volume_ratio is not None and float(volume_ratio) >= 1.0))
    earnings_clear = bool(indicators.get("earnings_clear", not indicators.get("earnings_blackout", False)))
    gtt = indicators.get("gtt") or indicators.get("gtt_recommendation")
    if not gtt:
        gtt = "Attach -7% stop / +20% target" if (isinstance(rsi, (int, float)) and rsi > 65) else "Not recommended (clean uptrend)"

    lines = [
        f"[{label}] BUY RECOMMENDATION",
        f"Symbol: {symbol.upper()} · Sector: {sector}",
        f"Suggested: {quantity} shares × ₹{ltp:,.2f} = ₹{quantity * ltp:,.2f}",
        f"Verdict: {verdict}",
        "```",
        _condition(f"Price > 50 DMA: {'yes' if above_50 else 'no'}", above_50),
        _condition(f"Price > 200 DMA: {'yes' if above_200 else 'no'}", above_200),
        _condition(f"RSI(14): {rsi if rsi is not None else 'N/A'} (target 40–70)", rsi_pass),
        _condition(f"Volume / 20D avg: {volume_ratio if volume_ratio is not None else 'N/A'} (min 1.0x)", volume_pass),
        _condition(f"Earnings blackout (next 5d): {'clear' if earnings_clear else 'blocked'}", earnings_clear),
        "```",
        f"GTT: {gtt}",
    ]
    if label == "PAPER":
        lines.append("This is paper — a virtual fill at the LTP when you approve.")
    if chart_png_path:
        lines.append(f"PNG chart: <file://{Path(chart_png_path).resolve()}>")
    if chart_html_path:
        lines.append(f"Interactive chart: <file://{Path(chart_html_path).resolve()}>")
    lines.append("Reply: go / skip / customize <qty|price>")
    return "\n".join(lines)


def _message_id(result: Any) -> str:
    """Extract a Discord message id from common dispatcher response shapes."""
    if isinstance(result, dict):
        return str(result.get("id") or result.get("message_id") or result.get("data", {}).get("id") or "")
    return str(getattr(result, "id", ""))


def post_buy_prompt(
    portfolio_name: str, symbol: str, quantity: int, ltp: float, verdict: str,
    indicators: dict[str, Any], chart_png_path: str | None, chart_html_path: str | None,
    channel_id: str = DEFAULT_CHANNEL_ID, thread_id: str | None = None,
) -> str:
    """Post a buy prompt to Discord and return its message id.

    Without an injected dispatcher, returns a synthetic id so callers can
    follow the same fail-closed timeout path during local smoke checks.
    """
    body = build_buy_prompt(portfolio_name, symbol, quantity, ltp, verdict, indicators, chart_png_path, chart_html_path)
    if _discord_dispatcher is None:
        synthetic_id = f"OFFLINE_{uuid.uuid4().hex}"
        logger.warning("Discord unavailable; would post buy prompt %s to channel %s", synthetic_id, channel_id)
        return synthetic_id
    kwargs: dict[str, Any] = {"channel_id": channel_id, "content": body}
    if thread_id:
        kwargs["thread_id"] = thread_id
    result = _call_discord("create_message", **kwargs)
    message_id = _message_id(result)
    if not message_id:
        raise RuntimeError("Discord create_message returned no message id")
    return message_id


def _messages(result: Any) -> list[dict[str, Any]]:
    """Normalize common Discord fetch response shapes into message dicts."""
    if isinstance(result, list):
        return [m for m in result if isinstance(m, dict)]
    if isinstance(result, dict):
        items = result.get("messages") or result.get("data") or []
        return items if isinstance(items, list) else []
    return []


def _parse_customize(text: str) -> dict[str, float | int]:
    """Parse optional quantity and price values following ``customize``."""
    args: dict[str, float | int] = {}
    tail = text.strip()[len("customize"):].strip()
    for key, value in re.findall(r"(?:^|\s)(qty|quantity|price)\s*=\s*([0-9]+(?:\.[0-9]+)?)", tail, re.I):
        if key.lower() in {"qty", "quantity"}:
            args["qty"] = int(float(value))
        else:
            args["price"] = float(value)
    if not args:
        values = re.findall(r"[0-9]+(?:\.[0-9]+)?", tail)
        if values:
            args["qty"] = int(float(values[0]))
        if len(values) > 1:
            args["price"] = float(values[1])
    return args


def await_decision(
    message_id: str, channel_id: str = DEFAULT_CHANNEL_ID, timeout_minutes: int = 30,
) -> Union[str, tuple[str, dict[str, float | int]]]:
    """Wait for a reply to a prompt, returning a decision or customize args.

    Returns ``"go"``, ``"skip"``, or ``"timeout"``. For a customize
    reply it returns ``("customize", {"qty": ..., "price": ...})``;
    either parsed field may be absent. Only literal ``go`` is executable.
    """
    if _discord_dispatcher is None or message_id.startswith("OFFLINE_"):
        logger.warning("Discord unavailable; synthetic approval %s times out after one poll", message_id)
        return "timeout"
    deadline = time.monotonic() + timeout_minutes * 60
    while time.monotonic() <= deadline:
        result = _call_discord("fetch_messages", channel_id=channel_id)
        for message in _messages(result):
            author = message.get("author", {})
            author_name = (author.get("username") or author.get("name")) if isinstance(author, dict) else str(author)
            if str(author_name).lower() == "minitrader":
                continue
            reference = message.get("reference") or message.get("message_reference") or {}
            parent_id = str(reference.get("message_id", "")) if isinstance(reference, dict) else ""
            if parent_id and parent_id != message_id:
                continue
            content = str(message.get("content", "")).strip()
            lowered = content.lower()
            if lowered == "go":
                return "go"
            if lowered == "skip":
                return "skip"
            if lowered.startswith("customize"):
                return "customize", _parse_customize(content)
        time.sleep(5)
    return "timeout"


def record_decision(message_id: str, decision: str, symbol: str, portfolio_name: str) -> None:
    """Append an approval decision to ``logs/approval_log.csv`` for audit."""
    APPROVAL_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    is_new = not APPROVAL_LOG_PATH.exists()
    with APPROVAL_LOG_PATH.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=["timestamp", "message_id", "decision", "symbol", "portfolio"])
        if is_new:
            writer.writeheader()
        writer.writerow({"timestamp": datetime.now(timezone.utc).isoformat(), "message_id": message_id,
                         "decision": decision, "symbol": symbol.upper(), "portfolio": portfolio_name.lower()})


def main() -> None:
    """Print an offline-safe sample prompt."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    parser = argparse.ArgumentParser(description="MiniTrader Discord approval gate")
    parser.add_argument("--demo", action="store_true", help="Print an INFY sample prompt without posting")
    args = parser.parse_args()
    if not args.demo:
        parser.print_help()
        return
    body = build_buy_prompt("paper", "INFY", 3, 1500.00, "BUY-READY", {
        "sector": "IT", "rsi14": 45.2, "above_50dma": True, "above_200dma": True,
        "volume_ratio": 1.2, "earnings_clear": True,
    }, None, None)
    print(body)
    print(f"would post to Discord channel {DEFAULT_CHANNEL_ID}, awaiting reply")


if __name__ == "__main__":
    main()
