"""
Levisol Planning System - Analytics
SKU tiering (ABC by volume slab), XYZ volatility classes, forecast-error metrics,
and statistical inventory norms (safety stock, reorder point, days of cover)
for every SKU x CFA and SKU x Hub.

Methodology
-----------
Safety stock buffers the system against BOTH demand-side and supply-side risk
over the replenishment lead time:

    SS = z * sqrt( L * sigma_d^2  +  d^2 * sigma_L^2 )

    z        service-level factor from the tier fill-rate target
    L        mean replenishment lead time in days
             (production LT + plant->hub LT + hub->CFA LT)
    sigma_d  DAILY demand uncertainty. We use the std-dev of monthly FORECAST
             ERRORS (actual - forecast) scaled to daily via /sqrt(30), because
             replenishment is planned on the forecast: the risk the buffer must
             absorb is the error of that forecast, not raw demand variance.
    d        average daily demand (last 6 months actual sales / 180)
    sigma_L  lead-time std-dev = sqrt(var_prod^2 + var_transit^2)

    ROP = d * L + SS          (reorder point)
    DOC = ROP / d             (days of cover represented by the ROP)
    Cycle stock = 0.5 * monthly demand (monthly replenishment cadence)

Hub norms use the same logic at 98% service for all grades (per case), with
risk pooling across the CFAs each hub serves: sigma_hub = sqrt(sum sigma_cfa^2),
and hub lead time = production LT + plant->hub LT (demand-weighted).
"""
import numpy as np
import pandas as pd
from .data_loader import MONTHS, DAYS_PER_MONTH, CaseData

# Inverse-normal z for the tier fill-rate targets
Z = {0.98: 2.0537, 0.97: 1.8808, 0.92: 1.4051, 0.95: 1.6449, 0.99: 2.3263, 0.90: 1.2816}


def z_value(p: float) -> float:
    if p in Z:
        return Z[p]
    # Acklam rational approximation of the normal quantile (no scipy dependency)
    a = [-3.969683028665376e+01, 2.209460984245205e+02, -2.759285104469687e+02,
         1.383577518672690e+02, -3.066479806614716e+01, 2.506628277459239e+00]
    b = [-5.447609879822406e+01, 1.615858368580409e+02, -1.556989798598866e+02,
         6.680131188771972e+01, -1.328068155288572e+01]
    c = [-7.784894002430293e-03, -3.223964580411365e-01, -2.400758277161838e+00,
         -2.549732539343734e+00, 4.374664141464968e+00, 2.938163982698783e+00]
    d = [7.784695709041462e-03, 3.224671290700398e-01, 2.445134137142996e+00,
         3.754408661907416e+00]
    p = min(max(p, 1e-9), 1 - 1e-9)
    if p < 0.02425:
        q = np.sqrt(-2 * np.log(p))
        return (((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    if p > 1 - 0.02425:
        q = np.sqrt(-2 * np.log(1 - p))
        return -(((((c[0]*q+c[1])*q+c[2])*q+c[3])*q+c[4])*q+c[5]) / ((((d[0]*q+d[1])*q+d[2])*q+d[3])*q+1)
    q = p - 0.5
    r = q * q
    return (((((a[0]*r+a[1])*r+a[2])*r+a[3])*r+a[4])*r+a[5])*q / (((((b[0]*r+b[1])*r+b[2])*r+b[3])*r+b[4])*r+1)


def classify_tiers(data: CaseData, cuts=(0.50, 0.80, 0.95)) -> pd.DataFrame:
    """ABC-D tiers by cumulative share of 6-month sales volume (Exhibit F slabs)."""
    vol = data.sales.groupby("sku")[MONTHS].sum().sum(axis=1).sort_values(ascending=False)
    cum = vol.cumsum() / vol.sum()
    tier = pd.Series(np.where(cum <= cuts[0], "A",
                     np.where(cum <= cuts[1], "B",
                     np.where(cum <= cuts[2], "C", "D"))), index=vol.index, name="tier")
    out = pd.DataFrame({"vol_6m": vol, "cum_share": cum, "tier": tier})
    out["fill_rate"] = out["tier"].map(data.service_levels["fill_rate"])
    out["z"] = out["fill_rate"].map(z_value)
    return out


def xyz_classes(data: CaseData, cuts=(0.30, 0.70)) -> pd.Series:
    """XYZ volatility class on SKU-level monthly CV: X stable, Y variable, Z erratic."""
    m = data.sales.groupby("sku")[MONTHS].sum()
    cv = m.std(axis=1, ddof=1) / m.mean(axis=1).replace(0, np.nan)
    return pd.Series(np.where(cv <= cuts[0], "X", np.where(cv <= cuts[1], "Y", "Z")),
                     index=m.index, name="xyz")


def forecast_errors(data: CaseData) -> pd.DataFrame:
    """Per SKU x CFA forecast accuracy: bias, MAD, MAPE, RMSE, sigma of errors, CV."""
    s = data.sales.set_index(["sku", "cfa"])[MONTHS]
    f = data.forecast.set_index(["sku", "cfa"])[MONTHS]
    err = s - f                                  # actual - forecast
    out = pd.DataFrame(index=s.index)
    out["mean_sales_m"] = s.mean(axis=1)
    out["bias"] = err.mean(axis=1)
    out["mad"] = err.abs().mean(axis=1)
    out["rmse"] = np.sqrt((err ** 2).mean(axis=1))
    denom = s.where(s > 0.05)
    out["mape_pct"] = (err.abs() / denom).mean(axis=1) * 100
    out["sigma_fe_m"] = err.std(axis=1, ddof=1)            # monthly forecast-error sigma
    out["sigma_sales_m"] = s.std(axis=1, ddof=1)
    out["cv_sales"] = out["sigma_sales_m"] / out["mean_sales_m"].replace(0, np.nan)
    return out.reset_index()


def cfa_norms(data: CaseData, tiers: pd.DataFrame) -> pd.DataFrame:
    """Inventory norms per SKU x CFA."""
    fe = forecast_errors(data).set_index(["sku", "cfa"])
    lt = data.leadtime.set_index(["sku", "cfa"])
    idx = fe.index
    d_day = fe["mean_sales_m"] / DAYS_PER_MONTH
    sigma_d = fe["sigma_fe_m"].fillna(fe["sigma_sales_m"]).fillna(0) / np.sqrt(DAYS_PER_MONTH)
    L = (lt["lt_prod"] + lt["lt_plant_hub"] + lt["lt_hub_cfa"]).reindex(idx)
    sigma_L = np.sqrt(lt["var_prod"] ** 2 + lt["var_transit"] ** 2).reindex(idx)
    sku_tier = tiers["tier"].reindex(idx.get_level_values("sku")).values
    z = tiers["z"].reindex(idx.get_level_values("sku")).values

    ss = z * np.sqrt(L * sigma_d ** 2 + (d_day ** 2) * sigma_L ** 2)
    ss = ss.clip(lower=0).fillna(0)
    rop = (d_day * L + ss).fillna(0)
    doc = np.where(d_day > 0.005, rop / d_day, 0.0)

    out = pd.DataFrame({
        "tier": sku_tier, "z": z,
        "avg_daily_demand_kl": d_day.round(4),
        "sigma_d_daily": sigma_d.round(4),
        "lead_time_days": L, "sigma_LT_days": sigma_L.round(3),
        "safety_stock_kl": ss.round(2), "reorder_point_kl": rop.round(2),
        "days_of_cover": np.round(doc, 1),
        "cycle_stock_kl": (fe["mean_sales_m"] / 2).round(2),
        "avg_inventory_kl": (fe["mean_sales_m"] / 2 + ss).round(2),
    }, index=idx)
    out["source_hub"] = lt["source"].reindex(idx).map(
        {"East": "MHE", "Rest of India": "MHW"})
    return out.reset_index()


def hub_norms(data: CaseData, norms_cfa: pd.DataFrame, service=0.98) -> pd.DataFrame:
    """Risk-pooled norms per SKU x Hub at a flat 98% hub service level (per case)."""
    z = z_value(service)
    n = norms_cfa.copy()
    lt = data.leadtime.set_index(["sku", "cfa"])
    n = n.merge(lt[["lt_prod", "lt_plant_hub", "var_prod"]].reset_index(), on=["sku", "cfa"])
    rows = []
    for (sku, hub), g in n.groupby(["sku", "source_hub"]):
        d = g["avg_daily_demand_kl"].sum()
        sigma_pooled = np.sqrt((g["sigma_d_daily"] ** 2).sum())
        w = g["avg_daily_demand_kl"].values
        w = w / w.sum() if w.sum() > 0 else np.ones(len(g)) / len(g)
        L_hub = float((g["lt_prod"] + g["lt_plant_hub"]).values @ w)
        sigma_L = float(g["var_prod"].values @ w)
        ss = z * np.sqrt(L_hub * sigma_pooled ** 2 + d ** 2 * sigma_L ** 2)
        rop = d * L_hub + ss
        rows.append({"sku": sku, "hub": hub, "avg_daily_demand_kl": round(d, 4),
                     "lead_time_days": round(L_hub, 1),
                     "safety_stock_kl": round(ss, 2), "reorder_point_kl": round(rop, 2),
                     "days_of_cover": round(rop / d, 1) if d > 1e-9 else 0.0})
    return pd.DataFrame(rows)
