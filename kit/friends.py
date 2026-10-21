#!/usr/bin/env python3
"""The friend message, for a server with no Discord (kit.toml discord.enabled = false; KIT.md P12, HUMAN-STEPS R1).

  python3 kit/friends.py            write the message to ~/minecraft-friend-message.txt (mode 600); print its path
  python3 kit/friends.py --check    exit 0 only when that file is what this would write now (nothing else printed)

With Discord, #setup tells friends how to join. With no Discord, this writes one ready-to-paste message the person
sends their group however they talk: the Tailscale share link, the pack file to import (and where it is on this
laptop, to attach), the server's address, and how to get on the whitelist (send the person their Minecraft name).
The text is kit/friend-message.txt, filled in from kit.toml, the pack compose runs, and discord/.env.

The share link is a secret (discord/sync.py share_link: its shape and age are checked there, its one home). It goes
into the file and nowhere else: this prints only the file's path, so the link never passes through Claude's
transcript. The person opens the file and copies the message. Run it again after a new share link (HUMAN-STEPS R1:
python3 kit/secret.py share-link) or a new pack file: it replaces the file. Never cat, grep or print the file.
Stdlib only; run as the kit user, never root.
"""
import argparse
import datetime
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))
import kitconfig  # noqa: E402

TEMPLATE = HERE / "friend-message.txt"
OUT_NAME = "minecraft-friend-message.txt"
TOKEN = re.compile(r"\{([a-z_]+)\}")
HEADER = """FOR YOU, not part of the message:
- Copy everything between the two dashed lines and send it to your friends however you talk (a group chat, texts,
  email).
- Send the pack file with it: {pack_path}
- The link in the message is private: send it only to your friends. A link nobody used stops working after 30 days;
  then make a fresh one (Claude tells you how), and Claude writes this file again.
- When a friend sends you their Minecraft name, tell Claude: "whitelist <their name>".

----- copy from here -----
"""
FOOTER = "----- to here -----\n"


class FriendsError(Exception):
    pass


def _sync():
    """discord/sync.py, for share_link() and read_env(): the share link's one reader. discord/ goes on the path only
    here (sync.py is stdlib only and never imports discord.py)."""
    d = str(HERE.parent / "discord")
    if d not in sys.path:
        sys.path.insert(0, d)
    import sync
    return sync


def values(cfg, env, today, repo=REPO):
    """-> ({token: value}, warnings). Refuses with Discord on, a share link that is missing or not one, and a pack
    file that is not built."""
    if kitconfig.discord_enabled(cfg):
        raise FriendsError("kit.toml says Discord: friends get all of this in #setup (python3 discord/sync.py), so "
                           "there is no message to write")
    try:
        link, warnings = _sync().share_link(env, today)
    except ValueError as e:  # its message names the key and the fix, never the value
        raise FriendsError(str(e)) from None
    try:
        file = kitconfig.live_pack_file(repo)
    except kitconfig.ConfigError as e:
        raise FriendsError(str(e)) from None
    pack = Path(repo) / "dist" / file
    if not pack.is_file():
        raise FriendsError(f"dist/{file} (the pack the server runs) is missing: python3 pack/build.py first")
    return {
        "share_link": link,
        "host_name": cfg["tailscale"]["host_name"],
        "server_address": cfg["tailscale"]["server_address"],
        "pack_name": kitconfig.pack_section(kitconfig.pack_dir(cfg))["name"],
        "pack_file": file,
        "pack_path": str(pack),
    }, warnings


def fill(text, vals):
    def sub(m):
        if m.group(1) not in vals:
            raise FriendsError(f"{TEMPLATE.name}: unknown {{{m.group(1)}}} (known: {', '.join(sorted(vals))})")
        return vals[m.group(1)]
    return TOKEN.sub(sub, text)


def message(vals):
    """The whole file: the person's notes, then the message between the two dashed lines."""
    return fill(HEADER, vals) + fill(TEMPLATE.read_text(), vals) + FOOTER


def _refuse_odd(path):
    if path.is_symlink() or (path.exists() and not path.is_file()):
        raise FriendsError(f"{path} is a link or not a regular file; move it aside, then run this again")
    if path.exists() and path.stat().st_uid != os.geteuid():
        raise FriendsError(f"{path} belongs to uid {path.stat().st_uid}, not you; move it aside, then run this again")


def write_private(path, text):
    """Atomic, created at mode 600 (umask can only narrow it), in the same directory, then renamed over the old."""
    _refuse_odd(path)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise FriendsError(f"{tmp} exists (a run that died; it may hold the link): delete it, then run this again") \
            from None
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8"))
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def problems(path, text):
    """-> what is wrong with the file on disk, in words that never quote it."""
    if not path.exists() and not path.is_symlink():
        return [f"{path} is missing: python3 kit/friends.py"]
    try:
        _refuse_odd(path)
    except FriendsError as e:
        return [str(e)]
    out = []
    if path.stat().st_mode & 0o777 != 0o600:
        out.append(f"{path} is mode {oct(path.stat().st_mode & 0o777)[2:]}, want 600")
    if path.read_bytes() != text.encode("utf-8"):
        out.append(f"{path} is stale (the link, the pack or the address changed): python3 kit/friends.py")
    return out


def main(argv=None, today=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--check", action="store_true", help="write nothing; exit 1 unless the file is current")
    ap.add_argument("--out", help=f"the file (default: ~/{OUT_NAME})")
    a = ap.parse_args(argv)
    out = Path(a.out) if a.out else Path.home() / OUT_NAME
    try:
        if os.geteuid() == 0:
            raise FriendsError("run as the kit user, not root: the file goes in their home, readable by them only")
        cfg = kitconfig.load()
        vals, warnings = values(cfg, _sync().read_env(), today or datetime.date.today(), repo=REPO)
        for w in warnings:  # share_link's warnings name the age, never the link
            print(w)
        text = message(vals)
        if a.check:
            bad = problems(out, text)
            for p in bad:
                print(f"STALE {p}")
            if not bad:
                print(f"{out}: current (contents not shown)")
            return 1 if bad else 0
        write_private(out, text)
        print(f"wrote {out} (mode 600; the share link inside is not shown here). The person opens it and copies "
              "the message; the pack file to send with it is named at its top.")
        return 0
    except (FriendsError, kitconfig.ConfigError, OSError) as e:
        print(f"friends: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
