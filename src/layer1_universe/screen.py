import json
import copy
import yaml
import datetime
import pandas as pd
from pathlib import Path
from finvizfinance.screener.overview import Overview
from finvizfinance.screener.valuation import Valuation
from finvizfinance.screener.financial import Financial
from finvizfinance.screener.performance import Performance
from finvizfinance.screener.technical import Technical
import sys

# Add src to path so we can import common
sys.path.append(str(Path(__file__).resolve().parents[2]))
from src.common.database import DatabaseManager

PREVIEW_COLUMNS = [
    "Ticker",
    "Company",
    "Sector",
    "Industry",
    "Market Cap",
    "P/E",
    "Forward P/E",
    "Oper M",
    "Profit M",
    "Perf Year",
    "52W High",
    "52W Low",
    "RSI",
    "Price",
]

def load_config(config_path: str = "config/filters.yaml") -> dict:
    with open(config_path, "r") as f:
        return yaml.safe_load(f)

def fetch_finviz_universe() -> pd.DataFrame:
    """
    Fetches the full >$10B optionable universe from Finviz.
    Combines Overview, Valuation, Financial, Performance, and Technical data.
    """
    filters = {'Market Cap.': '+Large (over $10bln)', 'Option/Short': 'Optionable'}
    print("Fetching Overview...")
    ov = Overview()
    ov.set_filter(filters_dict=filters)
    df_ov = ov.screener_view()
    
    print("Fetching Valuation...")
    val = Valuation()
    val.set_filter(filters_dict=filters)
    df_val = val.screener_view()
    
    print("Fetching Financial...")
    fin = Financial()
    fin.set_filter(filters_dict=filters)
    df_fin = fin.screener_view()
    
    print("Fetching Performance...")
    perf = Performance()
    perf.set_filter(filters_dict=filters)
    df_perf = perf.screener_view()
    
    print("Fetching Technical...")
    tech = Technical()
    tech.set_filter(filters_dict=filters)
    df_tech = tech.screener_view()
    
    # Merge all dataframes on Ticker (and any overlapping columns like Company, Sector, etc.)
    # We will use reduce to merge them cleanly
    from functools import reduce
    dfs = [df_ov, df_val, df_fin, df_perf, df_tech]
    
    # Drop overlapping columns before merge to avoid _x, _y suffixes
    def clean_merge(left, right):
        overlap = set(left.columns).intersection(set(right.columns)) - {'Ticker'}
        right_clean = right.drop(columns=list(overlap))
        return pd.merge(left, right_clean, on='Ticker', how='outer')
        
    df_merged = reduce(clean_merge, dfs)
    
    return df_merged

def convert_percentage(val):
    if pd.isna(val) or val == '-':
        return None
    if isinstance(val, str) and val.endswith('%'):
        return float(val.strip('%')) / 100.0
    try:
        return float(val)
    except:
        return None

def first_existing_column(df: pd.DataFrame, candidates: list[str]) -> str | None:
    for column in candidates:
        if column in df.columns:
            return column
    return None

def process_playbooks(df: pd.DataFrame, config: dict, return_diagnostics: bool = False) -> tuple:
    df_work = df.copy()

    # Finviz data comes in as strings (e.g. "25.40", "15.20%", "-"). We need to clean numeric columns.
    def to_float(val):
        if pd.isna(val) or val == '-':
            return None
        try:
            return float(str(val).replace(',', ''))
        except:
            return None

    pe_col = first_existing_column(df_work, ['P/E'])
    operating_margin_col = first_existing_column(df_work, ['Oper M', 'Oper. Margin', 'Operating Margin'])
    profit_margin_col = first_existing_column(df_work, ['Profit M', 'Profit Margin', 'Net Profit Margin'])
    perf_year_col = first_existing_column(df_work, ['Perf Year'])
    high_52w_col = first_existing_column(df_work, ['52W High'])
    low_52w_col = first_existing_column(df_work, ['52W Low'])

    df_work['P/E_num'] = df_work[pe_col].apply(to_float) if pe_col else None
    df_work['Oper. Margin_num'] = df_work[operating_margin_col].apply(convert_percentage) if operating_margin_col else None
    df_work['Profit Margin_num'] = df_work[profit_margin_col].apply(convert_percentage) if profit_margin_col else None
    df_work['Perf Year_num'] = df_work[perf_year_col].apply(convert_percentage) if perf_year_col else None
    df_work['52W High_num'] = df_work[high_52w_col].apply(convert_percentage) if high_52w_col else None
    df_work['52W Low_num'] = df_work[low_52w_col].apply(convert_percentage) if low_52w_col else None
        
    # Finviz 52W High/Low represents distance from the High/Low. 
    # e.g., "0-10% above low" -> 52W Low is between 0% and 10%
    # "30% or more below High" -> 52W High is <= -30% (finviz shows it as negative distance from high)

    # Finviz Long Playbook
    fl = config.get('finviz_long', {})
    pb_long_mask = pd.Series(True, index=df_work.index)
    long_steps = [{"playbook": "finviz_long", "step": "starting_universe", "rows": int(pb_long_mask.sum())}]
    if 'pe_max' in fl:
        pb_long_mask &= (df_work['P/E_num'] <= fl['pe_max'])
        long_steps.append({"playbook": "finviz_long", "step": f"P/E <= {fl['pe_max']}", "rows": int(pb_long_mask.sum())})
    if 'operating_margin_min' in fl:
        pb_long_mask &= (df_work['Oper. Margin_num'] >= fl['operating_margin_min'])
        long_steps.append({"playbook": "finviz_long", "step": f"Operating margin >= {fl['operating_margin_min']}", "rows": int(pb_long_mask.sum())})
    if 'net_profit_margin_min' in fl:
        pb_long_mask &= (df_work['Profit Margin_num'] >= fl['net_profit_margin_min'])
        long_steps.append({"playbook": "finviz_long", "step": f"Profit margin >= {fl['net_profit_margin_min']}", "rows": int(pb_long_mask.sum())})
    if 'high_52w_distance_low_max' in fl:
        # Distance from 52W low is positive in finviz.
        pb_long_mask &= (df_work['52W Low_num'] <= fl['high_52w_distance_low_max'])
        long_steps.append({"playbook": "finviz_long", "step": f"52W low distance <= {fl['high_52w_distance_low_max']}", "rows": int(pb_long_mask.sum())})
    if 'ath_distance_high_min' in fl:
        # We use 52W high distance as proxy if ATH isn't directly available in standard columns,
        # Finviz shows distance from 52W high as a negative percentage (e.g. -35%).
        # So "30% or more below high" means <= -0.30
        pb_long_mask &= (df_work['52W High_num'] <= -fl['ath_distance_high_min'])
        long_steps.append({"playbook": "finviz_long", "step": f"52W high distance <= -{fl['ath_distance_high_min']}", "rows": int(pb_long_mask.sum())})
        
    df_finviz_long = df_work[pb_long_mask].copy()

    # Finviz Short Playbook
    fs = config.get('finviz_short', {})
    pb_short_mask = pd.Series(True, index=df_work.index)
    short_steps = [{"playbook": "finviz_short", "step": "starting_universe", "rows": int(pb_short_mask.sum())}]
    if 'pe_min' in fs:
        pb_short_mask &= (df_work['P/E_num'] >= fs['pe_min'])
        short_steps.append({"playbook": "finviz_short", "step": f"P/E >= {fs['pe_min']}", "rows": int(pb_short_mask.sum())})
    if 'performance_year_min' in fs:
        pb_short_mask &= (df_work['Perf Year_num'] >= fs['performance_year_min'])
        short_steps.append({"playbook": "finviz_short", "step": f"Perf Year >= {fs['performance_year_min']}", "rows": int(pb_short_mask.sum())})
    if 'high_52w_distance_high_max' in fs:
        # 0-10% below high -> 52W High is between -0.10 and 0.0
        pb_short_mask &= (df_work['52W High_num'] >= -fs['high_52w_distance_high_max'])
        short_steps.append({"playbook": "finviz_short", "step": f"52W high distance >= -{fs['high_52w_distance_high_max']}", "rows": int(pb_short_mask.sum())})

    df_finviz_short = df_work[pb_short_mask].copy()

    if return_diagnostics:
        diagnostics = {
            "filter_waterfall": long_steps + short_steps,
        }
        return df_finviz_long, df_finviz_short, diagnostics

    return df_finviz_long, df_finviz_short

def build_manifest(
    df_universe: pd.DataFrame,
    df_long: pd.DataFrame,
    df_short: pd.DataFrame,
    source: str,
    diagnostics: dict | None = None,
) -> dict:
    critical_fields = ['Ticker', 'Market Cap', 'P/E', 'Oper M', 'Profit M', 'Perf Year', '52W High', '52W Low', 'Avg Volume']
    field_coverage = {
        field: float(df_universe[field].notna().mean())
        for field in critical_fields
        if field in df_universe.columns
    }

    return {
        "run_timestamp": datetime.datetime.now().isoformat(),
        "source": source,
        "universe_rows": int(len(df_universe)),
        "unique_tickers": int(df_universe['Ticker'].nunique()) if 'Ticker' in df_universe.columns else None,
        "duplicate_tickers": int(df_universe.duplicated('Ticker').sum()) if 'Ticker' in df_universe.columns else None,
        "finviz_long_candidates": int(len(df_long)),
        "finviz_short_candidates": int(len(df_short)),
        "columns": list(df_universe.columns),
        "critical_field_coverage": field_coverage,
        "filter_waterfall": diagnostics.get("filter_waterfall", []) if diagnostics else [],
    }

def apply_overrides(config: dict, overrides: dict) -> dict:
    config = copy.deepcopy(config)
    for dotted_key, value in overrides.items():
        if value is None:
            continue
        section, key = dotted_key.split(".", 1)
        config.setdefault(section, {})[key] = value
    return config

def load_universe(
    source: str,
    db_path: str,
    parquet_path: str | None,
    save_live_to_db: bool,
) -> tuple[pd.DataFrame, str]:
    if source == "latest-db":
        return DatabaseManager(db_path).load_latest_universe(), "finviz-sqlite-latest"

    if source == "live":
        df_universe = fetch_finviz_universe()
        if save_live_to_db:
            DatabaseManager(db_path).save_universe_data(df_universe)
        return df_universe, "finviz-live"

    if source == "parquet":
        if not parquet_path:
            raise ValueError("parquet_path is required when source='parquet'")
        return pd.read_parquet(parquet_path), f"parquet:{parquet_path}"

    raise ValueError(f"Unknown source: {source}")

def output_folder(output_date: str | None, output_root: str) -> Path:
    date_part = output_date or datetime.datetime.now().strftime("%Y-%m-%d")
    return Path(output_root) / date_part

def write_outputs(
    df_universe: pd.DataFrame,
    df_long: pd.DataFrame,
    df_short: pd.DataFrame,
    diagnostics: dict,
    out_dir: Path,
    source_label: str,
) -> dict:
    out_dir.mkdir(parents=True, exist_ok=True)

    df_universe.to_parquet(out_dir / "universe.parquet", index=False)
    df_long.to_csv(out_dir / "finviz_long_candidates.csv", index=False)
    df_short.to_csv(out_dir / "finviz_short_candidates.csv", index=False)
    df_long.to_csv(out_dir / "playbook_a_candidates.csv", index=False)
    df_short.to_csv(out_dir / "playbook_b_candidates.csv", index=False)
    pd.DataFrame(diagnostics["filter_waterfall"]).to_csv(out_dir / "filter_waterfall.csv", index=False)

    manifest = build_manifest(df_universe, df_long, df_short, source=source_label, diagnostics=diagnostics)
    with open(out_dir / "manifest.json", "w", encoding="utf-8") as f:
        json.dump(manifest, f, indent=2)
    return manifest

def preview_columns(df: pd.DataFrame) -> list[str]:
    return [column for column in PREVIEW_COLUMNS if column in df.columns]

def print_report(result: dict, show_rows: int) -> None:
    print("\nLayer 1 run summary")
    print("=" * 72)
    print(f"Source: {result['source_label']}")
    print(f"Universe rows: {len(result['universe'])}")
    print(f"Long candidates: {len(result['long_candidates'])}")
    print(f"Short candidates: {len(result['short_candidates'])}")
    if result.get("out_dir"):
        print(f"Output folder: {result['out_dir']}")

    print("\nFilter waterfall")
    print(pd.DataFrame(result["diagnostics"]["filter_waterfall"]).to_string(index=False))

    print(f"\nLong preview, first {show_rows}")
    if len(result["long_candidates"]) == 0:
        print("(no long candidates)")
    else:
        cols = preview_columns(result["long_candidates"])
        print(result["long_candidates"][cols].head(show_rows).to_string(index=False))

    print(f"\nShort preview, first {show_rows}")
    if len(result["short_candidates"]) == 0:
        print("(no short candidates)")
    else:
        cols = preview_columns(result["short_candidates"])
        print(result["short_candidates"][cols].head(show_rows).to_string(index=False))

def run_layer1(
    source: str = "latest-db",
    config_path: str = "config/filters.yaml",
    db_path: str = "data/fundamentals.db",
    parquet_path: str | None = None,
    output_root: str = "data/universe",
    output_date: str | None = None,
    save_outputs: bool = True,
    save_live_to_db: bool = True,
    overrides: dict | None = None,
    show_rows: int = 10,
    print_summary: bool = True,
) -> dict:
    config = load_config(config_path)
    if overrides:
        config = apply_overrides(config, overrides)

    df_universe, source_label = load_universe(source, db_path, parquet_path, save_live_to_db)
    df_long, df_short, diagnostics = process_playbooks(df_universe, config, return_diagnostics=True)

    out_dir = None
    manifest = build_manifest(df_universe, df_long, df_short, source=source_label, diagnostics=diagnostics)
    if save_outputs:
        out_dir = output_folder(output_date, output_root)
        manifest = write_outputs(df_universe, df_long, df_short, diagnostics, out_dir, source_label)

    result = {
        "config": config,
        "source_label": source_label,
        "universe": df_universe,
        "long_candidates": df_long,
        "short_candidates": df_short,
        "diagnostics": diagnostics,
        "manifest": manifest,
        "out_dir": out_dir,
    }

    if print_summary:
        print_report(result, show_rows)

    return result

def main():
    print("Starting Layer 1: Universe & Fundamental Screen (Finviz Integration)")
    try:
        run_layer1(source="live", save_outputs=True, save_live_to_db=True, print_summary=True)
    except Exception as e:
        print(f"Error running Layer 1: {e}")

if __name__ == "__main__":
    main()
