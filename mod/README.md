# mod/ — `ve-commands`, the server-only Fabric mod

`ve-commands` is the server half of the Discord bridge. It is a small Fabric mod (MC 26.1.2, Fabric loader 0.19.5,
Fabric API 0.155.3+26.1.2, Java 25) that:

- gives every player `/discovery <text>` (1–200 chars) and `/coords <name>` (1–60 chars), with a 10 s cooldown
  per player per command
- caps the vanilla locator bar to friends within 200 blocks and gives every player `/hide` and `/here` ("Locator
  bar" below)
- writes game events to `<server dir>/mcve/out/events.jsonl` and acts on `<server dir>/mcve/in/inbox.jsonl`

The bridge and the mod talk **only** through those files. `out/` is the mod's and `in/` is the bridge's; the
bridge mounts `out/` read-only. The contract is **`mod/CONTRACT.md`**, its one home.
Change it there first, then change both sides.

The mod is `"environment": "server"`. Vanilla clients need nothing. The commands use only the vanilla brigadier
greedy-string argument (or none), so they reach clients through the server's normal command tree.

## Build

The build runs inside Docker, so the build machine needs no JDK 25. The Gradle home sits under `$HOME`, because snap Docker
cannot bind-mount `/tmp`.

```sh
mkdir -p "$HOME/.cache/ve-commands-gradle"   # else Docker creates it root-owned and Gradle cannot write
docker run --rm -u "$(id -u):$(id -g)" \
  -e GRADLE_USER_HOME=/gradle-home -e HOME=/gradle-home \
  -v "$HOME/.cache/ve-commands-gradle:/gradle-home" \
  -v "$PWD/mod:/src" -w /src/ve-commands \
  eclipse-temurin:25-jdk ./gradlew --no-daemon build
# -> mod/ve-commands/build/libs/ve-commands-<version>.jar  (about 50 KB); `build` also runs the unit tests,
#    which read mod/contract-examples/ (hence the whole mod/ mount)
```

**`bash mod/build.sh`** wraps this for a host (kit phase P5): it refuses unless `gradle.properties`, the pin in
`mod/RELEASE.sha256` and compose's `MODS` name the same jar, builds, checks the jar's sha256 against the pin, and
installs it in `extras/`. It never deletes or replaces anything in `extras/` (older jars stay for rollback; a
same-named jar with other bytes is a refusal), and a jar already there with the pinned sha is left alone without a
build. `--check` runs only the checks.

- **Template:** the official `fabric-example-mod` 26.1.2 branch, with one change: Loom is pinned to the release
  `1.18.2` instead of `1.18-SNAPSHOT`.
- **No mappings:** 26.1+ ships unobfuscated, so there are no mappings and no remap step. The `jar` output is the
  mod.
- **Reproducible:** archive timestamps are fixed and entry order is stable, so a clean rebuild is byte-identical.
  The released jar's sha256 is pinned in **`mod/RELEASE.sha256`**, and `host/check.sh` checks the deployed copy
  against it.

## Deploy (itzg `MODS`, proven on a spike 2026-10-01)

The jar is not on Modrinth, so it stays **out of the `.mrpack`**:

- `pack/build.py` fail-closes on `overrides/mods`.
- The lock pins Modrinth artifacts only.

Instead, compose hands it to itzg (`docker-compose.yml`, minecraft service): `MODS:
"/extras/ve-commands-<version>.jar"` and the volume `./extras:/extras:ro`. `extras/` is gitignored; it holds the
built jar, rebuilt from source.

At every start, after the Modrinth pack is processed, itzg's `mc-image-helper mcopy` copies the `MODS` list into
`/data/mods`. It records what it copied in `/data/mods/.var-list-manifest.json`.

**Proven live on the spike:**

| Action | Result |
|---|---|
| Plain restart (container config unchanged) | The jar is kept and loaded (`Loading 84 mods … ve-commands 1.0.0`) |
| `touch` the `.mrpack`, then restart | itzg logs a new pack `versionId` (the new mtime) and re-processes the pack; the jar is still loaded |
| Point `MODS` at a new filename | The new jar is copied, and the old one is removed as `Removing old file ve-commands.jar` |
| `docker compose restart` after a `MODS` change | **Nothing changes** (rv2, ops): restart keeps the old container, its old `MODS` and its old mounts. Only `docker compose up -d minecraft` recreates it |
| `MODS` names a jar missing from `extras/` | **Minecraft does not start:** `mcopy` errors (`Source file … does not exist`) and the container exits 2, a crash loop under `restart: unless-stopped`. The old jar and its manifest are left untouched |

The Modrinth installer never deletes the jar, because the jar is not in its manifest. `mcopy` runs after the
installer on every boot, so a pack bump cannot lose it.

`host/check.sh` therefore asserts, through `mod/check_extras.py`, that the effective compose config
(`docker compose config --format json`) binds `<repo>/extras` at `/extras` **read-only**, and three things about
every `MODS` jar:
- it has a pin in `mod/RELEASE.sha256`, and it is a regular file in `extras/` that uid 1000 (itzg) can read
- it matches `mod/RELEASE.sha256`
- after a boot, the copy in `mc_data/mods/` matches too, and it is the only ve-commands jar there

The check fails closed: a crash, no output, or a non-zero exit is a FAIL.

**Always use a new filename for a new build.** `mcopy` decides "up to date" **by size alone** (`Skipping existing
… since it has same size as source`). A rebuilt jar of the same size under the same name would silently not
deploy. With a new name, managed cleanup swaps the old copy out, so two copies of the mod id never load.

**Release (in one commit):**
1. Bump `version` in `mod/ve-commands/gradle.properties`.
2. Build.
3. Write `<sha256>  ve-commands-<version>.jar` to `mod/RELEASE.sha256`.
4. Change the `MODS` line in `docker-compose.yml` to the new name. `mod/test_check_extras.py` fails if the pin and
   compose disagree.

**Deploy on the game host:**
1. Run `mkdir -p extras` as the kit user, so Docker does not create it as root.
2. Copy the jar into `extras/` under its versioned name.
3. Bring the commit over (AGENTS.md "Updating a running server").
4. Run `bash host/check.sh`; the extras line must PASS.
5. Run `deploy/backup.sh`.
6. Confirm with `rcon-cli list` that nobody is on, then apply it between play sessions with
   **`docker compose up -d minecraft`** (it is a chapter-style change). Never use `docker compose restart`: it
   keeps the old container, so the new `MODS` line never takes effect.
7. Run `bash host/check.sh` again. The extras lines now also check the copy in `mc_data/mods/`: it must match
   the pin, and it must be the only ve-commands jar there.

**Backups:** the jar is not in `backup.sh`'s pack record. It does not affect world generation. The record of the
jar is its git commit plus `mod/RELEASE.sha256`.

## What it does on the server

- **Threads:** three daemon threads, one job each:
  - `ve-commands-events` appends event lines
  - `ve-commands-inbox` polls the inbox (1 s) and does the Mojang lookups
  - `ve-commands-status` runs the 60 s heartbeat

  The inbox and status jobs run on `Ticker`s, which catch any throw, an `Error` included, and keep their
  schedule.
- **Inbox limits:** the bridge is untrusted. Each poll handles at most 200 lines or 64 KiB (so a crash replays
  at most one poll's budget), and a backlog over 1 MiB is skipped. A shrunk inbox is re-read from 0 at most once
  a minute. `say` is limited to 5/s, and `whitelist_add` to 10/hour. Lines must be strict JSON. Every
  inbox-driven log line is throttled to one a minute (CONTRACT.md "Security boundary").
- **Main thread:** world-facing steps are scheduled with `server.execute`: the broadcast, the whitelist add, and
  reading the player list.
- **Mojang lookup:** the profile lookup for `whitelist_add` (an HTTP call) runs on the inbox thread, never on a
  tick.
- **File I/O:** all of it is confined to `<server dir>/mcve/out/` (writes) and `mcve/in/inbox.jsonl` (read
  only). No path is followed through a link (CONTRACT.md "Security boundary").
- **Rotation override (testing only):** `-Dvecommands.rotateBytes=<n>` replaces the 5 MiB threshold. With itzg,
  set `JVM_DD_OPTS=vecommands.rotateBytes:<n>`.

## Locator bar (1.0.5; option a2, ruled 2026-11-03)

The game rule `locator_bar` is on (`deploy/world.sh init`). Vanilla shows a friend's dot while the distance is under
min(their `minecraft:waypoint_transmit_range`, your `minecraft:waypoint_receive_range`); both default to 6.0E7 on a
player (MC 26.1.2 bytecode: `Attributes`, `Player.createAttributes`, `WaypointTransmitter.doesSourceIgnoreReceiver`).
The code is `Locator` (the rules, MC-free) and `LocatorBar` (on a ServerPlayer).

- **The cap:** on every join, and on the new player after every respawn (death or End exit; Fabric
  `ServerPlayerEvents.AFTER_RESPAWN`), a transient `ADD_VALUE` modifier `ve-commands:receive_cap` brings receive
  range down to **200 blocks** (`Locator.RECEIVE_RANGE_BLOCKS`, with its reason). It never raises a lower base.
  `-Dvecommands.locatorRange=<blocks>` (itzg: `JVM_DD_OPTS=vecommands.locatorRange:<blocks>`) overrides it for a
  spike; a value outside (0, 6.0E7] is ignored. The server log says the range at start. The cap is an offset on
  the base value, worked out at join or respawn, not a clamp on the final value: another modifier on receive range
  (vanilla and the pack add none today) would add to it, and an admin's `/attribute … base set` takes effect at that
  player's next join or respawn.
- **`/hide`:** toggles the entity tag `ve_hidden` and a transient ×0 modifier `ve-commands:hide` on your own transmit
  range (the shape of vanilla's crouch modifier). It never touches receive, so you still see friends. Vanilla saves
  tags in playerdata and copies them on every respawn, so `/hide` holds through deaths, relogs and restarts until
  you type it again; the join and respawn hook re-applies it from the tag. It hides your dot, not where you are:
  `/here` still posts your coordinates, and the `/hide` reply says so.
- **`/here`:** one chat line, `📍 <name> is at <x> <y> <z> in the Overworld`, sent to each player online (you
  included), with a 10 s cooldown. Sent player by player, never broadcast, so it stays in the game: Fabric's
  `GAME_MESSAGE` (the bridge's `game` event) fires on `broadcastSystemMessage` only. It emits no event.
- **Players, never places:** the code touches only a player's own two waypoint attributes. It never spawns or
  tags another entity, so no place can transmit a dot (`LocatorWiringTest` fails if it ever does).
- **Nothing in playerdata but the tag:** `AttributeInstance.pack()` saves the base value and permanent modifiers
  only, and both modifiers here are transient. A failure to apply is logged (`locator cap not applied to …`) and
  never breaks the join.
- **Known window:** a joining or respawning player is placed with vanilla's range first; the cap lands on the next
  tick (the attribute update re-adds the player to the level's waypoint manager, which drops the far
  connections). A far dot can show for about one tick.
- **Spectators see every dot:** vanilla's range check starts by never ignoring a spectator receiver
  (`WaypointTransmitter.doesSourceIgnoreReceiver`), so an op in spectator mode sees every transmitting player at
  any distance. A hidden player transmits nothing (transmit range 0 stops vanilla tracking it), so stays off even
  that bar.
- **Dimensions:** waypoints are per level (`ServerLevel.getWaypointManager`), so a friend in another dimension
  never shows. A portal keeps the same ServerPlayer (`ServerPlayer.teleport`), so the modifiers ride along.
- **Taking it out:** the release is one commit (code, tests, pin, `MODS`, this section), and the cap leaves
  nothing in playerdata to reset. In order:
  1. Tell the friends first (`#announcements`): anyone who used `/hide` shows on the bar again at their next join.
  2. One commit: `git revert` the release commit (its SHA is in the instance record, under "Taking the cap out" — a private record, not in this copy;
     deploy per AGENTS.md "Updating a running server"). It restores the 1.0.4 code, pin and `MODS` line and removes this
     section; the 1.0.4 jar is kept in `extras/`), plus the docs outside `mod/` that describe the cap: the
     `deploy/world.sh` init comment on the `locator_bar` line, AGENTS.md's `mod/` row, design §7,
     the original server's design note (private, not in this copy), the
     `docs/kit/KIT.md` P9 note, and the instance record's deployed-version line. Decide the game rule in the same
     commit: the bar on uncapped (option a1), or `gamerule minecraft:locator_bar false` on the live world and
     `false` in `world.sh init` again.
  3. "Deploy on the game host". Every player is uncapped at their next join. `/hide` users keep an inert
     `ve_hidden` tag; to clear it for players online, `rcon-cli "tag @a remove ve_hidden"`.

## Tests

`./gradlew build` runs the unit tests (JUnit 5). All are MC-free but `LocatorBarTest`, which bootstraps Minecraft's
registries (no server, no world):

| Test | Covers |
|---|---|
| `TextTest` | Formatting, control-character, bidi and blank-filler stripping, whitespace collapse, log escaping (`forLog`, DEL and C1 included), code-point truncation, the name regex |
| `CooldownsTest` | The cooldown |
| `EventLogTest` | The event envelope, one line per event, rotation keeping one old file, async drain on close |
| `InboxTest` | Both verbs, offset persistence across restarts, start at the end with no usable offset, partial lines, sanitizing and clamping, a shrunk inbox, an over-long line, the per-poll line and byte budgets with the offset saved per poll, a backlog over 1 MiB skipped |
| `LimitsTest` | The say rate limit, the whitelist_add cap with its `rate limited` answer, the id rule, strict JSON, the token bucket, the log throttle |
| `LogFloodTest` | No log amplification: skipped lines, repeated shrinks (one reset to 0 a minute), refused whitelist_adds, backlog skips, dropped says, read failures and a path flipping between link and file each give one line a minute; inbox strings are escaped at the call sites |
| `TickerTest` | A job that throws (an `Error` too) keeps its schedule; a blocked job never delays another Ticker (the status beat) |
| `ContractExamplesTest` | `mod/contract-examples/`: every event line rebuilt byte for byte, and the example inbox yields exactly `inbox.expected.jsonl` |
| `NoFollowRaceTest` | A link swapped in after the lstat check passed (the inbox, the offset, the offset tmp, `events.jsonl`) is still refused by the `O_NOFOLLOW` open |
| `McveDirsTest` | A symlinked `mcve/` is refused before `out/` and `in/` are created through it |
| `JobsTest` | A blocked inbox job never stops the status beat (they are separate threads), and VeCommands' call site (pinned from its source) passes the beat as its own job |
| `LocatorTest` | The cap is exactly the range on a vanilla player and never raises a lower base; it is in the a2 band; a respawn (a new player with the base values and tags only) gets the cap and `/hide` back; `/hide` flips transmit and the tag and makes no receive call; a tagged player rejoins hidden; the spike override takes only a range in (0, 6.0E7]; the chat lines |
| `LocatorBarTest` | `LocatorBar.Waypoints` on Minecraft's own `AttributeInstance` (bootstrapped, no server): the cap makes receive exactly 200 and transmit untouched, a lower base is left alone and a stale cap removed, `/hide` makes transmit 0 (with any base, alongside sneaking) and back with the modifier and the tag gone, a respawned player gets both back, `pack()` saves no modifier; and on the real command tree a friend without op can run `/hide`, `/here`, `/coords` and `/discovery` |
| `LocatorWiringTest` | Pinned from source: the cap goes on at join and on the respawned (new) player, nowhere else; `/hide` touches transmit only and the cap receive only; `LocatorBar.of` hands `Waypoints` the player's own two ranges and tag calls (the one line that needs a ServerPlayer, pinned whole); no `setBaseValue` or permanent modifier anywhere; no entity spawning or entity types anywhere, and only `LocatorBar` touches transmit; `/here` sends to each player online, never broadcasts, emits nothing, has its cooldown |
| `SymlinkTest` | Planted links for `events.jsonl`, `events.jsonl.1`, `inbox.offset(.tmp)`, `inbox.jsonl`, and linked `in/` and `out/`, plus a fifo inbox (with a 5 s bound, so a regression fails rather than hangs): all refused, the world stand-ins left byte-identical |

`mod/test_check_extras.py` (pytest) covers the deploy check and its `host/check.sh` wiring, which it runs. The spike evidence is in the private repository's commit
history, which is not part of this snapshot.
