#!/usr/bin/env bash
# Oracle Cloud VM setup for the IB execution bot (Ubuntu ARM/x86).
# Installs: Java, Xvfb (virtual display), IB Gateway, IBC (auto-restart), the bot.
# YOU still do two things this script cannot: (1) put your IB login in config.ini,
# (2) approve the 2FA on IBKR Mobile. Run this in Oracle Cloud Shell / SSH:
#     bash setup_vm.sh
# Versions/URLs drift — if a download 404s, grab the current link from the notes.
set -e
echo "==> 0/6 memory hardening (the free 1GB shape presents only ~500MB usable)"
MEM_MB=$(free -m | awk '/^Mem:/{print $2}')
if [ "$MEM_MB" -lt 1500 ]; then
  # reclaim the crash-dump RAM reservation (~190MB back after next reboot)
  sudo systemctl disable --now kdump 2>/dev/null || true
  sudo grubby --update-kernel=ALL --args="crashkernel=no" 2>/dev/null || true
  # the Oracle monitoring agent eats ~150MB — not needed for a cron bot
  sudo systemctl disable --now oracle-cloud-agent oracle-cloud-agent-updater 2>/dev/null || true
fi
# real swap >= 4G (OL9 ships a /.swapfile of only ~0.5G, which is why the naive
# "swap exists" check used to skip this — check SIZE, not existence)
SWAP_MB=$(free -m | awk '/^Swap:/{print $2}')
if [ "${SWAP_MB:-0}" -lt 3500 ]; then
  sudo fallocate -l 4G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

echo "==> 1/6 system packages (auto-detects apt / dnf; headless java, no extras)"
if command -v apt-get >/dev/null; then          # Ubuntu/Debian
  sudo apt-get update -y
  sudo apt-get install -y --no-install-recommends \
    openjdk-17-jre-headless xvfb unzip wget curl python3-pip git
else                                            # Oracle Linux / RHEL family
  sudo dnf install -y --setopt=install_weak_deps=False java-17-openjdk-headless \
    unzip wget curl python3-pip git xorg-x11-server-Xvfb || \
  sudo yum install -y java-17-openjdk-headless unzip wget curl python3-pip git \
    xorg-x11-server-Xvfb
fi

echo "==> 2/6 IB Gateway (stable standalone)"
cd ~
# stable standalone installer (Linux x64). ARM VMs run it fine under x86 Java on
# Ampere? No — on ARM use the x64 build only if emulated; simplest is an x86 VM.
wget -O ibgw.sh "https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh"
chmod +x ibgw.sh
# unattended install to ~/Jts
yes "" | ./ibgw.sh -q -dir "$HOME/Jts" || ./ibgw.sh   # falls back to interactive
# cap the Gateway JVM heap so it fits a ~500MB-RAM shape (default -Xmx768m thrashes)
for f in "$HOME"/Jts/ibgateway/*/ibgateway.vmoptions; do
  [ -f "$f" ] && sed -i 's/^-Xmx.*/-Xmx512m/' "$f" && echo "    heap capped: $f"
done

echo "==> 3/6 IBC (IbcAlpha) — auto-login + daily restart"
IBC_VER="3.20.0"   # check https://github.com/IbcAlpha/IBC/releases for the latest
wget -O ibc.zip "https://github.com/IbcAlpha/IBC/releases/download/${IBC_VER}/IBCLinux-${IBC_VER}.zip"
sudo mkdir -p /opt/ibc && sudo unzip -o ibc.zip -d /opt/ibc
sudo chmod -R u+x /opt/ibc/*.sh /opt/ibc/scripts/*.sh 2>/dev/null || true

echo "==> 4/6 IBC config template  (>>> YOU EDIT THIS FILE <<<)"
mkdir -p ~/ibc
cat > ~/ibc/config.ini <<CFG
IbLoginId=${IB_USER:-YOUR_IB_USERNAME}
IbPassword=${IB_PASS:-YOUR_IB_PASSWORD}
TradingMode=paper          # paper first; change to 'live' when ready
IbDir=
OverrideTwsApiPort=4002     # 4002 paper / 4001 live  (match TradingMode)
AcceptIncomingConnectionAction=accept
ReadOnlyApi=no
AcceptNonBrokerageAccountWarning=yes
IbAutoClosedown=no
ClosedownAt=
CFG
chmod 600 ~/ibc/config.ini
echo "    -> edit ~/ibc/config.ini  (nano ~/ibc/config.ini)"

echo "==> 5/6 start script (runs Gateway under a virtual display)"
cat > ~/start_gateway.sh <<'RUN'
#!/usr/bin/env bash
export DISPLAY=:1
export TWS_MAJOR_VRSN=1030
export IBC_INI="$HOME/ibc/config.ini"
export TWS_PATH="$HOME/Jts"
export IBC_PATH="/opt/ibc"
pkill Xvfb 2>/dev/null || true
Xvfb :1 -screen 0 1024x768x16 &
sleep 3
/opt/ibc/gatewaystart.sh
RUN
chmod +x ~/start_gateway.sh

echo "==> 6/6 the bot"
[ -d ~/multi-product-signals ] || git clone https://github.com/btctree/multi-product-signals.git ~/multi-product-signals
pip3 install -r ~/multi-product-signals/execution/requirements.txt

# ---- cloud-init auto-start: if IB creds were provided, launch Gateway now ----
if [ -n "${IB_USER:-}" ] && [ "${IB_USER}" != "YOUR_IB_USERNAME" ]; then
  echo "==> creds provided -> starting IB Gateway (approve the 2FA on your phone)"
  nohup ~/start_gateway.sh >> ~/gateway.log 2>&1 &
  # daily bot run at 00:35 UTC in DRY mode first (safe); switch to live later
  ( crontab -l 2>/dev/null; echo "35 0 * * * cd ~/multi-product-signals/execution && python3 ib_bot.py --dry >> ~/bot.log 2>&1" ) | crontab -
  echo "SETUP COMPLETE. Gateway starting; bot scheduled in DRY mode."
  echo "Verify later: tail ~/gateway.log ; tail ~/bot.log"
fi

cat <<'DONE'

============================================================
 SETUP FINISHED.
 * If you put your IB login in the setup box, Gateway is starting now —
   APPROVE THE 2FA PUSH on IBKR Mobile.
 * The bot is scheduled daily in DRY mode (places nothing) until you go live.
 To go live later (from a terminal): set TradingMode=live and
 OverrideTwsApiPort=4001 in ~/ibc/config.ini, restart ~/start_gateway.sh,
 and change the crontab bot line to: IB_PORT=4001 CONFIRM_FIRST=0 (drop --dry).
 Logs: ~/gateway.log  ~/bot.log  /var/log/cloud-init-output.log
============================================================
DONE
