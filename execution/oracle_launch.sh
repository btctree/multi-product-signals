#!/usr/bin/env bash
# ============================================================================
# One-shot launcher for the Multi-Product bot VM on Oracle Cloud (Always Free).
# RUN THIS INSIDE ORACLE CLOUD SHELL (the >_ terminal in the console top bar) —
# it already has the `oci` CLI authenticated as you, plus `jq` and `base64`.
#
#   Phase 1 (safe, no creds): just run it to VALIDATE the environment:
#       bash oracle_launch.sh
#     -> finds your availability domain, Oracle Linux 9 image, and public subnet,
#        prints them, and STOPS before launching (no IB login needed).
#
#   Phase 2 (actually launch): supply your IB login, then run it again:
#       export IB_USER='your_ib_username'
#       export IB_PASS='your_ib_password'
#       bash oracle_launch.sh
#     -> launches the VM; on boot it auto-installs Java + IB Gateway + IBC + the
#        bot and starts Gateway. Approve the 2FA push on IBKR Mobile once.
#
# Your IB password is NEVER in this repo; you type it into Cloud Shell yourself.
# It rides in the instance's cloud-init metadata (your VM only) so Gateway can
# auto-login — that is unavoidable for unattended running.
# ============================================================================
set -euo pipefail

NAME="multi-product-bot"
SHAPE="VM.Standard.E2.1.Micro"          # Always-Free x86 micro (1 GB; swap added)
SETUP_URL="https://raw.githubusercontent.com/btctree/multi-product-signals/main/execution/setup_vm.sh"

# Compartment: default to the tenancy root (Cloud Shell exports OCI_TENANCY).
COMPARTMENT="${OCI_COMPARTMENT:-${OCI_TENANCY:?Not in Cloud Shell — OCI_TENANCY unset}}"

echo "==> 1/4 availability domain"
AD=$(oci iam availability-domain list --compartment-id "$COMPARTMENT" \
       --output json | jq -r '.data[0].name')
[ -n "$AD" ] && echo "    AD      = $AD" || { echo "!! no availability domain"; exit 1; }

echo "==> 2/4 Oracle Linux 9 image for $SHAPE"
IMAGE=$(oci compute image list --compartment-id "$COMPARTMENT" \
       --operating-system "Oracle Linux" --operating-system-version "9" \
       --shape "$SHAPE" --sort-by TIMECREATED --sort-order DESC \
       --output json | jq -r '.data[0].id // empty')
[ -n "$IMAGE" ] && echo "    image   = $IMAGE" || { echo "!! no OL9 image for $SHAPE"; exit 1; }

echo "==> 3/4 public subnet (reusing your existing VCN)"
SUBNET=$(oci network subnet list --compartment-id "$COMPARTMENT" --all \
       --output json | jq -r '[.data[] | select(.["prohibit-public-ip-on-vnic"]==false)][0].id // empty')
if [ -z "$SUBNET" ]; then
  echo "!! No public subnet found in this compartment."
  echo "   If your VCN lives in a sub-compartment, re-run with:"
  echo "     export OCI_COMPARTMENT='<that-compartment-ocid>'"
  exit 1
fi
echo "    subnet  = $SUBNET"

echo "==> 4/4 ssh key (generated in Cloud Shell; you never need to touch it)"
[ -f ~/mp_vm_key ] || ssh-keygen -t rsa -b 2048 -f ~/mp_vm_key -N "" -q
PUBKEY=$(cat ~/mp_vm_key.pub)

# ---- guard: no creds -> validation only, stop here -------------------------
if [ -z "${IB_USER:-}" ] || [ -z "${IB_PASS:-}" ]; then
  echo
  echo "============================================================"
  echo " Environment looks good. To actually launch, run:"
  echo "     export IB_USER='your_ib_username'"
  echo "     export IB_PASS='your_ib_password'"
  echo "     bash oracle_launch.sh"
  echo "============================================================"
  exit 0
fi

# ---- build cloud-init (installs everything + passes IB creds) ---------------
CLOUD_INIT=$(cat <<EOF
#!/bin/bash
export IB_USER='${IB_USER}'
export IB_PASS='${IB_PASS}'
curl -fsSL ${SETUP_URL} | bash > /var/log/mp-setup.log 2>&1
EOF
)
USERDATA=$(printf '%s' "$CLOUD_INIT" | base64 -w0)

METADATA=$(jq -n --arg k "$PUBKEY" --arg u "$USERDATA" \
             '{ssh_authorized_keys:$k, user_data:$u}')

echo "==> launching $NAME (VM.Standard.E2.1.Micro, $AD) ..."
oci compute instance launch \
  --availability-domain "$AD" \
  --compartment-id "$COMPARTMENT" \
  --shape "$SHAPE" \
  --display-name "$NAME" \
  --image-id "$IMAGE" \
  --subnet-id "$SUBNET" \
  --assign-public-ip true \
  --metadata "$METADATA" \
  --wait-for-state RUNNING

echo
echo "============================================================"
echo " LAUNCHED. The VM is now installing IB Gateway + the bot"
echo " (takes ~5-10 min on first boot). When Gateway logs in,"
echo " APPROVE THE 2FA PUSH on IBKR Mobile."
echo " The bot is scheduled daily in DRY mode until you go live."
echo "============================================================"
