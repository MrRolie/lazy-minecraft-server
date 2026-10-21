"""Tests for kit/kitconfig.py (the kit.toml schema) and kit/render.py, and the kit's key gate: rendering
kit/fixtures/asus-node.toml reproduces asus-node's files byte for byte. Run: uv run --with pytest pytest kit/

The tests that are not about asus-node use kit/fixtures/example.toml, a made-up kit instance; the gate reads
asus-node's record and skips where a copy dropped it (kit/INSTANCE-PATHS).
The compose gate needs the docker CLI (not the daemon) and FAILS without it: a gate that cannot run must not pass.
Scratch repos for compose live under the repo, not /tmp, because snap Docker cannot read /tmp.
"""
import copy
import datetime
import json
import os
import re
import shutil
import stat
import subprocess
import tempfile
import tomllib
from pathlib import Path

import pytest

import instance
import kitconfig as config
import render

KIT = Path(__file__).resolve().parent
REPO = KIT.parent
EXAMPLE = KIT / "fixtures" / "example.toml"  # a made-up kit instance: the config of every test not about asus-node
FIXTURE = KIT / "fixtures" / "asus-node.toml"  # asus-node's record (an instance path)
GOLDEN = KIT / "fixtures" / "asus-node"  # asus-node's files as they were before the kit (master 06db28e)
NO_RECORD = instance.NO_RECORD
needs_record = pytest.mark.skipif(not instance.record(), reason=NO_RECORD)  # part of a record raises
SECRETS = "RCON_PASSWORD=dummy-rcon$not-interpolated\nLEVEL=world\n"
BRIDGE_SECRETS = "DISCORD_BRIDGE_TOKEN=dummy-token\n"


def fixture():
    return tomllib.loads(EXAMPLE.read_text())


def refused(data, match):
    with pytest.raises(config.ConfigError, match=match):
        config.validate(data)


def repo_copy(base, kit=EXAMPLE):
    """A checkout-shaped scratch dir: host/ (templates, scripts), kit/, docker-compose.yml, and kit.toml."""
    shutil.copytree(REPO / "host", base / "host", ignore=shutil.ignore_patterns("kit.env", "rendered", "__pycache__"))
    shutil.copy(REPO / "docker-compose.yml", base / "docker-compose.yml")
    shutil.copy(kit, base / "kit.toml")
    return base


@pytest.fixture
def repo(tmp_path):
    return repo_copy(tmp_path)


# ---- the schema ---------------------------------------------------------------------------------------------

@pytest.mark.parametrize("kit", [pytest.param(EXAMPLE, id="example"),
                                 pytest.param(FIXTURE, id="asus-node", marks=needs_record)])
def test_the_fixtures_are_valid(kit):
    cfg = config.load(kit)
    assert cfg["host"]["name"] == cfg["tailscale"]["host_name"]
    assert cfg["tailscale"]["server_address"].startswith(cfg["tailscale"]["host_name"] + ".")


def test_the_example_lists_exactly_the_schema_keys():
    """kit/kit.example.toml documents every key (optional ones commented out) and no key the schema lacks."""
    text = re.sub(r"^# ?(\[?[a-z_]+\]?( = |$))", r"\1", (KIT / "kit.example.toml").read_text(), flags=re.M)
    data = tomllib.loads(text)
    assert {s: set(t) for s, t in data.items()} == {s: set(k) for s, k in config.SCHEMA.items()}


@pytest.mark.parametrize("mutate,match", [
    (lambda d: d["host"].update(nmae="x"), r"host\.nmae: unknown key"),
    (lambda d: d.update(extra={"a": 1}), r"\[extra\]: unknown section"),
    (lambda d: d["host"].pop("user"), r"host\.user: missing"),
    (lambda d: d.pop("discord"), r"\[discord\]: missing section"),
    (lambda d: d["discord"].pop("profile"), r"discord\.profile: missing"),  # required: no silent default
    (lambda d: d["server"].update(max_players="12"), r"server\.max_players: must be int, not str"),
    (lambda d: d["server"].update(max_players=True), r"server\.max_players: must be int, not bool"),
    (lambda d: d["server"].update(memory=8), r"server\.memory: must be str, not int"),
    (lambda d: d["server"].update(eula_accepted="yes"), r"server\.eula_accepted: must be bool"),
    (lambda d: d["server"].update(open_date=datetime.datetime(2026, 10, 2, 9)), r"server\.open_date: must be date"),
    (lambda d: d["net"].update(dns="1.1.1.1"), r"net\.dns: must be list"),
    (lambda d: d["net"].update(dns=["1.1.1.1", 9]), r"net\.dns: must be a list of IPv4"),
    (lambda d: d.update(host=["x"]), r"\[host\]: must be a table"),
])
def test_unknown_keys_missing_keys_and_bad_types_are_refused(mutate, match):
    d = fixture()
    mutate(d)
    refused(d, match)


@pytest.mark.parametrize("section,key,value", [
    ("host", "user", 'al"ex'), ("host", "home", "/home/$USER"), ("net", "wifi_conn", "Home`id`"),
    ("net", "wired_conn", "Wired\nconnection"), ("host", "tz", "America/%i"), ("tailscale", "tag", "tag:@MC_USER@"),
])
def test_values_that_would_change_meaning_in_bash_or_systemd_are_refused(section, key, value):
    d = fixture()
    d[section][key] = value
    refused(d, rf"{section}\.{key}: must not contain")


@pytest.mark.parametrize("section,key,value,match", [
    ("host", "uid", 1001, "must be 1000"),
    ("host", "gid", 0, "must be 1000"),
    ("server", "eula_accepted", False, "only the person can accept the EULA"),
    ("net", "mode", "dhcp", r"net\.static_addr: set, but net\.mode is dhcp"),  # static keys under dhcp do nothing
    ("net", "mode", "ddhcp", "one of 'dhcp', 'static'"),
    ("server", "pause_when_empty_seconds", 0, "must be -1"),
    ("schedule", "maint_start", "03:00", "not wired yet"),
    ("pack", "mode", "vendor", "reserved"),
    ("server", "memory", "8GB", "heap size"),
    ("server", "difficulty", "nightmare", "one of"),
    ("host", "home", "/home/../root", "absolute path"),
    ("net", "static_addr", "192.0.2.5", "with a prefix"),
    ("net", "gateway", "10.0.0.1", "not inside"),
    ("net", "static_addr", "10.213.37.5/24", "overlaps the bridge network"),
    ("discord", "guild_id", "123", "Discord id"),
    ("discord", "guild_id", "000000000000000000", "no leading 0"),       # the example's placeholder
    ("discord", "bridge_bot_id", "000000000000000000", "no leading 0"),
    ("discord", "profile", "asus", "one of"),
    ("tailscale", "server_address", "kit-box.example-net.ts.net.", "MagicDNS name"),  # the trailing dot
    ("battery", "charge_cap", 101, "charge_cap"),
])
def test_values_the_kit_cannot_honour_are_refused(section, key, value, match):
    d = fixture()
    d.setdefault(section, {})[key] = value
    refused(d, match)


def test_every_problem_is_named_at_once():
    d = fixture()
    d["host"]["uid"], d["server"]["eula_accepted"], d["host"]["bogus"] = 7, False, 1
    with pytest.raises(config.ConfigError) as e:
        config.validate(d)
    assert all(s in str(e.value) for s in ("host.uid", "server.eula_accepted", "host.bogus"))


def test_load_refuses_a_missing_or_broken_file(tmp_path, monkeypatch):
    with pytest.raises(config.ConfigError, match="missing: copy kit/kit.example.toml"):
        config.load(tmp_path / "kit.toml")
    (tmp_path / "bad.toml").write_text("[host\n")
    with pytest.raises(config.ConfigError, match="not valid TOML"):
        config.load(tmp_path / "bad.toml")
    monkeypatch.setenv("MC_KIT_TOML", str(EXAMPLE))
    assert config.load()["host"]["name"] == fixture()["host"]["name"]


# ---- kit.env and templates ----------------------------------------------------------------------------------

def test_kit_env_round_trips_and_is_safe_to_source(tmp_path):
    values = render.kit_env_values(config.load(EXAMPLE))
    text = render.kit_env_text(values)
    assert render.parse_kit_env(text) == values
    f = tmp_path / "kit.env"
    f.write_text(text)
    out = subprocess.run(["bash", "-c", f'set -u; . {f}; for k in {" ".join(values)}; do printf "%s=%s\\n" "$k" "${{!k}}"; done'],
                         capture_output=True, text=True, check=True).stdout
    assert out == "".join(f"{k}={v}\n" for k, v in values.items())


def test_kit_env_refuses_a_value_bash_would_expand():
    with pytest.raises(render.RenderError):
        render.kit_env_text({"MC_USER": "$(reboot)"})  # config refuses it first; this is the second wall
    with pytest.raises(render.RenderError, match="not KEY"):
        render.parse_kit_env('MC_USER="a"\nexport MC_REPO=/x\n')


def test_the_host_bin_parsers_match_render():
    """kit_value is copied into three root-run scripts (no shared module on the host). All copies are identical and
    accept exactly the lines render writes."""
    blocks = set()
    for name in ("mc-heal", "mc-maint", "mc-netwatch"):
        text = (REPO / "host" / "bin" / name).read_text()
        blocks.add(text[text.index("# >>> kit_value"):text.index("# <<< kit_value")])
        assert "/home/" not in text and fixture()["host"]["user"] not in text, name
    assert len(blocks) == 1
    rx = re.search(r"KIT_ENV_LINE = re\.compile\((r'.*')\)", blocks.pop()).group(1)
    assert eval(rx) == render.KIT_ENV_LINE.pattern  # noqa: S307 (a literal from our own file)


def test_templates_refuse_unknown_and_leftover_tokens():
    assert render.render_template("User=@MC_USER@ %i @x@ a@b", {"MC_USER": "u"}) == "User=u %i @x@ a@b"
    with pytest.raises(render.RenderError, match="unknown token @MC_NOPE@"):
        render.render_template("User=@MC_NOPE@", {"MC_USER": "u"})
    with pytest.raises(render.RenderError, match="left after rendering"):
        render.render_template("User=@MC_USER@", {"MC_USER": "@MC_USER@"})


def test_every_file_under_host_systemd_must_be_a_template(repo):
    (repo / "host" / "systemd" / "stray.service").write_text("[Unit]\n")
    with pytest.raises(render.RenderError, match="not a .in template"):
        render.templates(repo)


# ---- THE KEY GATE: the asus-node fixture reproduces today's files ---------------------------------------------

# the units a render gives that 06db28e did not have (the golden set is the templates without these)
NEWER_THAN_06DB28E = {"mc-charge-cap.service"}


def golden_units():
    """The systemd files 06db28e had, by name: every template under host/systemd/ (.in stripped) but the newer ones.
    Derived from the templates, which every copy has, so the goldens' tests keep their cases where GOLDEN is gone."""
    root = REPO / "host" / "systemd"
    names = sorted(p.relative_to(root).as_posix().removesuffix(".in") for p in root.rglob("*") if p.is_file())
    return [n for n in names if n not in NEWER_THAN_06DB28E]


@needs_record
def test_gate_units_render_byte_for_byte_to_asus_nodes(tmp_path):
    repo = repo_copy(tmp_path, FIXTURE)
    assert render.main(["--repo", str(repo)]) == 0
    rendered = repo / "host" / "rendered" / "systemd"
    got = {p.relative_to(rendered).as_posix(): p.read_bytes() for p in rendered.rglob("*") if p.is_file()}
    want = {p.relative_to(GOLDEN / "systemd").as_posix(): p.read_bytes()
            for p in (GOLDEN / "systemd").rglob("*") if p.is_file()}
    assert sorted(got) == sorted(want) == golden_units()
    for name in want:
        assert got[name] == want[name], name


def _compose_config(d):
    out = subprocess.run(["docker", "compose", "config", "--format", "json"], cwd=d, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return out.stdout


@pytest.fixture
def scratch():
    """Under the repo (gitignored test-compose-*): snap Docker can't read /tmp or hidden dirs."""
    assert shutil.which("docker"), "docker CLI missing: the compose gate can't run"
    d = Path(tempfile.mkdtemp(prefix="test-compose-kit-", dir=REPO))
    yield d
    shutil.rmtree(d)


@needs_record
def test_gate_compose_config_is_unchanged_by_rendering_the_fixture(scratch):
    """Today's compose = this compose with each "${KEY:-default}" replaced by its default (the defaults are
    asus-node's values; at 06db28e that text equals master's modulo comments). Same dir, same dummy secrets:
    config before == config after rendering the fixture into .env and .env.bridge."""
    repo_copy(scratch, FIXTURE)
    gid = tomllib.loads(FIXTURE.read_text())["discord"]["guild_id"]
    compose = (scratch / "docker-compose.yml").read_text()
    literal, n = re.subn(r'"\$\{([A-Z_]+):-([^}]*)\}"', r'"\2"', compose)
    assert n == len(render.COMPOSE_KEYS)
    (scratch / ".env").write_text(SECRETS)
    (scratch / ".env.bridge").write_text(BRIDGE_SECRETS + f"DISCORD_GUILD_ID={gid}\n")
    (scratch / "docker-compose.yml").write_text(literal)
    today = _compose_config(scratch)
    (scratch / "docker-compose.yml").write_text(compose)
    assert render.main(["--repo", str(scratch)]) == 0
    assert _compose_config(scratch) == today
    env = (scratch / ".env").read_text()
    assert env.startswith(SECRETS) and "SEED" not in env and "OPS" not in env
    assert (scratch / ".env.bridge").read_text() == BRIDGE_SECRETS + f"DISCORD_GUILD_ID={gid}\n"  # unchanged bytes


def test_compose_reads_every_key_render_writes_and_nothing_else():
    """One home per value: each ${KEY:-...} in compose is a COMPOSE_KEY and vice versa (a key render writes that
    compose never reads would be config that silently does nothing, except via env_file)."""
    compose = (REPO / "docker-compose.yml").read_text()
    keys = re.findall(r'^\s+([A-Z_]+): "\$\{([A-Z_]+):-[^}]*\}"', compose, re.M)
    assert all(a == b for a, b in keys)  # each is interpolated into its own itzg name
    assert sorted(k for k, _ in keys) == sorted(render.COMPOSE_KEYS)


def test_a_kit_host_gets_its_own_values_and_its_secrets_survive(scratch):
    repo_copy(scratch)
    d = fixture()
    d["server"].update(memory="12G", difficulty="normal", seed=-42, owner_name="Steve_1")
    d["host"]["tz"] = "Europe/Berlin"
    d["discord"]["guild_id"] = "1000000000000000001"
    (scratch / "kit.toml").write_text(toml_dump(d))
    (scratch / ".env").write_text("# my secrets\n" + SECRETS + "MEMORY=4G")  # an old value, no final newline
    os.chmod(scratch / ".env", 0o644)
    (scratch / ".env.bridge").write_text(BRIDGE_SECRETS + "DISCORD_GUILD_ID=1\n")
    assert render.main(["--repo", str(scratch)]) == 0
    env = (scratch / ".env").read_text()
    assert env.startswith("# my secrets\n" + SECRETS + "MEMORY=12G\n")  # rewritten in place, other lines kept
    assert "SEED=-42\n" in env and "OPS=Steve_1\n" in env and "TZ=Europe/Berlin\n" in env
    assert stat.S_IMODE((scratch / ".env").stat().st_mode) == 0o600
    assert (scratch / ".env.bridge").read_text() == BRIDGE_SECRETS + "DISCORD_GUILD_ID=1000000000000000001\n"
    cfg = json.loads(_compose_config(scratch))
    m = cfg["services"]["minecraft"]["environment"]
    assert (m["MEMORY"], m["TZ"], m["DIFFICULTY"], m["SEED"], m["OPS"]) == ("12G", "Europe/Berlin", "normal",
                                                                             "-42", "Steve_1")
    assert m["RCON_PASSWORD"] == "dummy-rcon$$not-interpolated"  # raw: a literal $ (config prints it escaped)
    assert cfg["services"]["bridge"]["environment"]["DISCORD_GUILD_ID"] == "1000000000000000001"
    assert "MEMORY" not in cfg["services"]["bridge"].get("environment", {})


def toml_dump(d):
    """Enough TOML for the fixture's shapes (str, int, bool, date, list of str)."""
    def val(v):
        if isinstance(v, bool):
            return "true" if v else "false"
        if isinstance(v, (int, datetime.date)):
            return str(v) if not isinstance(v, int) else str(v)
        if isinstance(v, list):
            return "[" + ", ".join(val(x) for x in v) + "]"
        return json.dumps(v)
    return "".join(f"[{s}]\n" + "".join(f"{k} = {val(v)}\n" for k, v in t.items()) + "\n" for s, t in d.items())


# ---- the merge: secrets kept byte for byte ------------------------------------------------------------------

def test_merge_keeps_every_other_byte_and_rewrites_in_place():
    old = "# c\r\nA=1\r\nMEMORY=4G\r\n\n  # indented comment\nB=x=y"
    new = render.merge_env(old, {"MEMORY": "8G", "TZ": "UTC"})
    assert new == "# c\r\nA=1\r\nMEMORY=8G\r\n\n  # indented comment\nB=x=y\nTZ=UTC\n"
    assert render.merge_env(new, {"MEMORY": "8G", "TZ": "UTC"}) == new  # idempotent
    assert render.merge_env("", {"TZ": "UTC"}) == "TZ=UTC\n"


@pytest.mark.parametrize("old,match", [
    ("MEMORY=4G\nMEMORY=6G\n", "set twice"),
    ("export MEMORY=4G\n", "form render will not rewrite"),
    ("MEMORY = 4G\n", "form render will not rewrite"),
    ("  MEMORY=4G\n", "form render will not rewrite"),
])
def test_merge_refuses_a_file_that_means_two_things(old, match):
    with pytest.raises(render.RenderError, match=match):
        render.merge_env(old, {"MEMORY": "8G"})


def test_env_values_must_be_plain():
    for bad in ("a b", "a#b", "a$b", '"a"', ""):
        with pytest.raises(render.RenderError):
            render.check_env_value("OPS", bad)


def test_render_refuses_a_symlinked_or_foreign_env_and_writes_nothing(repo, monkeypatch):
    (repo / "elsewhere").write_text(SECRETS)
    (repo / ".env").symlink_to(repo / "elsewhere")
    assert render.main(["--repo", str(repo)]) == 2
    assert not (repo / "host" / "kit.env").exists()  # refused before the first write
    assert (repo / "elsewhere").read_text() == SECRETS
    (repo / ".env").unlink()
    (repo / ".env").write_text(SECRETS)
    real = os.geteuid()
    monkeypatch.setattr(render.os, "geteuid", lambda: real + 1)  # the file is now "someone else's"
    assert render.main(["--repo", str(repo)]) == 2
    assert (repo / ".env").read_text() == SECRETS and not (repo / "host" / "kit.env").exists()


@pytest.mark.parametrize("name", [".env", ".env.bridge"])
def test_render_waits_for_a_token_write_and_then_refuses(repo, monkeypatch, capsys, name):
    """kit/secret.py replacing a token in the second terminal holds the file's lock from its read to its rename. A
    render that read the file before that and wrote after would put the old token back (codex review 2026-10-16);
    held past the wait, render refuses and writes nothing."""
    import fcntl
    (repo / ".env").write_text(SECRETS)
    (repo / ".env.bridge").write_text(BRIDGE_SECRETS)
    monkeypatch.setattr(render, "LOCK_WAIT_SECS", 0.3)
    fd = os.open(repo / f"{name}.lock", os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    try:
        assert render.main(["--repo", str(repo)]) == 2
        assert f"{name} is being written by another kit command" in capsys.readouterr().err
        assert (repo / ".env").read_text() == SECRETS and (repo / ".env.bridge").read_text() == BRIDGE_SECRETS
        assert not (repo / "host" / "kit.env").exists()
    finally:
        os.close(fd)
    assert render.main(["--repo", str(repo)]) == 0


def test_render_refuses_root(repo, monkeypatch, capsys):
    monkeypatch.setattr(render.os, "geteuid", lambda: 0)
    assert render.main(["--repo", str(repo)]) == 2
    assert "refusing to render as root" in capsys.readouterr().err
    assert not (repo / "host" / "kit.env").exists() and not (repo / ".env").exists()


def test_writes_are_atomic_mode_600_and_leave_no_temp_files(repo):
    old = os.umask(0o077)  # a umask that would make kit.env unreadable to the group and others
    try:
        assert render.main(["--repo", str(repo)]) == 0
    finally:
        os.umask(old)
    for name in (".env", ".env.bridge"):
        assert stat.S_IMODE((repo / name).stat().st_mode) == 0o600, name
    assert stat.S_IMODE((repo / "host" / "kit.env").stat().st_mode) == 0o644
    assert not list(repo.rglob(".*.render-*.tmp"))


def test_a_failed_write_leaves_the_old_file(repo, monkeypatch):
    (repo / ".env").write_text(SECRETS)

    def boom(*a):
        raise OSError("disk full")
    monkeypatch.setattr(render.os, "replace", boom)
    with pytest.raises(OSError):
        render.render(repo, config.load(repo / "kit.toml"))
    assert (repo / ".env").read_text() == SECRETS and not list(repo.rglob(".*.render-*.tmp"))


def test_a_second_render_changes_nothing(repo, capsys):
    assert render.main(["--repo", str(repo)]) == 0
    mtimes = {p: p.stat().st_mtime_ns for p in repo.rglob("*") if p.is_file()}
    capsys.readouterr()
    assert render.main(["--repo", str(repo)]) == 0
    out = capsys.readouterr().out
    assert "written" not in out and "merged" not in out and "created" not in out
    assert {p: p.stat().st_mtime_ns for p in repo.rglob("*") if p.is_file()} == mtimes


# ---- --check (what setup.sh and check.sh run) ---------------------------------------------------------------

def test_check_is_clean_after_render_and_names_what_drifted(repo, capsys):
    assert render.main(["--repo", str(repo), "--check"]) == 1  # nothing rendered yet
    assert render.main(["--repo", str(repo)]) == 0
    assert render.main(["--repo", str(repo), "--check"]) == 0
    toml = (repo / "kit.toml").read_text()
    assert toml.count(f'user = "{fixture()["host"]["user"]}"') == 1
    (repo / "kit.toml").write_text(toml.replace(f'user = "{fixture()["host"]["user"]}"', 'user = "someone"'))
    capsys.readouterr()
    assert render.main(["--repo", str(repo), "--check"]) == 1
    out = capsys.readouterr().out
    assert "host/kit.env: stale" in out and "mc-server.service: stale" in out
    (repo / "kit.toml").write_text(toml)
    (repo / "host" / "rendered" / "systemd" / "old.service").write_text("x")
    assert render.main(["--repo", str(repo), "--check"]) == 1
    (repo / "host" / "rendered" / "systemd" / "old.service").unlink()
    (repo / ".env").write_text(SECRETS)  # the compose keys are gone
    assert render.main(["--repo", str(repo), "--check"]) == 1
    assert render.main(["--repo", str(repo), "--check", "--host-only"]) == 0  # setup.sh's scope: host files only


def test_check_writes_nothing(repo):
    before = sorted(p for p in repo.rglob("*"))
    render.main(["--repo", str(repo), "--check"])
    assert sorted(p for p in repo.rglob("*")) == before


def test_render_removes_a_unit_whose_template_is_gone(repo):
    assert render.main(["--repo", str(repo)]) == 0
    (repo / "host" / "systemd" / "mc-heal.timer.in").unlink()
    assert render.main(["--repo", str(repo)]) == 0
    assert not (repo / "host" / "rendered" / "systemd" / "mc-heal.timer").exists()


# ---- the secret files on disk: bytes, encodings, modes, temp files (verify:spine findings) -------------------

def test_render_keeps_every_other_byte_of_the_real_env_files(repo):
    """On the file path, not only merge_env: a CRLF line, a bare CR inside a value, and the characters
    str.splitlines would split on (\\x0b \\x0c \\x1c-\\x1e \\x85 \\u2028) all come back byte for byte. A key that only
    follows such a character (NOTE=a\\x85MEMORY=1G) is part of NOTE's value, as compose's dotenv reads it."""
    cfg = config.load(repo / "kit.toml")
    vals, gid = render.compose_env_values(cfg), cfg["discord"]["guild_id"]
    env = ("# c\r\nRCON_PASSWORD=ab\rcd\nLEVEL=wo\x0brld\x0c\x1c\x1d\x1e\x85 x\r\n"
           "NOTE=a\x85MEMORY=1G TZ=x\nMEMORY=4G\r\nLAST=no-eol").encode()
    bridge = "DISCORD_BRIDGE_TOKEN=t\rok\r\nDISCORD_GUILD_ID=1\r\n".encode()
    (repo / ".env").write_bytes(env)
    (repo / ".env.bridge").write_bytes(bridge)
    assert render.main(["--repo", str(repo)]) == 0
    want = env.replace(b"MEMORY=4G\r\n", f"MEMORY={vals['MEMORY']}\r\n".encode()) + b"\n" + "".join(
        f"{k}={v}\n" for k, v in vals.items() if k != "MEMORY").encode()
    assert (repo / ".env").read_bytes() == want
    assert (repo / ".env.bridge").read_bytes() == bridge.replace(b"=1\r\n", f"={gid}\r\n".encode())
    assert render.main(["--repo", str(repo), "--check"]) == 0  # what check reads is what render wrote


@pytest.mark.parametrize("name", [".env", ".env.bridge"])
@pytest.mark.parametrize("mode", [[], ["--check"]])
def test_a_secret_file_that_is_not_utf8_is_refused_by_name_and_never_shown(repo, capsys, name, mode):
    secret = b"RCON_PASSWORD=SECRETVALUE\xff\xfeX\nLEVEL=world\n"
    (repo / name).write_bytes(secret)
    assert render.main(["--repo", str(repo), *mode]) == 2  # a RenderError, not a traceback
    out = capsys.readouterr()
    assert f"render: {name} is not UTF-8 text" in out.err
    assert "SECRETVALUE" not in out.out + out.err and "\\xff" not in out.out + out.err
    assert (repo / name).read_bytes() == secret and not (repo / "host" / "kit.env").exists()


def test_check_names_each_drifted_key_and_never_a_value(repo, capsys):
    """The rollout control: a compose key in .env that disagrees with kit.toml is live now (compose reads it as
    ${KEY:-default}), so --check names it. Key names only, never the stale value or kit.toml's."""
    assert render.main(["--repo", str(repo)]) == 0
    cfg = config.load(repo / "kit.toml")
    env = (repo / ".env").read_text()
    env = re.sub(r"^MEMORY=.*$", "MEMORY=10G", env, flags=re.M)
    env = re.sub(r"^TZ=.*\n", "", env, flags=re.M)
    (repo / ".env").write_text(env)
    (repo / ".env.bridge").write_text("DISCORD_GUILD_ID=1000000000000000001\n")
    (repo / "host" / "rendered" / "systemd" / "mc-heal.timer").unlink()  # and one host output, for the scopes
    capsys.readouterr()
    assert render.main(["--repo", str(repo), "--check", "--env-only"]) == 1
    out = capsys.readouterr().out
    assert "STALE .env: MEMORY differs from kit.toml (compose already uses it)" in out
    assert "STALE .env: TZ missing" in out
    assert "STALE .env.bridge: DISCORD_GUILD_ID differs from kit.toml\n" in out
    assert out.count("STALE") == 3 and "host/" not in out  # --env-only: the env files, and only the drifted keys
    for value in ("10G", cfg["server"]["memory"], cfg["host"]["tz"], "1000000000000000001",
                  cfg["discord"]["guild_id"]):
        assert value not in out
    assert render.main(["--repo", str(repo), "--check", "--host-only"]) == 1
    out = capsys.readouterr().out
    assert out.count("STALE") == 1 and "STALE host/rendered/systemd/mc-heal.timer: missing" in out
    assert render.main(["--repo", str(repo), "--check"]) == 1
    assert capsys.readouterr().out.count("STALE") == 4


@pytest.mark.parametrize("text", ["", "MEMORY=8G\n", "MEMORY=8G\r\nTZ=UTC", "MEMORY=7G\r\nTZ=UTC\n",
                                  "X=a\x85MEMORY=8G\nTZ=UTC\n", "MEMORY=8G\nTZ=UTC\r\n", "TZ=UTC\nMEMORY=8G"])
def test_check_and_render_agree_on_what_drift_is(text):
    updates = {"MEMORY": "8G", "TZ": "UTC"}
    assert bool(render.env_drift(text, updates)) == (render.merge_env(text, updates) != text)


def test_check_refuses_a_secret_file_left_at_mode_644(repo, capsys):
    assert render.main(["--repo", str(repo)]) == 0
    os.chmod(repo / ".env", 0o644)
    capsys.readouterr()
    assert render.main(["--repo", str(repo), "--check"]) == 1
    assert "STALE .env: mode 644, want 600" in capsys.readouterr().out


def test_the_secret_temp_files_are_created_0600_not_chmodded_later(repo, monkeypatch):
    """The secret is written and fsynced into the temp file, so it must be 600 from its creation, under a umask
    that would not hide a 644 create."""
    seen, real = {}, os.fsync

    def spy(fd):
        st = os.fstat(fd)
        if stat.S_ISREG(st.st_mode):
            seen[os.readlink(f"/proc/self/fd/{fd}")] = stat.S_IMODE(st.st_mode)
        return real(fd)
    monkeypatch.setattr(render.os, "fsync", spy)
    (repo / ".env").write_text(SECRETS)
    old = os.umask(0o022)
    try:
        assert render.main(["--repo", str(repo)]) == 0
    finally:
        os.umask(old)
    temps = [str(render.temp_path(repo / n)) for n in render.ENV_FILES]
    assert all(t in seen for t in temps), sorted(seen)
    assert {t: seen[t] for t in temps} == {t: 0o600 for t in temps}


def test_a_stale_same_pid_temp_is_refused_before_any_write(repo, capsys):
    tmp = render.temp_path(repo / ".env")  # .env is written last: the refusal must come before kit.env
    tmp.write_text("left by a render that died\n")
    assert render.main(["--repo", str(repo)]) == 2
    assert f"render: {tmp.name} exists" in capsys.readouterr().err
    assert not (repo / "host" / "kit.env").exists() and not (repo / ".env").exists()
    assert tmp.read_text() == "left by a render that died\n"  # not ours to delete


def test_write_atomic_refuses_a_temp_it_did_not_create(tmp_path):
    tmp = render.temp_path(tmp_path / "f")
    tmp.write_text("old")
    with pytest.raises(render.RenderError, match="delete it, then re-run"):
        render.write_atomic(tmp_path / "f", "new", 0o600)
    assert tmp.read_text() == "old" and not (tmp_path / "f").exists()


def test_every_render_temp_file_is_gitignored():
    """write_atomic's temp names (host/.kit.env.render-N.tmp, ..env.render-N.tmp: note the two dots) are ignored,
    so a crash never leaves an untracked file next to the secrets. Paths only: the real .env is never read."""
    outputs = [REPO / "host" / "kit.env", REPO / "host" / "rendered" / "systemd" / "mc-server.service",
               *(REPO / n for n in render.ENV_FILES)]
    for p in outputs:
        tmp = render.temp_path(p).relative_to(REPO).as_posix()
        r = subprocess.run(["git", "check-ignore", "-q", "--no-index", tmp], cwd=REPO)
        assert r.returncode == 0, f"{tmp} is not gitignored"


@pytest.mark.parametrize("flag", ["--host-only", "--env-only"])
def test_a_scope_flag_without_check_renders_nothing(repo, flag):
    with pytest.raises(SystemExit) as e:
        render.main(["--repo", str(repo), flag])
    assert e.value.code == 2 and not (repo / "host" / "kit.env").exists() and not (repo / ".env").exists()


def test_the_agents_pre_pull_gate_counts_exactly_the_compose_keys():
    """AGENTS.md's pre-pull grep is a second statement of COMPOSE_KEYS: this test is its alarm."""
    agents = (REPO / "AGENTS.md").read_text()
    m = re.findall(r"grep -cE '\^\(([A-Z_|]+)\)=' \.env", agents)
    assert len(m) == 1 and m[0].split("|") == list(render.COMPOSE_KEYS)


# ---- the network rules (plan gap #3: dhcp by default, static and the Wi-Fi fallback on request) ---------------

def dhcp_net(**net):
    d = fixture()
    d["net"] = {"mode": "dhcp", **net}
    return d


@pytest.mark.parametrize("net", [
    {"wired_conn": "Wired connection 1"},                       # a wired laptop, no fallback: the default
    {"wired_conn": "Wired connection 1", "wifi_conn": "Home"},  # wired + the Wi-Fi fallback, on request
    {"wifi_conn": "Home"},                                      # a Wi-Fi-only laptop
])
def test_dhcp_shapes_validate(net):
    config.validate(dhcp_net(**net))


@pytest.mark.parametrize("data,match", [
    (dhcp_net(), r"net: name at least one connection"),
    (dhcp_net(wired_conn="W", gateway="192.168.1.1"), r"net\.gateway: set, but net\.mode is dhcp"),
    (dhcp_net(wifi_conn="Home", mode="static", static_addr="192.168.1.5/24", gateway="192.168.1.1",
              dns=["1.1.1.1"]), r"net\.mode: static needs net\.wired_conn"),
    (dhcp_net(wired_conn="W", mode="static", static_addr="192.168.1.5/24", dns=["1.1.1.1"]),
     r"net\.gateway: missing \(net\.mode is static\)"),
])
def test_the_net_rules_refuse(data, match):
    refused(data, match)


def test_a_dhcp_kit_env_leaves_the_unconfigured_keys_empty():
    values = render.kit_env_values(config.validate(dhcp_net(wifi_conn="Home")))
    assert values["MC_NET_MODE"] == "dhcp" and values["MC_WIFI_CONN"] == "Home"
    assert all(values[k] == "" for k in ("MC_WIRED_CONN", "MC_WIRED_ADDR", "MC_WIRED_GW", "MC_WIRED_DNS"))
    assert render.parse_kit_env(render.kit_env_text(values)) == values  # an empty value round-trips


@pytest.mark.parametrize("kit", [pytest.param(EXAMPLE, id="example"),
                                 pytest.param(FIXTURE, id="asus-node", marks=needs_record)])
def test_the_fixtures_keep_static_and_the_fallback(kit):
    values = render.kit_env_values(config.load(kit))
    assert values["MC_NET_MODE"] == "static" and values["MC_WIFI_CONN"] and values["MC_WIRED_ADDR"]
    assert values["MC_CHARGE_CAP"] == "none"  # no [battery]: no cap unit, as before the kit


# ---- the charge-cap unit: rendered only when kit.toml sets a cap --------------------------------------------

def test_the_charge_cap_unit_is_rendered_only_with_a_cap_and_removed_without(repo):
    d = fixture()
    d["battery"] = {"charge_cap": 80}
    (repo / "kit.toml").write_text(toml_dump(d))
    assert render.main(["--repo", str(repo)]) == 0
    unit = repo / "host" / "rendered" / "systemd" / "mc-charge-cap.service"
    assert "ExecStart=/usr/local/sbin/mc-charge-cap 80\n" in unit.read_text()
    assert 'MC_CHARGE_CAP="80"' in (repo / "host" / "kit.env").read_text()
    d["battery"] = {"charge_cap": "none"}
    (repo / "kit.toml").write_text(toml_dump(d))
    assert render.main(["--repo", str(repo), "--check"]) == 1  # the leftover unit is stale
    assert render.main(["--repo", str(repo)]) == 0
    assert not unit.exists()
    assert render.main(["--repo", str(repo), "--check"]) == 0


def test_a_conditional_naming_no_template_is_refused(repo, monkeypatch):
    monkeypatch.setitem(render.CONDITIONAL, "mc-gone.service", lambda cfg: True)
    with pytest.raises(render.RenderError, match="CONDITIONAL names no template: mc-gone.service"):
        render.templates(repo)


# ---- pause-when-empty: an itzg env key, written only when kit.toml sets it -----------------------------------

def test_pause_when_empty_reaches_the_container_only_when_set_and_is_never_silently_dropped(scratch):
    repo_copy(scratch)
    (scratch / ".env").write_text(SECRETS)
    (scratch / ".env.bridge").write_text(BRIDGE_SECRETS)
    assert render.main(["--repo", str(scratch)]) == 0
    m = json.loads(_compose_config(scratch))["services"]["minecraft"]["environment"]
    assert "PAUSE_WHEN_EMPTY_SECONDS" not in m  # unset (as on asus-node): server.properties stays hand-managed
    d = fixture()
    d["server"]["pause_when_empty_seconds"] = -1
    (scratch / "kit.toml").write_text(toml_dump(d))
    assert render.main(["--repo", str(scratch)]) == 0
    assert "PAUSE_WHEN_EMPTY_SECONDS=-1\n" in (scratch / ".env").read_text()
    m = json.loads(_compose_config(scratch))["services"]["minecraft"]["environment"]
    assert m["PAUSE_WHEN_EMPTY_SECONDS"] == "-1"  # itzg's name for pause-when-empty-seconds
    del d["server"]["pause_when_empty_seconds"]  # dropping the key would leave -1 in force, unsaid
    (scratch / "kit.toml").write_text(toml_dump(d))
    before = (scratch / ".env").read_text()
    assert render.main(["--repo", str(scratch)]) == 2
    assert (scratch / ".env").read_text() == before
    assert render.main(["--repo", str(scratch), "--check"]) == 2


# ---- the asus-node fixture claim after every lane merged (docs/instance/asus-node.md "Rollout") -----------------
# Units, `docker compose config` and servers.dat: byte-identical to 06db28e. host/kit.env: the spine's, plus
# MC_CHARGE_CAP="none", inert there. One test per part; the units and servers.dat parts are
# test_gate_units_render_byte_for_byte_to_asus_nodes and pack/test_build.py's
# test_the_fixture_reproduces_asus_nodes_servers_dat_byte_for_byte.

ROLLOUT_CLAIM = ("Rendering `kit/fixtures/asus-node.toml` gives the systemd units, the `docker compose config` and "
                 "the pack's `servers.dat` byte-identical to 06db28e; `host/kit.env` is the spine's plus one line, "
                 "`MC_CHARGE_CAP=\"none\"`, which is inert on asus-node")


@needs_record
def test_gate_compose_config_equals_06db28es(scratch):
    """kit/fixtures/asus-node/docker-compose.yml is 06db28e's compose, byte for byte. With the same secrets in the same
    directory, its config is the config of today's compose after the fixture is rendered into .env."""
    repo_copy(scratch, FIXTURE)
    gid = tomllib.loads(FIXTURE.read_text())["discord"]["guild_id"]
    (scratch / ".env").write_text(SECRETS)
    (scratch / ".env.bridge").write_text(BRIDGE_SECRETS + f"DISCORD_GUILD_ID={gid}\n")
    compose = (scratch / "docker-compose.yml").read_bytes()
    (scratch / "docker-compose.yml").write_bytes((GOLDEN / "docker-compose.yml").read_bytes())
    at_06db28e = _compose_config(scratch)
    (scratch / "docker-compose.yml").write_bytes(compose)
    assert render.main(["--repo", str(scratch)]) == 0
    assert _compose_config(scratch) == at_06db28e


@needs_record
def test_the_fixture_kit_env_is_the_spines_plus_the_inert_charge_cap(tmp_path):
    """kit/fixtures/asus-node/kit.env is the spine's render of the fixture (62c28f1, rendered once and kept). Every
    lane since added one line: MC_CHARGE_CAP="none" (no [battery]: no unit, setup.sh step 13 does nothing)."""
    repo = repo_copy(tmp_path, FIXTURE)
    assert render.main(["--repo", str(repo)]) == 0
    got = (repo / "host" / "kit.env").read_text()
    assert got == (GOLDEN / "kit.env").read_text() + 'MC_CHARGE_CAP="none"\n'
    assert not (repo / "host" / "rendered" / "systemd" / "mc-charge-cap.service").exists()


@needs_record
@pytest.mark.parametrize("golden,at", [("docker-compose.yml", "docker-compose.yml")] + [
    (f"systemd/{name}", f"host/systemd/{name}") for name in golden_units()])
def test_the_goldens_are_06db28es_bytes(golden, at):
    """Where the history is here, the goldens are checked against it (a shallow clone without 06db28e skips, and says
    so: the goldens then stand on this test's last run)."""
    r = subprocess.run(["git", "cat-file", "-e", "06db28e^{commit}"], cwd=REPO, capture_output=True)
    if r.returncode != 0:
        pytest.skip("06db28e is not in this clone's history")
    want = subprocess.run(["git", "show", f"06db28e:{at}"], cwd=REPO, capture_output=True, check=True).stdout
    assert (GOLDEN / golden).read_bytes() == want


@needs_record
def test_the_instance_record_states_the_fixture_claim_exactly():
    doc = REPO / "docs" / "instance" / "asus-node.md"
    assert ROLLOUT_CLAIM in doc.read_text().replace("\n  ", " ").replace("\n", " ")


def test_the_sticky_pause_key_is_read_on_merge_envs_own_lines(repo):
    """env_keys() splits like merge_env (on "\\n" only): a PAUSE_WHEN_EMPTY_SECONDS= that only follows a \\x85 inside
    another key's value is part of that value, as compose reads it, so it is not a key render would leave behind.
    A real line of it is (the refusal stays)."""
    assert render.env_keys("NOTE=a\x85PAUSE_WHEN_EMPTY_SECONDS=-1\nLEVEL=world\n") == {"NOTE", "LEVEL"}
    (repo / ".env").write_bytes("RCON_PASSWORD=x\nNOTE=a\x85PAUSE_WHEN_EMPTY_SECONDS=-1\nLEVEL=world\n".encode())
    assert render.main(["--repo", str(repo)]) == 0
    (repo / ".env").write_bytes(b"RCON_PASSWORD=x\nPAUSE_WHEN_EMPTY_SECONDS=-1\nLEVEL=world\n")
    assert render.main(["--repo", str(repo)]) == 2


# ---- pack.mode = "custom": pack-custom/ and the override that points compose at it (docs/kit/CUSTOM-PACK.md) ----

CUSTOM_PACK = '[pack]\nname = "Sky Isles"\nversion = "1.2.0"\nminecraft = "26.1.2"\nfabric_loader = "0.19.5"\n'


def custom_repo(base, pack=CUSTOM_PACK):
    repo_copy(base)
    d = fixture()
    d["pack"]["mode"] = "custom"
    (base / "kit.toml").write_text(toml_dump(d))
    (base / "pack-custom").mkdir()
    (base / "pack-custom" / "mods.toml").write_text(pack)
    (base / ".env").write_text(SECRETS)
    (base / ".env.bridge").write_text(BRIDGE_SECRETS)
    return base


def test_custom_is_a_pack_mode_and_vendor_is_still_reserved():
    d = fixture()
    d["pack"]["mode"] = "custom"
    assert config.validate(d)["pack"]["mode"] == "custom"
    assert config.pack_dir(d, Path("/r")) == Path("/r/pack-custom")
    assert config.pack_dir(fixture(), Path("/r")) == Path("/r/pack")


def test_the_mrpack_name_rule_gives_vanilla_extendeds_file_unchanged():
    """One rule for every pack's file name (pack/build.py writes it, the override names it); for Vanilla Extended
    it is the very name docker-compose.yml runs."""
    ve = config.pack_section(config.VE_DIR)
    compose = (REPO / "docker-compose.yml").read_text()
    assert config.MODPACK_LINE.search(compose).group(1) == config.mrpack_name(ve["name"], ve["version"], 1)
    assert config.mrpack_name("Sky Isles", "1.2.0", 1) == "sky-isles-1.2.0-ch1.mrpack"


@pytest.mark.parametrize("pack,match", [
    ('[pack]\nname = "Sky Isles"\n', r"version is missing.*minecraft is missing"),
    (CUSTOM_PACK.replace('"Sky Isles"', '"Sky \\"Isles"'), "1-40 letters"),
    (CUSTOM_PACK.replace('"Sky Isles"', '"Sky #Isles"'), "1-40 letters"),
    (CUSTOM_PACK.replace('"Sky Isles"', '"vanilla extended"'), "Vanilla Extended's name"),
    (CUSTOM_PACK.replace('"1.2.0"', '"1.2"'), "like 1.0.0"),
    ("[mod]\n", "no readable"),
])
def test_a_custom_pack_section_the_kit_cannot_name_things_after_is_refused(tmp_path, pack, match):
    (tmp_path / "mods.toml").write_text(pack)
    with pytest.raises(config.ConfigError, match=match):
        config.pack_section(tmp_path)


def test_a_missing_custom_pack_points_at_the_procedure(tmp_path):
    with pytest.raises(config.ConfigError, match="CUSTOM-PACK.md"):
        config.pack_section(tmp_path)


def test_the_live_pack_is_the_overrides_when_there_is_one(tmp_path):
    shutil.copy(REPO / "docker-compose.yml", tmp_path / "docker-compose.yml")
    compose_line = config.MODPACK_LINE.search((REPO / "docker-compose.yml").read_text()).group(1)
    assert config.live_pack_file(tmp_path) == compose_line  # no override: compose's own line
    (tmp_path / config.OVERRIDE_NAME).write_text('      MODRINTH_MODPACK: "/pack/sky-isles-1.2.0-ch1.mrpack"\n')
    assert config.live_pack_file(tmp_path) == "sky-isles-1.2.0-ch1.mrpack"
    (tmp_path / config.OVERRIDE_NAME).write_text("services: {}\n")  # an override that names none: compose's line
    assert config.live_pack_file(tmp_path).startswith("vanilla-extended-")
    (tmp_path / "docker-compose.yml").write_text("services: {}\n")
    with pytest.raises(config.ConfigError, match="names no MODRINTH_MODPACK"):
        config.live_pack_file(tmp_path)


def test_vanilla_extended_renders_no_override(repo):
    assert render.main(["--repo", str(repo)]) == 0
    assert not (repo / config.OVERRIDE_NAME).exists()
    assert render.main(["--repo", str(repo), "--check"]) == 0


def test_a_custom_pack_renders_the_override_compose_runs(scratch):
    custom_repo(scratch)
    assert render.main(["--repo", str(scratch), "--check"]) == 1  # missing until rendered
    assert render.main(["--repo", str(scratch)]) == 0
    override = scratch / config.OVERRIDE_NAME
    assert override.read_text().startswith(render.OVERRIDE_HEADER)
    assert stat.S_IMODE(override.stat().st_mode) == 0o644
    assert render.main(["--repo", str(scratch), "--check"]) == 0
    m = json.loads(_compose_config(scratch))["services"]["minecraft"]["environment"]
    assert m["MODRINTH_MODPACK"] == "/pack/sky-isles-1.2.0-ch1.mrpack"
    assert m["MOTD"] == "Sky Isles · pack 1.2.0"
    assert config.live_pack_file(scratch) == "sky-isles-1.2.0-ch1.mrpack"  # what backup, standup and sync read
    # a version bump (the owner's chapter) re-renders, world or not
    (scratch / "mc_data" / "world").mkdir(parents=True)
    (scratch / "mc_data" / "world" / "level.dat").write_bytes(b"x")
    (scratch / "pack-custom" / "mods.toml").write_text(CUSTOM_PACK.replace("1.2.0", "1.3.0"))
    assert render.main(["--repo", str(scratch), "--check"]) == 1
    assert render.main(["--repo", str(scratch)]) == 0
    assert config.live_pack_file(scratch) == "sky-isles-1.3.0-ch1.mrpack"


def test_an_override_render_did_not_write_is_never_replaced(tmp_path, capsys):
    custom_repo(tmp_path)
    (tmp_path / config.OVERRIDE_NAME).write_text("services: {}\n")
    assert render.main(["--repo", str(tmp_path)]) == 2
    assert "was not written by kit/render.py" in capsys.readouterr().err
    assert (tmp_path / config.OVERRIDE_NAME).read_text() == "services: {}\n"
    d = fixture()  # Vanilla Extended leaves someone else's override alone too
    (tmp_path / "kit.toml").write_text(toml_dump(d))
    assert render.main(["--repo", str(tmp_path)]) == 0
    assert (tmp_path / config.OVERRIDE_NAME).read_text() == "services: {}\n"


def test_leaving_custom_removes_the_override_only_while_there_is_no_world(tmp_path, capsys):
    custom_repo(tmp_path)
    assert render.main(["--repo", str(tmp_path)]) == 0
    (tmp_path / "kit.toml").write_text(toml_dump(fixture()))  # back to vanilla-extended
    assert render.main(["--repo", str(tmp_path), "--check"]) == 1
    assert "rendered for a custom pack, but pack.mode is vanilla-extended" in capsys.readouterr().out
    (tmp_path / "mc_data" / "world").mkdir(parents=True)
    (tmp_path / "mc_data" / "world" / "level.dat").write_bytes(b"x")
    assert render.main(["--repo", str(tmp_path)]) == 2  # the world stays on its pack
    assert "a world stays on its pack" in capsys.readouterr().err
    assert (tmp_path / config.OVERRIDE_NAME).exists()
    shutil.rmtree(tmp_path / "mc_data")
    assert render.main(["--repo", str(tmp_path)]) == 0
    assert not (tmp_path / config.OVERRIDE_NAME).exists()


def test_switching_to_custom_under_an_existing_world_is_refused(tmp_path, capsys):
    custom_repo(tmp_path)
    (tmp_path / "mc_data" / "world").mkdir(parents=True)
    (tmp_path / "mc_data" / "world" / "level.dat").write_bytes(b"x")
    assert render.main(["--repo", str(tmp_path)]) == 2
    assert "a world stays on its pack" in capsys.readouterr().err
    assert not (tmp_path / config.OVERRIDE_NAME).exists() and not (tmp_path / "host" / "kit.env").exists()


def test_custom_mode_without_its_pack_refuses_by_name(tmp_path, capsys):
    custom_repo(tmp_path)
    (tmp_path / "pack-custom" / "mods.toml").unlink()
    assert render.main(["--repo", str(tmp_path)]) == 2
    assert "pack-custom/mods.toml is missing" in capsys.readouterr().err


# ---- no Discord (discord.enabled = false: HUMAN-STEPS step 23's second question) -----------------------------------

def no_discord(**extra):
    d = fixture()
    d["discord"] = {"enabled": False, **extra}
    return d


def no_discord_toml(text):
    return re.sub(r"(?ms)^\[discord\]\n.*?(?=^\[)", "[discord]\nenabled = false\n\n", text)


def test_no_discord_is_the_enabled_key_alone():
    cfg = config.validate(no_discord())
    assert config.discord_enabled(cfg) is False
    assert config.discord_enabled(config.load(EXAMPLE)) is True  # absent: Discord, as every kit.toml before it
    d = fixture()
    d["discord"]["enabled"] = True
    assert config.discord_enabled(config.validate(d)) is True


@pytest.mark.parametrize("key", ["guild_id", "bridge_bot_id", "alert_channel", "invite_lockdown", "profile"])
def test_no_discord_refuses_a_discord_key_that_would_do_nothing(key):
    refused(no_discord(**{key: fixture()["discord"][key]}), rf"discord\.{key}: set, but discord\.enabled is false")


def test_discord_still_requires_every_key_and_enabled_must_be_a_bool():
    d = fixture()
    d["discord"]["enabled"] = True
    del d["discord"]["guild_id"]
    refused(d, r"discord\.guild_id: missing")
    d = fixture()
    d["discord"]["enabled"] = "no"
    refused(d, r"discord\.enabled: must be bool")
    refused({**fixture(), "discord": {}}, r"discord\.guild_id: missing")  # an empty section is not "no Discord"


def test_render_with_no_discord_writes_no_bridge_env_and_checks_none(repo):
    (repo / "kit.toml").write_text(no_discord_toml((repo / "kit.toml").read_text()))
    (repo / ".env").write_text(SECRETS)
    assert render.main(["--repo", str(repo)]) == 0
    assert not (repo / ".env.bridge").exists()
    assert 'MC_DISCORD="off"\n' in (repo / "host" / "kit.env").read_text()
    assert render.main(["--repo", str(repo), "--check"]) == 0
    assert render.main(["--repo", str(repo), "--check", "--env-only"]) == 0


def test_render_with_discord_still_writes_and_checks_the_bridge_env(repo, capsys):
    (repo / ".env").write_text(SECRETS)
    assert render.main(["--repo", str(repo)]) == 0
    assert (repo / ".env.bridge").read_text() == f"DISCORD_GUILD_ID={fixture()['discord']['guild_id']}\n"
    assert "MC_DISCORD" not in (repo / "host" / "kit.env").read_text()
    (repo / ".env.bridge").unlink()
    assert render.main(["--repo", str(repo), "--check", "--env-only"]) == 1
    assert "STALE .env.bridge: missing" in capsys.readouterr().out


def test_compose_parses_with_no_bridge_env(scratch):
    """No Discord means no .env.bridge at all: compose (required: false) must still parse, or every P4/P8 gate
    would fail on a no-Discord kit."""
    repo_copy(scratch)
    (scratch / "kit.toml").write_text(no_discord_toml((scratch / "kit.toml").read_text()))
    (scratch / ".env").write_text(SECRETS)
    assert render.main(["--repo", str(scratch)]) == 0
    r = subprocess.run(["docker", "compose", "config", "-q"], cwd=scratch, capture_output=True, text=True)
    assert r.returncode == 0 and not (scratch / ".env.bridge").exists()


def _mutant(tmp_path, name, guard, mutant):
    import importlib.util
    src = (KIT / f"{name}.py").read_text()
    assert src.count(guard) == 1, f"stale or ambiguous mutation pattern {guard!r}"
    path = tmp_path / f"{name}_mut.py"
    path.write_text(src.replace(guard, mutant))
    spec = importlib.util.spec_from_file_location(f"{name}_mut", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_choice_guard_catches_its_mutant(tmp_path):
    """Without _discord_off, enabled = false alone is refused for the ids it leaves out (the test above passes)."""
    mod = _mutant(tmp_path, "kitconfig", '    return isinstance(table, dict) and table.get("enabled") is False',
                  "    return False")
    with pytest.raises(mod.ConfigError, match=r"discord\.guild_id: missing"):
        mod.validate(no_discord())


def test_the_leftover_key_guard_catches_its_mutant(tmp_path):
    """Without it, a guild id left under enabled = false is silently taken."""
    mod = _mutant(tmp_path, "kitconfig", "                if key in table:\n                    errors.append(",
                  "                if False:\n                    errors.append(")
    mod.validate(no_discord(guild_id=fixture()["discord"]["guild_id"]))


def test_the_bridge_env_guard_catches_its_mutant(repo, tmp_path):
    """Without the env_updates filter, render cannot render a no-Discord kit at all (no guild id to write)."""
    mod = _mutant(tmp_path, "render", '    if kitconfig.discord_enabled(cfg):\n        out[".env.bridge"]',
                  '    if True:\n        out[".env.bridge"]')
    (repo / "kit.toml").write_text(no_discord_toml((repo / "kit.toml").read_text()))
    (repo / ".env").write_text(SECRETS)
    with pytest.raises(KeyError):
        mod.main(["--repo", str(repo)])
    assert not (repo / ".env.bridge").exists()


def test_the_kit_env_guard_catches_its_mutant(tmp_path):
    """Without the condition, a Discord kit (asus-node) would get MC_DISCORD in its kit.env: a changed golden."""
    mod = _mutant(tmp_path, "render", '**({} if kitconfig.discord_enabled(cfg) else {"MC_DISCORD": "off"})',
                  '**{"MC_DISCORD": "off"}')
    assert "MC_DISCORD" in mod.kit_env_values(config.load(EXAMPLE))
