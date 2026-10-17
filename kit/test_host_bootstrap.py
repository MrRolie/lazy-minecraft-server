"""Tests for the host bootstrap's kit-side scripts (KIT.md P0, P2; HUMAN-STEPS step 7):
kit/probe.sh + kit/probe.py (P0: observe, then judge), kit/packages.sh (P2) and kit/sudoers.sh (the NOPASSWD drop-in).
Every machine fact is a fixture: a fake /proc, /sys and /etc under PROBE_ROOT / PKG_ROOT, and PATH stubs for the
commands. Nothing here reads this machine's network, apt state or sudoers. Run: uv run --with pytest pytest kit/
"""
import copy
import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

import probe

KIT = Path(__file__).resolve().parent
TOOLS = ["bash", "sh", "awk", "sed", "tr", "cut", "grep", "sort", "tail", "head", "cat", "basename", "dirname",
         "python3", "mktemp", "rm", "mv", "cp", "chmod", "install", "printf", "seq", "sleep", "stat", "env", "ls",
         "mkdir", "visudo", "readlink", "touch", "wc", "date", "cmp"]


def bin_dir(tmp_path, **stubs):
    """A PATH with only TOOLS (linked to the real ones) and the given stubs: a command not listed is absent."""
    d = tmp_path / "bin"
    d.mkdir(exist_ok=True)
    for t in TOOLS:
        real = shutil.which(t) or shutil.which(t, path="/usr/sbin:/sbin")
        if real and not (d / t).exists():
            (d / t).symlink_to(real)
    for name, body in stubs.items():
        f = d / name
        if f.exists() or f.is_symlink():
            f.unlink()
        f.write_text("#!/bin/bash\n" + body + "\n")
        f.chmod(0o755)
    return str(d)


# ---- the judge (kit/probe.py) -------------------------------------------------------------------------------

GOOD = {
    "euid": "1000", "uid": "1000", "gid": "1000", "user": "kit", "os_id": "ubuntu", "os_codename": "noble",
    "os_version": "24.04", "kernel": "6.8.0-45-generic", "mem_total_kib": str(15 * 1024 * 1024 + 512 * 1024),
    "threads": "4", "cores": "2", "disk_free_bytes": str(400 * 10 ** 9), "nm_active": "active", "nmcli": "yes",
    "ip_ok": "yes",
    "nm_wired": ["Wired connection 1"], "nm_wifi": ["HomeNet"], "nic": ["enp0s31f6 ethernet", "wlp2s0 wifi"],
    "ipv4": ["127.0.0.1/8 lo", "192.168.1.20/24 enp0s31f6"],
    "route": ["192.168.1.0/24 enp0s31f6", "100.100.100.100 tailscale0", "127.0.0.0/8 lo"],
    "battery": ["BAT0"], "charge_threshold": ["/sys/class/power_supply/BAT0/charge_control_end_threshold"],
    "charge_types": [],
}


def facts(**change):
    f = copy.deepcopy(GOOD)
    for k, v in change.items():
        if v is None:
            f.pop(k, None)
        else:
            f[k] = v
    for k in probe.LISTS:
        f.setdefault(k, [])
    return f


def test_a_proven_class_laptop_passes():
    refusals, warnings, derived = probe.judge(facts())
    assert refusals == [] and warnings == []
    assert derived["ram_mib"] == 15 * 1024 + 512 and derived["charge_cap_support"] == "threshold"
    assert derived["suggested_charge_cap"] == 80 and derived["net_wired_conn"] == "Wired connection 1"


@pytest.mark.parametrize("change,says", [
    ({"euid": "0", "uid": "0", "gid": "0"}, "run as root"),
    ({"uid": "1001"}, "uid/gid is 1001/1000, not 1000/1000"),
    ({"gid": "1001"}, "uid/gid is 1000/1001"),
    ({"uid": None}, "uid/gid unknown"),
    ({"os_id": "debian"}, "not ubuntu"),
    ({"os_id": None}, "OS is unknown"),
    ({"nm_active": "inactive"}, "NetworkManager is not running"),
    ({"nmcli": "no"}, "NetworkManager is not running"),
    ({"nm_active": None}, "NetworkManager is not running"),
    ({"nm_wired": [], "nm_wifi": []}, "no wired or Wi-Fi connection"),
    ({"route": ["10.213.0.0/16 wg0"]}, "10.213.0.0/16 wg0 overlaps 10.213.37.0/24"),
    ({"ipv4": ["10.213.37.9/24 eth9"]}, "10.213.37.9/24 eth9 overlaps"),
    ({"route": ["10.213.37.0/24 docker0"]}, "overlaps"),  # the bridge subnet on any device but br-mcve
    ({"route": ["10.213.37.1 docker0"]}, "overlaps"),  # a local /32 on another device
    ({"route": ["10.213.0.0/16 br-mcve"]}, "10.213.0.0/16 br-mcve overlaps"),  # a supernet on br-mcve is no exception
    ({"route": ["10.0.0.0/8 tun0"]}, "overlaps"),
    ({"route": ["10.213.37.77 tailscale0"]}, "overlaps"),  # a host route (/32)
    ({"route": ["not-a-cidr eth0"]}, "unreadable"),
    ({"ip_ok": "no", "route": [], "ipv4": []}, "IPv4 routes and addresses unknown"),  # empty is not "no overlap"
    ({"ip_ok": None}, "IPv4 routes and addresses unknown"),
    ({"disk_free_bytes": str(50 * 10 ** 9 - 1)}, "under 50 GB"),
    ({"disk_free_bytes": None}, "free disk unknown"),
    ({"mem_total_kib": str((14 * 1024 - 1) * 1024)}, "under the proven 16 GB class"),
    ({"mem_total_kib": None}, "RAM unknown"),
])
def test_each_gate_refuses(change, says):
    refusals, _, _ = probe.judge(facts(**change))
    assert any(says in r for r in refusals), refusals


@pytest.mark.parametrize("change", [
    {"disk_free_bytes": str(50 * 10 ** 9)},  # the boundaries pass: a moved threshold fails one side
    {"mem_total_kib": str(14 * 1024 * 1024)},
    # the kit's own bridge, re-probed, in the shape probe.sh makes of a real `ip -4 route show table all`: the /24
    # link route plus the local-table /32s for its address and its broadcast
    {"route": ["10.213.37.0/24 br-mcve", "10.213.37.1 br-mcve", "10.213.37.255 br-mcve"],
     "ipv4": ["10.213.37.1/24 br-mcve"]},
    {"route": ["10.213.38.0/24 eth1", "10.213.36.0/24 eth2"]},  # neighbours, not overlaps
    {"nm_wired": [], "nm_wifi": ["HomeNet"]},  # a Wi-Fi-only laptop
])
def test_the_edges_pass(change):
    assert probe.judge(facts(**change))[0] == []


def test_under_the_ram_floor_only_an_explicit_acceptance_goes_on_and_it_is_recorded():
    low = facts(mem_total_kib=str(11 * 1024 * 1024))
    assert any("--accept-unproven-ram" in r for r in probe.judge(low)[0])
    refusals, warnings, _ = probe.judge(low, accept_unproven_ram=True)
    assert refusals == [] and any("accepted as UNPROVEN" in w for w in warnings)


def test_the_acceptance_does_not_waive_any_other_gate():
    refusals, _, _ = probe.judge(facts(mem_total_kib=str(8 * 1024 * 1024), uid="1001"), accept_unproven_ram=True)
    assert len(refusals) == 1 and "uid/gid" in refusals[0]


def test_warnings_for_what_is_unproven_but_not_refused():
    _, warnings, derived = probe.judge(facts(os_codename="resolute", threads="2", charge_threshold=[],
                                             charge_types=["/sys/class/power_supply/BAT0/charge_types"]))
    text = " ".join(warnings)
    assert "proven on noble" in text and "2 CPU threads" in text and "charge_types" in text
    assert derived["suggested_charge_cap"] == "none" and derived["charge_cap_support"] == "charge_types"
    _, warnings, _ = probe.judge(facts(battery=[], charge_threshold=[]))  # a VM or desktop: no battery, no warning
    assert warnings == []


# ---- the observer (kit/probe.sh), end to end against a fake machine ---------------------------------------

NMCLI = r'''case "$*" in
  "-t -f NAME,TYPE connection show") printf '%s\n' 'Wired connection 1:802-3-ethernet' 'Cafe\:Net:802-11-wireless' 'lo:loopback' 'tailscale0:tun' ;;
  *) exit 10 ;;
esac'''
IP = r'''case "$*" in
  "-4 -o addr show") printf '%s\n' '1: lo    inet 127.0.0.1/8 scope host lo\       valid_lft forever' '2: eno1    inet 192.168.1.20/24 brd 192.168.1.255 scope global dynamic eno1\       valid_lft 1sec' ;;
  "-4 route show table all") printf '%s\n' 'default via 192.168.1.1 dev eno1 proto dhcp metric 100' '192.168.1.0/24 dev eno1 proto kernel scope link src 192.168.1.20' 'local 127.0.0.0/8 dev lo table local proto kernel scope host src 127.0.0.1' "${EXTRA_ROUTE:-}" ;;
  *) exit 1 ;;
esac'''


def fake_machine(tmp_path, ram_gib=15.5, uid=1000, battery=True, os_id="ubuntu"):
    root = tmp_path / "root"
    (root / "proc").mkdir(parents=True)
    (root / "proc" / "meminfo").write_text(f"MemTotal:       {int(ram_gib * 1024 * 1024)} kB\nMemFree: 1 kB\n")
    (root / "etc").mkdir()
    (root / "etc" / "os-release").write_text(f'NAME="Ubuntu"\nID={os_id}\nVERSION_ID="24.04"\nVERSION_CODENAME=noble\n'
                                             'UBUNTU_CODENAME=noble\n')
    net = root / "sys" / "class" / "net"
    for nic, marker in (("eno1", "device"), ("wlp2s0", "wireless"), ("lo", None), ("docker0", None)):
        (net / nic).mkdir(parents=True)
        if marker:
            (net / nic / marker).mkdir()
    ps = root / "sys" / "class" / "power_supply"
    (ps / "AC").mkdir(parents=True)
    (ps / "AC" / "type").write_text("Mains\n")
    if battery:
        (ps / "BAT0").mkdir()
        (ps / "BAT0" / "type").write_text("Battery\n")
        (ps / "BAT0" / "charge_control_end_threshold").write_text("100\n")
    stubs = dict(
        id=f'case "$1" in -u) echo {uid} ;; -g) echo {uid} ;; -un) echo kit ;; *) exit 1 ;; esac',
        hostname="echo old-laptop", getent="echo 'kit:x:1000:1000::/home/kit:/bin/bash'",
        timedatectl="echo Europe/Berlin", uname="echo 6.8.0-45-generic", nproc="echo 4",
        lscpu="printf '# c\\n0,0\\n1,0\\n0,0\\n1,0\\n'", df="printf 'Avail\\n400000000000\\n'",
        systemctl='[ "$*" = "is-active NetworkManager" ] && echo active', nmcli=NMCLI, ip=IP)
    return root, stubs


def run_probe(tmp_path, *args, extra_env=None, drop=(), **machine):
    root, stubs = fake_machine(tmp_path, **machine)
    for name in drop:
        stubs.pop(name)
    path = bin_dir(tmp_path, **stubs)
    out = tmp_path / "probe.json"
    r = subprocess.run(["bash", str(KIT / "probe.sh"), "--out", str(out), *args], capture_output=True, text=True,
                       env={"PATH": path, "PROBE_ROOT": str(root), **(extra_env or {})})
    return r, (json.loads(out.read_text()) if out.exists() else None)


def test_probe_reads_a_fake_laptop_and_passes(tmp_path):
    r, doc = run_probe(tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    f = doc["facts"]
    assert doc["verdict"] == "pass" and doc["refusals"] == []
    assert f["nm_wired"] == ["Wired connection 1"] and f["nm_wifi"] == ["Cafe:Net"]  # an escaped ':' in a name
    assert f["nic"] == ["eno1 ethernet", "wlp2s0 wifi"]  # lo and docker0 (no device) are virtual: left out
    assert f["route"] == ["192.168.1.0/24 eno1", "127.0.0.0/8 lo"]  # default skipped, the type word stripped
    assert f["ipv4"] == ["127.0.0.1/8 lo", "192.168.1.20/24 eno1"]
    assert f["charge_threshold"] == ["/sys/class/power_supply/BAT0/charge_control_end_threshold"]
    assert (f["cores"], f["threads"], f["os_codename"], f["tz"]) == ("2", "4", "noble", "Europe/Berlin")
    assert doc["derived"]["ram_mib"] == int(15.5 * 1024)


def test_probe_writes_nothing_but_its_json(tmp_path):
    r, _ = run_probe(tmp_path)
    root = tmp_path / "root"
    assert (root / "sys/class/power_supply/BAT0/charge_control_end_threshold").read_text() == "100\n"  # read only
    assert sorted(p.name for p in tmp_path.iterdir()) == ["bin", "probe.json", "root"]


@pytest.mark.parametrize("machine,env,drop,says", [
    ({"uid": 1001}, {}, (), "not 1000/1000"),
    ({"ram_gib": 7.6}, {}, (), "under the proven 16 GB class"),
    ({"os_id": "fedora"}, {}, (), "not ubuntu"),
    ({}, {"EXTRA_ROUTE": "10.213.0.0/16 dev wg0 scope link"}, (), "overlaps 10.213.37.0/24"),
    ({}, {}, ("nmcli",), "NetworkManager is not running"),  # a missing tool is unknown, and unknown refuses
    ({}, {}, ("df",), "free disk unknown"),
    ({}, {}, ("ip",), "IPv4 routes and addresses unknown"),  # found live: ubuntu:24.04 has no iproute2
])
def test_probe_refuses_end_to_end_and_still_writes_why(tmp_path, machine, env, drop, says):
    r, doc = run_probe(tmp_path, extra_env=env, drop=drop, **machine)
    assert r.returncode == 1
    assert doc["verdict"] == "refused" and any(says in x for x in doc["refusals"]), doc["refusals"]
    assert "REFUSE" in r.stdout


BR_MCVE_ROUTES = "\n".join([  # the lines a host with the kit's bridge up adds (shape read from a dev machine's docker0)
    "10.213.37.0/24 dev br-mcve proto kernel scope link src 10.213.37.1",
    "local 10.213.37.1 dev br-mcve table local proto kernel scope host src 10.213.37.1",
    "broadcast 10.213.37.255 dev br-mcve table local proto kernel scope link src 10.213.37.1",
])


def test_a_reprobe_of_an_installed_host_passes_its_own_bridge(tmp_path):
    r, doc = run_probe(tmp_path, extra_env={"EXTRA_ROUTE": BR_MCVE_ROUTES})
    assert r.returncode == 0, r.stdout + r.stderr
    assert {"10.213.37.0/24 br-mcve", "10.213.37.1 br-mcve", "10.213.37.255 br-mcve"} <= set(doc["facts"]["route"])


def test_the_same_routes_on_another_device_refuse(tmp_path):
    r, doc = run_probe(tmp_path, extra_env={"EXTRA_ROUTE": BR_MCVE_ROUTES.replace("br-mcve", "docker0")})
    assert r.returncode == 1 and any("10.213.37.1 docker0" in x for x in doc["refusals"]), doc["refusals"]


def test_probe_low_ram_goes_on_only_when_accepted(tmp_path):
    r, doc = run_probe(tmp_path, "--accept-unproven-ram", ram_gib=11.4)
    assert r.returncode == 0 and doc["accepted_unproven_ram"] is True
    assert any("UNPROVEN" in w for w in doc["warnings"])


def test_probe_without_a_battery_threshold_suggests_no_cap(tmp_path):
    r, doc = run_probe(tmp_path, battery=False)
    assert r.returncode == 0 and doc["derived"]["suggested_charge_cap"] == "none"


def test_an_observer_that_dies_half_way_gives_no_verdict(tmp_path):
    """A crash inside the fact-gathering must not reach the judge as a machine with fewer facts."""
    root, stubs = fake_machine(tmp_path)
    path = bin_dir(tmp_path, **stubs)
    kit = tmp_path / "kitcopy"
    kit.mkdir()
    shutil.copy(KIT / "probe.py", kit / "probe.py")
    text = (KIT / "probe.sh").read_text()
    anchor = '  fact kernel "$(uname -r 2>/dev/null)"\n'
    assert anchor in text
    (kit / "probe.sh").write_text(text.replace(anchor, anchor + '  echo "$UNSET_ON_PURPOSE"\n'))
    out = tmp_path / "probe.json"
    r = subprocess.run(["bash", str(kit / "probe.sh"), "--out", str(out)], capture_output=True, text=True,
                       env={"PATH": path, "PROBE_ROOT": str(root)})
    assert r.returncode == 2 and "no verdict written" in r.stderr and not out.exists()


def test_probe_usage(tmp_path):
    r = subprocess.run(["bash", str(KIT / "probe.sh"), "--bogus"], capture_output=True, text=True)
    assert r.returncode == 2


# ---- the package phase (kit/packages.sh) ---------------------------------------------------------------------

DOCKER_FPR = "9DC858229FC7DD38854AE2D88D81803C0EBFCD88"
TS_FPR = "2596A99EAAB33821893C0A79458CA832957F5868"


def pkg_env(tmp_path, installed=(), conflicts=(), snap_docker=False, fpr_override=None, docker_info_rc=0,
            groups="kit adm", sources=None, apt_broken=False):
    """A fake root (PKG_ROOT) and a stubbed apt/dpkg/curl/gpg. The state dir records installed packages; every
    apt-get, curl and usermod call is logged to calls.log. SOURCES: {path under the root: content}, apt sources
    already there. APT_BROKEN: `apt-get update` fails the way apt does on two Signed-By values for one repo."""
    root = tmp_path / "root"
    (root / "etc").mkdir(parents=True)
    (root / "etc" / "os-release").write_text("ID=ubuntu\nVERSION_CODENAME=noble\nUBUNTU_CODENAME=noble\n")
    for rel, content in (sources or {}).items():
        (root / rel).parent.mkdir(parents=True, exist_ok=True)
        (root / rel).write_text(content)
    broken = ('if [ "$1" = update ]; then echo "E: Conflicting values set for option Signed-By regarding source '
              'https://download.docker.com/linux/ubuntu/ noble" >&2; echo "E: The list of sources could not be read."'
              ' >&2; exit 100; fi; ') if apt_broken else ""
    state = tmp_path / "state"
    state.mkdir()
    for p in (*installed, *conflicts):
        (state / p).touch()
    calls = tmp_path / "calls.log"
    calls.touch()
    keys = tmp_path / "keys"
    keys.mkdir()
    # a "key file" here is text naming its fingerprint; the gpg stub reads it back
    (keys / "docker").write_text(f"FPR {fpr_override or DOCKER_FPR}\n")
    (keys / "tailscale").write_text(f"FPR {TS_FPR}\n")
    stubs = dict(
        id=f'case "$1" in -u) echo 0 ;; -nG) echo "{groups}" ;; *) exit 1 ;; esac',
        dpkg='[ "$1" = --print-architecture ] && echo amd64',
        **{"dpkg-query": f'[ -e "{state}/${{@: -1}}" ] && printf "install ok installed"'},
        **{"apt-get": f'echo "apt-get $*" >>"{calls}"; {broken}if [ "$1" = install ]; then for p in "$@"; do '
                      f'case "$p" in -*|install) ;; *) touch "{state}/$p" ;; esac; done; fi'},
        curl=f'echo "curl $*" >>"{calls}"; out=""; url=""; while [ $# -gt 0 ]; do case "$1" in -o) out=$2; shift 2 ;;'
             f' -*) shift ;; *) url=$1; shift ;; esac; done; case "$url" in *docker*) cp "{keys}/docker" "$out" ;;'
             f' *tailscale*) cp "{keys}/tailscale" "$out" ;; esac',
        gpg='f="${@: -1}"; printf "pub:-:4096:1:X:::::\\nfpr:::::::::%s:\\n" "$(sed -n "s/^FPR //p" "$f")"',
        snap=f'[ "$*" = "list docker" ] && exit {0 if snap_docker else 1}; exit 1',
        usermod=f'echo "usermod $*" >>"{calls}"',
        docker=f'[ "$1" = info ] && {{ [ {docker_info_rc} = 0 ] && echo 27.3.1 || {{ echo "Cannot connect"; exit 1; }}; }}; '
               '[ "$1" = compose ] && echo "Docker Compose version v2.29.7"; true',
        tailscale='echo 1.88.2',
    )
    return root, calls, bin_dir(tmp_path, **stubs)


def run_pkgs(tmp_path, root, path, sudo_user="kit"):
    return subprocess.run(["bash", str(KIT / "packages.sh")], capture_output=True, text=True,
                          env={"PATH": path, "PKG_ROOT": str(root), "SUDO_USER": sudo_user})


def test_packages_install_from_the_vendor_repos_then_change_nothing(tmp_path):
    root, calls, path = pkg_env(tmp_path)
    r = run_pkgs(tmp_path, root, path)
    assert r.returncode == 0, r.stdout + r.stderr
    log = calls.read_text()
    assert "apt-get install -y -qq --no-install-recommends python3 ca-certificates curl gnupg" in log
    assert "docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin" in log
    assert "apt-get install -y -qq --no-install-recommends tailscale" in log
    assert "usermod -aG docker kit" in log
    assert "upgrade" not in log
    # the sources were written before the install that needed them, and apt re-read them (update after the write)
    after_fetch = log.index("apt-get update", log.index("curl -fsSL --retry 3 https://download.docker.com"))
    assert after_fetch < log.index("docker-ce ")
    assert log.rstrip("\n").endswith("apt-get update -qq")  # and the gate read the sources once more at the end
    assert "PASS  apt-get update" in r.stdout
    src = (root / "etc/apt/sources.list.d/docker.sources").read_text()
    assert "Suites: noble\n" in src and "Architectures: amd64\n" in src
    assert "Signed-By: /etc/apt/keyrings/docker.asc" in src
    ts = (root / "etc/apt/sources.list.d/tailscale.list").read_text()
    assert ("deb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg] https://pkgs.tailscale.com/stable/ubuntu"
            " noble main") in ts
    assert (root / "etc/apt/keyrings/docker.asc").exists()
    assert (root / "usr/share/keyrings/tailscale-archive-keyring.gpg").exists()
    assert "PASS  docker info" in r.stdout and "every gate passed" in r.stdout
    calls.write_text("")
    bin_dir(tmp_path, id='case "$1" in -u) echo 0 ;; -nG) echo "kit adm docker" ;; *) exit 1 ;; esac')
    r2 = run_pkgs(tmp_path, root, path)
    assert r2.returncode == 0
    # a second run fetches and installs nothing; its one apt call is the gate's read of the sources
    assert calls.read_text() == "apt-get update -qq\n"
    body = [ln for ln in r2.stdout.splitlines() if ln.startswith("   ")]
    assert body and all("unchanged" in ln or "no conflicting" in ln for ln in body if "docker group" not in ln)


def test_installed_packages_are_never_upgraded(tmp_path):
    root, calls, path = pkg_env(tmp_path, installed=EVERY_PKG)
    assert run_pkgs(tmp_path, root, path).returncode == 0
    assert "apt-get install" not in calls.read_text()


@pytest.mark.parametrize("kw,says", [
    ({"conflicts": ("docker.io",)}, "docker.io"),
    ({"conflicts": ("podman-docker",)}, "podman-docker"),
    ({"snap_docker": True}, "snap:docker"),
])
def test_a_conflicting_docker_is_refused_never_removed(tmp_path, kw, says):
    root, calls, path = pkg_env(tmp_path, **kw)
    r = run_pkgs(tmp_path, root, path)
    assert r.returncode == 1 and says in r.stderr
    assert calls.read_text() == ""  # nothing installed, fetched or removed
    assert not (root / "etc" / "apt").exists()


EVERY_PKG = ("python3", "ca-certificates", "curl", "gnupg", "docker-ce", "docker-ce-cli", "containerd.io",
             "docker-buildx-plugin", "docker-compose-plugin", "tailscale")
KIT_DOCKER_SOURCES = ("Types: deb\nURIs: https://download.docker.com/linux/ubuntu\nSuites: noble\nComponents: stable\n"
                      "Architectures: amd64\nSigned-By: /etc/apt/keyrings/docker.asc\n")
# the kit's tailscale.list for noble (the line of pkgs.tailscale.com's noble.tailscale-keyring.list, read 2026-10-14;
# whether Tailscale's install script writes these exact bytes is not verified here)
KIT_TS_LIST = ("# Tailscale packages for ubuntu noble\ndeb [signed-by=/usr/share/keyrings/tailscale-archive-keyring.gpg]"
               " https://pkgs.tailscale.com/stable/ubuntu noble main\n")
D = "etc/apt/sources.list.d"


@pytest.mark.parametrize("sources,says", [
    # the finding: an older Docker install guide's docker.list, another Signed-By beside the kit's docker.sources
    ({f"{D}/docker.list": "deb [arch=amd64 signed-by=/etc/apt/keyrings/docker.gpg] "
                          "https://download.docker.com/linux/ubuntu noble stable\n"}, "/etc/apt/sources.list.d/docker.list"),
    ({"etc/apt/sources.list": "deb http://archive.ubuntu.com/ubuntu noble main\n"
                              "deb https://pkgs.tailscale.com/stable/ubuntu noble main\n"}, "/etc/apt/sources.list "),
    ({f"{D}/ts.sources": "Types: deb\nURIs: https://pkgs.tailscale.com/stable/ubuntu\nSuites: noble\n"},
     "/etc/apt/sources.list.d/ts.sources"),
    # the kit's own file name, but not what the kit writes (here: Docker's guide without Architectures)
    ({f"{D}/docker.sources": KIT_DOCKER_SOURCES.replace("Architectures: amd64\n", "")},
     "/etc/apt/sources.list.d/docker.sources"),
    ({f"{D}/tailscale.list": KIT_TS_LIST.replace("noble main", "jammy main")}, "/etc/apt/sources.list.d/tailscale.list"),
])
def test_a_vendor_apt_source_the_kit_did_not_write_is_refused_before_anything_changes(tmp_path, sources, says):
    root, calls, path = pkg_env(tmp_path, installed=EVERY_PKG, sources=sources)
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    r = run_pkgs(tmp_path, root, path)
    assert r.returncode == 1 and "that the kit did not write" in r.stderr and says in r.stderr, r.stderr
    assert calls.read_text() == ""  # nothing installed, fetched or updated
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before  # nothing written or removed
    assert not (root / "etc/apt/keyrings").exists()


@pytest.mark.parametrize("sources", [
    {f"{D}/tailscale.list": KIT_TS_LIST},  # already there, byte-equal to the kit's: counted as the kit's own
    {f"{D}/docker.sources": KIT_DOCKER_SOURCES, f"{D}/tailscale.list": KIT_TS_LIST},  # a re-run
    {"etc/apt/sources.list": "# deb [signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu"
                             " noble stable\n"},  # commented out: apt does not read it
    {f"{D}/docker.list.save": "deb https://download.docker.com/linux/ubuntu noble stable\n"},  # apt skips the name
])
def test_the_kit_s_own_or_inert_vendor_sources_pass(tmp_path, sources):
    root, calls, path = pkg_env(tmp_path, sources=sources)
    r = run_pkgs(tmp_path, root, path)
    assert r.returncode == 0, r.stdout + r.stderr
    assert (root / D / "docker.sources").read_text() == KIT_DOCKER_SOURCES
    assert (root / D / "tailscale.list").read_text() == KIT_TS_LIST


def test_an_unreadable_source_list_is_refused(tmp_path):
    root, calls, path = pkg_env(tmp_path, sources={f"{D}/odd.list": "x\n"})
    (root / D / "odd.list").unlink()
    (root / D / "odd.list").mkdir()  # grep cannot read a directory: unjudgeable, so it counts
    r = run_pkgs(tmp_path, root, path)
    assert r.returncode == 1 and "odd.list (unreadable)" in r.stderr and calls.read_text() == ""


def test_a_broken_apt_fails_the_gate_even_when_everything_is_installed(tmp_path):
    """The cheap all-clear the gate exists for: nothing to install, so no apt call but the gate's own."""
    root, calls, path = pkg_env(tmp_path, installed=EVERY_PKG, apt_broken=True)
    r = run_pkgs(tmp_path, root, path)
    assert r.returncode == 1 and "FAIL  apt-get update" in r.stdout and "Conflicting values" in r.stdout
    assert "every gate passed" not in r.stdout and "apt-get install" not in calls.read_text()


def test_an_apt_error_line_fails_the_gate_even_on_exit_0(tmp_path):
    root, calls, path = pkg_env(tmp_path, installed=EVERY_PKG)
    bin_dir(tmp_path, **{"apt-get": 'echo "E: Failed to fetch https://download.docker.com/x" >&2; exit 0'})
    r = run_pkgs(tmp_path, root, path)
    assert r.returncode == 1 and "FAIL  apt-get update" in r.stdout and "Failed to fetch" in r.stdout


def test_a_key_with_the_wrong_fingerprint_is_never_installed(tmp_path):
    root, calls, path = pkg_env(tmp_path, fpr_override="0" * 40)
    r = run_pkgs(tmp_path, root, path)
    assert r.returncode == 1 and "not the pinned" in r.stderr
    assert not (root / "etc/apt/keyrings/docker.asc").exists()
    assert not (root / "etc/apt/sources.list.d/docker.sources").exists()
    assert "docker-ce" not in calls.read_text()


def test_the_gate_fails_when_the_docker_daemon_does_not_answer(tmp_path):
    root, _, path = pkg_env(tmp_path, docker_info_rc=1)
    r = run_pkgs(tmp_path, root, path)
    assert r.returncode == 1 and "FAIL  docker info" in r.stdout and "PASS  tailscale version" in r.stdout


@pytest.mark.parametrize("sudo_user", ["", "root"])
def test_packages_needs_the_kit_user_behind_sudo(tmp_path, sudo_user):
    root, calls, path = pkg_env(tmp_path)
    r = run_pkgs(tmp_path, root, path, sudo_user=sudo_user)
    assert r.returncode == 1 and "SUDO_USER" in r.stderr and calls.read_text() == ""


def test_packages_refuses_a_non_ubuntu_host(tmp_path):
    root, calls, path = pkg_env(tmp_path)
    (root / "etc" / "os-release").write_text("ID=debian\nVERSION_CODENAME=bookworm\n")
    r = run_pkgs(tmp_path, root, path)
    assert r.returncode == 1 and "not Ubuntu" in r.stderr and calls.read_text() == ""


def test_the_pinned_fingerprints_are_the_ones_in_the_script():
    text = (KIT / "packages.sh").read_text()
    assert f"DOCKER_FPR={DOCKER_FPR}" in text and f"TS_FPR={TS_FPR}" in text


# ---- the sudoers drop-in (kit/sudoers.sh) --------------------------------------------------------------------

def sudoers_env(tmp_path, visudo=None):
    d = tmp_path / "sudoers.d"
    d.mkdir()
    main = tmp_path / "sudoers"
    main.write_text(f"root ALL=(ALL:ALL) ALL\n@includedir {d}\n")
    stubs = {"id": 'case "$1" in -u) echo 0 ;; *) exit 1 ;; esac'}
    if visudo is not None:
        stubs["visudo"] = visudo
    return d, main, bin_dir(tmp_path, **stubs)


def sudoers(path, d, main, *args, user="kit", script=KIT / "sudoers.sh"):
    return subprocess.run(["bash", str(script), *args], capture_output=True, text=True,
                          env={"PATH": path, "SUDO_USER": user, "SUDOERS_DIR": str(d), "SUDOERS_MAIN": str(main)})


@pytest.fixture
def real_visudo():
    if not (shutil.which("visudo") or shutil.which("visudo", path="/usr/sbin")):
        pytest.fail("visudo missing: the sudoers tests validate with the real parser")


def test_sudoers_install_check_remove(tmp_path, real_visudo):
    d, main, path = sudoers_env(tmp_path)
    r = sudoers(path, d, main, "install")
    assert r.returncode == 0, r.stderr
    f = d / "90-mc-maintainer"
    assert f.read_text().splitlines()[-1] == "kit ALL=(ALL:ALL) NOPASSWD: ALL"
    assert oct(f.stat().st_mode & 0o777) == "0o440"
    assert sorted(p.name for p in d.iterdir()) == ["90-mc-maintainer"]  # no temp or backup left behind
    assert sudoers(path, d, main, "install").stdout.startswith("sudoers: unchanged")
    assert sudoers(path, d, main, "check").returncode == 0
    assert sudoers(path, d, main, "remove").returncode == 0 and not f.exists()
    assert sudoers(path, d, main, "check").returncode == 1


def test_the_real_parser_rejects_a_broken_grant(tmp_path, real_visudo):
    """The validation is real: a template visudo cannot parse is never installed."""
    d, main, path = sudoers_env(tmp_path)
    kit = tmp_path / "kitcopy"
    kit.mkdir()
    shutil.copy(KIT / "sudoers.sh", kit / "sudoers.sh")
    (kit / "sudoers.in").write_text((KIT / "sudoers.in").read_text().replace("(ALL:ALL)", "(ALL:ALL"))
    r = sudoers(path, d, main, "install", script=kit / "sudoers.sh")
    assert r.returncode == 1 and "does not parse" in r.stderr
    assert list(d.iterdir()) == []


def test_a_failed_whole_config_check_takes_the_drop_in_back_out(tmp_path):
    # visudo -cf on the candidate passes, then the whole-config check (on SUDOERS_MAIN) fails
    d, main, path = sudoers_env(tmp_path, visudo=f'[ "${{@: -1}}" = "{tmp_path}/sudoers" ] && exit 1; exit 0')
    r = sudoers(path, d, main, "install")
    assert r.returncode == 1 and "taken back out" in r.stderr
    assert list(d.iterdir()) == []


def test_a_grant_the_kit_did_not_write_is_never_touched(tmp_path, real_visudo):
    d, main, path = sudoers_env(tmp_path)
    mine = d / "90-mc-maintainer"
    mine.write_text("kit ALL=(ALL:ALL) NOPASSWD: /usr/bin/apt\n")
    for cmd in ("install", "remove"):
        r = sudoers(path, d, main, cmd)
        assert r.returncode == 1 and "not written by the kit" in r.stderr
        assert mine.read_text() == "kit ALL=(ALL:ALL) NOPASSWD: /usr/bin/apt\n"


@pytest.mark.parametrize("user", ["", "root", "Kit;rm", "a b"])
def test_sudoers_refuses_without_a_plain_kit_user(tmp_path, user):
    d, main, path = sudoers_env(tmp_path)
    r = sudoers(path, d, main, "install", user=user)
    assert r.returncode == 1 and "SUDO_USER" in r.stderr and list(d.iterdir()) == []


def test_a_drop_in_name_sudo_would_skip_is_refused(tmp_path):
    """sudo's @includedir ignores names with '.' or '~': such a grant would silently do nothing."""
    d, main, path = sudoers_env(tmp_path)
    kit = tmp_path / "kitcopy"
    kit.mkdir()
    shutil.copy(KIT / "sudoers.in", kit / "sudoers.in")
    text = (KIT / "sudoers.sh").read_text()
    assert "NAME=90-mc-maintainer" in text
    (kit / "sudoers.sh").write_text(text.replace("NAME=90-mc-maintainer", "NAME=90-mc-maintainer.conf"))
    r = sudoers(path, d, main, "install", script=kit / "sudoers.sh")
    assert r.returncode == 1 and "sudo would skip it" in r.stderr and list(d.iterdir()) == []
