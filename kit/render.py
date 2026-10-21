#!/usr/bin/env python3
"""Render kit.toml into the files the existing code reads (KIT.md P4).

  python3 kit/render.py            render (as the kit user, never root)
  python3 kit/render.py --check    write nothing; exit 1 naming every output that is missing or stale
  python3 kit/render.py --check --host-only     only host/kit.env and the units (what host/setup.sh runs, as root)
  python3 kit/render.py --check --env-only      only the keys in .env and .env.bridge, named one by one (check.sh)

Outputs, all under the repo (--repo DIR renders another checkout, e.g. a test copy):
  host/kit.env               KEY="value" lines, sourced by host/setup.sh and host/check.sh; setup.sh installs it as
                             /etc/mc-host/kit.env for the root-owned host/bin scripts
  host/rendered/systemd/     every host/systemd/**/*.in with @KEY@ (a kit.env key) filled in. systemd does not
                             expand env in User= or WorkingDirectory=, so the units are rendered, not configured
  .env                       the compose keys, merged by key (COMPOSE_KEYS, PASSTHROUGH_KEYS)
  .env.bridge                DISCORD_GUILD_ID, merged by key. Discord only: with discord.enabled = false there is
                             no bridge, and render neither writes nor checks it
  docker-compose.override.yml   pack.mode = "custom" only: MODRINTH_MODPACK and MOTD from pack-custom/mods.toml
                             (compose merges it over docker-compose.yml). Vanilla Extended writes none: its pack is
                             docker-compose.yml's own line, so asus-node renders exactly what it did before

The two .env files hold secrets, so the merge rewrites only the lines of the keys it owns and keeps every other
byte, comment and line as it was: they are read and written as bytes, decoded as UTF-8 (anything else is refused)
and split on "\n" only, so a CRLF line or a bare CR inside a value survives. It writes atomically (temp file in the same directory, then rename), at mode 600,
and only when something changed. A value is never printed. It refuses a symlinked, foreign-owned or ambiguous file.
Stdlib only (Ubuntu 24.04 ships Python 3.12).
"""
import argparse
import contextlib
import fcntl
import os
import re
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
# kit/ only, never the repo root: the root holds discord/, which would shadow discord.py for every importer.
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import kitconfig  # noqa: E402

TOKEN = re.compile(r"@([A-Z][A-Z0-9_]*)@")
KIT_ENV_LINE = re.compile(r'([A-Z][A-Z0-9_]*)="([^"\\$`\n]*)"')  # = the parsers in host/bin (test_render pins it)
HEADER = "# Rendered from kit.toml by kit/render.py. Do not edit: change kit.toml, then re-run the render.\n"

# Compose keys in .env, named as itzg's own variables. docker-compose.yml interpolates each as "${KEY:-<today's
# value>}", and .env is also the minecraft service's env_file. So a key here reaches the container twice with the
# same value, and `docker compose config` is unchanged (an MC_-prefixed name would add a variable to the container).
COMPOSE_KEYS = ("MEMORY", "TZ", "DIFFICULTY", "MAX_PLAYERS", "VIEW_DISTANCE", "SIMULATION_DISTANCE")
# Read by itzg straight from the env_file; compose never names them. Written only when set in kit.toml, and never
# deleted (render sets, it does not clean up), because adding one changes the container's config.
PASSTHROUGH_KEYS = ("SEED", "OPS", "PAUSE_WHEN_EMPTY_SECONDS")
# Passthrough keys render refuses to leave behind when kit.toml drops them. itzg leaves server.properties as it is
# when the variable is unset, so a removed PAUSE_WHEN_EMPTY_SECONDS=-1 would keep the server from ever pausing,
# with kit.toml saying nothing. The way back to the default is to set it (60), not to delete it.
STICKY_KEYS = {"PAUSE_WHEN_EMPTY_SECONDS": "set server.pause_when_empty_seconds = 60 in kit.toml (vanilla's "
                                           "default) instead of removing it"}


class RenderError(Exception):
    pass


# ---- pure ---------------------------------------------------------------------------------------------------

def kit_env_values(cfg):
    """-> {KEY: value} for host/kit.env. Every key is also a template token (@KEY@)."""
    h, n, t = cfg["host"], cfg["net"], cfg["tailscale"]
    return {
        "MC_HOST_NAME": h["name"],
        "MC_USER": h["user"],
        "MC_HOME": h["home"],
        "MC_REPO": h["repo"],
        "MC_UID": str(h["uid"]),
        "MC_GID": str(h["gid"]),
        "MC_TZ": h["tz"],
        "MC_NET_MODE": n["mode"],
        # Empty when kit.toml leaves the key out (dhcp: no static keys; a Wi-Fi-only or wired-only laptop: no
        # wired_conn or no wifi_conn). host/setup.sh and check.sh read an empty value as "not configured".
        "MC_WIRED_CONN": n.get("wired_conn", ""),
        "MC_WIFI_CONN": n.get("wifi_conn", ""),
        "MC_WIRED_ADDR": n.get("static_addr", ""),
        "MC_WIRED_GW": n.get("gateway", ""),
        "MC_WIRED_DNS": " ".join(n.get("dns", [])),
        "MC_MAINT_START": cfg["schedule"]["maint_start"],
        "MC_TS_HOST_NAME": t["host_name"],
        "MC_SERVER_ADDRESS": t["server_address"],
        # The battery charge cap host/bin/mc-charge-cap writes at every boot; "none" = no cap (no [battery] section).
        "MC_CHARGE_CAP": str(charge_cap(cfg)),
        # No Discord (discord.enabled = false) only. Written only then, so a Discord instance's kit.env is the same
        # bytes as before the choice existed (asus-node's golden); setup.sh, check.sh, mc-bridge-up and mc-alert read
        # an absent key as Discord, the stricter mode (the webhook and the bridge are then checked).
        **({} if kitconfig.discord_enabled(cfg) else {"MC_DISCORD": "off"}),
    }


# Every key kit_env_values() can write, in its order: setup.sh's and check.sh's KIT_KEYS allowlist is this list
# (host/test_host.py is its alarm). MC_DISCORD is the one key written only for some configs.
KIT_ENV_KEYS = ("MC_HOST_NAME", "MC_USER", "MC_HOME", "MC_REPO", "MC_UID", "MC_GID", "MC_TZ", "MC_NET_MODE",
                "MC_WIRED_CONN", "MC_WIFI_CONN", "MC_WIRED_ADDR", "MC_WIRED_GW", "MC_WIRED_DNS", "MC_MAINT_START",
                "MC_TS_HOST_NAME", "MC_SERVER_ADDRESS", "MC_CHARGE_CAP", "MC_DISCORD")


def charge_cap(cfg):
    """-> the cap (int) or "none"."""
    return cfg.get("battery", {}).get("charge_cap", "none")


# Templates rendered only when kit.toml asks for what they do: {output path: predicate(cfg)}. A template whose
# predicate is false is not rendered, and a stale render of it is removed, so a host without the feature (asus-node:
# no [battery]) gets exactly the units it had before (kit/test_render.py's unit gate).
CONDITIONAL = {
    "mc-charge-cap.service": lambda cfg: charge_cap(cfg) != "none",
}


OVERRIDE_HEADER = ('# Rendered by kit/render.py from kit.toml (pack.mode = "custom") and pack-custom/mods.toml [pack].\n'
                   "# Do not edit: change pack-custom/mods.toml, then re-run the render (docs/kit/CUSTOM-PACK.md).\n")


def override_text(cfg, repo):
    """-> docker-compose.override.yml for a custom pack, or None (Vanilla Extended: compose's own lines). The file
    name follows pack/build.py's rule (kitconfig.mrpack_name); a custom pack has one chapter."""
    if cfg["pack"]["mode"] != "custom":
        return None
    pack = kitconfig.pack_section(kitconfig.pack_dir(cfg, repo))
    file = kitconfig.mrpack_name(pack["name"], pack["version"], 1)
    return (OVERRIDE_HEADER + "services:\n  minecraft:\n    environment:\n"
            f'      MODRINTH_MODPACK: "/pack/{file}"\n'
            f'      MOTD: "{pack["name"]} · pack {pack["version"]}"\n')


def kit_env_text(values):
    lines = []
    for k, v in values.items():
        line = f'{k}="{v}"'
        if not KIT_ENV_LINE.fullmatch(line):  # config.validate refuses these; this is the last line of defence
            raise RenderError(f"{k}: value cannot be written to kit.env safely")
        lines.append(line + "\n")
    return HEADER + "".join(lines)


def parse_kit_env(text):
    """host/kit.env -> {KEY: value}. Comments and blank lines are skipped; any other shape is refused."""
    out = {}
    for n, line in enumerate(text.splitlines(), 1):
        if not line or line.startswith("#"):
            continue
        m = KIT_ENV_LINE.fullmatch(line)
        if not m:
            raise RenderError(f"kit.env line {n}: not KEY=\"value\"")
        out[m.group(1)] = m.group(2)
    return out


def render_template(text, values, name="template"):
    def sub(m):
        if m.group(1) not in values:
            raise RenderError(f"{name}: unknown token @{m.group(1)}@ (kit.env has no such key)")
        return values[m.group(1)]
    out = TOKEN.sub(sub, text)
    left = TOKEN.search(out)
    if left:  # a value that itself spelled a token; config refuses '@', so this is defence in depth
        raise RenderError(f"{name}: @{left.group(1)}@ left after rendering")
    return out


def compose_env_values(cfg):
    s, h = cfg["server"], cfg["host"]
    vals = {"MEMORY": s["memory"], "TZ": h["tz"], "DIFFICULTY": s["difficulty"],
            "MAX_PLAYERS": str(s["max_players"]), "VIEW_DISTANCE": str(s["view_distance"]),
            "SIMULATION_DISTANCE": str(s["simulation_distance"])}
    assert tuple(vals) == COMPOSE_KEYS
    if "seed" in s:
        vals["SEED"] = str(s["seed"])
    ops = s.get("owner_uuid") or s.get("owner_name")
    if ops:
        vals["OPS"] = ops
    if "pause_when_empty_seconds" in s:
        vals["PAUSE_WHEN_EMPTY_SECONDS"] = str(s["pause_when_empty_seconds"])
    return vals


def bridge_env_values(cfg):
    return {"DISCORD_GUILD_ID": cfg["discord"]["guild_id"]}


_STRICT = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)=")
_LOOSE = re.compile(r"[ \t]*(?:export[ \t]+)?([A-Za-z_][A-Za-z0-9_]*)[ \t]*=")


def env_lines(text):
    """Split on "\n" only, keeping it. Never str.splitlines: it also splits on \r, \x0b, \x0c, \x1c-\x1e, \x85 and
    \u2028, which can sit inside a secret value (and compose's raw env_file read would then see two lines)."""
    lines = [ln + "\n" for ln in text.split("\n")]
    lines[-1] = lines[-1][:-1]
    return lines if lines[-1] else lines[:-1]


def env_value(line, key):
    """The value of a `KEY=VALUE` line render owns, as merge_env sees it (without the line ending)."""
    return line.rstrip("\r\n")[len(key) + 1:]


def env_drift(text, updates):
    """-> [(key, 'missing' | 'differs')] for the keys of `updates` whose line merge_env would add or rewrite. Key
    names only: a value is never returned."""
    found = {}
    for line in env_lines(text):
        strict = _STRICT.match(line)
        if strict and strict.group(1) in updates:
            found.setdefault(strict.group(1), env_value(line, strict.group(1)))
    return [(k, "missing" if k not in found else "differs") for k, v in updates.items() if found.get(k) != v]


def merge_env(text, updates, name=".env"):
    """Set each key of `updates` in a KEY=VALUE file's text. Every other line is kept byte for byte.

    A key's existing line is rewritten in place, keeping its line ending. Keys not present are appended, after a
    newline if the file did not end in one (an append onto a last line with no newline would glue two keys). A key
    that appears twice, or in a form this would not rewrite (`export KEY=`, `KEY = v`), is refused: the file would
    then mean two things."""
    out, seen = [], set()
    for n, line in enumerate(env_lines(text), 1):
        loose = _LOOSE.match(line)
        key = loose.group(1) if loose else None
        if key not in updates:
            out.append(line)
            continue
        strict = _STRICT.match(line)
        if not strict or strict.group(1) != key:
            raise RenderError(f"{name} line {n}: {key} is set in a form render will not rewrite "
                              f"(want {key}=VALUE at the start of the line); fix that line by hand")
        if key in seen:
            raise RenderError(f"{name} line {n}: {key} is set twice; delete one line by hand")
        seen.add(key)
        body = line.rstrip("\r\n")
        out.append(f"{key}={updates[key]}{line[len(body):]}")  # the line's own ending, CRLF included
    missing = [k for k in updates if k not in seen]
    if missing:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"
        out += [f"{k}={updates[k]}\n" for k in missing]
    return "".join(out)


def env_keys(text):
    """The keys a KEY=VALUE file sets (the loose form merge_env recognises), on merge_env's own lines (env_lines)."""
    return {m.group(1) for m in (_LOOSE.match(ln) for ln in env_lines(text)) if m}


def check_env_value(key, value):
    # .env is read twice: by compose's dotenv parser (interpolation: quotes, $ and # mean something) and as a raw
    # env_file. check.sh also holds .env.bridge to KEY=VALUE with no whitespace. So a value must be plain.
    if not re.fullmatch(r"[A-Za-z0-9_./:+,-]+", value):
        raise RenderError(f"{key}: value is not plain (letters, digits and _./:+,- only)")


# ---- files --------------------------------------------------------------------------------------------------

def templates(repo, cfg=None):
    """-> {output path relative to host/rendered/systemd: template path}. Every file under host/systemd is a .in.
    With cfg: a CONDITIONAL template whose predicate is false is left out."""
    src = repo / "host" / "systemd"
    found = {}
    for f in sorted(src.rglob("*")):
        if f.is_dir():
            continue
        rel = f.relative_to(src).as_posix()
        if not rel.endswith(".in"):
            raise RenderError(f"host/systemd/{rel}: not a .in template (every unit is rendered; nothing is "
                              "installed from host/systemd directly)")
        found[rel[:-3]] = f
    if not found:
        raise RenderError("host/systemd has no .in templates")
    unknown = set(CONDITIONAL) - set(found)
    if unknown:  # a predicate for a template that no longer exists would silently guard nothing
        raise RenderError(f"CONDITIONAL names no template: {', '.join(sorted(unknown))}")
    if cfg is not None:
        found = {rel: f for rel, f in found.items() if rel not in CONDITIONAL or CONDITIONAL[rel](cfg)}
    return found


def planned(repo, cfg):
    """-> {path: (text, mode)} for every rendered output except the merged env files, plus the merged ones."""
    values = kit_env_values(cfg)
    out = {repo / "host" / "kit.env": (kit_env_text(values), 0o644)}
    for rel, tpl in templates(repo, cfg).items():
        out[repo / "host" / "rendered" / "systemd" / rel] = (render_template(tpl.read_text(), values, tpl.name), 0o644)
    override = override_text(cfg, repo)
    if override is not None:
        p = repo / kitconfig.OVERRIDE_NAME
        if p.is_symlink() or (p.exists() and not p.read_text().startswith(OVERRIDE_HEADER)):
            raise RenderError(f"{p.name} exists and was not written by kit/render.py; it changes what compose runs, "
                              "so render will not replace it (move it aside)")
        out[p] = (override, 0o644)
    return out


def stale_override(repo, cfg):
    """The docker-compose.override.yml render wrote for a custom pack, while pack.mode is no longer custom: compose
    would still run that pack. -> its path, or None. One that render did not write is never touched."""
    p = repo / kitconfig.OVERRIDE_NAME
    if cfg["pack"]["mode"] == "custom" or p.is_symlink() or not p.is_file():
        return None
    return p if p.read_text().startswith(OVERRIDE_HEADER) else None


def world_exists(repo):
    return any((repo / "mc_data").glob("*/level.dat"))


def _read_secret_file(p, writing=True):
    """The current text of .env / .env.bridge ('' if absent). Refuses a link, a non-file, and (when about to write
    it) another user's file."""
    if p.is_symlink():
        raise RenderError(f"{p.name} is a symlink; refusing to replace it (move it aside or make it a plain file)")
    if not p.exists():
        return ""
    if not p.is_file():
        raise RenderError(f"{p.name} is not a regular file")
    if writing and p.stat().st_uid != os.geteuid():
        raise RenderError(f"{p.name} is owned by uid {p.stat().st_uid}, not you ({os.geteuid()}); "
                          "render would change its owner, so it refuses")
    try:
        return p.read_bytes().decode("utf-8")  # no newline translation: every byte comes back as it was
    except UnicodeDecodeError:
        raise RenderError(f"{p.name} is not UTF-8 text; render will not rewrite it (find the stray byte by hand; "
                          "its content is not shown)") from None


def merged(repo, cfg, writing=True):
    """-> {path: (old text, new text)} for .env and .env.bridge."""
    out = {}
    for name, values in env_updates(cfg).items():
        for k, v in values.items():
            check_env_value(k, v)
        p = repo / name
        old = _read_secret_file(p, writing)
        for key, how in STICKY_KEYS.items():
            if name == ".env" and key not in values and key in env_keys(old):
                raise RenderError(f".env sets {key} but kit.toml does not: {how}")
        out[p] = (old, merge_env(old, values, name))
    return out


def temp_path(path):
    """write_atomic's temp file (.gitignore: .*.render-*.tmp)."""
    return path.with_name(f".{path.name}.render-{os.getpid()}.tmp")


def _stale_temp(tmp):
    return RenderError(f"{tmp.name} exists next to {tmp.parent.name}/: a temp file left by a render that died "
                       "(it may hold secrets); delete it, then re-run")


LOCK_WAIT_SECS = 10


@contextlib.contextmanager
def env_lock(*paths):
    """Hold each env file's lock (<name>.lock beside it, gitignored by .env.*) across a read, merge and replace.
    The atomic rename stops a half-written file, not a lost update: render.py, secret.py and tailscale.py each
    read a file, change their own keys and write the whole file back, so two at once (the person pasting a token
    in the second terminal while Claude renders) could put an old token back (codex review 2026-10-16). Locks are
    taken in path order, so two writers can never deadlock; one that waits longer than LOCK_WAIT_SECS refuses."""
    fds = []
    try:
        for p in sorted({Path(p) for p in paths}):
            lock = p.with_name(f"{p.name}.lock")
            try:
                fd = os.open(lock, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
            except OSError as e:
                raise RenderError(f"can't open the lock {lock.name} next to {p.name}: {e.strerror}") from None
            fds.append(fd)
            deadline = time.monotonic() + LOCK_WAIT_SECS
            while True:
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RenderError(f"{p.name} is being written by another kit command (render.py, secret.py "
                                          "or tailscale.py); let it finish, then run this again") from None
                    time.sleep(0.1)
                except OSError as e:  # after BlockingIOError, its subclass: a filesystem that can't lock (ENOLCK)
                    raise RenderError(f"can't lock {lock.name} next to {p.name}: {e.strerror}") from None
        yield
    finally:
        for fd in fds:
            os.close(fd)  # closing the descriptor releases its lock


def write_atomic(path, text, mode):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = temp_path(path)
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)  # created at `mode` (umask can only narrow)
    except FileExistsError:
        raise _stale_temp(tmp) from None  # not ours: never unlinked here
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)  # O_CREAT's mode is masked by the umask
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    dfd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dfd)
    finally:
        os.close(dfd)


def _status(path, text, mode):
    if not path.exists():
        return "missing"
    if path.is_symlink() or not path.is_file():
        return "not a regular file"
    if path.read_bytes() != text.encode("utf-8"):
        return "stale"
    if path.stat().st_mode & 0o777 != mode:
        return f"mode {oct(path.stat().st_mode & 0o777)[2:]}, want {oct(mode)[2:]}"
    return None


def stale_rendered(repo, plan):
    rendered = repo / "host" / "rendered" / "systemd"
    if not rendered.exists():
        return []
    return [p for p in sorted(rendered.rglob("*")) if p.is_file() and p not in plan]


ENV_FILES = (".env", ".env.bridge")


def env_updates(cfg):
    """-> {env file: its keys}. With no Discord there is no bridge, so .env.bridge is neither written nor checked."""
    out = {".env": compose_env_values(cfg)}
    if kitconfig.discord_enabled(cfg):
        out[".env.bridge"] = bridge_env_values(cfg)
    return out


def check(repo, cfg, host_only=False, env_only=False):
    """-> a list of problems (key and file names only, never a value). Writes nothing. host_only: kit.env and the
    units (setup.sh's inputs). env_only: the keys in .env and .env.bridge (check.sh's second line).

    A compose key in .env that differs from kit.toml is live now, not after the next render: docker-compose.yml
    reads it through ${KEY:-default}. So each one is named."""
    problems = []
    if not env_only:
        plan = planned(repo, cfg)
        for p, (text, mode) in plan.items():
            st = _status(p, text, mode)
            if st:
                problems.append(f"{p.relative_to(repo)}: {st}")
        problems += [f"{p.relative_to(repo)}: not rendered from any template (stale)"
                     for p in stale_rendered(repo, plan)]
        if stale_override(repo, cfg):
            problems.append(f"{kitconfig.OVERRIDE_NAME}: rendered for a custom pack, but pack.mode is "
                            f"{cfg['pack']['mode']}")
    updates = env_updates(cfg)
    for p, (old, new) in ({} if host_only else merged(repo, cfg, writing=False)).items():
        if not p.exists():
            problems.append(f"{p.name}: missing")
        elif old != new:
            live = " (compose already uses it)" if p.name == ".env" else ""
            drift = env_drift(old, updates[p.name])
            problems += [f"{p.name}: {k} missing" if st == "missing" else f"{p.name}: {k} differs from kit.toml{live}"
                         for k, st in drift]
            if not drift:  # merge_env changes nothing but owned lines; this is a guard, not a known case
                problems.append(f"{p.name}: differs from what render would write (values not shown)")
        elif p.stat().st_mode & 0o777 != 0o600:
            problems.append(f"{p.name}: mode {oct(p.stat().st_mode & 0o777)[2:]}, want 600")
    return problems


def render(repo, cfg):
    """Write every output that differs. -> [(relative path, 'written' | 'unchanged' | ...)]. The env files stay
    locked from their read to their write (env_lock)."""
    with env_lock(*(repo / name for name in ENV_FILES)):
        return _render(repo, cfg)


def _render(repo, cfg):
    report = []
    plan = planned(repo, cfg)
    env_plan = merged(repo, cfg)  # every refusal happens before the first write
    # A world runs on the pack it was made with (CLAUDE.md), so a pack.mode switch under an existing world is
    # refused. A custom pack's version bump (the override rewritten, not created) is its owner's chapter.
    override, gone = repo / kitconfig.OVERRIDE_NAME, stale_override(repo, cfg)
    if world_exists(repo) and (gone or (override in plan and not override.exists())):
        raise RenderError(f"pack.mode {cfg['pack']['mode']!r} would change the pack under the world in mc_data/, "
                          "and a world stays on its pack (CLAUDE.md). A new pack means a new world: archive this "
                          "one first (AGENTS.md \"Retiring a world\")")
    for p in [*plan, *env_plan]:
        if temp_path(p).exists() or temp_path(p).is_symlink():
            raise _stale_temp(temp_path(p))
    for p, (text, mode) in plan.items():
        if _status(p, text, mode) is None:
            report.append((p.relative_to(repo).as_posix(), "unchanged"))
        else:
            write_atomic(p, text, mode)
            report.append((p.relative_to(repo).as_posix(), "written"))
    for p in stale_rendered(repo, plan):
        p.unlink()
        report.append((p.relative_to(repo).as_posix(), "removed (no template)"))
    if gone:
        gone.unlink()
        report.append((gone.name, "removed (pack.mode is not custom)"))
    for p, (old, new) in env_plan.items():
        if p.exists() and old == new:
            if p.stat().st_mode & 0o777 != 0o600:
                os.chmod(p, 0o600)
                report.append((p.name, "mode set to 600"))
            else:
                report.append((p.name, "unchanged"))
        else:
            write_atomic(p, new, 0o600)
            report.append((p.name, "merged (other lines kept)" if old else "created"))
    return report


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", help="kit.toml path (default: <repo>/kit.toml, or $MC_KIT_TOML)")
    ap.add_argument("--repo", default=str(HERE.parent), help="the checkout to render into (default: this one)")
    ap.add_argument("--check", action="store_true", help="write nothing; exit 1 if any output is missing or stale")
    scope = ap.add_mutually_exclusive_group()
    scope.add_argument("--host-only", action="store_true", help="with --check: only host/kit.env and the units")
    scope.add_argument("--env-only", action="store_true", help="with --check: only the keys in .env and .env.bridge")
    args = ap.parse_args(argv)
    if (args.host_only or args.env_only) and not args.check:
        ap.error("--host-only and --env-only go with --check")
    repo = Path(args.repo).resolve()
    try:
        cfg = kitconfig.load(args.config or os.environ.get(kitconfig.PATH_ENV) or repo / "kit.toml")
        if args.check:
            problems = check(repo, cfg, host_only=args.host_only, env_only=args.env_only)
            for p in problems:
                print(f"STALE {p}")
            print(f"{len(problems)} stale output(s), values not shown; run: python3 kit/render.py" if problems
                  else "rendered outputs match kit.toml")
            return 1 if problems else 0
        if os.geteuid() == 0:
            raise RenderError("refusing to render as root: the outputs (.env, .env.bridge, host/kit.env) must stay "
                              "the kit user's; run it without sudo")
        if cfg["host"]["repo"] != str(repo):
            print(f"note: kit.toml host.repo is {cfg['host']['repo']}, rendering into {repo}")
        for path, status in render(repo, cfg):
            print(f"{status:>26}  {path}")
        return 0
    except (kitconfig.ConfigError, RenderError) as e:
        print(f"render: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
