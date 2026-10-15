#!/usr/bin/env bash
# World rings: the launch borders and every chapter's border push, as one tool. Runs as mm-mike in the
# repo dir on asus-node, against the running server, via in-container rcon-cli.
#
#   deploy/world.sh init                 one-time setup for a fresh world (idempotent): gamerules (no locator
#                                        bar; players_sleeping_percentage 50), and
#                                        mc_data/mcve/{out,in} (the mod <-> Discord bridge mailbox) owned
#                                        by mm-mike
#   deploy/world.sh ring <dim> <radius>  set <dim>'s square border to radius (diameter 2r, centre 0,0) and
#                                        start Chunky pre-generation of exactly that square
#   deploy/world.sh status               border per dimension + Chunky progress
#   deploy/world.sh maintenance [on <why> | off]
#                                        show, set or clear the maintenance flag: host/bin/mc-heal never restarts
#                                        the server while it is set (rings, restores, chapter work). A ring sets
#                                        it itself; clear it after the ring finishes
#
# <dim> is overworld | the_nether | the_end. Borders are per dimension on 26.x (design doc §5/§13).
# A ring only ever GROWS: shrinking a border strands players and builds outside it, so it is refused.
# Pre-gen runs inside the server (Chunky, continueOnRestart=true via pack overrides), so a maintenance
# reboot pauses it rather than ending it. Never admin-/locate on the live world (AGENTS.md).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
# The maintenance flag, in the repo root (= MAINT_FLAG in host/bin/mc-heal). While it exists mc-heal stands down: a
# ring's Chunky load can stall RCON in long GC pauses, and with continueOnRestart a heal would restart straight
# back into the ring. Persistent on purpose: a ring survives a reboot, so the flag must too.
MAINT_FLAG=maintenance
# The container is found by its compose labels and reached with plain `docker exec`: no compose parsing, so a
# malformed .env.bridge can't break the admin path (rv3). MC_RCON_CONTAINER targets a bare container instead
# (used to rehearse on a copy).
rcon() {
  local c="${MC_RCON_CONTAINER:-}"
  if [ -z "$c" ]; then
    c="$(docker ps -q --filter label=com.docker.compose.project=minecraft-ai-host \
      --filter label=com.docker.compose.service=minecraft --filter status=running | head -1)"
    [ -n "$c" ] || { echo "minecraft is not running (no container with the compose labels)" >&2; return 1; }
  fi
  docker exec "$c" rcon-cli "$@"
}

dim_id() {
  case "$1" in
    overworld | the_nether | the_end) echo "minecraft:$1" ;;
    *) echo "unknown dimension '$1' (overworld | the_nether | the_end)" >&2; exit 2 ;;
  esac
}

# Current border diameter of a dimension, as an integer ("World border is currently 6000.0 block(s) wide").
# Fails loudly (exit 1) when RCON or the reply is unreadable — never an empty value.
border_of() {
  local out n
  out="$(rcon "execute in $(dim_id "$1") run worldborder get" 2>&1)" || { echo "RCON failed reading the $1 border: ${out}" >&2; exit 1; }
  n="$(grep -oE '[0-9]+(\.[0-9]+)?' <<<"$out" | head -1 | cut -d. -f1)"
  [ -n "$n" ] || { echo "unreadable $1 border reply: ${out}" >&2; exit 1; }
  echo "$n"
}

case "${1:-}" in
  init)
    # The bridge's bind source: created here as the invoking user, so Docker never creates it root-owned.
    for d in mc_data mc_data/mcve mc_data/mcve/out mc_data/mcve/in; do  # never through a link (setup.sh step 10)
      if [ -L "$d" ] || { [ -e "$d" ] && [ "$(realpath -m "$d")" != "$(realpath -m .)/${d}" ]; }; then
        echo "refusing: ${d} is (or runs through) a symlink; nothing created" >&2; exit 1
      fi
    done
    install -d -m 755 mc_data/mcve mc_data/mcve/out mc_data/mcve/in
    for d in mc_data/mcve/out mc_data/mcve/in; do echo "${d}: $(stat -c '%U:%G %a' "$d")"; done
    rcon "gamerule minecraft:locator_bar false"  # keep "meet me at the big spire" navigation (design §7)
    rcon "gamerule minecraft:locator_bar"
    rcon "gamerule minecraft:players_sleeping_percentage 50"  # one of two sleepers skips the night (on-and-off group)
    rcon "gamerule minecraft:players_sleeping_percentage"
    ;;
  ring)
    dim="${2:?usage: world.sh ring <dim> <radius>}"
    radius="${3:?usage: world.sh ring <dim> <radius>}"
    [[ "$radius" =~ ^[0-9]+$ ]] && [ "$radius" -ge 100 ] || { echo "radius must be an integer >= 100" >&2; exit 2; }
    id="$(dim_id "$dim")"
    # Chunky + pause-when-empty exhausts the heap: a paused server never unloads the chunks Chunky makes
    # (prod OOM 2026-10-01, AGENTS.md hard-won facts). Refuse until pausing is off for the pre-gen.
    props="${MC_PROPERTIES:-mc_data/server.properties}"
    pause="$( { sed -nE 's/^pause-when-empty-seconds=(-?[0-9]+)$/\1/p' "$props" 2>/dev/null || true; } | tail -1)"
    [ -n "$pause" ] || { echo "refusing: can't read pause-when-empty-seconds from ${props}" >&2; exit 1; }
    if [ "$pause" -gt 0 ]; then
      echo "refusing: pause-when-empty-seconds=${pause} in ${props}; set it to -1 and restart for the pre-gen" >&2
      exit 1
    fi
    want=$((radius * 2))
    have="$(border_of "$dim")"
    if [ "$have" -gt "$want" ] && [ "$have" -lt 59000000 ]; then
      echo "refusing: ${dim} border is ${have}, a ring of ${want} would SHRINK it" >&2
      exit 1
    fi
    # A running Chunky task keeps its OLD radius ("Task already started"), so the new ring would silently go
    # un-pre-generated: refuse until it finishes (rehearsal on a copy, 2026-10-01).
    if rcon "chunky progress" | grep -q "Task running for ${id}\."; then
      echo "refusing: a Chunky task is still running for ${id}; wait for it (deploy/world.sh status)" >&2
      exit 1
    fi
    # mc-heal stands down BEFORE Chunky starts (the flag's why is at MAINT_FLAG). A flag already set by hand is kept.
    flag_ours=0
    if [ ! -e "$MAINT_FLAG" ]; then
      printf 'ring %s r=%s since %s\n' "$dim" "$radius" "$(date -u +%FT%TZ)" >"$MAINT_FLAG" \
        || { echo "refusing: can't write the maintenance flag ${MAINT_FLAG}; nothing changed" >&2; exit 1; }
      flag_ours=1
      # Any exit before Chunky confirms (an rcon failure under set -e included) takes our flag back: a flag left
      # with no ring running would keep mc-heal off silently. Disarmed once the ring is confirmed.
      trap 'rm -f "$MAINT_FLAG"' EXIT
    fi
    rcon "execute in ${id} run worldborder center 0 0"
    rcon "execute in ${id} run worldborder set ${want}"
    rcon "chunky world ${id}"
    rcon "chunky shape square"
    rcon "chunky center 0 0"
    rcon "chunky radius ${radius}"
    started="$(rcon "chunky start")"
    echo "$started"
    if ! grep -q "Task started in ${id} " <<<"$started"; then
      echo "Chunky did not confirm a new task for ${id}; the ring is NOT pre-generating" >&2
      exit 1  # the EXIT trap takes our flag back
    fi
    trap - EXIT  # the ring runs: the flag stays until 'maintenance off'
    echo "ring set: ${dim} border ${have} -> ${want}; Chunky pre-generating r=${radius} (status: deploy/world.sh status)"
    echo "maintenance flag set ($(cat "$MAINT_FLAG")): mc-heal stands down. When the ring is done:"
    echo "  deploy/world.sh maintenance off   (and pause-when-empty-seconds back to 60)"
    ;;
  status)
    for d in overworld the_nether the_end; do
      b="$(border_of "$d")"
      echo "${d}: border ${b}"
    done
    rcon "chunky progress"
    if [ -e "$MAINT_FLAG" ]; then echo "maintenance flag set: $(cat "$MAINT_FLAG") (mc-heal stands down)"; fi
    ;;
  maintenance)
    case "${2:-}" in
      on)
        why="${3:?usage: world.sh maintenance on <why>}"
        printf '%s since %s\n' "$why" "$(date -u +%FT%TZ)" >"$MAINT_FLAG"
        echo "maintenance flag set: $(cat "$MAINT_FLAG") (mc-heal stands down until 'maintenance off')"
        ;;
      off)
        if [ -e "$MAINT_FLAG" ]; then
          echo "maintenance flag cleared (was: $(cat "$MAINT_FLAG")); mc-heal guards the server again"
          rm -f "$MAINT_FLAG"
        else
          echo "no maintenance flag set"
        fi
        ;;
      "")
        if [ -e "$MAINT_FLAG" ]; then echo "maintenance flag set: $(cat "$MAINT_FLAG")"; else echo "no maintenance flag set"; fi
        ;;
      *) echo "usage: world.sh maintenance [on <why> | off]" >&2; exit 2 ;;
    esac
    ;;
  *)
    sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
