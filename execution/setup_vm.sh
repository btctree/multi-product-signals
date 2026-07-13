#!/usr/bin/env bash
# Oracle Cloud VM setup for the IB execution bot (Ubuntu ARM/x86).
# Installs: Java, Xvfb (virtual display), IB Gateway, IBC (auto-restart), the bot.
# YOU still do two things this script cannot: (1) put your IB login in config.ini,
# (2) approve the 2FA on IBKR Mobile. Run this in Oracle Cloud Shell / SSH:
#     bash setup_vm.sh
# Versions/URLs drift — if a download 404s, grab the current link from the notes.
set -e
echo "==> 0/6 swap (1GB free shape is tight for IB Gateway)"
if ! sudo swapon --show | grep -q swap; then
  sudo fallocate -l 2G /swapfile && sudo chmod 600 /swapfile
  sudo mkswap /swapfile && sudo swapon /swapfile
  echo '/swapfile none swap sw 0 0' | sudo tee -a /etc/fstab
fi

echo "==> 1/6 system packages (auto-detects apt / dnf)"
if command -v apt-get >/dev/null; then          # Ubuntu/Debian
  sudo apt-get update -y
  sudo apt-get install -y openjdk-17-jre xvfb unzip wget curl python3-pip git
else                                            # Oracle Linux / RHEL family
  sudo dnf install -y java-17-openjdk unzip wget curl python3-pip git \
    xorg-x11-server-Xvfb || sudo yum install -y java-17-openjdk unzip wget \
    curl python3-pip git xorg-x11-server-Xvfb
fi

echo "==> 2/6 IB Gateway (stable standalone)"
cd ~
# stable standalone installer (Linux x64). ARM VMs run it fine under x86 Java on
# Ampere? No — on ARM use the x64 build only if emulated; simplest is an x86 VM.
wget -O ibgw.sh "https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh"
chmod +x ibgw.sh
# unattended install to ~/Jts
yes "" | ./ibgw.sh -q -dir "$HOME/Jts" || ./ibgw.sh   # falls back to interactive

echo "==> 3/6 IBC (IbcAlpha) — auto-login + daily restart"
IBC_VER="3.20.0"   # check https://github.com/IbcAlpha/IBC/releases for the latest
wget -O ibc.zip "https://github.com/IbcAlpha/IBC/releases/download/${IBC_VER}/IBCLinux-${IBC_VER}.zip"
sudo mkdir -p /opt/ibc && sudo unzip -o ibc.zip -d /opt/ibc
sudo chmod -R u+x /opt/ibc/*.sh /opt/ibc/scripts/*.sh 2>/dev/null || true

echo "==> 4/6 IBC config template  (>>> YOU EDIT THIS FILE <<<)"
mkdir -p ~/ibc
cat > ~/ibc/config.ini <<'CFG'
# ---- EDIT THESE TWO LINES with your IB login, then save ----
IbLoginId=YOUR_IB_USERNAME
IbPassword=YOUR_IB_PASSWORD
# ------------------------------------------------------------
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

cat <<'DONE'

============================================================
 DONE with the automated parts. Now YOU do:
 1) nano ~/ibc/config.ini     # enter your IB username + password, save
 2) ~/start_gateway.sh        # starts IB Gateway; approve the 2FA on IBKR Mobile
 3) In another shell, DRY RUN the bot (places nothing):
      cd ~/multi-product-signals/execution && python3 ib_bot.py --dry
 4) When happy, go live: set TradingMode=live + OverrideTwsApiPort=4001 in
    config.ini, restart gateway, then run with IB_PORT=4001 CONFIRM_FIRST=0.
 If anything errors, copy the output and send it back for a fix.
============================================================
DONE
