# The Discord bridge

Game chat ↔ `#game-chat`, `/discovery` → `#discoveries`, `/coords` → `#coords`, the player count as the bot's
presence, and `/whitelist <minecraft_name>` for friends. It runs as the `bridge` service in
`docker-compose.yml`, beside the server.

**The interface to the server is only the mcve file contract** (`mod/CONTRACT.md`, mcve v1), in
`mc_data/mcve/`, split by writer:

- `out/` belongs to the mod (`events.jsonl`, `events.jsonl.1`, the mod's inbox offset). It is mounted into the
  bridge **read-only** at `/mcve/out`, and the bridge tails `events.jsonl`.
- `in/` belongs to the bridge (`inbox.jsonl`, `bridge.offset`), mounted at `/mcve/in`. The bridge appends
  `inbox.jsonl` and the mod polls it. Nothing of the bridge's ever lives under `out/`.
- **No symlinks, ever.** Every file is opened `O_NOFOLLOW` and must be a regular file. A link where a file
  should be is logged once and treated as absent. The split exists so the bridge can't plant a symlink
  anywhere the mod writes.

The bridge never uses RCON, never runs a server command and never reads the Minecraft log. The security
boundary is the mod: the inbox can only broadcast text and add whitelist names.

## Containment (if the bridge is ever compromised)

- **Network: Internet only.** The bridge has its own network, `discord`, with the fixed interface `br-mcve`
  and subnet `10.213.37.0/24` (no ICC, public DNS `1.1.1.1` / `9.9.9.9`).
  - The host fence, `host/bin/mc-bridge-fence` run by `mc-bridge-fence.service` **before docker**, drops
    everything from `br-mcve` to loopback, 10/8, 172.16/12, 192.168/16, 100.64/10 (the tailnet) and
    169.254/16, plus all IPv6.
  - It does this in the raw table, before routing and DNAT, so it covers the host itself, published ports
    (25565 included), the LAN and the tailnet. DOCKER-USER would not: on a Tailscale host, FORWARD hits
    `ts-forward` first, which ACCEPTs anything bound for the tailnet.
  - **Why:** without it, the bridge could relay a stranger from the Internet to the game port. Together with
    `/whitelist`, that would bypass the Tailscale-only rule. Proven on the workstation (rv2: review round 2), and fixed and re-proven:
    the gateway, LAN IP and tailnet IP all time out on the published port, while `discord.com:443` connects.
  - **Every start path is fenced.**
    - bridge.py refuses, with the FATAL hold, unless the fence's stamp `/run/mc-host/bridge-fence.ok`
      exists. The host's `/run/mc-host` is mounted read-only.
    - That covers mc-server.service, the restart policy (a crash, or dockerd restarting it after an unclean
      stop), and a hand-run `up`. Proven live on the workstation (rv3).
    - `mc-server.service` also checks the stamp, in `host/bin/mc-bridge-up`, and mc-alerts if the bridge
      doesn't start.
    - The fence is deliberately not a `Requires=` of docker: a fence failure must never block minecraft.
- **Resources:** 256 MiB memory, 64 pids, 0.5 CPU, and `/tmp` is 16 MiB (the default is half the host's
  RAM). The bridge can't OOM the minecraft JVM or fork-bomb the host. Its stdout (and minecraft's) is capped
  at json-file 3 × 10 MiB, so it can't fill the disk through docker's logs either (rv3).
- **Privileges:**
  - uid 1000, with an empty capability bounding set
  - no setuid or setgid binaries in the image
  - read-only root fs
  - root only in the entrypoint, long enough to hand a root-owned `in/` to 1000
- **Disk, the accepted residual risk:** `mc_data/mcve/in` sits on the world's filesystem, and a compromised
  bridge can append to `inbox.jsonl` without limit. That is an accepted risk (the owner's decision, rv2), and
  `host/check.sh` alarms on it:
  - WARN when `in/` is over 50 MiB
  - FAIL when it is over 500 MiB, or when the `mc_data` filesystem has under 5 GiB free
  - **unattended:** the daily `mc-maint` run mc-alerts when mc_data has under 20 GiB free, or `in/` is over
    50 MiB

  Bounding what an inbox flood does inside the server (per-poll budgets, rate limits) is the mod's side of the
  contract (the rv2 mod finding).

| File | What |
|---|---|
| `core.py` | the pure half: the tailer, event → message formatting, inbox records, rate limits, the watchdog (no discord import) |
| `bridge.py` | the discord.py glue and `main()` |
| `entrypoint.sh` | hands a root-owned `/mcve/in` to uid 1000 (never touches `out/`), then drops to uid 1000 with an empty capability bounding set |
| `requirements.in` / `requirements.txt` | `discord.py==2.7.1`, compiled with hashes (the regenerate command is the first line of the `.txt`) |
| `test_core.py` / `test_bridge.py` | offline tests (the glue tests mock the client) |

## The bridge bot (least capability)

The bridge has **its own bot**. It never uses the admin bot (Claudius) token that `discord/sync.py` uses (`discord/.env`, workstation only).

**Developer Portal** (New Application, name it e.g. "Vanilla Extended Bridge"):
- **Installation:**
  - Installation Contexts: **Guild Install** only (untick User Install)
  - Install Link: **None** (a private bot can't have a default install link)
- **Bot:**
  - Public Bot **off**; Requires OAuth2 Code Grant **off**
  - Privileged Gateway Intents: **Message Content on**; Presence and Server Members **off**
  - Reset Token, and copy it straight into `.env.bridge` on asus-node (below). It never goes in git, chat or
    logs.

**Permissions**, integer **`2147568640`**:

| Permission | Bit | Why |
|---|---|---|
| View Channel | `1 << 10` = 1024 | see the four channels |
| Send Messages | `1 << 11` = 2048 | post events and ops lines |
| Embed Links | `1 << 14` = 16384 | link previews when a player pastes a URL |
| Read Message History | `1 << 16` = 65536 | standard for a channel bot |
| Use Application Commands | `1 << 31` = 2147483648 | `/whitelist` (strictly a member permission; harmless on the bot) |

1024 + 2048 + 16384 + 65536 + 2147483648 = 2147568640.

- **Presence needs no permission:** it is a gateway update, not a channel action.
- **Not granted:**
  - Mention Everyone (every send also uses `allowed_mentions` none)
  - Add Reactions, Manage Messages, Attach Files, and anything Manage
- **Intents in code:** exactly guilds, guild_messages and message_content (`bridge.intents()`, tested).

**Invite URL** (applications.commands is what lets it register `/whitelist`):

```
https://discord.com/oauth2/authorize?client_id=<APP_ID>&scope=bot%20applications.commands&permissions=2147568640
```

Append `&guild_id=<GUILD_ID>&disable_guild_select=true` to pin it to the friend guild.

**`#ops-log` is private** (the ADMIN category denies View to @everyone). A non-admin bot only sees it through an
overwrite, and overwrites are code, so a hand edit would be reverted by the next sync. After inviting the bot:

1. Copy the bot's user id: right-click the bot in the member list → Copy User ID (Developer Mode). For a bot it
   equals the Application ID.
2. Put it in `discord/layout.toml`, ADMIN: `extra_bots = ["<id>"]`.
3. Dry run `python3 discord/sync.py` on the workstation. Expect exactly two patches, ADMIN and ops-log
   `permission_overwrites`. Then `--apply`, then a dry run showing `0 actions`.

Until then, the bridge refuses to start: `#ops-log: bot lacks view_channel, send_messages (add its user id to
ADMIN extra_bots, then sync)`.

## Deploy (asus-node, from `~/minecraft-ai-host`)

This is a server-side change, so `deploy/backup.sh` comes first. Do it between play sessions (`rcon-cli list`).

1. Bring the code over by git bundle (AGENTS.md "Host").
2. `cp .env.bridge.example .env.bridge && chmod 600 .env.bridge`. Fill in `DISCORD_BRIDGE_TOKEN`.
3. `sudo bash host/setup.sh`.
   - Step 10 creates `mc_data/mcve/out`, `mc_data/mcve/in` and `extras/` as mm-mike. It refuses, creating
     nothing, if any part of the path is a symlink.
   - Step 11 enables `mc-bridge-fence.service` and applies the fence.

   Then run `sudo bash host/check.sh`. Its "bridge mailbox" and "bridge fence" sections should PASS, and the
   fence drops can be read only with sudo.
4. `docker compose build bridge`. It needs the network. At boot, `mc-server.service` runs `up -d minecraft`
   first, then `up -d --no-build bridge` with its failure ignored. A missing image therefore costs only the
   bridge, never Minecraft.
5. `docker compose up -d --no-build bridge`. This doesn't touch minecraft, because there is no `depends_on`.
6. Run `docker compose logs -f bridge` and look for `ready as <bot>; channels #game-chat, …`. Then check live:
   - type in `#game-chat` and see it in game
   - chat in game and see it in `#game-chat`
   - `/whitelist` with an already-listed name gives "already on the list", plus one `#ops-log` line
7. **After a bridge code change:** `docker compose build bridge && docker compose up -d --no-build bridge`.

**Never a bare `docker compose up -d`.** If the bridge image is missing and can't be built, it aborts every
service, minecraft included (proven on the workstation). Start minecraft alone, then the bridge.

`mc_data/mcve/{out,in}` are created by `host/setup.sh` and `deploy/world.sh init`. If Docker created them
anyway, they would be root-owned:
- the entrypoint hands a root-owned `in/` to uid 1000
- `out/` would stay root-owned and the mod couldn't write, which `check.sh` FAILs on
Any other owner is left alone, and the bridge refuses to start.

## Behaviour worth knowing

- **Health:** the container is healthy only while `/tmp/ready` is fresh (under 60 s). bridge.py touches it on
  every poll once it is ready on Discord. A FATAL hold, a hung pump, or a bridge not logged in reads
  unhealthy. `host/check.sh` requires healthy, and mc-maint's daily run (05:40) mc-alerts when the bridge is
  running but not healthy.

- **Offsets:** `bridge.offset` holds `{"ino", "off"}`.
  - **First start** (no offset file): the bridge starts at the end of an existing `events.jsonl`. Old history is
    never posted.
  - **Rotation** (inode change): the bridge drains the rest of `events.jsonl.1`, then reads the new file from
    0. A same-inode shrink restarts at 0.
  - Rotation is a rename, so there is no live `events.jsonl` until the mod's next event (up to 60 s). The rest of
    `.1` is drained at once, so a `whitelist_result` in it never waits out the 30 s.
  - A corrupt offset file stops the bridge, rather than guessing. Move it aside to restart from EOF.
  - **Known limit:** the bridge might be down across *two* rotations, about 10 MiB of events. The new file can
    then reuse the saved inode (the mod saw inode reuse). If it is already longer than the saved offset, the
    bridge resumes mid-file: the first partial line is skipped as malformed, and that stretch of history is
    never posted. A shorter file is handled as a restart at 0.
- **At most once:** the offset is saved *before* a batch is posted, so a crash can drop a line but never posts
  one twice.
- **Server known down:** this means after a `server stopping` event, or once the offline watchdog has fired.
  - `#game-chat` messages aren't relayed, so nobody gets a burst of stale chat at the next boot.
  - `/whitelist` answers ⏳ without writing to the inbox.
- **Rate limits:** `say` is 1 per 2 s per author, and extra messages are dropped. `/whitelist` is 3 per user per
  hour, counted only when a request is sent.
- **Watchdog:** if `events.jsonl` hasn't been written for over 3 min while the bridge runs, the presence becomes
  `Server offline?` and one line goes to `#ops-log`. It recovers silently, restoring the last status presence.
  A log briefly missing mid-rotation is not silence: `.1`'s mtime counts, and with neither file the last mtime
  seen still stands.
- **Backlog:** `chat`, `game` and `server` events older than 5 min by `ts` aren't posted, for example after
  the bridge or Discord was down. Server state still follows them. `discovery` and `coords` are posted
  whatever their age. An unreadable `ts` counts as fresh.
- **Late `whitelist_result`** (after the 30 s wait, or from before a restart): **one** `#ops-log` summary line
  per batch, never one per result.
- **Cleaning** is the mod's `Text.clean` (ve-commands 1.0.4), code point for code point, both ways:
  - `§` and the code point after it go, whatever it is (astral too)
  - each run of Unicode White_Space becomes one space, with none at either end. This is an explicit set;
    Python's `isspace` differs.
  - C0, DEL, C1, bidi controls (U+202A–202E, U+2066–2069, U+200E/200F/061C) and blank fillers (U+2800,
    U+3164, U+115F, U+1160, U+FFA0, U+200B, U+FEFF) go. A dropped one doesn't end a whitespace run. An
    all-filler author becomes `?`.
- **Inbox lines are strict JSON** (RFC 8259, which the mod requires since 1.0.4): ASCII-escaped, never NaN,
  one whole line per write. A lone surrogate becomes a `\uXXXX` escape instead of an encode error.
  `test_contract.py` pins which example lines the mod acts on: the lenient and bad-id lines drop out.
- **New whitelist detail:** "rate limited" is the mod's own whitelist_add limit (10 per hour). It gets a ⏳
  "try again in an hour" reply.

  `test_contract.py` pins this against the mod's own expected output.
- **Echo guard:** `game` events starting with `[Discord] ` (the mod's broadcast of our own `say`) are dropped.
- **Fatal start errors** wait `FATAL_HOLD_SECONDS` (600) before exiting. The fatal errors are: a missing or bad
  token, a missing channel or permission, the intent being off, or a missing commands scope.
  - Under `restart: unless-stopped`, that is at most ~144 logins a day. Discord resets a token that passes
    1000 IDENTIFYs in 24 h.
  - A SIGTERM still stops the bridge at once.

## Tests

```
uv run --with pytest --with discord.py==2.7.1 pytest bridge/
```

`test_core.py` and `test_contract.py` need no discord.py. `test_bridge.py` skips itself without it.

`test_contract.py` runs the shared `mod/contract-examples/` through the bridge. **It fails (never skips) if
they are missing**, so a contract change on either side fails a test:
- every example event goes through the real tailer, is routed and is formatted exactly
- the whitelist replies match each example `detail`
- the inbox records have exactly the fields the mod reads
- the bridge's cleaning equals the mod's on every example `say`
- name validation agrees with the mod's `name_valid`
