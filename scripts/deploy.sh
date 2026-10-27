#!/bin/sh
# Deploy to the Lightsail server (`Host lifeos` in ~/.ssh/config).
#   scripts/deploy.sh          code only
#   scripts/deploy.sh --env    also copy .env
set -e
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
.venv/bin/python -m pytest -q tests
rsync -az --delete \
  --exclude .venv/ --exclude .dev/ --exclude desktop/ --exclude .env --exclude crew/ --exclude .pytest_cache/ \
  --exclude __pycache__/ --exclude .DS_Store --exclude '*.pem' \
  ./ lifeos:/srv/life-os/
if [ "$1" = "--env" ]; then
  scp -q .env lifeos:/srv/life-os/.env && ssh lifeos chmod 600 /srv/life-os/.env
fi
ssh lifeos 'cd /srv/life-os && sudo docker compose up -d --build --remove-orphans 2>&1 | tail -2 \
  && for i in $(seq 30); do curl -sf localhost:8000/healthz && echo && exit 0; sleep 1; done; \
  sudo docker compose logs --tail 30; exit 1'
