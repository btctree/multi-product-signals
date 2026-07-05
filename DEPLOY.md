# Deploy to GitHub (automatic daily run + mobile dashboard)

The repo is already committed locally on branch `main`. Do this once to put it
online and have it run itself every day.

## 1. Create a new, empty GitHub repo
On github.com → **New repository** (do **not** add a README/.gitignore).
Name it e.g. `multi-product-signals`. **Do not reuse the BTC repo.**

## 2. Push
```bash
cd "C:\Users\user\OneDrive\Desktop\Multi Product"
git remote add origin https://github.com/<YOUR_USER>/<YOUR_REPO>.git
git push -u origin main
```
If asked to authenticate, use a **new** GitHub Personal Access Token (see security note).

## 3. Turn on GitHub Pages
Repo → **Settings → Pages → Build and deployment → Source = "GitHub Actions"**.

## 4. Run it
Repo → **Actions → "Daily signals + dashboard" → Run workflow** (first run fetches
~194 products + company names; ~3–6 min). After it succeeds, your dashboard is at:
```
https://<YOUR_USER>.github.io/<YOUR_REPO>/
```
Open that on your iPhone 14 Pro and "Add to Home Screen" for an app-like view.
It then re-runs automatically every day at 00:00 UTC (edit the `cron` in
`.github/workflows/daily.yml` to change the time).

## 5. Record your trades (so Positions/History fill in)
Locally, when you act on a signal:
```bash
cd engine
python position_cli.py buy 3988.HK 4.81      # records a fill + prints the GTC orders to place
python position_cli.py sell 3988.HK 5.20      # records the exit
```
Commit `data/positions.json` (and a `data/trade_log.json` you keep) and push;
the next dashboard build shows them on the Positions and History tabs.

## Security note (important)
`Multi-Market System.txt` contains a GitHub token and is **git-ignored** so it is
never pushed. That token has been sitting in a plain file — **rotate it**:
GitHub → Settings → Developer settings → Personal access tokens → revoke it and
issue a new one for the push above. Never paste tokens into files or chat.

## What runs
`.github/workflows/daily.yml`: refresh universe (market-cap re-rank) → download
10y daily data → `build_dashboard.py` (per-product analysis + price history) →
commit `docs/` → deploy to Pages. One job, no stale-deploy race.
