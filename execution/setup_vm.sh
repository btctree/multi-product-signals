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
# X client libs: Gateway ships its own FULL (non-headless) JRE whose AWT dlopens
# libXext/libXrender/libXtst at runtime — without them it crashes in Toolkit.<clinit>
if command -v apt-get >/dev/null; then          # Ubuntu/Debian
  sudo apt-get update -y
  sudo apt-get install -y --no-install-recommends \
    openjdk-17-jre-headless xvfb unzip wget curl python3-pip git \
    libxext6 libxrender1 libxtst6 libxi6 libgtk-3-0
else                                            # Oracle Linux / RHEL family
  sudo dnf install -y --setopt=install_weak_deps=False java-17-openjdk-headless \
    unzip wget curl python3-pip git xorg-x11-server-Xvfb \
    libX11 libXext libXrender libXtst libXi libXrandr gtk3 alsa-lib || \
  sudo yum install -y java-17-openjdk-headless unzip wget curl python3-pip git \
    xorg-x11-server-Xvfb libX11 libXext libXrender libXtst libXi libXrandr gtk3 alsa-lib
fi

echo "==> 2/6 IB Gateway (stable standalone; skipped if already installed)"
cd ~
if [ ! -e "$HOME/Jts/ibgateway" ] && [ ! -f "$HOME/Jts/ibgateway.vmoptions" ]; then
  # stable standalone installer (Linux x64) — x86 VMs only (bundled x64 JRE).
  wget -O ibgw.sh "https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh"
  chmod +x ibgw.sh
  # unattended install to ~/Jts
  yes "" | ./ibgw.sh -q -dir "$HOME/Jts" || ./ibgw.sh   # falls back to interactive
fi
# IBC expects the layout $TWS_PATH/ibgateway/<version>/. The standalone installer
# with -dir ~/Jts installs FLAT into ~/Jts — bridge via a separate tree so we
# never collide with the ~/Jts/ibgateway launcher binary. Version is detected
# from the .desktop file the installer drops (e.g. "IB Gateway 10.45.desktop").
GW_VER=$(ls "$HOME/Jts" 2>/dev/null | sed -n 's/^IB Gateway \([0-9]*\)\.\([0-9]*\)\.desktop$/\1\2/p' | head -1)
GW_VER="${GW_VER:-1030}"
TWS_PATH_VAL="$HOME/Jts"
if [ -f "$HOME/Jts/ibgateway.vmoptions" ] && [ ! -d "$HOME/Jts/ibgateway/$GW_VER" ]; then
  mkdir -p "$HOME/ibc-tws/ibgateway"
  ln -sfn "$HOME/Jts" "$HOME/ibc-tws/ibgateway/$GW_VER"
  TWS_PATH_VAL="$HOME/ibc-tws"
  echo "    layout bridged: ~/ibc-tws/ibgateway/$GW_VER -> ~/Jts"
fi
# cap the Gateway JVM heap so it fits a ~500MB-RAM shape (default -Xmx768m thrashes)
for f in "$HOME/Jts/ibgateway.vmoptions" "$HOME"/Jts/ibgateway/*/ibgateway.vmoptions; do
  [ -f "$f" ] && sed -i 's/^-Xmx.*/-Xmx512m/' "$f" && echo "    heap capped: $f"
done

echo "==> 3/6 IBC (IbcAlpha) — auto-login + daily restart (skipped if present)"
if [ ! -f /opt/ibc/version ]; then
  IBC_VER="3.20.0"   # check https://github.com/IbcAlpha/IBC/releases for the latest
  wget -O ibc.zip "https://github.com/IbcAlpha/IBC/releases/download/${IBC_VER}/IBCLinux-${IBC_VER}.zip"
  sudo mkdir -p /opt/ibc && sudo unzip -o ibc.zip -d /opt/ibc
  sudo chmod -R u+x /opt/ibc/*.sh /opt/ibc/scripts/*.sh 2>/dev/null || true
fi

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
TWS_MAJOR_VRSN=__GWVER__
IBC_INI="$HOME/ibc/config.ini"
TWS_PATH="__TWSPATH__"
IBC_PATH="/opt/ibc"
# clean slate: kill any prior IBC/Gateway java + X, clear stale X locks
pkill -f ibcalpha 2>/dev/null
pkill Xvfb 2>/dev/null
sleep 2
rm -f /tmp/.X1-lock /tmp/.X11-unix/X1
# -ac: no access control (loopback-only VM); -nolisten tcp: unix socket only
for i in 1 2 3; do
  Xvfb :1 -screen 0 1024x768x16 -ac -nolisten tcp &
  sleep 4
  [ -S /tmp/.X11-unix/X1 ] && break
  echo "Xvfb attempt $i failed, retrying"
done
[ -S /tmp/.X11-unix/X1 ] || { echo "FATAL: Xvfb would not start"; exit 1; }
# call IBC's worker directly — the gatewaystart.sh wrapper needs xterm (desktop)
exec "$IBC_PATH/scripts/ibcstart.sh" "$TWS_MAJOR_VRSN" --gateway \
  "--tws-path=$TWS_PATH" "--ibc-path=$IBC_PATH" "--ibc-ini=$IBC_INI"
RUN
sed -i "s|__TWSPATH__|$TWS_PATH_VAL|; s|__GWVER__|$GW_VER|" ~/start_gateway.sh
chmod +x ~/start_gateway.sh

echo "==> 6/6 the bot"
[ -d ~/multi-product-signals ] || git clone https://github.com/btctree/multi-product-signals.git ~/multi-product-signals
# ib_async needs Python >= 3.10; OL9's default python3 is 3.9 -> install 3.11
PY=python3
if ! $PY -c 'import sys; assert sys.version_info >= (3,10)' 2>/dev/null; then
  sudo dnf install -y python3.11 python3.11-pip 2>/dev/null || \
    sudo apt-get install -y python3.11 python3-pip 2>/dev/null || true
  command -v python3.11 >/dev/null && PY=python3.11
fi
$PY -m pip install -r ~/multi-product-signals/execution/requirements.txt

# ---- cloud-init auto-start: if IB creds were provided, launch Gateway now ----
if [ -n "${IB_USER:-}" ] && [ "${IB_USER}" != "YOUR_IB_USERNAME" ]; then
  echo "==> creds provided -> starting IB Gateway (approve the 2FA on your phone)"
  # healthy = API port actually listening; otherwise (re)start
  ss -tln | grep -qE ":400[12] " || nohup ~/start_gateway.sh >> ~/gateway.log 2>&1 &
  # daily bot run at 00:35 UTC in DRY mode first (safe); switch to live later
  ( crontab -l 2>/dev/null | grep -v ib_bot.py; \
    echo "35 0 * * * cd ~/multi-product-signals/execution && $PY ib_bot.py --dry >> ~/bot.log 2>&1" ) | crontab -
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
