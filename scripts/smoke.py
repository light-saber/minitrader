"""Offline-safe end-to-end MiniTrader smoke test."""

from __future__ import annotations

import json
import logging
import subprocess
import sys
from datetime import date
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

import chart_render
import kite_session
import technical_workup

logger = logging.getLogger(__name__)
SHORTLIST_PATH = Path("/root/.hermes/cron/output/stock_screener_with_momentum.json")
MOCK_SHORTLIST = ["INFY", "TCS", "RELIANCE", "HDFCBANK", "ICICIBANK"]


def _load_shortlist() -> list[str]:
    """Return screener symbols, using a deterministic five-name fallback.

    Returns:
        A non-empty list of bare NSE symbols.
    """
    if not SHORTLIST_PATH.exists():
        logger.warning("shortlist file is missing; using smoke-test mock shortlist")
        return MOCK_SHORTLIST
    try:
        records: list[dict[str, Any]] = json.loads(SHORTLIST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("could not read shortlist %s; using mock shortlist: %s", SHORTLIST_PATH, exc)
        return MOCK_SHORTLIST
    symbols = [str(item.get("symbol", "")).upper().removesuffix(".NS") for item in records if item.get("symbol")]
    return symbols or MOCK_SHORTLIST


def main() -> int:
    """Run the end-to-end smoke test and return its process status.

    Returns:
        Zero for a completed or intentionally offline smoke test.
    """
    print("=== MiniTrader end-to-end smoke test ===")
    session_state = kite_session.get_session_status()
    if session_state == "expired":
        print("Kite session is expired; offline smoke test passes without live checks.")
        print("=== Smoke test passed ===")
        return 0

    earnings = subprocess.run(
        ["make", "earnings"], cwd=ROOT_DIR, check=True, capture_output=True, text=True
    )
    print(earnings.stdout.strip())
    if earnings.stderr:
        logger.info("make earnings stderr: %s", earnings.stderr.strip())

    symbol = _load_shortlist()[0]
    if session_state != "active":
        logger.warning("Kite session state is %s; technical workup may use synthetic OHLC", session_state)
    df = technical_workup.fetch_ohlc_cached(symbol)
    df = technical_workup.compute_indicators(df)
    result = technical_workup.verdict(symbol, df)
    print(f"{symbol} verdict: {result}")

    today = date.today().strftime("%Y%m%d")
    png_path = chart_render.render_png(symbol, df, chart_render.PNG_DIR / f"{symbol}_{today}.png")
    html_path = chart_render.render_html(symbol, df, chart_render.HTML_DIR / f"{symbol}_{today}.html")
    print(f"PNG: {png_path}")
    print(f"HTML: {html_path}")
    print("=== Smoke test passed ===")
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    raise SystemExit(main())
