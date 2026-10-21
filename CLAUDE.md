# CLAUDE.md — friend server doctrine

`AGENTS.md` is the operational source of truth (host, runbook, facts). This file is the doctrine that must not
be eroded.

## What this is

A private Minecraft Java server for about 10 friends who have played vanilla together for years. The goal is
**Vanilla Extended**: Minecraft as if Mojang had kept shipping cohesive exploration, structure, boss and world
updates. More reasons to stay invested in one world after the Dragon and Wither, and it still *feels like
Minecraft*. Optimize for cohesion, discovery,
longevity, multiplayer fun and **low cognitive overhead**, not mod count.

This is recreation. The owner's time is the scarce input, so the server
should run itself.

**Which host:** this doctrine binds every instance of this repo. The live friends' server is one instance, and its
host, address and guild are its instance record (listed in `kit/INSTANCE-PATHS`), which is the original server's
private record and is not in this copy. A server
set up with the kit (`docs/kit/KIT.md`) is another; its record is its own `kit.toml`. Below, "the game host" means
the instance's host.

## The world is the asset

- **The friends' world is irreplaceable, so it is never deleted.** Retire it by **moving** it to an archive
  (`~/archive/<date>_<name>/`) with a MANIFEST.
- **Before any server-side change:** run a fresh `deploy/backup.sh`, then make the change. This covers a pack
  update, a mod add or remove, a config edit, a restore, or a version bump.
- **The world depends on its pack.** A world tar is dead data without the exact pack version it was generated
  with. Archive the pack artifact (or its pinned version ID) alongside the world.
- **Mid-world pack changes are "chapters".** They land between play sessions, are announced to the players first
  (in `#announcements` when the server uses Discord), and come with a backup. Nobody logs in to find the game
  changed under them.
- **No stopping, restarting or migrating while friends are online** (`rcon-cli list` first) without a heads-up.

## Access stays private

- **Never port-forward publicly.** Friends connect through a Tailscale **node share** of the game host, and are
  never invited into the tailnet.
- **Keep RCON unpublished** and whitelist-on.

## The pack-design bar

This bar is **Vanilla Extended's**: the pack this repo ships and the live server runs. A kit instance may run a
custom pack instead (`docs/kit/CUSTOM-PACK.md`, design decision, 2026-10-14). Its design is its owner's, and the kit
claims only that it passes the build gates and booted once. "The world is the asset" and "Access stays private" bind
every pack.

- A veteran vanilla player should figure things out **by playing**, not from a wiki or quest book.
- Leave out tech trees, magic systems, and RPG class systems unless the owner decides otherwise.
- Keep the parts of Minecraft that are fun because they are slow: roads, nether routes, horses, and earning the
  Elytra. Fast travel or heavy QoL that deletes those is a regression.
- Prefer curated, meaningful structures over density.

## Discord is code

The guild surface (channels, topics, onboarding copy) lives in `discord/` and is changed **only** through
`discord/sync.py`: dry run first, then `--apply`, then a `0 actions` re-check. A hand edit in the Discord UI is
drift, and the next sync reports it. Friend-facing copy is casual and plain, not the terse register of these docs.

## Regrow clause: the AI DM

The mc-dm bounded-agent AI DM is **dormant, not dead**. Its old code is kept privately and is not in this copy, and
its deployment is archived on the original server's host. If it regrows (for example as a chapter-release or
weekend-event layer), its **hard-floor doctrine returns with it**, verbatim from its original `CLAUDE.md`:
- no command field in proposals
- a static RCON verb allowlist with no irreversible verbs
- `DRY_RUN` as a safety control
- no autonomy without cheap reversibility
- second-order irreversibility checks

Known retarget cost: the reward `give` NBT predates 1.20.5+ item components.

## Verification

Never claim done without the checks in `AGENTS.md` "Verifying changes". Anything touching the game host or
Discord is verified **live on that boundary**; a local green says nothing about the host or the guild.
