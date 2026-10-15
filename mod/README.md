# mod/ — `ve-commands`, the server-only Fabric mod

`ve-commands` is the server half of the Discord bridge. It is a small Fabric mod (MC 26.1.2, Fabric loader 0.19.5,
Fabric API 0.155.3+26.1.2, Java 25) that:

- gives every player `/discovery <text>` (1–200 chars) and `/coords <name>` (1–60 chars), with a 10 s cooldown
  per player per command
- writes game events to `<server dir>/mcve/out/events.jsonl` and acts on `<server dir>/mcve/in/inbox.jsonl`

The bridge and the mod talk **only** through those files. `out/` is the mod's and `in/` is the bridge's; the
bridge mounts `out/` read-only. The contract is **`mod/CONTRACT.md`**, its one home.
Change it there first, then change both sides.

The mod is `"environment": "server"`. Vanilla clients need nothing. Both commands use only the vanilla brigadier
greedy-string argument, so they reach clients through the server's normal command tree.

## Build

The build runs inside Docker, so the workstation needs no JDK 25. The Gradle home sits under `$HOME`, because snap Docker
cannot bind-mount `/tmp`.

```sh
mkdir -p "$HOME/.cache/ve-commands-gradle"   # else Docker creates it root-owned and Gradle cannot write
docker run --rm -u "$(id -u):$(id -g)" \
  -e GRADLE_USER_HOME=/gradle-home -e HOME=/gradle-home \
  -v "$HOME/.cache/ve-commands-gradle:/gradle-home" \
  -v "$PWD/mod:/src" -w /src/ve-commands \
  eclipse-temurin:25-jdk ./gradlew --no-daemon build
# -> mod/ve-commands/build/libs/ve-commands-<version>.jar  (about 34 KB); `build` also runs the unit tests,
#    which read mod/contract-examples/ (hence the whole mod/ mount)
```

- **Template:** the official `fabric-example-mod` 26.1.2 branch, with one change: Loom is pinned to the release
  `1.18.2` instead of `1.18-SNAPSHOT`.
- **No mappings:** 26.1+ ships unobfuscated, so there are no mappings and no remap step. The `jar` output is the
  mod.
- **Reproducible:** archive timestamps are fixed and entry order is stable, so a clean rebuild is byte-identical.
  The released jar's sha256 is pinned in **`mod/RELEASE.sha256`**, and `host/check.sh` checks the deployed copy
  against it.

## Deploy (itzg `MODS`, proven on the workstation spike 2026-10-01)

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

**Deploy on asus-node:**
1. Run `mkdir -p extras` as mm-mike, so Docker does not create it as root.
2. Copy the jar into `extras/` under its versioned name.
3. Bring the commit over with the usual git bundle.
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

## Tests

`./gradlew build` runs the MC-free unit tests (JUnit 5):

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
| `SymlinkTest` | Planted links for `events.jsonl`, `events.jsonl.1`, `inbox.offset(.tmp)`, `inbox.jsonl`, and linked `in/` and `out/`, plus a fifo inbox (with a 5 s bound, so a regression fails rather than hangs): all refused, the world stand-ins left byte-identical |

`mod/test_check_extras.py` (pytest) covers the deploy check and its `host/check.sh` wiring, which it runs. The spike evidence is in the private repository's commit messages.
