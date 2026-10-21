#!/usr/bin/env bash
# Verify the game host's maintenance (what host/setup.sh installs) from LIVE state, not from files
# alone: each line is PASS/FAIL with the evidence it read. Run as the kit user: `bash host/check.sh`.
#   --no-ssh   the host owner wants no SSH access (a one-machine kit install, docs/kit/KIT.md P7): the key-only
#              sshd lines are skipped, and an SSH server that is running FAILs instead. Only the ssh section reads it.
# The instance values come from host/kit.env (kit/render.py, from kit.toml), the same file setup.sh reads.
# Root-only reads (sshd -T, snap) use `sudo -n` when available and SKIP otherwise.
#   PASS/FAIL  a property that must hold (any FAIL -> exit 1)
#   WARN       owed by the operator or by production stand-up (does not fail the run)
#   INFO/SKIP  report only
set -uo pipefail

HOST_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(realpath "${HOST_DIR}/..")"
NO_SSH=0
case "${1:-}" in
  "") ;;
  --no-ssh) NO_SSH=1 ;;
  *) echo "usage: bash host/check.sh [--no-ssh]" >&2; exit 2 ;;
esac
# >>> kit config: the instance values, from host/kit.env (kit/render.py). host/test_host.py runs this block.
KIT_ENV="${HOST_DIR}/kit.env"
if [ ! -f "$KIT_ENV" ]; then
  echo "FAIL  ${KIT_ENV} is missing: nothing to check against (python3 kit/render.py renders it from kit.toml)"
  exit 1
fi
# Only render's own lines are sourced: KIT_ENV_LINE in kit/render.py (no $, `, \ or quote inside), for only the
# keys render writes (kit_env_values; host/test_host.py pins this list), so never PATH, IFS or KIT_ENV.
KIT_KEYS='MC_HOST_NAME|MC_USER|MC_HOME|MC_REPO|MC_UID|MC_GID|MC_TZ|MC_NET_MODE|MC_WIRED_CONN|MC_WIFI_CONN'
KIT_KEYS+='|MC_WIRED_ADDR|MC_WIRED_GW|MC_WIRED_DNS|MC_MAINT_START|MC_TS_HOST_NAME|MC_SERVER_ADDRESS|MC_CHARGE_CAP|MC_DISCORD'
if grep -qvE '^(#.*|('"$KIT_KEYS"')="[^"\\$`]*")?$' "$KIT_ENV"; then
  echo "FAIL  ${KIT_ENV} has a line that is not KEY=\"value\" (re-render it: python3 kit/render.py)"
  exit 1
fi
unset MC_DISCORD  # the one optional key: only kit.env may set it, never the caller's environment
# shellcheck source=/dev/null
. "$KIT_ENV"
for k in MC_HOST_NAME MC_USER MC_HOME MC_NET_MODE MC_CHARGE_CAP; do
  if [ -z "${!k:-}" ]; then
    echo "FAIL  ${KIT_ENV} has no ${k} (re-render it: python3 kit/render.py)"
    exit 1
  fi
done
# Present but possibly empty: empty = kit.toml leaves it out (dhcp; a Wi-Fi-only or wired-only laptop).
for k in MC_WIRED_CONN MC_WIRED_ADDR MC_WIRED_GW MC_WIRED_DNS MC_WIFI_CONN; do
  if [ -z "${!k+set}" ]; then
    echo "FAIL  ${KIT_ENV} has no ${k} (re-render it: python3 kit/render.py)"
    exit 1
  fi
done
WIRED="$MC_WIRED_CONN"
WIFI="$MC_WIFI_CONN"
WIRED_ADDR="$MC_WIRED_ADDR"
WIRED_GW="$MC_WIRED_GW"
WIRED_DNS="$MC_WIRED_DNS"
# Discord or not (kit.toml discord.enabled): render writes MC_DISCORD="off" for no Discord, and nothing for Discord,
# so an absent key is Discord, the stricter mode (the webhook and the bridge are then required).
DISCORD="${MC_DISCORD:-on}"
case "$DISCORD" in
  on | off) ;;
  *)
    echo "FAIL  ${KIT_ENV} MC_DISCORD=${MC_DISCORD} (off, or absent for Discord; re-render it: python3 kit/render.py)"
    exit 1
    ;;
esac
# <<< kit config
UNIT_DIR=/etc/systemd/system
LINGER_DIR=/var/lib/systemd/linger
UNIT_SRC="${HOST_DIR}/rendered/systemd"
# shellcheck source=host/lib/net.sh
. "${HOST_DIR}/lib/net.sh"

FAILS=0
res() {
  local st=$1
  shift
  printf '%-4s  %s\n' "$st" "$*"
  if [ "$st" = FAIL ]; then FAILS=$((FAILS + 1)); fi
}
# expect NAME WANT HAVE
expect() {
  if [ "$3" = "$2" ]; then res PASS "$1 = $3"; else res FAIL "$1 = '${3}' (want '${2}')"; fi
}
section() { printf '\n# %s\n' "$*"; }

# as_root CMD...: run with root rights if we can get them without a prompt; exit 99 if not.
as_root() {
  if [ "$(id -u)" -eq 0 ]; then
    "$@"
  elif sudo -n true 2>/dev/null; then
    sudo -n "$@"
  else
    return 99
  fi
}

# list-timers resolves monotonic timers (mc-netwatch: OnBootSec/OnUnitActiveSec) to wall-clock too.
next_run() { systemctl list-timers --all --no-legend "$1" 2>/dev/null | awk '{ print $1, $2, $3, $4 }'; }

# ---- sleep ------------------------------------------------------------------------------------
section "never sleep"
for t in sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target; do
  expect "systemctl is-enabled ${t}" masked "$(systemctl is-enabled "$t" 2>/dev/null)"
done
if cmp -s "${HOST_DIR}/conf/logind-10-mc-host.conf" /etc/systemd/logind.conf.d/10-mc-host.conf; then
  res PASS "/etc/systemd/logind.conf.d/10-mc-host.conf matches the repo"
else
  res FAIL "/etc/systemd/logind.conf.d/10-mc-host.conf missing or differs from host/conf/logind-10-mc-host.conf"
fi
# What logind is actually running with (catches a drop-in that was never reloaded).
for p in HandleLidSwitch HandleLidSwitchExternalPower HandleLidSwitchDocked HandleSuspendKey \
  HandleSuspendKeyLongPress HandleHibernateKey HandleHibernateKeyLongPress IdleAction; do
  v="$(busctl get-property org.freedesktop.login1 /org/freedesktop/login1 org.freedesktop.login1.Manager "$p" 2>/dev/null)"
  expect "logind ${p}" 's "ignore"' "$v"
done

# ---- network ----------------------------------------------------------------------------------
section "network"
nm() { local v; v="$(nmcli -g "$2" connection show "$1" 2>/dev/null)"; printf '%s' "${v%% *}"; }
if [ -z "$WIRED" ]; then
  res INFO "no wired connection in kit.toml (Wi-Fi-only laptop)"
elif nmcli -g connection.id connection show "$WIRED" >/dev/null 2>&1; then
  expect "${WIRED}: connection.autoconnect" yes "$(nm "$WIRED" connection.autoconnect)"
  expect "${WIRED}: connection.autoconnect-retries" 0 "$(nm "$WIRED" connection.autoconnect-retries)"
  expect "${WIRED}: connection.autoconnect-priority" 100 "$(nm "$WIRED" connection.autoconnect-priority)"
  expect "${WIRED}: ipv4.route-metric" 100 "$(nm "$WIRED" ipv4.route-metric)"
  if [ "$MC_NET_MODE" = static ]; then
    expect "${WIRED}: ipv4.method" manual "$(nm "$WIRED" ipv4.method)"
    expect "${WIRED}: ipv4.addresses" "$WIRED_ADDR" "$(norm_list "$(nmcli -g ipv4.addresses connection show "$WIRED" 2>/dev/null)")"
    expect "${WIRED}: ipv4.gateway" "$WIRED_GW" "$(nm "$WIRED" ipv4.gateway)"
    expect "${WIRED}: ipv4.dns" "$WIRED_DNS" "$(norm_list "$(nmcli -g ipv4.dns connection show "$WIRED" 2>/dev/null)")"
    dev="$(nmcli -g GENERAL.DEVICES connection show "$WIRED" 2>/dev/null)"
    live="$(ip -4 -o addr show dev "${dev:-none}" 2>/dev/null | awk '{ print $4 }' | xargs)"
    if [ -n "$dev" ] && grep -qw -- "$WIRED_ADDR" <<<"$live"; then
      res PASS "${dev} holds ${WIRED_ADDR} (live: ${live})"
    else
      res FAIL "${WIRED} device '${dev:-inactive}' does not hold ${WIRED_ADDR} (live: '${live}')"
    fi
  else
    expect "${WIRED}: ipv4.method (kit.toml net.mode=dhcp)" auto "$(nm "$WIRED" ipv4.method)"
  fi
else
  res FAIL "NetworkManager connection '${WIRED}' not found"
fi
if [ -n "$WIFI" ]; then
  wlan_soft="$(rfkill -n -o TYPE,SOFT 2>/dev/null | awk '$1 == "wlan" { print $2 }' | sort -u | tr '\n' ' ')"
  expect "rfkill wlan soft-block" "unblocked " "$wlan_soft"
  expect "nmcli radio wifi" enabled "$(nmcli radio wifi 2>/dev/null)"
fi
if [ -z "$WIFI" ]; then
  res INFO "no Wi-Fi connection in kit.toml (no Wi-Fi fallback)"
elif nmcli -g connection.id connection show "$WIFI" >/dev/null 2>&1; then
  ac="$(nm "$WIFI" connection.autoconnect)" pr="$(nm "$WIFI" connection.autoconnect-priority)"
  me="$(nm "$WIFI" ipv4.route-metric)" flags="$(nm "$WIFI" 802-11-wireless-security.psk-flags)"
  perms="$(nm "$WIFI" connection.permissions)" wmeth="$(nm "$WIFI" ipv4.method)"
  if [ -n "$WIRED" ]; then  # the wired link's fallback
    evidence="autoconnect=${ac} priority=${pr} metric=${me} ipv4.method=${wmeth} psk-flags=${flags} permissions='${perms}'"
    if [ "$ac" = yes ] && [ "$pr" = 10 ] && [ "$me" = 600 ] && [ "$wmeth" = auto ] && [ "$flags" = 0 ] && [ -z "$perms" ]; then
      res PASS "${WIFI} fallback: ${evidence}"
    elif [ "$flags" != 0 ] || [ -n "$perms" ]; then
      res WARN "${WIFI} fallback not configured: password not stored system-wide (${evidence}); see setup.sh next steps"
    else
      res FAIL "${WIFI} fallback: ${evidence} (want autoconnect=yes priority=10 metric=600 ipv4.method=auto)"
    fi
  else  # a Wi-Fi-only laptop's one link: it must come up at boot with nobody logged in
    retries="$(nm "$WIFI" connection.autoconnect-retries)"
    evidence="autoconnect=${ac} autoconnect-retries=${retries} psk-flags=${flags} permissions='${perms}'"
    if [ "$ac" = yes ] && [ "$retries" = 0 ] && [ "$flags" = 0 ] && [ -z "$perms" ]; then
      res PASS "${WIFI} (the only link): ${evidence}"
    else
      res FAIL "${WIFI} (the only link): ${evidence} (want autoconnect=yes autoconnect-retries=0, password stored system-wide; sudo bash host/setup.sh)"
    fi
  fi
elif [ -n "$WIRED" ]; then
  res WARN "no '${WIFI}' connection: no Wi-Fi fallback"
else
  res FAIL "no '${WIFI}' connection, and it is this laptop's only link (kit.toml net.wifi_conn)"
fi
# NetworkManager's address conflict detection (ACD, on by default: ipv4.dad-timeout -1 = 200 ms), this boot
# only. A conflict for the address the wired connection has NOW is a FAIL (NM dropped it and re-probes: the
# host flaps). Conflicts for other addresses (e.g. the old DHCP .234 from before the change) are INFO.
nm_journal="$(journalctl -b -u NetworkManager --no-pager -q -o short 2>&1)"
if grep -qi "insufficient permissions\|no journal files" <<<"$nm_journal"; then
  nm_journal="$(as_root journalctl -b -u NetworkManager --no-pager -q -o short 2>&1)" || nm_journal=""
fi
if [ -z "$nm_journal" ]; then
  res SKIP "NetworkManager journal unreadable (add ${MC_USER} to adm, or use sudo)"
else
  wired_ip=""
  [ -n "$WIRED" ] && wired_ip="$(norm_list "$(nmcli -g ipv4.addresses connection show "$WIRED" 2>/dev/null)")"
  wired_ip="${wired_ip%%/*}"
  all_conflicts="$(conflict_lines <<<"$nm_journal")"
  mine="$( [ -n "$wired_ip" ] && lines_for_ip "$wired_ip" <<<"$all_conflicts")"
  others="$(grep -vxF -- "$mine" <<<"$all_conflicts" | grep -c . || true)"
  [ -z "$mine" ] && others="$(grep -c . <<<"$all_conflicts" || true)"
  if [ -n "$mine" ]; then
    res FAIL "address conflict for the wired address ${wired_ip} this boot ($(grep -c . <<<"$mine") line(s)), last: $(tail -1 <<<"$mine")"
  else
    res PASS "no address conflict for the wired address ${wired_ip:-(none configured)} this boot"
  fi
  if [ "${others:-0}" -gt 0 ]; then
    res INFO "${others} conflict line(s) this boot for other addresses, last: $(grep -vxF -- "${mine:-@@none@@}" <<<"$all_conflicts" | tail -1)"
  fi
fi
if [ -x /usr/local/sbin/mc-netwatch ]; then
  probe_bin=/usr/local/sbin/mc-netwatch
else
  probe_bin="${HOST_DIR}/bin/mc-netwatch"
fi
if probe="$("$probe_bin" --probe)"; then res PASS "netwatch probe: ${probe}"; else res FAIL "netwatch probe: ${probe}"; fi
state=/var/lib/mc-host/netwatch.json
if [ -r "$state" ]; then
  res INFO "netwatch state: $(python3 - "$state" 2>&1 <<'PY'
import json, sys, time
s = json.load(open(sys.argv[1]))
first, last_reboot = s.get("first_failure"), s.get("last_reboot")
acts = [a.get("action") for a in s.get("actions", [])]
out = [f"failing for {round((time.time() - first) / 60)} min, actions {acts}" if first else "no failure episode"]
if last_reboot:
    out.append("last watchdog reboot " + time.strftime("%Y-%m-%d %H:%M", time.localtime(last_reboot)))
print("; ".join(out))
PY
)"
fi

# ---- deployed files ---------------------------------------------------------------------------
section "installed scripts and units match the repo"
# The render first: if kit.env or the units are not what kit.toml says, the comparisons below compare old values.
# -B: under sudo, an import would leave a root-owned kit/__pycache__ in the kit user's checkout.
if render_out="$(python3 -B "${REPO}/kit/render.py" --check --host-only 2>&1)"; then
  res PASS "host/kit.env and host/rendered/ match kit.toml"
else
  res FAIL "host/kit.env or host/rendered/ does not match kit.toml: $(grep -v '^[0-9]* stale' <<<"$render_out" | tr '\n' ' ')(python3 kit/render.py)"
fi
# The keys render owns in .env and .env.bridge, by name (never a value). A compose key in .env that differs from
# kit.toml is live at the next `up -d minecraft` (docker-compose.yml reads it as ${KEY:-default}), render or not.
if env_out="$(python3 -B "${REPO}/kit/render.py" --check --env-only 2>&1)"; then
  res PASS ".env and .env.bridge hold kit.toml's keys"
else
  res FAIL ".env or .env.bridge does not match kit.toml: $(grep -v '^[0-9]* stale' <<<"$env_out" | tr '\n' ' ')(python3 kit/render.py)"
fi
if cmp -s "$KIT_ENV" /etc/mc-host/kit.env; then
  expect "/etc/mc-host/kit.env owner+mode" "root:${MC_USER} 640" "$(stat -c '%U:%G %a' /etc/mc-host/kit.env)"
else
  res FAIL "/etc/mc-host/kit.env missing, unreadable or differs from host/kit.env (the host/bin scripts read it; sudo bash host/setup.sh)"
fi
for f in "${HOST_DIR}"/bin/mc-*; do
  dst="/usr/local/sbin/$(basename "$f")"
  if cmp -s "$f" "$dst" && [ -x "$dst" ]; then res PASS "$dst"; else res FAIL "$dst missing, not executable, or differs from the repo"; fi
done
for f in "${UNIT_SRC}"/*.service "${UNIT_SRC}"/*.timer "${UNIT_SRC}"/*.d/*.conf; do
  rel="${f#"${UNIT_SRC}"/}"
  if cmp -s "$f" "${UNIT_DIR}/${rel}"; then res PASS "${UNIT_DIR}/${rel}"; else res FAIL "${UNIT_DIR}/${rel} missing or differs from host/rendered/systemd"; fi
done

# ---- timers and units -------------------------------------------------------------------------
section "timers and units"
for t in mc-netwatch.timer mc-maint.timer mc-heal.timer; do
  en="$(systemctl is-enabled "$t" 2>/dev/null)" ac="$(systemctl is-active "$t" 2>/dev/null)"
  if [ "$en" = enabled ] && [ "$ac" = active ]; then
    res PASS "${t} enabled+active, next $(next_run "$t")"
  else
    res FAIL "${t} is-enabled=${en:-none} is-active=${ac:-none}"
  fi
done
# >>> heal state: mc-heal's own reading of its state file and the maintenance flag (one home for both formats).
# Fail closed: no output or a line that is not INFO/WARN is a FAIL.
if [ -x /usr/local/sbin/mc-heal ]; then heal_bin=/usr/local/sbin/mc-heal; else heal_bin="${HOST_DIR}/bin/mc-heal"; fi
heal_out="$(MC_REPO="$REPO" "$heal_bin" --status 2>&1)"
heal_lines=0
while IFS= read -r line; do
  [ -n "$line" ] || continue
  heal_lines=$((heal_lines + 1))
  case "${line%%$'\t'*}" in
    INFO | WARN) res "${line%%$'\t'*}" "mc-heal: ${line#*$'\t'}" ;;
    *) res FAIL "mc-heal --status: unexpected output: ${line}" ;;
  esac
done <<< "$heal_out"
[ "$heal_lines" -gt 0 ] || res FAIL "mc-heal --status printed nothing: cannot show the heal state"
# <<< heal state
for u in mc-netwatch.service mc-maint.service mc-heal.service mc-backup.service mc-server.service; do
  r="$(systemctl show "$u" -p Result --value 2>/dev/null)"
  expect "${u} last result" success "$r"
done
en="$(systemctl is-enabled mc-backup.timer 2>/dev/null)"
if [ "$en" = enabled ]; then
  res PASS "mc-backup.timer enabled, next $(next_run mc-backup.timer)"
elif [ "$en" = disabled ]; then
  res WARN "mc-backup.timer installed but disabled (enable at production stand-up)"
else
  res FAIL "mc-backup.timer is-enabled=${en:-not installed}"
fi
en="$(systemctl is-enabled mc-server.service 2>/dev/null)"
if [ "$en" = enabled ]; then
  res PASS "mc-server.service enabled ($(systemctl is-active mc-server.service 2>/dev/null))"
elif [ "$en" = disabled ]; then
  res WARN "mc-server.service installed but disabled (enable at production stand-up; until then reboots rely on mc-maint's own graceful stop)"
else
  res FAIL "mc-server.service is-enabled=${en:-not installed}"
fi
if [ -e "${MC_HOME}/.config/systemd/user/mc-backup.timer" ] || [ -e "${MC_HOME}/.config/systemd/user/mc-backup.service" ]; then
  res FAIL "old --user mc-backup units still in ~${MC_USER}/.config/systemd/user/"
else
  res PASS "no --user mc-backup units left"
fi

# ---- linger + charge cap ----------------------------------------------------------------------
section "linger and battery charge cap"
# >>> linger
if [ -e "${LINGER_DIR}/${MC_USER}" ]; then
  res PASS "linger on for ${MC_USER}"
else
  res FAIL "linger off for ${MC_USER} (sudo bash host/setup.sh, step 12)"
fi
# <<< linger
# >>> charge cap
if [ "$MC_CHARGE_CAP" = none ]; then
  if [ -e "${UNIT_DIR}/mc-charge-cap.service" ]; then
    res FAIL "no charge cap in kit.toml, but ${UNIT_DIR}/mc-charge-cap.service is installed (sudo bash host/setup.sh removes it)"
  else
    res INFO "no battery charge cap configured (kit.toml battery.charge_cap)"
  fi
else
  cap_en="$(systemctl is-enabled mc-charge-cap.service 2>/dev/null)"
  cap_res="$(systemctl show mc-charge-cap.service -p Result --value 2>/dev/null)"
  cap_act="$(systemctl show mc-charge-cap.service -p ActiveState --value 2>/dev/null)"
  if [ "$cap_en" = enabled ] && [ "$cap_act" = active ] && [ "$cap_res" = success ]; then
    # Written, never verified: drivers round the value, and asus-wmi reads back its own cached copy.
    res PASS "charge cap ${MC_CHARGE_CAP} WRITTEN this boot by mc-charge-cap.service (not verified: the readback is the driver's)"
  else
    res FAIL "mc-charge-cap.service is-enabled=${cap_en:-none} ActiveState=${cap_act:-none} Result=${cap_res:-none}: cap ${MC_CHARGE_CAP} not written this boot (journalctl -u mc-charge-cap.service)"
  fi
fi
# <<< charge cap

# ---- docker -----------------------------------------------------------------------------------
section "docker"
expect "docker info LiveRestoreEnabled" true "$(docker info --format '{{.LiveRestoreEnabled}}' 2>&1)"
# A malformed .env.bridge breaks EVERY compose config/exec call (not `up -d minecraft` or `stop`; backup.sh and
# world.sh avoid compose). Its error text is never shown here: it can quote the file.
if (cd "$REPO" && docker compose config -q >/dev/null 2>&1); then
  res PASS "docker compose config parses"
else
  res FAIL "docker compose config FAILS: most likely .env.bridge (a malformed line, wrong owner or mode, or a directory; see its shape-only lines in 'bridge mailbox' below) or .env (python3 kit/render.py --check names the key). Never print compose's own error: it can quote a token"
fi
grace="$(cd "$REPO" && docker compose config --format json 2>&1 | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin)["services"]["minecraft"].get("stop_grace_period"))
except Exception as e:
    print(f"compose config unreadable: {e}")')"
expect "compose minecraft stop_grace_period" 2m0s "$grace"
stop_duration="$(cd "$REPO" && docker compose config --format json 2>&1 | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin)["services"]["minecraft"]["environment"].get("STOP_DURATION"))
except Exception as e:
    print(f"compose config unreadable: {e}")')"
expect "compose minecraft STOP_DURATION (mc-server-runner kill deadline)" 110 "$stop_duration"
# >>> extras: the ve-commands jar (mod/check_extras.py). A MODS jar missing from extras/ stops minecraft from
# starting at all. Fail closed: no output, a stray line, or a non-zero exit without a FAIL line is itself a FAIL.
# The wiring is tested by mod/test_check_extras.py.
extras_out="$(cd "$REPO" && docker compose config --format json 2>/dev/null | python3 "${REPO}/mod/check_extras.py" "$REPO" 2>&1)"
extras_rc=$?
extras_lines=0
extras_failed=0
while IFS= read -r line; do
  [ -n "$line" ] || continue
  extras_lines=$((extras_lines + 1))
  st="${line%%$'\t'*}"
  case "$st" in
    PASS | WARN) res "$st" "${line#*$'\t'}" ;;
    FAIL) res FAIL "${line#*$'\t'}"; extras_failed=1 ;;
    *) res FAIL "extras check: unexpected output: ${line}"; extras_failed=1 ;;
  esac
done <<< "$extras_out"
if [ "$extras_lines" -eq 0 ]; then
  res FAIL "extras check printed nothing (exit ${extras_rc}): cannot verify the ve-commands jar"
elif [ "$extras_rc" -ne 0 ] && [ "$extras_failed" -eq 0 ]; then
  res FAIL "extras check exited ${extras_rc} without a FAIL line: cannot verify the ve-commands jar"
fi
# <<< extras
# The container scope's own stop timeout is the real ceiling at shutdown (docker-.scope.d drop-in).
mc_id="$(docker ps -q --no-trunc --filter label=com.docker.compose.service=minecraft --filter status=running 2>/dev/null | head -1)"
if [ -n "$mc_id" ]; then
  scope_show="$(systemctl show "docker-${mc_id}.scope" -p LoadState -p TimeoutStopUSec 2>&1 | tr '\n' ' ')"
  if grep -q "LoadState=loaded" <<<"$scope_show" && grep -q "TimeoutStopUSec=3min " <<<"$scope_show"; then
    res PASS "running container scope docker-${mc_id:0:12}….scope: ${scope_show}"
  else
    res FAIL "running container scope docker-${mc_id:0:12}….scope: ${scope_show}(want LoadState=loaded TimeoutStopUSec=3min; cgroup driver: $(docker info --format '{{.CgroupDriver}}' 2>&1))"
  fi
elif cmp -s "${UNIT_SRC}/docker-.scope.d/10-mc-host.conf" "${UNIT_DIR}/docker-.scope.d/10-mc-host.conf"; then
  res PASS "no minecraft container running; ${UNIT_DIR}/docker-.scope.d/10-mc-host.conf present (TimeoutStopSec=180)"
else
  res FAIL "no minecraft container running, and ${UNIT_DIR}/docker-.scope.d/10-mc-host.conf is missing or differs"
fi

# ---- update window ----------------------------------------------------------------------------
section "maintenance window"
for pair in "apt-daily.timer 04:30:00" "apt-daily-upgrade.timer 05:00:00"; do
  t="${pair% *}" at="${pair#* }"
  cal="$(systemctl show "$t" -p TimersCalendar --value 2>/dev/null | grep -o 'OnCalendar=[^;]*' | sed 's/ *$//' | tr '\n' '|')"
  expect "${t} calendar" "OnCalendar=*-*-* ${at}|" "$cal"
  expect "${t} RandomizedDelayUSec" 0 "$(systemctl show "$t" -p RandomizedDelayUSec --value 2>/dev/null)"
  res INFO "${t} next $(next_run "$t")"
done
patterns="$(apt-config dump Unattended-Upgrade::Origins-Pattern 2>/dev/null)"
# ${distro_codename} is literal here: apt substitutes it, not the shell.
# shellcheck disable=SC2016
p='origin=Tailscale,label=Tailscale,codename=${distro_codename},component=main'
if grep -qF "\"${p}\"" <<<"$patterns"; then res PASS "Origins-Pattern has ${p}"; else res FAIL "Origins-Pattern lacks ${p}"; fi
# Docker is never upgraded unattended (live-restore does not survive minor/major dockerd upgrades).
if grep -qi 'origin=Docker' <<<"$patterns"; then
  res FAIL "Origins-Pattern allows Docker: $(grep -i 'origin=Docker' <<<"$patterns" | head -1)"
else
  res PASS "Origins-Pattern has no Docker origin"
fi
blacklist="$(apt-config dump Unattended-Upgrade::Package-Blacklist 2>/dev/null)"
expect "Unattended-Upgrade::Automatic-Reboot" false "$(apt-config dump 2>/dev/null | sed -n 's/^Unattended-Upgrade::Automatic-Reboot "\(.*\)";$/\1/p' | tail -1)"
# The patterns must match a CONFIGURED apt source, using unattended-upgrades' own matcher.
while IFS=$'\t' read -r st msg; do res "$st" "$msg"; done < <(python3 - "$patterns" "$blacklist" <<'PY'
import importlib.machinery, importlib.util, re, sys
try:
    import apt_pkg
    loader = importlib.machinery.SourceFileLoader("uu", "/usr/bin/unattended-upgrade")
    spec = importlib.util.spec_from_loader("uu", loader)
    uu = importlib.util.module_from_spec(spec)
    loader.exec_module(uu)
    apt_pkg.init()
    files = [f for f in apt_pkg.Cache(progress=None).file_list if f.origin]
except Exception as exc:
    print(f"FAIL\tcannot load apt/unattended-upgrade to test the patterns: {exc}")
    sys.exit(0)
configured = re.findall(r'^Unattended-Upgrade::Origins-Pattern:: "(.+)";$', sys.argv[1], re.M)
pats = [p for p in configured if p.startswith("origin=Tailscale,")]
if not pats:
    print("FAIL\tno Tailscale Origins-Pattern configured to test")
else:
    hits = sorted({f"{f.site} {f.archive or f.codename}/{f.component}" for f in files if uu.match_whitelist_string(pats[0], f)})
    print(f"PASS\tapt source matched by {pats[0]}: {', '.join(hits)}" if hits else f"FAIL\tno configured apt source matches {pats[0]}")
# The blacklist must stop every Docker package, judged by unattended-upgrades' own blacklist function.
blacklist = re.findall(r'^Unattended-Upgrade::Package-Blacklist:: "(.+)";$', sys.argv[2], re.M)
docker_pkgs = ["docker-ce", "docker-ce-cli", "docker-ce-rootless-extras", "containerd.io",
               "docker-buildx-plugin", "docker-compose-plugin"]
missed = [p for p in docker_pkgs if not uu.is_pkgname_in_blacklist(p, blacklist)]
print(f"FAIL\tPackage-Blacklist does not stop {', '.join(missed)} (entries: {blacklist})" if missed
      else f"PASS\tPackage-Blacklist stops {', '.join(docker_pkgs)}")
PY
)
# Pending Docker upgrades are applied by hand with the server stopped (AGENTS.md); report them.
pending="$(apt list --upgradable 2>/dev/null | grep -E '^(docker-[a-z-]+|containerd\.io)/' | cut -d' ' -f1,2 | tr '\n' ' ')"
if [ -n "$pending" ]; then
  res INFO "Docker upgrades pending (manual stopped-server procedure, AGENTS.md): ${pending}"
else
  res INFO "no Docker upgrade pending"
fi
ts_apply="$(tailscale debug prefs 2>&1 | python3 -c 'import json,sys
try:
    print((json.load(sys.stdin).get("AutoUpdate") or {}).get("Apply"))
except Exception as e:
    print(f"unreadable: {e}")')"
expect "tailscale AutoUpdate.Apply" False "$ts_apply"
if command -v snap >/dev/null; then
  sv="$(as_root snap get system refresh.timer 2>&1)"
  if [ $? -eq 99 ]; then res SKIP "snap refresh.timer (needs sudo)"; else expect "snap refresh.timer" 05:00-05:30 "$sv"; fi
fi

# ---- ssh --------------------------------------------------------------------------------------
section "ssh"
if [ "$NO_SSH" = 1 ]; then
  # No SSH wanted: nothing may listen for it. Both units must read exactly "inactive" (a unit that does not exist
  # does too); anything else, or a systemctl that does not answer, FAILs.
  ssh_state="$(systemctl is-active ssh.service ssh.socket 2>/dev/null | xargs)"
  if [ "$ssh_state" = "inactive inactive" ]; then
    res PASS "--no-ssh: no SSH server running (ssh.service, ssh.socket: ${ssh_state})"
  else
    res FAIL "--no-ssh, but ssh.service/ssh.socket are '${ssh_state}': make it key-only (setup.sh step 8) and run check.sh without --no-ssh, or stop and disable them"
  fi
  res SKIP "key-only sshd config (--no-ssh: no SSH access wanted)"
else
  if cmp -s "${HOST_DIR}/conf/sshd-10-mc-host.conf" /etc/ssh/sshd_config.d/10-mc-host.conf; then
    res PASS "/etc/ssh/sshd_config.d/10-mc-host.conf matches the repo"
  else
    res FAIL "/etc/ssh/sshd_config.d/10-mc-host.conf missing or differs"
  fi
  sshd_t="$(as_root sshd -T 2>&1)"
  rc=$?
  if [ "$rc" -eq 99 ]; then
    res SKIP "sshd -T effective config (needs sudo; run 'sudo bash host/check.sh' for it)"
  elif [ "$rc" -ne 0 ]; then
    res FAIL "sshd -T failed: ${sshd_t:0:200}"
  else
    for kv in "passwordauthentication no" "kbdinteractiveauthentication no" "permitrootlogin no" "pubkeyauthentication yes"; do
      if grep -qx "$kv" <<<"$sshd_t"; then res PASS "sshd -T: ${kv}"; else res FAIL "sshd -T: want '${kv}', have '$(grep "^${kv% *} " <<<"$sshd_t")'"; fi
    done
  fi
fi

# ---- bridge mailbox ---------------------------------------------------------------------------
section "bridge mailbox"
# The directories only (owner, mode, not a link anywhere on the path). The jar inside extras/ is checked once,
# by mod/check_extras.py in the docker section.
if [ -L "${REPO}/mc_data" ]; then
  res FAIL "${REPO}/mc_data is a symlink (must be a real directory)"
fi
for d in "${REPO}/mc_data/mcve" "${REPO}/mc_data/mcve/out" "${REPO}/mc_data/mcve/in" "${REPO}/extras"; do
  if [ -L "$d" ] || { [ -e "$d" ] && [ "$(realpath -m "$d")" != "$(realpath -m "$REPO")${d#"$REPO"}" ]; }; then
    res FAIL "${d} is (or runs through) a symlink (must be a real directory)"
  elif [ -d "$d" ]; then
    expect "${d} owner+mode" "${MC_USER}:${MC_USER} 755" "$(stat -c '%U:%G %a' "$d")"
  else
    res FAIL "${d} missing (sudo bash host/setup.sh creates it)"
  fi
done
# A compromised bridge can fill in/ (it sits on the world's filesystem): the accepted residual risk, alarmed here.
mcve_in="${REPO}/mc_data/mcve/in"
if [ -d "$mcve_in" ] && [ ! -L "$mcve_in" ]; then
  in_mib=$(( $(du -sb "$mcve_in" 2>/dev/null | cut -f1) / 1048576 ))
  if [ "$in_mib" -gt 500 ]; then res FAIL "${mcve_in} is ${in_mib} MiB (> 500: inbox flood? bridge/README.md)"
  elif [ "$in_mib" -gt 50 ]; then res WARN "${mcve_in} is ${in_mib} MiB (> 50)"
  else res PASS "${mcve_in} is ${in_mib} MiB"; fi
fi
if [ -d "${REPO}/mc_data" ]; then
  free_gib=$(( $(df --output=avail -B1 "${REPO}/mc_data" | tail -1) / 1073741824 ))
  if [ "$free_gib" -lt 5 ]; then res FAIL "mc_data filesystem has ${free_gib} GiB free (< 5: world saves and backups at risk)"
  else res PASS "mc_data filesystem has ${free_gib} GiB free"; fi
fi
# itzg (the mod) and the bridge container both write mc_data/mcve as uid 1000.
expect "${MC_USER} uid (= itzg and bridge uid)" 1000 "$(id -u "$MC_USER")"
# Running is not enough: during a FATAL hold (bad token, missing channel, no fence) it runs for 600 s. Healthy
# means ready on Discord with the pump turning (compose healthcheck on /tmp/ready). Found by label, not compose.
bridge_id="$(docker ps -aq --filter label=com.docker.compose.project=minecraft-ai-host --filter label=com.docker.compose.service=bridge 2>/dev/null | head -1)"
if [ -z "$bridge_id" ] && [ "$DISCORD" = off ]; then
  res INFO "no bridge container: no Discord on this server (kit.toml discord.enabled = false)"
elif [ -z "$bridge_id" ]; then
  res WARN "bridge container not created yet (bridge/README.md, Deploy)"
else
  bridge_health="$(docker inspect --format '{{.State.Status}}/{{if .State.Health}}{{.State.Health.Status}}{{else}}no-healthcheck{{end}}' "$bridge_id" 2>&1)"
  if [ "$bridge_health" = running/healthy ]; then
    res PASS "bridge container running/healthy"
  else
    res FAIL "bridge container ${bridge_health} (want running/healthy; docker compose logs bridge shows why, e.g. a FATAL hold)"
  fi
fi
# The bridge's token file: shape and permissions only, never its values.
envb="${REPO}/.env.bridge"
if [ -L "$envb" ] || { [ -e "$envb" ] && [ ! -f "$envb" ]; }; then
  res FAIL "${envb} is not a regular file (a link or a directory breaks every compose call)"
elif [ -f "$envb" ]; then
  expect "${envb} owner+mode" "${MC_USER}:${MC_USER} 600" "$(stat -c '%U:%G %a' "$envb")"
  if [ ! -r "$envb" ]; then
    res FAIL "${envb} is not readable by $(id -un) (compose can't parse it)"
  else
    bad="$(grep -nvE '^(#.*|[A-Z_][A-Z0-9_]*=[^[:space:]]*)?$' "$envb" | cut -d: -f1 | tr '\n' ' ')"
    if [ -n "$bad" ]; then res FAIL "${envb} malformed line(s) ${bad}(want KEY=VALUE, no spaces; values not shown)"
    else res PASS "${envb} is KEY=VALUE lines"; fi
    for k in DISCORD_BRIDGE_TOKEN DISCORD_GUILD_ID; do
      grep -qE "^${k}=[^[:space:]]+\$" "$envb" || res FAIL "${envb} has no non-empty ${k}"
    done
  fi
elif [ "$DISCORD" = off ]; then
  res INFO "${envb} absent: no Discord on this server, so no bridge to log in"
else
  res WARN "${envb} missing: the bridge can't log in (bridge/README.md, Deploy)"
fi

# ---- bridge fence -----------------------------------------------------------------------------
section "bridge fence"
# The bridge reaches the Internet only (host/bin/mc-bridge-fence): no host, LAN or tailnet, so no relay to 25565.
expect "mc-bridge-fence.service enabled" enabled "$(systemctl is-enabled mc-bridge-fence.service 2>&1)"
if fence="$(as_root /usr/local/sbin/mc-bridge-fence check 2>&1)"; then
  res PASS "$fence"
elif [ $? -eq 99 ]; then
  res SKIP "bridge fence drops not read (needs sudo: sudo bash host/check.sh)"
else
  res FAIL "bridge fence: $(tr '\n' ' ' <<<"$fence")(sudo bash host/setup.sh, step 11)"
fi
# The fence is keyed on the interface name: the live network must carry it.
want_if="$(bash "${HOST_DIR}/bin/mc-bridge-fence" print | sed -n 's/^iface //p')"
have_if="$(docker network inspect minecraft-ai-host_discord --format '{{index .Options "com.docker.network.bridge.name"}}' 2>/dev/null || true)"
if [ -z "$have_if" ] && [ "$DISCORD" = off ]; then
  res INFO "network minecraft-ai-host_discord absent: no Discord on this server, so no bridge to start it"
elif [ -z "$have_if" ]; then
  res WARN "network minecraft-ai-host_discord not created yet (it is, with the bridge's first up)"
else
  expect "bridge network interface (the fence's anchor)" "$want_if" "$have_if"
fi

# ---- alerts -----------------------------------------------------------------------------------
section "alerts"
wh=/etc/mc-host/webhook
if [ "$DISCORD" = off ]; then
  # Chosen at the kit's card, not owed (docs/kit/KIT.md "With no Discord"): said plainly, never a PASS.
  res INFO "alerts are OFF: no Discord on this server (kit.toml discord.enabled = false). The watchdogs still restart and repair; nobody is told when something fails"
elif [ ! -e "$wh" ]; then
  res FAIL "${wh} missing (alerts only reach the journal); see setup.sh next steps"
else
  meta="$(stat -c '%U:%G %a' "$wh")"
  expect "${wh} owner+mode" "root:${MC_USER} 640" "$meta"
  if [ -r "$wh" ] && grep -Eq '^https://discord(app)?\.com/api/webhooks/' "$wh"; then
    res PASS "${wh} holds a Discord webhook URL (not shown)"
  else
    res FAIL "${wh} unreadable by $(id -un) or not a Discord webhook URL"
  fi
fi

# ---- info -------------------------------------------------------------------------------------
section "info"
if [ -e /var/run/reboot-required ]; then
  res INFO "reboot required ($(tr '\n' ' ' </var/run/reboot-required.pkgs 2>/dev/null)); mc-maint handles it at 05:40"
else
  res INFO "no reboot required"
fi

echo
if [ "$FAILS" -gt 0 ]; then
  echo "${FAILS} FAIL(s)"
  exit 1
fi
echo "all checks passed"
