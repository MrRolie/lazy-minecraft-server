"""The compose invariants the bridge's safety rests on, read through Compose itself (`docker compose config`).

These are the structural fixes for the two review blockers and the rules around them. Each one was proved
live once; this pins them, so an edit that undoes one fails a test instead of passing review:
- out/ read-only, in/ read-write, and nothing else of the host mounted
- its own network (not minecraft's: RCON), with the fixed interface and subnet the host fence keys on
- no RCON env, no .env; the token file is optional
- no depends_on either way
- no ports; the resource limits; the cap set

Needs the docker CLI (not the daemon). Without it the tests FAIL, because a check that can't run must not
pass. Run: uv run --with pytest pytest bridge/
"""
import ipaddress
import json
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
FENCE = REPO / "host" / "bin" / "mc-bridge-fence"


def compose_config(tmp_path, bridge_env=True):
    """The resolved config of the repo's docker-compose.yml, with stub env files (RCON_PASSWORD in .env)."""
    assert shutil.which("docker"), "docker CLI missing: the compose invariants can't be checked"
    shutil.copy(REPO / "docker-compose.yml", tmp_path / "docker-compose.yml")
    (tmp_path / ".env").write_text("RCON_PASSWORD=stub-rcon\nLEVEL=world\n")
    if bridge_env:
        (tmp_path / ".env.bridge").write_text("DISCORD_BRIDGE_TOKEN=stub-token\nDISCORD_GUILD_ID=1\n")
    out = subprocess.run(["docker", "compose", "-f", str(tmp_path / "docker-compose.yml"), "config",
                          "--format", "json"], capture_output=True, text=True, cwd=tmp_path)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


@pytest.fixture
def tmp_path():
    """A scratch dir under the repo, not /tmp: snap Docker can't see /tmp or hidden dirs (AGENTS.md)."""
    d = Path(tempfile.mkdtemp(prefix="test-compose-", dir=REPO))
    yield d
    shutil.rmtree(d)


@pytest.fixture
def cfg(tmp_path):
    return compose_config(tmp_path)


def fence(key):
    out = subprocess.run(["bash", str(FENCE), "print"], capture_output=True, text=True, check=True).stdout
    return [ln.split(" ", 1)[1] for ln in out.splitlines() if ln.startswith(key + " ")]


def test_bridge_mounts_exactly_out_read_only_and_in_read_write(cfg, tmp_path):
    vols = [(v["type"], v["source"], v["target"], bool(v.get("read_only"))) for v in cfg["services"]["bridge"]["volumes"]]
    assert vols == [("bind", str(tmp_path / "mc_data/mcve/out"), "/mcve/out", True),
                    ("bind", str(tmp_path / "mc_data/mcve/in"), "/mcve/in", False),
                    ("bind", "/run/mc-host", "/run/mc-host", True)]  # the fence stamp, read-only
    for v in cfg["services"]["bridge"]["volumes"]:
        assert v.get("bind", {}).get("create_host_path", True), "create_host_path: false blocks minecraft's up"


def test_bridge_is_alone_on_its_fenced_network(cfg):
    services = cfg["services"]
    assert set(services["bridge"]["networks"]) == {"discord"}
    assert "discord" not in set(services["minecraft"].get("networks", {"default": None}))
    net = cfg["networks"]["discord"]
    assert net["driver_opts"]["com.docker.network.bridge.name"] == fence("iface")[0]
    assert net["driver_opts"]["com.docker.network.bridge.enable_icc"] == "false"
    subnet = net["ipam"]["config"][0]["subnet"]
    assert subnet == fence("subnet")[0]
    sub = ipaddress.ip_network(subnet)
    assert any(sub.subnet_of(ipaddress.ip_network(d)) for d in fence("drop"))  # its gateway is fenced too
    for pool in ["172.17.0.0/12", "192.168.0.0/16"]:  # Docker's default address pools: no collision
        assert not sub.overlaps(ipaddress.ip_network(pool, strict=False))
    assert services["bridge"]["dns"] == ["1.1.1.1", "9.9.9.9"]  # not the LAN router, which the fence drops
    assert not services["bridge"].get("ports")


def test_bridge_gets_only_its_own_env(cfg):
    env = cfg["services"]["bridge"].get("environment", {})
    assert set(env) == {"DISCORD_BRIDGE_TOKEN", "DISCORD_GUILD_ID"}  # nothing from .env (RCON_PASSWORD)
    assert not any("RCON" in k for k in env)


def test_a_missing_token_file_never_stops_compose(tmp_path):
    cfg = compose_config(tmp_path, bridge_env=False)  # .env.bridge absent: config (and up) still succeed
    assert "DISCORD_BRIDGE_TOKEN" not in cfg["services"]["bridge"].get("environment", {})


def test_neither_service_depends_on_the_other(cfg):
    assert not cfg["services"]["minecraft"].get("depends_on")
    assert not cfg["services"]["bridge"].get("depends_on")


def test_bridge_limits_and_privileges(cfg):
    b = cfg["services"]["bridge"]
    assert int(b["mem_limit"]) == 256 * 1024 * 1024
    assert int(b["pids_limit"]) == 64
    assert float(b["cpus"]) == 0.5
    assert b["tmpfs"] == ["/tmp:size=16m"]
    assert b["read_only"] is True and b["init"] is True
    assert b["cap_drop"] == ["ALL"]
    assert sorted(b["cap_add"]) == ["CHOWN", "KILL", "SETGID", "SETPCAP", "SETUID"]
    assert "privileged" not in b and "network_mode" not in b and "pid" not in b


def test_minecraft_stop_budget_is_untouched(cfg):
    m = cfg["services"]["minecraft"]
    assert m["stop_grace_period"] == "2m0s" and m["environment"]["STOP_DURATION"] == "110"


def test_an_oom_exits_the_jvm_and_never_dumps_the_heap(cfg):
    """An OOM'd JVM hung for 2 h with `restart` never firing (2026-10-01): it must exit so Docker restarts it.
    JVM_XX_OPTS appends to the Aikar flags (seen in `docker top`, spike 2026-10-02); a heap dump of 8 GB would
    land on the world's disk."""
    env = cfg["services"]["minecraft"]["environment"]
    assert env["JVM_XX_OPTS"] == "-XX:+ExitOnOutOfMemoryError"
    assert env["USE_AIKAR_FLAGS"] == "true" and env["MEMORY"] == "8G"
    assert cfg["services"]["minecraft"]["restart"] == "unless-stopped"  # what turns the exit into a restart
    assert not [k for k, v in env.items() if "HeapDump" in str(v) or k == "JVM_OPTS"]


def test_both_services_cap_their_container_logs(cfg):
    """json-file keeps all stdout by default, on the world's disk: a chatty (or hostile) bridge fills it."""
    for name in ("minecraft", "bridge"):
        assert cfg["services"][name]["logging"] == {"driver": "json-file",
                                                     "options": {"max-size": "10m", "max-file": "3"}}, name


def test_the_bridge_is_healthy_only_while_its_ready_heartbeat_is_fresh(cfg):
    hc = cfg["services"]["bridge"]["healthcheck"]
    assert hc == {
        "test": ["CMD", "python", "-c",
                 "import os,sys,time; sys.exit(time.time() - os.stat('/tmp/ready').st_mtime > 60)"],
        "interval": "30s", "timeout": "10s", "start_period": "1m0s", "retries": 2,
    }
    assert "healthcheck" not in cfg["services"]["minecraft"]  # itzg's own, from the image, untouched


def test_the_image_build_keeps_its_supply_chain_and_privilege_controls():
    df = (REPO / "bridge" / "Dockerfile").read_text()
    assert re.search(r"^FROM python:3\.12-slim@sha256:[0-9a-f]{64}$", df, re.M), "base image not pinned by digest"
    pip = next(ln for ln in df.splitlines() if "pip install" in ln)
    assert "--require-hashes" in pip and "--only-binary=:all:" in pip and "-r /app/requirements.txt" in pip
    assert "RUN find / -xdev -perm /6000 -type f -exec chmod a-s {} +" in df.splitlines()
    assert 'ENTRYPOINT ["/app/entrypoint.sh"]' in df.splitlines()
    req = (REPO / "bridge" / "requirements.txt").read_text()
    pins = re.findall(r"^([a-z0-9._-]+)==(\S+) \\$", req, re.M | re.I)
    assert ("discord-py", "2.7.1") in pins and len(pins) >= 10
    for block in re.split(r"\n(?=[a-z0-9._-]+==)", req.split("\n", 1)[1], flags=re.I):
        assert "--hash=sha256:" in block, block[:60]
