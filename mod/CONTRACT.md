# mcve contract v1: the server mod (`ve-commands`) and the bridge, which talk only through files

**Directories:** two, one per writer. The minecraft container sees them under `/data/mcve/`, and the host under
`mc_data/mcve/`.

| Directory | Who writes it | What is in it | The bridge mounts it |
|---|---|---|---|
| `mcve/out/` | the mod only | `events.jsonl`, `events.jsonl.1`, `inbox.offset` (and its `inbox.offset.tmp`) | **read-only** |
| `mcve/in/` | the bridge only | `inbox.jsonl`, plus the bridge's own offset file | read-write |

The mod creates `mcve/`, `out/` and `in/` at start, and disables itself if any of them is a symlink or not a
directory. In `in/` it only ever reads `inbox.jsonl`; it never touches the bridge's other files.

**Encoding:** UTF-8 JSON Lines, one object per line, `\n`-terminated. Inbox lines must be **strict** JSON (RFC 8259):
no comments, no single quotes, no unquoted keys or values, no `NaN`, no trailing data after the object. A lenient
line is malformed. Writers append whole lines with a single
write and flush. Readers keep a byte offset.

## out/events.jsonl (the mod writes, the bridge reads)

Every event carries `"v":1`, `"ts"` and `"type"`, in that order, then its fields in the order below. `ts` is
ISO-8601 UTC with exactly three fractional digits, and always matches
`^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$` (`2026-10-01T20:00:03.000Z`).

| type | fields | emitted when |
|---|---|---|
| `server` | `state`: `"started"` or `"stopping"` | server started; server stopping |
| `chat` | `player` (name), `text` (plain string) | a player's chat message (Fabric `ServerMessageEvents.CHAT_MESSAGE`) |
| `game` | `text` (rendered plain text) | a broadcast system/game message: joins, leaves, deaths, advancements. Use Fabric `ServerMessageEvents.GAME_MESSAGE`, or the closest equivalent on 26.1.2 |
| `discovery` | `player`, `dim`, `x`, `y`, `z` (int), `biome` (id), `text` | `/discovery <text>` |
| `coords` | `player`, `dim`, `x`, `y`, `z` (int), `biome` (id), `name` | `/coords <name>` |
| `status` | `online` (list of names), `max` (int) | every 60 s, and on every join or leave |
| `whitelist_result` | `id` (the request id), `name`, `ok` (bool), `detail` (short string) | after handling an inbox `whitelist_add` |

**Rotation:** the mod rotates `events.jsonl` to `events.jsonl.1` once it passes 5 MiB, keeping one old file.
The bridge detects rotation (inode change or size shrink) and continues from offset 0 of the new file.

## in/inbox.jsonl (the bridge writes, the mod reads)

The mod polls about once a second. It persists its read offset in `out/inbox.offset` **after every poll**, and
each poll handles at most **200 lines or 64 KiB**, so a backlog is worked through in bounded steps. A crash between
dispatching a poll's lines and saving the offset can replay **at most that one poll's budget** (200 lines or
64 KiB) on the next start, never more. A backlog of more than **1 MiB** past the offset is a flood, not news: the mod skips to the
end (just past the last complete line), acts on none of it, and logs the skip (throttled, see below). **With no usable offset**
(missing, unreadable, negative, or a symlink) the mod starts at the **end** of `inbox.jsonl` and records that at
once. It never replays from 0, because a replayed `whitelist_add` would re-add a name the operator removed. If
`inbox.jsonl` does not exist yet, the end is 0, and the bridge's first lines are read in full.

| type | fields | the mod's action |
|---|---|---|
| `say` | `id`, `author` (Discord display name), `text` | Broadcast to all players as `[Discord] <author>: <text>`, cleaned (see "Cleaning"). The author is at most 32 code points, the text at most 256. Never interpreted as a command. **At most 5 per second** (bursts of 5); the excess is dropped, with a summary logged at most once a minute. |
| `whitelist_add` | `id`, `name`, `requested_by` | In this order: (1) validate `name` against `^[A-Za-z0-9_]{3,16}$`; (2) look for it in the existing whitelist by name, ignoring case; a hit answers `ok=true`, `detail="already"` with the stored canonical name, and **no Mojang lookup is made**, so throttling or an outage cannot turn "already" into "unknown account"; (3) resolve the profile through the server's own profile lookup and add it. Emit `whitelist_result`. Unknown account gives `ok=false`. **At most 10 per hour** (bursts of 10), counting only names that pass (1): an invalid name is answered `invalid name` without spending a token. The excess is never acted on: it is answered `ok=false`, `detail="rate limited"`. |

**`id`** is a JSON string or a JSON integer (`-?[0-9]{1,18}`), echoed back unchanged. A line whose id is missing,
`null`, a bool, a fraction, an exponent, an object or an array is malformed. The bridge writes uuid strings.

**Cleaning** (`say` author and text; the mod's `/discovery` and `/coords` input too). The bridge applies the same
rules, and the shared examples pin both sides:
1. `§` removes itself and the code point after it, whatever that code point is.
2. Whitespace is the Unicode White_Space set: U+0009–000D, U+0020, U+0085, U+00A0, U+1680, U+2000–200A,
   U+2028, U+2029, U+202F, U+205F, U+3000. Each run of it becomes one space, and none is left at either end. So a
   say cannot pad itself onto what looks like a second chat line.
3. Every other control character is removed: the rest of C0, DEL, and the rest of C1. So are the bidi controls
   U+202A–202E, U+2066–2069, U+200E, U+200F and U+061C, and the characters that render blank without being
   White_Space: U+2800 (braille blank), U+3164 and U+FFA0 (Hangul fillers), U+115F and U+1160 (Hangul jamo
   fillers), U+200B (zero-width space) and U+FEFF (BOM). So an author made only of fillers becomes `?`.
4. Then the result is clamped by code points. If the text is empty, the line is skipped. If the author is empty,
   it becomes `?`.

**Any other type, or a malformed line,** is skipped. Skips are logged **throttled**: the first at once, then at most
one line a minute carrying the count since the last report and the first offending line (escaped). Every other
inbox-driven log line (dropped says, refused whitelist_adds, backlog skips, shrinks, read failures, a refused path)
is throttled the same way, so no inbox content can grow the server log faster than a few lines a minute.

## Security boundary

The mod treats the bridge as **untrusted**. The bridge's own rate limits live in the component this boundary
assumes compromised, so the mod enforces its own. A compromised bridge controls everything in `mcve/in/`, and
through the mod it can do only two things:
- broadcast cleaned, clamped text as `[Discord] <author>: <text>`, at most 5 lines per second
- add to the whitelist a name that matches `^[A-Za-z0-9_]{3,16}$` and resolves to a real Mojang account, at most
  10 per hour (each one is logged with its escaped `requested_by`)

What it cannot do:
- **Run a command.** The mod has no path from the inbox to the command dispatcher. Unknown types and extra fields
  are ignored.
- **Exhaust the server with a flood.** Rv2 crashed a 1 GB server with 5M say lines (360 MB): an OOM in the tick
  loop, replayed on every restart. Now:
  - each poll is bounded to 200 lines or 64 KiB, and the offset is saved after it
  - a backlog over 1 MiB is skipped unread
  - say and whitelist_add are rate-limited as above, so the server thread's queue stays bounded
  - the inbox and status jobs survive any throw, an `Error` included, and their failures are logged at most once
    a minute
- **Amplify itself into the server log.** Each inbox-driven log line has its own one-a-minute throttle (see "Any
  other type" above), and a refused path is reported at most once a minute per path, so a bridge flipping
  `inbox.jsonl` between a link and a file every poll gets one line, not two a second. Measured live on the workstation
  spike (2026-10-01): 327 KB of control-byte lines, 10 truncate-rewrites and 20 link flips grew the server log by
  2.85 MB (2245 lines) on 1.0.3, and by 3.5 KB (5 lines) on 1.0.4.
- **Forge the server log.** Every inbox-sourced string that reaches the log (the skipped line, a parse error,
  an unknown type, `requested_by`, a refused id or name) is escaped. Controls, ESC, CR, LF, bidi controls and
  U+2028/2029 are written as JSON-style escapes, and the string is cut to a bounded length.
- **Make the mod write outside `mcve/out/`.** The bridge cannot write `out/` (read-only mount), and the mod never
  writes into `in/`.
- **Redirect the mod through a link.** Every mcve path is checked with lstat semantics (`NOFOLLOW_LINKS`) before
  use, opened with `O_NOFOLLOW`, and must be a regular file (or a real directory, for the three dirs). A symlink,
  fifo, device or directory in a file's place is refused and logged (throttled per path), and the mod never
  follows it.
  A planted `inbox.offset.tmp` link is deleted (the link, not its target) before the mod writes a fresh file with
  `O_CREAT|O_EXCL`.
- **Feed the mod world bytes.** A symlinked `in/inbox.jsonl` is refused, not read.

What the mod cannot bound, and the bridge's container limits must:
- **Disk:** `in/` sits on the world's filesystem, so a bridge can fill it.
- **Host RAM:** a bridge can exhaust it, and the host OOM killer then goes for the minecraft JVM.

Those are compose and host concerns, not part of this contract.

Residual risk: the lstat-then-open check is not atomic. A process that can write the directory could swap a file
for a fifo in that window and stall the inbox thread in `open()`. It can never stall the server thread or the
status beat, which has its own thread. In this layout only `in/` is bridge-writable, and only `inbox.jsonl` is read
from it. The stall survives removing the fifo; a **server restart** clears it (see "Poisoned inbox").

## Poisoned inbox (recovery)

A flood or a garbage-filled `in/inbox.jsonl` needs no server restart. **A stalled inbox thread does** (below).
- **What the mod does on its own:**
  - over 1 MiB of backlog, it skips to the end
  - under that, it drains the backlog in bounded polls, applying the limits
- **To clear the file** (for example to stop a misbehaving bridge re-filling it):
  1. `docker compose stop bridge`
  2. Move the file aside, never delete it, so it is kept as evidence:
     `mv mc_data/mcve/in/inbox.jsonl ~/archive/inbox-poisoned-<ts>.jsonl`
  3. Fix the bridge, then `docker compose build bridge` and `docker compose up -d --no-build bridge`. Never use
     a bare `up -d`, which aborts every service if the bridge build fails (AGENTS.md "Discord bridge").

  The bridge recreates the inbox. It is shorter than the mod's saved offset, so the mod reads the new file from
  byte 0, and nothing old is replayed.
- **A stalled inbox thread** (the fifo race in "Security boundary"). Symptoms: `status` events keep coming, but
  Discord messages never reach the game and `whitelist_add` gets no `whitelist_result`. The thread is blocked in
  `open()` on a fifo, and moving the fifo aside does not wake it. To recover:
  1. `docker compose stop bridge`, then move `mc_data/mcve/in/inbox.jsonl` aside as above.
  2. **Restart the server between play sessions** (`rcon-cli list` first; nobody online): `docker compose up -d
     --force-recreate minecraft`. The new inbox thread starts from the saved offset.
  3. Fix the bridge, then `docker compose build bridge && docker compose up -d --no-build bridge`.
- **Never "fix" it by deleting `out/inbox.offset`.** That is harmless, since the mod then starts at the end, but
  it hides how far the mod got.
- **The server log tells the story:**
  - `inbox backlog of N bytes is over 1048576: skipped to byte M unread`
  - `inbox.jsonl shrank (S < offset O); reading it from 0`, or, for a repeat within a minute, `…: skipped to its
    end`
  - `skipped N malformed or unknown inbox line(s); the first at byte B (…): …`
  - `dropped N inbox say lines over the 5/s limit`
  - `refused N whitelist_add over the 10/hour cap; the first: …`

## Shared examples (`mod/contract-examples/`)

Both sides' tests consume these files, and they are the executable form of this contract:
- `events.jsonl`: one or more lines of every event type. The mod's tests rebuild each line through its event
  builders and require byte-identical output.
- `events.jsonl` also pins the `ts` shape (`.000`, `.001`, `.999`), and has every `whitelist_result` detail: `added`,
  `already`, `invalid name`, `unknown account`, `lookup failed: …` and `rate limited`.
- `inbox.jsonl`: well-formed `say` and `whitelist_add` lines, plus the edge cases:
  - formatting codes, including `§` before an astral code point
  - raw control, C1 range boundaries plus U+0085, and DEL, and every one of the bidi set
  - every one of the 25 White_Space code points, collapsed, with long runs and leading or trailing NBSP
  - every blank filler, in text and as a whole author (which becomes `?`)
  - a JSON-escaped newline carrying a fake inbox line
  - an extra `command` field
  - ids: missing, object, array, `true`, `null`, `1.5`, `1e3`, a 19-digit integer; and valid numeric ids (`-7`,
    an 18-digit one)
  - injection names, and a name that is not a string
  - an unknown type, and duplicate `type` keys (the last wins)
  - non-JSON, an array, blank lines
  - lenient JSON (unquoted keys and values, a comment, a trailing comma, trailing data, `NaN`, single quotes),
    all malformed
  - a final partial line with no newline

  It stays under the rate limits.
- `inbox.expected.jsonl`: the exact handler calls the mod makes for `inbox.jsonl`, in order (with `name_valid` for
  each `whitelist_add`). The partial line yields nothing, and the offset stops before it.

## Clarifications (ve-commands 1.0.4, verified on the workstation spike 2026-10-01)

These pin down behaviour that v1 left open. None of them changes a field.

- **`discovery` / `coords` from a non-player source** (RCON, console) log `player: "Server"` at the source
  position. The cooldown applies to that source as well.
- **`status`** is emitted at server start and then every 60 s by the mod's own timer thread, which does nothing
  else (a slow Mojang lookup on the inbox thread cannot delay it). It does not hang off the tick loop, because an
  empty server stops ticking after `pause-when-empty-seconds` (60). It is also emitted on every join and leave.
  `max` is the server's max-players.
- **`game` never echoes the mod's own `[Discord]` broadcast.** Without that rule a Discord message would loop
  back as a `game` event. Overlay (action-bar) messages are not emitted either.
- **`/say` and `/me` are neither `game` nor `chat`.** Vanilla routes them through Fabric's `COMMAND_MESSAGE`.
  v1 defines no event for them, so the mod logs them and does not forward them.
- **`whitelist_result`:**
  - On success, `name` is the account's canonical name (a request for `notch` returns `Notch`). On failure it is
    the requested name, cut to 32 chars.
  - `detail` is one of `added`, `already`, `invalid name`, `unknown account`, `lookup failed: <error>`, or
    `rate limited`.
  - A failed Mojang lookup reports `unknown account`, because the server's profile repository does not tell
    "no such account" apart from a network failure.
- **Inbox shrink:** if `inbox.jsonl` is shorter than the saved offset, the mod reads it again from 0, **at most
  once a minute**. A repeat within the minute skips to the end of the new file (just past its last complete line)
  instead, so a bridge that keeps truncating and rewriting cannot replay its lines. A bridge that recreates the
  inbox once (as in "Poisoned inbox") is read from 0. Only complete (`\n`-terminated) lines are consumed. A line with no newline after 64 KiB is skipped as malformed.
- **Rotation, for the bridge:** a line can land in `events.jsonl` after the bridge's last read and before the
  rotation, so on rotation the bridge should first drain `events.jsonl.1` from its old offset. Inode numbers can
  be reused once `.1` is replaced, so also treat "size < offset" as rotation.
- **Right after a rotation, `events.jsonl` does not exist** until the next event is appended. Rotation is a
  rename, so a missing file is normal and not an error. The mod refuses to rotate onto an `events.jsonl.1` that is
  not a regular file; `events.jsonl` then keeps growing, and the refusal is logged.
