# AGENTS.md — friend server operational truth

Canonical operational doc for this repo: a private **Vanilla Extended** Minecraft Java server for ~10 friends,
on one laptop, reached through Tailscale, with onboarding and chat in a Discord guild. `CLAUDE.md` holds the
doctrine. Everything here holds for every instance: the friends' live server, and any server set up with the kit
(`docs/kit/KIT.md`, "The kit" below). An instance's own values live in its `kit.toml`.

**The live server (asus-node)** has its instance record in `docs/instance/asus-node.md`: its status, host, address,
guild, rollout steps and history. That file and the rest of the instance's own paths are listed in
`kit/INSTANCE-PATHS`. They are the original server's private record and are not in this copy
(`kit/instance-check.sh` checks a copy without them).

## Layout

| Path | What |
|---|---|
| `docker-compose.yml` | The server (itzg/minecraft-server, bind mount at `./mc_data`) and the Discord `bridge` service |
| `bridge/` | The Discord bridge: game chat, discoveries, coords, presence and `/whitelist`, through the mcve file contract only (`bridge/README.md`) |
| `deploy/backup.sh` | The daily backup (run at 04:00 by the system unit `host/systemd/mc-backup.{service,timer}`) |
| `host/` | The game host's maintenance as code: `setup.sh` (root, idempotent), `check.sh`, `bin/`, `lib/` (pure shell helpers, tested), `systemd/` (unit templates, `*.in`), `conf/` |
| `docs/kit/` | The kit, for a new server on a fresh laptop: `KIT.md` (the procedure Claude follows, P0–P12, and what is still open), `HUMAN-STEPS.md` (the steps only a person can do, and the count N), `CUSTOM-PACK.md` (choosing and testing another pack) and the first proof run's findings. `kit/test_kit_md.py` holds KIT.md's commands to the code |
| `kit/` | The instance config: `kit.toml` (repo root, gitignored; schema `kitconfig.py`, template `kit.example.toml`) and `render.py`, which renders it into `host/kit.env`, `host/rendered/systemd/` and keys in `.env` / `.env.bridge`. `kit/fixtures/example.toml` is a made-up instance the tests use; `kit/INSTANCE-PATHS` lists the live server's own paths (its `kit.toml` is `kit/fixtures/asus-node.toml`; private, not in this copy). The kit's tools, by phase, are listed in `kit/README.md`: `probe.sh` + `probe.py`, `sudoers.sh`, `packages.sh`, `tailscale.py`, `record.py` (a run's record, `kit/runs/`, gitignored) |
| `discord/` | The Discord guild surface as code (`sync.py`, `layout.toml`, `content/*.md`; a kit guild's own content in `content/kit/`), and `bootstrap.py`, a kit guild's first steps |
| `pack/` | The pack as code: `mods.toml` (the mods), `lock.json` (exact versions), `overrides/config/` (config edits), `build.py` |
| `mod/` | `ve-commands`, the server-only Fabric mod (`/discovery`, `/coords`, the Discord bridge's file side); its contract is `mod/CONTRACT.md` |

## Updating a running server

Any change that reaches a running server (a pull, a chapter, a fix) lands between play sessions (`rcon-cli list`
first), never near 05:40, and with a fresh `deploy/backup.sh` before any server-side change (CLAUDE.md).

- **Pre-pull gate (before every `git pull`).** `docker-compose.yml` reads six keys from `.env` as
  `${KEY:-default}`, so a line for one of them that is already in `.env` goes live at the next `up -d minecraft`
  after the pull (mc-heal, the 05:40 mc-maint reboot, mc-server at boot), before render has run. Count them,
  never print them:
  `grep -cE '^(MEMORY|TZ|DIFFICULTY|MAX_PLAYERS|VIEW_DISTANCE|SIMULATION_DISTANCE)=' .env`
  - `0`: pull as usual.
  - Non-zero: `deploy/world.sh maintenance on render` (mc-heal stands down), pull and render in one command
    (`git pull … && python3 kit/render.py`), then `python3 kit/render.py --check --env-only` (it names each key
    that still differs, never a value), then `maintenance off`. No `up -d minecraft` in between, and not near
    05:40.
  - Either way, `docker compose config --hash minecraft` before the pull and after the render must match: no recreate.
- **After the pull:** `python3 kit/render.py` as the kit user (never sudo; `--check` must then exit 0), then
  `sudo bash host/setup.sh` (any `->`, `set`, `armed` or `ABORTED` line is a stop; a second run prints only
  `unchanged`) and `sudo bash host/check.sh` (any FAIL is a stop). A Discord change: `python3 discord/sync.py`, a
  dry run, before any `--apply`.
- **The kit's new-host tools are for a new laptop only** ("The kit" below): never run them on a server that is
  already up.

## Hard-won server facts (carry forward; do not relitigate)

- **The data dir is a bind mount (`./mc_data:/data`), never a named volume.** Backups, restores and log reads
  all need plain host paths. A named volume silently broke the old telemetry.
- **Pass secrets with `env_file` + `format: raw`.** Modern Compose interpolates env_file values, so a `$` in a
  password or bcrypt-style API key gets mangled without `raw`.
- **Pin the image's Java to the MC version** (1.20.1 → `:java17`, 1.21.x → `:java21`, 26.x → `:java25`,
  booted 2026-09-29). `:latest` ships the newest Java, and modded servers crash on it.
- **Use a real server pack.** A client `.mrpack` can ship renderer/shader/UI mods that crash a dedicated server
  at preLaunch (`net.minecraft.class_310`, Indium). Verify that a pack is server-installable before building on
  it.
- **Pin Modrinth by version ID, not display string.** The display string can silently no-op the pin (seen with
  `3.1.53hf`).
- **Assume the CurseForge API is unavailable.** itzg `AUTO_CURSEFORGE` needs a `CF_API_KEY` approved for
  `/v1/mods/*`. Approval is opaque (403 on every mods call while `/v1/games` works). Prefer Modrinth, or a
  manually downloaded server pack.
- **Pack metadata can be stale.** A pack's `variables.txt` loader version was out of date (0.16.14, while its
  mods needed ≥0.18.3). Trust the pack's bundled launcher jar or the mods' own requirements.
- **RAM:** set `MEMORY: 8G` + Aikar flags on a 15 GiB laptop. At 10G, JVM off-heap left only about 760 MiB
  free. Re-measure with the real pack and ~10 players (`free -h` in steady state) before raising it.
- **Boot:** on the original laptop (2 cores / 4 threads, 15 GiB) this pack takes **about 108 s to `Done`** (the
  spike), and is back about 1.5 min after a supervised reboot. A faster machine is quicker; Prominence II took 3–5
  min. Watch `docker compose logs -f minecraft` for `Done (`.
- **Never admin-`/locate` on the live world.** Each sweep leaves hundreds of zero-byte region files, and a
  slow locate can trip the 60 s watchdog and kill the server (seen with a now-removed End mod). Sweep a copy.
- **Never run `spark health`.** It uploads the report to a public spark.lucko.me URL. For TPS, use
  `rcon-cli "tick query"`; `spark tps` output doesn't come back through RCON.
- **With snap Docker, bind-mount under `$HOME` only.** Snap Docker gives the daemon a private `/tmp`, so a
  bind under `/tmp` silently lands somewhere else. `$HOME` paths work (proved on the dev machine).
- **RCON:** leave it enabled and **unpublished**. `rcon-cli` runs inside the container:
  `docker compose exec minecraft rcon-cli <cmd>`.
- **itzg `MODS` (how `ve-commands` deploys, `mod/README.md`):**
  - A missing source file is fatal: `mcopy` exits 2 and minecraft never starts. `host/check.sh` asserts it.
  - It treats a same-size file as up to date, so every build gets a new filename. The rename removes the old copy.
  - A poisoned bridge inbox (a flood in `mc_data/mcve/in/inbox.jsonl`) is bounded by the mod and recovered by
    moving the file aside: `mod/CONTRACT.md` "Poisoned inbox".
- **The healthcheck can show "unhealthy" while the server is fine.** The container is "unhealthy" when the
  server rate-blocks the healthcheck's pings ("repeated malformed traffic"). Check with `rcon-cli list`, not
  the health status. If RCON is dead too, the JVM is hung.
- **A hung JVM never trips `restart: unless-stopped`; mc-heal now covers it.** An OOM'd JVM never exited, so the
  server sat unhealthy for 2 h on 2026-10-01. Two fixes:
  - compose `JVM_XX_OPTS: "-XX:+ExitOnOutOfMemoryError"` turns an OOM into an exit, which Docker restarts. It is
    appended to the Aikar flags (seen in `docker top`). There is no heap dump, because 8 GB would land on the
    world's disk.
  - `mc-heal.timer` restarts a server whose RCON stays dead ("Host maintenance").
  - **RCON answers while the server is paused because it is empty** (spike 2026-10-02: `Server empty for 60
    seconds, pausing`, then 12 `rcon-cli list` replies in ≤0.3 s over 5 min). So an idle server never looks hung.
- **Pre-gen needs `pause-when-empty-seconds` off (-1), and the heap watched.** Chunky generates chunks faster
  than the server unloads them, and unloading happens in the tick loop. A paused server unloads nothing, so
  the 8G heap filled in 7.5 h and OOM'd (2026-10-01, Overworld at 77%). Unpaused, the backlog still grows: an
  allocation-failure Full GC came after about 30k chunks. Once Chunky stops, chunks unload (603 MB live), so it
  is not a leak and normal play is unaffected.
  - `deploy/world.sh ring` refuses while pausing is on.
  - **A ring sets the maintenance flag** (`maintenance` in the repo root) before Chunky starts, so mc-heal stands
    down. Chunky's load can stall RCON in long GC pauses, and with `continueOnRestart` a heal would restart
    straight back into the ring. The flag does **not** bound an OOM: with ExitOnOutOfMemoryError, Docker's restart
    policy restarts the server, and Chunky resumes. The bridge's `#ops-log` line ("restarted without a clean
    stop") is how such a restart shows.
  - **Running a ring:**
    1. Set `-1` in `mc_data/server.properties` and restart. On a kit host whose `kit.toml` sets
       `server.pause_when_empty_seconds`, set it there instead (`-1`, render, `docker compose up -d minecraft`): itzg
       writes `PAUSE_WHEN_EMPTY_SECONDS` into server.properties at every start (its `property-definitions.json`,
       checked 2026-10-14 on master and in the `java25` image), so a hand edit would be overwritten. Without the key
       itzg leaves the file alone. render refuses to drop the key once `.env` has it: set `60` instead.
    2. Run the ring.
    3. If the log shows `Pause Full (G1 Compaction`, run `rcon-cli "chunky pause"`. Wait for the unload, then
       run `"chunky continue"`.
    4. Restore `60` after it finishes, and run `deploy/world.sh maintenance off`. `check.sh` WARNs while the flag
       is set.
    5. `deploy/world.sh done [<dim>=<radius> ...]` is the check that a ring finished. `ring` stamps the world dir
       (`mc_data/<LEVEL>/.ve-ring-<dim>`) before Chunky starts, and `done` counts only evidence newer than that
       stamp: Chunky's task file (`mc_data/config/chunky/tasks/`, centre 0,0 and the ring's radius) and, for a ring
       saved short of its total (a finished ring can be saved more than 50 short: the original server's were 51, 38 and 51),
       Chunky's "Task finished … Processed: <total> chunks" log line. It reads files only, and `MC_DATA=<copy>`
       points it at a copy made with `cp -a` (the checks compare file times). A world rung before the stamp existed
       (the original server's launch rings) has none, so `done` fails there until its next ring.
  - **Measuring the heap:** the G1 `Pause Remark` figure is occupancy, not the live set. The honest number is a
    heap-inspection full GC. The itzg image has no `jcmd`; use a sidecar: `docker run --rm
    --pid=container:minecraft-ai-host-minecraft-1 --user 1000:1000 -e HOME=/tmp eclipse-temurin:25-jdk jcmd
    <pid> GC.class_histogram`.

## Backups & restore

- **Schedule:** `mc-backup.timer` (a system unit running `backup.sh` as the kit user) runs daily at 04:00 local
  (`Persistent=true`: a missed run fires at next boot). A failed run posts a Discord alert
  (`OnFailure=mc-alert@%n.service`).
- **What `backup.sh` does:** if the server is running, it waits (up to 180 s) until RCON answers. Then RCON
  `save-off` + `save-all flush`, and it tars into `backups/world-*.tar.gz` (7-day rotation):
  - the world dir, which is **`$LEVEL` from `.env`** (default `world`)
  - `config`, `server.properties`, `whitelist.json`, `ops.json`
  - **the pack record:** `dist/<the .mrpack compose names>` and `pack/lock.json`
  - **`provenance/`:** `docker-compose.yml` (its `MODS` line), `mod/RELEASE.sha256`, and `build.txt` (the
    MODS value, the .mrpack, and the git commit). It says which ve-commands build the world ran.
- **backup.sh and world.sh never parse compose.** They find the minecraft container by its compose labels and
  use plain `docker exec`. A malformed `.env.bridge` breaks every `docker compose exec/config`, but never the
  backup or the admin path (rv3).
- **Mod jars are not backed up.** The bundled `.mrpack` pins the Modrinth ones, and they re-download.
  **ve-commands is the exception:** it is not on Modrinth and lives in the gitignored `extras/`. Its record is
  the git commit plus `mod/RELEASE.sha256` (the build is reproducible), so a restore rebuilds it (step 5 below).
- **These are failures (exit 1, so the unit shows failed), never silent skips:**
  - a missing world dir or pack file
  - RCON not answering while the server runs
  - a failed `save-off` or `save-all flush`

  A live world is never tarred unflushed. Keep the timer disabled until the production world exists.
- **The tar is written as `*.partial`,** checked with `gzip -t`, then renamed. An interrupted run leaves no
  valid-looking archive, and the next run deletes its `.partial`.
- **Backups are on the same disk.** An off-host copy is still owed once friends have real progress.
- **Install:** `sudo bash host/setup.sh` installs the units; `sudo bash host/setup.sh --stand-up` arms
  `mc-backup.timer` and `mc-server.service` at production stand-up (refused until the world, its pack and a first
  backup exist, and refused when an earlier step of the same run aborted).
- **Restore** (`deploy/world.sh maintenance on restore` first and `maintenance off` last, so mc-heal stands down):
  1. `docker compose down`
  2. `mv mc_data/<LEVEL> mc_data/<LEVEL>.pre-restore-<ts>` (move it, never delete it)
  3. `tar -xzf backups/world-<ts>.tar.gz -C mc_data --exclude='dist/*' --exclude='pack/*' --exclude='provenance/*'`
     (`tar -xzOf … provenance/build.txt` shows the MODS jar and the commit the world ran)
  4. **Pack:** if the tar's `dist/` file differs from the one compose runs, the world needs its own pack back.
     Extract `dist/` and `pack/lock.json` into a scratch dir, compare, and restore the matching `.mrpack`
     into `dist/` plus its `MODRINTH_MODPACK` line before `up`.
  5. **ve-commands jar:** on a fresh checkout or another host, `extras/` is empty, and minecraft will not start
     without the jar that `MODS` names. Rebuild it at the restored commit (`provenance/build.txt` in the tar names it; mod/README "Build"), put it in
     `extras/` under that name, and run `bash host/check.sh`; the extras lines must PASS (the sha matches
     `mod/RELEASE.sha256`).
  6. `docker compose up -d minecraft`, then check with `rcon-cli list`. Then `docker compose up -d --no-build bridge`
     (with Discord only: a kit with `discord.enabled = false` has no bridge, so it starts nothing more).

  The tar holds no ring stamps (`.ve-ring-<dim>`), so `deploy/world.sh done` fails on every restored world until
  it is rung again. Chunky's task files and logs outside the tar can be from after the backup (a backup taken
  mid-ring, or a later ring of the same radius), and a restored stamp would let them vouch for a world that holds
  only the chunks made before it. That failure is the tool refusing what it cannot see, not a broken world:
  `deploy/world.sh status` shows Chunky's state, and a ring at the same radius is never a shrink.
- **Retiring a world** (a new pack, a re-seed): never delete it. Stop the server, then **move** `mc_data/<LEVEL>` to
  `~/archive/<date>_<name>/` with a `MANIFEST.txt` (what it is, its seed, the pack it was generated with: keep that
  pack's `.mrpack` or its pinned version next to it, since a world is dead data without its pack). CLAUDE.md "The
  world is the asset".

## Access (friends)

- **Transport: Tailscale node sharing.** Friends are **not** invited into the tailnet, because they would see
  the owner's other machines.
  - In the admin console, the tailnet's owner goes to Machines → the game host → **Share** and creates a reusable
    invite link (up to 1,000 uses; unused links expire after 30 days).
  - Friends install Tailscale, sign in with any free account, and accept the link.
  - A shared node is quarantined: it gets inbound connections only, and recipients see nothing else in the
    tailnet.
  - Sharing a tagged node works (confirmed 2026-09-28).
  - **Policy rule for friends:** shared users get only `tcp:25565` on `tag:mc-server`, via the rule
    `{"src":["autogroup:shared"],"dst":["tag:mc-server"],"ip":["tcp:25565"]}`.
    - `autogroup:shared` is valid only as a `src`.
    - Any `"src":["*"]` rule would also let shared users in, because `*` includes `autogroup:shared`.
      Replace such a `*` with `autogroup:member` + `autogroup:tagged`, never delete it: that keeps every
      tailnet device and drops only shared users.
  - **Simple Voice Chat** (if the pack uses it) needs its UDP port added to that rule.
  - **A kit host** does all of the above with `kit/tailscale.py` (P3; dry run, then `setup --apply`): it merges that
    rule and the `*` replacement into the policy (If-Match), tags the machine, disables its key expiry, writes a
    multi-use share link to `discord/.env`, then deletes the one-use API token and prints the revoke step.
    **Run live once**, on the kit proof run (2026-10-16) against a fresh tailnet; the tests use a fake built
    from the API spec. Never run it on a
    tailnet whose policy someone already manages by hand: the module would refuse or rewrite it.
- **Address:** `kit.toml` `tailscale.server_address`, port 25565. Shared machines resolve **only by FQDN**; the
  100.x IP also works.
- **Never port-forward publicly.**
- **Whitelist:** friends use `/whitelist <name>` in Discord (the bridge). The admin path is
  `docker compose exec minecraft rcon-cli whitelist add <name>`.

## Discord surface (`discord/`)

- **Guild:** `kit.toml` `discord.guild_id`. The admin bot builds the layout and posts content.
- **Guild settings:** there are no custom roles. The guild is invite-only friends.
- **Layout and content are code.** `discord/layout.toml` defines categories and channels (with topic and
  read-only flag). `discord/content/<channel>.md` holds bot-authored messages; parts are split on a line
  containing only `---`, and each part must be ≤2000 chars.
- **Attachments:** a line containing only `{attach:<name>}` attaches one named local file to that part, and is
  removed from the posted text (max one per part). The only name is `pack`: the `dist/` file compose runs, read
  by `kit/kitconfig.py` `live_pack_file` (a custom pack's `docker-compose.override.yml` first, then
  `docker-compose.yml` `MODRINTH_MODPACK`), as `deploy/backup.sh` and `host/lib/standup.sh` read it.
  - So the pack file in `#setup` follows `MODRINTH_MODPACK` automatically. **After a chapter bump, run sync**
    (dry run, then `--apply`).
  - Fail-closed: a missing file stops the sync before any action ("run python3 pack/build.py first"). Run
    sync from a checkout that has built the pack.
  - An attachment is current when the message has exactly one attachment with the same `filename` and `size`.
    Otherwise the message is edited in place (multipart PATCH; the new file replaces the old attachments).
- **Secrets:** `{secret:<name>}` anywhere in content is filled at sync time from `discord/.env`, resolved up
  front like `{attach:}`. The only name is `share_link`: `TAILSCALE_SHARE_LINK`, the server's share link in
  `#setup` step 1 (see `discord/.env.example`). The link is an access credential, so it is never in git and
  never printed (sync shows `<share_link>`).
  - Fail-closed: a missing, empty or non-`https://login.tailscale.com/admin/invite/<code>` value, an unknown
    secret name, or a malformed or future `TAILSCALE_SHARE_LINK_DATE` stops the sync before any API call.
  - `TAILSCALE_SHARE_LINK_DATE=YYYY-MM-DD` (optional, the day the link was made) prints a WARNING from day 25,
    since unused links may expire at 30 days. **To rotate:** make a new link in the Tailscale admin, run
    `python3 kit/secret.py share-link` and paste it at the hidden prompt (it replaces both keys in place, under
    the env files' lock, and reads the link back through sync's reader), then sync (dry run, then `--apply`).
  - **Deploy order:** `setup.md` uses the secret, so every sync (a chapter's pack sync included) refuses until
    `discord/.env` has the key. Set it before the first sync with `kit/secret.py share-link` (a kit's P3 writes
    it itself); the missing-key refusal prints that command (`SHARE_LINK_CMD` in `sync.py`). Never append with
    `>>`: the link would land in the shell history and a rotation would leave a second pair of lines. Sync
    refuses a glued line and names only the keys. Then dry run (one `edit_msg` in `#setup`, link shown as `<share_link>`),
    `--apply`, and a `0 actions` re-check.
  - **Invite lockdown (design decision, 2026-10-14):** with the link in `#setup`, a forwarded Discord invite is server
    access with no human in the loop. With `kit.toml` `discord.invite_lockdown = true` (every instance so far),
    sync plans `~ role @everyone: Create Invite off` whenever `@everyone` can create invites, PATCHes only that
    bit, and refuses when it cannot read `@everyone`'s permissions. With `false`, sync only WARNs, as before. The
    first dry run on a guild shows whether the bit is on (a `~ role @everyone: Create Invite off` line) before any
    `--apply`.
- **Config:** `{config:<name>}` in content is filled from `kit.toml`, resolved up front like `{secret:}`; it is not
  secret. Names (an allowlist, `CONFIG` in `sync.py`): `server_address`, `host_name` (`setup.md`), `pack_name`
  (the kit `setup.md`: the `[pack]` name of the pack `pack.mode` builds), `pack_intro` (`welcome.md`'s opening:
  `welcome-intro.md` in that pack's directory, so `pack/welcome-intro.md` for Vanilla Extended; a custom pack
  without `pack-custom/welcome-intro.md` refuses, never falls back to Vanilla Extended's text, and its shape is
  checked: one message, at most 1500 characters, no `{secret:}`/`{config:}`/`{attach:}`, every `{#channel}` link
  a text channel of this server's layout), and `world_status`
  (`welcome.md`: "being built" until `server.open_date` is set, then "open (since <date>)"). An
  unknown name stops the sync before any API call. Filled from the original server's fixture, `welcome.md` and its
  `setup.md` are byte for byte the text its guild showed before the kit (a test, where its record is present).
- **Profile:** `kit.toml` `discord.profile` picks the guild's history (`PROFILES` in `sync.py`). `kit`: a fresh
  guild, so `renamed_from` is ignored, `#the-story` is left out (`PROFILES` `drop`) and `#setup` is
  `discord/content/kit/setup.md` (the Modrinth App's current import path and its unknown-modpack warning; a test
  holds its placeholders equal to the original `#setup`'s where that file is present). `welcome.md` is shared
  (`KIT_SHARED_CONTENT`: it names no instance); only its opening is the pack's (`pack_intro`). Everything else is the one layout. `asus-node`: the original
  server's guild, the layout and content as written (its story, `renamed_from`). Its fixture, story, `#setup` as
  posted and `discord/posts/` are instance paths (`kit/INSTANCE-PATHS`).
  - **The P6 stop (`profile_guard`, before any API call):** the `asus-node` profile syncs only the asus-node guild
    (its id from the fixture), so a kit that copied the fixture cannot post that guild's story into a new one. Any
    other profile refuses the asus-node guild, a channel whose content resolves outside `content/kit/` and
    `KIT_SHARED_CONTENT`, and the asus-node address in content, a `{config:}` value or a one-off post; `post`
    refuses `discord/posts/` and asus-node's content files. In a copy without the instance record the `kit`
    profile runs as usual (there is no asus-node guild to protect, and the path checks still hold) and the
    `asus-node` profile refuses, naming the record. Whether the record is here is `kit/instance.py`'s one
    predicate: a tree with only part of it (a deleted or renamed fixture) refuses both profiles, never reads as a
    copy. A kit's own one-off post goes from any path outside `discord/posts/`. A refusal is a stop: never change
    the profile to pass it.
- **Two bots, two tokens, never swapped:**
  - **The admin bot's token** lives in `discord/.env` (gitignored, mode 600; see `discord/.env.example`) on the
    machine that runs Claude Code and sync. It is kept apart from the Compose `.env`, because Compose passes every
    line of that file into the game container's environment.
    - **Kit installs** (design decision, 2026-10-14, one machine): Claude Code runs on the game laptop, so the
      admin token sits there, in `discord/.env` at mode 600 and never in a container or the Compose `.env`.
      Separate processes stand in for the separate machine.
  - **The least-capability bridge bot's token lives on the game host,** in `.env.bridge` (gitignored by `.env.*`,
    mode 600; see `.env.bridge.example`). Only the bridge container reads it.
- **`#the-story`:** the original server's read-only saga (`discord/content/story.md`, an instance path). A kit
  guild has none.
- **Renames:** `renamed_from = ["Old name"]` on a channel renames the existing channel in place, so its
  history and settings stay. Without it, a renamed entry is created as a new channel, and the old one is
  reported as unlisted.
- **Sync behaviour:**
  - `python3 discord/sync.py` is a **dry run**: it prints every action. `--apply` executes.
  - A re-run after apply must print `0 actions` (idempotent).
  - Channels are matched by name within their category.
  - Unlisted channels are reported, **never deleted**.
  - Content is edited in place. On a message-count change, it deletes only the bot's own messages in that
    channel and reposts (with their attachments).
- **Tests:** `uv run --with pytest pytest discord/`.
- **The admin bot keeps Administrator (design decision, 2026-10-02).** It is Claude's hands for reshaping the guild
  as code, and least capability lives in where the token sits and what drives it:
  - **The token:** only in `discord/.env` (mode 600, gitignored) on the machine that runs sync; on a kit install,
    the game laptop (the decision above).
  - **What drives it:** only `sync.py`, which never deletes a channel, and, on a kit host, `discord/bootstrap.py`,
    whose `clean` deletes only the channels Discord's template made with a fresh guild, under the guards in "A kit
    guild's first steps" below.
  - **Why the role doesn't buy much:** a narrower role would still need Manage Channels and Manage Roles,
    which can already wreck the guild. Its one real gain is ban/kick. Meanwhile it would block real needs:
    `system_channel` needs Manage Guild, and Discord events need Manage Events.
  - **What friends' traffic touches:** the least-capability bridge bot (2147568640), not the admin bot.
  - **The fallback set, if this is ever decided otherwise:** 378225675280, which is Manage Channels, View, Send, Embed,
    Attach, History, Manage Roles and the three thread permissions. Add Manage Guild for `system_channel`.
- **`extra_bots`:** `extra_bots = ["<user id>" | "bridge"]` on a category gives other bots the bot's own
  allow overwrite in that category's locked channels. ADMIN names `"bridge"`, so the bridge can post to `#ops-log`;
  its id is `kit.toml` `discord.bridge_bot_id`.
- **Guild id:** `kit.toml` `discord.guild_id` is its one home; `kit/render.py` writes it into `.env.bridge`. sync refuses
  a `DISCORD_GUILD_ID` left in `discord/.env` (or the environment) that differs. sync needs `kit.toml` on the
  machine that runs it. `kit.toml` refuses an id with a leading 0, so the example's placeholder never reaches a sync
  or `.env.bridge`.
- **A kit guild's first steps (`discord/bootstrap.py`; not for a guild that already has them).** Dry run by
  default; exit 0 = done and checked, 2 = an action or a human step is pending, anything else = refused. Bot
  tokens only (`GET /users/@me` must say `bot: true`); a user token is never used.
  - `discover` (KIT.md P1, read-only): both tokens (`discord/.env`, `.env.bridge`) must be different bots, each in
    exactly one guild, the same one. Prints `guild_id` and `bridge_bot_id` for `kit.toml`, and checks `kit.toml`
    against them (refuses a different non-placeholder value). A bot in no guild: prints its authorize link (admin:
    Administrator, 8; bridge: 2147568640, pinned to the guild).
  - `intents` (P1): Message Content for the bridge app by `PATCH /applications/@me` (flags OR
    `GATEWAY_MESSAGE_CONTENT_LIMITED`, `1<<19`), then a GET read-back. A refused PATCH or a missing bit prints
    the portal toggle as a HUMAN STEP.
  - `clean` (P6, before the first `sync.py --apply`): deletes every channel and category the kit layout does not
    list (`sync.unlisted`, plan()'s own matching; no template names, which depend on the client's language).
    Refused before any API call unless `discord.profile = "kit"` and `profile_guard` passes. With something to
    delete, it is also refused unless `approximate_member_count` (`GET /guilds/{id}?with_counts=true`) equals the
    verified members among the owner and the two kit bots (`GET /guilds/{id}/members/{user}`, which needs no Server
    Members Intent), the admin bot holds Administrator (without Read Message History the API returns an empty
    message list, not an error), every channel to delete was made with the guild (its snowflake time within 60 s
    of the guild id's: the template's channels are created with the guild, so an empty channel a person made
    later, or anything made after the first sync, is never taken), every text channel to delete has no thread
    (`GET /guilds/{id}/threads/active` by parent, and its archived public and private threads: a private thread
    leaves no message in its parent, and deleting the parent deletes it), and holds only member-join messages
    (type 7) from those members. Children are deleted before categories, then the channel list is read back.
    The snowflake window and the thread reads are from Discord's docs, not seen on a real guild yet.
  - `webhook` (P6, after `sync.py --apply`): the `mc-alert` webhook in `#<discord.alert_channel>` (reused if the
    bot already made one; one with no token in the listing is refused, never duplicated), checked with
    `GET /webhooks/{id}/{token}` (nothing posted), then staged at `<host.home>/.config/mc-kit/webhook` (600).
    `host/setup.sh` step 7 reads it as the kit user, installs `/etc/mc-host/webhook` and removes the staged copy.
    The URL is never printed.
  - `invite` (P12): after the lockdown (refuses while `@everyone` can create invites), a 7-day, any-uses invite
    into the layout's first channel (`#welcome`); a bot-made one with a day or more left is reused. Checked with
    `GET /invites/{code}`. It prints the link: with the lockdown on it is the one way in.
  - Every API path here is from the Discord docs. `discover`, `intents`, `webhook` and `invite` worked live on the
    kit proof run (2026-10-16, `docs/kit/2026-10-16_proof-run-findings.md`); `clean` came after it and is
    **unproven live**.

## Discord bridge (`bridge/`)

The `bridge` compose service links the game and the guild. The details are in `bridge/README.md`.

- **Interface:** only the mcve file contract (`mod/CONTRACT.md`), in `mc_data/mcve/`, split by writer.
  - `out/` is the mod's: the server-side `ve-commands` appends `events.jsonl` there. The bridge mounts it
    **read-only**.
  - `in/` is the bridge's: `inbox.jsonl` and `bridge.offset`.
  - The bridge never follows a symlink (`O_NOFOLLOW` plus a regular-file check), so it can't plant one where the
    mod writes.
  - **No RCON, no server commands, no log reads.** It runs on its own Docker network (`br-mcve`,
    `10.213.37.0/24`), and the host fence lets it reach the **public Internet only**.
- **Containment:**
  - **Egress fence:** `mc-bridge-fence.service`, run before docker. Raw-table drops from `br-mcve` to
    loopback, RFC 1918, the tailnet (100.64/10) and link-local, plus all IPv6.
    - Without it, the bridge relayed the published game port through the gateway and the LAN IP (rv2,
      proven).
    - DOCKER-USER can't do it on a Tailscale host, because `ts-forward` ACCEPTs tailnet traffic first.
    - The bridge starts only behind the fence's stamp, **on every start path**: `bridge.py` refuses (a held
      FATAL) unless `/run/mc-host/bridge-fence.ok` exists. That dir is mounted read-only into the container,
      so this covers mc-server.service, the restart policy after a crash or unclean daemon stop, and a
      hand-run `up` (rv3, proven live).
    - `mc-server.service` starts it through `mc-bridge-up`, which also mc-alerts when it can't.
  - **Limits:** 256 MiB, 64 pids, 0.5 CPU, 16 MiB `/tmp`. Container logs (both services) are capped at
    json-file 3 × 10 MiB.
  - **Disk:** the residual risk of `in/` filling the world's filesystem is accepted.
    - check.sh WARNs above 50 MiB, and FAILs above 500 MiB or with under 5 GiB free.
    - **Unattended:** the daily `mc-maint` run mc-alerts when the mc_data filesystem has under 20 GiB free,
      or `in/` is over 50 MiB.
  - **Health:** check.sh requires the bridge **running/healthy**. The compose healthcheck wants the
    `/tmp/ready` heartbeat, which bridge.py touches every poll once it is ready on Discord, so a 600 s FATAL
    hold reads unhealthy. Unattended, mc-maint's daily run mc-alerts on a running but unhealthy bridge.
    check.sh also validates `.env.bridge`'s shape and mode (values never shown) and
    FAILs when `docker compose config` can't parse.
- **What it carries:**
  - chat, joins/leaves/deaths/advancements and server up/down ↔ `#game-chat`
  - `/discovery` → `#discoveries`
  - `/coords` → `#coords`
  - the player count as the bot's presence
  - `/whitelist <minecraft_name>` (guild-scoped slash command, any member, 3 per user per hour; every request
    is logged in `#ops-log`)
  - `Server offline?` plus one `#ops-log` line once `events.jsonl` has been silent for over 3 min
- **Its own bot,** separate from the admin bot. It has permissions `2147568640` (View, Send, Embed Links, Read
  History, Use Application Commands), with Message Content the only privileged intent. The Developer Portal
  settings and the invite URL are in `bridge/README.md`.
  - Its user id goes in `kit.toml` `discord.bridge_bot_id` (layout.toml's ADMIN `extra_bots` names `"bridge"`), or it
    can't see `#ops-log` and refuses to start.
- **The rule: nothing about the bridge may ever stop Minecraft from starting.**
  - **The rule, as proven on the dev machine (2026-10-01), with a stub minecraft service:**
    - A bare `docker compose up -d` whose bridge image is missing and fails to build aborts **every** service,
      minecraft included.
    - So `mc-server.service` runs `up -d minecraft`, then `-timeout 60 … up -d --no-build bridge`. The
      timeout means a hung bridge start can't eat `TimeoutStartSec`.
    - Run the same two steps by hand: never a bare `up -d`.
  - `env_file` `.env.bridge` is `required: false`. A missing token file stops nothing.
  - A missing `mc_data/mcve/{out,in}` stops nothing either, because Docker creates them. They would be
    root-owned, though, and the mod couldn't write `out/`.
    - So `host/setup.sh` (step 10) creates `mcve/out`, `mcve/in` and `extras/` as the kit user, 755.
      `deploy/world.sh init` creates the two mcve dirs.
      - Both refuse, creating nothing, if any part of the path is a symlink. `install -d` would follow it,
        as root.
    - `check.sh` asserts that `mcve`, `out`, `in` and `extras` are real directories with no symlink on the
      path, the kit user's and 755, plus that user's uid being 1000 (itzg's and the bridge's). It WARNs if the bridge
      container isn't running.
    - The entrypoint hands a root-owned `in/` (only) to 1000.
  - **Never `create_host_path: false`.** A missing source then leaves minecraft "created" but never started
    (proven).
  - No `depends_on`, because `up -d bridge` must never start minecraft.
  - `init: true`, so SIGTERM reaches Python.
  - Read-only root fs, and capabilities dropped to what the entrypoint needs to own `/mcve/in`. The bridge itself
    runs as uid 1000 with none.
  - `mc-server.service` brings it up and down with the server.
- **Stand-up / update:**
  1. `deploy/backup.sh`
  2. `docker compose build bridge` (needs the network; never leave the image to be built at boot)
  3. `docker compose up -d --no-build bridge` (after a code change, `docker compose build bridge` first)
  4. `docker compose logs bridge` shows `ready as …`
- **A fatal start error** (bad token, missing channel or permission, intent off) is logged once and then held
  600 s before exit. A restart loop must not burn Discord's 1000-logins-a-day token budget.

## The pack (`pack/`)

- **Build:** `python3 pack/build.py [--chapter N]` writes `dist/vanilla-extended-<version>-ch<N>.mrpack`
  (gitignored; about 11 KB, since it references Modrinth downloads).
  - `--update` re-resolves every unpinned mod.
  - `pack/lock.json` is the committed reproducibility record: every mod of every chapter at an exact version
    id.
- **Lint is fail-closed:** Modrinth "unsupported" sides, jar `environment`, per-side `depends` satisfaction
  (including jar-in-jar and version predicates), `breaks`, Modrinth-required projects, no
  `overrides/mods`, and nothing in `client-overrides/` but `servers.dat`.
- **`client-overrides/servers.dat` (pack 1.0.2):** the server, pre-listed in every friend's Multiplayer list.
  - `build.py` writes it from `kit.toml` `tailscale.server_address`, the one home of the address (it refuses to build
    without `kit.toml`). `pack/test_build.py` fails if a tailnet address appears in any tracked file outside the
    instance paths (`kit/INSTANCE-PATHS`), and holds `#setup`'s fallback line to `{config:server_address}`.
  - The format is 26.1.2's `ServerList`: uncompressed NBT, `{servers: [{name, ip, hidden: 0b}]}`, with no
    `acceptTextures`, since the server sends no resource pack. The bytes are fixed, so the no-rewrite rule holds.
  - 1.0.2 is 1.0.1 plus `servers.dat` alone: same index files and overrides, only `versionId` changes (diffed
    2026-10-02). A spike 1.0.1 → 1.0.2 reinstall kept one `ve-commands` jar (loaded), the same 84 loaded
    mods and the same itzg manifest, and put no `servers.dat` anywhere under `/data`. itzg never installs
    `client-overrides/`.
  - The compose `MOTD` carries `pack <version>`, equal to `mods.toml` and to `MODRINTH_MODPACK` (a test), so a
    friend on an old pack sees it in the server list. The `·` reaches the status ping intact (spike).
  - **Unverified:** the GUI import in Prism and in the Modrinth App, whether either shows the entry, and
    whether a launcher "update from file" over an existing instance replaces a friend's own servers list.
    Check these at the first client install.
- **Config edits go through `pack/overrides/` only.** Copy the mod's harvested default into
  `pack/overrides/config/` and edit only the lines you mean to change, so a diff against the default shows
  exactly the intent.
  - **Never hand-edit a file under `mc_data/config/` that `pack/overrides` also ships.** itzg re-copies
    overrides with replace-existing on every pack install, so a hand edit is silently reverted (the
    mc-image-helper 1.70.2 bytecode was checked).
  - Some mods rewrite their own file at boot, but the values hold (spike).
  - Files the pack does *not* ship can be edited in place, but prefer adding them to overrides.
- **Worldgen mods are `freeze = true`** in `mods.toml`: `--update` never moves them, because a version change
  can alter generation mid-world.
- **Server side:** compose mounts `./dist` at `/pack`; `MODRINTH_MODPACK` names the exact file.
  - itzg skips `env.server=unsupported` jars.
  - For a **local** `.mrpack`, itzg keys "has the pack changed?" on the **file's mtime**, not its versionId.
    Any rewrite of the file triggers a reinstall, including the overrides re-copy.
  - `build.py` therefore refuses to overwrite a `dist/` file whose contents would change, and leaves an
    identical one untouched, so the mtime is preserved.
  - Every real change needs a `[pack] version` bump, which gives a new filename and a matching
    `MODRINTH_MODPACK` edit in compose.
- **On the host, build before the first `up`.** `dist/` is gitignored, so a fresh checkout has none. Run
  `python3 pack/build.py` on the game host; it builds from the committed lock.
  Otherwise the container crash-loops on a missing pack, and Docker creates a root-owned empty `dist/`.
- **A kit's custom pack:** a kit instance with `pack.mode = "custom"` builds `pack-custom/` (gitignored) with the same
  `build.py`, which also refuses a pack without the kit's own mods (Fabric API, Chunky and its config) or on
  another Minecraft / Fabric pair. `kit/render.py` writes `docker-compose.override.yml` (`MODRINTH_MODPACK`, `MOTD`),
  which compose merges; never hand-edit it. A custom pack's chapter is a `[pack] version` bump
  (`docs/kit/CUSTOM-PACK.md` "Changing the pack later"): the new `.mrpack` boots first in
  `pack/spike.sh --pack <file> --world <backup tar>` (a throwaway copy of the world), and only then does render
  point the override, which `mc-server.service` boots, at it. A Vanilla Extended instance has no override.
  `#welcome`'s opening is the pack's too: `pack-custom/welcome-intro.md`, written with the person
  (`docs/kit/CUSTOM-PACK.md` step 4); sync refuses without it, and `deploy/backup.sh` saves it.
- **Chapters only add mods.** Removing a mod deletes its blocks and items from the world. A chapter is:
  1. labelled backup
  2. rehearse on a copy
  3. bump the version and `MODRINTH_MODPACK`
  4. apply it with `docker compose up -d minecraft`. Never `docker compose restart`: it keeps the old container,
     so a changed `MODRINTH_MODPACK`, `MODS` or mount does not take effect.
  5. pre-generate the new ring (it sets the maintenance flag; `deploy/world.sh maintenance off` once it is done)
  6. `execute in <dim> run worldborder set <diameter>`
  7. announce in `#announcements`

## Host maintenance

`host/` keeps the laptop up, on the network and patched without anyone watching it. It came out of a real outage
(a DHCP address conflict with no Wi-Fi fallback, Wi-Fi rfkill-blocked: the node was off for 8 h) and an earlier
suspend.

- **Getting a change onto a running server** (the pre-pull gate, render, setup.sh, check.sh): "Updating a running
  server" above. `bash host/check.sh` afterwards; `sudo bash host/check.sh` includes the `sshd -T` and snap checks.
- **`setup.sh`** runs as root and is idempotent: a second run prints `unchanged` for every step and restarts
  nothing. Every instance value comes from `host/kit.env`; the units come from `host/rendered/systemd/`. It refuses
  when either is missing or does not match `kit.toml`, on any host but `kit.toml` `host.name`, and outside
  `host.repo`. Step 3 installs `/etc/mc-host/kit.env` (root:<kit user> 640) before the `mc-*` scripts, which read
  their repo, user and wired connection from it. It prints the owner's remaining steps at the end.
  `sudo bash host/setup.sh --stand-up` (production stand-up only) also runs step 14, which enables
  `mc-server.service` + `mc-backup.timer`; without the flag setup never enables either.
- **`check.sh`** prints PASS/FAIL per property from live state (logind over D-Bus, `docker info`,
  `systemctl show`, `apt-config`, `sshd -T`). It exits 1 on any FAIL. WARN marks something owed by stand-up or
  by the owner. It FAILs, by key name only, on a key render owns in `.env` or `.env.bridge` that is missing or
  differs from `kit.toml` (`kit/render.py --check --env-only`).

**What it installs:**

| Piece | What | Disable |
|---|---|---|
| Never sleep | masks the five sleep targets; `/etc/systemd/logind.conf.d/10-mc-host.conf` sets lid, suspend and hibernate keys and `IdleAction` to `ignore` (logind is reloaded, never restarted) | `systemctl unmask sleep.target suspend.target hibernate.target hybrid-sleep.target suspend-then-hibernate.target`; delete the drop-in; `systemctl reload systemd-logind` |
| Network | `kit.toml` `net.mode`. `dhcp`, the default, leaves the wired address to the router and refuses (changes nothing) if the wired connection is still `manual`. `static`, only on request: `net.wired_conn` gets `net.static_addr`, commit-confirm style: it probes that the address is free (ping + neighbour lladdr, with the gateway as a positive control), arms `mc-net-rollback` (restores `/var/lib/mc-host/wired-ipv4.prev` after 180 s), applies, and disarms only on fresh evidence: 3 consecutive checks spanning ≥15 s, each seeing the address as the NIC's only IPv4 and the gateway answering a ping sourced from it and one bound to the wired NIC (Tailscale online is a sanity check). All of it happens no later than 20 s before the rollback fires, and the step reports FAILED, never confirmed, if the rollback started anyway. The Wi-Fi fallback is configured before this step, so it is up during the commit window. The wired connection autoconnects forever (priority 100, metric 100); Wi-Fi unblocked (Bluetooth stays blocked). The Wi-Fi is touched only when `net.wifi_conn` is set: with a wired connection it is the fallback (priority 10, metric 600), and on a Wi-Fi-only laptop it is the one link (autoconnect forever); either way it is configured only once its password is stored system-wide, so it comes up at boot with nobody logged in | back to DHCP: `nmcli connection modify "<net.wired_conn>" ipv4.method auto ipv4.addresses '' ipv4.gateway '' ipv4.dns ''` then `nmcli connection up "<net.wired_conn>"`; `nmcli connection modify "<net.wifi_conn>" connection.autoconnect no`; `nmcli radio wifi off` |
| `mc-netwatch.timer` | every 2 min from boot +5 min. It keeps two observations apart: **gateway** (the default gateway answers ping) and **Tailscale** (`Self.Online`, which means *connected to Tailscale's control plane*). Tailscale online = healthy. **Link fault** (gateway and Tailscale both down): ≥4 min `nmcli connection up` wired, ≥8 min restart `tailscaled`, ≥15 min reboot through `mc-maint --reboot` (player check applies), at most **one reboot per episode** and one per 6 h; a second reboot needs the gateway to have answered since. **Upstream fault** (gateway up, Tailscale offline: WAN or control-plane outage, where tunnels may still carry players): never a reboot or a link bounce; after ≥30 min one `tailscaled` restart, only if `mc-maint --idle` confirms nobody is playing. One alert on recovery (after any action, or after ≥30 min). A state file of the wrong shape is replaced by an empty one. State: `/var/lib/mc-host/netwatch.json` | `systemctl disable --now mc-netwatch.timer` |
| `mc-server.service` | `docker compose up -d minecraft` at boot, then `mc-bridge-up` (fence stamp, `timeout 60 … up -d --no-build bridge`, an mc-alert if it fails; with no Discord it exits 0 at once, starting nothing), whose failure is ignored, so the bridge can never stop Minecraft starting; `docker compose stop -t 120` at shutdown, **before** docker stops. Installed but **not enabled** until stand-up (`setup.sh --stand-up`, step 14: refused until `mc_data/<LEVEL>/level.dat`, the `dist/` pack compose names and a `backups/world-*.tar.gz` exist, and when an earlier step of that run aborted) | `systemctl disable mc-server.service` |
| Docker | `/etc/docker/daemon.json` `live-restore: true` (hot-reloaded, so no container restart); compose `stop_grace_period: 2m` | remove the key; `systemctl reload docker` |
| Update window | apt timer drop-ins (`/etc/systemd/system/apt-daily{,-upgrade}.timer.d/10-mc-host.conf`); `/etc/apt/apt.conf.d/52mc-host` adds the Tailscale repo to unattended-upgrades, blacklists every Docker package (Docker upgrades are manual, see below), and sets `Automatic-Reboot "false"`; `tailscale set --auto-update=false`; snap `refresh.timer=05:00-05:30` | delete the drop-ins (`daemon-reload`) or `52mc-host`; `tailscale set --auto-update=true`; `snap unset system refresh.timer` |
| `mc-maint.timer` | 05:40 daily. If `/var/run/reboot-required` exists, it reboots when Minecraft is verifiably stopped or RCON says 0 players. "Stopped" needs docker to answer, `minecraft-ai-host-minecraft-1` not running, **and** no `mc-server-runner` process on the host; a runner without a running container is an orphan (defer + alert). With players online it defers and alerts ("deferred: N online"), then tries again the next day. It also defers while `apt-daily-upgrade.service` or `mc-backup.service` is still running (ActiveState `activating` counts). If Docker, pgrep or RCON can't be read, it doesn't reboot and alerts. **Disk and bridge watch, every run:** an alert when mc_data's filesystem has under 20 GiB free, `mc_data/mcve/in` is over 50 MiB, or the bridge container is running but not healthy | `systemctl disable --now mc-maint.timer` |
| `mc-backup.timer` | the 04:00 backup as a system unit. **Disabled** until stand-up | `systemctl disable --now mc-backup.timer` |
| `mc-heal.timer` | every 2 min from boot +10 min, as the kit user (see "mc-heal" below) | `systemctl disable --now mc-heal.timer` |
| Alerts | `mc-alert "<msg>"` posts to the Discord webhook in `/etc/mc-host/webhook` (root:<kit user> 640, never in git; on a kit, `discord/bootstrap.py webhook` stages it and setup.sh step 7 installs it). `mc-alert@.service` is the `OnFailure=` target of mc-backup, mc-maint, mc-server, mc-netwatch, mc-heal, mc-bridge-fence and (kit hosts with a cap) mc-charge-cap. With no webhook file, alerts go only to the journal. With no Discord (`kit.toml` `discord.enabled = false`, so `MC_DISCORD="off"` in kit.env) alerts are off on purpose: mc-alert logs the alert with "alerts are off" and posts nothing, setup.sh step 7 owes nothing, and check.sh says so as an INFO line (it still FAILs a missing webhook with Discord). The callers log mc-alert's outcome (`mc-alert exit … posted (HTTP 204)` / `alert not sent: …`) in their own journal. **Mentions are off** (`allowed_mentions` empty), so an alert never pings anyone's phone; that, and an alert from off the laptop, is not built | delete the webhook file |
| Bridge fence | `mc-bridge-fence.service` (before docker; no ExecStop, so it is fail-closed): raw PREROUTING `-i br-mcve` drops to loopback, RFC 1918, 100.64/10 and 169.254/16, plus all v6. It writes `/run/mc-host/bridge-fence.ok`, which `mc-server.service` needs before it starts the bridge. Step 11 | stop the bridge, then `sudo /usr/local/sbin/mc-bridge-fence remove`; `systemctl disable mc-bridge-fence.service` |
| SSH | `/etc/ssh/sshd_config.d/10-mc-host.conf`: no passwords, no keyboard-interactive, no root. Refused if the kit user has no authorized key; checked with `sshd -t` before any reload | delete the file; `systemctl reload ssh` |
| Linger | `loginctl enable-linger` for the kit user (step 12) | `loginctl disable-linger <user>` |
| Charge cap | only with `kit.toml` `battery.charge_cap`: `mc-charge-cap.service`, a oneshot at every boot (asus-wmi resets the cap to 100 when it attaches), writes the cap to every `/sys/class/power_supply/*/charge_control_end_threshold` once the driver's file exists. It reports **"written", never "verified"**: drivers round the value and asus-wmi reads back its own cached copy. No file after 60 s, or a refused write, fails the unit (mc-alert). Step 13; with the cap removed from `kit.toml`, setup disables and deletes the unit | remove `[battery]` from `kit.toml`, render, `sudo bash host/setup.sh` |

**mc-heal (the hang watchdog, `host/bin/mc-heal`):**
- **Probe:** `docker exec <container> rcon-cli list`, answered with a player count within 30 s. RCON runs on the
  game thread, so it sees what friends see. The container is found by its compose labels (world.sh's filter).
- **Heal** after ≥3 consecutive failed probes spanning ≥6 min. A good probe ends the streak; so does a gap of
  more than 10 min since the last failure, or a new container start. The heal:
  1. saves `docker logs --tail 2000` and `mc_data/logs/latest.log` to `~/archive/heal/<ts>/` (the newest 20 are kept)
  2. `docker compose stop -t 120 minecraft`, the normal stop budget, so a JVM that can still save gets its chance
  3. `docker compose up -d minecraft`: never the bridge, never a bare `up -d`. It applies whatever compose says
     now, so set the maintenance flag while a compose edit (a chapter bump) is not yet applied.
  4. an mc-alert naming the streak and the outcome
- **Skips** (no probe, nothing counted, the history kept):
  - the maintenance flag `~/minecraft-ai-host/maintenance` exists (`deploy/world.sh maintenance [on <why> | off]`;
    a ring sets it itself)
  - under 10 min since boot, or since the container started
  - `mc-backup.service` running (backup.sh waits on RCON itself), or systemctl can't say
  - docker not answering
  - no running minecraft container: stopped or crash-looping is not "hung", so Docker's restart policy and
    mc-server own it
  - a heal in the last 30 min (cooldown)
- **Cap:** at most 2 heals per 6 h. Past that, one alert per streak: `mc-heal: cap reached, not restarting`.
- **Doctrine:** "no restarts while friends are online" holds, because nobody can be playing on a server whose RCON
  has been dead for 6 min.
- **State:** `/var/lib/mc-heal/state.json` (its own `StateDirectory`, owned by the kit user; a shared `mc-host` one
  would be chowned between this unit and the root ones). `mc-heal --status` prints it, and check.sh relays it.
- **Rehearse** on a spike compose project with `MC_REPO=<dir> MC_COMPOSE_PROJECT=<name>` (plus `MC_HEAL_STATE`,
  `MC_HEAL_ARCHIVE`). These are for tests and rehearsal only; the unit sets none of them.
- **Proven live on the dev machine (2026-10-02),** on a compose spike of the real pack (`top3-heal`), run by hand every
  2 min:
  - The JVM was SIGSTOPped, and `rcon-cli list` failed at its own 10 s i/o timeout.
  - The 4th failed check (7 min) healed. The stop took 2 min, because `STOP_DURATION` ran out and the runner
    killed the frozen JVM. `Done` came 18 s after the start.
  - The next 8 checks skipped: first the container was under 10 min old, then the cooldown applied. It healed
    exactly once.
  - **Cap:** with 2 heals already in the window (seeded), a second freeze got one `cap reached` alert at the 4th
    failure. There was no compose call, and the container was not restarted.
  - **Thawing a frozen JVM kills it.** On SIGCONT, Minecraft's own watchdog saw a 652 s tick and force-stopped
    the server, and Docker's restart policy brought it back. mc-heal skipped the new container. So the stop of a
    hung server is never a clean save.

**Daily timeline (local time):**
- 04:00 backup
- 04:30 apt lists
- 05:00 unattended-upgrades (Ubuntu + Tailscale; never Docker)
- 05:00–05:30 snap refresh window
- 05:40 `mc-maint`: reboot only if one is required and nobody is on

**Facts behind the design:**
- **What a shutdown really does to Minecraft.** Three things happen at once:
  - With live-restore, dockerd no longer stops containers when it stops.
  - `mc-server.service`'s `docker compose stop -t 120` runs while docker is still up.
  - The container's own `docker-<id>.scope` is stopped too, in parallel and not ordered after mc-server:
    systemd SIGTERMs every process in it, including the JVM, whose shutdown hook saves.
  - **The ceiling is the scope's `TimeoutStopSec`**, which defaults to 90 s.
    `/etc/systemd/system/docker-.scope.d/10-mc-host.conf` raises it to 180 s. Never order the scope
    `Before=mc-server.service`, which would deadlock startup.
  - **Inside the container, mc-server-runner SIGKILLs the JVM `STOP_DURATION` seconds after `stop`.** The image
    default is 60; compose sets 110.
  - **Budget order:** `STOP_DURATION` 110 < `stop_grace_period` / `stop -t` 120 < `TimeoutStopSec` 180. The
    runner, not Docker or systemd, ends the save.
  - Before stand-up `mc-server.service` is not enabled, so `mc-maint` itself runs `docker compose stop -t 120`
    before a reboot. The server then stays down after boot, which is fine for spike worlds.
- **`mc-server.service` uses `Wants=docker.service`, not `Requires=`.** `Requires=` also restarts the dependent
  unit whenever docker restarts, so a docker restart would bounce Minecraft; live-restore keeps it running
  instead. `After=` alone gives the shutdown order.
- **Docker is never upgraded unattended.** A docker-ce or containerd.io upgrade restarts dockerd, and
  live-restore is only supported across patch releases: after a minor or major upgrade dockerd may lose the
  running container. So 52mc-host leaves Docker's origin out and blacklists its packages.
  - `check.sh` reports pending Docker upgrades as INFO.
  - **A plain `apt upgrade` still upgrades Docker,** so run one only with the server stopped.
  - **Docker upgrade procedure (between play sessions, announced like a chapter):**
    1. `rcon-cli list`, and make sure nobody is on
    2. `deploy/backup.sh`
    3. `docker compose stop -t 120`
    4. `sudo apt-get update && sudo apt-get install --only-upgrade docker-ce docker-ce-cli
       docker-ce-rootless-extras containerd.io docker-buildx-plugin docker-compose-plugin`
    5. `docker compose up -d minecraft`, then `docker compose up -d --no-build bridge` (with Discord only: a kit
       with `discord.enabled = false` has no bridge), then `rcon-cli list` and `bash host/check.sh`
- **The Wi-Fi fallback works because of NetworkManager's connectivity check.** Ubuntu ships
  `20-connectivity-ubuntu.conf`, and a device without internet gets +20000 on its default-route metric, so Wi-Fi
  (600) takes over from a broken wired link that is still up (100).
- **With `net.mode = "static"`, address conflicts are prevented by a static address outside the router's DHCP pool**
  (`net.dhcp_pool` records it), for a router that can't reserve leases. NetworkManager's conflict detection
  (default `ipv4.dad-timeout` 200 ms) fails the activation if the address is taken, and Wi-Fi then carries the
  node. **If the router's pool is ever widened to include the address, conflicts come back.**
  - If another host takes the address later, NM drops it and re-probes, so the host flaps.
  - `check.sh` FAILs on a conflict line for the wired connection's current address in this boot's NetworkManager
    journal. Conflicts for other addresses, such as the old DHCP lease, are INFO.
- **Apply the static IP over Tailscale SSH, not the old LAN address.** The apply and confirm run as their own
  unit (`mc-net-apply`), so a dropped session doesn't matter. A reboot inside the 180 s window loses the
  transient rollback and keeps the new settings.
- **The Tailscale `Origins-Pattern`** comes from its repo's `Release` file (2026-09-29). It has `Codename: noble`
  and no `Suite`, so it matches on `codename=`:
  `origin=Tailscale,label=Tailscale,codename=${distro_codename},component=main`.
  - `check.sh` runs unattended-upgrades' own matcher against the host's configured apt sources.
  - It also runs u-u's own blacklist function over the Docker package names.

## The kit (a new host)

A new server on a fresh laptop is set up by the person's own Claude Code, following `docs/kit/KIT.md` (P0–P12, each
phase gated through `kit/record.py`); the person's side is `docs/kit/HUMAN-STEPS.md`. Each tool documents itself in
its header, and `kit/README.md` lists them by phase: `kit/probe.sh` (P0, read-only, refuses what the kit cannot run
on), `kit/sudoers.sh` (human step 7: the kit user's NOPASSWD drop-in, kept after launch, design decision, 2026-10-14),
`discord/bootstrap.py` (P1, P6, P12), `kit/secret.py` (human steps 12, 15, 18 and R1: tokens and the share link),
`kit/packages.sh` (P2), `kit/tailscale.py` (P3), `kit/render.py` (P4), `pack/spike.sh` (P5, a custom pack's test
boot; its `--pack`/`--world` rehearse a later pack change on a copy of the world), `host/setup.sh` (P7;
`--stand-up` at P10). None of them is for a server that is already running ("Updating a running server"), except
`kit/secret.py share-link`, which reads no kit.toml and is how any server rotates its share link. What is still open
is KIT.md's own table. The first end-to-end run, and what it changed: `docs/kit/2026-10-16_proof-run-findings.md`.
Discord is optional (operator ruling 2026-10-20): `kit.toml` `discord.enabled = false` skips P1, P6 and P11
(`kit/record.py skip`), drops human steps 9–16, turns alerts off, starts no bridge, and replaces P12's sync and invite
with `kit/friends.py` (the friend message, written to a mode-600 file whose path is all it prints) and a whitelist the
person asks for (HUMAN-STEPS R3). `discord/sync.py` and `discord/bootstrap.py` refuse on such a server. KIT.md "With
no Discord"; not run live yet.

## Verifying changes

- `docker compose config -q` (the compose file parses)
- `uv run --with pytest --with discord.py==2.7.1 pytest host/ pack/ discord/ bridge/ mod/ kit/`, and a clean
  `python3 pack/build.py`.
  - Without discord.py, `bridge/test_bridge.py` (the glue) skips. `test_core.py` and `test_contract.py` still
    run.
  - `test_contract.py` holds the bridge to `mod/contract-examples/` and FAILS if they are missing.
  - A test about the live server reads its record and skips, with the reason, where a copy dropped it (all of it:
    `kit/instance.py` decides, and part of a record fails the run instead of skipping).
- `bash kit/instance-check.sh` (in the repo that holds the record) before a copy of this repo goes to anyone: it
  drops `kit/INSTANCE-PATHS` from a copy of HEAD, runs the suite there (no failure, no error) and greps the copy for
  the instance's values (no hit).
- `docker build bridge/`
- `bash -n deploy/backup.sh`, and `shellcheck -x host/*.sh host/lib/*.sh host/bin/mc-charge-cap deploy/backup.sh
  kit/*.sh` (`uvx --from shellcheck-py shellcheck`)
- `systemd-analyze verify` on `host/rendered/systemd/*` (render first; render a `[battery]` config too, so
  `mc-charge-cap.service` is covered). On a machine whose Docker is the snap, use a temp `--root` holding a copy of
  `/usr/lib/systemd/system`, a stub `docker.service`, stub `/usr/local/sbin/mc-*` and stub
  `/usr/bin/{docker,bash,timeout}`
- The package phase and the probe: in a throwaway `ubuntu:24.04` container under `$HOME/spikes/` (no systemd, no
  NetworkManager, no Docker daemon there, so the probe refuses on NetworkManager and the package gate fails on
  `docker info`; a second package run must print only `unchanged` and call no apt)
- For Discord: dry-run → `--apply` → dry-run shows `0 actions`
- For the host: `sudo bash host/setup.sh` a second time prints only `unchanged`, then `bash host/check.sh`

Anything that crosses to the game host is verified **on the game host** (unit state, `docker ps`, `rcon-cli list`).
Green locally does not vouch for the host.
