#!/usr/bin/env bash
# Installation sur une VM Ubuntu (Oracle Cloud Always Free, Google Cloud e2-micro, etc.).
# Usage : ITF_BASIC_AUTH='testeur:MotDePasseLong' bash deploy/vm/install.sh
set -euo pipefail
: "${ITF_BASIC_AUTH:?Définir ITF_BASIC_AUTH=utilisateur:motdepasse avant de lancer le script}"
if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
fi
# Ouverture des ports dans le pare-feu de l'OS (images Ubuntu d'Oracle Cloud : iptables restrictif)
for p in 8080 8583; do
  sudo iptables -C INPUT -p tcp --dport $p -j ACCEPT 2>/dev/null || sudo iptables -I INPUT 6 -p tcp --dport $p -j ACCEPT
done
command -v netfilter-persistent >/dev/null && sudo netfilter-persistent save || true
cd "$(dirname "$0")/../.."
export ITF_BASIC_AUTH
sudo -E docker compose up -d --build
IP=$(curl -fsS https://ifconfig.me || hostname -I | awk '{print $1}')
echo "Interface : http://$IP:8080   (identifiants : $ITF_BASIC_AUTH)"
echo "Simulateur ISO 8583 pour JMeter : $IP:8583"
echo "Penser à ouvrir aussi 8080 et 8583 dans la Security List / le pare-feu du fournisseur cloud."
