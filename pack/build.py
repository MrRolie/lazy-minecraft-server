#!/usr/bin/env python3
"""Build the instance's .mrpack from <dir>/mods.toml + <dir>/lock.json + <dir>/overrides/.

  python3 pack/build.py                 # build chapter 1 from the lock (resolves + locks any new mod)
  python3 pack/build.py --chapter 2     # include chapters <= 2
  python3 pack/build.py --update        # re-resolve every unpinned, unfrozen mod to its newest release
  python3 pack/build.py --which         # print the .mrpack compose runs (a name under dist/); builds nothing
  python3 pack/build.py --mode          # print kit.toml's pack.mode (vanilla-extended | custom); builds nothing

<dir> is pack/ (Vanilla Extended), or pack-custom/ (gitignored) when kit.toml says pack.mode = "custom"
(docs/kit/CUSTOM-PACK.md); --dir names another. The output is dist/<[pack] name, slugged>-<version>-ch<N>.mrpack.
A custom pack has one chapter (a change is a [pack] version bump) and its [pack] name is checked (kit/kitconfig.py).

The lock is the reproducibility record: a world is only restorable with the exact pack it was generated with.
File hashes, URLs and sizes come from the Modrinth API (never computed); each download is checked against them.
A frozen mod (worldgen: its update would change generation or generated content) never moves on --update.

Fail-closed lint (any error -> no .mrpack), resolving each side the way Fabric Loader 0.19.5 does (see _lint_side):
  - declared side vs Modrinth "unsupported" sides and vs the jar's fabric.mod.json "environment"
  - every "depends" of every module loaded on a side — root jars AND jar-in-jar modules, each gated by its own
    "environment" and its parent — is satisfied there (id present + version predicate holds); no "breaks" matches
  - no two root jars claim one mod id (Fabric would silently load only one)
  - every Modrinth "required" dependency is in the pack
  - no overrides/mods (itzg extracts overrides unfiltered — a client jar there would crash the server)
  - client-overrides/ holds only servers.dat (see servers_dat)
  - what the kit's own machinery needs is in every pack (KIT_SERVER_MODS, KIT_OVERRIDES, the Minecraft and Fabric
    loader of pack/mods.toml): see kit_problems
The .mrpack is deterministic. An identical rebuild leaves the file untouched (itzg reinstalls on mtime change);
different content under an unchanged [pack] version is refused.
Stdlib only.
"""
import argparse
import hashlib
import io
import json
import re
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
API = "https://api.modrinth.com/v2"
UA = "minecraft-ai-host pack/build.py (a private friend server kit)"
SIDES = ("server", "client", "both")
CLIENT_OVERRIDES = {"client-overrides/servers.dat"}  # the only client-only files the pack may ship
ENV = {"server": {"client": "unsupported", "server": "required"},
       "client": {"client": "required", "server": "unsupported"},
       "both": {"client": "required", "server": "required"}}
# What the kit's own machinery loads or runs on the server, so every pack must carry it (a custom one included).
# ve-commands itself is not in any pack: compose's MODS hands it to itzg from extras/ (mod/README.md).
KIT_SERVER_MODS = {
    "fabric-api": "ve-commands (the Discord bridge, /whitelist from Discord) depends on it "
                  "(mod/ve-commands/src/main/resources/fabric.mod.json)",
    "chunky": "deploy/world.sh pre-generates the world with Chunky (KIT.md P9) and reads its progress",
}
# Overrides the kit relies on, byte for byte as pack/overrides/ has them.
KIT_OVERRIDES = {
    "config/chunky/config.json": "continueOnRestart: a pre-generation ring resumes after a restart (deploy/world.sh)",
}


# ── Fabric version predicates (mirrors fabric-loader 0.19.5 SemanticVersionImpl / VersionPredicateParser) ──

class BadVersion(ValueError):
    pass


X = -1  # wildcard component ('x', 'X', '*'); Fabric stores Integer.MIN_VALUE
_PRE = re.compile(r"|[-0-9A-Za-z]+(\.[-0-9A-Za-z]+)*")
_UINT = re.compile(r"0|[1-9][0-9]*")
OPS = (">=", "<=", ">", "<", "=", "~", "^")  # Fabric tries them in this order


def parse_version(s, wildcards=False):
    """'26.1.2-Fabric-6.1.3+build' -> (components, prerelease tokens | None).

    A trailing hyphen ('8.0.0-') is an EMPTY prerelease, (), which sorts below every other prerelease.
    Raises BadVersion where Fabric falls back to a non-semantic StringVersion."""
    s = s.strip().split("+", 1)[0]
    core, dash, pre = s.partition("-")
    if (dash and not _PRE.fullmatch(pre)) or not core or core.startswith(".") or core.endswith("."):
        raise BadVersion(s)
    comps = []
    for p in core.split("."):
        if wildcards and p in ("x", "X", "*"):
            if dash:
                raise BadVersion(s)  # pre-release versions may not use x-ranges
            comps.append(X)
        elif not re.fullmatch(r"[0-9]+", p) or (comps and comps[-1] == X):
            raise BadVersion(s)  # not a number, or an interjacent wildcard (1.x.2)
        else:
            comps.append(int(p))
    if comps == [X]:
        raise BadVersion(s)
    if X in comps:
        comps = comps[:comps.index(X) + 1]
    return tuple(comps), (tuple(t for t in pre.split(".") if t) if dash else None)


def _component(c, i):
    return c[i] if i < len(c) else (X if c[-1] == X else 0)


def _cmp_pre(a, b):
    for i, x in enumerate(a):
        if i >= len(b):
            return 1
        y = b[i]
        if _UINT.fullmatch(x):
            if not _UINT.fullmatch(y):
                return -1
            if len(x) != len(y):
                return (len(x) > len(y)) - (len(x) < len(y))
        elif _UINT.fullmatch(y):
            return 1
        if x != y:
            return (x > y) - (x < y)
    return 0 if len(a) == len(b) else -1


def compare(a, b):
    """-1/0/1 comparing two parsed versions; a wildcard component compares equal to anything."""
    (ca, pa), (cb, pb) = a, b
    for i in range(max(len(ca), len(cb))):
        x, y = _component(ca, i), _component(cb, i)
        if X not in (x, y) and x != y:
            return -1 if x < y else 1
    if pa is not None and pb is not None:
        return _cmp_pre(pa, pb)
    if pa is None and pb is None:
        return 0
    if pa is not None:
        return 0 if X in cb else -1
    return 0 if X in ca else 1


def _term(version, v, term):
    """One predicate term against version (raw str) / v (parsed, or None for a non-semantic version)."""
    op = next((o for o in OPS if term.startswith(o)), None)
    target = term[len(op):] if op else term
    op = op or "="
    try:
        t = parse_version(target, wildcards=True)
    except BadVersion:
        if op in (">", "<") or not target:
            raise BadVersion(term)  # Fabric refuses exclusive bounds on a non-semantic version
        return version.strip() == target  # StringVersion: exact match
    if X in t[0] and op != "=":
        raise BadVersion(term)  # Fabric: wildcards only with '=' or no operator
    if v is None:  # a StringVersion only ever string-equals another StringVersion
        return False
    if X in t[0]:  # 26.x -> ^26-   26.1.x -> ~26.1-   26.1.2.x -> >=26.1.2- <26.1.3-
        base = (t[0][:-1], ())
        if len(t[0]) == 2:
            op, t = "^", base
        elif len(t[0]) == 3:
            op, t = "~", base
        else:
            upper = (base[0][:-1] + (base[0][-1] + 1,), ())
            return compare(v, base) >= 0 and compare(v, upper) < 0
    c = compare(v, t)
    if op == "~":
        return c >= 0 and all(_component(v[0], i) == _component(t[0], i) for i in (0, 1))
    if op == "^":
        return c >= 0 and _component(v[0], 0) == _component(t[0], 0)
    return {">=": c >= 0, "<=": c <= 0, ">": c > 0, "<": c < 0, "=": c == 0}[op]


def satisfies(version, predicate):
    """Fabric depends value: str (space-separated AND) or list of str (OR). BadVersion if Fabric can't parse it."""
    if isinstance(predicate, list):
        return any([satisfies(version, p) for p in predicate])
    try:
        v = parse_version(version)
    except BadVersion:
        v = None
    terms = [t for t in (x.strip() for x in predicate.split(" ")) if t and t != "*"]
    return all([_term(version, v, t) for t in terms])  # a list, so every term is parsed (Fabric parses first)


# ── Modrinth I/O ─────────────────────────────────────────────────────────────

def get(path):
    for attempt in range(5):
        req = urllib.request.Request(API + path, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            if e.code == 429:
                time.sleep(float(e.headers.get("X-Ratelimit-Reset", 5)))
                continue
            sys.exit(f"GET {path} -> HTTP {e.code}")
    sys.exit(f"GET {path}: rate-limited")


def resolve(slug, pin, mc, loader="fabric"):
    """-> lock entry for one mod."""
    project = get(f"/project/{slug}")
    if pin:
        v = get(f"/version/{pin}")
        if v["project_id"] != project["id"]:
            sys.exit(f"{slug}: pin {pin} belongs to another project")
        if mc not in v["game_versions"] or loader not in v["loaders"]:
            sys.exit(f"{slug}: pin {pin} does not support {loader} {mc}")
    else:
        q = urllib.parse.urlencode({"loaders": json.dumps([loader]), "game_versions": json.dumps([mc])})
        versions = get(f"/project/{slug}/version?{q}")
        if not versions:
            sys.exit(f"{slug}: no {loader} version for {mc}")
        releases = [x for x in versions if x["version_type"] == "release"]
        v = max(releases or versions, key=lambda x: x["date_published"])
    files = [f for f in v["files"] if f["primary"]] or v["files"][:1]
    f = files[0]
    return {"project_id": project["id"], "version_id": v["id"], "version_number": v["version_number"],
            "version_type": v["version_type"], "date": v["date_published"][:10], "filename": f["filename"],
            "url": f["url"], "sha1": f["hashes"]["sha1"], "sha512": f["hashes"]["sha512"], "size": f["size"],
            "client_side": project["client_side"], "server_side": project["server_side"],
            "required_projects": sorted({d["project_id"] for d in v["dependencies"]
                                         if d["dependency_type"] == "required" and d.get("project_id")})}


def fetch(entry, cache):
    path = cache / f"{entry['sha1']}.jar"
    if not path.exists():
        req = urllib.request.Request(entry["url"], headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=300) as r:
            data = r.read()
        if hashlib.sha512(data).hexdigest() != entry["sha512"]:
            sys.exit(f"{entry['filename']}: sha512 mismatch against Modrinth")
        path.write_bytes(data)
    return path.read_bytes()


# ── Jar metadata ─────────────────────────────────────────────────────────────

def jar_meta(data, name="?"):
    """-> root module {id, version, provides, environment, depends, breaks, jars: [nested modules, recursively]}."""
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        try:
            fmj = json.loads(z.read("fabric.mod.json").decode("utf-8-sig"), strict=False)
        except KeyError:
            raise ValueError(f"{name}: no fabric.mod.json (not a Fabric mod jar)")
        jars = [jar_meta(z.read(n["file"]), n["file"]) for n in fmj.get("jars", [])]
    return {"id": fmj["id"], "version": fmj["version"], "provides": fmj.get("provides", []),
            "environment": fmj.get("environment", "*"), "depends": fmj.get("depends", {}),
            "breaks": fmj.get("breaks", {}), "jars": jars}


# ── Pure lint ────────────────────────────────────────────────────────────────
# Per-side resolution, as fabric-loader 0.19.5 does it (verified against its ModDiscoverer/ModResolver):
#  - a module loads iff its "environment" matches the side AND its parent loads; a child of a module that
#    doesn't load is simply absent. A root jar that doesn't load on a side is an error in its own right.
#  - mod ids are matched literally: "cardinal-components-*" names no mod, so it never breaks and never satisfies.
#  - a "depends" on an id that no loaded module has, but that an environment-disabled module has at a matching
#    version, is downgraded to a suggestion (fabric-api's client-only modules on the server).
#  - two root jars claiming one id (own id or "provides") do NOT fail: Fabric loads one and the other silently
#    vanishes. A nested module sharing a root's id is normal (the root wins) and is not linted itself.
#  - a nested module whose depends/breaks fail is silently dropped (not a launch refusal), leaving its parent
#    without a bundled library -> still an error here.

def _walk(module, env, slug, root, loaded, disabled):
    if module["environment"] not in ("*", env):
        disabled.setdefault(module["id"], []).append(module["version"])
        return
    loaded.append((slug, module, root))
    for child in module["jars"]:
        _walk(child, env, slug, False, loaded, disabled)


def _any_satisfies(versions, pred):
    return any([satisfies(v, pred) for v in versions])  # a list, so a bad predicate raises even if one matches


def _lint_side(env, installed, metas, builtins):
    errors, loaded, disabled = [], [], {}
    for m in installed:
        _walk(metas[m["slug"]], env, m["slug"], True, loaded, disabled)
    have, roots = {k: [v] for k, v in builtins.items()}, {}
    for slug, mod, root in loaded:
        for mid in {mod["id"], *mod["provides"]}:
            have.setdefault(mid, []).append(mod["version"])
            if root:
                roots.setdefault(mid, set()).add(slug)
            if mid in builtins:
                errors.append(f"[{env}] {slug}: {mod['id']} claims the id {mid!r} of a builtin (Fabric refuses)")
    for mid, slugs in sorted(roots.items()):
        if len(slugs) > 1:
            errors.append(f"[{env}] {', '.join(sorted(slugs))}: all claim mod id {mid!r} — Fabric loads only one "
                          f"and the others silently disappear")
    for slug, mod, root in loaded:
        if not root and ({mod["id"], *mod["provides"]} & roots.keys()):
            continue  # a root module with the same id wins; this copy is not loaded
        who = slug if root else f"{slug} > nested {mod['id']}"
        dropped = "" if root else " (Fabric would silently drop this bundled module)"
        for dep, pred in mod["depends"].items():
            try:
                if dep in have:
                    if not _any_satisfies(have[dep], pred):
                        errors.append(f"[{env}] {who}: needs {dep} {pred!r}, have {', '.join(have[dep])}{dropped}")
                elif not _any_satisfies(disabled.get(dep, []), pred):
                    literal = " (Fabric matches mod ids literally)" if "*" in dep else ""
                    errors.append(f"[{env}] {who}: depends on {dep!r} {pred!r}, not installed on this side"
                                  f"{literal}{dropped}")
            except BadVersion:
                errors.append(f"[{env}] {who}: cannot evaluate {dep} {pred!r}")
        for dep, pred in mod["breaks"].items():
            try:
                if dep in have and _any_satisfies(have[dep], pred):
                    errors.append(f"[{env}] {who}: breaks {dep} {pred!r} (have {', '.join(have[dep])}){dropped}")
            except BadVersion:
                errors.append(f"[{env}] {who}: cannot evaluate breaks {dep} {pred!r}")
    return errors


def lint(mods, locks, metas, builtins, overrides_dir=None):
    """mods: manifest entries in the build; locks/metas keyed by slug. -> list of error strings."""
    errors = []
    for m in mods:
        s, lk, meta = m["slug"], locks[m["slug"]], metas[m["slug"]]
        side = m["side"]
        if lk["client_side"] == "unsupported" and side != "server":
            errors.append(f"{s}: Modrinth says client unsupported, but side={side}")
        if lk["server_side"] == "unsupported" and side != "client":
            errors.append(f"{s}: Modrinth says server unsupported, but side={side}")
        # Modrinth "required" on the side we omit is generic metadata (libraries are marked required everywhere);
        # whether that side truly needs the jar is decided precisely by the per-side depends check below.
        if meta["environment"] == "client" and side != "client":
            errors.append(f"{s}: jar environment=client, but side={side}")
        if meta["environment"] == "server" and side != "server":
            errors.append(f"{s}: jar environment=server, but side={side}")
    in_pack = {locks[m["slug"]]["project_id"] for m in mods}
    for m in mods:
        for pid in locks[m["slug"]]["required_projects"]:
            if pid not in in_pack:
                errors.append(f"{m['slug']}: Modrinth-required project {pid} is not in the pack")
    for env in ("server", "client"):
        errors += _lint_side(env, [m for m in mods if m["side"] in (env, "both")], metas, builtins)
    if overrides_dir and (overrides_dir / "mods").exists():
        errors.append("overrides/mods exists — itzg extracts overrides unfiltered; ship mods via the index only")
    return errors


def version_problems(pack, ve_dir=HERE):
    """The kit runs pack/mods.toml's Minecraft and Fabric loader, whatever the pack. -> list of error strings."""
    ve = tomllib.loads((ve_dir / "mods.toml").read_text())["pack"]
    return [f"[pack] {key} = {pack[key]!r}, but the kit runs {ve[key]!r} (pack/mods.toml): ve-commands is built and "
            f"pinned for exactly that pair, and the server image for its Java"
            for key in ("minecraft", "fabric_loader") if pack[key] != ve[key]]


def kit_problems(pack, mods, metas, overrides_dir, ve_dir=HERE):
    """What the kit's machinery needs from any pack: KIT_SERVER_MODS, KIT_OVERRIDES, and version_problems. Vanilla
    Extended is the reference, so it passes by construction. -> list of error strings."""
    errors = version_problems(pack, ve_dir)
    server_ids = set()
    for m in mods:
        if m["side"] != "client":
            server_ids |= {metas[m["slug"]]["id"], *metas[m["slug"]]["provides"]}
    for mid, why in KIT_SERVER_MODS.items():
        if mid not in server_ids:
            errors.append(f"{mid}: not in the pack on the server side; {why}")
    for rel, why in KIT_OVERRIDES.items():
        have, want = overrides_dir / rel, ve_dir / "overrides" / rel
        if not have.is_file() or have.read_bytes() != want.read_bytes():
            errors.append(f"overrides/{rel}: missing, or not the kit's copy in pack/overrides/ ({why})")
    return errors


def _nbt_str(s):
    if "\0" in s or any(ord(c) > 0xFFFF for c in s):
        raise ValueError(f"{s!r}: Java writes this differently in modified UTF-8")
    b = s.encode("utf-8")  # otherwise modified UTF-8 (DataOutput.writeUTF) is plain UTF-8
    if len(b) > 0xFFFF:
        raise ValueError(f"{s[:20]!r}...: too long for an NBT string")
    return len(b).to_bytes(2, "big") + b


def servers_dat(name, ip):
    """-> the client's Multiplayer list holding one server, in the form 26.1.2 ServerList.load reads.

    Uncompressed NBT (NbtIo.read(Path)): a root compound with an empty name, holding "servers", a list of
    compounds {name, ip, hidden: 0b}. No "acceptTextures": the server sends no resource pack, and absent means
    PROMPT. The field order is fixed, so every build gives the same bytes (the no-rewrite rule)."""
    entry = (b"\x08" + _nbt_str("name") + _nbt_str(name) + b"\x08" + _nbt_str("ip") + _nbt_str(ip)
             + b"\x01" + _nbt_str("hidden") + b"\x00" + b"\x00")
    servers = b"\x09" + _nbt_str("servers") + b"\x0a" + (1).to_bytes(4, "big") + entry
    return b"\x0a" + _nbt_str("") + servers + b"\x00"


def lint_entries(entries):
    """The finished .mrpack members. -> list of error strings."""
    return [f"{name}: client-overrides/ may hold only {', '.join(sorted(CLIENT_OVERRIDES))}"
            for name in sorted(entries) if name.startswith("client-overrides/") and name not in CLIENT_OVERRIDES]


def index(pack, mods, locks, version_id):
    return {
        "formatVersion": 1, "game": "minecraft", "versionId": version_id, "name": pack["name"],
        "summary": f"{pack['name']} — private friend server",
        "files": [{"path": f"mods/{locks[m['slug']]['filename']}",
                   "hashes": {"sha1": locks[m["slug"]]["sha1"], "sha512": locks[m["slug"]]["sha512"]},
                   "env": ENV[m["side"]], "downloads": [locks[m["slug"]]["url"]],
                   "fileSize": locks[m["slug"]]["size"]} for m in mods],
        "dependencies": {"minecraft": pack["minecraft"], "fabric-loader": pack["fabric_loader"]},
    }


def load_manifest(text):
    data = tomllib.loads(text)
    mods = data["mod"]
    seen = set()
    for m in mods:
        if m["side"] not in SIDES:
            raise ValueError(f"{m['slug']}: side must be one of {SIDES}")
        if not isinstance(m.get("freeze", False), bool):
            raise ValueError(f"{m['slug']}: freeze must be true or false")
        if m["slug"] in seen:
            raise ValueError(f"{m['slug']}: listed twice")
        seen.add(m["slug"])
    return data["pack"], mods


def refresh_lock(lock, all_mods, mc, update, resolve=resolve):
    """Resolve new mods and stale pins; with update, move unpinned, unfrozen mods to their newest release."""
    for m in all_mods:  # lock EVERY chapter, so later chapters are pinned from day one
        cur = lock["mods"].get(m["slug"])
        stale_pin = cur and m.get("pin") and cur["version_id"] != m["pin"]
        if cur is None or stale_pin or (update and not m.get("pin") and not m.get("freeze")):
            new = resolve(m["slug"], m.get("pin"), mc)
            if cur is None or cur["version_id"] != new["version_id"]:
                print(f"  lock {m['slug']}: {cur and cur['version_number']} -> {new['version_number']}"
                      f" ({new['version_type']}, {new['date']})")
            lock["mods"][m["slug"]] = new
        elif update and m.get("freeze") and not m.get("pin"):
            newest = resolve(m["slug"], None, mc)
            if newest["version_id"] != cur["version_id"]:
                print(f"  frozen {m['slug']}: keeping {cur['version_number']}"
                      f" (--update would pick {newest['version_number']}; worldgen mods move only by pin)")
    for slug in sorted(set(lock["mods"]) - {m["slug"] for m in all_mods}):
        print(f"  lock: dropping {slug} (no longer in mods.toml)")
        del lock["mods"][slug]
    return lock


def _kitconfig():
    if str(ROOT / "kit") not in sys.path:
        sys.path.insert(0, str(ROOT / "kit"))  # kit/ only, never the repo root (it holds discord/)
    import kitconfig
    return kitconfig


def kit_config(kit_toml=None):
    kitconfig = _kitconfig()
    try:
        return kitconfig.load(kit_toml)
    except kitconfig.ConfigError as e:
        sys.exit(f"pack/build.py needs kit.toml for the server address and pack.mode: {e}")


def server_address(kit_toml=None):
    """The friends' server address, kit.toml tailscale.server_address: its one home (pack/test_build.py keeps every
    other tailnet address out of the files a copy keeps). Baked into servers.dat, so every instance builds its own."""
    return kit_config(kit_toml)["tailscale"]["server_address"]


def custom_problems(src, mods, chapter):
    """A custom pack (src is not pack/): one chapter, and a [pack] the kit can name things after. -> list of errors."""
    kitconfig = _kitconfig()
    errors = []
    try:
        kitconfig.pack_section(src)
    except kitconfig.ConfigError as e:
        errors.append(str(e))
    if chapter != 1 or any(m.get("chapter", 1) != 1 for m in mods):
        errors.append("a custom pack has one chapter: drop chapter keys and --chapter; a change to it is a [pack] "
                      "version bump (backup, announce)")
    return errors


def mrpack_entries(pack, mods, locks, version_id, overrides_dir, address):
    """-> {zip member: bytes} — the whole content of the .mrpack. Exits if lint_entries refuses it."""
    entries = {"modrinth.index.json": json.dumps(index(pack, mods, locks, version_id), indent=1).encode()}
    for f in sorted(overrides_dir.rglob("*")):
        if f.is_file():
            entries["overrides/" + f.relative_to(overrides_dir).as_posix()] = f.read_bytes()
    # Launchers apply client-overrides/ on top of overrides/; itzg installs only overrides/ and server-overrides/.
    entries["client-overrides/servers.dat"] = servers_dat(pack["name"], address)
    errors = lint_entries(entries)
    if errors:
        print("\n".join(f"  ERROR {e}" for e in errors))
        sys.exit(f"lint failed: {len(errors)} error(s); no .mrpack written")
    return entries


def write_mrpack(out, entries):
    """Deterministic zip. -> "written" | "unchanged"; exits if out already holds DIFFERENT content.

    itzg reinstalls a local .mrpack when its mtime changes and re-copies the overrides each time, so an identical
    pack is never rewritten, and a changed pack under an unchanged version is refused."""
    if out.exists():
        try:
            with zipfile.ZipFile(out) as z:
                old = {n: z.read(n) for n in z.namelist()}
        except zipfile.BadZipFile:
            old = None
        if old == entries:
            return "unchanged"
        sys.exit(f"{out.name}: pack contents changed but [pack] version did not — bump version in mods.toml"
                 f" (or delete the file if it never shipped)")
    tmp = out.with_name(out.name + ".tmp")
    with zipfile.ZipFile(tmp, "w") as z:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type, info.external_attr = zipfile.ZIP_DEFLATED, 0o644 << 16
            z.writestr(info, entries[name])
    tmp.replace(out)
    return "written"


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--chapter", type=int, default=1)
    ap.add_argument("--update", action="store_true", help="re-resolve unpinned, unfrozen mods to their newest release")
    ap.add_argument("--dir", help="the pack directory (default from kit.toml pack.mode: pack/, or pack-custom/)")
    ap.add_argument("--which", action="store_true", help="print the .mrpack compose runs (a name under dist/), "
                                                          "build nothing")
    ap.add_argument("--mode", action="store_true", help="print kit.toml's pack.mode, build nothing "
                                                         "(pack/spike.sh --if-custom)")
    args = ap.parse_args()
    if args.mode:
        print(kit_config()["pack"]["mode"])
        return 0
    kitconfig = _kitconfig()
    if args.which:
        try:
            print(kitconfig.live_pack_file(ROOT))
        except kitconfig.ConfigError as e:
            sys.exit(f"pack/build.py --which: {e}")
        return 0
    cfg = kit_config()  # before any network work: no kit.toml, no pack
    address = cfg["tailscale"]["server_address"]
    src = Path(args.dir).resolve() if args.dir else kitconfig.pack_dir(cfg, ROOT)
    custom = src != HERE

    pack, all_mods = load_manifest((src / "mods.toml").read_text())
    if custom:
        errors = custom_problems(src, all_mods, args.chapter) + version_problems(pack)  # before any network work
        if errors:
            print("\n".join(f"  ERROR {e}" for e in errors))
            sys.exit(f"{src.name}: not a pack the kit can build; no .mrpack written")
        for m in all_mods:
            m.setdefault("chapter", 1)
    lock_path = src / "lock.json"
    lock = json.loads(lock_path.read_text()) if lock_path.exists() else {"mods": {}}
    if lock.get("minecraft", pack["minecraft"]) != pack["minecraft"]:
        sys.exit("lock.json is for a different Minecraft version — a world is locked to its version")
    lock.update({"minecraft": pack["minecraft"], "fabric_loader": pack["fabric_loader"]})
    refresh_lock(lock, all_mods, pack["minecraft"], args.update)
    lock_path.write_text(json.dumps(lock, indent=1, sort_keys=True) + "\n")

    mods = [m for m in all_mods if m["chapter"] <= args.chapter]
    cache = HERE / ".cache"
    cache.mkdir(exist_ok=True)
    metas = {m["slug"]: jar_meta(fetch(lock["mods"][m["slug"]], cache), m["slug"]) for m in mods}
    builtins = {"minecraft": pack["minecraft"], "fabricloader": pack["fabric_loader"],
                "fabric-loader": pack["fabric_loader"], "java": "25"}
    errors = lint(mods, lock["mods"], metas, builtins, src / "overrides")
    errors += kit_problems(pack, mods, metas, src / "overrides")
    for m in mods:
        lk = lock["mods"][m["slug"]]
        if lk["version_type"] != "release":
            print(f"  WARN {m['slug']} {lk['version_number']} is a {lk['version_type']} build")
    if errors:
        print("\n".join(f"  ERROR {e}" for e in errors))
        sys.exit(f"lint failed: {len(errors)} error(s); no .mrpack written")

    version_id = f"{pack['version']}-ch{args.chapter}"
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)
    out = dist / kitconfig.mrpack_name(pack["name"], pack["version"], args.chapter)
    status = write_mrpack(out, mrpack_entries(pack, mods, lock["mods"], version_id, src / "overrides", address))
    n_server = sum(m["side"] != "client" for m in mods)
    n_client = sum(m["side"] != "server" for m in mods)
    print(f"{status} {out.relative_to(ROOT)}: {len(mods)} jars ({n_server} server, {n_client} client)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
