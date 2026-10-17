"""Pure-function tests for pack/build.py. Run: uv run --with pytest pytest pack/"""
import io
import json
import os
import re
import subprocess
import sys
import zipfile

import pytest

import build

# The tests' kit.toml: a made-up kit instance. asus-node's own record (kit/fixtures/asus-node.toml) is read only by
# the tests about that server, which skip where a copy dropped it (kit/INSTANCE-PATHS; kit/instance.py decides, and
# part of a record raises, so a damaged live tree fails instead of skipping).
EXAMPLE = build.ROOT / "kit" / "fixtures" / "example.toml"
FIXTURE = build.ROOT / "kit" / "fixtures" / "asus-node.toml"
build._kitconfig()  # puts kit/ on sys.path
import instance  # noqa: E402

NO_RECORD = instance.NO_RECORD
RECORD = instance.record()
ADDR = build.server_address(EXAMPLE)
B = {"minecraft": "26.1.2", "fabricloader": "0.19.5", "java": "25"}


# Every row verified against fabric-loader 0.19.5: VersionPredicate.parse(pred).test(Version.parse(v)).
@pytest.mark.parametrize("v,pred,ok", [
    ("26.1.2", "*", True),
    ("26.1.2", "26.1.2", True),
    ("26.1.2", "26.1.x", True),
    ("26.2", "26.1.x", False),
    ("26.1.2", ">=26.1 <26.2", True),
    ("26.2.0", ">=26.1 <26.2", False),
    ("26.1.2", "~26.1", True),
    ("26.2", "~26.1", False),
    ("26.1.2", "~26.1.1", True),
    ("27.0", "^26.1", False),
    ("26.9", "^26.1", True),
    ("0.19.5", ">=0.19.5", True),
    ("0.19.4", ">=0.19.5", False),
    ("26.1.2-Fabric-6.1.3", ">=26.1.2-Fabric-6.1.3", True),
    ("26.1.2-Fabric-6.1.2", ">=26.1.2-Fabric-6.1.3", False),
    ("1.0.0-beta.2", ">=1.0.0", False),      # a pre-release sorts below its release
    ("1.0.0+build.7", "1.0.0", True),        # build metadata ignored
    ("26.1.2", ["<26", ">=26.1"], True),     # list = OR
    ("25", ">=21", True),
    # trailing hyphen = the lowest pre-release of that version
    ("8.0.0-beta.1", "<8.0.0-", False),
    ("8.0.0-alpha.1", ">=8.0.0-", True),
    ("26.1-rc.2", ">=26.1-", True),
    ("8.0.0", "<=8.0.0-", False),
    ("26.1.2", "~26.1-", True),
    ("8.0.0-", "8.0.0-", True),
    ("8.0.0-", "<8.0.0-alpha", True),
    ("7.9.9", "<8.0.0-", True),
    ("8.0.0", "<8.0.0-", False),
    ("26.1-rc.2", "~26.1-", True),
    ("26.2-alpha.1", "~26.1-", False),
    ("26.2-", "~26.1-", False),
    ("26.1.2", "26.1.2-", False),
    ("1.0.0-alpha", ">1.0.0-alpha-", False),
    # ~ and ^ are "at least, same major[.minor]", not an interval with a release upper bound
    ("26.1.2-rc1", "~26.1.2", False),
    ("26.1.2-rc1", "^26.1.2", False),
    ("26.1.2", "~26", False),
    ("26.1.2", "^26", True),
    ("26.1.2", "~26.1.2.5", False),
    # x-ranges: 26.1.x means ~26.1-, 26.x means ^26-, 26.1.2.x means >=26.1.2- <26.1.3-
    ("26.1-rc.2", "26.1.x", True),
    ("26.1.2-beta", "26.1.x", True),
    ("26", "26.1.x", False),
    ("26.1.2", "26.x", True),
    ("26.1.2", "26.1.2.x", True),
    ("1.2.3", "1.x.x", True),
    ("1.2.3", "1.x.3", False),
    ("1.2.3", "x", False),
    # component padding and equality
    ("26.1.2", "=26.1", False),
    ("26.1", "=26.1.0", True),
    ("1.2.0.0", "=1.2", True),
    ("26.1.2", "<26.1.2.0.1", True),
    # pre-release ordering: numeric < alphanumeric, numeric by length then digits, ASCII case
    ("1.0.0-alpha.10", ">1.0.0-alpha.9", True),
    ("1.0.0-alpha.beta", ">1.0.0-alpha.1", True),
    ("1.0.0-1a", ">1.0.0-2", True),
    ("1.0.0-01", ">1.0.0-1", True),
    ("1.0.0-alpha", "<1.0.0-alpha.1", True),
    ("1.0.0-Alpha", "<1.0.0-alpha", True),
    ("26.1.2", ">1.19-alpha.22.11.a", True),
    # non-semantic versions: Fabric's StringVersion only string-equals
    ("mc26.1.2-fabric", ">=1.0", False),
    ("mc1", "mc1", True),
    ("mc1", ">=mc1", True),
    ("1.x", "1.x", False),
])
def test_satisfies(v, pred, ok):
    assert build.satisfies(v, pred) is ok


# Predicates fabric-loader 0.19.5 refuses to parse (the mod then fails to load at all).
@pytest.mark.parametrize("pred", [">=26.1.x", "<26.x", "^26.x", ">mc1", ">", ">="])
def test_unparseable_predicate_raises(pred):
    with pytest.raises(build.BadVersion):
        build.satisfies("26.1.2", pred)


def test_bad_term_raises_even_when_another_fails():  # Fabric parses the whole predicate before testing
    with pytest.raises(build.BadVersion):
        build.satisfies("26.1.2", "<1 >=26.1.x")


def _module(mid, version="1.0.0", env="*", depends=None, breaks=None, provides=(), jars=()):
    return {"id": mid, "version": version, "provides": list(provides), "environment": env,
            "depends": depends or {}, "breaks": breaks or {}, "jars": list(jars)}


def _mod(slug, side, cs="optional", ss="optional", env="*", depends=None, breaks=None, req=(), mid=None,
         version="1.0.0", provides=(), jars=()):
    m = {"slug": slug, "side": side, "chapter": 1}
    lk = {"project_id": "P" + slug, "client_side": cs, "server_side": ss, "required_projects": list(req)}
    return m, lk, _module(mid or slug, version, env, depends, breaks, provides, jars)


def run(*entries):
    mods = [e[0] for e in entries]
    return build.lint(mods, {e[0]["slug"]: e[1] for e in entries}, {e[0]["slug"]: e[2] for e in entries}, B)


def test_clean_pack_passes():
    assert run(_mod("api", "both"), _mod("a", "both", depends={"api": ">=1", "minecraft": "~26.1"})) == []


def test_server_only_dependency_of_client_mod_is_caught():  # the Lithostitched trap
    errs = run(_mod("lib", "server"), _mod("a", "both", depends={"lib": "*"}))
    assert any("[client] a" in e and "'lib'" in e for e in errs)


def test_client_only_jar_marked_both_is_caught():
    errs = run(_mod("sodium", "both", env="client"))
    assert any("environment=client" in e for e in errs)


def test_client_env_jar_deps_not_checked_on_server():
    assert run(_mod("prickle", "client"), _mod("desc", "client", env="client", depends={"prickle": "*"})) == []


def test_modrinth_side_mismatches():
    assert any("client unsupported" in e for e in run(_mod("s", "both", cs="unsupported")))
    assert any("server unsupported" in e for e in run(_mod("s", "both", ss="unsupported")))
    # "required" on an omitted side is advisory; the per-side depends check decides
    assert run(_mod("lib", "client", ss="required")) == []


def test_version_predicate_and_breaks():
    errs = run(_mod("api", "both", version="6.1.2"), _mod("a", "both", depends={"api": ">=6.1.3"}))
    assert any("needs api" in e for e in errs)
    errs = run(_mod("x", "both"), _mod("y", "both", breaks={"x": "*"}))
    assert any("breaks x" in e for e in errs)


def test_jar_in_jar_provides_count():
    api = _mod("fabric-api", "both", version="0.155.3", jars=[_module("fabric-api-base")])
    assert run(api, _mod("a", "both", depends={"fabric-api-base": "*"})) == []
    aliased = _mod("lib", "both", jars=[_module("yumi_core", "2.0.0", provides=["yumi-core"])])
    assert run(aliased, _mod("a", "both", depends={"yumi-core": "^2.0.0"})) == []


def test_missing_modrinth_required_project():
    assert any("not in the pack" in e for e in run(_mod("a", "both", req=["Pghost"])))


def test_overrides_mods_is_refused(tmp_path):
    (tmp_path / "mods").mkdir()
    m, lk, meta = _mod("a", "both")
    assert any("overrides/mods" in e for e in build.lint([m], {"a": lk}, {"a": meta}, B, tmp_path))


# ── 1. nested modules' own depends/breaks ────────────────────────────────────

def test_nested_module_depends_are_linted():  # illager-invasion > multiloaderdataextensions shape
    parent = _mod("illager-invasion", "both", jars=[_module("mlde", depends={"fabric-api": ">=0.145.1"})])
    errs = run(parent)
    assert sum("illager-invasion > nested mlde" in e and "silently drop" in e for e in errs) == 2  # both sides
    assert run(parent, _mod("fabric-api", "both", version="0.145.1")) == []
    assert any("needs fabric-api" in e for e in run(parent, _mod("fabric-api", "both", version="0.140.0")))


def test_nested_module_breaks_are_linted():
    parent = _mod("artifacts", "both", jars=[_module("ccb", "8.0.0", breaks={"old-lib": "<8.0.0-"})])
    errs = run(parent, _mod("old-lib", "both", version="7.1.0"))
    assert any("artifacts > nested ccb: breaks old-lib" in e for e in errs)
    assert run(parent, _mod("old-lib", "both", version="8.0.0-beta.1")) == []


def test_deeply_nested_module_is_linted():  # trinkets-updated > yumi_mc_core > yumi_commons_*
    deep = _module("yumi_commons_event", "2.0.0", depends={"yumi-commons-collections": "^2.0.0"})
    parent = _mod("trinkets", "both", jars=[_module("yumi_mc_core", jars=[deep])])
    assert any("nested yumi_commons_event" in e and "yumi-commons-collections" in e for e in run(parent))


def test_mod_ids_are_literal_not_globs():  # Fabric 0.19.5 looks ids up by exact string
    breaker = _mod("artifacts", "both", breaks={"cardinal-components-*": "<8.0.0-"})
    assert run(breaker, _mod("ccb", "both", mid="cardinal-components-base", version="7.0.0")) == []
    wants = _mod("a", "both", depends={"cardinal-components-*": "*"})
    errs = run(wants, _mod("ccb", "both", mid="cardinal-components-base"))
    assert any("matches mod ids literally" in e for e in errs)


def test_nested_copy_of_a_root_id_is_not_linted():  # the root wins; the nested copy never loads
    parent = _mod("p", "both", jars=[_module("lib", "2.0.0", depends={"missing": "*"})])
    assert run(parent, _mod("lib", "both")) == []


def test_dependency_satisfied_by_any_candidate_version():  # the solver picks a copy that fits
    a = _mod("a", "both", jars=[_module("lib", "1.0.0")])
    b = _mod("b", "both", jars=[_module("lib", "2.0.0")])
    assert run(a, b, _mod("c", "both", depends={"lib": ">=2"})) == []


# ── 2. nested-module environment ─────────────────────────────────────────────

def _fabric_api():
    return _mod("fabric-api", "both", version="0.145.1",
                jars=[_module("fabric-api-base"), _module("fabric-screen-api-v1", "5.1.0", env="client",
                                                          jars=[_module("screen-internal")])])


def test_client_only_nested_module_is_not_provided_on_server():
    errs = run(_fabric_api(), _mod("a", "both", depends={"fabric-screen-api-v1": ">=6"}))
    assert any("[server] a: depends on 'fabric-screen-api-v1' '>=6', not installed" in e for e in errs)
    assert any("[client] a: needs fabric-screen-api-v1" in e for e in errs)


def test_depends_on_env_disabled_module_at_matching_version_is_downgraded():  # Fabric: becomes a suggestion
    assert run(_fabric_api(), _mod("a", "both", depends={"fabric-screen-api-v1": ">=5"})) == []


def test_child_of_env_disabled_module_is_absent():
    errs = run(_fabric_api(), _mod("a", "both", depends={"screen-internal": "*"}))
    assert any("[server] a: depends on 'screen-internal'" in e for e in errs)
    assert not any("[client]" in e for e in errs)


def test_env_disabled_module_breaks_nothing_and_is_not_linted():
    api = _mod("api", "both", jars=[_module("cl", env="client", breaks={"x": "*"}, depends={"ghost": "*"})])
    errs = run(api, _mod("x", "server"))
    assert not any("[server]" in e for e in errs)  # x is server-only, cl client-only: they never meet
    assert any("[client] api > nested cl: depends on 'ghost'" in e for e in errs)


# ── 6. duplicate root ids ────────────────────────────────────────────────────

def test_two_roots_with_one_id_are_refused():
    errs = run(_mod("a", "both", mid="lib"), _mod("b", "both", mid="lib", version="2.0.0"))
    assert any("a, b: all claim mod id 'lib'" in e for e in errs)


def test_root_provides_colliding_with_root_id_is_refused():
    errs = run(_mod("a", "both", mid="lib"), _mod("b", "both", provides=["lib"]))
    assert any("claim mod id 'lib'" in e for e in errs)


def test_duplicate_ids_on_disjoint_sides_are_fine():
    assert run(_mod("a", "server", mid="lib"), _mod("b", "client", mid="lib")) == []


def test_nested_duplicate_of_root_id_is_fine():
    assert run(_mod("lib", "both"), _mod("p", "both", jars=[_module("lib", "2.0.0")])) == []


def test_module_claiming_builtin_id_is_refused():
    assert any("builtin" in e for e in run(_mod("a", "both", jars=[_module("java", "21")])))


# ── jar parsing ──────────────────────────────────────────────────────────────

def _jar(fmj, nested=()):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        fmj = {"schemaVersion": 1, "version": "1.0.0", **fmj,
               "jars": [{"file": f"META-INF/jars/{i}.jar"} for i in range(len(nested))]}
        z.writestr("fabric.mod.json", json.dumps(fmj))
        for i, data in enumerate(nested):
            z.writestr(f"META-INF/jars/{i}.jar", data)
    return buf.getvalue()


def test_jar_meta_returns_the_module_tree():
    inner = _jar({"id": "deep", "environment": "client", "breaks": {"x": "<2"}})
    mid = _jar({"id": "lib", "version": "2.0.0", "provides": ["lib-alias"], "depends": {"y": "*"}}, [inner])
    root = build.jar_meta(_jar({"id": "root"}, [mid]))
    assert root["id"] == "root" and root["environment"] == "*"
    (lib,) = root["jars"]
    assert (lib["id"], lib["version"], lib["provides"], lib["depends"]) == ("lib", "2.0.0", ["lib-alias"], {"y": "*"})
    (deep,) = lib["jars"]
    assert (deep["environment"], deep["breaks"], deep["jars"]) == ("client", {"x": "<2"}, [])


# ── 4. freeze ────────────────────────────────────────────────────────────────

def _entry(vid):
    return {"version_id": vid, "version_number": vid, "version_type": "release", "date": "2026-09-29"}


def _refresh(mods, lock, update, newest="NEW"):
    calls = []

    def fake_resolve(slug, pin, mc):
        calls.append(slug)
        return _entry(pin or newest)
    build.refresh_lock(lock, mods, "26.1.2", update, resolve=fake_resolve)
    return calls


def test_update_never_moves_a_frozen_mod(capsys):
    mods = [{"slug": "terralith", "freeze": True}, {"slug": "lootr"}]
    lock = {"mods": {"terralith": _entry("OLD"), "lootr": _entry("OLD")}}
    _refresh(mods, lock, update=True)
    assert lock["mods"]["terralith"]["version_id"] == "OLD"
    assert lock["mods"]["lootr"]["version_id"] == "NEW"
    assert "frozen terralith: keeping OLD (--update would pick NEW" in capsys.readouterr().out


def test_frozen_mod_moves_only_by_explicit_pin():
    lock = {"mods": {"terralith": _entry("OLD")}}
    _refresh([{"slug": "terralith", "freeze": True, "pin": "PINNED"}], lock, update=False)
    assert lock["mods"]["terralith"]["version_id"] == "PINNED"


def test_frozen_mod_is_still_locked_when_new():
    lock = {"mods": {}}
    _refresh([{"slug": "terralith", "freeze": True}], lock, update=False)
    assert lock["mods"]["terralith"]["version_id"] == "NEW"


def test_no_update_touches_nothing():
    lock = {"mods": {"lootr": _entry("OLD")}}
    assert _refresh([{"slug": "lootr"}], lock, update=False) == []


def test_worldgen_mods_are_frozen_in_the_manifest():
    _, mods = build.load_manifest((build.HERE / "mods.toml").read_text())
    frozen = {m["slug"] for m in mods if m.get("freeze")}
    for slug in ("tectonic", "terralith", "lithostitched", "incendium", "nullscape", "yungs-api",
                 "dungeons-and-taverns", "dungeons-and-taverns-ancient-city-overhaul", "bigger-better-end-cities",
                 "sparsestructures", "structory", "structory-towers"):
        assert slug in frozen
    assert {m["slug"] for m in mods if m["slug"].startswith("yungs-better-")} <= frozen
    assert "lootr" not in frozen and "fabric-api" not in frozen


def test_freeze_must_be_a_bool():
    with pytest.raises(ValueError):
        build.load_manifest('[pack]\n[[mod]]\nslug = "a"\nside = "both"\nchapter = 1\nfreeze = "yes"\n')


# ── 5. dist overwrite ────────────────────────────────────────────────────────

ENTRIES = {"overrides/config/a.toml": b"x = 1\n", "modrinth.index.json": b'{"versionId": "1.0.0-ch1"}'}


def test_mrpack_is_deterministic(tmp_path):
    a, b = tmp_path / "a.mrpack", tmp_path / "b.mrpack"
    assert build.write_mrpack(a, ENTRIES) == "written"
    assert build.write_mrpack(b, dict(reversed(ENTRIES.items()))) == "written"
    assert a.read_bytes() == b.read_bytes()
    with zipfile.ZipFile(a) as z:
        assert z.namelist() == sorted(ENTRIES)
        assert {i.date_time for i in z.infolist()} == {(1980, 1, 1, 0, 0, 0)}


def test_identical_rebuild_keeps_the_file_and_its_mtime(tmp_path):
    out = tmp_path / "p.mrpack"
    build.write_mrpack(out, ENTRIES)
    os.utime(out, (1_000_000_000, 1_000_000_000))
    assert build.write_mrpack(out, dict(ENTRIES)) == "unchanged"
    assert out.stat().st_mtime == 1_000_000_000


@pytest.mark.parametrize("changed", [
    {**ENTRIES, "modrinth.index.json": b'{"versionId": "1.0.0-ch1", "files": []}'},
    {**ENTRIES, "overrides/config/a.toml": b"x = 2\n"},
    {**ENTRIES, "overrides/config/b.toml": b""},
])
def test_changed_content_under_same_version_is_refused(tmp_path, changed):
    out = tmp_path / "p.mrpack"
    build.write_mrpack(out, ENTRIES)
    before = out.read_bytes()
    with pytest.raises(SystemExit, match="bump version"):
        build.write_mrpack(out, changed)
    assert out.read_bytes() == before


def test_manifest_loads_and_env_mapping():
    pack, mods = build.load_manifest((build.HERE / "mods.toml").read_text())
    assert pack["minecraft"] == "26.1.2" and len(mods) == 46
    assert sum(m["chapter"] == 1 for m in mods) == 38
    assert build.ENV["server"] == {"client": "unsupported", "server": "required"}


# ── client-overrides/servers.dat: the server pre-listed in Multiplayer ──────

def _nbt(data):
    """Strict reader for the NBT subset servers.dat uses (26.1.2 NbtIo.read(Path)): -> root compound as a dict."""
    pos = 0

    def take(n):
        nonlocal pos
        assert pos + n <= len(data), "truncated NBT"
        out, pos = data[pos:pos + n], pos + n
        return out

    def string():
        return take(int.from_bytes(take(2), "big")).decode("utf-8")

    def payload(tag):
        if tag == 1:
            return int.from_bytes(take(1), "big", signed=True)
        if tag == 8:
            return string()
        if tag == 9:
            elem, n = take(1)[0], int.from_bytes(take(4), "big", signed=True)
            assert n >= 0 and (elem != 0 or n == 0), "bad list header"
            return [payload(elem) for _ in range(n)]
        if tag == 10:
            out = {}
            while (t := take(1)[0]) != 0:
                key = string()
                assert key not in out, f"duplicate key {key!r}"
                out[key] = payload(t)
            return out
        raise AssertionError(f"tag type {tag} is not used by servers.dat")

    assert take(1) == b"\x0a", "root must be a compound"
    assert string() == "", "root compound name must be empty"
    root = payload(10)
    assert pos == len(data), "trailing bytes"
    return root


def test_servers_dat_lists_the_server():
    pack, _ = build.load_manifest((build.HERE / "mods.toml").read_text())
    entries = build.mrpack_entries(pack, [], {}, "x", build.HERE / "overrides", ADDR)
    root = _nbt(entries["client-overrides/servers.dat"])
    assert root == {"servers": [{"name": "Vanilla Extended", "ip": ADDR, "hidden": 0}]}
    assert build.lint_entries(entries) == []


def test_servers_dat_bytes_are_stable():  # the no-rewrite rule: same version -> same bytes
    a = build.servers_dat("Vanilla Extended", ADDR)
    assert a == build.servers_dat("Vanilla Extended", ADDR)
    assert a[:3] == b"\x0a\x00\x00" and a.endswith(b"\x00\x00")


@pytest.mark.parametrize("bad", ["a\0b", "\U0001F600"])
def test_servers_dat_refuses_strings_java_encodes_differently(bad):
    with pytest.raises(ValueError):
        build.servers_dat(bad, ADDR)


# The length prefix is an unsigned short of encoded BYTES: 0xFFFF fits, one more byte must refuse,
# and a 2-byte character counts twice (0x8000 x "é" is 0x8000 chars but 0x10000 bytes).
@pytest.mark.parametrize("s, ok", [("x" * 0xFFFF, True), ("x" * 0x10000, False), ("é" * 0x8000, False)])
def test_servers_dat_string_length_limit_counts_utf8_bytes(s, ok):
    if ok:
        assert build._nbt_str(s)[:2] == b"\xff\xff"
    else:
        with pytest.raises(ValueError, match="too long"):
            build.servers_dat(s, ADDR)


def test_client_overrides_hold_only_servers_dat():
    ok = {"client-overrides/servers.dat": b"", "overrides/config/a.toml": b""}
    assert build.lint_entries(ok) == []
    errs = build.lint_entries({**ok, "client-overrides/options.txt": b""})
    assert len(errs) == 1 and "client-overrides/options.txt" in errs[0]
    assert build.lint_entries({**ok, "client-overrides/mods/sodium.jar": b""})


def test_entries_failing_lint_never_reach_the_writer(monkeypatch):  # mrpack_entries runs lint_entries itself
    pack, _ = build.load_manifest((build.HERE / "mods.toml").read_text())
    monkeypatch.setattr(build, "CLIENT_OVERRIDES", set())
    with pytest.raises(SystemExit, match="lint failed"):
        build.mrpack_entries(pack, [], {}, "x", build.HERE / "overrides", ADDR)


ADDRESS = re.compile(r"[A-Za-z0-9-]+\.tail[0-9a-f]+\.ts\.net")


def address_hits(root=build.ROOT):
    """-> {tailnet address: {tracked files naming it}}."""
    files = subprocess.run(["git", "ls-files", "-z"], cwd=root, capture_output=True, check=True)
    hits = {}
    for rel in files.stdout.decode().split("\0"):
        path = root / rel
        if rel and path.is_file():  # every tracked file, any type
            for addr in ADDRESS.findall(path.read_text(errors="replace")):
                hits.setdefault(addr, set()).add(rel)
    return hits


def outside_the_instance_paths(hits, paths):
    """-> {address: [files a copy keeps that name it]}: hits less the files under a kit/INSTANCE-PATHS line."""
    kept = {a: sorted(f for f in files if not any(f == p or (p.endswith("/") and f.startswith(p)) for p in paths))
            for a, files in hits.items()}
    return {a: f for a, f in kept.items() if f}


def test_server_address_has_one_home():
    """kit.toml tailscale.server_address is the address's one home: a real tailnet address appears only in an
    instance path (asus-node's record), never in a file a copy keeps. The tests' own address is not a tailnet one."""
    assert not ADDRESS.search(ADDR)
    hits = address_hits()
    assert outside_the_instance_paths(hits, instance.entries()) == {}
    if RECORD:  # and the only one is asus-node's own
        assert set(hits) <= {build.server_address(FIXTURE)}, hits
    else:
        assert hits == {}
    assert ADDR not in (build.HERE / "build.py").read_text()  # build.py reads kit.toml; it holds no address
    for name in ("kit/setup.md", "setup.md"):
        setup = build.ROOT / "discord" / "content" / name
        if setup.exists() or name == "kit/setup.md":
            # the fallback when servers.dat is missing: sync fills it from kit.toml (discord/test_sync.py pins it)
            assert "Add Server: `{config:server_address}`" in setup.read_text()


def test_the_one_home_scan_finds_an_address_outside_the_instance_paths(tmp_path):
    """The scan, end to end on a planted repository: a real-shaped MagicDNS name (split here, so this file never
    matches it) in a kept file is reported, the same name under a file line or a directory line is not, and a
    directory line covers only what is under it."""
    address = "box.tail" + "0a1b2c.ts.net"
    assert ADDRESS.findall(f"join {address} now") == [address]
    files = {"rec/asus.toml": address, "posts/a.md": address, "postsX/b.md": address, "README.md": f"at {address}",
             "kit.py": "no address"}
    for rel, text in files.items():
        (tmp_path / rel).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / rel).write_text(text)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, check=True)
    hits = address_hits(tmp_path)
    assert hits == {address: {"rec/asus.toml", "posts/a.md", "postsX/b.md", "README.md"}}
    assert outside_the_instance_paths(hits, ["rec/asus.toml", "posts/"]) == {address: ["README.md", "postsX/b.md"]}
    assert outside_the_instance_paths(hits, ["rec/asus.toml", "posts/", "postsX/", "README.md"]) == {}


def test_compose_runs_and_advertises_the_manifest_version():
    pack, _ = build.load_manifest((build.HERE / "mods.toml").read_text())
    compose = (build.ROOT / "docker-compose.yml").read_text()
    modpack = re.search(r'MODRINTH_MODPACK: *"/pack/vanilla-extended-(\d+\.\d+\.\d+)-ch\d+\.mrpack"', compose)
    motd = re.search(r'MOTD: *"[^"]*\bpack (\d+\.\d+\.\d+)"', compose)
    assert modpack and motd, "compose must name the pack file and put 'pack <version>' in the MOTD"
    assert modpack.group(1) == motd.group(1) == pack["version"]


@pytest.mark.skipif(not RECORD, reason=NO_RECORD)
def test_the_fixture_reproduces_asus_nodes_servers_dat_byte_for_byte():
    """The kit gate: the pack built from kit/fixtures/asus-node.toml lists the server exactly as before the kit
    (kit/fixtures/asus-node/servers.dat was written by the old build.py, with the address a literal)."""
    pack, _ = build.load_manifest((build.HERE / "mods.toml").read_text())
    entries = build.mrpack_entries(pack, [], {}, "x", build.HERE / "overrides", build.server_address(FIXTURE))
    assert entries["client-overrides/servers.dat"] == (FIXTURE.parent / "asus-node" / "servers.dat").read_bytes()


def test_the_address_comes_from_kit_toml_and_a_missing_one_refuses(tmp_path, monkeypatch):
    other = tmp_path / "kit.toml"
    box = "box." + "tail0000.ts.net"  # split, so this file never itself matches the one-home scan
    other.write_text(EXAMPLE.read_text().replace(f'"{ADDR}"', f'"{box}"'))
    assert build.server_address(other) == box
    monkeypatch.setenv("MC_KIT_TOML", str(other))
    assert build.server_address() == box  # the env names the file; the default is <repo>/kit.toml
    with pytest.raises(SystemExit, match="kit.toml"):
        build.server_address(tmp_path / "absent.toml")


# ── a custom pack (docs/kit/CUSTOM-PACK.md): what the kit's machinery needs from any pack ──────────────────────

VE_PACK = {"name": "Vanilla Extended", "version": "1.0.2", "minecraft": "26.1.2", "fabric_loader": "0.19.5"}
CUSTOM = {**VE_PACK, "name": "Sky Isles", "version": "1.2.0"}
CUSTOM_TOML = '[pack]\nname = "Sky Isles"\nversion = "1.2.0"\nminecraft = "26.1.2"\nfabric_loader = "0.19.5"\n'


def _kit_mods(*drop, chunky_side="server"):
    entries = [_mod("fabric-api", "both"), _mod("chunky", chunky_side), _mod("appleskin", "both")]
    entries = [e for e in entries if e[0]["slug"] not in drop]
    return [e[0] for e in entries], {e[0]["slug"]: e[2] for e in entries}


def _custom_overrides(tmp_path, chunky=True):
    over = tmp_path / "overrides"
    over.mkdir(parents=True, exist_ok=True)
    if chunky:
        (over / "config" / "chunky").mkdir(parents=True, exist_ok=True)
        (over / "config" / "chunky" / "config.json").write_bytes(
            (build.HERE / "overrides" / "config" / "chunky" / "config.json").read_bytes())
    return over


def test_a_custom_pack_with_the_kits_set_passes(tmp_path):
    mods, metas = _kit_mods()
    assert build.kit_problems(CUSTOM, mods, metas, _custom_overrides(tmp_path)) == []


def test_vanilla_extended_carries_the_kits_set():  # the reference passes by construction, and keeps passing
    _, mods = build.load_manifest((build.HERE / "mods.toml").read_text())
    server = {m["slug"] for m in mods if m["chapter"] == 1 and m["side"] != "client"}
    assert set(build.KIT_SERVER_MODS) <= server  # slug = mod id for both
    for rel in build.KIT_OVERRIDES:
        assert (build.HERE / "overrides" / rel).is_file()


@pytest.mark.parametrize("drop,side,match", [
    (("chunky",), "server", r"^chunky: not in the pack on the server side; deploy/world\.sh"),
    (("fabric-api",), "server", r"^fabric-api: not in the pack on the server side; ve-commands"),
    ((), "client", r"^chunky: not in the pack on the server side"),  # a client-only Chunky never reaches P9
])
def test_a_custom_pack_missing_a_kit_mod_is_refused(tmp_path, drop, side, match):
    mods, metas = _kit_mods(*drop, chunky_side=side)
    errors = build.kit_problems(CUSTOM, mods, metas, _custom_overrides(tmp_path))
    assert len(errors) == 1 and re.search(match, errors[0]), errors


def test_a_kit_mod_provided_under_another_id_counts(tmp_path):  # ids and provides, as Fabric resolves them
    mods, metas = _kit_mods("chunky")
    m, _, meta = _mod("chunky-fork", "server", provides=["chunky"])
    assert build.kit_problems(CUSTOM, mods + [m], {**metas, "chunky-fork": meta}, _custom_overrides(tmp_path)) == []


def test_a_custom_pack_without_the_kits_chunky_config_is_refused(tmp_path):
    mods, metas = _kit_mods()
    errors = build.kit_problems(CUSTOM, mods, metas, _custom_overrides(tmp_path, chunky=False))
    assert errors == ["overrides/config/chunky/config.json: missing, or not the kit's copy in pack/overrides/ "
                      "(continueOnRestart: a pre-generation ring resumes after a restart (deploy/world.sh))"]
    over = _custom_overrides(tmp_path)
    (over / "config" / "chunky" / "config.json").write_text('{"continueOnRestart": false}')
    assert len(build.kit_problems(CUSTOM, mods, metas, over)) == 1


@pytest.mark.parametrize("key,value", [("minecraft", "26.1.3"), ("fabric_loader", "0.19.6")])
def test_a_custom_pack_on_another_version_is_refused(tmp_path, key, value):
    mods, metas = _kit_mods()
    errors = build.kit_problems({**CUSTOM, key: value}, mods, metas, _custom_overrides(tmp_path))
    assert len(errors) == 1 and errors[0].startswith(f"[pack] {key} = {value!r}, but the kit runs")
    assert "ve-commands" in errors[0]


def test_the_kit_set_names_what_the_code_depends_on():
    """KIT_SERVER_MODS is derived from the code, so it is checked against the code: every non-builtin depends of
    ve-commands is in it, and deploy/world.sh drives Chunky."""
    fmj = json.loads((build.ROOT / "mod" / "ve-commands" / "src" / "main" / "resources" / "fabric.mod.json")
                     .read_text())
    builtin = {"minecraft", "fabricloader", "fabric-loader", "java"}
    assert set(fmj["depends"]) - builtin <= set(build.KIT_SERVER_MODS)
    assert fmj["environment"] == "server"
    assert 'rcon "chunky start"' in (build.ROOT / "deploy" / "world.sh").read_text()


def test_a_custom_pack_has_one_chapter_and_a_nameable_pack(tmp_path):
    (tmp_path / "mods.toml").write_text(CUSTOM_TOML)
    assert build.custom_problems(tmp_path, [{"slug": "a"}, {"slug": "b", "chapter": 1}], 1) == []
    assert build.custom_problems(tmp_path, [{"slug": "a", "chapter": 2}], 1)[0].startswith("a custom pack has one")
    assert build.custom_problems(tmp_path, [{"slug": "a"}], 2)[0].startswith("a custom pack has one")
    (tmp_path / "mods.toml").write_text(CUSTOM_TOML.replace('"Sky Isles"', '"Vanilla Extended"'))
    assert "Vanilla Extended's name" in build.custom_problems(tmp_path, [], 1)[0]


def test_a_custom_pack_names_its_server_list_entry_and_summary_after_itself(tmp_path):
    entries = build.mrpack_entries(CUSTOM, [], {}, "1.2.0-ch1", _custom_overrides(tmp_path), ADDR)
    assert _nbt(entries["client-overrides/servers.dat"]) == {
        "servers": [{"name": "Sky Isles", "ip": ADDR, "hidden": 0}]}
    index = json.loads(entries["modrinth.index.json"])
    assert (index["name"], index["summary"]) == ("Sky Isles", "Sky Isles — private friend server")
    assert "overrides/config/chunky/config.json" in entries
    ve = build.mrpack_entries(VE_PACK, [], {}, "x", build.HERE / "overrides", ADDR)
    assert json.loads(ve["modrinth.index.json"])["summary"] == "Vanilla Extended — private friend server"


def test_build_refuses_a_bad_custom_pack_before_any_network_work(tmp_path):
    kit = tmp_path / "kit.toml"
    kit.write_text(EXAMPLE.read_text().replace('mode = "vanilla-extended"', 'mode = "custom"'))
    src = tmp_path / "mine"
    src.mkdir()
    (src / "mods.toml").write_text(CUSTOM_TOML.replace("26.1.2", "26.1.3")
                                   + '[[mod]]\nslug = "chunky"\nside = "server"\nchapter = 2\n')
    r = subprocess.run(["python3", str(build.HERE / "build.py"), "--dir", str(src)], capture_output=True, text=True,
                       env={**os.environ, "MC_KIT_TOML": str(kit)})
    assert r.returncode == 1 and "a custom pack has one chapter" in r.stdout
    assert "[pack] minecraft = '26.1.3', but the kit runs '26.1.2'" in r.stdout
    assert "not a pack the kit can build" in r.stderr
    assert not (src / "lock.json").exists()  # refused before resolving anything


def test_which_prints_the_pack_compose_runs():
    r = subprocess.run(["python3", str(build.HERE / "build.py"), "--which"], capture_output=True, text=True)
    override = build.ROOT / "docker-compose.override.yml"
    compose = override if override.exists() else build.ROOT / "docker-compose.yml"
    want = re.search(r'MODRINTH_MODPACK: *"/pack/([^"]+)"', compose.read_text()).group(1)
    assert r.returncode == 0 and r.stdout == want + "\n"


def _fake_modrinth(monkeypatch, ids):
    """build.main against a fake Modrinth: every slug resolves to one release of a jar whose mod id is ids[slug]."""
    jars = {slug: _jar({"id": mid, "environment": "*"}) for slug, mid in ids.items()}

    def get(path):
        slug = path.split("/")[2].split("?")[0]
        if "/version" in path:
            return [{"id": "V" + slug, "project_id": "P" + slug, "version_number": "1.0.0", "version_type": "release",
                     "date_published": "2026-10-16T00:00:00Z", "game_versions": ["26.1.2"], "loaders": ["fabric"],
                     "dependencies": [], "files": [{"primary": True, "filename": f"{slug}.jar", "url": f"u/{slug}",
                                                    "size": 1, "hashes": {"sha1": "s" + slug, "sha512": "x"}}]}]
        return {"id": "P" + slug, "client_side": "optional", "server_side": "optional"}
    monkeypatch.setattr(build, "get", get)
    monkeypatch.setattr(build, "fetch", lambda entry, cache: jars[entry["filename"][:-4]])


def test_main_builds_a_custom_pack_named_after_itself_from_its_own_dir(tmp_path, monkeypatch, capsys):
    """End to end through main(), with Modrinth faked: pack-custom's mods.toml, its own lock.json and overrides, and
    a dist/ file, servers.dat entry and summary named after its [pack]. Then the same pack without Chunky refuses."""
    kit = tmp_path / "kit.toml"
    kit.write_text(EXAMPLE.read_text().replace('mode = "vanilla-extended"', 'mode = "custom"'))
    root = tmp_path / "root"
    src = root / "pack-custom"
    _custom_overrides(src)
    (src / "mods.toml").write_text(CUSTOM_TOML + "".join(f'[[mod]]\nslug = "{s}"\nside = "{side}"\n' for s, side in
                                                          (("fabric-api", "both"), ("chunky", "server"))))
    _fake_modrinth(monkeypatch, {"fabric-api": "fabric-api", "chunky": "chunky"})
    monkeypatch.setattr(build, "ROOT", root)  # dist/ goes under tmp_path; pack/ (the reference) stays the real one
    monkeypatch.setenv("MC_KIT_TOML", str(kit))
    monkeypatch.setattr(sys, "argv", ["build.py", "--dir", str(src)])
    assert build.main() == 0
    out = root / "dist" / "sky-isles-1.2.0-ch1.mrpack"
    assert "written dist/sky-isles-1.2.0-ch1.mrpack: 2 jars" in capsys.readouterr().out
    with zipfile.ZipFile(out) as z:
        index = json.loads(z.read("modrinth.index.json"))
        dat = z.read("client-overrides/servers.dat")
        assert "overrides/config/chunky/config.json" in z.namelist()
    assert (index["name"], index["versionId"]) == ("Sky Isles", "1.2.0-ch1")
    assert _nbt(dat)["servers"][0]["name"] == "Sky Isles"
    assert set(json.loads((src / "lock.json").read_text())["mods"]) == {"fabric-api", "chunky"}
    assert not (root / "pack").exists()  # nothing written beside the tracked pack/
    (src / "mods.toml").write_text(CUSTOM_TOML.replace("1.2.0", "1.2.1") + '[[mod]]\nslug = "fabric-api"\nside = "both"\n')
    with pytest.raises(SystemExit, match="lint failed: 1 error"):
        build.main()
    assert "ERROR chunky: not in the pack on the server side" in capsys.readouterr().out
    assert not (root / "dist" / "sky-isles-1.2.1-ch1.mrpack").exists()
