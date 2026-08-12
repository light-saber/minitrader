"""Phase 3 chart-gate renderers: static PNG digest + interactive HTML.

`render_png()` produces the 90-day candlestick + EMA + volume PNG that gets
attached to the Phase 4 Discord buy prompt (SPEC.md §6). `render_html()`
produces a single-file, browser-openable interactive chart (lightweight-
charts, TradingView's MIT library) for on-demand inspection via the
`file://` link also included in that prompt.

Both functions accept an indicator-enriched OHLCV DataFrame — the shape
returned by `technical_workup.compute_indicators(technical_workup.fetch_ohlc(...))`
— and compute indicators on the fly if the columns are missing.
"""

from __future__ import annotations

import argparse
import json
import logging
from datetime import date
from pathlib import Path
from typing import Union

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import mplfinance as mpf
import pandas as pd

import technical_workup

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
PNG_DIR = BASE_DIR / "charts" / "png"
HTML_DIR = BASE_DIR / "charts" / "html"

PNG_CHART_DAYS = 90
INDICATOR_COLUMNS = ("ema_50", "ema_200", "rsi_14", "vol_avg_20")
LIGHTWEIGHT_CHARTS_CDN = "https://unpkg.com/lightweight-charts@4.1.0/dist/lightweight-charts.standalone.production.js"


def _ensure_indicators(df: pd.DataFrame) -> pd.DataFrame:
    """Return `df` unchanged if indicator columns are present, else compute them.

    Args:
        df: OHLCV (optionally indicator-enriched) DataFrame.

    Returns:
        A DataFrame guaranteed to have `INDICATOR_COLUMNS`.
    """
    if set(INDICATOR_COLUMNS).issubset(df.columns):
        return df
    return technical_workup.compute_indicators(df)


def render_png(symbol: str, df: pd.DataFrame, out_path: Union[str, Path]) -> str:
    """Render the 90-day PNG chart used in the Phase 4 Discord buy prompt.

    Candlesticks + EMA50/EMA200 overlays, a volume pane with its 20-day
    average line, horizontal swing-high/swing-low bands, and an annotation
    panel (price, % distance from 50 DMA, RSI(14), volume vs 20-day average).

    Args:
        symbol: NSE trading symbol, used in the chart title.
        df: Indicator-enriched OHLCV DataFrame, oldest row first.
        out_path: Destination PNG path.

    Returns:
        The absolute path to the written PNG file.
    """
    df = _ensure_indicators(df)
    window = df.tail(PNG_CHART_DAYS).copy()
    latest = window.iloc[-1]

    lookback = df.tail(technical_workup.SWING_LOOKBACK_DAYS + 1).iloc[:-1]
    swing_high = float(lookback["high"].max()) if not lookback.empty else float(latest["high"])
    swing_low = float(lookback["low"].min()) if not lookback.empty else float(latest["low"])

    dist_from_50dma_pct = (latest["close"] - latest["ema_50"]) / latest["ema_50"] * 100
    vol_ratio = latest["volume"] / latest["vol_avg_20"] if latest["vol_avg_20"] else float("nan")

    addplots = [
        mpf.make_addplot(window["ema_50"], color="tab:blue", width=1.0),
        mpf.make_addplot(window["ema_200"], color="tab:orange", width=1.0),
        mpf.make_addplot(window["vol_avg_20"], panel=1, color="tab:red", width=1.0),
    ]

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    fig, axes = mpf.plot(
        window,
        type="candle",
        style="charles",
        addplot=addplots,
        volume=True,
        panel_ratios=(3, 1),
        hlines=dict(hlines=[swing_high, swing_low], colors=["g", "r"], linestyle="--", linewidths=0.8),
        title=f"\n{symbol}",
        figsize=(12, 8),
        returnfig=True,
    )

    annotation = (
        f"Price: {latest['close']:.2f}   "
        f"Dist from 50DMA: {dist_from_50dma_pct:+.2f}%   "
        f"RSI(14): {latest['rsi_14']:.1f}   "
        f"Vol vs 20d avg: {vol_ratio:.2f}x"
    )
    axes[0].annotate(
        annotation,
        xy=(0.01, 0.98),
        xycoords="axes fraction",
        va="top",
        ha="left",
        fontsize=9,
        bbox=dict(boxstyle="round", facecolor="white", alpha=0.85),
    )

    fig.savefig(out_path, dpi=150, bbox_inches="tight")
    plt.close(fig)
    logger.info("wrote PNG chart for %s to %s", symbol, out_path)
    return str(out_path.resolve())


def _series_for_html(df: pd.DataFrame) -> dict[str, list]:
    """Serialize an indicator-enriched DataFrame into JSON-ready series for the HTML template.

    Args:
        df: Indicator-enriched OHLCV DataFrame, oldest row first.

    Returns:
        A dict of parallel-indexed lists keyed by series name.
    """
    candles = [
        {
            "time": idx.strftime("%Y-%m-%d"),
            "open": round(float(row["open"]), 2),
            "high": round(float(row["high"]), 2),
            "low": round(float(row["low"]), 2),
            "close": round(float(row["close"]), 2),
            "volume": int(row["volume"]),
        }
        for idx, row in df.iterrows()
    ]
    ema50 = [
        {"time": idx.strftime("%Y-%m-%d"), "value": round(float(v), 2)}
        for idx, v in df["ema_50"].items()
    ]
    ema200 = [
        {"time": idx.strftime("%Y-%m-%d"), "value": round(float(v), 2)}
        for idx, v in df["ema_200"].items()
    ]
    rsi14 = [
        {"time": idx.strftime("%Y-%m-%d"), "value": round(float(v), 2)}
        for idx, v in df["rsi_14"].items()
    ]
    return {"candles": candles, "ema50": ema50, "ema200": ema200, "rsi14": rsi14}


_HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>{symbol} — MiniTrader chart</title>
<script src="{cdn_url}"></script>
<style>
  body {{ font-family: -apple-system, Segoe UI, Roboto, sans-serif; margin: 0; background: #0d1117; color: #c9d1d9; }}
  #toolbar {{ display: flex; gap: 12px; align-items: center; padding: 10px 14px; background: #161b22; border-bottom: 1px solid #30363d; flex-wrap: wrap; }}
  #toolbar h1 {{ font-size: 15px; margin: 0 16px 0 0; color: #58a6ff; }}
  #toolbar label {{ font-size: 13px; cursor: pointer; user-select: none; }}
  #toolbar button {{ background: #21262d; color: #c9d1d9; border: 1px solid #30363d; border-radius: 6px; padding: 4px 10px; cursor: pointer; font-size: 13px; }}
  #toolbar button.active {{ background: #1f6feb; border-color: #1f6feb; color: #fff; }}
  #chart-wrap {{ position: relative; }}
  #main-chart {{ width: 100%; height: 480px; }}
  #rsi-chart {{ width: 100%; height: 140px; }}
  #draw-canvas {{ position: absolute; top: 0; left: 0; pointer-events: none; }}
  #draw-canvas.drawing {{ pointer-events: auto; cursor: crosshair; }}
</style>
</head>
<body>
<div id="toolbar">
  <h1>{symbol}</h1>
  <label><input type="checkbox" id="toggle-ema50" checked> EMA 50</label>
  <label><input type="checkbox" id="toggle-ema200" checked> EMA 200</label>
  <label><input type="checkbox" id="toggle-rsi" checked> RSI(14) pane</label>
  <span style="width:1px;height:18px;background:#30363d;display:inline-block;"></span>
  <button data-range="1D" class="range-btn active">1D</button>
  <button data-range="1W" class="range-btn">1W</button>
  <button data-range="1M" class="range-btn">1M</button>
  <span style="width:1px;height:18px;background:#30363d;display:inline-block;"></span>
  <button id="draw-toggle">Draw trend line</button>
  <button id="draw-clear">Clear drawings</button>
</div>
<div id="chart-wrap">
  <div id="main-chart"></div>
  <canvas id="draw-canvas"></canvas>
</div>
<div id="rsi-chart"></div>

<script>
const DATA = {data_json};

function aggregate(candles, mode) {{
  if (mode === '1D') return candles;
  const buckets = [];
  const keyFor = (t) => {{
    const d = new Date(t + 'T00:00:00Z');
    if (mode === '1W') {{
      const onejan = new Date(Date.UTC(d.getUTCFullYear(), 0, 1));
      const week = Math.ceil((((d - onejan) / 86400000) + onejan.getUTCDay() + 1) / 7);
      return d.getUTCFullYear() + '-W' + week;
    }}
    return d.getUTCFullYear() + '-' + d.getUTCMonth();
  }};
  let currentKey = null, bucket = null;
  for (const c of candles) {{
    const k = keyFor(c.time);
    if (k !== currentKey) {{
      if (bucket) buckets.push(bucket);
      bucket = {{ time: c.time, open: c.open, high: c.high, low: c.low, close: c.close, volume: c.volume || 0 }};
      currentKey = k;
    }} else {{
      bucket.high = Math.max(bucket.high, c.high);
      bucket.low = Math.min(bucket.low, c.low);
      bucket.close = c.close;
      bucket.volume += (c.volume || 0);
    }}
  }}
  if (bucket) buckets.push(bucket);
  return buckets;
}}

function aggregateLine(points, mode) {{
  if (mode === '1D') return points;
  const step = mode === '1W' ? 5 : 21;
  const out = [];
  for (let i = 0; i < points.length; i += step) {{
    out.push(points[Math.min(i + step - 1, points.length - 1)]);
  }}
  return out;
}}

const mainChart = LightweightCharts.createChart(document.getElementById('main-chart'), {{
  layout: {{ background: {{ color: '#0d1117' }}, textColor: '#c9d1d9' }},
  grid: {{ vertLines: {{ color: '#21262d' }}, horzLines: {{ color: '#21262d' }} }},
  timeScale: {{ borderColor: '#30363d' }},
  rightPriceScale: {{ borderColor: '#30363d' }},
}});
const candleSeries = mainChart.addCandlestickSeries({{
  upColor: '#3fb950', downColor: '#f85149', borderVisible: false,
  wickUpColor: '#3fb950', wickDownColor: '#f85149',
}});
const volumeSeries = mainChart.addHistogramSeries({{
  color: '#484f58', priceFormat: {{ type: 'volume' }}, priceScaleId: 'vol',
}});
mainChart.priceScale('vol').applyOptions({{ scaleMargins: {{ top: 0.85, bottom: 0 }} }});
const ema50Series = mainChart.addLineSeries({{ color: '#58a6ff', lineWidth: 1 }});
const ema200Series = mainChart.addLineSeries({{ color: '#d29922', lineWidth: 1 }});

const rsiChart = LightweightCharts.createChart(document.getElementById('rsi-chart'), {{
  layout: {{ background: {{ color: '#0d1117' }}, textColor: '#c9d1d9' }},
  grid: {{ vertLines: {{ color: '#21262d' }}, horzLines: {{ color: '#21262d' }} }},
  timeScale: {{ borderColor: '#30363d' }},
  rightPriceScale: {{ borderColor: '#30363d' }},
}});
const rsiSeries = rsiChart.addLineSeries({{ color: '#bc8cff', lineWidth: 1 }});
rsiSeries.createPriceLine({{ price: 70, color: '#f85149', lineStyle: 2, lineWidth: 1 }});
rsiSeries.createPriceLine({{ price: 30, color: '#3fb950', lineStyle: 2, lineWidth: 1 }});

function render(mode) {{
  candleSeries.setData(aggregate(DATA.candles, mode));
  volumeSeries.setData(aggregate(DATA.candles, mode).map(c => ({{
    time: c.time, value: c.volume, color: c.close >= c.open ? '#3fb95055' : '#f8514955',
  }})));
  ema50Series.setData(aggregateLine(DATA.ema50, mode));
  ema200Series.setData(aggregateLine(DATA.ema200, mode));
  rsiSeries.setData(aggregateLine(DATA.rsi14, mode));
  mainChart.timeScale().fitContent();
  rsiChart.timeScale().fitContent();
}}
render('1D');

document.querySelectorAll('.range-btn').forEach(btn => {{
  btn.addEventListener('click', () => {{
    document.querySelectorAll('.range-btn').forEach(b => b.classList.remove('active'));
    btn.classList.add('active');
    render(btn.dataset.range);
  }});
}});
document.getElementById('toggle-ema50').addEventListener('change', (e) => {{
  ema50Series.applyOptions({{ visible: e.target.checked }});
}});
document.getElementById('toggle-ema200').addEventListener('change', (e) => {{
  ema200Series.applyOptions({{ visible: e.target.checked }});
}});
document.getElementById('toggle-rsi').addEventListener('change', (e) => {{
  document.getElementById('rsi-chart').style.display = e.target.checked ? 'block' : 'none';
}});

// Minimal trend-line drawing tool: click two points on the canvas overlay to draw a line.
const canvas = document.getElementById('draw-canvas');
const ctx = canvas.getContext('2d');
function resizeCanvas() {{
  const rect = document.getElementById('main-chart').getBoundingClientRect();
  canvas.width = rect.width;
  canvas.height = rect.height;
}}
resizeCanvas();
window.addEventListener('resize', resizeCanvas);

let drawing = false, points = [];
document.getElementById('draw-toggle').addEventListener('click', () => {{
  drawing = !drawing;
  canvas.classList.toggle('drawing', drawing);
  document.getElementById('draw-toggle').classList.toggle('active', drawing);
  points = [];
}});
document.getElementById('draw-clear').addEventListener('click', () => {{
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  points = [];
}});
canvas.addEventListener('click', (e) => {{
  if (!drawing) return;
  const rect = canvas.getBoundingClientRect();
  points.push({{ x: e.clientX - rect.left, y: e.clientY - rect.top }});
  if (points.length === 2) {{
    ctx.strokeStyle = '#e3b341';
    ctx.lineWidth = 2;
    ctx.beginPath();
    ctx.moveTo(points[0].x, points[0].y);
    ctx.lineTo(points[1].x, points[1].y);
    ctx.stroke();
    points = [];
  }}
}});
</script>
</body>
</html>
"""


def render_html(symbol: str, df: pd.DataFrame, out_path: Union[str, Path]) -> str:
    """Render the single-file interactive HTML chart (lightweight-charts).

    Includes an EMA50/EMA200/RSI-pane indicator picker, a 1D/1W/1M range
    toggle (client-side aggregation of the embedded daily series), and a
    minimal click-to-draw trend-line tool. No external data calls — the CDN
    script tag is the only network dependency, and the file opens directly
    via `file://` in any browser.

    Args:
        symbol: NSE trading symbol, shown in the page header.
        df: Indicator-enriched OHLCV DataFrame, oldest row first.
        out_path: Destination HTML path.

    Returns:
        The absolute path to the written HTML file.
    """
    df = _ensure_indicators(df)
    series = _series_for_html(df)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    html = _HTML_TEMPLATE.format(
        symbol=symbol,
        cdn_url=LIGHTWEIGHT_CHARTS_CDN,
        data_json=json.dumps(series),
    )
    out_path.write_text(html, encoding="utf-8")
    logger.info("wrote HTML chart for %s to %s", symbol, out_path)
    return str(out_path.resolve())


def _demo(symbol: str) -> None:
    """Render both chart formats for `symbol` using the technical_workup OHLC stub."""
    df = technical_workup.compute_indicators(technical_workup.fetch_ohlc(symbol))
    today_str = date.today().strftime("%Y%m%d")
    png_path = render_png(symbol, df, PNG_DIR / f"{symbol}_{today_str}.png")
    html_path = render_html(symbol, df, HTML_DIR / f"{symbol}_{today_str}.html")
    print(f"PNG:  {png_path} ({Path(png_path).stat().st_size} bytes)")
    print(f"HTML: {html_path} ({Path(html_path).stat().st_size} bytes)")


def main() -> None:
    """CLI entry point for `chart_render.py`."""
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")

    parser = argparse.ArgumentParser(prog="chart_render.py", description="Render MiniTrader PNG/HTML charts")
    parser.add_argument("--demo", metavar="SYMBOL", help="Render a demo PNG+HTML pair for SYMBOL")
    args = parser.parse_args()

    if args.demo:
        _demo(args.demo)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
