"""Centralised Kite Connect session wrapper — the only module that calls `mcp__kite__*`.

Every other module that needs live Kite data (holdings, positions, LTP,
historical OHLC) goes through this module rather than calling an MCP tool
directly. This keeps the "is the session alive" question, the
symbol -> instrument_token resolution, and the response-shape parsing in one
place.

MCP integration seam: mirrors the pattern already used by `kite_exec.py` and
`daily_digest.py` — `_call_mcp_tool()` raises `NotImplementedError` until the
Hermes runtime harness injects a real dispatcher via `set_mcp_dispatcher()`.
A bare `python kite_session.py` (or `import kite_session`) never touches a
live Kite session. SPEC.md's own naming (`mcp__kite__get_profile`, etc.) is
used for the tool-name strings passed to the dispatcher; the injected
dispatcher owns whatever real name mapping its deployment needs (in this dev
session, the equivalent tools are connected under the `mcp__claude_ai_Kite__*`
prefix).

Missing/unreachable MCP is treated as equivalent to an expired session
(build brief, Phase 1 "CRITICAL" note) — every read helper below fails safe
(empty result or `KiteSessionExpired`) rather than raising an unexpected
exception into caller code.
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from datetime import date, datetime
from pathlib import Path
from typing import Any, Callable, Literal, Optional

import pandas as pd

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
INSTRUMENT_CACHE_PATH = BASE_DIR / ".venv" / "var" / "instruments_cache.json"

# Substrings looked for (case-insensitively) in an error message or a
# get_profile response to recognize an expired/invalid Kite session. Kite
# Connect's own exceptions (TokenException etc.) and the MCP gateway's own
# wording are both covered on a best-effort basis.
SESSION_ERROR_SUBSTRINGS = (
    "session expired",
    "invalid_token",
    "invalid token",
    "incorrect `api_key`",
    "incorrect api_key",
    "login required",
    "token exception",
    "tokenexception",
    "access_token",
    "not authorized",
    "unauthorized",
)

_mcp_dispatcher: Optional[Callable[..., Any]] = None


class KiteSessionExpired(RuntimeError):
    """Raised when the Kite session is not active (or the MCP gateway is unreachable)."""


def set_mcp_dispatcher(dispatcher: Callable[..., Any]) -> None:
    """Inject the Hermes runtime's MCP tool-calling function.

    Args:
        dispatcher: Callable with signature ``dispatcher(tool_name: str, **kwargs) -> Any``,
            wired up by the live sub-agent runtime to actually invoke the
            named MCP tool and return its structured result.
    """
    global _mcp_dispatcher
    _mcp_dispatcher = dispatcher


def _call_mcp_tool(tool_name: str, **kwargs: Any) -> Any:
    """Invoke an MCP tool through the injected runtime dispatcher.

    Args:
        tool_name: MCP tool name, e.g. ``"mcp__kite__get_profile"``.
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


def _looks_like_session_error(text: str) -> bool:
    """Case-insensitive substring match against `SESSION_ERROR_SUBSTRINGS`."""
    lowered = text.lower()
    return any(substr in lowered for substr in SESSION_ERROR_SUBSTRINGS)


def get_session_status() -> Literal["active", "expired", "unknown"]:
    """Check whether the Kite session is currently authenticated.

    Calls `mcp__kite__get_profile`. A missing/unreachable MCP dispatcher is
    treated the same as an expired session (build brief, Phase 1).

    Returns:
        ``"active"`` if a profile came back, ``"expired"`` if the response
        (or the raised exception) looks like a session/auth error,
        ``"unknown"`` otherwise.
    """
    try:
        profile = _call_mcp_tool("mcp__kite__get_profile")
    except NotImplementedError as exc:
        logger.warning("Kite MCP not reachable, treating session as expired: %s", exc)
        return "expired"
    except Exception as exc:
        if _looks_like_session_error(str(exc)):
            return "expired"
        logger.warning("get_profile raised an unexpected error: %s", exc)
        return "unknown"

    if isinstance(profile, dict):
        if profile.get("user_id") or profile.get("client_id") or profile.get("user_name"):
            return "active"
        if _looks_like_session_error(json.dumps(profile)):
            return "expired"
        logger.warning("get_profile returned an unrecognized shape: keys=%s", list(profile.keys()))
        return "unknown"
    if isinstance(profile, str):
        return "expired" if _looks_like_session_error(profile) else "unknown"
    logger.warning("get_profile returned an unrecognized type: %s", type(profile))
    return "unknown"


def ensure_session() -> None:
    """Raise `KiteSessionExpired` unless the Kite session is currently active.

    Raises:
        KiteSessionExpired: If the session is expired, unreachable, or in an
            unrecognized state — callers should stop and ask Sachin to
            re-authenticate rather than guess.
    """
    status = get_session_status()
    if status != "active":
        raise KiteSessionExpired(
            f"Kite session not {status!r} — call mcp__kite__login and confirm login in chat, then retry."
        )


def get_holdings() -> list[dict[str, Any]]:
    """Return current Kite holdings, or ``[]`` on any error.

    Returns:
        A list of holding dicts (Kite `get_holdings` shape). Never raises —
        callers decide whether an empty list is meaningful.
    """
    try:
        result = _call_mcp_tool("mcp__kite__get_holdings")
    except Exception as exc:
        logger.warning("get_holdings failed, returning []: %s", exc)
        return []
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        return result.get("data") or result.get("holdings") or []
    return []


def get_positions() -> list[dict[str, Any]]:
    """Return current Kite positions (net), or ``[]`` on any error.

    Returns:
        A list of position dicts. Kite's own `get_positions` response is
        typically ``{"net": [...], "day": [...]}``; this returns the `net`
        book. Never raises.
    """
    try:
        result = _call_mcp_tool("mcp__kite__get_positions")
    except Exception as exc:
        logger.warning("get_positions failed, returning []: %s", exc)
        return []
    if isinstance(result, list):
        return result
    if isinstance(result, dict):
        if "net" in result:
            return result["net"] or []
        return result.get("data") or result.get("positions") or []
    return []


def get_ltp(symbols: list[str]) -> dict[str, float]:
    """Fetch last-traded prices for NSE symbols.

    Args:
        symbols: Bare NSE trading symbols, e.g. ``["INFY", "SBIN"]``.

    Returns:
        ``{"INFY": 1620.50, ...}`` — symbols with no quote are simply
        absent. Empty dict on any error (never raises).
    """
    if not symbols:
        return {}
    instruments = [f"NSE:{s.upper()}" for s in symbols]
    try:
        result = _call_mcp_tool("mcp__kite__get_ltp", instruments=instruments)
    except Exception as exc:
        logger.warning("get_ltp failed for %s: %s", symbols, exc)
        return {}

    data = result.get("data", result) if isinstance(result, dict) else {}
    out: dict[str, float] = {}
    for key, val in data.items():
        bare = key.split(":")[-1].upper()
        price = val.get("last_price") if isinstance(val, dict) else val
        if price is not None:
            out[bare] = float(price)
    return out


def get_quote(symbols: list[str]) -> dict[str, dict[str, Any]]:
    """Fetch full market-data quotes (OHLCV + depth) for NSE symbols.

    Args:
        symbols: Bare NSE trading symbols.

    Returns:
        ``{"INFY": {...quote...}, ...}``. Empty dict on any error.
    """
    if not symbols:
        return {}
    instruments = [f"NSE:{s.upper()}" for s in symbols]
    try:
        result = _call_mcp_tool("mcp__kite__get_quotes", instruments=instruments)
    except Exception as exc:
        logger.warning("get_quote failed for %s: %s", symbols, exc)
        return {}

    data = result.get("data", result) if isinstance(result, dict) else {}
    return {key.split(":")[-1].upper(): val for key, val in data.items() if isinstance(val, dict)}


def _load_instrument_cache() -> dict[str, Any]:
    """Read the on-disk symbol -> instrument_token cache, tolerating a missing/corrupt file."""
    if not INSTRUMENT_CACHE_PATH.exists():
        return {}
    try:
        return json.loads(INSTRUMENT_CACHE_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("failed to read instrument cache %s: %s", INSTRUMENT_CACHE_PATH, exc)
        return {}


def _save_instrument_cache(cache: dict[str, Any]) -> None:
    """Write the symbol -> instrument_token cache."""
    INSTRUMENT_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        INSTRUMENT_CACHE_PATH.write_text(json.dumps(cache, indent=2), encoding="utf-8")
    except OSError as exc:
        logger.warning("failed to write instrument cache %s: %s", INSTRUMENT_CACHE_PATH, exc)


def _resolve_instrument_token(symbol: str, exchange: str = "NSE") -> int:
    """Resolve `symbol` to a Kite `instrument_token`, using the on-disk cache.

    Args:
        symbol: Bare NSE trading symbol.
        exchange: Exchange to prefer when multiple matches are returned.

    Returns:
        The resolved instrument token.

    Raises:
        RuntimeError: If `search_instruments` returns no usable match.
    """
    bare = symbol.upper()
    cache_key = f"{exchange}:{bare}"
    cache = _load_instrument_cache()
    cached = cache.get(cache_key)
    if cached and cached.get("instrument_token"):
        return int(cached["instrument_token"])

    result = _call_mcp_tool("mcp__kite__search_instruments", query=bare, filter_on="tradingsymbol")
    matches = result if isinstance(result, list) else (result.get("data") or result.get("instruments") or [])
    logger.debug("search_instruments(%s) raw match count=%d", bare, len(matches))

    token: Optional[int] = None
    for m in matches:
        if not isinstance(m, dict):
            continue
        m_symbol = str(m.get("tradingsymbol", "")).upper()
        m_exchange = str(m.get("exchange", "")).upper()
        if m_symbol == bare and (not m_exchange or m_exchange == exchange):
            token = m.get("instrument_token")
            break
    if token is None and matches and isinstance(matches[0], dict):
        token = matches[0].get("instrument_token")

    if token is None:
        raise RuntimeError(f"could not resolve instrument_token for {exchange}:{bare} via search_instruments")

    cache[cache_key] = {"instrument_token": int(token), "cached_at": datetime.now().isoformat()}
    _save_instrument_cache(cache)
    return int(token)


def historical_data(symbol: str, from_date: date, to_date: date, interval: str = "day") -> pd.DataFrame:
    """Fetch daily (or other interval) OHLCV history for `symbol` from Kite.

    Replaces the `technical_workup.fetch_ohlc` mock. Resolves `symbol` to an
    instrument_token first (cached on disk), then calls
    `mcp__kite__get_historical_data`.

    Args:
        symbol: Bare NSE trading symbol, e.g. ``"INFY"``.
        from_date: Start date (inclusive).
        to_date: End date (inclusive).
        interval: Kite candle interval — ``"day"`` by default.

    Returns:
        A DataFrame indexed by date (DatetimeIndex) with columns
        ``open, high, low, close, volume``, oldest row first.

    Raises:
        KiteSessionExpired: If the session isn't active.
        RuntimeError: If the instrument can't be resolved or the API call fails.
    """
    ensure_session()
    token = _resolve_instrument_token(symbol)

    try:
        raw = _call_mcp_tool(
            "mcp__kite__get_historical_data",
            instrument_token=token,
            from_date=f"{from_date.isoformat()} 00:00:00",
            to_date=f"{to_date.isoformat()} 23:59:59",
            interval=interval,
        )
    except Exception as exc:
        raise RuntimeError(f"historical_data fetch failed for {symbol}: {exc}") from exc

    logger.debug("get_historical_data(%s) raw type=%s", symbol, type(raw).__name__)
    records = raw if isinstance(raw, list) else (raw.get("candles") or raw.get("data") or [])

    rows: list[dict[str, Any]] = []
    for rec in records:
        if isinstance(rec, dict):
            rows.append(
                {
                    "date": rec.get("date"),
                    "open": rec.get("open"),
                    "high": rec.get("high"),
                    "low": rec.get("low"),
                    "close": rec.get("close"),
                    "volume": rec.get("volume", 0),
                }
            )
        elif isinstance(rec, (list, tuple)) and len(rec) >= 6:
            rows.append({"date": rec[0], "open": rec[1], "high": rec[2], "low": rec[3], "close": rec[4], "volume": rec[5]})

    if not rows:
        raise RuntimeError(f"historical_data returned no candles for {symbol} ({from_date} to {to_date})")

    df = pd.DataFrame(rows)
    df["date"] = pd.to_datetime(df["date"], utc=True).dt.tz_localize(None)
    df = df.set_index("date").sort_index()
    return df[["open", "high", "low", "close", "volume"]]


def _check() -> None:
    """`--check` CLI: print session status without leaking sensitive profile fields."""
    status = get_session_status()
    if status == "active":
        try:
            profile = _call_mcp_tool("mcp__kite__get_profile")
        except Exception as exc:
            print(f"[error] {exc}")
            return
        client_id = None
        if isinstance(profile, dict):
            client_id = profile.get("client_id") or profile.get("user_id")
        print(f"[active] session verified, profile = {client_id}")
    elif status == "expired":
        print("[expired] session not active")
    else:
        print("[error] could not determine session status — see logs")


def main() -> None:
    """CLI entry point for `kite_session.py`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(prog="kite_session.py", description="Centralised Kite Connect session wrapper")
    parser.add_argument("--check", action="store_true", help="Print Kite session status ([active]/[expired]/[error])")
    args = parser.parse_args()

    if args.check:
        _check()
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
