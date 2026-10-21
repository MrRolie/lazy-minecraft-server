"""kit.toml: the instance's one config file (template: kit/kit.example.toml). Schema, loader, validation.

It holds no secrets. `load()` refuses an unknown section or key, a missing required key, a value of the wrong type
and a value the kit cannot honour yet. It never guesses or fills in a default for a required key. Stdlib only, so
pack/build.py and discord/sync.py can import it as well as kit/render.py.

Where a value is used:
- host/kit.env (kit/render.py), sourced by host/setup.sh and host/check.sh and installed to /etc/mc-host/kit.env
  for the root-owned host/bin scripts
- the systemd units, rendered from host/systemd/*.in
- the compose keys in .env, and DISCORD_GUILD_ID in .env.bridge
- pack/build.py (tailscale.server_address) and discord/sync.py (discord.guild_id, discord.bridge_bot_id)
- discord.enabled = false (no Discord: HUMAN-STEPS step 23): MC_DISCORD="off" in host/kit.env, no .env.bridge key
  (kit/render.py), the Discord phases skipped (kit/record.py skip), and sync/bootstrap refuse
- pack.mode: which directory pack/build.py builds, and (custom) docker-compose.override.yml (kit/render.py)
"""
import datetime
import ipaddress
import os
import re
import tomllib
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DEFAULT_PATH = REPO / "kit.toml"
PATH_ENV = "MC_KIT_TOML"  # overrides the path (tests, rehearsal); never a second config home

# A value that kit.env carries inside double quotes and that systemd units carry raw. These characters are the
# ones that would change meaning when sourced by bash or read by systemd, so they are refused, never escaped.
_UNSAFE = re.compile(r'["\\$`\n\r%@]')


class ConfigError(ValueError):
    pass


def _match(rx, what):
    rx = re.compile(rx)

    def check(v):
        if not rx.fullmatch(v):
            raise ConfigError(f"must be {what}")
    return check


def _int_range(lo, hi):
    def check(v):
        if not lo <= v <= hi:
            raise ConfigError(f"must be between {lo} and {hi}")
    return check


def _one_of(*allowed, why=""):
    def check(v):
        if v not in allowed:
            raise ConfigError(f"must be one of {', '.join(map(repr, allowed))}{why}")
    return check


def _abs_path(v):
    if not re.fullmatch(r"/[A-Za-z0-9._/-]+", v) or "/../" in v + "/" or v != "/" and v.endswith("/"):
        raise ConfigError("must be an absolute path of letters, digits and ._/- (no .., no trailing /)")


def _cidr(v):
    try:
        iface = ipaddress.IPv4Interface(v)
    except ValueError:
        raise ConfigError("must be an IPv4 address with a prefix, like 192.168.1.5/24") from None
    if "/" not in v or str(iface) != v:
        raise ConfigError("must be an IPv4 address with a prefix, like 192.168.1.5/24")


def _ipv4(v):
    try:
        if str(ipaddress.IPv4Address(v)) != v:
            raise ValueError
    except ValueError:
        raise ConfigError("must be an IPv4 address") from None


def _ipv4_list(v):
    if not v:
        raise ConfigError("must name at least one IPv4 address")
    for a in v:
        if not isinstance(a, str):
            raise ConfigError("must be a list of IPv4 address strings")
        _ipv4(a)


def _hhmm(v):
    if not re.fullmatch(r"([01][0-9]|2[0-3]):[0-5][0-9]", v):
        raise ConfigError("must be HH:MM (24 h)")


def _uid_1000(v):
    # The mailbox design: itzg (the mod) and the bridge image both write mc_data/mcve as uid 1000 (bridge/Dockerfile,
    # bridge/entrypoint.sh). Any other uid needs a bridge build arg first, so it is refused here.
    if v != 1000:
        raise ConfigError("must be 1000: the bridge image and itzg both run as uid/gid 1000; "
                          "use the first account the installer made")


def _pause(v):
    if not (v == -1 or 1 <= v <= 86400):
        raise ConfigError("must be -1 (never pause: pre-gen rings) or 1-86400 seconds (60 is vanilla's default)")


def _memory(v):
    if not re.fullmatch(r"[1-9][0-9]{0,5}[MG]", v):
        raise ConfigError("must be a JVM heap size like 8G or 6144M")


# Each key: (type, required, check). type is a Python type, or a tuple of types. Unknown keys are refused.
SNOWFLAKE = ("a Discord id (17-20 digits, as a string, no leading 0); a kit derives both with "
             "python3 discord/bootstrap.py discover")
NAME_CHARS = r"[A-Za-z0-9 ._+-]{1,64}"  # NetworkManager connection names; quotes, $ and the like are refused
SCHEMA = {
    "host": {
        "name": (str, True, _match(r"[A-Za-z0-9]([A-Za-z0-9-]{0,61}[A-Za-z0-9])?", "a hostname (letters, digits, -)")),
        "user": (str, True, _match(r"[a-z_][a-z0-9_-]{0,31}", "a unix user name")),
        "home": (str, True, _abs_path),
        "repo": (str, True, _abs_path),
        "uid": (int, True, _uid_1000),
        "gid": (int, True, _uid_1000),
        "tz": (str, True, _match(r"[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*", "an IANA zone like America/Toronto")),
        # probe facts (P0); informational, nothing renders them yet
        "ram_mib": (int, False, _int_range(1, 10 ** 8)),
        "cores": (int, False, _int_range(1, 4096)),
        "disk_free_gib": (int, False, _int_range(0, 10 ** 7)),
    },
    "server": {
        "memory": (str, True, _memory),
        "view_distance": (int, True, _int_range(2, 32)),
        "simulation_distance": (int, True, _int_range(2, 32)),
        "difficulty": (str, True, _one_of("peaceful", "easy", "normal", "hard")),
        "max_players": (int, True, _int_range(1, 1000)),
        "seed": (int, False, _int_range(-2 ** 63, 2 ** 63 - 1)),
        "owner_name": (str, False, _match(r"[A-Za-z0-9_]{3,16}", "a Minecraft name (3-16 letters, digits, _)")),
        "owner_uuid": (str, False, _match(r"[0-9a-f]{8}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{4}-?[0-9a-f]{12}",
                                          "a Minecraft profile uuid")),
        # The person's own consent to the Minecraft EULA (HUMAN-STEPS step 8). Never written on their behalf.
        "eula_accepted": (bool, True, _one_of(True, why=": only the person can accept the EULA "
                                              "(https://aka.ms/MinecraftEULA), and the kit renders nothing until they do")),
        "open_date": (datetime.date, False, None),
        # itzg PAUSE_WHEN_EMPTY_SECONDS -> server.properties pause-when-empty-seconds (itzg property-definitions.json,
        # checked 2026-10-14 on master and in the itzg/minecraft-server:java25 image). Optional: unset, itzg leaves
        # server.properties alone (asus-node: hand edits). -1 for a pre-gen ring (deploy/world.sh), 60 for play.
        "pause_when_empty_seconds": (int, False, _pause),
    },
    "schedule": {
        # The maintenance window's anchor; every other time is a fixed offset (+30, +60, +60..90, +100 min).
        # Only the asus-node anchor is wired so far: the units, setup.sh step 5 and check.sh still carry the offsets.
        "maint_start": (str, True, _one_of("04:00", why=": other anchors are not wired yet (the "
                                           "units, setup.sh step 5 and check.sh hold the 04:00 window)")),
    },
    "net": {
        # dhcp (the default): the router hands out the address. static: wired_conn gets static_addr, commit-confirm
        # style (host/setup.sh step 2); only on request. The rules between these keys are in _extra (NET_RULES).
        "mode": (str, True, _one_of("dhcp", "static")),
        # The wired connection, when the laptop has one. Absent on a Wi-Fi-only laptop.
        "wired_conn": (str, False, _match(NAME_CHARS, "a NetworkManager connection name")),
        # With wired_conn: the Wi-Fi FALLBACK (only on request). Without it: the Wi-Fi-only laptop's one link.
        "wifi_conn": (str, False, _match(NAME_CHARS, "a NetworkManager connection name")),
        "static_addr": (str, False, _cidr),
        "gateway": (str, False, _ipv4),
        "dns": (list, False, _ipv4_list),
        "dhcp_pool": (str, False, _match(r"[0-9.]+-[0-9.]+", "first-last, like 192.168.1.10-192.168.1.254")),
    },
    "tailscale": {
        "host_name": (str, True, _match(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?", "a Tailscale machine name")),
        "server_address": (str, True, _match(r"[a-z0-9-]+\.[a-z0-9-]+\.ts\.net", "a MagicDNS name like "
                                             "host.tailnet.ts.net (Self.DNSName without the trailing dot)")),
        "tag": (str, True, _match(r"tag:[a-z0-9-]+", "a Tailscale tag like tag:mc-server")),
        "device_id": (str, False, _match(r"[A-Za-z0-9]+", "a Tailscale device id")),
    },
    "discord": {
        # The person's choice at the card (HUMAN-STEPS step 23). Absent or true: Discord, and every key below is
        # required. false: no Discord at all, and this key is then the section's only one (validate).
        "enabled": (bool, False, None),
        # A snowflake never starts with 0, so the example's all-zero placeholder is refused, not synced against.
        "guild_id": (str, True, _match(r"[1-9][0-9]{16,19}", SNOWFLAKE)),
        "bridge_bot_id": (str, True, _match(r"[1-9][0-9]{16,19}", SNOWFLAKE)),
        "alert_channel": (str, True, _match(r"[a-z0-9_-]{1,100}", "a channel name from discord/layout.toml")),
        "invite_lockdown": (bool, True, None),
        # Which guild content discord/sync.py serves (discord/sync.py PROFILES): "asus-node" keeps that guild's
        # history (its story, renamed_from); "kit" is a fresh guild (no #the-story, no renames).
        "profile": (str, True, _one_of("asus-node", "kit")),
    },
    "battery": {
        "charge_cap": ((int, str), False, None),  # checked in _extra: 1-100 or "none"
    },
    "pack": {
        # vanilla-extended: pack/ (tracked), the one verified pack. custom: pack-custom/ (gitignored), a pack the
        # person chose with their Claude (docs/kit/CUSTOM-PACK.md); pack/build.py builds it and kit/render.py points
        # compose at it. A vendor mode (a third-party .mrpack) is still reserved.
        "mode": (str, True, _one_of("vanilla-extended", "custom", why=": a vendor mode is reserved")),
    },
}
OPTIONAL_SECTIONS = {"battery"}


def _type_ok(value, typ):
    types = typ if isinstance(typ, tuple) else (typ,)
    if isinstance(value, bool) and bool not in types:  # bool is an int subclass: true must not pass for 1
        return False
    if isinstance(value, datetime.datetime) and datetime.datetime not in types:  # a datetime is a date subclass
        return False
    return isinstance(value, types)


def _extra(cfg, errors):
    cap = cfg.get("battery", {}).get("charge_cap")
    if cap is not None and not (cap == "none" or (isinstance(cap, int) and not isinstance(cap, bool)
                                                  and 1 <= cap <= 100)):
        errors.append("battery.charge_cap: must be 1-100 or \"none\"")
    net = cfg.get("net", {})
    errors += net_problems(net)
    if net.get("mode") != "static":
        return
    try:
        iface = ipaddress.IPv4Interface(net["static_addr"])
        if ipaddress.IPv4Address(net["gateway"]) not in iface.network:
            errors.append(f"net.gateway: {net['gateway']} is not inside net.static_addr's network {iface.network}")
        if ipaddress.IPv4Network("10.213.37.0/24").overlaps(iface.network):
            errors.append("net.static_addr: overlaps the bridge network 10.213.37.0/24 (a kit constant)")
    except (KeyError, ValueError):
        pass  # already reported


STATIC_KEYS = ("static_addr", "gateway", "dns")


def net_problems(net):
    """The rules between the [net] keys. One link at least; static needs a wired connection and all
    three static keys; a static key under dhcp would be config that silently does nothing, so it is refused."""
    out = []
    if not net.get("wired_conn") and not net.get("wifi_conn"):
        out.append("net: name at least one connection: wired_conn (wired laptop) or wifi_conn (Wi-Fi-only laptop)")
    if net.get("mode") == "static":
        if not net.get("wired_conn"):
            out.append("net.mode: static needs net.wired_conn (the static address goes on the wired connection; "
                       "a Wi-Fi-only laptop uses dhcp)")
        out += [f"net.{k}: missing (net.mode is static)" for k in STATIC_KEYS if k not in net]
    else:
        out += [f"net.{k}: set, but net.mode is dhcp (it would do nothing; remove it or set mode = \"static\")"
                for k in STATIC_KEYS if k in net]
    return out


def _discord_off(data):
    table = data.get("discord")
    return isinstance(table, dict) and table.get("enabled") is False


def discord_enabled(cfg):
    """A validated config's choice: True for Discord (the default: discord.enabled absent or true), False for none."""
    return cfg["discord"].get("enabled", True) is not False


def validate(data):
    """-> the config (the same dict). Raises ConfigError naming every problem at once."""
    errors = []
    if not isinstance(data, dict):
        raise ConfigError("kit.toml: not a table")
    discord_off = _discord_off(data)
    for section in data:
        if section not in SCHEMA:
            errors.append(f"[{section}]: unknown section (known: {', '.join(SCHEMA)})")
    for section, keys in SCHEMA.items():
        table = data.get(section)
        if table is None:
            if section not in OPTIONAL_SECTIONS:
                errors.append(f"[{section}]: missing section")
            continue
        if not isinstance(table, dict):
            errors.append(f"[{section}]: must be a table")
            continue
        for key in table:
            if key not in keys:
                errors.append(f"{section}.{key}: unknown key (known: {', '.join(keys)})")
        for key, (typ, required, check) in keys.items():
            if discord_off and section == "discord" and key != "enabled":
                # No Discord: a guild id or a bot id here would be config that silently does nothing (the way a
                # static key under dhcp is refused), and a later switch back to Discord gets them from discover.
                if key in table:
                    errors.append(f"discord.{key}: set, but discord.enabled is false (no Discord: it would do "
                                  "nothing; remove it, or remove enabled = false for Discord)")
                continue
            if key not in table:
                if required:
                    errors.append(f"{section}.{key}: missing")
                continue
            value = table[key]
            if not _type_ok(value, typ):
                names = "/".join(t.__name__ for t in (typ if isinstance(typ, tuple) else (typ,)))
                errors.append(f"{section}.{key}: must be {names}, not {type(value).__name__}")
                continue
            if isinstance(value, str) and _UNSAFE.search(value):
                errors.append(f'{section}.{key}: must not contain " \\ $ ` % @ or a line break')
                continue
            if check:
                try:
                    check(value)
                except ConfigError as e:
                    errors.append(f"{section}.{key}: {e}")
    if not errors:
        _extra(data, errors)
    if errors:
        raise ConfigError("kit.toml refused:\n  " + "\n  ".join(errors))
    return data


def path(p=None):
    return Path(p or os.environ.get(PATH_ENV) or DEFAULT_PATH)


def discord_choice(p=None):
    """kit.toml's discord.enabled as written, for kit/record.py: True (Discord) or False (none). Not validated, since
    kit.toml is filled in phase by phase and the record reads this before it is complete. No kit.toml yet, or no
    `enabled` key, is Discord, the default (the stricter choice: every Discord step and phase is then required)."""
    f = path(p)
    try:
        data = tomllib.loads(f.read_text())
    except FileNotFoundError:
        return True
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{f}: not valid TOML: {e}") from None
    table = data.get("discord", {})
    value = table.get("enabled", True) if isinstance(table, dict) else None
    if not isinstance(value, bool):
        raise ConfigError(f"{f}: discord.enabled must be true or false")
    return value


# ---- the pack the instance runs (pack.mode) -----------------------------------------------------------------
# Which pack is live has one home: the MODRINTH_MODPACK line compose reads. For Vanilla Extended that is
# docker-compose.yml (tracked). For a custom pack it is docker-compose.override.yml, which kit/render.py writes from
# pack-custom/mods.toml and compose merges over docker-compose.yml on every call that names no -f. deploy/backup.sh,
# host/lib/standup.sh and discord/sync.py read it the same way (live_pack_file; host/test_host.py holds them to it).

VE_DIR = REPO / "pack"
CUSTOM_DIR_NAME = "pack-custom"  # gitignored: a custom pack never dirties the tracked pack/ (a pull would conflict)
OVERRIDE_NAME = "docker-compose.override.yml"
MODPACK_LINE = re.compile(r'MODRINTH_MODPACK: *"/pack/([^"]+)"')
# A custom [pack] name: what friends see in their server list and in #setup, and (slugged) the .mrpack's name.
PACK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._+-]{0,39}")
PACK_VERSION = re.compile(r"[0-9]+\.[0-9]+\.[0-9]+")


def pack_dir(cfg, repo=REPO):
    """The directory pack/build.py builds for this instance: pack/, or pack-custom/ for pack.mode = "custom"."""
    return Path(repo) / CUSTOM_DIR_NAME if cfg["pack"]["mode"] == "custom" else Path(repo) / "pack"


def pack_slug(name):
    return re.sub(r"[ ._+]+", "-", name.lower()).strip("-")


def mrpack_name(name, version, chapter):
    """dist/<this>: "Vanilla Extended", "1.0.2", 1 -> vanilla-extended-1.0.2-ch1.mrpack."""
    return f"{pack_slug(name)}-{version}-ch{chapter}.mrpack"


def pack_section(directory):
    """<directory>/mods.toml's [pack] table. A custom one is held to the shapes a file name, the server list and an
    unquoted MOTD can carry, and may not pass itself off as Vanilla Extended."""
    f = Path(directory) / "mods.toml"
    try:
        pack = tomllib.loads(f.read_text())["pack"]
    except FileNotFoundError:
        raise ConfigError(f"{f} is missing (a custom pack: docs/kit/CUSTOM-PACK.md)") from None
    except (tomllib.TOMLDecodeError, KeyError) as e:
        raise ConfigError(f"{f}: no readable [pack] table ({e})") from None
    if Path(directory).resolve() == VE_DIR.resolve():
        return pack
    problems = [f"[pack] {k} is missing" for k in ("name", "version", "minecraft", "fabric_loader") if k not in pack]
    if not problems:
        if not isinstance(pack["name"], str) or not PACK_NAME.fullmatch(pack["name"]):
            problems.append("[pack] name: 1-40 letters, digits, spaces and ._+- (it names the .mrpack and the "
                            "server in the friends' list)")
        elif pack_slug(pack["name"]) == pack_slug(tomllib.loads((VE_DIR / "mods.toml").read_text())["pack"]["name"]):
            problems.append("[pack] name: that is Vanilla Extended's name; a custom pack needs its own")
        if not isinstance(pack["version"], str) or not PACK_VERSION.fullmatch(pack["version"]):
            problems.append("[pack] version: must be like 1.0.0")
    if problems:
        raise ConfigError(f"{f}: " + "; ".join(problems))
    return pack


def live_pack_file(repo=REPO):
    """The .mrpack (a name under dist/) compose runs: the override's MODRINTH_MODPACK when it has one, else
    docker-compose.yml's. Raises ConfigError when neither names one."""
    for name in (OVERRIDE_NAME, "docker-compose.yml"):
        f = Path(repo) / name
        m = MODPACK_LINE.search(f.read_text()) if f.is_file() else None
        if m:
            return m.group(1)
    raise ConfigError('docker-compose.yml names no MODRINTH_MODPACK: "/pack/<file>"')


def load(p=None):
    """Read and validate kit.toml (default: the repo root, or $MC_KIT_TOML). A missing file is a ConfigError."""
    f = path(p)
    try:
        text = f.read_text()
    except FileNotFoundError:
        raise ConfigError(f"{f} is missing: copy kit/kit.example.toml to kit.toml and fill it in "
                          "(a filled-in example: kit/fixtures/example.toml)") from None
    try:
        data = tomllib.loads(text)
    except tomllib.TOMLDecodeError as e:
        raise ConfigError(f"{f}: not valid TOML: {e}") from None
    return validate(data)
