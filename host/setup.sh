#!/usr/bin/env bash
# Host maintenance for asus-node, as code. Run on asus-node from the repo checkout:
#
#   sudo bash host/setup.sh      then      bash host/check.sh
#
# Idempotent: a second run reports every step "unchanged" and reloads/restarts nothing.
# It installs (AGENTS.md "Host maintenance (asus-node)" has the why and how to undo each):
#   1. never sleep        masked sleep targets + logind drop-in
#   2. network            wired: static IP outside the router's DHCP pool (commit-confirm with rollback),
#                         autoconnect forever, metric 100; Wi-Fi unblocked; Wi-Fi DHCP fallback (600)
#   3. scripts + units    /usr/local/sbin/mc-*, /etc/systemd/system/mc-*, apt timer drop-ins
#   4. docker             live-restore (hot-reloaded; docker is never restarted under a running server)
#   5. update window      unattended-upgrades also takes Tailscale (Docker blacklisted: manual upgrades only);
#                         tailscale/snap auto-update moved into the window
#   6. backups            system mc-backup units (timer left DISABLED until stand-up); user-unit copies removed
#   7. alerts             /etc/mc-host/ for the Discord webhook
#   8. ssh                key-only (refused if mm-mike has no authorized key)
#   9. timers             mc-netwatch.timer, mc-maint.timer and mc-heal.timer enabled now (mc-server + mc-backup
#                         wait for stand-up)
#  10. bridge mailbox     mc_data/mcve/{out,in} (the mod <-> Discord bridge files) and extras/ (the mod jar)
#                         exist, owned by mm-mike; refused through any symlinked component
#  11. bridge fence       mc-bridge-fence.service enabled and its raw-table drops live (bridge: Internet only)
# It never enables mc-server.service or mc-backup.timer and never touches the world.
set -euo pipefail

MC_USER=mm-mike
MC_HOME=/home/mm-mike
REPO="${MC_HOME}/minecraft-ai-host"
WIRED="Wired connection 1"
# Example values for a typical home LAN: set for your network. The static address must sit outside the
# router's DHCP pool.
WIRED_ADDR="192.168.1.5/24"
WIRED_GW="192.168.1.1"
WIRED_DNS="192.168.1.1 1.1.1.1"
ROLLBACK_SECS=180
# Example: set to your Wi-Fi connection's NetworkManager name.
WIFI="HomeWiFi"
UNIT_DIR=/etc/systemd/system
HOST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=host/lib/net.sh
. "${HOST_DIR}/lib/net.sh"

step() { printf '\n== %s\n' "$*"; }
log() { printf '   %s\n' "$*"; }
warn() { printf '   WARNING: %s\n' "$*" >&2; }
NEXT=()
next() { NEXT+=("$*"); }
ABORTED=0
# abort_step MSG: a fail-closed refusal. The step changes nothing, the run goes on, and setup exits 1.
abort_step() {
  warn "ABORTED: $*"
  next "$*"
  ABORTED=1
}

# ---- preflight --------------------------------------------------------------------------------
if [ "$(id -u)" -ne 0 ]; then
  echo "run as root: sudo bash host/setup.sh" >&2
  exit 1
fi
if [ "$(hostname)" != "asus-node" ] && [ "${MC_HOST_FORCE:-0}" != 1 ]; then
  echo "refusing: this is $(hostname), not asus-node (MC_HOST_FORCE=1 overrides)" >&2
  exit 1
fi
# The units hard-code ${REPO}; installing them from any other checkout would point them elsewhere.
if [ "$(realpath "${HOST_DIR}/..")" != "$REPO" ]; then
  echo "refusing: run from ${REPO} (this checkout is $(realpath "${HOST_DIR}/.."))" >&2
  exit 1
fi

CHANGED=0
# install_file SRC DST MODE: install only if content or mode differs. Sets CHANGED=1 if it wrote.
install_file() {
  local src=$1 dst=$2 mode=$3
  CHANGED=0
  if [ -f "$dst" ] && cmp -s "$src" "$dst" && [ "$(stat -c %a "$dst")" = "$mode" ]; then
    log "unchanged $dst"
    return 0
  fi
  install -D -m "$mode" -o root -g root "$src" "$dst"
  log "installed $dst"
  CHANGED=1
}

# nm_set CONN PROP VALUE [PROP VALUE ...]: modify only the properties that differ.
nm_set() {
  local conn=$1 prop want have
  local args=()
  shift
  while [ $# -gt 0 ]; do
    prop=$1 want=$2
    shift 2
    have="$(nmcli -g "$prop" connection show "$conn")"
    have="${have%% *}"  # "0 (forever)" style output -> "0"
    if [ "$have" != "$want" ]; then
      args+=("$prop" "$want")
      log "${conn}: ${prop} ${have} -> ${want}"
    fi
  done
  if [ ${#args[@]} -eq 0 ]; then
    log "${conn}: unchanged"
  else
    nmcli connection modify "$conn" "${args[@]}"
  fi
}

nm_exists() { nmcli -g connection.id connection show "$1" >/dev/null 2>&1; }

# wired_static: move WIRED to WIRED_ADDR (manual), commit-confirm style. Unchanged if already there.
wired_static() {
  local ip="${WIRED_ADDR%/*}" method addrs gw dns dev control neigh verdict deadline answered=0
  method="$(nmcli -g ipv4.method connection show "$WIRED")"
  addrs="$(nmcli -g ipv4.addresses connection show "$WIRED")"
  gw="$(nmcli -g ipv4.gateway connection show "$WIRED")"
  dns="$(nmcli -g ipv4.dns connection show "$WIRED")"
  if wired_at_target "$method" "$addrs" "$gw" "$dns" "$WIRED_ADDR" "$WIRED_GW" "$WIRED_DNS"; then
    log "unchanged ${WIRED}: ipv4 manual ${WIRED_ADDR} gw ${WIRED_GW} dns ${WIRED_DNS}"
    return 0
  fi
  log "${WIRED}: ipv4 ${method} '${addrs}' gw '${gw}' dns '${dns}' -> manual ${WIRED_ADDR} gw ${WIRED_GW} dns ${WIRED_DNS}"
  dev="$(nmcli -g GENERAL.DEVICES connection show "$WIRED")"
  if [ -z "$dev" ]; then
    abort_step "static IP: '${WIRED}' is not active, so ${ip} cannot be probed; plug in the cable and re-run setup"
    return 0
  fi

  # Precheck, fail-closed: unless we already hold the address, prove nobody else does. No arping on the
  # host, so: ping, then look for a resolved lladdr in the neighbour table. The gateway is the positive
  # control: if it shows no lladdr, the probe itself is broken and proves nothing.
  if ip -4 -o addr show dev "$dev" | grep -q " ${ip}/"; then
    log "${dev} already holds ${ip}; no conflict probe needed"
  else
    ping -c2 -W1 -I "$dev" "$WIRED_GW" >/dev/null 2>&1 || true
    control="$(ip neigh show "$WIRED_GW" dev "$dev")"
    ip neigh flush to "$ip" dev "$dev" 2>/dev/null || true  # a stale entry must not count as an answer
    ping -c2 -W1 -I "$dev" "$ip" >/dev/null 2>&1 && answered=1
    neigh="$(ip neigh show "$ip" dev "$dev")"
    verdict="$(probe_verdict "$control" "$answered" "$neigh")"
    if [ "$verdict" = broken ]; then
      abort_step "static IP: probe broken, the gateway control ${WIRED_GW} shows no lladdr on ${dev} ('${control}'); nothing changed"
      return 0
    elif [ "$verdict" = in-use ]; then
      abort_step "static IP: ${ip} is IN USE on the LAN (ping answered=${answered}, neighbour '${neigh}'); pick a free address outside the DHCP pool and set WIRED_ADDR"
      return 0
    fi
    log "${ip} is free (no ping reply, neighbour '${neigh:-none}'); control ${WIRED_GW}: lladdr present"
  fi

  if systemctl is-active --quiet mc-net-rollback.timer; then
    abort_step "static IP: a network rollback is already armed (mc-net-rollback.timer); let it finish, then re-run setup"
    return 0
  fi
  systemctl reset-failed mc-net-rollback.timer mc-net-rollback.service mc-net-apply.service 2>/dev/null || true
  install -d -m 755 /var/lib/mc-host
  write_prev /var/lib/mc-host/wired-ipv4.prev "$WIRED" "$method" "$addrs" "$gw" "$dns"
  install_file "${HOST_DIR}/bin/mc-net-commit" /usr/local/sbin/mc-net-commit 755

  # Commit-confirm: arm the rollback FIRST, then change, then confirm, then disarm. The deadline is taken
  # before arming, so it is never later than the real firing time.
  rm -f /var/lib/mc-host/net-rollback.fired
  deadline=$(($(date +%s) + ROLLBACK_SECS))
  systemd-run --quiet --on-active="$ROLLBACK_SECS" --unit=mc-net-rollback /usr/local/sbin/mc-net-commit rollback
  log "armed mc-net-rollback: restores /var/lib/mc-host/wired-ipv4.prev in ${ROLLBACK_SECS} s unless confirmed"
  nmcli connection modify "$WIRED" ipv4.method manual ipv4.addresses "$WIRED_ADDR" \
    ipv4.gateway "$WIRED_GW" ipv4.dns "$WIRED_DNS"
  # The apply+confirm runs as its own unit, so it finishes even if this SSH session drops with the old address.
  log "applying (nmcli connection up); an SSH session on the old LAN address may drop here: reconnect via Tailscale or ${ip}"
  local rc=0
  systemd-run --quiet --wait --collect --unit=mc-net-apply -p Type=oneshot -p TimeoutStartSec="$ROLLBACK_SECS" \
    /usr/local/sbin/mc-net-commit apply "$WIRED" "$ip" "$WIRED_GW" "$deadline" || rc=$?
  if [ "$rc" = 0 ]; then
    log "confirmed: ${WIRED} is ${WIRED_ADDR}; rollback disarmed"
  elif [ "$rc" = 4 ]; then
    abort_step "static IP: FAILED, the rollback fired before the confirm finished; the previous settings are back (journalctl -u mc-net-apply -u mc-net-rollback)"
  else
    abort_step "static IP: NOT confirmed; mc-net-rollback restores the previous settings ${ROLLBACK_SECS} s after arming (journalctl -u mc-net-apply -u mc-net-rollback)"
  fi
}

mc_container_running() {
  [ -n "$(docker ps -q --filter label=com.docker.compose.service=minecraft --filter status=running 2>/dev/null)" ]
}

# ---- 1. never sleep ---------------------------------------------------------------------------
step "1. never sleep"
to_mask=()
for t in sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target; do
  if [ "$(systemctl is-enabled "$t" 2>/dev/null || true)" = masked ]; then
    log "unchanged ${t} (masked)"
  else
    to_mask+=("$t")
  fi
done
if [ ${#to_mask[@]} -gt 0 ]; then
  systemctl mask "${to_mask[@]}"
  log "masked ${to_mask[*]}"
fi
install_file "${HOST_DIR}/conf/logind-10-mc-host.conf" /etc/systemd/logind.conf.d/10-mc-host.conf 644
if [ "$CHANGED" = 1 ]; then
  # reload (SIGHUP), never restart: restarting logind can take the GNOME session down with it.
  systemctl reload systemd-logind
  log "reloaded systemd-logind"
fi

# ---- 2. resilient network ---------------------------------------------------------------------
step "2. resilient network"
if nm_exists "$WIRED"; then
  nm_set "$WIRED" connection.autoconnect yes connection.autoconnect-retries 0 \
    connection.autoconnect-priority 100 ipv4.route-metric 100
else
  warn "NetworkManager connection '${WIRED}' not found; wired settings skipped"
  next "Wired connection '${WIRED}' is missing: check 'nmcli connection show' and fix WIRED in host/setup.sh + MC_WIRED_CONN in mc-netwatch"
fi

if rfkill -n -o TYPE,SOFT | awk '$1 == "wlan" && $2 == "blocked" { found = 1 } END { exit !found }'; then
  rfkill unblock wifi  # Bluetooth stays blocked
  log "rfkill: unblocked wifi"
else
  log "unchanged rfkill wifi (not blocked)"
fi
if [ "$(nmcli radio wifi)" != enabled ]; then
  nmcli radio wifi on
  log "nmcli: wifi radio on"
else
  log "unchanged wifi radio (enabled)"
fi

if nm_exists "$WIFI"; then
  psk_flags="$(nmcli -g 802-11-wireless-security.psk-flags connection show "$WIFI")"
  psk_flags="${psk_flags%% *}"
  perms="$(nmcli -g connection.permissions connection show "$WIFI")"
  psk_stored=0
  # Only a system-owned secret is readable here; never print it.
  if [ "$psk_flags" = 0 ] && [ -n "$(nmcli -s -g 802-11-wireless-security.psk connection show "$WIFI" 2>/dev/null)" ]; then
    psk_stored=1
  fi
  if [ "$psk_flags" = 0 ] && [ -z "$perms" ] && [ "$psk_stored" = 1 ]; then
    nm_set "$WIFI" connection.autoconnect yes connection.autoconnect-priority 10 ipv4.route-metric 600
  else
    warn "${WIFI}: password not stored system-wide (psk-flags=${psk_flags}, permissions='${perms}', psk stored=${psk_stored}); fallback NOT configured"
    next "Wi-Fi fallback: store the ${WIFI} password system-wide, then re-run setup:
       read -rs -p '${WIFI} password: ' PSK; echo
       sudo nmcli connection modify ${WIFI} connection.permissions '' 802-11-wireless-security.psk-flags 0 802-11-wireless-security.psk \"\$PSK\"; unset PSK
       sudo bash host/setup.sh"
  fi
else
  warn "NetworkManager connection '${WIFI}' not found; no Wi-Fi fallback"
  next "Wi-Fi fallback: no '${WIFI}' connection exists; create it with 'sudo nmcli device wifi connect ${WIFI} --ask', then re-run setup"
fi

# Last in step 2, so the Wi-Fi safety net above is already up during the static-IP commit window.
if nm_exists "$WIRED"; then
  wired_static
fi

# ---- 3. scripts + units -----------------------------------------------------------------------
step "3. scripts and units (/usr/local/sbin, ${UNIT_DIR})"
for f in "${HOST_DIR}"/bin/mc-*; do
  install_file "$f" "/usr/local/sbin/$(basename "$f")" 755
done
units_changed=0
timers_to_restart=()
for f in "${HOST_DIR}"/systemd/*.service "${HOST_DIR}"/systemd/*.timer; do
  name="$(basename "$f")"
  install_file "$f" "${UNIT_DIR}/${name}" 644
  if [ "$CHANGED" = 1 ]; then
    units_changed=1
    [[ "$name" == *.timer ]] && timers_to_restart+=("$name")
  fi
done
for d in "${HOST_DIR}"/systemd/*.d; do
  unit="$(basename "$d" .d)"
  for f in "$d"/*.conf; do
    install_file "$f" "${UNIT_DIR}/${unit}.d/$(basename "$f")" 644
    if [ "$CHANGED" = 1 ]; then
      units_changed=1
      [[ "$unit" == *.timer ]] && timers_to_restart+=("$unit")
    fi
  done
done
if [ "$units_changed" = 1 ]; then
  systemctl daemon-reload
  log "systemctl daemon-reload"
fi
for t in "${timers_to_restart[@]}"; do
  # A changed schedule only takes effect on a running timer after a restart.
  if systemctl is-active --quiet "$t"; then
    systemctl restart "$t"
    log "restarted ${t} (schedule changed)"
  fi
done
log "mc-server.service installed, NOT enabled (production stand-up enables it)"
next "At production stand-up: sudo systemctl enable --now mc-server.service mc-backup.timer"

# ---- 4. docker live-restore -------------------------------------------------------------------
step "4. docker live-restore"
if [ -f /etc/docker/daemon.json ] && ! [ -f /etc/docker/daemon.json.pre-mc-host ]; then
  cp -p /etc/docker/daemon.json /etc/docker/daemon.json.pre-mc-host
fi
mkdir -p /etc/docker
# Merge: keep every existing key, set live-restore. Invalid existing JSON aborts the script (set -e).
daemon_json="$(python3 - /etc/docker/daemon.json <<'PY'
import json, os, sys
path = sys.argv[1]
cur = {}
if os.path.exists(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    cur = json.loads(text) if text.strip() else {}
if cur.get("live-restore") is True:
    print("unchanged")
    sys.exit(0)
cur["live-restore"] = True
tmp = path + ".mc-host.tmp"
with open(tmp, "w", encoding="utf-8") as fh:
    json.dump(cur, fh, indent=2)
    fh.write("\n")
os.replace(tmp, path)
print("changed")
PY
)"
log "/etc/docker/daemon.json: ${daemon_json}"
if [ "$(docker info --format '{{.LiveRestoreEnabled}}' 2>/dev/null)" = true ]; then
  log "unchanged: docker reports live-restore active"
else
  # live-restore is hot-reloadable (SIGHUP): no container is touched.
  systemctl reload docker.service || warn "docker reload failed"
  if [ "$(docker info --format '{{.LiveRestoreEnabled}}' 2>/dev/null)" = true ]; then
    log "reloaded docker: live-restore active"
  elif mc_container_running; then
    warn "live-restore not active after reload, and a minecraft container is running: NOT restarting docker"
    next "live-restore: after the players are off, 'sudo systemctl restart docker' (then docker info shows Live Restore Enabled: true)"
  else
    systemctl restart docker.service
    log "restarted docker (no minecraft container running)"
  fi
fi

# ---- 5. maintenance window --------------------------------------------------------------------
step "5. maintenance window (04:30 lists, 05:00 upgrades, 05:00-05:30 snaps, 05:40 mc-maint)"
# A broken apt.conf.d file breaks every apt command, so parse it before installing it.
apt-config -c "${HOST_DIR}/conf/apt-52mc-host" dump >/dev/null
install_file "${HOST_DIR}/conf/apt-52mc-host" /etc/apt/apt.conf.d/52mc-host 644
apt-config dump >/dev/null
apt_timers="$(systemctl show apt-daily.timer apt-daily-upgrade.timer -p TimersCalendar --value | grep -o 'OnCalendar=[^;]*' | tr '\n' ' ')"
log "apt timers: ${apt_timers}"

if command -v tailscale >/dev/null; then
  ts_apply="$(tailscale debug prefs | python3 -c 'import json,sys; print((json.load(sys.stdin).get("AutoUpdate") or {}).get("Apply"))')"
  if [ "$ts_apply" = False ]; then
    log "unchanged tailscale auto-update (off)"
  else
    tailscale set --auto-update=false
    log "tailscale auto-update ${ts_apply} -> off (apt takes it at 05:00)"
  fi
else
  warn "tailscale not found"
fi

if command -v snap >/dev/null; then
  if [ "$(snap get system refresh.timer 2>/dev/null || true)" = "05:00-05:30" ]; then
    log "unchanged snap refresh.timer (05:00-05:30)"
  else
    snap set system refresh.timer=05:00-05:30
    log "snap refresh.timer -> 05:00-05:30"
  fi
else
  log "no snapd; skipped"
fi

# ---- 6. backups -------------------------------------------------------------------------------
step "6. backups (system units; timer stays disabled until stand-up)"
user_dir="${MC_HOME}/.config/systemd/user"
if [ -e "${user_dir}/mc-backup.timer" ] || [ -e "${user_dir}/mc-backup.service" ] \
  || [ -L "${user_dir}/timers.target.wants/mc-backup.timer" ]; then
  systemctl --user -M "${MC_USER}@" disable --now mc-backup.timer \
    || warn "could not disable the user timer via the user manager; removing its files anyway"
  rm -f "${user_dir}/mc-backup.timer" "${user_dir}/mc-backup.service" "${user_dir}/timers.target.wants/mc-backup.timer"
  systemctl --user -M "${MC_USER}@" daemon-reload || true
  log "removed the old --user mc-backup units"
else
  log "unchanged: no --user mc-backup copies"
fi
log "mc-backup.timer: $(systemctl is-enabled mc-backup.timer 2>/dev/null || true)"

# ---- 7. alerts --------------------------------------------------------------------------------
step "7. alerts (/etc/mc-host/webhook)"
if [ "$(stat -c '%U:%G %a' /etc/mc-host 2>/dev/null || true)" = "root:${MC_USER} 750" ]; then
  log "unchanged /etc/mc-host"
else
  install -d -m 750 -o root -g "$MC_USER" /etc/mc-host
  log "created /etc/mc-host (root:${MC_USER} 750)"
fi
if [ -f /etc/mc-host/webhook ]; then
  if [ "$(stat -c '%U:%G %a' /etc/mc-host/webhook)" = "root:${MC_USER} 640" ]; then
    log "unchanged /etc/mc-host/webhook"
  else
    chown "root:${MC_USER}" /etc/mc-host/webhook
    chmod 640 /etc/mc-host/webhook
    log "fixed /etc/mc-host/webhook ownership/mode (root:${MC_USER} 640)"
  fi
else
  warn "/etc/mc-host/webhook missing: alerts are logged to the journal only"
  next "Alerts: create a Discord webhook (Channel settings > Integrations > Webhooks), then:
       read -rs -p 'webhook URL: ' URL; echo; printf '%s\n' \"\$URL\" | sudo install -m 640 -o root -g ${MC_USER} /dev/stdin /etc/mc-host/webhook; unset URL
       /usr/local/sbin/mc-alert 'test alert from asus-node'"
fi

# ---- 8. ssh -----------------------------------------------------------------------------------
step "8. ssh (key-only)"
auth_keys="${MC_HOME}/.ssh/authorized_keys"
sshd_conf=/etc/ssh/sshd_config.d/10-mc-host.conf
# ssh.service normally creates this; with socket activation it may not exist yet, and sshd -t/-T need it.
install -d -m 755 /run/sshd
if cmp -s "${HOST_DIR}/conf/sshd-10-mc-host.conf" "$sshd_conf"; then
  log "unchanged ${sshd_conf}"
elif ! grep -Ev '^[[:space:]]*(#|$)' "$auth_keys" 2>/dev/null \
  | grep -Eq '(^|[[:space:]])(ssh-[a-z0-9-]+|ecdsa-sha2-[a-z0-9-]+|sk-[a-z0-9@.-]+)[[:space:]]+AAAA[0-9A-Za-z+/]'; then
  warn "REFUSED: ${auth_keys} holds no public key; disabling passwords now could lock you out"
  next "SSH: add your key to ${auth_keys} (ssh-copy-id from the workstation), then re-run setup"
elif ! sshd -T 2>/dev/null | grep -qx 'pubkeyauthentication yes'; then
  warn "REFUSED: sshd has PubkeyAuthentication off; disabling passwords would lock you out"
else
  prev=""
  if [ -f "$sshd_conf" ]; then
    prev="$(mktemp)"
    cp -p "$sshd_conf" "$prev"
  fi
  install -D -m 644 -o root -g root "${HOST_DIR}/conf/sshd-10-mc-host.conf" "$sshd_conf"
  if sshd -t; then
    log "installed ${sshd_conf}; sshd -t OK"
    if systemctl is-active --quiet ssh.service; then
      systemctl reload ssh.service  # existing sessions are untouched
      log "reloaded ssh.service"
    else
      log "ssh.service not running (socket-activated); the next connection reads the new config"
    fi
  else
    warn "sshd -t rejected the config; restoring the previous state, NOT reloading"
    if [ -n "$prev" ]; then cp -p "$prev" "$sshd_conf"; else rm -f "$sshd_conf"; fi
  fi
  [ -n "$prev" ] && rm -f "$prev"
fi

# ---- 9. timers --------------------------------------------------------------------------------
step "9. enable mc-netwatch.timer + mc-maint.timer + mc-heal.timer"
# Refuse to arm the watchdog on a verdict it gets wrong here (e.g. a gateway that drops ping):
# it would bounce the network and reboot every 6 h. This script runs over a working network.
if probe="$(/usr/local/sbin/mc-netwatch --probe)"; then
  log "netwatch probe: ${probe}"
  netwatch_ok=1
else
  warn "netwatch probe says '${probe}' although this session has network: NOT enabling mc-netwatch.timer"
  next "mc-netwatch: its health probe fails on a working network (${probe}); fix the probe before 'sudo systemctl enable --now mc-netwatch.timer'"
  netwatch_ok=0
fi
for t in mc-netwatch.timer mc-maint.timer mc-heal.timer; do
  if [ "$t" = mc-netwatch.timer ] && [ "$netwatch_ok" = 0 ]; then
    continue
  fi
  if systemctl is-enabled --quiet "$t" && systemctl is-active --quiet "$t"; then
    log "unchanged ${t} (enabled, active)"
  else
    systemctl enable --now "$t"
    log "enabled + started ${t}"
  fi
done

# ---- 10. bridge mailbox -----------------------------------------------------------------------
step "10. bridge mailbox (${REPO}/mc_data/mcve/{out,in}, ${REPO}/extras)"
# Bind sources: if Docker created them they would be root-owned, and the mod (itzg, uid 1000) could not
# write out/ nor the bridge in/. out/ = the mod's (bridge mounts it read-only), in/ = the bridge's, extras/ =
# the mod jar. mc_data itself is created only when missing (itzg owns what is inside it).
# Refuse BEFORE touching anything if any component (mc_data, mcve, a leaf) is a symlink: install -d would follow
# it and create or chown dirs wherever it points, as root.
mailbox=("${REPO}/mc_data" "${REPO}/mc_data/mcve" "${REPO}/mc_data/mcve/out" "${REPO}/mc_data/mcve/in" "${REPO}/extras")
linked=""
for d in "${mailbox[@]}"; do
  if [ -L "$d" ] || { [ -e "$d" ] && [ "$(realpath -m "$d")" != "$d" ]; }; then linked="$d"; break; fi
done
if [ -n "$linked" ]; then
  abort_step "${linked} is (or runs through) a symlink; refusing to follow it, nothing created (move it aside and re-run)"
else
  [ -d "${REPO}/mc_data" ] || install -d -m 755 -o "$MC_USER" -g "$MC_USER" "${REPO}/mc_data"
  for d in "${mailbox[@]:1}"; do
    if [ "$(stat -c '%U:%G %a' "$d" 2>/dev/null || true)" = "${MC_USER}:${MC_USER} 755" ]; then
      log "unchanged ${d}"
    else
      install -d -m 755 -o "$MC_USER" -g "$MC_USER" "$d"
      log "set ${d} (${MC_USER}:${MC_USER} 755)"
    fi
  done
fi

# ---- 11. bridge fence -------------------------------------------------------------------------
step "11. bridge egress fence (mc-bridge-fence.service: br-mcve reaches the Internet only)"
# Installed by step 3; ordered Before=docker.service, so after a reboot the drops exist before any container.
if systemctl is-enabled --quiet mc-bridge-fence.service && /usr/local/sbin/mc-bridge-fence check >/dev/null; then
  log "unchanged mc-bridge-fence (enabled, drops live)"
else
  systemctl enable mc-bridge-fence.service
  systemctl restart mc-bridge-fence.service  # no ExecStop: a restart never opens the fence, it re-applies
  log "enabled + applied mc-bridge-fence: $(/usr/local/sbin/mc-bridge-fence check)"
fi

# ---- next steps -------------------------------------------------------------------------------
step "done. Verify with: bash host/check.sh"
if [ ${#NEXT[@]} -gt 0 ]; then
  echo
  echo "Operator next steps:"
  for n in "${NEXT[@]}"; do
    printf ' - %s\n' "$n"
  done
fi
if [ "$ABORTED" = 1 ]; then
  echo
  echo "setup finished with ABORTED step(s) (see above); exit 1"
  exit 1
fi
