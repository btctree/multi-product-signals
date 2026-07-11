"""Telegram push of today's top signals (daily CI run only).

Runs only when the repo secrets TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are set
(Settings -> Secrets and variables -> Actions). Silent no-op otherwise, so the
pipeline never fails because alerts are unconfigured.
"""
import json
import os
import urllib.parse
import urllib.request

from config import ROOT

TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
CHAT = os.environ.get("TELEGRAM_CHAT_ID", "").strip()


def send(text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    data = urllib.parse.urlencode({"chat_id": CHAT, "text": text,
                                   "disable_web_page_preview": "true"}).encode()
    with urllib.request.urlopen(urllib.request.Request(url, data=data),
                                timeout=20) as r:
        return r.status == 200


def main():
    if not TOKEN or not CHAT:
        print("telegram: secrets not set - skipping (in-app dashboard still updates)")
        return
    d = json.loads((ROOT / "docs" / "data.json").read_text())
    a = d.get("actions", [])
    lines = [f"📊 Signals {d.get('generated')} — {len(a)} BUY"
             f" ({d.get('universe_count')} products monitored)"]
    for c in a[:8]:
        lines.append(f"• {c['symbol']} {c.get('name','')[:22]} — score {c.get('score')}"
                     f" | in {c.get('entry')} tgt {c.get('target')} stop {c.get('stop')}")
    if not a:
        lines.append("No qualifying signals today — standing aside.")
    lines.append("Open positions: check the app for exit alerts.")
    lines.append("https://btctree.github.io/multi-product-signals/")
    ok = send("\n".join(lines))
    print(f"telegram: {'sent' if ok else 'FAILED'}")


if __name__ == "__main__":
    main()
