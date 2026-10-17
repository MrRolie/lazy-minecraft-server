#!/usr/bin/env bash
# kit/probe.sh: KIT.md P0, the read-only probe of a would-be game host.
#
#   bash kit/probe.sh [--accept-unproven-ram] [--out FILE]      as the kit user (never root)
#
# It reads the machine and changes nothing: RAM, CPU, disk free under the repo, uid/gid, Ubuntu release, kernel,
# NetworkManager and its connections, the NICs, the battery charge-cap sysfs, and the IPv4 routes and addresses
# (for an overlap with the bridge network 10.213.37.0/24, a kit constant). This script only OBSERVES: it writes
# raw facts, and kit/probe.py JUDGES them (the REFUSE gates) and writes the JSON (default kit/probe.json,
# gitignored). A fact it cannot read is written as unknown, and an unknown gated fact is a refusal, never a pass.
#
# Exit: 0 every gate passed; 1 refused (the JSON says why); 2 usage, python3 missing, or the observer failed.
# Tests point it at fixtures: PROBE_ROOT prefixes /proc, /sys and /etc/os-release; commands come from PATH.
set -uo pipefail

KIT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "${KIT_DIR}/.." && pwd)"
ROOT="${PROBE_ROOT:-}"
OUT="${KIT_DIR}/probe.json"
ACCEPT=()
while [ $# -gt 0 ]; do
  case "$1" in
    --accept-unproven-ram) ACCEPT=(--accept-unproven-ram); shift ;;
    --out) OUT="${2:?--out needs a file}"; shift 2 ;;
    *) echo "usage: bash kit/probe.sh [--accept-unproven-ram] [--out FILE]" >&2; exit 2 ;;
  esac
done
if ! command -v python3 >/dev/null; then
  echo "probe: python3 is missing (Ubuntu ships it; sudo apt-get install python3), and the kit needs it throughout" >&2
  exit 2
fi

# fact KEY VALUE: one raw fact, tab-separated. A key may repeat (a list). Tabs and newlines in VALUE are flattened.
fact() { printf '%s\t%s\n' "$1" "$(tr '\t\n' '  ' <<<"$2" | sed 's/ *$//')"; }

facts() {
  local v line f dev kind name typ
  fact euid "$(id -u 2>/dev/null)"
  fact user "$(id -un 2>/dev/null)"
  fact uid "$(id -u 2>/dev/null)"
  fact gid "$(id -g 2>/dev/null)"
  fact hostname "$(hostname 2>/dev/null)"
  fact home "$(getent passwd "$(id -un 2>/dev/null)" 2>/dev/null | cut -d: -f6)"
  fact repo "$REPO"
  fact tz "$(timedatectl show -p Timezone --value 2>/dev/null)"
  if [ -r "${ROOT}/etc/os-release" ]; then
    # shellcheck disable=SC1090,SC1091
    v="$(. "${ROOT}/etc/os-release" && printf '%s\t%s\t%s' "${ID:-}" "${UBUNTU_CODENAME:-${VERSION_CODENAME:-}}" "${VERSION_ID:-}")"
    fact os_id "$(cut -f1 <<<"$v")"
    fact os_codename "$(cut -f2 <<<"$v")"
    fact os_version "$(cut -f3 <<<"$v")"
  fi
  fact kernel "$(uname -r 2>/dev/null)"
  fact mem_total_kib "$(awk '$1 == "MemTotal:" { print $2 }' "${ROOT}/proc/meminfo" 2>/dev/null)"
  fact threads "$(nproc --all 2>/dev/null)"
  fact cores "$(lscpu -p=CORE,SOCKET 2>/dev/null | grep -v '^#' | sort -u | grep -c .)"
  fact disk_free_bytes "$(df -B1 --output=avail "$REPO" 2>/dev/null | tail -1 | tr -d ' ')"
  fact nm_active "$(systemctl is-active NetworkManager 2>/dev/null)"
  if command -v nmcli >/dev/null; then
    fact nmcli yes
    # -t escapes a ':' inside a field as '\:'; split on the unescaped ones.
    while IFS= read -r line; do
      [ -n "$line" ] || continue
      name="$(sed -E 's/^((\\.|[^\\:])*):.*/\1/; s/\\:/:/g' <<<"$line")"
      typ="$(sed -E 's/^(\\.|[^\\:])*:([^:]*).*/\2/' <<<"$line")"
      case "$typ" in
        802-3-ethernet) fact nm_wired "$name" ;;
        802-11-wireless) fact nm_wifi "$name" ;;
      esac
    done < <(nmcli -t -f NAME,TYPE connection show 2>/dev/null)
  else
    fact nmcli no
  fi
  for dev in "${ROOT}"/sys/class/net/*; do
    [ -e "$dev" ] || continue
    name="$(basename "$dev")"
    if [ -e "${dev}/wireless" ] || [ -e "${dev}/phy80211" ]; then kind=wifi
    elif [ -e "${dev}/device" ]; then kind=ethernet
    else kind=virtual; fi
    [ "$kind" = virtual ] || fact nic "${name} ${kind}"
  done
  # The overlap gate judges these lists, so an empty list must mean "read, and empty", never "could not read":
  # ip_ok says the two reads worked (a container without iproute2 has no `ip` at all).
  local addrs routes
  if addrs="$(ip -4 -o addr show 2>/dev/null)" && routes="$(ip -4 route show table all 2>/dev/null)"; then
    fact ip_ok yes
  else
    fact ip_ok no
    addrs="" routes=""
  fi
  while IFS= read -r line; do  # "2: eno1    inet 192.168.1.205/24 brd ..." -> "192.168.1.205/24 eno1"
    [ -n "$line" ] || continue
    fact ipv4 "$(awk '{ print $4, $2 }' <<<"$line")"
  done <<<"$addrs"
  while IFS= read -r line; do  # "[type] DEST ... dev DEV ..." -> "DEST DEV"
    [ -n "${line// /}" ] || continue
    # shellcheck disable=SC2086  # word-split on purpose
    set -- $line
    case "$1" in local | broadcast | unreachable | blackhole | prohibit | throw | multicast | anycast | unicast) shift ;; esac
    f="$1"
    dev="$(sed -nE 's/.* dev ([^ ]+).*/\1/p' <<<"$line")"
    [ "$f" = default ] || fact route "${f} ${dev:--}"
  done <<<"$routes"
  for f in "${ROOT}"/sys/class/power_supply/*; do
    [ -e "$f" ] || continue
    [ "$(cat "${f}/type" 2>/dev/null)" = Battery ] && fact battery "$(basename "$f")"
    [ -e "${f}/charge_control_end_threshold" ] && fact charge_threshold "${f#"${ROOT}"}/charge_control_end_threshold"
    [ -e "${f}/charge_types" ] && fact charge_types "${f#"${ROOT}"}/charge_types"
  done
  true
}

# The observer must finish: a probe that died half-way would hand the judge a machine with facts missing.
if ! observed="$(facts)"; then
  echo "probe: reading the machine failed (above); no verdict written" >&2
  exit 2
fi
printf '%s\n' "$observed" | python3 "${KIT_DIR}/probe.py" --out "$OUT" "${ACCEPT[@]}"
