# Deploy to GitHub — 3 commands, ~2 minutes

Everything is committed locally on branch `main` and ready. The dashboard
auto-updates **hourly** once online (free on a public repo).

## Step 1 — create the repo (30 seconds, in browser)
github.com → **New repository** → name: `multi-product-signals` → **Public**
(required for free GitHub Pages + unlimited free Actions minutes) →
leave everything unticked → **Create repository**.

## Step 2 — push (paste in PowerShell)
```powershell
cd "C:\Users\user\OneDrive\Desktop\Multi Product"
git remote add origin https://github.com/<YOUR_USERNAME>/multi-product-signals.git
git push -u origin main
```
When prompted, sign in (browser window) — or paste a fresh Personal Access Token
as the password. **Do not use the old token from Multi-Market System.txt — it is
exposed; revoke it and create a new one** (github.com → Settings → Developer
settings → Fine-grained tokens → this repo → Contents: Read/Write).

## Step 3 — turn on Pages (30 seconds, in browser)
Repo → **Settings → Pages → Source = "GitHub Actions"**.
Then **Actions tab → "Signals + dashboard (hourly)" → Run workflow** for the
first build (~4 min).

## Your dashboard link
```
https://<YOUR_USERNAME>.github.io/multi-product-signals/
```

### Install on iPhone 14 Pro as an app
1. Open the link in **Safari**
2. Tap the **Share** button → **Add to Home Screen** → Add
3. It opens full-screen with its own icon, no browser bars — like a native app.

## What runs automatically
- **Hourly** (`:05` past each hour): fresh prices for all ~194 products →
  rebuild every analysis card + charts → deploy dashboard. No repo bloat —
  price data is deployed as an artifact, never committed.
- **Midnight UTC daily**: additionally re-ranks the universe by market cap
  (adds/removes products) and commits the universe + name cache.
- Why hourly and not every 30 min: GitHub skips sub-hourly crons under load and
  Yahoo throttles heavy scraping (our own BTC project's finding: "hourly =
  reliable; sub-hourly = flaky"). Signals are computed on daily closes anyway —
  hourly refresh is already more than the strategy needs.

## Recording trades (fills the Positions/History tabs)
```powershell
cd engine
python position_cli.py buy 3988.HK 4.81
python position_cli.py sell 3988.HK 5.20
git add ../data/positions.json; git commit -m "trade"; git push
```
