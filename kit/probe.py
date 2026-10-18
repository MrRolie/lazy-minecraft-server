#!/usr/bin/env python3
"""kit/probe.py: the judge half of KIT.md P0. kit/probe.sh observes and pipes raw
facts here (tab-separated KEY VALUE lines on stdin; a key may repeat); this applies the REFUSE gates and writes the
probe JSON. Pure apart from reading stdin and writing --out, so the gates are tested on fact fixtures.

REFUSE (exit 1), each with the reason in the JSON:
  - run as root (the probe, render and the checks run as the kit user)
  - uid or gid other than 1000 (itzg and the bridge image write the mailbox as 1000)
  - not Ubuntu (every later phase is apt, NetworkManager and unattended-upgrades)
  - python3 older than PYTHON_FLOOR (3.11: the kit's config reader imports tomllib; Ubuntu 22.04 ships 3.10). This
    file and kit/record.py, the first kit command a run executes, must themselves run on an older python3 so they can
    refuse it: stdlib only, no 3.11-only syntax or imports here
  - NetworkManager not active, or nmcli missing (host/setup.sh's network step is nmcli throughout)
  - no network connection NetworkManager could configure (no wired and no Wi-Fi connection)
  - an IPv4 route or address overlapping 10.213.37.0/24, the bridge network (a kit constant; br-mcve's own routes and
    addresses inside it, on an already-installed host, are not an overlap)
  - under 50 GB free on the repo's filesystem (world, 7 daily backups, images; mc-maint alarms under 20 GiB)
  - under RAM_FLOOR_MIB of RAM, unless --accept-unproven-ram: 16 GB with an 8G heap is the only proven
    configuration (asus-node); below it is an estimate (research (i))
A gated fact that could not be read is "unknown", and unknown refuses: a probe that cannot see must not pass.
"""
import argparse
import ipaddress
import json
import os
import sys

BRIDGE_NET = ipaddress.IPv4Network("10.213.37.0/24")  # = docker-compose.yml networks.discord, mc-bridge-fence SUBNET
BRIDGE_IFACE = "br-mcve"
KIT_UID = 1000
DISK_FLOOR_BYTES = 50 * 10 ** 9
# A "16 GB" laptop reports less than 16 GiB as MemTotal: firmware and the integrated GPU keep some (asus-node's 16 GB
# shows as 15 GiB in free -h). 14 GiB keeps every 16 GB machine in and every 12 GB one (about 11.5 GiB) out.
RAM_FLOOR_MIB = 14 * 1024
# The kit's Python floor: kit/kitconfig.py and kit/tailscale.py import tomllib (3.11). Ubuntu 24.04 ships 3.12,
# 22.04 ships 3.10. kit/record.py refuses below it too (python_floor_problem), so a run on old Python stops at its
# first command.
PYTHON_FLOOR = (3, 11)
PROVEN_CODENAME = "noble"  # Ubuntu 24.04: asus-node, and the Tailscale Origins-Pattern (host/conf/apt-52mc-host)
LISTS = ("nm_wired", "nm_wifi", "nic", "ipv4", "route", "battery", "charge_threshold", "charge_types")


def parse(lines):
    """Tab-separated facts -> {key: str, or [str] for LISTS}. An empty value is unknown (None)."""
    facts = {k: [] for k in LISTS}
    for line in lines:
        line = line.rstrip("\n")
        if not line:
            continue
        key, _, value = line.partition("\t")
        if key in LISTS:
            if value:
                facts[key].append(value)
        else:
            facts[key] = value or None
    return facts


def _int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return None


def python_floor_problem(version):
    """-> why this python3 is below the kit's floor, or None. version: a tuple of ints (sys.version_info works), or
    None when it could not be read (unknown refuses)."""
    floor = ".".join(map(str, PYTHON_FLOOR))
    if version is None:
        return f"python3 version unknown: the kit needs Python {floor} or newer"
    if tuple(version[:2]) >= PYTHON_FLOOR:
        return None
    return (f"python3 is {'.'.join(map(str, version[:3]))}, older than {floor}: the kit needs Python {floor} or newer, "
            "which Ubuntu 24.04 LTS and newer ship (22.04 has 3.10); install Ubuntu Desktop 24.04 LTS or newer")


def _version(v):
    """'3.10.12' -> (3, 10, 12); anything else -> None."""
    parts = (v or "").split(".")
    if len(parts) < 2 or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def overlaps(facts):
    """-> the routes/addresses (as 'CIDR dev') that overlap the bridge network, br-mcve's own excepted."""
    out = []
    for entry in facts["route"] + facts["ipv4"]:
        cidr, _, dev = entry.partition(" ")
        try:
            net = ipaddress.IPv4Network(cidr, strict=False)
        except ValueError:
            out.append(f"{entry} (unreadable)")  # cannot judge it, so it counts
            continue
        # br-mcve's own routes: the /24 link route, and the local-table /32s (its address, its broadcast) that
        # `ip -4 route show table all` also lists. A supernet on br-mcve is not the kit's bridge and still counts.
        if dev == BRIDGE_IFACE and net.subnet_of(BRIDGE_NET):
            continue
        if net.prefixlen > 0 and net.overlaps(BRIDGE_NET):
            out.append(entry)
    return out


def judge(facts, accept_unproven_ram=False):
    """-> (refusals, warnings, derived) for the facts."""
    refusals, warnings = [], []
    euid, uid, gid = _int(facts.get("euid")), _int(facts.get("uid")), _int(facts.get("gid"))
    if euid is None or uid is None or gid is None:
        refusals.append("uid/gid unknown (id failed)")
    else:
        if euid == 0:
            refusals.append("run as root: run the probe as the kit user, without sudo")
        elif uid != KIT_UID or gid != KIT_UID:
            refusals.append(f"uid/gid is {uid}/{gid}, not {KIT_UID}/{KIT_UID}: the bridge image and itzg write the "
                            "mailbox as 1000; use the first account the Ubuntu installer made")
    os_id, codename = facts.get("os_id"), facts.get("os_codename")
    if os_id != "ubuntu":
        refusals.append(f"OS is {os_id or 'unknown'}, not ubuntu: the kit installs with apt and runs on "
                        "NetworkManager (install Ubuntu Desktop 24.04 LTS or newer)")
    elif codename != PROVEN_CODENAME:
        warnings.append(f"Ubuntu {codename or 'unknown'}: the kit is proven on {PROVEN_CODENAME} (24.04) only")
    py = python_floor_problem(_version(facts.get("python3_version")))
    if py:
        refusals.append(py)
    if facts.get("nm_active") != "active" or facts.get("nmcli") != "yes":
        refusals.append(f"NetworkManager is not running (systemctl is-active: {facts.get('nm_active') or 'unknown'}; "
                        f"nmcli: {facts.get('nmcli') or 'unknown'}): host/setup.sh needs it (Ubuntu Desktop has it)")
    elif not facts["nm_wired"] and not facts["nm_wifi"]:
        refusals.append("NetworkManager has no wired or Wi-Fi connection to configure (nmcli connection show)")
    bad = overlaps(facts)
    if facts.get("ip_ok") != "yes":  # empty route and address lists would otherwise read as "no overlap"
        refusals.append("IPv4 routes and addresses unknown (ip -4 failed or is missing): the overlap with "
                        f"{BRIDGE_NET} cannot be judged")
    elif bad:
        refusals.append(f"{', '.join(bad)} overlaps {BRIDGE_NET}, the bridge network (a kit constant): the bridge "
                        "and the fence would collide with this network")
    disk = _int(facts.get("disk_free_bytes"))
    if disk is None:
        refusals.append("free disk unknown (df failed)")
    elif disk < DISK_FLOOR_BYTES:
        refusals.append(f"{disk / 10 ** 9:.1f} GB free on the repo's filesystem, under {DISK_FLOOR_BYTES // 10 ** 9} GB")
    kib = _int(facts.get("mem_total_kib"))
    ram_mib = None if kib is None else kib // 1024
    if ram_mib is None:
        refusals.append("RAM unknown (/proc/meminfo unreadable)")
    elif ram_mib < RAM_FLOOR_MIB:
        msg = (f"{ram_mib} MiB RAM, under the proven 16 GB class ({RAM_FLOOR_MIB} MiB): unproven for this pack "
               "(12 GB is an estimate, 8 GB likely too small)")
        if accept_unproven_ram:
            warnings.append(msg + "; accepted as UNPROVEN (--accept-unproven-ram)")
        else:
            refusals.append(msg + "; re-run with --accept-unproven-ram to go on as unproven")
    threads = _int(facts.get("threads"))
    if threads is not None and threads < 4:
        warnings.append(f"{threads} CPU threads: the proven host has 2 cores / 4 threads")
    if facts["battery"] and not facts["charge_threshold"]:
        warnings.append("battery without charge_control_end_threshold: no charge cap on this hardware or kernel "
                        "(battery.charge_cap = \"none\")" + ("; it has charge_types (IdeaPad-style conservation "
                                                              "mode), which the kit does not drive" if
                                                              facts["charge_types"] else ""))
    derived = {
        "ram_mib": ram_mib,
        "disk_free_gib": None if disk is None else disk // 2 ** 30,
        "net_wired_conn": facts["nm_wired"][0] if len(facts["nm_wired"]) == 1 else None,
        "net_wifi_conns": facts["nm_wifi"],
        "wifi_only": bool(facts["nm_wifi"]) and not facts["nm_wired"],
        "charge_cap_support": "threshold" if facts["charge_threshold"] else
                              ("charge_types" if facts["charge_types"] else "none"),
        "suggested_charge_cap": 80 if facts["charge_threshold"] else "none",
    }
    if len(facts["nm_wired"]) > 1:
        warnings.append(f"{len(facts['nm_wired'])} wired connections ({', '.join(facts['nm_wired'])}): pick one for "
                        "net.wired_conn")
    return refusals, warnings, derived


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--out", required=True)
    ap.add_argument("--accept-unproven-ram", action="store_true")
    args = ap.parse_args(argv)
    facts = parse(sys.stdin)
    refusals, warnings, derived = judge(facts, args.accept_unproven_ram)
    doc = {"verdict": "refused" if refusals else "pass", "refusals": refusals, "warnings": warnings,
           "accepted_unproven_ram": args.accept_unproven_ram, "derived": derived, "facts": facts}
    tmp = f"{args.out}.tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")
    os.replace(tmp, args.out)
    for r in refusals:
        print(f"REFUSE  {r}")
    for w in warnings:
        print(f"WARN    {w}")
    print(f"probe: {doc['verdict']} ({len(refusals)} refusal(s), {len(warnings)} warning(s)); wrote {args.out}")
    return 1 if refusals else 0


if __name__ == "__main__":
    sys.exit(main())
