# Deploy

> **The repo already exists and is live.** It is
> `https://github.com/btctree/multi-product-signals.git`, Pages is on, the
> workflows run, and an Oracle VM trades a real IB account off what lands on
> `main`. Steps 1–3 below are the original first-time setup, kept for the
> record. For an everyday change, read **"Deploying a change"** first.

## Deploying a change

```powershell
cd "C:\Users\user\OneDrive\Desktop\Claude\Multi Product"
git push origin HEAD:main
```

Then three clocks decide when your change actually does anything:

| What | When | Why |
|---|---|---|
| Dashboard rebuild + Pages deploy | immediately — `daily.yml` has `push: branches: [main]` | the site is rebuilt from the push, not from the next hourly cron |
| **The VM picks up the new code** | at the next **`:25`** | the hourly `publish_web` crontab line is the only one that runs `git fetch; git reset --hard origin/main`. `/root/monday_catchup.sh` does **not** pull first |
| The new code first **trades** | the next trading run — **23:35 UTC** while the UK is on BST | the VM's root crontab is `CRON_TZ=Europe/London`, so its `35 0` line is 23:35 UTC in summer (and 00:35 UTC after the clocks go back on 25 Oct 2026 — see `execution/README.md`) |

So a fix pushed at 14:00 UTC is on the dashboard within minutes, on the VM's
disk at 14:25, and in front of the market at 23:35.

### The token needs the `workflow` scope for `.github` changes

A push that touches anything under `.github/` — `daily.yml`, `add-product.yml`,
any workflow file — is **rejected** unless the token carries workflow
permission: the `workflow` scope on a classic PAT, or "Workflows: Read and
write" on a fine-grained one. The error names the refused ref and mentions the
workflow scope; it is not a credential problem, so re-entering the same token
will not help. The scope was added to the operator's token on 2026-09-22.

If a push asks for credentials at all, the operator runs it himself rather than
pasting a token into a prompt:

```powershell
git -C "C:\Users\user\OneDrive\Desktop\Claude\Multi Product" push origin HEAD:main
```

### Rolling a change back

`git revert -m 1 <merge-commit>` and push; the VM picks it up at the next `:25`.
Behaviour switches such as `MAX_HOLD_BARS=0` are **not** in the code — they are
environment variables that must be set on every crontab line that runs
`ib_bot.py`. See the rollback table in `execution/README.md`.

## What runs automatically

- **Hourly** (`5 * * * *`): fresh prices for the whole pool
  (`data/universe.json` listed **995 tickers** on 2026-09-25) → rebuild every
  analysis card + charts → deploy dashboard. No repo bloat — price data is
  deployed as a Pages artifact, never committed. `docs/data.json` is rebuilt
  each run, so the copy committed in the repo is stale and is not what the site
  serves.
- **Daily at 00:20 UTC** (`20 0 * * *`): additionally re-ranks the universe by
  market cap (adds/removes products) and exports the backtest trade history.
  The three files that change — `data/universe.json`, `data/company_names.json`,
  `data/backtest_trades.json` — are handed to a separate **`persist`** job,
  which is the only job allowed to push.
- **VM liveness watchdog**, every run: warns and messages Telegram when
  `data/bot_state.json` is more than 3 h old. Every other Telegram message
  comes from the VM, so this is what notices a VM that has gone silent.
- Why hourly and not every 30 min: GitHub skips sub-hourly crons under load and
  Yahoo throttles heavy scraping (our own BTC project's finding: "hourly =
  reliable; sub-hourly = flaky"). Signals are computed on daily closes anyway —
  hourly refresh is already more than the strategy needs.

### CI installs the hashed lock, not `requirements.txt`

Both workflows run `pip install --require-hashes -r requirements-ci.txt`. That
file pins every package with `==` and carries every sha256 PyPI lists for that
version, so a new or re-uploaded PyPI release cannot reach the job that builds
the `data.json` the bot trades on. `requirements.txt` stays the short
human-readable list for the desktop and the VM's python3.11 test export —
**after changing it, regenerate the lock** (its header says how) or CI keeps
running the old pins.

### Least privilege

The build job holds a read-only token (`contents: read`, plus `pages: write`
and `id-token: write` for the deploy) and checks out with
`persist-credentials: false`. It runs third-party PyPI code, so it is not
allowed to push. The small `persist` job holds `contents: write`, installs
nothing, runs no repo Python, and commits only those three files — and only
when each is valid JSON.

## Recording trades

Nothing to do. The bot records its own fills and the VM's
`execution/publish_web.py` publishes `data/bot_state.json` hourly at `:25`;
that file is what fills the Positions and History tabs.

> **Do not use the old manual path.** `engine/position_cli.py` writes
> `data/positions.json`, which no longer exists in this repo, and committing it
> would create a second set of books beside the bot's while changing nothing the
> dashboard shows. Deposits, withdrawals and the earmark routine are in
> `execution/README.md` under "Money in and out".

---

## Original first-time setup (historical — already done)

### Step 1 — create the repo
github.com → **New repository** → name: `multi-product-signals` → **Public**
(required for free GitHub Pages + unlimited free Actions minutes) →
leave everything unticked → **Create repository**.

### Step 2 — push
```powershell
cd "C:\Users\user\OneDrive\Desktop\Claude\Multi Product"
git remote add origin https://github.com/<YOUR_USERNAME>/multi-product-signals.git
git push -u origin main
```
When prompted, sign in (browser window) — or paste a fresh Personal Access
Token as the password. **Do not use the old token from Multi-Market System.txt
— it is exposed; revoke it and create a new one** (github.com → Settings →
Developer settings → Fine-grained tokens → this repo → Contents: Read/Write,
**and Workflows: Read/Write** if it will ever push a `.github/` change).

### Step 3 — turn on Pages
Repo → **Settings → Pages → Source = "GitHub Actions"**.
Then **Actions tab → "Signals + dashboard (hourly)" → Run workflow** for the
first build (~4 min). A manual run counts as a daily run: it also refreshes the
universe and commits the three daily files.

## Your dashboard link
```
https://btctree.github.io/multi-product-signals/
```

### Install on iPhone as an app
1. Open the link in **Safari**
2. Tap the **Share** button → **Add to Home Screen** → Add
3. It opens full-screen with its own icon, no browser bars — like a native app.

⚠️ A deploy verified on the server can still be invisible on the phone: the
installed app caches aggressively. Check the build stamp on the page before
concluding a change did not ship.
