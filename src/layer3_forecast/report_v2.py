"""v2 Plotly dark-mode interactive HTML report.

Financial-engineering-analyst-grade dark-mode interactive report with
deeper technical content than the v1 static renderer.

Interactive (Plotly):
  * Forecast price-path fan + ensemble + per-method overlays
  * Factor contribution waterfall
  * Factor decay heatmap (factor x days-forward)
  * Factor IC time-series (rolling 21d)
  * MC sample path-spaghetti

Tables / cards:
  * Macro context panel (regime + yield curve + VIX z + sector relative)
  * Per-method comparison table
  * Catalysts table
  * Per-factor IC stats
  * LLM bull / bear / synthesis cards
  * Risk-flag bullet list

Single self-contained HTML file (Plotly via CDN).
Tokyo-Night-adapted dark CSS.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Optional

import numpy as np
import pandas as pd

from src.layer3_forecast.ensemble import EnsembleResult
from src.layer3_forecast.factor_decay import decay_grid
from src.layer3_forecast.llm_narrative import LLMNarrative
from src.layer3_forecast.macro_factors import MacroContext
from src.layer3_forecast.monte_carlo import MonteCarloResult


REPORT_V2_VERSION = "1.0"


def _snapshot_to_ta_features(snap: dict, last_close: float | None = None) -> dict:
    """Map multi_timeframe.compute_ta_snapshot output -> ta_signal_rules features.

    The snapshot uses ``willr_14``/``donch_pos_20``/``donch_break_up/dn``/etc.
    The interpreter expects ``williams_r``/``donchian_pos``/``donchian_break``/etc.
    This adapter handles the rename plus the few derived fields
    (macd_cross +/-1, sma_50_200_state, ema_9_21_state, donchian_break,
    price_vs_sma200_ratio).
    """
    if not snap or not snap.get("available"):
        return {}

    # MACD cross direction: +1 up, -1 dn, 0 none
    macd_cross = 0
    if snap.get("macd_cross_up"):
        macd_cross = 1
    elif snap.get("macd_cross_dn"):
        macd_cross = -1

    # Donchian breakout direction (single field for the interpreter)
    donchian_break = 0
    if snap.get("donch_break_up"):
        donchian_break = 1
    elif snap.get("donch_break_dn"):
        donchian_break = -1

    # SMA50/200 state: requires both SMAs present
    sma_50_200_state = None
    if snap.get("sma_50") is not None and snap.get("sma_200") is not None:
        try:
            sma_50_200_state = (
                "golden" if float(snap["sma_50"]) > float(snap["sma_200"])
                else "death"
            )
        except (TypeError, ValueError):
            sma_50_200_state = None

    # EMA 9/21 state from diff_pct sign
    ema_9_21_state = None
    diff = snap.get("ema_9_21_diff_pct")
    if diff is not None:
        try:
            ema_9_21_state = "bull" if float(diff) > 0 else "bear"
        except (TypeError, ValueError):
            ema_9_21_state = None

    # Price/SMA200 ratio
    price_vs_sma200_ratio = None
    if last_close is not None and snap.get("sma_200"):
        try:
            price_vs_sma200_ratio = float(last_close) / float(snap["sma_200"])
        except (TypeError, ValueError, ZeroDivisionError):
            price_vs_sma200_ratio = None

    return {
        "rsi_14":              snap.get("rsi_14"),
        "rsi_2":               snap.get("rsi_2"),
        "macd_hist":           snap.get("macd_hist"),
        "macd_hist_prev":      None,
        "macd_cross":          macd_cross,
        "bb_pctb":             snap.get("bb_pctb"),
        "bb_squeeze":          snap.get("bb_squeeze"),
        "adx_14":              snap.get("adx_14"),
        "adx_14_prev":         None,
        "plus_di":             None,
        "minus_di":            None,
        "stoch_k":             snap.get("stoch_k"),
        "williams_r":          snap.get("willr_14"),
        "obv_slope":           snap.get("obv_slope_10"),
        "obv_slope_z":         None,
        "mfi_14":              snap.get("mfi_14"),
        "cmf_20":              snap.get("cmf_20"),
        "atr_pct":             snap.get("atr_pct"),
        "atr_pct_percentile":  None,
        "sma_50_200_state":    sma_50_200_state,
        "sma_50_200_days":     None,
        "ema_9_21_state":      ema_9_21_state,
        "ema_9_21_days":       None,
        "donchian_pos":        snap.get("donch_pos_20"),
        "donchian_break":      donchian_break,
        "price_vs_sma200_ratio": price_vs_sma200_ratio,
    }


def _build_ta_panel(
    mtf_snapshots: Optional[dict],
    last_close: Optional[float] = None,
) -> dict:
    """Compute interpret_timeframe() per timeframe and return panel dict.

    Returns ``{tf: {label, net_strength, buy_count, sell_count, neutral_count,
    n_total, details, raw}}``. Empty dict if no snapshots available.
    """
    if not mtf_snapshots:
        return {}
    try:
        from src.methodology.ta_signal_rules import interpret_timeframe
    except Exception:
        return {}
    panel: dict = {}
    for tf, snap in mtf_snapshots.items():
        feats = _snapshot_to_ta_features(snap, last_close=last_close)
        if not feats:
            continue
        try:
            verdict = interpret_timeframe(feats, timeframe=tf)
        except Exception:
            continue
        verdict["raw"] = snap
        panel[tf] = verdict
    return panel


def _plotly_html(fig, *, div_id: str) -> str:
    import plotly.io as pio
    return pio.to_html(
        fig,
        include_plotlyjs="cdn",
        full_html=False,
        div_id=div_id,
        config={"displaylogo": False, "responsive": True},
    )


def _make_forecast_fan(
    ticker: str,
    historical_close: pd.Series,
    *,
    mc_paths: Optional[np.ndarray],
    last_price: float,
    horizon_days: int,
    ensemble_lower_80: float,
    ensemble_upper_80: float,
    ensemble_lower_95: float,
    ensemble_upper_95: float,
    ensemble_point: float,
    method_points: dict[str, float],
):
    import plotly.graph_objects as go

    fig = go.Figure()
    if len(historical_close) > 0:
        fig.add_trace(go.Scatter(
            x=historical_close.index, y=historical_close.values,
            mode="lines", name="Historical close",
            line=dict(color="#7aa2f7", width=1.5),
            hovertemplate="<b>%{x|%Y-%m-%d}</b><br>$%{y:.2f}<extra></extra>",
        ))
        last_date = historical_close.index[-1]
    else:
        last_date = pd.Timestamp.today()

    forecast_dates = pd.bdate_range(
        start=last_date + pd.Timedelta(days=1), periods=horizon_days,
    )

    if mc_paths is not None and mc_paths.size > 0 and last_price > 0:
        n_show = min(50, mc_paths.shape[0])
        idx = np.random.default_rng(42).choice(mc_paths.shape[0], size=n_show, replace=False)
        for i in idx:
            path_prices = mc_paths[i] * last_price
            fig.add_trace(go.Scatter(
                x=forecast_dates,
                y=path_prices[1:1 + len(forecast_dates)],
                mode="lines", showlegend=False,
                line=dict(color="rgba(122,162,247,0.10)", width=1),
                hoverinfo="skip",
            ))

    def price_from_return(r: float) -> float:
        return last_price * (1.0 + r)

    if len(forecast_dates) > 0:
        end_date = forecast_dates[-1]
        # 95% band
        fig.add_trace(go.Scatter(
            x=[last_date, end_date, end_date, last_date],
            y=[last_price, price_from_return(ensemble_lower_95),
               price_from_return(ensemble_upper_95), last_price],
            fill="toself", fillcolor="rgba(158, 206, 106, 0.10)",
            line=dict(color="rgba(0,0,0,0)"), name="95% CI",
            hoverinfo="skip",
        ))
        # 80% band
        fig.add_trace(go.Scatter(
            x=[last_date, end_date, end_date, last_date],
            y=[last_price, price_from_return(ensemble_lower_80),
               price_from_return(ensemble_upper_80), last_price],
            fill="toself", fillcolor="rgba(158, 206, 106, 0.22)",
            line=dict(color="rgba(0,0,0,0)"), name="80% CI",
            hoverinfo="skip",
        ))
        # Ensemble point
        fig.add_trace(go.Scatter(
            x=[last_date, end_date],
            y=[last_price, price_from_return(ensemble_point)],
            mode="lines+markers", name="Ensemble point",
            line=dict(color="#9ece6a", width=2.5),
            marker=dict(size=6, color="#9ece6a"),
            hovertemplate=(
                "Ensemble<br>%{x|%Y-%m-%d}<br>$%{y:.2f}<extra></extra>"
            ),
        ))
        # Per-method overlays
        method_colors = {
            "linear": "#bb9af7",
            "monte_carlo": "#7dcfff",
            "ar1": "#e0af68",
            "random_walk": "#f7768e",
        }
        for name, point in method_points.items():
            color = method_colors.get(name, "#a9b1d6")
            fig.add_trace(go.Scatter(
                x=[last_date, end_date],
                y=[last_price, price_from_return(point)],
                mode="lines", name=f"{name} ({point:+.2%})",
                line=dict(color=color, width=1.2, dash="dot"),
                hovertemplate=(
                    f"{name}<br>"
                    "%{x|%Y-%m-%d}<br>$%{y:.2f}<extra></extra>"
                ),
            ))

    fig.update_layout(
        template="plotly_dark",
        title=dict(
            text=f"{ticker} -- {horizon_days}d forecast (ensemble + methods)",
            font=dict(size=16),
        ),
        xaxis=dict(
            title="Date", rangeslider=dict(visible=True),
            showgrid=True, gridcolor="rgba(122,162,247,0.10)",
        ),
        yaxis=dict(
            title="Price ($)",
            showgrid=True, gridcolor="rgba(122,162,247,0.10)",
        ),
        hovermode="x unified",
        margin=dict(t=60, b=40, l=60, r=20),
        plot_bgcolor="#1a1b26",
        paper_bgcolor="#1a1b26",
        font=dict(color="#c0caf5"),
        legend=dict(
            bgcolor="rgba(36,40,59,0.85)",
            bordercolor="rgba(122,162,247,0.20)",
            borderwidth=1,
            font=dict(size=11),
        ),
        height=480,
    )
    return fig


def _make_waterfall(factor_table: list[dict]):
    import plotly.graph_objects as go

    rows = [r for r in factor_table if r["factor"] != "(intercept)"]
    rows = sorted(rows, key=lambda r: r["contribution"], reverse=True)
    labels = [r["factor"] for r in rows] + ["Ensemble point"]
    contributions = [r["contribution"] for r in rows]
    contributions.append(sum(contributions))
    measures = ["relative"] * len(rows) + ["total"]

    fig = go.Figure(go.Waterfall(
        x=labels, y=contributions, measure=measures,
        connector=dict(line=dict(color="#7aa2f7")),
        increasing=dict(marker=dict(color="#9ece6a")),
        decreasing=dict(marker=dict(color="#f7768e")),
        totals=dict(marker=dict(color="#7dcfff")),
        hovertemplate="<b>%{x}</b><br>Contribution: %{y:+.4f}<extra></extra>",
    ))
    fig.update_layout(
        template="plotly_dark",
        title=dict(text="Factor contribution waterfall (decimal return)"),
        plot_bgcolor="#1a1b26", paper_bgcolor="#1a1b26",
        font=dict(color="#c0caf5"),
        margin=dict(t=50, b=40, l=60, r=20),
        height=380,
        yaxis=dict(showgrid=True, gridcolor="rgba(122,162,247,0.10)"),
    )
    return fig


def _make_decay_heatmap(contributions: dict[str, float], *, horizon_days: int):
    import plotly.graph_objects as go

    grid = decay_grid(contributions, horizon_days=horizon_days, step_days=5)
    grid.pop("__intercept__", None)
    factors = list(grid.keys())
    days = list(range(0, horizon_days + 1, 5))
    z = [grid[f] for f in factors]

    fig = go.Figure(go.Heatmap(
        z=z, x=days, y=factors,
        colorscale=[
            [0.0, "#f7768e"], [0.5, "#1a1b26"], [1.0, "#9ece6a"],
        ],
        zmid=0.0,
        hovertemplate="<b>%{y}</b> @ d+%{x}<br>Contribution: %{z:+.4f}<extra></extra>",
        colorbar=dict(title=dict(text="Decimal", side="right"), tickfont=dict(color="#c0caf5")),
    ))
    fig.update_layout(
        template="plotly_dark",
        title=dict(text=f"Factor decay (exponential half-life per factor) -- {horizon_days}d"),
        xaxis=dict(title="Days forward", dtick=5),
        yaxis=dict(title="Factor"),
        plot_bgcolor="#1a1b26", paper_bgcolor="#1a1b26",
        font=dict(color="#c0caf5"),
        margin=dict(t=50, b=40, l=140, r=20),
        height=380,
    )
    return fig


def _make_tornado_chart(sensitivity_rows: list):
    """Horizontal bar chart sorted by |impact|; +1sd green, -1sd red."""
    import plotly.graph_objects as go
    if not sensitivity_rows:
        return None
    rows = list(sensitivity_rows)
    rows.reverse()
    factors = [r.factor for r in rows]
    plus_impacts = [r.impact_plus_1sd for r in rows]
    minus_impacts = [r.impact_minus_1sd for r in rows]
    fig = go.Figure()
    fig.add_trace(go.Bar(
        y=factors, x=plus_impacts, orientation="h", name="+1 sd",
        marker=dict(color="#9ece6a"),
        hovertemplate="<b>%{y}</b> +1sd<br>Impact: %{x:+.4f}<extra></extra>",
    ))
    fig.add_trace(go.Bar(
        y=factors, x=minus_impacts, orientation="h", name="-1 sd",
        marker=dict(color="#f7768e"),
        hovertemplate="<b>%{y}</b> -1sd<br>Impact: %{x:+.4f}<extra></extra>",
    ))
    fig.add_vline(x=0, line=dict(color="rgba(192,202,245,0.30)"))
    fig.update_layout(
        template="plotly_dark",
        title=dict(text="Factor sensitivity tornado (+/-1 sd impact)"),
        barmode="overlay",
        xaxis=dict(title="Return impact (decimal)", showgrid=True,
                   gridcolor="rgba(122,162,247,0.10)"),
        yaxis=dict(title="Factor"),
        plot_bgcolor="#1a1b26", paper_bgcolor="#1a1b26",
        font=dict(color="#c0caf5"),
        legend=dict(bgcolor="rgba(36,40,59,0.85)"),
        margin=dict(t=50, b=40, l=140, r=20),
        height=380,
    )
    return fig


def _make_ic_chart(ic_series: pd.DataFrame):
    import plotly.graph_objects as go

    if ic_series is None or ic_series.empty:
        return None
    fig = go.Figure()
    palette = ["#9ece6a", "#7aa2f7", "#bb9af7", "#7dcfff", "#e0af68", "#f7768e"]
    for i, col in enumerate(ic_series.columns):
        s = ic_series[col].dropna()
        if s.empty:
            continue
        smoothed = s.rolling(21, min_periods=5).mean()
        fig.add_trace(go.Scatter(
            x=smoothed.index, y=smoothed.values, mode="lines", name=col,
            line=dict(color=palette[i % len(palette)], width=1.5),
            hovertemplate=f"{col}<br>%{{x|%Y-%m-%d}}<br>IC: %{{y:+.3f}}<extra></extra>",
        ))
    fig.add_hline(y=0, line=dict(color="rgba(192,202,245,0.30)", dash="dash"))
    fig.update_layout(
        template="plotly_dark",
        title=dict(text="Factor Information Coefficient (rolling 21d, Spearman)"),
        xaxis=dict(title="Date", showgrid=True, gridcolor="rgba(122,162,247,0.10)"),
        yaxis=dict(title="IC", showgrid=True, gridcolor="rgba(122,162,247,0.10)"),
        plot_bgcolor="#1a1b26", paper_bgcolor="#1a1b26",
        font=dict(color="#c0caf5"),
        legend=dict(bgcolor="rgba(36,40,59,0.85)"),
        margin=dict(t=50, b=40, l=60, r=20),
        height=320,
    )
    return fig


def _build_factor_table_rows(
    factor_contributions: dict[str, float],
    loadings: pd.Series,
    ticker_factors: dict[str, float],
) -> list[dict]:
    rows = []
    for factor in loadings.index:
        rows.append({
            "factor": factor,
            "loading": float(loadings[factor]),
            "value": float(ticker_factors.get(factor, 0.0)),
            "contribution": float(factor_contributions.get(factor, 0.0)),
        })
    rows.append({
        "factor": "(intercept)",
        "loading": None,
        "value": None,
        "contribution": float(factor_contributions.get("__intercept__", 0.0)),
    })
    return rows


def render_stock_report_v2(
    ticker: str,
    *,
    company_name: Optional[str],
    sector: Optional[str],
    as_of_date: str,
    horizon_days: int,
    last_price: float,
    historical_close: pd.Series,
    ensemble: EnsembleResult,
    factor_contributions: dict[str, float],
    loadings: pd.Series,
    ticker_factors: dict[str, float],
    monte_carlo: Optional[MonteCarloResult],
    mc_paths: Optional[np.ndarray],
    macro_context: MacroContext,
    catalysts: list[dict],
    narrative: LLMNarrative,
    ic_series: Optional[pd.DataFrame] = None,
    factor_ic_stats: Optional[dict] = None,
    output_dir: Optional[Path] = None,
    template_dir: Optional[Path] = None,
    is_synthetic: bool = False,
    realized_vol_yang_zhang: Optional[float] = None,
    realized_vol_close_to_close: Optional[float] = None,
    # Sensitivity / bias / sentiment / LLM critique panels.
    sensitivity_result=None,
    bias_signal=None,
    sentiment_snapshot=None,
    llm_critique=None,
    ic_backtest_provenance: str = "synthetic_panel",
    # Walk-forward + volume + descriptive-stats panels.
    walk_forward_summary=None,
    volume_composite: Optional[dict] = None,
    descriptive_stats: Optional[dict] = None,
    # Sector / multi-timeframe / FMP panels.
    sector_features=None,
    mtf_snapshots: Optional[dict] = None,
    mtf_confluence: Optional[dict] = None,
    fmp_ratios: Optional[dict] = None,
    fmp_revisions_info: Optional[dict] = None,
    # Peer-comparison panel (sector-relative valuation + momentum).
    peer_comparison=None,
) -> Path:
    """Render the v2 dark-mode interactive HTML report."""
    from jinja2 import Environment, FileSystemLoader

    if template_dir is None:
        template_dir = Path(__file__).parent / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)), autoescape=True)
    template = env.get_template("stock_report_v2.html.j2")

    method_points = {m.method: m.point_return for m in ensemble.methods}
    factor_table = _build_factor_table_rows(
        factor_contributions, loadings, ticker_factors,
    )

    forecast_fan_html = _plotly_html(
        _make_forecast_fan(
            ticker, historical_close,
            mc_paths=mc_paths, last_price=last_price,
            horizon_days=horizon_days,
            ensemble_lower_80=ensemble.ensemble_lower_80,
            ensemble_upper_80=ensemble.ensemble_upper_80,
            ensemble_lower_95=ensemble.ensemble_lower_95,
            ensemble_upper_95=ensemble.ensemble_upper_95,
            ensemble_point=ensemble.ensemble_point,
            method_points=method_points,
        ),
        div_id="forecast_fan",
    )
    waterfall_html = _plotly_html(
        _make_waterfall(factor_table), div_id="waterfall",
    )
    decay_html = _plotly_html(
        _make_decay_heatmap(
            {k: v for k, v in factor_contributions.items()},
            horizon_days=horizon_days,
        ),
        div_id="decay",
    )
    ic_html = ""
    if ic_series is not None and not ic_series.empty:
        ic_fig = _make_ic_chart(ic_series)
        if ic_fig is not None:
            ic_html = _plotly_html(ic_fig, div_id="ic_chart")

    # Tornado sensitivity chart.
    tornado_html = ""
    if sensitivity_result is not None and sensitivity_result.factor_sensitivities:
        tornado_fig = _make_tornado_chart(sensitivity_result.factor_sensitivities)
        if tornado_fig is not None:
            tornado_html = _plotly_html(tornado_fig, div_id="tornado")

    method_rows = []
    for m in ensemble.methods:
        method_rows.append({
            "method": m.method,
            "point_return": m.point_return,
            "lower_80": m.lower_80,
            "upper_80": m.upper_80,
            "lower_95": m.lower_95,
            "upper_95": m.upper_95,
            "weight": m.weight,
        })

    ic_table_rows = []
    if factor_ic_stats:
        for factor, stats in factor_ic_stats.items():
            ic_table_rows.append({
                "factor": factor,
                "mean_ic": getattr(stats, "mean_ic", None) if hasattr(stats, "mean_ic")
                           else stats.get("mean_ic") if isinstance(stats, dict) else None,
                "icir": getattr(stats, "icir", None) if hasattr(stats, "icir")
                        else stats.get("icir") if isinstance(stats, dict) else None,
                "fraction_positive": getattr(stats, "fraction_positive", None) if hasattr(stats, "fraction_positive")
                                     else stats.get("fraction_positive") if isinstance(stats, dict) else None,
                "cumulative_attribution": getattr(stats, "cumulative_attribution", None) if hasattr(stats, "cumulative_attribution")
                                          else stats.get("cumulative_attribution") if isinstance(stats, dict) else None,
                "n_periods": getattr(stats, "n_periods", None) if hasattr(stats, "n_periods")
                             else stats.get("n_periods") if isinstance(stats, dict) else None,
            })

    html = template.render(
        ticker=ticker, company_name=company_name, sector=sector,
        as_of_date=as_of_date, horizon_days=horizon_days,
        last_price=last_price, is_synthetic=is_synthetic,
        generated_at=_dt.datetime.utcnow().isoformat() + "Z",
        ensemble_point=ensemble.ensemble_point,
        ensemble_lower_80=ensemble.ensemble_lower_80,
        ensemble_upper_80=ensemble.ensemble_upper_80,
        ensemble_lower_95=ensemble.ensemble_lower_95,
        ensemble_upper_95=ensemble.ensemble_upper_95,
        method_dispersion=ensemble.method_dispersion,
        beats_random_walk=ensemble.beats_random_walk,
        mc_drift=monte_carlo.drift_annual if monte_carlo else None,
        mc_sigma=monte_carlo.sigma_annual if monte_carlo else None,
        mc_n_paths=monte_carlo.n_paths if monte_carlo else 0,
        bull_scenario_return=monte_carlo.bull_scenario_return if monte_carlo else 0.0,
        base_scenario_return=monte_carlo.base_scenario_return if monte_carlo else 0.0,
        bear_scenario_return=monte_carlo.bear_scenario_return if monte_carlo else 0.0,
        realized_vol_yang_zhang=realized_vol_yang_zhang,
        realized_vol_close_to_close=realized_vol_close_to_close,
        macro=macro_context, catalysts=catalysts, narrative=narrative,
        factor_table=factor_table, method_rows=method_rows,
        ic_table_rows=ic_table_rows,
        forecast_fan_html=forecast_fan_html,
        waterfall_html=waterfall_html,
        decay_html=decay_html,
        ic_html=ic_html,
        tornado_html=tornado_html,
        sensitivity=sensitivity_result,
        bias=bias_signal,
        sentiment=sentiment_snapshot,
        critique=llm_critique,
        ic_backtest_provenance=ic_backtest_provenance,
        report_v2_version=REPORT_V2_VERSION,
        # Walk-forward + volume + descriptive panels.
        walk_forward=walk_forward_summary,
        volume_composite=volume_composite,
        descriptive_stats=descriptive_stats,
        # Sector / multi-timeframe / FMP context.
        sector_features=sector_features,
        mtf_snapshots=mtf_snapshots or {},
        mtf_confluence=mtf_confluence or {},
        fmp_ratios=fmp_ratios or {},
        fmp_revisions_info=fmp_revisions_info or {},
        # TradingView-style aggregated BUY/SELL/NEUTRAL verdict per timeframe.
        ta_panel=_build_ta_panel(mtf_snapshots, last_close=last_price),
        # Peer-comparison panel (cheap_leader / expensive_laggard / mixed).
        peer_comparison=peer_comparison,
    )

    if output_dir is None:
        output_dir = Path("data") / "reports" / as_of_date
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    out_path = output_dir / f"{ticker}.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path


def render_index_page_v2(
    ticker_meta: list[dict],
    *,
    output_dir: Path,
    as_of_date: str,
    is_synthetic: bool = False,
) -> Path:
    """Dark-mode index page linking the per-ticker reports."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    rows = []
    for m in ticker_meta:
        pt = m.get("ensemble_point", 0.0)
        sign = "+" if pt >= 0 else ""
        color = "#9ece6a" if pt >= 0 else "#f7768e"
        rows.append(
            f"<tr><td><a href='{m['ticker']}.html'>{m['ticker']}</a></td>"
            f"<td>{m.get('company','')}</td>"
            f"<td>{m.get('sector','')}</td>"
            f"<td>${m.get('last_price', 0.0):,.2f}</td>"
            f"<td style='color:{color}'>{sign}{pt:.2%}</td></tr>"
        )
    banner = ""
    if is_synthetic:
        banner = (
            "<div style='background:#7c1f1f;color:#fff;padding:12px;"
            "border-radius:6px;margin-bottom:16px;font-weight:600;'>"
            "WARNING: synthetic-mode reports. Numbers are illustrative only.</div>"
        )
    html = f"""<!DOCTYPE html>
<html><head><meta charset='UTF-8'>
<title>Trade Identifier -- Forecast Reports {as_of_date}</title>
<style>
  body {{ background:#1a1b26; color:#c0caf5; font-family:-apple-system,sans-serif; margin:24px; }}
  a {{ color:#7aa2f7; }}
  h1 {{ color:#bb9af7; }}
  table {{ width:100%; border-collapse:collapse; font-size:14px; margin-top:12px; }}
  th, td {{ padding:8px; text-align:left; border-bottom:1px solid #24283b; }}
  th {{ background:#24283b; color:#7aa2f7; }}
  .footer {{ color:#737aa2; font-size:11px; margin-top:24px; }}
</style></head>
<body>
{banner}
<h1>Trade Identifier -- Forecast Reports</h1>
<div>Report date: <b>{as_of_date}</b> | report v2 dark-mode interactive</div>
<table>
  <thead><tr><th>Ticker</th><th>Company</th><th>Sector</th><th>Last price</th><th>30d ensemble forecast</th></tr></thead>
  <tbody>
{chr(10).join(rows)}
  </tbody>
</table>
<div class='footer'>
Generated by Trade Identifier Forecast Layer v2.
Ensemble methodology: linear factor regression + GBM Monte Carlo + AR(1) + random-walk baseline.
Plotly interactive plots; not investment advice.
</div>
</body></html>"""
    out_path = output_dir / "index.html"
    out_path.write_text(html, encoding="utf-8")
    return out_path
