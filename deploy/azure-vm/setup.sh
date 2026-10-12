#!/usr/bin/env bash
# One-time setup on an Ubuntu 22.04/24.04 Azure VM. Run from the repo root:  sudo bash deploy/azure-vm/setup.sh <domain>
set -euo pipefail
DOMAIN="${1:-}"
APP_DIR="$(pwd)"
APP_USER="${SUDO_USER:-azureuser}"

apt-get update
apt-get install -y python3-venv python3-pip nginx curl
curl -fsSL https://deb.nodesource.com/setup_22.x | bash - && apt-get install -y nodejs

sudo -u "$APP_USER" python3 -m venv "$APP_DIR/.venv"
sudo -u "$APP_USER" "$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
(cd "$APP_DIR/frontend" && sudo -u "$APP_USER" npm ci && sudo -u "$APP_USER" npm run build)
[ -f "$APP_DIR/.env" ] || sudo -u "$APP_USER" cp "$APP_DIR/.env.example" "$APP_DIR/.env"

for svc in backend gateway runtime; do
  sed -e "s#__APP_DIR__#$APP_DIR#g" -e "s#__APP_USER__#$APP_USER#g" \
      "$APP_DIR/deploy/azure-vm/callwright-$svc.service" > "/etc/systemd/system/callwright-$svc.service"
done
systemctl daemon-reload
systemctl enable --now callwright-backend callwright-gateway callwright-runtime

sed "s#__DOMAIN__#${DOMAIN:-_}#g" "$APP_DIR/deploy/azure-vm/nginx.conf" > /etc/nginx/sites-available/callwright
ln -sf /etc/nginx/sites-available/callwright /etc/nginx/sites-enabled/callwright
rm -f /etc/nginx/sites-enabled/default
nginx -t && systemctl reload nginx

if [ -n "$DOMAIN" ]; then
  apt-get install -y certbot python3-certbot-nginx
  certbot --nginx -d "$DOMAIN" --non-interactive --agree-tos -m "admin@$DOMAIN" --redirect
fi
echo "Done. Edit $APP_DIR/.env, then: sudo systemctl restart callwright-runtime"
