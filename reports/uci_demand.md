# Demand model on real data (Step 2)

- **Dataset:** UCI Online Retail (id352) · 366 daily rows
- **Split:** temporal 80/20 (292 train / 74 test days) (temporal — no shuffling, no leakage)
- **MAE:** model **8390.96** vs naive persistence **16360.93** → skill **+48.7%**
- **Verdict:** beats the naive baseline ✓

Top features: `target_dow` (0.51), `target_month` (0.12), `price_lag1` (0.10), `avg_price` (0.10)

> Honest note: much of the skill comes from calendar features (day-of-week) — the model mostly learns *when* people buy, not *how price changes demand*. Price elasticity needs within-product controls (roadmap step 4), which is why this is a demand model, not an elasticity claim.