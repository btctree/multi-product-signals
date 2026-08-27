#!/usr/bin/env python3
"""Action + P&L digest to Telegram, for MANUAL execution while the IB gateway
cannot log in (IBKR forced passkey 2FA, 2026-08-24).

READ-ONLY BY CONSTRUCTION. It places no orders, opens no IB connection, and
imports nothing from ib_bot.py. It replays ib_bot.py's exit/entry decision rules
against the published dashboard - the same source the live bot reads - and
reports what the bot WOULD do, for the operator to place by hand.

Two entry points share build_report():
    daily_signal.py            cron, 23:40 UTC - sends and updates the P&L baseline
    telegram_poll.py /update   on demand - sends WITHOUT touching the baseline,
                               so the "Today" figure keeps measuring from the
                               last scheduled run rather than from the last tap.

Config (never in the repo): /root/telegram.env, mode 600
    TELEGRAM_TOKEN=...
    TELEGRAM_CHAT_ID=...
"""
import json
import os
import sys
import datetime
import urllib.request
import urllib.parse

BASE = "https://btctree.github.io/multi-product-signals/"
PRODUCTS = BASE + "products/"
REPO = os.environ.get("MPS_REPO", "/root/multi-product-signals")
STATE = os.path.join(REPO, "execution", "state.json")
BOT_STATE = os.path.join(REPO, "data", "bot_state.json")
NETLIQ_HIST = os.path.join(REPO, "data", "netliq_history.json")
PREV = os.environ.get("MPS_PREV", "/root/daily_signal_prev.json")
MANUAL = os.environ.get("MPS_MANUAL", "/root/manual_state.json")
ENVF = os.environ.get("MPS_ENV", "/root/telegram.env")

# mirrors ib_bot.py
TARGET_POSITIONS = int(os.environ.get("TARGET_POSITIONS", "15"))
MAX_ORDER_BASE = float(os.environ.get("MAX_ORDER_BASE", "20000"))
LIMIT_BUFFER = float(os.environ.get("LIMIT_BUFFER", "0.005"))
MAX_HOLD_BARS = int(os.environ.get("MAX_HOLD_BARS", "60"))
DAILY_LOSS_KILL = float(os.environ.get("DAILY_LOSS_KILL", "0.08"))


def log(*a):
    print(datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC"), *a,
          file=sys.stderr)


def load_cfg():
    cfg = {}
    for line in open(ENVF, encoding="utf-8"):
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, v = line.split("=", 1)
            cfg[k.strip()] = v.strip()
    if not cfg.get("TELEGRAM_TOKEN") or not cfg.get("TELEGRAM_CHAT_ID"):
        raise RuntimeError("TELEGRAM_TOKEN / TELEGRAM_CHAT_ID missing from %s" % ENVF)
    return cfg


def get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "mps-daily-signal"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.load(r)


def send_message(token, chat, text):
    data = urllib.parse.urlencode({"chat_id": chat, "parse_mode": "HTML",
                                   "disable_web_page_preview": "true",
                                   "text": text}).encode("utf-8")
    req = urllib.request.Request("https://api.telegram.org/bot%s/sendMessage" % token, data=data)
    with urllib.request.urlopen(req, timeout=30) as r:
        resp = json.load(r)
    if not resp.get("ok"):
        raise RuntimeError("telegram rejected: %s" % json.dumps(resp)[:300])
    return resp["result"]["message_id"]


def yahoo(sym, problems):
    """Fallback price + SMA200 for anything the dashboard has no card for.
    Every .T ticker currently 404s: cards are generated for PUBLISHED holdings
    and publishing froze at 2026-08-24 07:20 UTC, before 5301 was bought."""
    try:
        d = get_json("https://query1.finance.yahoo.com/v8/finance/chart/"
                     + urllib.parse.quote(sym) + "?range=1y&interval=1d", timeout=40)
        res = d["chart"]["result"][0]
        px = res["meta"].get("regularMarketPrice")
        closes = [c for c in res["indicators"]["quote"][0]["close"] if c is not None]
        return px, (sum(closes[-200:]) / 200 if len(closes) >= 200 else None)
    except Exception as e:
        problems.append("yahoo %s: %s" % (sym, e))
        return None, None


def live_quote(sym, problems):
    """Current market price for VALUATION. The dashboard cards are regenerated
    hourly, so intraday they lag by up to an hour; net worth should not. Exit
    DECISIONS still use the card price, because that is what ib_bot.py reads and
    the strategy evaluates on the close - see build_report()."""
    try:
        d = get_json("https://query1.finance.yahoo.com/v8/finance/chart/"
                     + urllib.parse.quote(sym) + "?range=1d&interval=1d", timeout=30)
        m = d["chart"]["result"][0]["meta"]
        return m.get("regularMarketPrice"), m.get("regularMarketTime")
    except Exception as e:
        problems.append("live %s: %s" % (sym, e))
        return None, None


def fx_rates(problems):
    """<ccy> -> HKD."""
    out = {"HKD": 1.0}
    usdhkd = None
    try:
        d = get_json("https://query1.finance.yahoo.com/v8/finance/chart/HKD=X"
                     "?range=5d&interval=1d", timeout=30)
        usdhkd = d["chart"]["result"][0]["meta"].get("regularMarketPrice")
    except Exception as e:
        problems.append("fx USDHKD: %s" % e)
    if not usdhkd:
        usdhkd = 7.846
        problems.append("fx USDHKD fell back to 7.846")
    out["USD"] = usdhkd
    for ccy, pair in (("JPY", "JPY=X"), ("EUR", "EURUSD=X"), ("GBP", "GBPUSD=X")):
        try:
            d = get_json("https://query1.finance.yahoo.com/v8/finance/chart/"
                         + pair + "?range=5d&interval=1d", timeout=30)
            v = d["chart"]["result"][0]["meta"].get("regularMarketPrice")
            out[ccy] = usdhkd / v if ccy == "JPY" else usdhkd * v
        except Exception as e:
            problems.append("fx %s: %s" % (ccy, e))
    return out


def bars_held(entry_date):
    """Weekdays since entry_date, exclusive of entry day. Same as ib_bot.py."""
    try:
        y, m, d = (int(x) for x in str(entry_date)[:10].split("-"))
        start = datetime.date(y, m, d)
    except Exception:
        return 0
    n, cur, today = 0, start, datetime.date.today()
    while cur < today:
        cur += datetime.timedelta(days=1)
        if cur.weekday() < 5:
            n += 1
    return n


def money(v, dp=0):
    return ("{:,.%df}" % dp).format(v)


def _sign(v):
    return "0" if abs(v) < 1 else ("+" if v > 0 else "") + money(v)


def build_report(on_demand=False):
    """Returns (message_text, snapshot). Pure read; writes nothing."""
    problems = []
    state = json.load(open(STATE, encoding="utf-8"))
    bs = json.load(open(BOT_STATE, encoding="utf-8"))

    # While the gateway is down bot_state.json cannot refresh, so any manual fill
    # is invisible to it. MANUAL, when present, is an operator-maintained override
    # of the CURRENT book. Delete it once IB access returns and bot_state resumes
    # refreshing, or it will mask the real positions.
    manual = None
    try:
        manual = json.load(open(MANUAL, encoding="utf-8"))
        if manual.get("positions"):
            bs = dict(bs, positions=manual["positions"],
                      cash=manual.get("cash", bs.get("cash")),
                      updated=manual.get("updated", "manual"))
    except FileNotFoundError:
        manual = None
    except Exception as e:
        problems.append("manual_state.json unreadable (%s) - using bot_state" % e)
        manual = None

    spos = state.get("pos", {})
    peak = state.get("_peak_netliq") or bs.get("netliq") or 0
    fx = fx_rates(problems)

    positions = bs.get("positions", [])
    held_syms = {p.get("symbol") for p in positions}
    exits, holds = [], []
    mv_hkd = cost_hkd = 0.0
    newest_ts = []

    for p in positions:
        ysym = p.get("symbol")
        qty = p.get("qty") or 0
        ccy = p.get("ccy", "USD")
        avg = p.get("avg_cost") or p.get("entry") or 0
        atr = 0
        try:
            card = get_json(PRODUCTS + ysym.replace("/", "_") + ".json")["card"]
            price, sma200, atr = card.get("price"), card.get("sma200"), card.get("atr") or 0
        except Exception:
            price, sma200 = yahoo(ysym, problems)
            problems.append("%s: no dashboard card, used Yahoo" % ysym)
        if not price:
            problems.append("%s: no price at all - SKIPPED" % ysym)
            continue

        st = spos.get(ysym, {})
        entry = st.get("entry") or avg
        hw = max(st.get("hw", price), price)
        k = 2.0 if (entry and price >= entry + 1.5 * atr) else 3.5
        trail = max(st.get("stop", 0), hw - k * atr) if atr else st.get("stop", 0)
        bars = bars_held(st.get("entry_date")) if st.get("entry_date") else 0

        sell = None
        if sma200 and price < sma200:
            sell = "regime break (%.2f < SMA200 %.2f)" % (price, sma200)
        elif trail and price <= trail:
            sell = "trailing stop %.2f" % trail
        elif MAX_HOLD_BARS and bars >= MAX_HOLD_BARS:
            sell = "time stop (%d bars >= %d)" % (bars, MAX_HOLD_BARS)

        # VALUATION uses the live quote (cards refresh hourly and lag intraday);
        # the exit DECISION above deliberately used the card price, because that
        # is what ib_bot.py reads and the strategy evaluates on the close.
        lp, lts = live_quote(ysym, problems)
        mark = lp or price
        if lts:
            newest_ts.append(lts)

        r = fx.get(ccy, 1.0)
        mv_hkd += qty * mark * r
        cost_hkd += qty * avg * r
        row = {"ysym": ysym, "qty": qty, "ccy": ccy, "price": mark, "close": price,
               "stop": trail, "bars": bars, "upl_hkd": qty * (mark - avg) * r,
               "upl_pct": ((mark / avg - 1) * 100) if avg else 0.0,
               "head": ((mark - trail) / trail * 100) if trail else None,
               "reason": sell}
        (exits if sell else holds).append(row)

    cash_hkd = sum((v or 0) * fx.get(c, 1.0) for c, v in (bs.get("cash") or {}).items())
    est_netliq = cash_hkd + mv_hkd
    killed = est_netliq < peak * (1 - DAILY_LOSS_KILL)

    free = TARGET_POSITIONS - len(positions)
    if killed:
        free = 0
    buys = []
    try:
        d = get_json(BASE + "data.json")
        for a in (d.get("actions") or []):
            if free <= 0:
                break
            ysym = a.get("symbol")
            price = a.get("price") or 0
            if not ysym or ysym in held_syms or price <= 0:
                continue
            ccy = {"US": "USD", "JP": "JPY", "HK": "HKD", "EU": "EUR"}.get(a.get("market"), "USD")
            r = fx.get(ccy, 1.0)
            notional = min(est_netliq / TARGET_POSITIONS, MAX_ORDER_BASE)
            shares = int(notional / r / price)
            lot = 100 if ccy == "JPY" else 1
            if lot > 1:
                shares = (shares // lot) * lot
            if shares <= 0:
                problems.append("%s: 1 lot exceeds position size" % ysym)
                continue
            buys.append({"ysym": ysym, "shares": shares, "price": price,
                         "limit": round(price * (1 + LIMIT_BUFFER), 2),
                         "score": a.get("score"), "hkd": int(shares * price * r)})
            free -= 1
    except Exception as e:
        problems.append("actions: %s" % e)

    prev = {}
    try:
        prev = json.load(open(PREV, encoding="utf-8"))
    except Exception:
        pass
    day_pnl = est_netliq - prev["est_netliq"] if prev.get("est_netliq") else None

    overall = overall_pct = None
    try:
        h = json.load(open(NETLIQ_HIST, encoding="utf-8"))
        start = h["series"][0]["nl"]
        flows = sum(f.get("amt", 0) for f in h.get("flows", []))
        overall = est_netliq - start - flows
        overall_pct = overall / start * 100
    except Exception as e:
        problems.append("netliq_history: %s" % e)

    now = datetime.datetime.now(datetime.timezone.utc)
    head = ("\U0001F504 UPDATE — %s UTC" % now.strftime("%d %b %H:%M")) if on_demand \
        else ("\U0001F4CB ACTION LIST — %s" % now.strftime("%a %d %b %Y"))
    L = ["<b>%s</b>" % head,
         "<i>Manual mode — IB gateway down. Your strategy's own rules, replayed offline.</i>", ""]

    L.append("<b>\U0001F4B0 NET WORTH</b>")
    L.append("Est. NetLiq  <b>HK$%s</b>" % money(est_netliq))
    L.append("  positions HK$%s · cash HK$%s" % (money(mv_hkd), money(cash_hkd)))
    if day_pnl is not None:
        L.append("Since last digest  <b>%s</b> HKD" % _sign(day_pnl))
    if overall is not None:
        L.append("Since launch  <b>%s</b> HKD (%+.2f%%)  <i>flows excluded</i>"
                 % (_sign(overall), overall_pct))
    L.append("Unrealised  %s HKD on HK$%s cost" % (_sign(mv_hkd - cost_hkd), money(cost_hkd)))
    if newest_ts:
        stamp = datetime.datetime.fromtimestamp(max(newest_ts), datetime.timezone.utc)
        L.append("<i>marks as of %s UTC</i>" % stamp.strftime("%d %b %H:%M"))
    L.append("")

    L.append("<b>\U0001F534 SELL</b>")
    if exits:
        for e in exits:
            L.append("<code>SELL %s %g @ MKT</code>" % (e["ysym"], e["qty"]))
            L.append("   %s" % e["reason"])
    else:
        L.append("   none")
    L.append("")

    L.append("<b>\U0001F7E2 BUY</b>")
    if buys:
        for b in buys:
            L.append("<code>BUY %s %d @ LMT %s</code>" % (b["ysym"], b["shares"], b["limit"]))
            L.append("   score %s · ~HK$%s" % (b["score"], money(b["hkd"])))
    else:
        L.append("   none" + ("  <i>(kill switch active)</i>" if killed else
                              ("  <i>(no free slot — %d/%d held)</i>"
                               % (len(positions), TARGET_POSITIONS) if free <= 0 else "")))
    L.append("")

    L.append("<b>\U0001F4CA POSITIONS (%d)</b>" % len(holds + exits))
    for h in sorted(holds + exits, key=lambda x: (x["head"] is None, x["head"])):
        hd = ("%+.1f%%" % h["head"]) if h["head"] is not None else "n/a"
        L.append("<code>%-7s %8.2f %7s %+6.1f%%</code>%s"
                 % (h["ysym"], h["price"], hd, h["upl_pct"], " ⚠️" if h["reason"] else ""))
    L.append("<i>price · headroom above stop · unrealised</i>")
    L.append("")

    L.append("<b>ℹ️ Notes</b>")
    if manual:
        L.append("• Book: <b>operator-confirmed</b> <code>%s</code>" % bs.get("updated", "?"))
    else:
        L.append("• Quantities from bot_state <code>%s</code> — later fills NOT visible."
                 % bs.get("updated", "?"))
    L.append("• Kill switch: %s (%s vs %s)"
             % ("<b>ACTIVE</b>" if killed else "clear", money(est_netliq), money(peak * 0.92)))
    L.append("• Exits are market-at-next-open, not at the stop price.")
    L.append("• Prices are LIVE marks; exit rules evaluate on the CLOSE, as the bot does.")
    L.append("• Cash is operator-supplied, not read from IB — NetLiq is an estimate.")
    if problems:
        L.append("")
        L.append("<b>⚠️ Problems</b>")
        for p in problems[:8]:
            L.append("• %s" % p)

    snap = {"date": datetime.date.today().isoformat(), "est_netliq": est_netliq,
            "mv_hkd": mv_hkd, "cost_hkd": cost_hkd}
    return "\n".join(L), snap


def save_snapshot(snap):
    tmp = PREV + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(snap, f)
    os.replace(tmp, PREV)


def main():
    try:
        cfg = load_cfg()
    except Exception as e:
        log("FATAL: %s" % e)
        return 2
    try:
        msg, snap = build_report(on_demand=False)
    except Exception as e:
        log("build_report failed: %r" % e)
        return 1
    try:
        mid = send_message(cfg["TELEGRAM_TOKEN"], cfg["TELEGRAM_CHAT_ID"], msg)
        log("sent message_id %s" % mid)
    except Exception as e:
        log("telegram send failed: %s" % e)
        return 1
    save_snapshot(snap)     # only the scheduled run moves the P&L baseline
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        log("UNHANDLED: %r" % e)
        sys.exit(1)
