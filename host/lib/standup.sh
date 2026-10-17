# shellcheck shell=bash
# The stand-up gate (docs/kit/KIT.md P10), sourced by host/setup.sh and tested by host/test_host.py. Read-only.

# standup_refusal REPO: prints why mc-server.service + mc-backup.timer must not be armed yet, or nothing.
# Armed, mc-backup.timer fails nightly without a world, and mc-server.service starts a server whose pack or world
# is missing at every boot. The first deploy/backup.sh (KIT.md P9) is the proof that all three exist together, the
# same files backup.sh itself reads: LEVEL from .env (default world) and the .mrpack compose names.
standup_refusal() {
  local repo=$1 level pack="" f
  level="$(sed -n 's/^LEVEL=//p' "${repo}/.env" 2>/dev/null | tail -1)"
  level="${level:-world}"
  # A custom pack's docker-compose.override.yml first, as compose (and deploy/backup.sh) reads it.
  for f in "${repo}/docker-compose.override.yml" "${repo}/docker-compose.yml"; do
    if [ -z "$pack" ] && [ -f "$f" ]; then
      pack="$(sed -n 's|.*MODRINTH_MODPACK: *"/pack/\([^"]*\)".*|\1|p' "$f" | head -1)"
    fi
  done
  if [ ! -f "${repo}/mc_data/${level}/level.dat" ]; then
    echo "no world at mc_data/${level}/level.dat yet (KIT.md P8-P9: docker compose up -d minecraft, deploy/world.sh init)"
  elif [ -z "$pack" ] || [ ! -f "${repo}/dist/${pack}" ]; then
    echo "the pack compose names (dist/${pack:-?}) is missing (python3 pack/build.py)"
  elif ! compgen -G "${repo}/backups/world-*.tar.gz" >/dev/null; then
    echo "no backups/world-*.tar.gz yet: run the first deploy/backup.sh (KIT.md P9) before arming the nightly one"
  fi
}
