#!/usr/bin/env bash
# The spike boot (docs/kit/CUSTOM-PACK.md): boot a pack once, in a throwaway world (or a throwaway copy of a world),
# before the real server runs it. It proves the server starts with that pack, nothing more.
#
#   bash pack/spike.sh [--if-custom | --pack <file in dist/>] [--world <backup tar>]
#   (under `sg docker -c '...'` until the P10 reboot gives your shell the docker group)
#
#   --if-custom      KIT.md P5's gate, run on every kit: when kit.toml pack.mode is vanilla-extended it prints
#                    "spike: skipped" and passes (P8's first start is that pack's boot); for a custom pack it boots
#                    the pack the override names, and refuses when render has not written the override yet.
#                    Never with --pack, which names the pack itself: the two together are refused
#   --pack <file>    boot dist/<file> instead of the pack compose runs (a custom pack's new version): a spike-only
#                    override names it, so the live docker-compose.override.yml, which mc-server.service boots, is
#                    rewritten only after the spike passes
#   --world <tar>    seed the throwaway data dir from a deploy/backup.sh tar (the world, config/, server.properties):
#                    the rehearsal on a copy of the friends' world that a pack change needs (AGENTS.md chapters)
#
# Same image and environment as the real server: docker-compose.yml, plus a custom pack's
# docker-compose.override.yml, plus pack/spike.compose.yml (no published port, no restart, its own data dir under
# spike/). Its own compose project (mc-spike-<time>-<pid>), so it never touches minecraft-ai-host's container or world.
# PASS (exit 0) only when the log reaches "Done (" AND everything the spike made is gone again: its containers,
# network and volumes, and spike/<project>/. The spike world is thrown away; it never becomes the friends' world.
# It refuses while the real server is running: two servers at once can run the laptop out of memory.
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"
TIMEOUT_SECS="${MC_SPIKE_TIMEOUT:-900}"   # a first boot downloads the server and every mod (P8 allows 15 min)
# The start time, to the second, and this shell's pid: two spikes started in the same second never share a project,
# a data dir or a pack override, so one's teardown never removes the other's (codex review 2026-10-16).
PROJECT="mc-spike-$(date +%Y%m%d%H%M%S)-$$"
DATA="spike/${PROJECT}"
PACK_OVR="spike/${PROJECT}.pack.yml"      # --pack's override: beside the data dir, never inside the server's /data
export MC_SPIKE_DATA="./${DATA}"

die() { echo "spike: $*" >&2; exit 1; }

if_custom=0; want_pack=""; world_tar=""
while [ $# -gt 0 ]; do
  case "$1" in
    --if-custom) if_custom=1; shift ;;
    --pack) want_pack="${2:?usage: spike.sh --pack <file in dist/>}"; shift 2 ;;
    --world) world_tar="${2:?usage: spike.sh --world <backup tar>}"; shift 2 ;;
    *) die "usage: bash pack/spike.sh [--if-custom | --pack <file in dist/>] [--world <backup tar>]" ;;
  esac
done
# --if-custom asks kit.toml whether to boot at all; --pack names what to boot. Together, kit.toml's answer would go
# unread (a Vanilla Extended kit booting a spike instead of "skipped"), so one or the other.
if [ "$if_custom" = 1 ] && [ -n "$want_pack" ]; then
  die "--if-custom and --pack don't go together: --if-custom is P5's gate (kit.toml decides), --pack boots a named pack"
fi

if [ "$if_custom" = 1 ]; then
  mode="$(python3 pack/build.py --mode)" || die "cannot read pack.mode from kit.toml"
  if [ "$mode" != custom ]; then
    echo "spike: skipped: kit.toml pack.mode is ${mode}, not custom; P8's first start is that pack's boot"
    exit 0
  fi
  [ -f docker-compose.override.yml ] || die "kit.toml says pack.mode = custom, but docker-compose.override.yml is \
missing, so compose would run Vanilla Extended: python3 kit/render.py first"
fi

files=(-f docker-compose.yml)
if [ -f docker-compose.override.yml ]; then files+=(-f docker-compose.override.yml); fi
if [ -n "$want_pack" ]; then
  [[ "$want_pack" =~ ^[A-Za-z0-9][A-Za-z0-9._+-]*\.mrpack$ ]] || die "--pack takes a file name in dist/ (x.mrpack)"
  pack="$want_pack"
  files+=(-f "$PACK_OVR")
else
  pack="$(python3 pack/build.py --which)" || die "cannot tell which pack compose runs"
fi
files+=(-f pack/spike.compose.yml)
dc() { docker compose -p "$PROJECT" "${files[@]}" "$@"; }

[ -f "dist/${pack}" ] || die "dist/${pack} is missing: python3 pack/build.py first"
if [ -n "$world_tar" ]; then
  [ -f "$world_tar" ] || die "--world: ${world_tar} is not a file (a backups/world-<time>.tar.gz)"
fi
real="$(docker ps -q --filter "label=com.docker.compose.project=minecraft-ai-host" \
  --filter "label=com.docker.compose.service=minecraft" --filter status=running)"
[ -z "$real" ] || die "the real server is running; a spike next to it can run the laptop out of memory. Spike before \
P8, or stop it first (rcon-cli list: nobody online, and a fresh deploy/backup.sh)"

leftovers() {
  docker ps -aq --filter "label=com.docker.compose.project=${PROJECT}"
  docker network ls -q --filter "label=com.docker.compose.project=${PROJECT}"
  docker volume ls -q --filter "label=com.docker.compose.project=${PROJECT}"
  if [ -e "$DATA" ]; then echo "$DATA"; fi
  if [ -e "$PACK_OVR" ]; then echo "$PACK_OVR"; fi
}
teardown() {
  dc down --volumes --remove-orphans --timeout 0 >/dev/null 2>&1 || true   # a throwaway world: no save needed
  rm -rf "$DATA"
  rm -f "$PACK_OVR"
  rmdir spike 2>/dev/null || true
}
trap teardown EXIT

# Made here, as you: a bind source Docker creates is root's, and the teardown could not delete it.
mkdir -p "$DATA"
if [ -n "$want_pack" ]; then
  printf 'services:\n  minecraft:\n    environment:\n      MODRINTH_MODPACK: "/pack/%s"\n' "$want_pack" >"$PACK_OVR"
fi
what="a throwaway world"
if [ -n "$world_tar" ]; then
  # The world as deploy/backup.sh tarred it, without the pack record and provenance (AGENTS.md restore step 3).
  level="$( { sed -n 's/^LEVEL=//p' .env 2>/dev/null || true; } | tail -1)"
  level="${level:-world}"
  tar -xzf "$world_tar" -C "$DATA" --exclude='dist/*' --exclude='pack/*' --exclude='pack-custom/*' \
    --exclude='provenance/*' || die "--world: could not unpack ${world_tar}"
  [ -f "${DATA}/${level}/level.dat" ] || die "--world: ${world_tar} holds no ${level}/level.dat (LEVEL from .env)"
  what="a throwaway copy of the world in ${world_tar}"
fi
echo "spike: booting dist/${pack} as compose project ${PROJECT} (no published port, ${what} in ${DATA}/)"
start=$(date +%s)
booted=""
if dc up -d minecraft; then
  while [ $(( $(date +%s) - start )) -lt "$TIMEOUT_SECS" ]; do
    log="$(dc logs --no-color minecraft 2>&1 || true)"
    booted="$(grep -m1 -o 'Done ([0-9.]*s)!.*' <<<"$log" || true)"
    [ -z "$booted" ] || break
    state="$(dc ps -a --format '{{.State}}' minecraft 2>/dev/null || true)"
    if [ "$state" = exited ] || [ "$state" = dead ]; then break; fi
    sleep 5
  done
fi
log="$(dc logs --no-color minecraft 2>&1 || true)"
mods="$(grep -m1 -o 'Loading [0-9]* mods' <<<"$log" || true)"
# Fabric's own list of what loaded ("\t- <id> <version>"): the evidence, and ve-commands must be in it when
# compose's MODS hands it over (the bridge and /whitelist from Discord are that mod).
loaded="$(grep -oE $'^[^\t]*\t- [a-z0-9_.-]+ [^ ]+$' <<<"$log" | sed -E $'s/^[^\t]*\t- //' | sort -u || true)"
if [ -n "$loaded" ]; then echo "spike: Fabric loaded: $(tr '\n' ' ' <<<"$loaded")"; fi
ve=""
if grep -q 'MODS: *"/extras/ve-commands' docker-compose.yml && ! grep -q '^ve-commands ' <<<"$loaded"; then
  ve="ve-commands is not in Fabric's list of loaded mods"
fi
if [ -z "$booted" ]; then
  echo "spike: the server did not reach \"Done (\" (its last lines):" >&2
  tail -n 40 <<<"$log" >&2
fi

teardown
left="$(leftovers)"
if [ -n "$left" ]; then
  echo "spike: FAIL: the spike left these behind (remove them by hand): ${left//$'\n'/ }" >&2
  exit 1
fi
[ -n "$booted" ] || die "FAIL: dist/${pack} did not boot"
[ -z "$ve" ] || die "FAIL: dist/${pack} booted, but ${ve}"
echo "spike: PASS: dist/${pack} reached \"${booted}\" (${mods:-mod count not in the log}) in $(( $(date +%s) - start ))s; torn down, nothing left (project ${PROJECT}, ${what})"
