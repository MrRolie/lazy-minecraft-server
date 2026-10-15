"""The bridge's pure half: tail events.jsonl, turn events into Discord text, write inbox.jsonl.

No discord import here. The only interface to the server is the file contract (mod/CONTRACT.md, mcve v1):
the mod appends out/events.jsonl (read-only to the bridge), the bridge appends in/inbox.jsonl and keeps its
own offset in in/. bridge.py is the Discord glue.

Every file is opened without following a symlink (O_NOFOLLOW) and must be a regular file: the bridge never
plants, follows or writes through a link.
"""
import asyncio
import errno
import json
import logging
import os
import re
import stat
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime

log = logging.getLogger("bridge")

STALE_AFTER = 180          # status lands every 60 s; three missed beats means the server is gone
WHITELIST_TIMEOUT = 30
READ_CAP = 1 << 20         # bytes per poll; a newline-free chunk this long is skipped as malformed
DISCORD_MAX = 2000
MC_NAME = re.compile(r"[A-Za-z0-9_]{3,16}")
DISCORD_PREFIX = "[Discord] "  # the mod's broadcast of our own `say`; never echo it back
STALE_EVENT = 300          # chat/game/server older than this (by ts) is backlog after an outage: not posted
TIMELY = {"chat", "game", "server"}  # discovery/coords are posted whatever their age


# ── files: no symlinks, regular files only ───────────────────────────────────

def open_regular(path, flags, mode=0o644):
    """os.open with O_NOFOLLOW (a symlink raises ELOOP) and a regular-file check on the opened fd."""
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EINVAL, "not a regular file", path)
    except BaseException:
        os.close(fd)
        raise
    return fd


_irregular = set()


def lstat_regular(path):
    """lstat(path) if it is a regular file, None if absent. A symlink or other type is logged once and is
    treated as absent: never followed."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISREG(st.st_mode):
        _irregular.discard(path)
        return st
    if path not in _irregular:
        _irregular.add(path)
        log.error("%s is not a regular file (symlink or special): ignored, never followed", path)
    return None


# ── tailing ──────────────────────────────────────────────────────────────────

class StateError(Exception):
    """bridge.offset exists but can't be used (empty, corrupt, a link). main() holds before exiting."""


class Tailer:
    """Reads complete new lines from events.jsonl and remembers where it stopped.

    State is {"ino", "off"} in bridge.offset. With no state file the bridge starts at EOF (an existing log is
    history, not news); a file that appears later is read from 0. Rotation (the mod renames the log to .1 and
    starts a new one) is an inode change: the rest of the old file is drained from .1, then the new file is
    read from 0. A same-inode shrink is a truncation and restarts at 0. A partial last line waits for its
    newline. Each file is opened once and judged by fstat of that fd, so a rotation mid-poll is seen on the next
    poll, never half-applied. State moves only after every read of a call succeeded.
    """

    def __init__(self, path, state_path):
        self.path, self.state_path = path, state_path
        self.ino, self.off, self._saved = None, 0, None
        try:
            with os.fdopen(open_regular(state_path, os.O_RDONLY)) as f:
                st = json.load(f)
            self.ino = None if st["ino"] is None else int(st["ino"])  # None: no log yet, read it from 0
            self.off = int(st["off"])
            self._saved = (self.ino, self.off)
        except FileNotFoundError:
            live = _open_log(path)
            if live is not None:
                live[0].close()
                s = live[1]
                self.ino, self.off = s.st_ino, s.st_size
                log.info("no %s: starting at the end of %s (%d bytes of history skipped)",
                         state_path, path, s.st_size)
            self.save()
        except (OSError, ValueError, KeyError, TypeError) as e:
            raise StateError(f"{state_path} is unreadable ({e}); move it aside to restart from EOF") from e

    def save(self):
        if (self.ino, self.off) == self._saved:
            return  # nothing moved: no write
        tmp = self.state_path + ".tmp"
        try:
            os.unlink(tmp)  # a leftover, or a planted link: the name goes, never its target
        except FileNotFoundError:
            pass
        with os.fdopen(open_regular(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "w") as f:
            json.dump({"ino": self.ino, "off": self.off}, f)
            f.flush()
            os.fsync(f.fileno())  # a power cut must not leave an empty bridge.offset
        os.replace(tmp, self.state_path)  # rename replaces a link itself, never its target
        dfd = os.open(os.path.dirname(self.state_path) or ".", os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
        self._saved = (self.ino, self.off)

    def read_new(self):
        """New events since the last call (state advanced in memory; call save() to persist)."""
        try:
            return self._read_new()
        except OSError as e:  # e.g. a read error: nothing moves, the next poll retries
            log.warning("reading %s failed (%s); retrying next poll", self.path, e)
            return []

    def _read_new(self):
        # Each file is opened ONCE (O_NOFOLLOW) and its inode and size come from fstat of that same fd, so a
        # rotation landing mid-poll can't pair one file's inode with another file's bytes.
        rotated = self.path + ".1"
        live = _open_log(self.path)
        if live is None:
            # Right after a rotation (a rename) there is no live file until the mod's next event, up to 60 s
            # away. Drain the rest of .1 now if it is the file being tailed, so nothing waits on that event.
            old = _open_log(rotated)
            if old is None:
                return []
            with old[0] as f:
                if self.ino is None or old[1].st_ino != self.ino or old[1].st_size <= self.off:
                    return []
                data, consumed = _complete(f, self.off, rotated)
            self.off += consumed
            return _parse(data)
        events, ino, base = [], self.ino, self.off
        with live[0] as f:
            st = live[1]
            if ino is None:
                ino, base = st.st_ino, 0
            elif st.st_ino != ino:
                old = _open_log(rotated)
                if old is not None:
                    with old[0] as of:
                        if old[1].st_ino == self.ino:  # .1 is the file we were reading: finish it
                            events += _parse(_complete(of, self.off, rotated)[0])
                log.info("%s rotated; continuing from the start of the new file", self.path)
                ino, base = st.st_ino, 0
            elif st.st_size < base:
                log.warning("%s shrank below offset %d; restarting at 0", self.path, base)
                base = 0
            data, consumed = _complete(f, base, self.path)
        self.ino, self.off = ino, base + consumed  # only now: every read above succeeded
        return events + _parse(data)


def _open_log(path):
    """(binary file, its fstat) for a regular file at path, or None if absent. A symlink or other type is
    logged once and treated as absent: never followed."""
    try:
        fd = open_regular(path, os.O_RDONLY)
    except FileNotFoundError:
        return None
    except OSError as e:
        if e.errno not in (errno.ELOOP, errno.EINVAL):
            raise
        if path not in _irregular:
            _irregular.add(path)
            log.error("%s is not a regular file (symlink or special): ignored, never followed", path)
        return None
    _irregular.discard(path)
    return os.fdopen(fd, "rb"), os.fstat(fd)


def _complete(f, off, label):
    """(bytes of complete lines from off, bytes consumed) of the open file f. Loops to EOF, READ_CAP at a time."""
    out, consumed = [], 0
    f.seek(off)
    while True:
        chunk = f.read(READ_CAP)
        if not chunk:
            break
        end = chunk.rfind(b"\n")
        if end < 0:
            if len(chunk) == READ_CAP:  # a "line" over 1 MiB: not a writer we speak with
                log.warning("skipping %d bytes with no newline at offset %d of %s", len(chunk), off + consumed, label)
                consumed += len(chunk)
                continue
            break  # partial line: wait for the rest
        out.append(chunk[:end + 1])
        consumed += end + 1
        if end + 1 < len(chunk):
            f.seek(off + consumed)
    return b"".join(out), consumed


def _parse(data):
    events = []
    for raw in data.split(b"\n"):
        if not raw.strip():
            continue
        try:
            ev = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as e:
            log.warning("skipping malformed line (%s): %.120r", e, raw)
            continue
        if not (isinstance(ev, dict) and ev.get("v") == 1 and isinstance(ev.get("type"), str)):
            log.warning("skipping line without v=1/type: %.120r", raw)
            continue
        events.append(ev)
    return events


# ── formatting ───────────────────────────────────────────────────────────────

_MD = re.compile(r"([\\*_~`|>#\-\[\]()<])")  # < too: no mention or timestamp markup from game text
_URL = re.compile(r"(https?://\S+)")
# Unicode White_Space, as the mod's Text.isWhitespace (Python's str.isspace/strip use a different set)
WHITESPACE = frozenset([*map(chr, range(0x09, 0x0E)), " ", "\x85", "\xa0", "\u1680",
                        *map(chr, range(0x2000, 0x200B)), "\u2028", "\u2029", "\u202f", "\u205f", "\u3000"])
BIDI = frozenset([*map(chr, range(0x202A, 0x202F)), *map(chr, range(0x2066, 0x206A)), "\u200e", "\u200f", "\u061c"])
# Blank fillers (render as nothing or as a blank glyph), as the mod's Text.isBlankFiller (ve-commands 1.0.4)
FILLERS = frozenset(["\u2800", "\u3164", "\u115f", "\u1160", "\uffa0", "\u200b", "\ufeff"])
DIMS = {"minecraft:overworld": "Overworld", "minecraft:the_nether": "Nether", "minecraft:the_end": "End"}


def one_line(s):
    """The mod's Text.clean, code point for code point (mod/CONTRACT.md; the shared examples pin it):
    - `§` and the code point after it (whatever it is) go;
    - each run of Unicode White_Space becomes one space, none at either end (so no text starts a Discord line);
    - C0, DEL, C1, bidi controls (U+202A-202E, U+2066-2069, U+200E/200F/061C) and blank fillers (U+2800,
      U+3164, U+115F, U+1160, U+FFA0, U+200B, U+FEFF) go, without ending a run.
    The order matters: a control between two spaces is dropped and the spaces still collapse to one."""
    out, skip, space = [], False, True  # space: the last kept thing was a space, or nothing yet
    for ch in str(s):
        if skip:
            skip = False
        elif ch == "§":
            skip = True
        elif ch in WHITESPACE:
            if not space:
                out.append(" ")
            space = True
        elif ch >= " " and ch != "\x7f" and not ("\x80" <= ch <= "\x9f") and ch not in BIDI and ch not in FILLERS:
            out.append(ch)
            space = False
    if out and out[-1] == " ":
        out.pop()
    return "".join(out)


def esc(s):
    """Backslash-escape Discord markdown outside URLs (a URL with a backslash in it stops being a link)."""
    parts = _URL.split(one_line(s))
    return "".join(p if i % 2 else _MD.sub(r"\\\1", p) for i, p in enumerate(parts))


def _words(path):
    return " ".join(w.capitalize() for w in path.replace("/", " ").replace("_", " ").split())


def pretty_dim(dim):
    dim = str(dim)
    if dim in DIMS:
        return DIMS[dim]
    path = dim.split(":", 1)[-1]
    return _words(path[4:] if path.startswith("the_") else path) or "?"


def pretty_biome(biome):
    return _words(str(biome).split(":", 1)[-1].rsplit("/", 1)[-1]) or "?"


def _int(v):
    return isinstance(v, int) and not isinstance(v, bool)


def _where(ev):
    return f"{pretty_dim(ev['dim'])} {ev['x']}, {ev['y']}, {ev['z']} ({pretty_biome(ev['biome'])})"


def _str(ev, *keys):
    return all(isinstance(ev.get(k), str) for k in keys)


def _placed(ev):
    return _str(ev, "player", "dim", "biome") and all(_int(ev.get(k)) for k in "xyz")


def route(ev):
    """An event -> ("post", channel, text) | ("presence", (kind, text)) | ("server", state)
    | ("whitelist_result", ev) | None (skip). Pure."""
    t = ev["type"]
    if t == "chat" and _str(ev, "player", "text"):
        return ("post", "game-chat", f"**{esc(ev['player'])}**: {esc(ev['text'])}")
    if t == "game" and _str(ev, "text"):
        text = one_line(ev["text"])
        if not text or text.startswith(DISCORD_PREFIX):
            return None
        return ("post", "game-chat", f"*{esc(text)}*")
    if t == "server" and ev.get("state") in ("started", "stopping"):
        return ("server", ev["state"])
    if t == "discovery" and _placed(ev) and _str(ev, "text"):
        return ("post", "discoveries", f"**{esc(ev['player'])}** found: {esc(ev['text'])}\n📍 {_where(ev)}")
    if t == "coords" and _placed(ev) and _str(ev, "name"):
        return ("post", "coords", f"📌 **{esc(ev['name'])}** — {_where(ev)} · saved by {esc(ev['player'])}")
    if t == "status" and isinstance(ev.get("online"), list) and _int(ev.get("max")):
        return ("presence", presence(len(ev["online"]), ev["max"]))
    if (t == "whitelist_result" and _str(ev, "name", "detail") and isinstance(ev.get("ok"), bool)
            and (isinstance(ev.get("id"), str) or _int(ev.get("id")))):  # id: a JSON scalar, echoed unchanged
        return ("whitelist_result", ev)
    log.warning("skipping event of unknown type or shape: %.200r", ev)
    return None


def event_age(ev, now):
    """Seconds since the event's ts (ISO-8601 UTC), or None if it has no readable ts."""
    try:
        t = datetime.fromisoformat(ev["ts"])
    except (KeyError, TypeError, ValueError):
        return None
    return None if t.tzinfo is None else now - t.timestamp()  # a naive ts would be read as local time


def is_backlog(ev, now, max_age=STALE_EVENT):
    """chat/game/server older than max_age: stale after an outage, not worth posting. An unreadable ts is
    treated as fresh (better one odd line than silently eaten chat)."""
    if ev.get("type") not in TIMELY:
        return False
    age = event_age(ev, now)
    return age is not None and age > max_age


SERVER_TEXT = {"started": "🟢 Server is up", "stopping": "🔴 Server is going down"}


def unclean_restart(prev, state):
    """True for a `started` with no `stopping` since the previous `started`: the JVM died without its shutdown
    (an OOM exit, a kill, or mc-heal's restart of a hung server). prev is the last server state this bridge saw;
    None (nothing seen since the bridge started) proves nothing, so it never counts."""
    return prev == "started" and state == "started"


def unclean_restart_text(ev):
    """The #ops-log line. The time is re-rendered from the parsed ts, so no file text reaches Discord."""
    try:
        t = datetime.fromisoformat(ev["ts"])
        when = t.strftime("%Y-%m-%d %H:%M:%S UTC") if t.utcoffset() is not None and not t.utcoffset() else "?"
    except (KeyError, TypeError, ValueError):
        when = "?"
    return (f"⚠️ minecraft restarted without a clean stop (crash or heal): `started` at {when} "
            "with no `stopping` since the previous start")
OFFLINE = ("custom", "Server offline?")


def presence(n, mx):
    """("playing", "3/12 online") renders as "Playing 3/12 online"; ("custom", text) is a plain status."""
    return ("playing", f"{n}/{mx} online") if n > 0 else ("custom", "Server empty")


def clip(text, n=DISCORD_MAX):
    return text if len(text) <= n else text[:n - 1] + "…"


# ── Discord -> game ──────────────────────────────────────────────────────────

class RateLimiter:
    """At most n hits per key inside a sliding window of seconds."""

    def __init__(self, n, window, clock=time.monotonic):
        self.n, self.window, self.clock = n, window, clock
        self.hits = defaultdict(deque)

    def allow(self, key):
        now, q = self.clock(), self.hits[key]
        while q and now - q[0] >= self.window:
            q.popleft()
        if len(q) >= self.n:
            return False
        q.append(now)
        return True


class Inbox:
    """Appends whole JSON lines to inbox.jsonl, one write() per line (O_APPEND). Strict JSON (RFC 8259, as the
    mod requires since 1.0.4): no NaN, and ASCII-escaped, so a lone surrogate is a valid \\uXXXX escape instead
    of a UTF-8 encode error."""

    def __init__(self, path):
        self.path = path

    def append(self, rec):
        line = (json.dumps(rec, ensure_ascii=True, allow_nan=False, separators=(",", ":")) + "\n").encode("ascii")
        fd = open_regular(self.path, os.O_WRONLY | os.O_APPEND | os.O_CREAT)
        try:
            if os.write(fd, line) != len(line):
                raise OSError(f"short write to {self.path}")
        finally:
            os.close(fd)


def say_record(author, text, attachments=(), new_id=lambda: str(uuid.uuid4())):
    """A Discord message -> inbox `say`, or None when nothing is left to say.
    attachments: content types; images are noted as [image], anything else as [file]."""
    notes = ["[image]" if (ct or "").startswith("image/") else "[file]" for ct in attachments]
    body = " ".join(p for p in (one_line(text), *notes) if p)
    if not body:
        return None
    return {"type": "say", "id": new_id(), "author": one_line(author)[:32] or "?", "text": body[:256]}


def valid_name(name):
    return isinstance(name, str) and MC_NAME.fullmatch(name) is not None  # fullmatch: '$' would pass "Notch\n"


class Whitelister:
    """/whitelist: validate, rate-limit, append whitelist_add, await the matching whitelist_result by id."""

    def __init__(self, inbox, timeout=WHITELIST_TIMEOUT, limiter=None, new_id=lambda: str(uuid.uuid4())):
        self.inbox, self.timeout, self.new_id = inbox, timeout, new_id
        self.limiter = limiter or RateLimiter(3, 3600)
        self.pending = {}

    async def request(self, user_key, name, requested_by, server_down=False):
        """-> (ephemeral reply, ops-log outcome)."""
        name = name.strip()
        if not valid_name(name):
            return (f"❌ “{esc(name[:40])}” isn't a Minecraft name (3–16 letters, digits or _).", "invalid name")
        if server_down:
            return ("⏳ The server isn't running right now. Try again later.", "server down, not sent")
        if not self.limiter.allow(user_key):
            return ("⏳ That's 3 whitelist requests this hour. Try again later.", "rate-limited")
        rid = self.new_id()
        fut = asyncio.get_running_loop().create_future()
        self.pending[rid] = fut
        try:
            self.inbox.append({"type": "whitelist_add", "id": rid, "name": name, "requested_by": requested_by})
            res = await asyncio.wait_for(fut, self.timeout)
        except asyncio.TimeoutError:
            return ("⏳ The server isn't running right now. Try again later.",
                    f"no result in {self.timeout}s (request {rid} stays in the inbox)")
        finally:
            self.pending.pop(rid, None)
        # detail (mod/CONTRACT.md): added | already | invalid name | unknown account | lookup failed: <error>
        # | rate limited.
        # On success `name` is the canonical spelling (notch -> Notch).
        shown = res["name"] if valid_name(res["name"]) else name
        detail = esc(res["detail"])
        if res["ok"]:
            if res["detail"] == "already":
                return (f"✅ `{shown}` is already on the list.", "already on the list")
            return (f"✅ `{shown}` is on the whitelist. See you in game!", f"added as {shown} ({detail})")
        if res["detail"] == "unknown account":  # authlib returns empty for a Mojang outage too
            return (f"❌ No Minecraft account called `{name}`. Check the spelling; if it's right, Mojang may "
                    "be down, so try again later.", f"refused: {detail}")
        if res["detail"].startswith("lookup failed"):
            return ("⏳ Couldn't reach Mojang to check that name. Try again later.", f"refused: {detail}")
        if res["detail"] == "rate limited":  # the mod's own whitelist_add limit (10/hour, server-wide)
            return ("⏳ The server is taking a lot of whitelist requests right now. Try again in an hour.",
                    f"refused: {detail}")
        return (f"❌ Couldn't add `{name}`.", f"refused: {detail}")

    def resolve(self, ev):
        """Deliver a whitelist_result; False when nobody is waiting (timed out, or a previous run)."""
        fut = self.pending.get(ev["id"])
        if fut is None or fut.done():
            return False
        fut.set_result(ev)
        return True


# ── the offline watchdog ─────────────────────────────────────────────────────

class Watchdog:
    """events.jsonl silent for more than STALE_AFTER while the bridge runs -> offline, once.

    A moment with no events.jsonl (right after a rotation, until the mod's next event) is not silence: the
    caller passes None then, and the last mtime seen still counts.
    """

    def __init__(self, started, stale_after=STALE_AFTER):
        self.started, self.stale_after, self.offline = started, stale_after, False
        self.last_seen = None

    def check(self, now, mtime):
        """mtime: the log's mtime, or None if no log exists. -> "offline" | "recovered" | None."""
        if mtime is not None:
            self.last_seen = max(self.last_seen or 0, mtime)
        last = max(self.last_seen or 0, self.started)
        stale = now - last > self.stale_after
        if stale and not self.offline:
            self.offline = True
            return "offline"
        if not stale and self.offline:
            self.offline = False
            return "recovered"
        return None
