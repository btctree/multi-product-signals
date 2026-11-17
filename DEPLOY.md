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
| **The VM picks up the new code** | at the next **`:25`** | the hourly `publish_web` crontab line runs `git fetch; git reset --hard origin/main` (and the 23:35 trading line does the same for its own run). `/root/monday_catchup.sh` does **not** pull first |
| A new **VM cron line** (such as the 22:40 post-close request) | only when the owner installs it on the VM | the crontab is not in the repo; `execution/vm_ops/crontab.reference` is a copy. See "The 22:40 post-close build request" below |
| The new code first **trades** | the next trading run after the VM has it: the **23:35 UTC** run (which also pulls just before it trades), or the **10:00 London** catch-up when the push landed before the `:25` ahead of it (09:25 UTC since 25 Oct 2026, 08:25 UTC in summer) | the VM's root crontab is `CRON_TZ=Europe/London`, but since 2026-09-25 the trading line alone sits in a `CRON_TZ=UTC` bracket as `35 23 * * *`, so the October clock change cannot move it. The catch-up is London-timed (10:00 London: 09:00 UTC in summer, 10:00 UTC in winter) and pulls nothing itself. The live crontab, as read on 2026-10-27, is `execution/vm_ops/crontab.reference` (refresh it after any change on the VM) |

So a fix pushed at 14:00 UTC is on the dashboard within minutes, on the VM's
disk at 14:25, and in front of the market at 23:35. After that run, check
`data/bot_state.json` `config`: its rule values should be what you expect,
and its `commit` (the VM checkout's HEAD, usually the `bot: state update`
commit just before the run) should contain yours:
`git merge-base --is-ancestor <your-commit> <config commit> && echo included`.

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

For a change merged ON `main` with `git merge --no-ff`, run
`git revert -m 1 <merge-commit>` and push; the VM picks it up at the next
`:25`. Check the parents first (`git show -s --format=%p <merge-commit>`): `-m` must name the
old `main`. The October releases (`0d96763d`, and `b6145324` under `63feabac`)
were made the other way round - `origin/main` merged into the release branch,
then `main` fast-forwarded - so there the old `main` is parent **2**, and
`-m 1` would revert only the VM state commits that merge pulled in (live
data files) and none of the release.

Single features have code flags, changed by a commit: `KILL_SWITCH`,
`OWN_BASE_FUND`, `PARK_MODE` and `MARKET_FILTER` in `ib_bot.py` (and
`daily_signal.py` where it has one), and `MODEL = "E"` in `engine/config.py`.
Environment switches such as `MAX_HOLD_BARS=0` are **not** in the code: set
them on every site that runs `ib_bot.py` - the 23:35 crontab line and
`/root/monday_catchup.sh`. See the rollback table in `execution/README.md`.

## What runs automatically

- **Hourly** (`5 * * * *`): fresh prices for the whole pool
  (`data/universe.json` listed 995 tickers on 2026-09-25 and **992** on
  2026-10-27) → SEC earnings facts for that hour's US dips (the E1 filter;
  repo secret `SEC_USER_AGENT`; a failure only means plain E) → rebuild every
  analysis card + charts → the display-only Sim lines (`docs/sim_lines.json`)
  → deploy dashboard. No repo bloat — price data is deployed as a Pages
  artifact, never committed. `docs/data.json` is rebuilt each run and is not
  in the repo.
- **Daily at 00:20 UTC** (`20 0 * * *`): additionally refreshes the universe -
  grow-only: names are added, and one is removed only after 5 daily checks in
  a row find very low trading volume - and exports the backtest trade history.
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
- **Post-close build, 22:40 UTC Monday to Friday** (built 2026-11-17, owner's
  approval; runs only once the VM cron line below is installed): see the next
  section.

### The 22:40 post-close build request (2026-11-17)

**Why.** The 23:35 UTC trading run decides a market only on a signal build
that *started* after that market's close + 90 minutes - for the US, 22:30 UTC
in winter (21:30 in US summer). GitHub fires the "hourly" cron only 3 to 6
times a day, and the VM's own commits carry `[skip ci]`, so on most nights no
build started between 22:30 and 23:35 (since 1 Nov, only 3 of 11 nights had
one). The run then deferred every US exit and entry to the 10:00 London
catch-up, and the 23:40 digest showed `BUY: none`.

**How.** At 22:40 UTC, Monday to Friday, the VM runs
`execution/post_close_build.py`. It writes `data/post_close_build.json`
(`requested_at`, `for_close`, `reason`), commits **only that file** as `ib-bot`
in a commit **without** `[skip ci]`, and pushes it through `gitpush.py`. The
push trigger of `daily.yml` (`push: branches: [main]`, no paths filter) then
builds and deploys signals; the price download starts after 22:30, so the
build's `generated_at` counts as post-close for every market. 22:30 to 00:00
UTC is the only window in which the US, Europe, Hong Kong and Japan are all
closed and settled (Tokyo opens at 00:00 UTC). It does nothing on Saturday or
Sunday (UTC), before today's US settle, or when today's request is already on
`origin/main`; `--dry` shows what it would do. Log: `/root/post_close.log`.

- **`daily.yml` on that push.** The build runs like any push build: the
  universe refresh and the `persist` job are skipped (both only run on the
  00:20 cron or a manual run), so nothing else is committed. `concurrency:
  cancel-in-progress` means an hourly run already going at 22:40 is replaced
  by this one, and an hourly run fired while this one builds replaces it -
  either way the surviving build started after 22:30. Builds take about 3
  minutes (2 to 11), so it is on Pages long before 23:35.
- **US summer** (settle 21:30 UTC): 22:40 is 70 minutes after the settle, so
  the build is still post-close; an hourly build after 21:30 would also do.
- **US holidays**: no market calendar, by design. The request still goes out
  and the build finds no new bar - one spare build, nothing else.
- **The alarm.** If the build the 23:35 run reads still started before a
  market's settled close, `ib_bot.py` queues one Telegram message per market
  and close: "After-close update missing: ... US buys and sells wait for the
  next run (Tue 10:00 UTC)". If the same close is still undecidable at a later
  run that is its last chance (the 10:00 catch-up for the US), a louder one:
  "US close of ... is being MISSED". Europe is one market (one message for
  all its exchanges), and a candidate the run would skip anyway (London, or
  one `entry_blocked_reason` refuses) does not count. Message only: no order
  changes.
- **The alarm goes live with the deploy, the request only with the cron
  line.** The 23:35 line pulls `ib_bot.py` itself, so the alarm runs from the
  first evening after this release is on `main`; the 22:40 request needs the
  install below. Install it the same day: without it, expect the plain US
  alarm on most weeknights (8 of the 11 nights from 1 November had no build
  in time).

**Install (once, from Oracle Cloud Shell, after this release is on `main` and
the VM has pulled it at a `:25`).** One line - never paste multiple lines
into the VM. It backs up the crontab to `/root/crontab.bak-<UTC time>-<pid>`,
refuses if `post_close_build.py` is not on the VM yet, adds the two lines
only if they are not there already, and prints `crontab -l`:

```bash
ssh -i ~/mp_vm_key opc@152.67.158.175 'sudo sh -c "F=/root/multi-product-signals/execution/post_close_build.py; B=/root/crontab.bak-\$(date -u +%Y%m%dT%H%M%SZ)-\$\$; if [ ! -f \$F ]; then echo NOT INSTALLED - \$F is not on the VM yet, deploy first; elif crontab -l > \$B && [ -s \$B ]; then if grep -q post_close_build.py \$B; then echo ALREADY INSTALLED - crontab unchanged; else { cat \$B; echo CRON_TZ=UTC; echo \"40 22 * * 1-5 cd /root/multi-product-signals/execution && /usr/bin/python3 post_close_build.py >> /root/post_close.log 2>&1\"; } | crontab - && echo INSTALLED - backup in \$B; fi; else echo NOT INSTALLED - could not read the crontab; fi; crontab -l"'
```

The two lines it appends, at the end of the London-timed crontab (the
ADDED block of `execution/vm_ops/crontab.reference`):

```
CRON_TZ=UTC
40 22 * * 1-5 cd /root/multi-product-signals/execution && /usr/bin/python3 post_close_build.py >> /root/post_close.log 2>&1
```

Optional checks, each one line. Before installing, a dry run (it fetches,
writes nothing; before 22:30 UTC or at a weekend it only says "refused",
which still proves it runs):
`ssh -i ~/mp_vm_key opc@152.67.158.175 'sudo sh -c "cd /root/multi-product-signals/execution && /usr/bin/python3 post_close_build.py --dry"'`.
Right after installing, the crontab should end in the two lines:
`ssh -i ~/mp_vm_key opc@152.67.158.175 'sudo crontab -l | tail -2; sudo tail -3 /root/post_close.log'`
(the log does not exist until the first 22:40). After the first weekday
22:40 the same line should show `requested: data/post_close_build.json pushed
for <date>`, the Actions tab should show a push build of `signals: post-close
build request for <date>`, and that night's `bot.log` should have no
"deferred to a fresh build" line for US names.

To remove it, one line. It backs up the crontab again, removes the two lines
only when they are still the last two, and prints `crontab -l`; otherwise it
changes nothing and says so (then restore a backup with
`sudo crontab /root/crontab.bak-...`):

```bash
ssh -i ~/mp_vm_key opc@152.67.158.175 'sudo sh -c "B=/root/crontab.bak-\$(date -u +%Y%m%dT%H%M%SZ)-\$\$; if crontab -l > \$B && [ -s \$B ]; then if tail -1 \$B | grep -q post_close_build.py && [ \"\$(tail -2 \$B | head -1)\" = CRON_TZ=UTC ]; then head -n -2 \$B | crontab - && echo REMOVED - backup in \$B; else echo NOT REMOVED - the 22:40 lines are not the last two, crontab unchanged; fi; else echo NOT REMOVED - could not read the crontab; fi; crontab -l"'
```

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
that file is what fills the Positions and History tabs. One exception: a
share you buy by hand in IB is not managed by the bot's exit rules until you
add a line for it to `execution/adopt.json` and push (see
`execution/README.md`, step 2b).

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
