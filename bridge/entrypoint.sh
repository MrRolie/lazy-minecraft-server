#!/bin/sh
# host/setup.sh and `deploy/world.sh init` create mc_data/mcve/{in,out} as kit.toml host.user (uid 1000). If Docker
# created in/ first, it is root-owned and the bridge (uid 1000) can't append the inbox, so a ROOT-owned in/
# (only) is handed to 1000. out/ is the mod's and read-only here: never touched. Any other owner is left
# alone, and the bridge then refuses to start (not writable), so it never fights check.sh. Then run as 1000
# with an empty capability bounding set, so not even a setuid binary can get root back.
set -eu
dir="${MCVE_DIR:-/mcve}/in"
if [ "$(id -u)" = 0 ]; then
  [ "$(stat -c %u "$dir")" != 0 ] || chown 1000:1000 "$dir"
  exec setpriv --reuid=1000 --regid=1000 --clear-groups --inh-caps=-all --bounding-set=-all python /app/bridge.py
fi
exec python /app/bridge.py
