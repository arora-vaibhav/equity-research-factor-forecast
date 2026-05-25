"""Stock-report HTML renderer (Phase F.2).

Renders a single self-contained HTML file per ticker with:
  * Forecast summary card (point + 80/95% CIs)
  * matplotlib forecast plot embedded as base64 PNG
  * Factor contributions table
  * Catalysts in the forecast window
  * Reasoning paragraph (hybrid template + optional LLM enrichment
    behind ANTHROPIC_API_KEY env-var guard).

Output: `data/reports/<as_of_date>/<ticker>.html`.
"""
from __future__ import annotations

import base64
import datetime as _dt
import io
import os
from pathlib import Path
from typing import Optional

import pandas as pd

from src.layer3_forecast.factor_forecast import (
    FORECAST_VERSION,
    ForecastResult,
)


REPORT_VERSION = "1.0"


def _matplotlib_forecast_plot_b64(
    historical_close: pd.Series,
    forecast: ForecastResult,
) -> str:
    """Render a forecast plot to base64 PNG (utf-8 string, no 'data:' prefix)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    fig, ax = plt.subplots(figsize=(6.0, 3.0), dpi=110)

    if len(historical_close) > 0:
        ax.plot(historical_close.index, historical_close.values,
                color="#1d1d1f", linewidth=1.0)
        last_date = historical_close.index[-1]
        last_price = float(historical_close.iloc[-1])
    else:
        last_date = pd.Timestamp(forecast.as_of_date or _dt.date.today())
        last_price = 1.0

    horizon = forecast.horizon_days or 30
    forecast_date = last_date + pd.Timedelta(days=horizon)

    point_price = last_price * (1.0 + forecast.point_return)
    low80_price = last_price * (1.0 + forecast.lower_80)
    high80_price = last_price * (1.0 + forecast.upper_80)
    low95_price = last_price * (1.0 + forecast.lower_95)
    high95_price = last_price * (1.0 + forecast.upper_95)

    ax.plot([last_date, forecast_date], [last_price, point_price],
            color="#0a7d2f", linewidth=2.0, label="Point forecast")
    ax.fill_between(
        [last_date, forecast_date], [last_price, low95_price],
        [last_price, high95_price],
        color="#0a7d2f", alpha=0.10, label="95% CI",
    )
    ax.fill_between(
        [last_date, forecast_date], [last_price, low80_price],
        [last_price, high80_price],
        color="#0a7d2f", alpha=0.20, label="80% CI",
    )
    ax.axvline(last_date, color="#888", linestyle=":", linewidth=0.8)

    ax.set_title(f"{forecast.ticker} -- {horizon}d forecast", fontsize=11)
    ax.set_ylabel("Price")
    ax.legend(loc="best", fontsize=8, frameon=False)
    ax.grid(True, alpha=0.25)
    fig.tight_layout()

    buf = io.BytesIO()
    fig.savefig(buf, format="png", bbox_inches="tight")
    plt.close(fig)
    return base64.b64encode(buf.getvalue()).decode("utf-8")


def _build_factor_table(
    forecast: ForecastResult,
    loadings: pd.Series,
    ticker_factors: dict[str, float],
) -> list[dict]:
    out = []
    for factor in loadings.index:
        out.append({
            "factor": factor,
            "loading": float(loadings[factor]),
            "value": float(ticker_factors.get(factor, 0.0)),
            "contribution": forecast.factor_contributions.get(factor, 0.0),
        })
    intercept = forecast.factor_contributions.get("__intercept__", 0.0)
    out.append({
        "factor": "(intercept)",
        "loading": None,
        "value": None,
        "contribution": float(intercept),
    })
    return out


def _build_reasoning_template(
    ticker: str,
    forecast: ForecastResult,
    catalysts: list[dict],
    factor_table: list[dict],
) -> str:
    direction = "rising" if forecast.point_return >= 0 else "falling"
    band80 = f"[{forecast.lower_80:+.2%}, {forecast.upper_80:+.2%}]"
    n_cat = len(catalysts)
    catalyst_summary = (
        f"{n_cat} catalyst(s) in window"
        if n_cat else "no catalysts in window"
    )

    rows = [r for r in factor_table if r["factor"] != "(intercept)"]
    sorted_rows = sorted(rows, key=lambda r: r["contribution"], reverse=True)
    pos = [r for r in sorted_rows if r["contribution"] > 0][:2]
    neg = [r for r in sorted_rows if r["contribution"] < 0][:1]
    drivers_parts = []
    for r in pos:
        drivers_parts.append(f"+{r['factor']} ({r['contribution']:+.4f})")
    for r in neg:
        drivers_parts.append(f"-{r['factor']} ({r['contribution']:+.4f})")
    drivers = ", ".join(drivers_parts) if drivers_parts else "no dominant drivers"

    return (
        f"{ticker} forecast is {direction} ({forecast.point_return:+.2%}) "
        f"over the {forecast.horizon_days}-day horizon with 80% confidence "
        f"band {band80}. Key drivers: {drivers}. Calendar: {catalyst_summary}. "
        f"Trained on {forecast.n_train} cross-sectional observations."
    )


def _maybe_llm_enrich(
    template_text: str,
    ticker: str,
    forecast: ForecastResult,
    *,
    allow_llm: Optional[bool] = None,
) -> str:
    """Optional LLM enrichment via Anthropic SDK; falls back to template
    on any error."""
    if allow_llm is None:
        allow_llm = bool(os.environ.get("ANTHROPIC_API_KEY"))
    if not allow_llm:
        return template_text
    try:
        import anthropic  # type: ignore[import-not-found]
    except Exception:
        return template_text
    try:
        client = anthropic.Anthropic()
        prompt = (
            "You are summarising a quantitative stock forecast in 2-3 sentences. "
            "DO NOT add new facts. DO NOT predict prices outside the given band. "
            "Reuse the numbers verbatim. Tone: neutral, analytical.\n\n"
            f"Template summary:\n{template_text}\n"
        )
        msg = client.messages.create(
            model="claude-3-5-sonnet-latest",
            max_tokens=200,
            messages=[{"role": "user", "content": prompt}],
        )
        parts = []
        for block in msg.content:
            if getattr(block, "type", None) == "text":
                parts.append(block.text)
        enriched = "".join(parts).strip()
        if enriched:
            return enriched
    except Exception:
        pass
    return template_text


def render_stock_report(
    ticker: str,
    forecast: ForecastResult,
    loadings: pd.Series,
    ticker_factors: dict[str, float],
    *,
    historical_close: Optional[pd.Series] = None,
    catalysts: Optional[list[dict]] = None,
    company_name: Optional[str] = None,
    sector: Optional[str] = None,
    output_dir: Optional[Path] = None,
    template_dir: Optional[Path] = None,
    allow_llm: Optional[bool] = None,
) -> Path:
    """Render the per-ticker HTML report and return the output path."""
    from jinja2 import Environment, FileSystemLoader

    if template_dir is None:
        template_dir = Path(__file__).parent / "templates"

    env = Environment(
        loader=FileSystemLoader(str(template_dir)),
        autoescape=True,
    )
    template = env.get_template("stock_report.html.j2")

    factor_table = _build_factor_table(forecast, loadings, ticker_factors)

    plot_b64 = ""
    if historical_close is not None and len(historical_close) > 0:
        try:
            plot_b64 = _matplotlib_forecast_plot_b64(historical_close, forecast)
        except Exception:
            plot_b64 = ""

    reasoning_text = _build_reasoning_template(
        ticker, forecast, catalysts or [], factor_table,
    )
    reasoning_text = _maybe_llm_enrich(
        reasoning_text, ticker, forecast, allow_llm=allow_llm,
    )

    html = template.render(
        ticker=ticker,
        company_name=company_name,
        sector=sector,
        as_of_date=forecast.as_of_date or _dt.date.today().isoformat(),
        horizon_days=forecast.horizon_days,
        generated_at=_dt.datetime.utcnow().isoformat() + "Z",
        point_return=forecast.point_return,
        lower_80=forecast.lower_80,
        upper_80=forecast.upper_80,
        lower_95=forecast.lower_95,
        upper_95=forecast.upper_95,
        n_train=forecast.n_train,
        n_bootstrap=forecast.n_bootstrap,
        factor_table=factor_table,
        catalysts=catalysts or [],
        plot_b64=plot_b64,
        reasoning_text=reasoning_text,
        forecast_version=forecast.forecast_version or FORECAST_VERSION,
    )

    if output_dir is None:
        output_dir = (
            Path("data") / "reports"
            / (forecast.as_of_date or _dt.date.today().isoformat())
        )
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{ticker}.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


def render_index_page(
    tickers: list[str],
    *,
    output_dir: Path,
    as_of_date: str,
) -> Path:
    """Write a minimal index.html linking to the per-ticker reports."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = "\n".join(
        f'<li><a href="{t}.html">{t}</a></li>'
        for t in tickers
    )
    html = (
        "<!DOCTYPE html><html><head><meta charset='UTF-8'>"
        f"<title>Trade Identifier Reports - {as_of_date}</title></head>"
        "<body style='font-family:sans-serif;margin:24px;'>"
        f"<h1>Trade Identifier - Reports {as_of_date}</h1>"
        f"<ul>{rows}</ul></body></html>"
    )
    out_path = output_dir / "index.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path
