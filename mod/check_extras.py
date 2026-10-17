#!/usr/bin/env python3
"""Check that every jar compose hands to itzg through MODS is in extras/, is the released build, and is what runs.

  docker compose config --format json | python3 mod/check_extras.py <repo>

Prints one "<PASS|WARN|FAIL>\t<evidence>" line per finding, for host/check.sh, and exits 1 if anything FAILed.
It fails closed: any error, of any kind, is a FAIL line, never silence.

Why each check:
- A MODS file missing from extras/, or one uid 1000 cannot read, is fatal at boot. itzg's mcopy (run as uid 1000)
  errors, and minecraft never starts. That was seen on a spike 2026-10-01 (exit 2 when missing; exit 1 when
  unreadable, per rv2). Under `restart: unless-stopped` the container crash-loops.
- The expected sha256 of each released jar lives in mod/RELEASE.sha256. The build is reproducible, so a rebuild
  from the tagged source matches it.
- compose must bind <repo>/extras at /extras read-only in the effective config (rv3: a dropped or mistyped volume
  left the jar on the host, the gate green, and minecraft crash-looping on "Source file ... does not exist").
- The copy itzg made in mc_data/mods/ is what actually runs. mcopy skips a same-size file, so a same-name rebuild
  can leave the old bytes running. A second ve-commands jar there means two copies of the mod id, and Fabric
  refuses to boot.

Stdlib only.
"""
import hashlib
import json
import os
import stat
import sys
from pathlib import Path

ITZG_UID = ITZG_GID = 1000  # itzg/minecraft-server drops to uid:gid 1000 before mcopy runs


def readable_by_itzg(st):
    m = st.st_mode
    return bool((st.st_uid == ITZG_UID and m & stat.S_IRUSR) or (st.st_gid == ITZG_GID and m & stat.S_IRGRP)
                or m & stat.S_IROTH)


def sha256(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def pins_of(repo):
    pins = {}
    for line in (repo / "mod" / "RELEASE.sha256").read_text().splitlines():
        if line.strip():
            sha, name = line.split(maxsplit=1)
            pins[name.strip()] = sha.lower()
    return pins


def jar_findings(repo, src, pins):
    if not src.startswith("/extras/") or "/" in src[len("/extras/"):] or src == "/extras/":
        return [("FAIL", f"MODS entry {src!r} is not a file directly under /extras/")]
    name = src[len("/extras/"):]
    want = pins.get(name)
    if want is None:
        return [("FAIL", f"{name} has no pin in mod/RELEASE.sha256")]
    out = []
    host = repo / "extras" / name
    try:
        st = os.lstat(host)
        if not stat.S_ISREG(st.st_mode):
            out.append(("FAIL", f"{host} is not a regular file"))
        elif not readable_by_itzg(st):
            out.append(("FAIL", f"{host} is not readable by uid {ITZG_UID} (owner {st.st_uid}, mode "
                                f"{stat.filemode(st.st_mode)}): itzg mcopy fails and minecraft will not start"))
        elif (have := sha256(host)) != want:
            out.append(("FAIL", f"{host} sha256 {have[:16]}… differs from mod/RELEASE.sha256 {want[:16]}…"))
        else:
            out.append(("PASS", f"MODS {src} present in extras/, sha256 {have[:16]}… matches mod/RELEASE.sha256"))
    except FileNotFoundError:
        out.append(("FAIL", f"{host} missing: itzg mcopy exits 2 and minecraft will not start"))
    except OSError as e:
        out.append(("FAIL", f"{host} unreadable here ({e!r}): cannot verify it"))

    mods = repo / "mc_data" / "mods"
    try:
        jars = sorted(n for n in os.listdir(mods) if n.startswith("ve-commands") and n.endswith(".jar"))
    except FileNotFoundError:
        return out + [("WARN", f"{mods} does not exist yet: boot once with `docker compose up -d minecraft`")]
    except OSError as e:
        return out + [("FAIL", f"{mods} unlistable ({e!r}): cannot verify the deployed jar")]
    if len(jars) > 1:
        out.append(("FAIL", f"{mods} holds {len(jars)} ve-commands jars {jars}: Fabric refuses two copies of the mod"))
    if name in jars:
        try:
            have = sha256(mods / name)
        except OSError as e:
            return out + [("FAIL", f"{mods / name} unreadable ({e!r}): cannot verify the deployed jar")]
        if have != want:
            out.append(("FAIL", f"deployed {mods / name} sha256 {have[:16]}… differs from the pin {want[:16]}… "
                                f"(a same-size rebuild that itzg skipped?)"))
        else:
            out.append(("PASS", f"deployed {mods / name} matches the pin"))
    elif jars:
        out.append(("FAIL", f"{mods} runs {jars}, not {name}: apply it with `docker compose up -d minecraft` "
                            f"(`docker compose restart` does not)"))
    else:
        out.append(("WARN", f"{name} not yet in {mods}: apply it with `docker compose up -d minecraft`"))
    return out


def mount_finding(minecraft, repo):
    want = os.path.realpath(repo / "extras")
    vols = minecraft.get("volumes") or []
    for v in vols:
        if (isinstance(v, dict) and v.get("type") == "bind" and v.get("target") == "/extras"
                and os.path.realpath(str(v.get("source", ""))) == want):
            if v.get("read_only") is True:
                return ("PASS", f"compose binds {want} at /extras read-only")
            return ("FAIL", f"compose binds {want} at /extras but not read-only")
    seen = [f"{v.get('source')}:{v.get('target')}" for v in vols if isinstance(v, dict)]
    return ("FAIL", f"compose does not bind {want} at /extras (minecraft volumes: {seen}): itzg cannot see the jar "
                    f"and minecraft will not start")


def findings(compose, repo):
    try:
        minecraft = compose["services"]["minecraft"]
        mods = minecraft.get("environment", {}).get("MODS")
    except (KeyError, TypeError, AttributeError) as e:
        return [("FAIL", f"compose config unreadable: {e!r}")]
    if not mods:
        return [("FAIL", "compose minecraft has no MODS (ve-commands is not deployed)")]
    try:
        pins = pins_of(repo)
    except (OSError, ValueError) as e:
        return [("FAIL", f"mod/RELEASE.sha256 unreadable: {e!r}")]
    if not isinstance(mods, str):
        return [("FAIL", f"compose MODS is not a string: {mods!r}")]
    out = [mount_finding(minecraft, repo)]
    for src in (s.strip() for s in mods.replace("\n", ",").split(",")):
        if src:
            out += jar_findings(repo, src, pins)
    return out if len(out) > 1 else out + [("FAIL", f"MODS {mods!r} names no file")]


def main():
    try:
        repo = Path(sys.argv[1])
        try:
            compose = json.load(sys.stdin)
        except ValueError as e:
            compose = {"error": str(e)}
        result = findings(compose, repo)
    except BaseException as e:  # fail closed: whatever went wrong is a FAIL, never silence
        result = [("FAIL", f"check_extras crashed: {e!r}")]
    for st, msg in result:
        print(f"{st}\t{msg}", flush=True)
    sys.exit(1 if any(st == "FAIL" for st, _ in result) else 0)


if __name__ == "__main__":
    main()
