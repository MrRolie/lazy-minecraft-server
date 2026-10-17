#!/usr/bin/env python3
"""Hand the kit a token without it being shown (human steps 12, 15 and 18, and R1's share link). The person runs it
in their second terminal window, never in the Claude chat:

  python3 kit/secret.py discord-admin     the admin bot's token     -> DISCORD_TOKEN in discord/.env
  python3 kit/secret.py discord-bridge    the bridge bot's token    -> DISCORD_BRIDGE_TOKEN in .env.bridge
  python3 kit/secret.py tailscale         the Tailscale API token   -> ~/.config/mc-kit/tailscale-api-token
  python3 kit/secret.py share-link        a new Tailscale share link -> TAILSCALE_SHARE_LINK (+ _DATE, today) in
                                                                        discord/.env

The token (or link) is read at a hidden prompt (getpass, from the terminal itself; with no terminal it refuses rather than
read a visible stdin). It is never printed, and never put in argv or in the environment of another process.

Its SHAPE is checked before anything is written: a Discord bot token is three base64url parts joined by dots; a
Tailscale API access token starts with tskey-api- (tailscale.py's TOKEN_RX). Whitespace or a shell character means
a command, or more than the token, was pasted (proof run 2026-10-16: the whole step-18 command went in at the
hidden prompt): refused. A refusal says what was expected, never what was pasted, and the file is left as it was.

The .env files: the key's line is REPLACED, never appended (proof run 2026-10-16: a re-paste after a bad first one
left a mangled DISCORD_TOKEN line plus a second one). Every line that sets the key (`KEY=`, `export KEY=`,
`KEY = v`) goes, and the new `KEY=token` line takes the first one's place with that line's own ending. Every other
byte is kept, read and written the way render.py does it (UTF-8, split on "\\n" only, temp file then rename, mode
600; a symlinked or foreign-owned file is refused). A line of another key that also holds `KEY=` (two lines glued
together) is refused: the file would mean two things. The readers: discord/sync.py read_env (DISCORD_TOKEN),
discord/bootstrap.py bridge_token and compose's raw env_file (DISCORD_BRIDGE_TOKEN), and render.py, which later
merges DISCORD_GUILD_ID into .env.bridge.
The Tailscale token file is written whole (the token and a newline, mode 600, in ~/.config/mc-kit at mode 700), and
read back with tailscale.py's own read_token before this says it is done.
The share link goes through tailscale.py's own writer (write_link: the key's line replaced, TAILSCALE_SHARE_LINK_DATE
set to today) and is read back through discord/sync.py's reader, as P3 does; never `>>` (review 2026-10-16: the
old printf append put the link in the shell history and a second pair of lines in the file at every rotation).
Each .env write holds render.env_lock across its read and replace, so render.py cannot put an old value back.
Stdlib only.
"""
import argparse
import datetime
import getpass
import os
import re
import stat
import sys
import warnings
from pathlib import Path

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:  # kit/ only, never the repo root (its discord/ would shadow discord.py)
    sys.path.insert(0, str(HERE))
import render  # noqa: E402
import tailscale  # noqa: E402

# target -> (what the person pastes, the key, the file relative to the repo, or None for the Tailscale token file)
TARGETS = {
    "discord-admin": ("the admin bot's token", "DISCORD_TOKEN", Path("discord") / ".env"),
    "discord-bridge": ("the bridge bot's token", "DISCORD_BRIDGE_TOKEN", Path(".env.bridge")),
    "tailscale": ("the Tailscale API access token", None, None),
    "share-link": ("the new share link", "TAILSCALE_SHARE_LINK", Path("discord") / ".env"),
}
DISCORD_RX = re.compile(r"[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+")
COMMANDISH = re.compile(r"[\s$'\"`;|&<>()~/\\]")  # never in either token: a pasted command, or more than the token
LINK_COMMANDISH = re.compile(r"[\s$'\"`;|&<>()~\\]")  # the same, less the / a link has
EXPECT = {
    "discord-admin": "a Discord bot token: three parts of letters, digits, - and _, joined by dots (from the bot "
                     "page: Reset Token, then Copy)",
    "tailscale": "a Tailscale API access token: it starts with tskey-api- (console: Settings, Keys)",
    "share-link": "a Tailscale share link: https://login.tailscale.com/admin/invite/ and a code (console: "
                  "Machines, this machine, Share, Copy share link)",
}
EXPECT["discord-bridge"] = EXPECT["discord-admin"]


class SecretError(Exception):
    pass


# ---- pure -----------------------------------------------------------------------------------------------------

def check_shape(target, token):
    """Refuse a token of the wrong shape. No message carries any of what was pasted."""
    if not token:
        raise SecretError(f"nothing was pasted; expected {EXPECT[target]}. Nothing was written")
    if (LINK_COMMANDISH if target == "share-link" else COMMANDISH).search(token):
        raise SecretError(f"that looks like a command, or more than the token (it has spaces or shell characters; "
                          f"not shown). Paste only the token: {EXPECT[target]}. Nothing was written")
    if target == "tailscale":
        if token.startswith("tskey-client-"):
            raise SecretError("that is an OAuth client secret (tskey-client-...), which cannot create device "
                              f"invites; expected {EXPECT[target]}. Nothing was written")
        ok = tailscale.TOKEN_RX.fullmatch(token)
    elif target == "share-link":
        ok = tailscale.INVITE_RX.fullmatch(token)
    else:
        ok = DISCORD_RX.fullmatch(token)
    if not ok:
        raise SecretError(f"that is not shaped like {EXPECT[target]} (not shown). Nothing was written")


def set_key(text, key, value, name):
    """-> (text with exactly one `key=value` line, how many lines set the key before). The new line takes the first
    old line's place and keeps its line ending; every other line is kept byte for byte (render.env_lines)."""
    out, old = [], 0
    for n, line in enumerate(render.env_lines(text), 1):
        loose = render._LOOSE.match(line)
        if loose and loose.group(1) == key:
            old += 1
            if old == 1:
                body = line.rstrip("\r\n")
                out.append(f"{key}={value}{line[len(body):]}")
            continue
        if f"{key}=" in line and not line.lstrip().startswith("#"):
            raise SecretError(f"{name} line {n} sets another key but also holds {key}= (two lines glued together); "
                              f"split it onto two lines by hand, then run this again. Nothing was written")
        out.append(line)
    if not old:
        if out and not out[-1].endswith("\n"):
            out[-1] += "\n"  # an append onto a last line with no newline would glue two keys
        out.append(f"{key}={value}\n")
    return "".join(out), old


# ---- files ----------------------------------------------------------------------------------------------------

def write_env(path, key, token, name):
    try:
        with render.env_lock(path):
            old_text = render._read_secret_file(path)  # refuses a symlink, a non-file, another user's, non-UTF-8
            new_text, old = set_key(old_text, key, token, name)
            render.write_atomic(path, new_text, 0o600)
            text = path.read_bytes().decode("utf-8")
    except render.RenderError as e:
        raise SecretError(f"{e}. Nothing was written") from None
    got = [ln.rstrip("\r\n") for ln in render.env_lines(text)
           if (m := render._LOOSE.match(ln)) and m.group(1) == key]
    if got != [f"{key}={token}"]:
        raise SecretError(f"{name}: read back, {key} is not the one line just written (values not shown)")
    if old == 0:
        return f"{key} written to {name} (mode 600); the file's other lines are as they were"
    return (f"{key} written to {name} (mode 600), replacing {old} old {key} line{'s' if old > 1 else ''}; the "
            "file's other lines are as they were")


def write_token_file(path, token):
    d = path.parent
    if d.is_symlink() or path.is_symlink():
        raise SecretError(f"{path} (or its directory) is a symlink; it must be a plain file of your own. "
                          "Nothing was written")
    d.mkdir(mode=0o700, parents=True, exist_ok=True)
    if d.stat().st_uid != os.geteuid():
        raise SecretError(f"{d} is owned by uid {d.stat().st_uid}, not you ({os.geteuid()}). Nothing was written")
    os.chmod(d, 0o700)
    if path.exists() and (not stat.S_ISREG(path.stat().st_mode) or path.stat().st_uid != os.geteuid()):
        raise SecretError(f"{path} is not a regular file of your own. Nothing was written")
    try:
        render.write_atomic(path, token + "\n", 0o600)
    except render.RenderError as e:
        raise SecretError(f"{e}. Nothing was written") from None
    try:
        if tailscale.read_token(path) != token:  # the reader P3 runs, on what was just written
            raise SecretError(f"{path}: read back, it is not the token just written")
    except tailscale.TsError as e:
        raise SecretError(f"read back: {e}") from None
    return f"the token is in {path} (mode 600); python3 kit/tailscale.py setup reads it"


def write_share_link(path, link, today=None):
    """tailscale.py's writer (the link's line replaced, its date set), then sync.py's reader, as P3's gate reads it."""
    today = today or datetime.date.today()
    try:
        tailscale.write_link(path, link, today)
        if tailscale.read_back_link(path, today) != link:
            raise SecretError("discord/.env: read back through sync.py, the share link is not the one just written")
    except tailscale.TsError as e:
        raise SecretError(f"{e}") from None
    return (f"TAILSCALE_SHARE_LINK written to discord/.env (mode 600) with TAILSCALE_SHARE_LINK_DATE={today}; the "
            "file's other lines are as they were. Next: python3 discord/sync.py (dry run), then --apply")


def read_hidden(prompt):
    """getpass from the terminal. Its fallback (no terminal: read stdin with echo, after a warning) is refused."""
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass(prompt)
        except getpass.GetPassWarning:
            raise SecretError("no terminal to read from without showing what you paste: run this in a terminal "
                              "window") from None
        except EOFError:
            return ""


def run(target, repo, read=None):
    what, key, rel = TARGETS[target]
    token = (read or read_hidden)(f"Paste {what} (only the token; it will not show), then Enter: ")
    check_shape(target, token)
    if key is None:
        return write_token_file(Path(tailscale.TOKEN_FILE), token)
    if target == "share-link":
        return write_share_link(Path(repo) / rel, token)
    return write_env(Path(repo) / rel, key, token, rel.as_posix())


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("target", choices=list(TARGETS))
    ap.add_argument("--repo", default=str(HERE.parent), help=argparse.SUPPRESS)  # the tests' checkout
    args = ap.parse_args(argv)
    try:
        if os.geteuid() == 0:
            raise SecretError("run it as yourself, without sudo: the file must stay yours")
        print(run(args.target, args.repo))
        return 0
    except SecretError as e:
        print(f"secret: {e}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nsecret: stopped; nothing was written", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
