#!/usr/bin/env python3
"""Golden tests: the bot's effective trading rules reach data/bot_state.json.

Run from this directory:  python test_bot_config.py

The owner's reviewer, 2026-10-26: the rules must be checkable without VM
access. The code defaults changed (HK$50k cap, free-cash sizing, kill switch
off, crypto blocked, cash parking in shadow), and any environment variable
left in the VM's crontab silently overrides a default - the crontab is not in
the repo. So every LIVE trading run records the rules it actually ran with,
env overrides applied (ib_bot.effective_config), and publishes them as
bot_state.json "config". Locked down here:

  * the values are the ones in effect, read at run time - a leftover
    MAX_ORDER_BASE or DAILY_LOSS_KILL shows up, both as the value and in "env";
  * only whitelisted variable names are ever published;
  * a live run sets it, --dry never does;
  * BOTH writers keep it: ib_bot.publish_state writes the run's own or carries
    the previous one (publish_only), and publish_web.py - the hourly publisher,
    which never trades - carries it untouched and never writes its own.

Nothing here touches /root or pushes anything: every path is a temp dir and
git is a stub.
"""
import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

os.environ.setdefault("IB_BACKEND", "web")
import testenv                                     # noqa: E402
_TMP = testenv.isolate("mps-config-")
os.environ.pop("EXCLUDED_CASH", None)
for _v in ("MAX_ORDER_BASE", "TARGET_POSITIONS", "DAILY_LOSS_KILL", "LIMIT_BUFFER",
           "FX_CONVERT", "FX_FUND_NONBASE", "BASE_FUND_BUFFER", "HK_ENABLED",
           "MAX_HOLD_BARS", "CONFIRM_FIRST", "SIGNALS_URL", "IB_BASE_CCY"):
    os.environ.pop(_v, None)

import alerts                                      # noqa: E402
import broker                                      # noqa: E402
import ib_bot                                      # noqa: E402
import ib_web                                      # noqa: E402
testenv.assert_isolated()

BASE = ib_bot.BASE_CCY
RUN_AT = datetime(2026, 9, 16, 23, 35, 20, tzinfo=timezone.utc)


class Patch:
    def __init__(self, mod, **kw):
        self.mod, self.kw, self.old = mod, kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = getattr(self.mod, k)
            setattr(self.mod, k, v)
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            setattr(self.mod, k, v)


class Env:
    """Set environment variables for a block, restoring what was there."""

    def __init__(self, **kw):
        self.kw, self.old = kw, {}

    def __enter__(self):
        for k, v in self.kw.items():
            self.old[k] = os.environ.get(k)
            os.environ[k] = v
        return self

    def __exit__(self, *a):
        for k, v in self.old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def t1_the_rules_in_effect_by_default():
    cfg = ib_bot.effective_config()
    want = {"target_positions": 15, "sizing": "free_cash/free_slots",
            "max_order_base": 50000.0, "entry_move_max": 0.10, "limit_buffer": 0.005,
            "kill_switch": "off", "fx_convert": False, "fx_fund_nonbase": True,
            "own_hkd_funding": True, "base_fund_buffer": 1.03, "hk_enabled": False,
            "max_hold_bars": 60, "crypto_entries": "blocked",
            "entry_order": "tier, then score", "park_mode": "shadow",
            "park_symbol": "SGOV", "base_ccy": "HKD", "dry": False}
    for k, v in want.items():
        assert cfg.get(k) == v, (k, cfg.get(k), v)
    assert set(cfg["env"]) <= {"IB_BACKEND"}, cfg["env"]     # set by this harness only
    assert cfg["commit"] is None or len(cfg["commit"]) == 12, cfg["commit"]
    json.dumps(cfg)                                          # publishable as it is
    print("t1 the rules in effect, by default: cap 50k, free cash / free slots, kill "
          "switch off, crypto blocked, parking shadow OK")


def t2_a_leftover_vm_override_is_visible():
    # What a cron line with these set would give at import - and what this
    # file exists to expose: the value in effect AND the variable behind it.
    with Env(MAX_ORDER_BASE="20000", DAILY_LOSS_KILL="0", HK_ENABLED="1",
             TELEGRAM_TOKEN="123:secret", IB_OAUTH_SECRET="do-not-publish"), \
            Patch(ib_bot, MAX_ORDER_BASE=20000.0, DAILY_LOSS_KILL=0.0, HK_ENABLED=True):
        cfg = ib_bot.effective_config()
    assert cfg["max_order_base"] == 20000.0 and cfg["hk_enabled"] is True, cfg
    assert cfg["daily_loss_kill"] == 0.0 and cfg["kill_switch"] == "off", cfg
    assert cfg["env"]["MAX_ORDER_BASE"] == "20000", cfg["env"]
    assert cfg["env"]["DAILY_LOSS_KILL"] == "0" and cfg["env"]["HK_ENABLED"] == "1"
    blob = json.dumps(cfg)
    assert "secret" not in blob and "TELEGRAM" not in blob and "OAUTH" not in blob, blob
    assert set(cfg["env"]) <= set(ib_bot.CONFIG_ENV_VARS), cfg["env"]
    # a value that cannot be read never costs the run its config
    with Patch(ib_bot, _code_commit=lambda: 1 / 0):
        assert "error" in ib_bot.effective_config()
    print("t2 a leftover MAX_ORDER_BASE / DAILY_LOSS_KILL on the VM shows in config; "
          "nothing outside the whitelist is published OK")


class RunIB:
    def accountValues(self):
        return [broker.AccountValue("NetLiquidation", "100000", BASE),
                broker.AccountValue("CashBalance", "7", BASE),
                broker.AccountValue("CashBalance", "1000", "USD")]

    def positions(self):
        return []

    def reqAllOpenOrders(self):
        pass

    def openTrades(self):
        return []

    def sleep(self, *a):
        pass

    def disconnect(self):
        pass


def t3_a_live_run_records_its_config_and_dry_does_not():
    seen = []
    for dry in (False, True):
        d = Path(tempfile.mkdtemp(prefix="run-", dir=str(_TMP)))
        state = d / "state.json"
        state.write_text('{"map": {}, "pos": {}}', encoding="utf-8")
        alerts.DIR = d / "outbox"
        with Patch(ib_bot, STATE=state, IB=RunIB, connect_or_heal=lambda *a, **k: None,
                   get_json=lambda url: {"generated": "x", "actions": []},
                   _now_utc=lambda: RUN_AT, ADOPT=d / "adopt.json",
                   publish_state=lambda ib_, st, nl: seen.append(dict(ib_bot._RUN_CONFIG)),
                   EXIT_ATTEMPTS=d / "exit_attempts.json", FILLS_LEDGER=d / "fills.jsonl",
                   log=lambda *a: None):
            ib_bot.run(dry=dry)
            if dry:
                assert ib_bot._RUN_CONFIG == {}, "--dry recorded a config"
        ib_bot._POCKET_RUN.clear()
    assert len(seen) == 1, "--dry must not publish"
    assert seen[0]["max_order_base"] == 50000.0 and seen[0]["dry"] is False, seen[0]
    assert seen[0]["kill_switch"] == "off" and seen[0]["at"].endswith(" UTC"), seen[0]
    ib_bot._RUN_CONFIG.clear()
    print("t3 a live run records the config it ran with; --dry records none OK")


class Done:
    returncode, stdout, stderr = 0, "", ""


class PubIB:
    def positions(self):
        return []

    def accountValues(self):
        return [broker.AccountValue("NetLiquidation", "100000", BASE),
                broker.AccountValue("CashBalance", "10", BASE)]


def seed_repo(prev):
    repo = Path(tempfile.mkdtemp(prefix="repo-", dir=str(_TMP)))
    (repo / "data").mkdir()
    (repo / "execution").mkdir()
    (repo / "execution" / "state.json").write_text('{"map": {}, "pos": {}}', encoding="utf-8")
    (repo / "data" / "bot_state.json").write_text(json.dumps(prev, indent=1), encoding="utf-8")
    return repo


def bot_publish(repo):
    import fills_capture
    import flex_dividends as fd
    import uk_cgt
    old = (ib_bot.__file__, subprocess.run, fills_capture.capture,
           fd.capture_if_configured, uk_cgt.build_report)
    try:
        # publish_state finds the repo from its own file: point that at the temp
        # repo, so neither the real data/ nor a real git push can be touched
        ib_bot.__file__ = str(repo / "execution" / "ib_bot.py")
        subprocess.run = lambda *a, **k: Done()
        fills_capture.capture = lambda *a, **k: 0
        fd.capture_if_configured = lambda *a, **k: 0
        uk_cgt.build_report = lambda *a, **k: None
        ib_bot.publish_state(PubIB(), {"map": {}, "pos": {}}, 100000.0)
    finally:
        (ib_bot.__file__, subprocess.run, fills_capture.capture,
         fd.capture_if_configured, uk_cgt.build_report) = old
    return json.loads((repo / "data" / "bot_state.json").read_text(encoding="utf-8"))


OLD_CFG = {"at": "2026-10-26 23:35 UTC", "max_order_base": 50000.0, "kill_switch": "off",
           "env": {"HK_ENABLED": "1", "IB_BACKEND": "web"}}


def t4_ib_bot_publish_state_writes_or_carries_the_config():
    # (a) a trading run: its own config, replacing the previous one
    run_cfg = ib_bot.effective_config()
    ib_bot._RUN_CONFIG.update(run_cfg)
    try:
        snap = bot_publish(seed_repo({"activity": [], "config": OLD_CFG}))
    finally:
        ib_bot._RUN_CONFIG.clear()
    assert snap["config"] == json.loads(json.dumps(run_cfg)), snap.get("config")
    # (b) --publish-only (no trading run in this process): carried over as it is
    snap = bot_publish(seed_repo({"activity": [], "config": OLD_CFG}))
    assert snap["config"] == OLD_CFG, snap.get("config")
    # (c) nothing to carry: no key - never an invented one
    snap = bot_publish(seed_repo({"activity": []}))
    assert "config" not in snap, snap.get("config")
    assert snap["netliq"] == 100000 and "positions" in snap        # the rest as before
    print("t4 ib_bot.publish_state writes the run's config, or carries the last one OK")


def t5_publish_web_carries_the_config_untouched_and_never_writes_its_own():
    import publish_web
    for prev, want in (({"activity": [], "config": OLD_CFG}, OLD_CFG),
                       ({"activity": []}, None)):
        repo = seed_repo(prev)
        old = (publish_web.STATE, publish_web.DATA, publish_web.REPO, ib_web.snapshot,
               ib_web.ledger, subprocess.run, publish_web.sys.argv)
        try:
            publish_web.STATE = repo / "execution" / "state.json"
            publish_web.DATA, publish_web.REPO = repo / "data", repo
            ib_web.snapshot = lambda: {"positions": [], "netliq": 100000.0,
                                       "cash": {BASE: 10.0}}
            ib_web.ledger = lambda *a, **k: {"BASE": {"netliquidationvalue": 100000.0,
                                                      "exchangerate": 1}}
            subprocess.run = lambda *a, **k: Done()
            publish_web.sys.argv = ["publish_web.py"]
            # its own environment differs from the trading line's - that must
            # not leak into what it publishes
            with Env(MAX_ORDER_BASE="12345"):
                assert publish_web.main() == 0
        finally:
            (publish_web.STATE, publish_web.DATA, publish_web.REPO, ib_web.snapshot,
             ib_web.ledger, subprocess.run, publish_web.sys.argv) = old
        snap = json.loads((repo / "data" / "bot_state.json").read_text(encoding="utf-8"))
        assert snap.get("config") == want, snap.get("config")
        assert "12345" not in json.dumps(snap), snap
    print("t5 publish_web carries the bot's config untouched and never writes its own OK")


if __name__ == "__main__":
    t1_the_rules_in_effect_by_default()
    t2_a_leftover_vm_override_is_visible()
    t3_a_live_run_records_its_config_and_dry_does_not()
    t4_ib_bot_publish_state_writes_or_carries_the_config()
    t5_publish_web_carries_the_config_untouched_and_never_writes_its_own()
    print("ALL BOT CONFIG TESTS PASS")
