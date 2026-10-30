#!/usr/bin/env bash
# Prépare un dossier prêt à pousser vers un Space Hugging Face de type Docker.
# Usage : bash deploy/huggingface/prepare_space.sh <utilisateur_hf> <nom_du_space>
set -euo pipefail
USER_HF=${1:?utilisateur Hugging Face}
SPACE=${2:-isotestforge}
ROOT=$(cd "$(dirname "$0")/../.." && pwd)
DEST=$(mktemp -d)/$SPACE
git clone "https://huggingface.co/spaces/$USER_HF/$SPACE" "$DEST"
rsync -a --exclude .git --exclude 'data/workflows' --exclude 'data/itf.sqlite' --exclude '__pycache__' \
      --exclude 'examples/generated' "$ROOT"/ "$DEST"/
# Un Space Docker sert le port déclaré par app_port
{ cat <<'HDR'
---
title: ISO-TestForge
emoji: 💳
colorFrom: blue
colorTo: gray
sdk: docker
app_port: 8080
pinned: false
---

HDR
cat "$ROOT/README.md"; } > "$DEST/README.md"
cd "$DEST"
git add -A && git commit -m "Déploiement ISO-TestForge" && git push
echo "Space publié : https://huggingface.co/spaces/$USER_HF/$SPACE"
