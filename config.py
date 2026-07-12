"""Central configuration for the Levisol Planning System."""
BATCH_KL = 25.0
DAYS_PER_MONTH = 30
HUB_SERVICE_LEVEL = 0.98          # per case: hub SL for all grades
TIER_CUTS = (0.50, 0.80, 0.95)    # cumulative volume slabs A/B/C/D
XYZ_CUTS = (0.30, 0.70)           # CV thresholds
DEFAULT_HUB_SHORTFALL_COST = 75000  # Rs/kL soft penalty for hub SS shortfall
