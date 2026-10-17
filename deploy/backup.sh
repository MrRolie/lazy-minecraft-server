#!/usr/bin/env bash
# Daily server backup. Run as the kit user by the mc-backup.timer system unit
# (host/systemd/) at 04:00 local. Safe to run manually any time (idempotent).
#
# Strategy: if the MC container is running, wait until RCON answers (a server
# that is still booting cannot flush), then RCON `save-off` + `save-all flush`
# pin a consistent on-disk state, then tar, then `save-on`. If RCON never
# answers, or a flush command fails, the backup FAILS (exit 1) instead of
# tarring a live world. If the container is stopped, just tar (world is at
# rest, safe). The tar is written as *.partial, verified with `gzip -t`, and
# only then renamed, so an interrupted run never leaves a valid-looking
# archive. 7-day rotation.
#
# Tars the world AND the server-side state needed to restore it on a modded
# server (config, server.properties, whitelist, ops), PLUS the pack record the
# world depends on: the .mrpack compose runs (tiny; it references Modrinth
# downloads) and pack/lock.json (a custom pack: also pack-custom/'s mods.toml, lock.json and overrides/, and the
# docker-compose.override.yml that names it). Mod jars are not tarred — the .mrpack pins them;
# ve-commands (extras/, not on Modrinth) is rebuilt from git + mod/RELEASE.sha256 on restore; the tar's
# provenance/ records which build that was (docker-compose.yml with its MODS line, mod/RELEASE.sha256, the
# git commit), so a restore on a fresh checkout knows what to rebuild.
# The world's ring stamps (.ve-ring-<dim>, deploy/world.sh) are left out: Chunky's task files and logs outside
# the tar may say a later ring finished, and a restored stamp would let `world.sh done` vouch for a world that
# holds only the chunks made before the backup. A restored world fails `done` until it is rung again.
# The container is found by its compose labels and reached with plain `docker exec`: no compose parsing, so a
# malformed .env.bridge (which breaks every `docker compose exec`) can never stop the backup (rv3).
# The world directory is $LEVEL from .env (default "world"). A missing world is
# an ERROR (non-zero exit -> the systemd unit shows failed), never a silent skip.
set -euo pipefail

# The checkout this script is in (mc-backup.service runs it there, as kit.toml host.repo).
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="${REPO_DIR}/backups"
DATA_DIR="${REPO_DIR}/mc_data"
LEVEL="$(sed -n 's/^LEVEL=//p' "${REPO_DIR}/.env" 2>/dev/null | tail -1)"
LEVEL="${LEVEL:-world}"
# The .mrpack compose runs: a custom pack's docker-compose.override.yml (kit/render.py) names it, else
# docker-compose.yml (kit/kitconfig.py live_pack_file; host/test_host.py holds this copy to it).
PACK_FILE=""
for f in "${REPO_DIR}/docker-compose.override.yml" "${REPO_DIR}/docker-compose.yml"; do
  if [ -z "$PACK_FILE" ] && [ -f "$f" ]; then
    PACK_FILE="$(sed -n 's|.*MODRINTH_MODPACK: *"/pack/\([^"]*\)".*|\1|p' "$f" | head -1)"
  fi
done
MODS="$(sed -n 's|^ *MODS: *"\([^"]*\)".*|\1|p' "${REPO_DIR}/docker-compose.yml")"
PROJECT=minecraft-ai-host   # = docker-compose.yml `name:` (pinned)
RETENTION_DAYS=7
RCON_WAIT_SECS="${MC_BACKUP_RCON_WAIT:-180}"
TS="$(date +%Y%m%d-%H%M%S)"
ARCHIVE="${BACKUP_DIR}/world-${TS}.tar.gz"
PARTIAL="${ARCHIVE}.partial"

mkdir -p "$BACKUP_DIR"
cd "$REPO_DIR"
# An earlier run that was interrupted leaves only a .partial: never a backup, drop it.
find "$BACKUP_DIR" -maxdepth 1 -name 'world-*.tar.gz.partial' -delete

if [ ! -d "${DATA_DIR}/${LEVEL}" ]; then
  echo "[backup] ERROR: world dir ${DATA_DIR}/${LEVEL} (LEVEL=${LEVEL}) does not exist" >&2
  exit 1
fi
if [ -z "$PACK_FILE" ] || [ ! -f "${REPO_DIR}/dist/${PACK_FILE}" ]; then
  echo "[backup] ERROR: pack file '${PACK_FILE}' named by docker-compose(.override).yml is not in dist/" >&2
  exit 1
fi
if [ ! -f "${REPO_DIR}/mod/RELEASE.sha256" ]; then
  echo "[backup] ERROR: mod/RELEASE.sha256 missing; the backup could not say which ve-commands build it needs" >&2
  exit 1
fi

cid="$(docker ps -q --filter "label=com.docker.compose.project=${PROJECT}" \
  --filter "label=com.docker.compose.service=minecraft" --filter status=running 2>/dev/null | head -1)"
running=0
[ -n "$cid" ] && running=1

rcon() { docker exec "$cid" rcon-cli "$@"; }

PROV="$(mktemp -d)"
cleanup() {
  rm -f "$PARTIAL"
  rm -rf "$PROV"
  if [ "$running" -eq 1 ]; then rcon save-on >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

if [ "$running" -eq 1 ]; then
  waited=0
  until rcon list >/dev/null 2>&1; do
    if [ "$waited" -ge "$RCON_WAIT_SECS" ]; then
      echo "[backup] ERROR: minecraft is running but RCON did not answer within ${RCON_WAIT_SECS}s; not tarring a live world" >&2
      exit 1
    fi
    sleep 5
    waited=$((waited + 5))
  done
  echo "[backup] flushing world via RCON..."
  if ! rcon save-off >/dev/null || ! rcon save-all flush >/dev/null; then
    echo "[backup] ERROR: RCON save-off / save-all flush failed; not tarring a live world" >&2
    exit 1
  fi
  sync
fi

# Only include the extras that exist (config/ may be absent on a vanilla-ish pack).
paths=("$LEVEL")
for p in config server.properties whitelist.json ops.json; do
  [ -e "${DATA_DIR}/${p}" ] && paths+=("$p")
done

mkdir "${PROV}/provenance"
cp "${REPO_DIR}/docker-compose.yml" "${REPO_DIR}/mod/RELEASE.sha256" "${PROV}/provenance/"
# A custom pack: its override, and its source (pack-custom/ is in no git history, so the backup is its record).
record=("dist/${PACK_FILE}" pack/lock.json)
if [ -f "${REPO_DIR}/docker-compose.override.yml" ]; then
  cp "${REPO_DIR}/docker-compose.override.yml" "${PROV}/provenance/"
fi
for p in pack-custom/mods.toml pack-custom/lock.json pack-custom/overrides; do
  if [ -e "${REPO_DIR}/${p}" ]; then record+=("$p"); fi
done
{
  echo "MODS=${MODS}"
  echo "MODRINTH_MODPACK=/pack/${PACK_FILE}"
  echo "commit=$(git -C "$REPO_DIR" rev-parse HEAD 2>/dev/null || echo unknown)"
} > "${PROV}/provenance/build.txt"

echo "[backup] tar (${paths[*]}, pack record, provenance/) -> $ARCHIVE"
tar -czf "$PARTIAL" --exclude='.ve-ring-*' \
  -C "$DATA_DIR" "${paths[@]}" -C "$REPO_DIR" "${record[@]}" -C "$PROV" provenance
gzip -t "$PARTIAL"
mv "$PARTIAL" "$ARCHIVE"

# Retention
find "$BACKUP_DIR" -maxdepth 1 -name 'world-*.tar.gz' -mtime +${RETENTION_DAYS} -delete

echo "[backup] done: size=$(du -h "$ARCHIVE" | cut -f1); kept=$(find "$BACKUP_DIR" -maxdepth 1 -name 'world-*.tar.gz' | wc -l)"
