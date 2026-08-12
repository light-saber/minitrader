"""Earnings/results-date calendar fetcher for the Phase 3 blackout filter.

Fetches upcoming results-calendar entries from StockeZee (primary,
https://www.stockezee.com/results-calendar) or FreeScreener (fallback,
https://freescreener.in/calendar) per SPEC.md §11 resolved question 5, and
exposes a rolling blackout set of symbols reporting within the next N
trading days. `technical_workup.py` calls `get_blackout_set()` to apply the
"no earnings/blackout event in next 5 days" PASS condition (SPEC.md §4,
Phase 3).

KNOWN LIMITATION (TODO): both source sites are client-side rendered Next.js
apps — their calendar tables are populated by JS/XHR after page load, not
present in the raw server HTML the spec assumed was "scrape-friendly."
`_parse_html_table()` is the spec-mandated bs4 approach and will pick up a
real table the moment either site ships (or reverts to) server-rendered
markup. Until then, `_parse_next_data_json()` is a secondary fallback that
reads StockeZee's embedded `__NEXT_DATA__` script tag, which currently only
carries an SEO sample of *today's* reporters (not a full forward-looking
calendar) — real multi-day coverage needs the sites' actual XHR/data
endpoints, which were not discoverable via plain `requests` (no headless
browser is in requirements.txt). FreeScreener uses Next.js App Router RSC
streaming (`self.__next_f.push(...)`) instead of `__NEXT_DATA__`, so it
currently yields no events via either parser and exists purely as the
spec-mandated fallback source.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)
REQUEST_HEADERS = {"User-Agent": USER_AGENT}
REQUEST_TIMEOUT_SECONDS = 15

CACHE_PATH = Path("/tmp/.earnings_cache.json")
CACHE_TTL_SECONDS = 6 * 60 * 60

SOURCES: dict[str, str] = {
    "stockezee": "https://www.stockezee.com/results-calendar",
    "freescreener": "https://freescreener.in/calendar",
}


def _parse_date(raw: str) -> Optional[date]:
    """Parse a date string in any of the calendar sites' common formats.

    Args:
        raw: Raw date text scraped from a table cell or JSON field.

    Returns:
        The parsed date, or ``None`` if no known format matched.
    """
    raw = raw.strip()
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).date()
    except ValueError:
        pass
    for fmt in ("%Y-%m-%d", "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y", "%B %d, %Y"):
        try:
            return datetime.strptime(raw, fmt).date()
        except ValueError:
            continue
    return None


def _parse_html_table(html: str) -> list[dict[str, Any]]:
    """Scrape any `<table>` with symbol + date (and optionally company) columns.

    Args:
        html: Raw response body of a results-calendar page.

    Returns:
        A list of ``{"symbol", "company", "result_date"}`` dicts, with
        `result_date` as an ISO date string. Empty if no matching table.
    """
    soup = BeautifulSoup(html, "html.parser")
    events: list[dict[str, Any]] = []
    for table in soup.find_all("table"):
        headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
        if not headers:
            continue
        symbol_idx = next((i for i, h in enumerate(headers) if "symbol" in h or "scrip" in h), None)
        date_idx = next((i for i, h in enumerate(headers) if "date" in h), None)
        company_idx = next((i for i, h in enumerate(headers) if "company" in h or "name" in h), None)
        if symbol_idx is None or date_idx is None:
            continue
        for row in table.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) <= max(symbol_idx, date_idx):
                continue
            symbol = cells[symbol_idx].get_text(strip=True)
            parsed_date = _parse_date(cells[date_idx].get_text(strip=True))
            if not symbol or parsed_date is None:
                continue
            company = (
                cells[company_idx].get_text(strip=True)
                if company_idx is not None and len(cells) > company_idx
                else symbol
            )
            events.append({"symbol": symbol.upper(), "company": company, "result_date": parsed_date.isoformat()})
    return events


def _parse_next_data_json(html: str) -> list[dict[str, Any]]:
    """Fallback for Next.js `__NEXT_DATA__` pages: pull any {symbol, date} dicts.

    See module TODO — on StockeZee this currently surfaces only a same-day
    SEO sample, not the full calendar.

    Args:
        html: Raw response body of a results-calendar page.

    Returns:
        A list of ``{"symbol", "company", "result_date"}`` dicts, possibly empty.
    """
    soup = BeautifulSoup(html, "html.parser")
    tag = soup.find("script", id="__NEXT_DATA__")
    if tag is None or not tag.string:
        return []
    try:
        data = json.loads(tag.string)
    except json.JSONDecodeError:
        return []

    events: list[dict[str, Any]] = []

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            if "symbol" in node and ("date" in node or "result_date" in node):
                raw_date = str(node.get("date") or node.get("result_date"))
                parsed_date = _parse_date(raw_date)
                if parsed_date is not None:
                    events.append(
                        {
                            "symbol": str(node["symbol"]).upper(),
                            "company": str(node.get("company", node["symbol"])),
                            "result_date": parsed_date.isoformat(),
                        }
                    )
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(data)
    return events


def _fetch_source(source: str) -> list[dict[str, Any]]:
    """Fetch and parse a single calendar source.

    Args:
        source: Key into `SOURCES` ("stockezee" or "freescreener").

    Returns:
        Parsed events (possibly empty if the page has no scrapeable data).

    Raises:
        requests.RequestException: On network/HTTP failure.
    """
    url = SOURCES[source]
    response = requests.get(url, headers=REQUEST_HEADERS, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()
    events = _parse_html_table(response.text)
    if not events:
        events = _parse_next_data_json(response.text)
    return events


def fetch_earnings_calendar(source: str = "stockezee") -> list[dict[str, Any]]:
    """Fetch the results calendar, preferring `source` and falling back automatically.

    Tries `source` first; on failure (network error, HTTP error, or no
    parseable events), tries the other configured source. Never hits any
    cache — see `get_earnings_calendar()` for the cached wrapper used by
    `get_blackout_set()`.

    Args:
        source: Which source to try first — ``"stockezee"`` or ``"freescreener"``.

    Returns:
        A list of ``{"symbol": str, "company": str, "result_date": date}`` dicts.

    Raises:
        RuntimeError: If every configured source fails.
    """
    if source not in SOURCES:
        raise ValueError(f"unknown earnings source {source!r}, expected one of {sorted(SOURCES)}")

    order = [source] + [s for s in SOURCES if s != source]
    errors: dict[str, str] = {}
    for src in order:
        try:
            raw_events = _fetch_source(src)
        except requests.RequestException as exc:
            errors[src] = str(exc)
            logger.warning("earnings fetch failed for source %s: %s", src, exc)
            continue
        if not raw_events:
            errors[src] = "no events parsed from response (page structure changed or JS-rendered)"
            logger.warning("earnings fetch for source %s returned no events", src)
            continue
        logger.info("fetched %d earnings events from %s", len(raw_events), src)
        return [
            {"symbol": e["symbol"], "company": e["company"], "result_date": date.fromisoformat(e["result_date"])}
            for e in raw_events
        ]

    raise RuntimeError(
        "earnings calendar fetch failed for all sources "
        f"({', '.join(SOURCES)}): {errors}. Check network connectivity and "
        "whether stockezee.com / freescreener.in changed their page structure."
    )


def _load_cache() -> Optional[list[dict[str, Any]]]:
    """Read the on-disk earnings cache if present and within TTL.

    Returns:
        Cached raw event dicts (ISO date strings), or ``None`` if missing,
        unreadable, or stale.
    """
    if not CACHE_PATH.exists():
        return None
    try:
        with CACHE_PATH.open("r", encoding="utf-8") as f:
            cached = json.load(f)
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("failed to read earnings cache %s: %s", CACHE_PATH, exc)
        return None
    fetched_at = cached.get("fetched_at", 0)
    if time.time() - fetched_at > CACHE_TTL_SECONDS:
        return None
    return cached.get("events", [])


def _save_cache(events: list[dict[str, Any]]) -> None:
    """Write raw (ISO-date) event dicts to the on-disk cache.

    Args:
        events: Event dicts with `result_date` as an ISO date string.
    """
    payload = {"fetched_at": time.time(), "events": events}
    try:
        with CACHE_PATH.open("w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)
    except OSError as exc:
        logger.warning("failed to write earnings cache to %s: %s", CACHE_PATH, exc)


def get_earnings_calendar(source: str = "stockezee") -> list[dict[str, Any]]:
    """Return the earnings calendar, using a 6-hour on-disk cache when fresh.

    Args:
        source: Which source to prefer if a network fetch is needed.

    Returns:
        A list of ``{"symbol": str, "company": str, "result_date": date}`` dicts.

    Raises:
        RuntimeError: If the cache is stale/missing and every source fails.
    """
    cached = _load_cache()
    if cached is not None:
        logger.info("using cached earnings calendar (%d events, ttl=%ss)", len(cached), CACHE_TTL_SECONDS)
        return [
            {"symbol": e["symbol"], "company": e["company"], "result_date": date.fromisoformat(e["result_date"])}
            for e in cached
        ]

    events = fetch_earnings_calendar(source=source)
    _save_cache(
        [
            {"symbol": e["symbol"], "company": e["company"], "result_date": e["result_date"].isoformat()}
            for e in events
        ]
    )
    return events


def _next_trading_days(start: date, count: int) -> set[date]:
    """Return `count` weekday (Mon-Fri) trading days starting from `start`, inclusive.

    Does not account for NSE market holidays — a documented simplification;
    it errs conservative (may include a couple of extra non-trading days,
    never fewer than requested).

    Args:
        start: First day to consider (included if it is a weekday).
        count: Number of trading days to collect.

    Returns:
        The set of trading dates.
    """
    days: set[date] = set()
    current = start
    while len(days) < count:
        if current.weekday() < 5:
            days.add(current)
        current += timedelta(days=1)
    return days


def get_blackout_set(lookahead_days: int = 5) -> set[str]:
    """Return symbols with a results date within the next `lookahead_days` trading days.

    Args:
        lookahead_days: Number of upcoming trading (business) days to check,
            inclusive of today.

    Returns:
        A set of uppercase NSE symbols to exclude from BUY-READY verdicts.

    Raises:
        RuntimeError: If the earnings calendar cannot be fetched from any source.
    """
    events = get_earnings_calendar()
    trading_window = _next_trading_days(date.today(), lookahead_days)
    blackout = {e["symbol"] for e in events if e["result_date"] in trading_window}
    logger.info("blackout set (next %d trading days): %s", lookahead_days, sorted(blackout))
    return blackout


def main() -> None:
    """CLI entry point for `earnings_calendar.py`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(prog="earnings_calendar.py", description="Earnings/results calendar fetcher")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print the upcoming blackout symbol set")
    parser.add_argument("--lookahead-days", type=int, default=5, help="Trading days to look ahead (default: 5)")
    args = parser.parse_args()

    if not args.dry_run:
        parser.print_help()
        return

    blackout = get_blackout_set(lookahead_days=args.lookahead_days)
    print(f"Blackout set (next {args.lookahead_days} trading days): {sorted(blackout)}")


if __name__ == "__main__":
    main()
