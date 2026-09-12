#!/bin/bash
# One-off bootstrap for the first Let's Encrypt certificate.
#
# nginx refuses to start with a server block pointing at a certificate that
# doesn't exist yet, and certbot needs nginx serving the ACME HTTP-01
# challenge to issue one — so this script breaks the chicken-and-egg problem
# by starting nginx with a throwaway self-signed cert, requesting the real
# one, then reloading nginx with it in place.
#
# Usage: ./docker/nginx/init-letsencrypt.sh
# Run once per environment, from the repo root, after `just prod-up`'s domain
# and email are set in .env. Safe to re-run; it skips issuance if a
# certificate for $DOMAIN already exists.

set -euo pipefail

if [ ! -f .env ]; then
    echo "❌ .env not found. Copy .env.example to .env and set DOMAIN/CERTBOT_EMAIL first."
    exit 1
fi

# shellcheck disable=SC1091
source .env

: "${DOMAIN:?Set DOMAIN in .env (e.g. DOMAIN=app.example.com)}"
: "${CERTBOT_EMAIL:?Set CERTBOT_EMAIL in .env (e.g. CERTBOT_EMAIL=admin@example.com)}"

COMPOSE="docker compose -f docker-compose.prod.yml"
RSA_KEY_SIZE=4096
DUMMY_PATH="/etc/letsencrypt/live/$DOMAIN"

if $COMPOSE run --rm certbot certificates 2>/dev/null | grep -q "Domains: $DOMAIN"; then
    echo "✅ Certificate for $DOMAIN already exists. Nothing to do."
    exit 0
fi

echo "📄 Creating a temporary self-signed certificate for $DOMAIN..."
$COMPOSE run --rm --entrypoint "\
  mkdir -p '$DUMMY_PATH' && \
  openssl req -x509 -nodes -newkey rsa:$RSA_KEY_SIZE -days 1 \
    -keyout '$DUMMY_PATH/privkey.pem' \
    -out '$DUMMY_PATH/fullchain.pem' \
    -subj '/CN=localhost'" certbot

echo "🐳 Starting nginx..."
$COMPOSE up -d nginx

echo "🗑️  Deleting the temporary certificate..."
$COMPOSE run --rm --entrypoint "rm -rf /etc/letsencrypt/live/$DOMAIN /etc/letsencrypt/archive/$DOMAIN /etc/letsencrypt/renewal/$DOMAIN.conf" certbot

echo "🔐 Requesting the real Let's Encrypt certificate..."
$COMPOSE run --rm --entrypoint "\
  certbot certonly --webroot -w /var/www/certbot \
    -d '$DOMAIN' \
    --email '$CERTBOT_EMAIL' \
    --rsa-key-size $RSA_KEY_SIZE \
    --agree-tos \
    --no-eff-email" certbot

echo "🔄 Reloading nginx with the real certificate..."
$COMPOSE exec nginx nginx -s reload

echo "✅ Done. $DOMAIN is now serving HTTPS with a Let's Encrypt certificate."
