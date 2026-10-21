# Lazy Minecraft Server

Laziness here is engineered.

A modded Minecraft server for you and your friends, on an old laptop you already own. Claude Code does the setup:
your Discord server's channels (Discord is optional), the private network, the Minecraft server itself, backups and self-repair. You do the handful
of steps only a person can do, and then you play.

## Start here

These six steps come before Claude can help. Every click is in [docs/kit/HUMAN-STEPS.md](docs/kit/HUMAN-STEPS.md).

1. On another computer, write **Ubuntu Desktop 24.04 LTS or newer** to a USB stick of 12 GB or more. 24.04 is the
   tested version; newer should work but is untested; 22.04 or older won't work.
2. Install it on the laptop. The account you create is the one the server runs as, so make it the laptop's first
   account.
3. (Optional) In the laptop's BIOS, turn on power-on after a power cut, if it has that setting.
4. Get a Claude Pro or Max plan at claude.ai.
5. On the laptop, open a terminal (Ctrl+Alt+T) and paste:
   ```sh
   sudo apt-get update && sudo apt-get install -y curl git && curl -fsSL https://claude.ai/install.sh | bash
   ```
6. Run `claude`, log in, and tell it: "set up the Minecraft friend server kit from
   https://github.com/MrRolie/lazy-minecraft-server".

From there Claude takes over, and tells you each time it needs you.

## Who it's for

Part-time players who want a server for their group without spending their free evenings running it.

## What you do, what Claude does

**You, once:** about 20 steps (19 if you already have Discord, 12 with no Discord). Most of them are accounts (a
Claude plan, Tailscale, and Discord if you want it), installing Ubuntu, logging Claude in, signing in to Tailscale,
and choosing the game; with Discord, also creating the two Discord bots and clicking their invites. A Wi-Fi password
or a fixed address adds a step, only if your setup needs one.

**Each friend, once:** 10 steps (9 with no Discord): own Minecraft Java Edition, join your Discord (only with
Discord), install Tailscale and accept the share, install a launcher, import the pack, whitelist themselves (with no
Discord, they send you their Minecraft name and Claude adds them).

**You, now and then:** 2 steps (3 with no Discord): a fresh share link for a late friend, and logging Claude Code
back in when asked; with no Discord, also adding each new friend to the server by their Minecraft name.

Every step, and where to click: [docs/kit/HUMAN-STEPS.md](docs/kit/HUMAN-STEPS.md). The first six are under
"Start here" above.

**Claude does the rest,** and tells you when it needs you. It checks the laptop and stops with a plain reason if
the kit can't run there. It installs Docker and Tailscale and sets up friend access. With Discord, it builds your
Discord server's channels, the chat bridge between the game and Discord, and an alerts channel. With no Discord, it
writes the message you send your friends, and alerts are off: the server still repairs itself, but nobody is told
when something fails. It builds the pack, sets the laptop up to run unattended, starts Minecraft and pre-generates
the world. Then it restarts the laptop once to prove everything comes back by itself.

Claude never creates accounts or types passwords. Bot tokens go through a hidden prompt you run in a second terminal
window, into files only your account can read.

## Pick the experience

**Vanilla Extended** is the default: Minecraft as if Mojang had kept adding structures, bosses and places to
explore, and it still feels like Minecraft.

Or tell your Claude the experience you want, or a pack you already know. It researches and assembles one from
Modrinth (Modrinth only), sized to your laptop's memory, on Vanilla Extended's Minecraft version so
the Discord bridge still works. The pack must pass the same build checks and a recorded test boot
([docs/kit/CUSTOM-PACK.md](docs/kit/CUSTOM-PACK.md)). That proves it builds and boots. Whether it is fun is for
your group to find out by playing. A friend ran this custom path on 2026-10-20 and reported it went smoothly; it
found one defect (`#welcome` showed Vanilla Extended's text), fixed in this snapshot; its run record is pending.

## What you need

- **An old laptop with 16 GB of RAM**, an SSD with **50 GB free**, and a network cable to your router (Wi-Fi alone
  is supported but not yet tested). 16 GB is the only size proven so far. Below that, Claude tells you and lets you decide.
- **Ubuntu Desktop 24.04 LTS or newer** for the laptop (you install it in steps 1 and 2). 24.04 is the tested version;
  newer should work but is untested. Desktop, not Server, and not 22.04 or older: the kit stops on those.
- **A USB stick of 12 GB or more**, and another computer to make it on. The account you make on the laptop must be
  its first.
- **A Claude plan that includes Claude Code** (Pro or Max).
- **A Tailscale account**, and **a Discord account, only if you want Discord.**
- **Friends** with Minecraft Java Edition, and Prism Launcher or the Modrinth App.
- The laptop stays plugged in and switched on; closing the lid is fine.

## How long it took, once

One recorded run, on a fresh virtual machine (Ubuntu 24.04, 16 GB, 4 vCPU, wired), not on a laptop:

- Installing Ubuntu and Claude Code (steps 1 to 6): about 15 min.
- The Claude-driven setup: about 1 h 55 min. Of that, Discord with the person clicking took 14.6 min, Tailscale
  12.4 min, and world pre-generation 1 h 07 min, stopped at 46%.

A full pre-generation would have added about 2 to 3 hours there, unattended. On the original laptop it took about
7.7 hours.

## Proven so far, and not yet

**The original server.** It runs on an old ASUS laptop (i5-7200U, 2 cores / 4 threads, 15 GiB RAM) for a group of
friends, open since 2026-10-02. It runs the version the kit was generalized from, not the kit itself.

**The kit.** Its fresh setup is proven once, on the virtual machine above: every phase ran, Discord-to-game chat
and `/whitelist` worked, and the test restart came back on its own. What that run found is fixed (in code with tests, or in
the docs), except one open finding (below), and no run has gone through those fixes yet.

**Not yet:**
- No kit run on a real old laptop, so the Wi-Fi fallback, a fixed address, the battery charge cap and the BIOS
  power-on setting are untested.
- The run stopped world pre-generation early.
- The custom-pack path: a friend ran it on 2026-10-20 and reported it went smoothly; its run record is pending. The
  defect it found, `#welcome` showing Vanilla Extended's text, is fixed in this snapshot; no run has gone through
  that fix yet.
- The no-Discord path has not been run yet, on a laptop or on the virtual machine; its step count is counted from
  the step list, not timed.
- Game-to-Discord chat was not checked by eye in the run (Discord-to-game was).
- One open finding: the Nether and End borders reverted once after a cancel and a restart. A gate caught it; it
  has not been reproduced.

## How it stays private

- **No port forwarding.** Friends reach the server through a Tailscale share of the laptop and never join your
  Tailscale network.
- **RCON is never published,** and only whitelisted players can join.
- **The chat bridge is its own, weaker Discord bot.** It talks to the server only through files
  (`mod/CONTRACT.md`), on its own Docker network, and `host/bin/mc-bridge-fence` blocks it from the laptop, your
  home network and the tailnet.
- **Tokens, the share link and the alert webhook stay in files outside git** (`.gitignore`). `.env.example` and
  `.env.bridge.example` show their shape.

## How it looks after itself

- **Never sleeps.** `host/setup.sh` masks the sleep targets; `host/conf/logind-10-mc-host.conf` ignores the lid.
- **Network watchdog.** `host/bin/mc-netwatch` checks the router and Tailscale every 2 minutes. With both
  unreachable it brings the link up at 4 minutes, restarts Tailscale at 8 and reboots at 15 (at most once per 6 h).
- **Hang watchdog.** `host/bin/mc-heal` asks the server for its player list every 2 minutes and restarts it after
  3 failed checks spanning at least 6 minutes, then alerts.
- **Out of memory.** The server exits and Docker restarts it (`-XX:+ExitOnOutOfMemoryError`, `docker-compose.yml`).
- **Nightly backups.** `deploy/backup.sh` at 04:00 (`host/systemd/mc-backup.timer.in`): flushes the world, checks
  the archive, keeps 7 days. A failed run posts a Discord alert (`host/bin/mc-alert`); with no Discord, alerts
  are off.
- **Reboots only when nobody is online.** `host/bin/mc-maint` at 05:40 reboots only if one is needed and nobody is
  playing; otherwise it waits and alerts.
- **Update window.** Ubuntu and Tailscale update unattended at 05:00; Docker never does (`host/conf/apt-52mc-host`).
- **Battery charge cap,** where the hardware allows it: `host/bin/mc-charge-cap` at every boot.

## Repository layout

| Path | What |
|---|---|
| `kit/` | The kit's tools |
| `docs/kit/` | Claude's procedure, your steps, custom packs, the test run's findings |
| `docker-compose.yml` | The server and the chat bridge |
| `pack/` | The pack as code, and its test boot |
| `mod/` | The server-side mod the bridge talks through |
| `bridge/` | The Discord chat bridge |
| `discord/` | The Discord server's layout as code |
| `host/` | Laptop maintenance as code |
| `deploy/` | Backups and world admin |
| `AGENTS.md` | The runbook |
| `CLAUDE.md` | The rules the agent works under |

## Tests

```sh
uv run --with pytest --with discord.py==2.7.1 pytest
```

On this snapshot (2026-10-20): 1825 pass and 54 skip. Every skip says why: it needs the original server's
private record, which is not in this copy. On a machine without Python 3.10, two more tests skip, also with their
reason. Some tests read git history, so run them in a clone, not a bare download.

## How it was built

Built with Claude Code under Mikael Hillman-Pépin's direction. He set the goals and the rules the agent works
under (`CLAUDE.md`); Claude wrote the code, the tests and the docs. Changes are checked by the test suite, by live
proofs on the laptop or on a test machine, and by review passes from separate AI sessions, including a cross-model
review. There is no line-by-line human review. Comments that mention "the reviewer", review rounds (rv2, rv3)
or a "codex review" mean those AI passes.

## This snapshot

A public snapshot of a private repository, taken 2026-10-20 from commit `7f3806f`. Dropped, exactly as
`kit/INSTANCE-PATHS` lists: the original server's instance record, its friends' Discord text and posts, and Mikael's
working notes. A few lines elsewhere were edited where they pointed at those files or at private context, and this
README and the LICENSE are new. The history stays private.

## History

It began as an AI "dungeon master" for weekend events on a pack of about 370 mods, retired on 2026-09-28 for the
Vanilla Extended friend server (open since 2026-10-02). The kit was generalized from that server in October 2026.

## Licence

MIT. See [LICENSE](LICENSE).
