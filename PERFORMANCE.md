# Candidate Engine Scorecard (11.2y, net of costs, honest fills)

*C4 (calls) is a MODELED deep-ITM overlay, not a chain backtest — see options_model.py banner. maxDD is the peak-to-trough equity fall.*

> ⚠️ **None of the engines below is the live product, and this file is
> machine-written.**
>
> **What it is.** A scorecard of four *candidate* engines (C1–C4) from the
> high-R:R research line, kept so the frontier can be re-read. `engine/PROGRESS.md`
> lists it under SP1, the branch that showed win ≥ 55% *and* R:R ≥ 2 is not
> reachable (it needs PF 2.44; the best measured was 1.87). C4 in particular is
> a modelled options overlay with a −91.8% drawdown — it was never a shipping
> candidate.
>
> **What runs instead.** The live product since 2026-07-11 is **config D**, a
> 15-position pool (13 equity + 2 crypto) with no sleeve split. Its two honest
> measurements, neither of which appears in the tables below:
>
> | | Full-ruleset revalidation | What the dashboard header shows |
> |---|---|---|
> | Source | `data/revalidation.json`, tag `D KILO-S60-15 …` | `data/exit_timing_test.json`, arm `E live k-anchor + time stop 60` |
> | Win / CAGR / maxDD | 52.5% / 30.8% / −29.0% | 51.6% / 30.1% / −28.5% |
>
> The second is the bot's own exit rules — stop tested on the close, sold
> market-at-next-open. See README.md, "Two headline measurements".
>
> **This file is generated.** `engine/research_rr_detail.py` rewrites
> PERFORMANCE.md whole (`open("../PERFORMANCE.md", "w")`), so **this note is
> deleted by the next run of that script** and the tables below are whatever
> that run measured. The generator stamps no date, so the file itself cannot
> tell you how old its numbers are — check the script's last commit instead.

## Overall

| Engine | Win | R:R | PF | CAGR | Total ret | 150k → | maxDD | Sharpe | Calmar |
|---|--:|--:|--:|--:|--:|--:|--:|--:|--:|
| C1 Balanced (Q6 spot) | 55.0% | 1.43 | 1.60 | 20.8% | 727% | 1,240,588 | -21.6% | 0.85 | 0.96 |
| C2 Max-R:R (A1 spot) | 46.3% | 2.10 | 1.72 | 19.8% | 659% | 1,138,114 | -32.8% | 0.71 | 0.60 |
| C3 Max-R:R in DD (A5 spot) | 45.8% | 2.18 | 1.72 | 21.0% | 742% | 1,262,710 | -30.5% | 0.71 | 0.69 |
| C4 Full deep-ITM CALLS | 39.6% | 2.39 | 1.17 | 30.5% | 1,864% | 2,945,653 | -91.8% | 0.89 | 0.33 |

## Return per year (%)

| Year | C1 | C2 | C3 | C4 |
|---|--:|--:|--:|--:|
| 2016 | +30 | +28 | +27 | +98 |
| 2017 | +44 | +53 | +54 | +472 |
| 2018 | -2 | -11 | -8 | -64 |
| 2019 | +32 | +30 | +24 | +226 |
| 2020 | +24 | +17 | +29 | -21 |
| 2021 | +52 | +35 | +36 | +120 |
| 2022 | -5 | -5 | -9 | -65 |
| 2023 | +18 | +27 | +33 | +34 |
| 2024 | +26 | +40 | +54 | +116 |
| 2025 | +52 | +57 | +46 | +295 |
| 2026 | +0 | +3 | +3 | -10 |

## Win rate per year (%)

| Year | C1 | C2 | C3 | C4 |
|---|--:|--:|--:|--:|
| 2016 | 61 | 53 | 52 | 46 |
| 2017 | 61 | 52 | 52 | 44 |
| 2018 | 52 | 41 | 41 | 34 |
| 2019 | 62 | 58 | 57 | 49 |
| 2020 | 53 | 38 | 41 | 31 |
| 2021 | 55 | 50 | 49 | 49 |
| 2022 | 41 | 37 | 36 | 26 |
| 2023 | 59 | 47 | 47 | 39 |
| 2024 | 60 | 56 | 52 | 49 |
| 2025 | 56 | 48 | 48 | 45 |
| 2026 | 48 | 32 | 31 | 25 |
