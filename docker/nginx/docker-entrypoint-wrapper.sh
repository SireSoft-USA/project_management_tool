#!/bin/sh
# The stock nginx image only runs its templating/config-generation scripts
# (/docker-entrypoint.d/*, including envsubst on our app.conf.template) when
# its OWN entrypoint sees "nginx" as $1. Overriding `command:` with a plain
# shell one-liner (to add a periodic reload loop for cert renewal) skips
# that step entirely, leaving only the image's built-in default.conf
# ("Welcome to nginx!") active. This wrapper keeps the reload loop but hands
# off to the real entrypoint with "nginx" as $1 so templating still runs.
set -e

(
  trap exit TERM
  while :; do
    sleep 12h &
    wait $!
    nginx -s reload
  done
) &

exec /docker-entrypoint.sh "$@"
