# shellcheck shell=bash
# Pure helpers for the wired static IP, sourced by host/setup.sh and host/check.sh and tested by
# host/test_host.py. No side effects apart from write_prev's one file.

# norm_list STR: "a,b" or "a  b" -> "a b" (nmcli -g separates list values with commas).
norm_list() { tr ',' ' ' <<<"$1" | xargs; }

# wired_at_target METHOD ADDRS GW DNS WANT_ADDR WANT_GW WANT_DNS: exit 0 if nmcli's current ipv4
# values already are the target, so setup.sh leaves the connection alone.
wired_at_target() {
  [ "$1" = manual ] && [ "$(norm_list "$2")" = "$5" ] && [ "$3" = "$6" ] && [ "$(norm_list "$4")" = "$7" ]
}

# probe_verdict CONTROL_NEIGH TARGET_ANSWERED TARGET_NEIGH: prints broken | in-use | free.
#   CONTROL_NEIGH   `ip neigh show <gateway> dev <dev>` after pinging it: must carry an lladdr, or the probe
#                   itself is not working and proves nothing (broken)
#   TARGET_ANSWERED 1 if `ping <target>` got a reply
#   TARGET_NEIGH    `ip neigh show <target> dev <dev>` after that ping (a host that drops ping still answers
#                   ARP, which leaves an lladdr)
probe_verdict() {
  if ! grep -q lladdr <<<"$1"; then
    echo broken
  elif [ "$2" = 1 ] || grep -q lladdr <<<"$3"; then
    echo in-use
  else
    echo free
  fi
}

# conflict_lines: stdin -> the NetworkManager address-conflict (ACD) lines. NM 1.46 logs one of these
# (strings from its binary) when it loses an address to another host; it then drops the address and
# re-probes. A successful defence is logged only at trace level.
conflict_lines() { grep -iE 'conflict detected|address conflict for .* detected|sees conflict' || true; }

# lines_for_ip IP: stdin -> the lines naming exactly IP (192.0.2.5 does not match 192.0.2.50).
lines_for_ip() {
  local re="${1//./\\.}"
  grep -E "(^|[^0-9.])${re}([^0-9]|\$)" || true
}

# write_prev FILE CONN METHOD ADDRS GW DNS: the settings mc-net-commit rollback restores.
write_prev() {
  printf 'conn=%s\nmethod=%s\naddresses=%s\ngateway=%s\ndns=%s\n' "$2" "$3" "$4" "$5" "$6" >"$1"
}
