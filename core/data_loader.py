"""
Levisol Planning System - Data Loader
Reads the case data workbook ("Supply Chain Supporting Data.xlsx" format) into
clean pandas DataFrames. Tolerant of banner/blank rows; validates keys.
"""
from dataclasses import dataclass, field
import re
import pandas as pd
import numpy as np

MONTHS = ["jul", "aug", "sep", "oct", "nov", "dec"]
HUBS = ["MHW", "MHE"]
PLANTS = ["BOM", "AHM", "KOL"]
PLANT_LOC = {"Mumbai": "BOM", "Ahmedabad": "AHM", "Kolkata": "KOL"}
LINE_GROUPS = ["cap_le1.5", "cap_3_5", "cap_7_20", "cap_50", "cap_180_210"]
LINE_LABELS = {"cap_le1.5": "<=1.5 LT", "cap_3_5": "3-5 LT", "cap_7_20": "7-20 LT",
               "cap_50": "50 LT", "cap_180_210": "180-210 LT"}
DAYS_PER_MONTH = 30  # per case: 30 working days


def pack_to_line(pack: str) -> str:
    """Map a pack size string (e.g. '20 X 900 ML', '1 X 210 LT') to a filling line group."""
    m = re.match(r"\s*(\d+)\s*X\s*([\d.]+)\s*(ML|LT|KG)", str(pack).upper())
    if not m:
        raise ValueError(f"Unparseable pack size: {pack}")
    size = float(m.group(2))
    if m.group(3) == "ML":
        size /= 1000.0
    if size <= 1.5:
        return "cap_le1.5"
    if size <= 5:
        return "cap_3_5"
    if size <= 20:
        return "cap_7_20"
    if size <= 50:
        return "cap_50"
    return "cap_180_210"


@dataclass
class CaseData:
    plants: pd.DataFrame
    plant_hub_cost: pd.DataFrame
    hub_cfa_cost: pd.DataFrame
    sku: pd.DataFrame
    leadtime: pd.DataFrame
    sales: pd.DataFrame
    forecast: pd.DataFrame
    opening_cfa: pd.DataFrame
    opening_hub: pd.DataFrame
    jan_forecast: pd.DataFrame
    service_levels: pd.DataFrame
    warnings: list = field(default_factory=list)


def _sheet(path, name, usecols, colnames):
    df = pd.read_excel(path, sheet_name=name, header=None, usecols=usecols)
    df.columns = colnames
    df = df[df[colnames[0]].astype(str).str.startswith("SKU")].reset_index(drop=True)
    for c in df.columns:
        if c not in ("sku", "pack", "region", "cfa", "loc", "source", "contractual"):
            df[c] = pd.to_numeric(df[c], errors="coerce").fillna(0.0)
    return df


def _find_sheet(xls, token):
    for s in xls.sheet_names:
        if token.lower() in s.lower():
            return s
    raise KeyError(f"No sheet containing '{token}' in workbook")


def load_case_data(path) -> CaseData:
    xls = pd.ExcelFile(path)
    warn = []

    sh = _find_sheet(xls, "Plants")
    raw = pd.read_excel(path, sheet_name=sh, header=None)
    plants = raw[raw[0].isin(PLANTS)].iloc[:, :9].copy()
    plants.columns = ["plant", "location", "region"] + LINE_GROUPS + ["prod_cost"]
    plants = plants.set_index("plant")
    plants[LINE_GROUPS + ["prod_cost"]] = plants[LINE_GROUPS + ["prod_cost"]].astype(float)

    sh = _find_sheet(xls, "Plant-Hub")
    raw = pd.read_excel(path, sheet_name=sh, header=None)
    phc = raw[raw[0].isin(PLANT_LOC)].iloc[:, :3].copy()
    phc.columns = ["location", "MHW", "MHE"]
    phc["plant"] = phc["location"].map(PLANT_LOC)
    phc = phc.set_index("plant")[["MHW", "MHE"]].astype(float)

    sh = _find_sheet(xls, "Hub-CFA")
    raw = pd.read_excel(path, sheet_name=sh, header=None)
    rows = raw[raw[2].apply(lambda v: isinstance(v, (int, float))) & raw[0].notna()]
    hcc = rows.iloc[:, :4].copy()
    hcc.columns = ["cfa", "region", "MHW", "MHE"]
    hcc["cfa"] = hcc["cfa"].astype(str).str.strip() + " CFA"
    hcc = hcc.set_index("cfa")
    hcc[["MHW", "MHE"]] = hcc[["MHW", "MHE"]].astype(float)

    sh = _find_sheet(xls, "Portfolio")
    sku = _sheet(path, sh, [0, 1, 2, 3], ["sku", "pack", "penalty", "contractual"])
    sku["contractual"] = sku["contractual"].astype(str).str.upper().str.startswith("YES")
    sku["line"] = sku["pack"].map(pack_to_line)
    sku = sku.set_index("sku")

    sh = _find_sheet(xls, "Source")
    lt = _sheet(path, sh, list(range(10)),
                ["sku", "pack", "region", "cfa", "source", "lt_plant_hub",
                 "lt_hub_cfa", "lt_prod", "var_prod", "var_transit"])

    sh = _find_sheet(xls, "Sales History")
    sales = _sheet(path, sh, list(range(10)), ["sku", "pack", "region", "cfa"] + MONTHS)
    sh = _find_sheet(xls, "Forecast History")
    fc = _sheet(path, sh, list(range(10)), ["sku", "pack", "region", "cfa"] + MONTHS)

    n_neg = int((sales[MONTHS] < 0).sum().sum())
    if n_neg:
        warn.append(f"{n_neg} negative sales values floored to 0 (returns/corrections).")
        sales[MONTHS] = sales[MONTHS].clip(lower=0)

    sh = _find_sheet(xls, "opening Inventory")
    oi = _sheet(path, sh, list(range(5)), ["sku", "pack", "region", "loc", "qty"])
    hub_mask = oi["loc"].astype(str).str.contains("Hub")
    opening_hub = oi[hub_mask].copy()
    opening_hub["hub"] = np.where(opening_hub["loc"].str.contains("West"), "MHW", "MHE")
    opening_hub = opening_hub[["sku", "hub", "qty"]].reset_index(drop=True)
    opening_cfa = oi[~hub_mask].rename(columns={"loc": "cfa"})[["sku", "cfa", "qty"]].reset_index(drop=True)

    sh = _find_sheet(xls, "Jan Forecast")
    jf = _sheet(path, sh, list(range(5)), ["sku", "pack", "region", "cfa", "qty"])

    sh = _find_sheet(xls, "Service Levels")
    raw = pd.read_excel(path, sheet_name=sh, header=None)
    sl = raw[raw[0].isin(list("ABCD"))].iloc[:, [0, 3]].copy()
    sl.columns = ["tier", "fill_rate"]
    sl["fill_rate"] = sl["fill_rate"].apply(
        lambda v: float(str(v).replace("%", "")) / 100 if isinstance(v, str) else float(v))
    sl.loc[sl["fill_rate"] > 1, "fill_rate"] /= 100
    sl = sl.set_index("tier")

    key = lambda df: set(zip(df["sku"], df["cfa"]))
    base = key(sales)
    for name, df in [("forecast", fc), ("jan_forecast", jf), ("leadtime", lt),
                     ("opening_cfa", opening_cfa)]:
        if key(df) != base:
            warn.append(f"Key mismatch sales vs {name}: {len(base ^ key(df))} rows differ.")

    return CaseData(plants, phc, hcc, sku, lt, sales, fc,
                    opening_cfa, opening_hub, jf, sl, warn)
