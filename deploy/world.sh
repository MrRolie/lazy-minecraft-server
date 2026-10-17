#!/usr/bin/env bash
# World rings: the launch borders and every chapter's border push, as one tool. Runs as the kit user in the
# repo dir on the game host, against the running server, via in-container rcon-cli.
#
#   deploy/world.sh init                 one-time setup for a fresh world (idempotent): gamerules (no locator
#                                        bar; players_sleeping_percentage 50), and
#                                        mc_data/mcve/{out,in} (the mod <-> Discord bridge mailbox) owned
#                                        by the kit user
#   deploy/world.sh ring <dim> <radius>  set <dim>'s square border to radius (diameter 2r, centre 0,0) and
#                                        start Chunky pre-generation of exactly that square; it stamps the world
#                                        first (mc_data/<LEVEL>/.ve-ring-<dim>), for `done`
#   deploy/world.sh status               border per dimension + Chunky progress
#   deploy/world.sh done [<dim>=<radius> ...]
#                                        exit 0 only when Chunky has FINISHED the last ring started on THIS world
#                                        in all three dimensions (and, for each <dim>=<radius> given, that ring
#                                        was that radius); a ring in progress, paused, cancelled, never started,
#                                        or not started by `ring` fails. Reads files only (the ring stamps,
#                                        Chunky's task files and the server logs), no rcon
#   deploy/world.sh maintenance [on <why> | off]
#                                        show, set or clear the maintenance flag: host/bin/mc-heal never restarts
#                                        the server while it is set (rings, restores, chapter work). A ring sets
#                                        it itself; clear it after the ring finishes
#
# <dim> is overworld | the_nether | the_end. Borders are per dimension on 26.x.
# A ring only ever GROWS: shrinking a border strands players and builds outside it, so it is refused.
# Pre-gen runs inside the server (Chunky, continueOnRestart=true via pack overrides), so a maintenance
# reboot pauses it rather than ending it. Never admin-/locate on the live world (AGENTS.md).
set -euo pipefail

cd "$(dirname "${BASH_SOURCE[0]}")/.."
# The world dir: LEVEL from .env, default "world" (the same read as deploy/backup.sh).
LEVEL="$( { sed -n 's/^LEVEL=//p' .env 2>/dev/null || true; } | tail -1)"
LEVEL="${LEVEL:-world}"
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
    # The ring's stamp goes in the world dir (below), so it leaves with the world when the world is archived.
    [ -f "mc_data/${LEVEL}/level.dat" ] || {
      echo "refusing: no world at mc_data/${LEVEL}/level.dat (LEVEL from .env); start the server once first" >&2
      exit 1
    }
    # Chunky + pause-when-empty exhausts the heap: a paused server never unloads the chunks Chunky makes
    # (prod OOM 2026-10-01, AGENTS.md hard-won facts). Refuse until pausing is off for the pre-gen.
    props="${MC_PROPERTIES:-mc_data/server.properties}"
    pause="$( { sed -nE 's/^pause-when-empty-seconds=(-?[0-9]+)$/\1/p' "$props" 2>/dev/null || true; } | tail -1)"
    [ -n "$pause" ] || { echo "refusing: can't read pause-when-empty-seconds from ${props}" >&2; exit 1; }
    if [ "$pause" -gt 0 ]; then
      echo "refusing: pause-when-empty-seconds=${pause} in ${props}; set it to -1 for the pre-gen: kit.toml" \
        "server.pause_when_empty_seconds = -1, python3 kit/render.py, docker compose up -d minecraft (without that" \
        "key, itzg leaves server.properties alone: edit it and restart)" >&2
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
    # The stamp, just before Chunky starts: `done` accepts only a task file and a "Task finished" log line newer than
    # it. Chunky's task files and the logs sit outside the world dir, so an archived world's (or an earlier ring's)
    # would otherwise vouch for this one (review 2026-10-16). It records latest.log's size and the sha256 of those
    # bytes: `done` knows that file by them wherever it is later (still latest.log, a `cp -a` copy with new inodes, or
    # gzipped whole by a restart) and reads only what follows them. Kept when the start is not confirmed: with no newer
    # task file, `done` fails.
    stamp="mc_data/${LEVEL}/.ve-ring-${dim}"
    log_size=0
    if [ -f mc_data/logs/latest.log ]; then log_size="$(stat -c %s mc_data/logs/latest.log)"; fi
    log_head="$( { head -c "$log_size" mc_data/logs/latest.log 2>/dev/null || true; } | sha256sum | cut -d' ' -f1)"
    printf 'radius=%s\nsince=%s\nlog_size=%s\nlog_head=%s\n' "$radius" "$(date -u +%FT%TZ)" "$log_size" "$log_head" \
      >"$stamp" || { echo "refusing: can't write the ring stamp ${stamp}; Chunky not started" >&2; exit 1; }
    started="$(rcon "chunky start")"
    echo "$started"
    if ! grep -q "Task started in ${id} " <<<"$started"; then
      echo "Chunky did not confirm a new task for ${id}; the ring is NOT pre-generating" >&2
      exit 1  # the EXIT trap takes our flag back
    fi
    trap - EXIT  # the ring runs: the flag stays until 'maintenance off'
    echo "ring set: ${dim} border ${have} -> ${want}; Chunky pre-generating r=${radius} (status: deploy/world.sh status)"
    echo "maintenance flag set ($(cat "$MAINT_FLAG")): mc-heal stands down. When the ring is done:"
    echo "  deploy/world.sh maintenance off   (and pause-when-empty-seconds back to 60: kit.toml"
    echo "  server.pause_when_empty_seconds = 60 + render + up -d minecraft, or server.properties if kit.toml has no key)"
    ;;
  status)
    for d in overworld the_nether the_end; do
      b="$(border_of "$d")"
      echo "${d}: border ${b}"
    done
    rcon "chunky progress"
    if [ -e "$MAINT_FLAG" ]; then echo "maintenance flag set: $(cat "$MAINT_FLAG") (mc-heal stands down)"; fi
    ;;
  done)
    # Chunky 1.5.3 (pack/lock.json; source commit 7226809 of github.com/pop4959/Chunky = tag 1.5.0 + 3 commits)
    # keeps one task file per dimension, config/chunky/tasks/minecraft/<dim>.properties (TaskLoader.writeTask). It
    # is written only when a task ends, is cancelled, or the server stops (GenerationTask.run, TaskLoader.cancelTask,
    # Chunky.disable), never while it runs: no file = never started (or never saved).
    #  - cancelled=false: paused or stopped by a shutdown; continueOnRestart picks it up again. Not done.
    #  - cancelled=true is written BOTH by a cancel and by a task that finished (run(): `else cancelled = true`),
    #    so it says only "will not resume". The count decides: chunks against the task's total, which for the
    #    square shape and region pattern a ring uses is (2*ceil(radius/16)+1)^2 (Selection, RegionChunkIterator).
    #  - A finished task saves before its last in-flight chunks report: run() does not wait for them, and a chunk
    #    gives back its slot before its count lands, so the saved count can be more than MAX_WORKING_COUNT (50) short
    #    (asus-node's own finished rings: 51, 51 and 38 short, review 2026-10-16). So below the total, the log line
    #    Chunky prints the moment the count reaches the total decides, at any shortfall (a cancel never prints it):
    #    "[Chunky] Task finished for <world>. Processed: <total> chunks" (lang/en.json task_done; config language "en").
    # All of it must be THIS world's and THIS ring's: the task files and the logs sit outside mc_data/<LEVEL>/, so a
    # world archived or regenerated in the same mc_data leaves its own behind. `ring` stamps the world dir
    # (.ve-ring-<dim>) before Chunky starts; only a task file and log text newer than that stamp count. The log the
    # stamp recorded holds text from before the ring too; it is known by its first bytes (their size and sha256, in
    # the stamp), not by its inode or name, so a `cp -a` copy and a log a restart gzipped are read past them as well.
    # MC_DATA points it at a copy of the server directory (a restore rehearsal, or a read-only check of another host):
    # copy with `cp -a`, which keeps the times the checks compare.
    data="${MC_DATA:-mc_data}"
    shift
    declare -A want_r=()
    for a in "$@"; do
      [[ "$a" =~ ^(overworld|the_nether|the_end)=([0-9]+)$ ]] \
        || { echo "usage: world.sh done [<dim>=<radius> ...] (<dim>: overworld | the_nether | the_end)" >&2; exit 2; }
      want_r[${BASH_REMATCH[1]}]="${BASH_REMATCH[2]}"
    done
    # The log text written since the ring's stamp ($1), from latest.log and every rotated log newer than the stamp.
    # A log that starts with the bytes the stamp recorded is the one the ring started in: only what follows them is
    # read. Any other log began after the stamp and is read whole. A stamp without a readable record reads nothing
    # (fails closed). Whole files are piped to grep: an early-exiting reader would SIGPIPE a writer under pipefail.
    logs_since() {
      local s="$1" size head f
      size="$(sed -n 's/^log_size=//p' "$s" | tail -1)"; head="$(sed -n 's/^log_head=//p' "$s" | tail -1)"
      [[ "$size" =~ ^[0-9]+$ ]] && [[ "$head" =~ ^[0-9a-f]{64}$ ]] || return 0
      for f in "${data}/logs/latest.log" "${data}"/logs/*.log.gz; do
        [ -f "$f" ] && [ "$f" -nt "$s" ] || continue
        if [ "$( { zcat -f "$f" 2>/dev/null || true; } | head -c "$size" | sha256sum | cut -d' ' -f1)" = "$head" ]; then
          { zcat -f "$f" 2>/dev/null || true; } | tail -c +"$((size + 1))"
        else
          zcat -f "$f" 2>/dev/null || true
        fi
      done
      return 0
    }
    bad=0
    for d in overworld the_nether the_end; do
      f="${data}/config/chunky/tasks/minecraft/${d}.properties"
      stamp="${data}/${LEVEL}/.ve-ring-${d}"
      if [ ! -f "$stamp" ]; then
        echo "${d}: NOT done: no ring stamp (${stamp}): this world was never rung by deploy/world.sh ring"
        bad=1; continue
      fi
      if [ ! -f "$f" ]; then
        echo "${d}: NOT done: no Chunky task file (${f}): never started, or still running its first task"
        bad=1; continue
      fi
      if ! [ "$f" -nt "$stamp" ]; then
        echo "${d}: NOT done: Chunky has saved no task since this world's ring started ($(sed -n 's/^since=//p' \
          "$stamp")): still running, or ${f} is an older ring's or another world's"
        bad=1; continue
      fi
      prop() { sed -n "s/^$1=//p" "$f" | tail -1; }
      world="$(prop world)"; cancelled="$(prop cancelled)"; radius="$(prop radius)"
      shape="$(prop shape)"; pattern="$(prop pattern)"; chunks="$(prop chunks)"
      cx="$(prop center-x)"; cz="$(prop center-z)"
      if [ "$world" != "minecraft:${d}" ] || [ "$shape" != square ] || [ "$pattern" != region ] \
        || ! [[ "$cx" =~ ^-?0(\.0+)?$ ]] || ! [[ "$cz" =~ ^-?0(\.0+)?$ ]] \
        || ! [[ "$radius" =~ ^[0-9]+(\.0)?$ ]] || ! [[ "$chunks" =~ ^[0-9]+$ ]]; then
        echo "${d}: NOT done: ${f} is not a world.sh ring (want world=minecraft:${d}, shape=square," \
          "pattern=region, centre 0,0, a whole radius and a chunk count)"
        bad=1; continue
      fi
      r="${radius%.0}"
      rc=$(((r + 15) / 16))
      total=$(((2 * rc + 1) * (2 * rc + 1)))
      ringr="$(sed -n 's/^radius=//p' "$stamp" | tail -1)"
      if [ "$r" != "$ringr" ]; then
        echo "${d}: NOT done: Chunky's last task was radius ${r}, but this world's last ring is radius ${ringr:-?}"
        bad=1; continue
      fi
      if [ -n "${want_r[$d]:-}" ] && [ "$r" != "${want_r[$d]}" ]; then
        echo "${d}: NOT done: this world's last ring was radius ${r}, not ${want_r[$d]}"
        bad=1; continue
      fi
      pct="$(awk -v c="$chunks" -v t="$total" 'BEGIN { printf "%.1f", 100 * c / t }')"
      if [ "$cancelled" != true ]; then
        echo "${d}: NOT done: radius ${r}, ${chunks} of ${total} chunks (${pct}%), paused or stopped; it resumes" \
          "at the next start (or: chunky continue)"
        bad=1
      elif [ "$chunks" -ge "$total" ]; then
        echo "${d}: done, radius ${r}, ${chunks} of ${total} chunks"
      elif logs_since "$stamp" | grep -F "[Chunky] Task finished for minecraft:${d}. Processed: ${total} chunks" \
        >/dev/null; then
        echo "${d}: done, radius ${r}, ${chunks} of ${total} chunks saved, and the log says it finished"
      else
        echo "${d}: NOT done: radius ${r}, cancelled at ${chunks} of ${total} chunks (${pct}%), and no log line" \
          "since the ring started says it finished"
        bad=1
      fi
    done
    exit "$bad"
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
    sed -n '2,27p' "$0" | sed 's/^# \{0,1\}//'
    exit 2
    ;;
esac
