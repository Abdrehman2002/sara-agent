#!/bin/bash
# Run this once on a fresh Hostinger VPS (Ubuntu 22.04)
# wget -O setup-vps.sh https://raw.githubusercontent.com/munirrazaa/Voice-Bot-/main/setup-vps.sh && bash setup-vps.sh

set -e

echo "==> Installing Docker..."
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg
install -m 0755 -d /etc/apt/keyrings
curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
chmod a+r /etc/apt/keyrings/docker.gpg
echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
  | tee /etc/apt/sources.list.d/docker.list > /dev/null
apt-get update -qq
apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-compose-plugin

echo "==> Cloning agent repository..."
mkdir -p ~/vextria && cd ~/vextria
git clone https://github.com/munirrazaa/Voice-Bot-.git

echo "==> Creating env file..."
cp ~/vextria/Voice-Bot-/.env.prod.example ~/vextria/.env.prod
echo ""
echo "================================================================"
echo " NEXT STEP: Edit ~/vextria/.env.prod with your actual API keys"
echo "   nano ~/vextria/.env.prod"
echo ""
echo " Then deploy:"
echo "   cd ~/vextria/Voice-Bot-"
echo "   docker compose -f docker-compose.prod.yml --env-file ../.env.prod up -d --build"
echo "================================================================"
