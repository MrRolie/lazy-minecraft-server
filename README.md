# Vanilla Extended

An old laptop that was gathering dust now runs a modded Minecraft Java server for a group of about ten friends.
They join through a private network link and a Discord server set up for the group. The server looks after
itself: nightly backups, an early-morning update window, a restart when it hangs, and reboots only when nobody
is playing.

This is a public snapshot of a private repository, taken on 2026-10-14 from commit `9a9db5b`. The commit history
and the operational runbook stay private because they are full of home-network detail. The friends' Discord text
is replaced by a short example. Network names and addresses, the Wi-Fi name, the server address and the Discord IDs
are replaced by placeholders, and one test that read the runbook is removed. `AGENTS.md` (the runbook), `docs/`
(design briefs) and `discord/posts/`, which the code and `CLAUDE.md` refer to, stay private.

## Status

Open to friends since 2026-10-02. Per Mikael (2026-10-14), friends have joined and played, Discord messages
reach the game, a friend ran `/whitelist`, and `/discovery` or `/coords` have posted to Discord.

## How friends get in

The launch post's four steps (its estimate: about 10 minutes):

1. Install Tailscale and accept the share link.
2. Import the pack file into Prism or the Modrinth App.
3. Type `/whitelist <your Minecraft name>` in the Discord server.
4. Open Multiplayer: the server is already listed, from a `servers.dat` that `pack/build.py` writes into the pack.

## Discord

The bridge (`bridge/`) and a server-only mod (`mod/`) carry:

- game chat both ways with `#game-chat`, plus joins and leaves
- `/whitelist <name>`, a Discord slash command; the server looks the name up with Mojang and adds it
- `/discovery <note>` to `#discoveries` with its coordinates, and `/coords <name>` to `#coords`
- the online player count as the bot's presence
- ops alerts in `#ops-log` (every whitelist request, a restart without a clean stop, the game going silent),
  plus host alerts by webhook through `host/bin/mc-alert`

The guild's channels, topics and bot-posted copy are code: `discord/layout.toml`, applied by `discord/sync.py`
(dry run first, then `--apply`, then a re-run that must print `0 actions`).

## How it looks after itself

- **Never sleeps.** `host/setup.sh` masks the sleep targets; `host/conf/logind-10-mc-host.conf` ignores the lid.
- **Network watchdog.** `host/bin/mc-netwatch` checks the gateway and Tailscale every 2 minutes. With both down
  it brings the wired link up at 4 minutes, restarts Tailscale at 8 and reboots at 15, at most once per 6 hours.
  Wi-Fi is configured as a fallback.
- **Hang watchdog.** `host/bin/mc-heal` asks for the player list over RCON every 2 minutes. After 3 failed probes
  spanning at least 6 minutes it saves the logs, stops the server, starts it again and alerts.
- **Out of memory.** `docker-compose.yml` passes `-XX:+ExitOnOutOfMemoryError`, so the JVM exits and Docker
  restarts it.
- **Backups.** `deploy/backup.sh` runs at 04:00 (`host/systemd/mc-backup.timer`), flushes the world over RCON,
  checks the tar with `gzip -t` and keeps 7 days. A failed run posts a Discord alert.
- **Update window.** Unattended upgrades at 05:00 for Ubuntu and Tailscale; Docker is blacklisted
  (`host/conf/apt-52mc-host`).
- **Reboots only when nobody is online.** `host/bin/mc-maint` runs at 05:40 and reboots only if one is required
  and RCON reports 0 players or the server is stopped. Otherwise it defers and alerts.
- **Key-only SSH.** `host/conf/sshd-10-mc-host.conf`.

`host/setup.sh` installs the host side idempotently (the server unit and the backup timer are enabled by hand at
first start-up); `host/check.sh` prints PASS or FAIL per property.

## How access stays private

- No port forwarding. Friends reach the laptop through a Tailscale node share; they never join the tailnet.
- RCON is never published, and the whitelist is enforced.
- The bridge is its own least-capability Discord bot. It reaches the server only through files
  (`mod/CONTRACT.md`), on its own Docker network, and `host/bin/mc-bridge-fence` limits it to the public internet.
- Tokens, the share link and the webhook live in files outside git; the `.env*.example` files show their shape.

## Learned the hard way

- **The laptop dropped off the network for 8 hours.** Another device took its address, and Wi-Fi was switched
  off. Fix: a static address outside the router's DHCP pool, Wi-Fi unblocked as a fallback, and `mc-netwatch`.
- **The heap filled during world pre-generation.** A server paused for being empty never unloads the generated
  chunks, and the out-of-memory JVM then hung for 2 hours. Fix: `deploy/world.sh ring` refuses while pausing is
  on, and `ExitOnOutOfMemoryError`.
- **The healthcheck said unhealthy while the server was fine.** Fix: `mc-heal` judges liveness by an RCON answer.
- **In a test, a clean shutdown took 10.2 s against a 10 s kill timer.** Docker kills a container 10 s after `stop` by
  default. Fix: the image's runner gets 110 s, Docker 120 s and systemd 180 s (`docker-compose.yml`,
  `host/systemd/`).
- **A named volume silently broke log reads** in an earlier version. Fix: the data is a bind mount,
  `./mc_data:/data`.

## The pack

Minecraft 26.1.2 on Fabric loader 0.19.5. The launch pack runs 35 mods on the server, counted from `pack/mods.toml`
(chapter 1: 18 server-only and 17 on both sides; 3 more are client-only). The pack is code: `mods.toml` lists the mods, `lock.json` pins exact
versions, and `build.py` builds the `.mrpack`. `ve-commands` in `mod/` is a server-only Fabric mod that adds
`/discovery` and `/coords` and talks to the bridge only through the files `mod/CONTRACT.md` defines.

## Repository layout

| Path | What |
|---|---|
| `docker-compose.yml` | The server and the bridge |
| `bridge/` | The Discord bridge |
| `mod/` | `ve-commands` and its contract |
| `pack/` | The pack as code |
| `discord/` | The guild layout, `sync.py` and example content |
| `host/` | Laptop maintenance as code |
| `deploy/` | Backups and world admin |
| `CLAUDE.md` | The rules the agent works under |

## Not a kit

It is written for this one laptop: the host name and paths are fixed in the scripts and units, and the LAN values
are examples to set, so using it elsewhere means changing those. `host/setup.sh` refuses to run on any host but `asus-node` unless
`MC_HOST_FORCE=1` is set.

## Tests

```sh
uv run -q --with pytest --with discord.py==2.7.1 pytest -q -p no:cacheprovider host/ pack/ discord/ bridge/ mod/
```

721 tests pass on this snapshot (2026-10-14).

## How it was built

Built with Claude Code under Mikael Hillman-Pépin's direction. He set the goals and the rules the agent works
under; `CLAUDE.md` is that doctrine. Claude wrote the code, the tests and the docs. Changes are checked by the test
suite and by live proofs on the laptop or on a test copy on a workstation. There is no line-by-line human review. Comments
that mention "the reviewer" or review rounds (rv1 to rv3) refer to review passes by separate AI sessions; his part
was deciding the trade-offs they raised.

## History

It began as an AI "dungeon master" that designed weekend events on a pack of about 370 mods. That version was
retired on 2026-09-28 in favour of a lighter pack, and its code is not in this copy.

## Licence

MIT. See `LICENSE`.
