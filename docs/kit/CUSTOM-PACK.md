# CUSTOM-PACK.md: a pack other than Vanilla Extended (for Claude)

You are here because the person picked **something different** on the pack card. Vanilla Extended is the kit's
one verified pack (design decision, 2026-10-14). Any other pack comes from a conversation between the person and
you, held to the process Vanilla Extended went through: a brief, a short list, the build gates, a test boot, and
cutting whatever breaks. This page is that procedure. When it ends, you go back to `docs/kit/KIT.md`.

**When:** after P0 (you need the laptop's RAM from `kit/probe.json`) and before P4 (the render reads the pack's
name). The test boot (step 6) runs in P5, after the build and before P8's real first start.

## What the kit can promise, and what it can't

Say this to the person before any research, in your own words:

> "I can build you a different pack. What I can promise is that it passes the kit's build checks and that it booted
> once in a recorded test. I can't promise it plays well, and it isn't the path the kit's own test run went
> through. Vanilla Extended is the one that has been tested end to end."

The claim for a custom pack is exactly this, and nothing more: **"passes the build/lint gates and booted once in a
recorded test; not the path the kit's proof run tested."** Never "plays well", never "verified".

The fixed points, and why:

- **Modrinth only.** `pack/build.py` resolves every mod through Modrinth's API and checks every download against
  Modrinth's hashes. No CurseForge, no jar from a website, no `.mrpack` the person downloaded.
- **Fabric on Minecraft 26.1.2, Fabric loader 0.19.5**, the same as `pack/mods.toml`. A different version is out
  of scope for the kit: `ve-commands` (the Discord bridge, game chat in Discord, `/whitelist` from Discord) is built
  and its checksum pinned for exactly that pair (`mod/README.md`), and the server image is the one for its Java.
  Another version means no bridge until someone rebuilds that mod, which is not this kit. `pack/build.py` refuses
  another pair.
- **One machine**, the same laptop. The pack is sized to its memory (step 2).
- **What the kit needs is in every pack:** Fabric API (ve-commands needs it), Chunky (the world pre-generation in
  P9) and the kit's Chunky settings. `pack/build.py` refuses a pack without them.

## 1. Ask the person

One question at a time, plainly:

1. **What do you want the game to feel like?** Some examples if they are stuck: more adventure and bosses; more
   building and decoration; farming and cooking; harder survival; exploring strange new places; machines and
   automation; magic. Or a pack they already know by name: if it is a Modrinth modpack, use its mod list as your
   starting point, held to everything below. You never install their pack file as it is.
2. **How many people will usually play at the same time?**
3. **Anything you want left out?**

Then write it back to them in two or three sentences ("a slower, harder survival game with new dungeons, for about
6 people, no tech mods") and get their yes. That is the brief.

## 2. The memory budget

Read the RAM from `kit/probe.json` (`derived.ram_mib`). The server's memory (`server.memory`, KIT.md P4) is 8G on a
16 GB laptop and about RAM - 7 GiB below that. Size the pack to it. This rule is **rough and unmeasured**; only
Vanilla Extended on 16 GB is measured:

| Server memory | Roughly how many server mods | World generation |
|---|---|---|
| 8G (16 GB laptop) | up to about Vanilla Extended's size (about 40 server jars) | a few world-generation mods |
| 5-6G (12 GB) | about half that (about 20) | at most one terrain mod |
| under 5G | about 10 | no terrain mods, no big structure packs |

What costs the most: terrain mods (they reshape the whole world), big structure packs, and anything that keeps
many machines or mobs running all the time. More players at once costs more too. Heavy world generation also makes
P9's pre-generation slower. When the brief does not fit, say so and offer the smaller version.

## 3. Research on Modrinth

Use Modrinth's public API only, with a User-Agent that names the kit. Let curl encode the brackets in the
parameters (`-G` with `--data-urlencode`): written bare in the URL, curl reads them as its own patterns and stops
with exit 3 and no output, which would look like "no version". For each candidate:

- It has a **Fabric** version for **26.1.2**:

  ```sh
  curl -sfG -A "minecraft-ai-host kit (custom pack)" --data-urlencode 'loaders=["fabric"]' --data-urlencode 'game_versions=["26.1.2"]' https://api.modrinth.com/v2/project/<slug>/version
  ```

  Exit 0 and `[]` means no. A non-zero exit is an error (22: no such project, or the request was refused), not a
  no. Prefer a `release` over a beta or alpha.
- Its sides (`client_side`, `server_side` from `/v2/project/<slug>`) decide `side`: server only, client only, or
  both. A mod the server does not need but players see (textures, sounds) is `both`.
- Its required dependencies (in the version's `dependencies`) go into the pack too.
- Search:

  ```sh
  curl -sfG -A "minecraft-ai-host kit (custom pack)" --data-urlencode 'query=<words>' --data-urlencode 'facets=[["versions:26.1.2"],["categories:fabric"],["project_type:mod"]]' https://api.modrinth.com/v2/search
  ```

Both were run against Modrinth on 2026-10-16: AppleSkin has `3.0.10+mc26.1.2`, and the search finds it.

Prefer fewer, well-kept, widely used mods over many small ones. Show the person the list with one line each (what
it adds, in game terms), and get their yes before you build.

## 4. Write pack-custom/

The custom pack lives in `pack-custom/`, which git ignores. Never edit anything under `pack/`: that is Vanilla
Extended's, and a changed tracked file would conflict with the next update of the kit.

```sh
mkdir -p pack-custom/overrides/config/chunky && cp pack/overrides/config/chunky/config.json pack-custom/overrides/config/chunky/
```

Then write `pack-custom/mods.toml`:

```toml
[pack]
name = "Sky Isles"          # what friends see in their server list and in #setup: 1-40 letters, digits, spaces
version = "1.0.0"           # bump it for every change once friends have the pack
minecraft = "26.1.2"        # the same as pack/mods.toml (build.py refuses anything else)
fabric_loader = "0.19.5"

# The kit's own. Never remove these: build.py refuses a pack without them.
[[mod]]
slug = "fabric-api"
side = "both"
[[mod]]
slug = "chunky"
side = "server"

# The person's picks
[[mod]]
slug = "<modrinth slug>"
side = "both"               # server | client | both
# freeze = true             # on every mod that changes world generation: it never moves on --update
```

- No `chapter` keys: a custom pack has one chapter. A change to it later is a version bump (see the end).
- The name must not be "Vanilla Extended".
- Then set `pack.mode = "custom"` in `kit.toml`. From then on `python3 pack/build.py` builds `pack-custom/`, and
  `python3 kit/render.py` writes `docker-compose.override.yml`, which points the server at this pack and shows its
  name and version in the server list (MOTD).

## 5. Build it with the gates

In P5, `python3 pack/build.py` is the same command as for Vanilla Extended. It locks every mod's exact version in
`pack-custom/lock.json` and runs every check `pack/build.py` has, plus the kit's own (Fabric API, Chunky, the Chunky
settings, the Minecraft and Fabric version). It writes `dist/<name>-<version>-ch1.mrpack` only when all of them
pass.

When it fails, **cut what breaks**: a mod whose dependencies have no 26.1.2 version, a client-only mod marked
`both`, two mods that refuse each other. Tell the person what you cut and why, in one line each. Then build again.

`pack/build.py` refuses to rewrite a pack whose content changed under the same version. Before P8 no world has
used it, so delete the old `dist/` file and build again; after P8, bump the version.

After any change to `[pack]` name or version, run `python3 kit/render.py` again.

## 6. The test boot (the spike)

It is one of P5's gates (KIT.md), after `mod/build.sh` and `pack/build.py`, before P8:

```sh
python3 kit/record.py gate P5 -- sg docker -c 'bash pack/spike.sh --if-custom'
```

With `pack.mode = "custom"`, `pack/spike.sh` starts the server once with this pack, in the same image and with the same settings as the real
server, but as a separate throwaway project: no published port, no restart, a fresh world in `spike/`. It waits up
to 15 minutes for the server log to say `Done (`, checks that `ve-commands` is in Fabric's list of loaded mods, and
then removes everything it made: the container, its network and volumes, and the throwaway world. It passes only
when the server booted **and** nothing is left. It refuses to run while the real server is running.

What proves it booted: its last line, `spike: PASS: dist/<pack> reached "Done (...)" (Loading N mods) ...`, and the
`Fabric loaded:` line above it. Record what it showed, since the record keeps a gate's exit code but not its output:

```sh
python3 kit/record.py observe P5 "spike: <the PASS line, and the mods Fabric loaded>"
```

The spike world is thrown away. It never becomes the friends' world: P8 makes that one, from scratch.

If it fails, read the lines it printed. A crash usually names the mod. Cut that mod, build again, spike again.

This procedure was run once on the dev machine (2026-10-16): Fabric API, Chunky, AppleSkin and Krypton, with ve-commands,
booted to `Done` and left nothing behind.

## 7. Tell the person, then go back to KIT.md

Tell them what they have, plainly: the pack's name, the mods (one line each), and the claim from the top of this
page. Then carry on with KIT.md: the rest of P5's gates, then P6.

What follows the pack on its own: the server list entry friends get (`servers.dat`), the name in `#setup`, the
server's MOTD, and the backups (`deploy/backup.sh` also saves `pack-custom/`, since it is in no git history).

What does not, yet: `#welcome` is text every kit server shares (`discord/content/welcome.md`), and its first
paragraph describes Vanilla Extended. Do not edit it (it is tracked, and the original server's too). Tell the
person, so they can say in `#announcements` what their pack is about.

## Changing the pack later

The world depends on its pack (`CLAUDE.md`). A change is the owner's chapter, done the way `AGENTS.md` "Chapters
only add mods" does Vanilla Extended's: a labelled backup, a rehearsal on a copy of the world, then the real
server, then an announcement. Do it between play sessions, when nobody is online (`rcon-cli list`).

The order matters. `docker-compose.override.yml` is what the server boots, at any restart: a power cut, or the 05:00
maintenance reboot. So it keeps naming the old pack until the new one has booted on a copy of the friends' world.

1. `bash deploy/backup.sh` (it must succeed), then `bash deploy/world.sh maintenance on "pack change"`, so mc-heal
   stands down while the server is stopped.
2. Edit `pack-custom/mods.toml` and bump `version`, never `docker-compose.yml`. Only add mods: removing one deletes
   its blocks and items from the world, and never remove or update a world-generation mod.
3. `python3 pack/build.py`. It ends with `written dist/<new file>`. Do **not** run `kit/render.py` yet: the
   override still names the old pack.
4. Stop the server (`docker compose stop -t 120 minecraft`; the spike refuses to run beside it), then rehearse the
   new pack on a copy of the backup from step 1:

   ```sh
   bash pack/spike.sh --pack <new file> --world backups/world-<time>.tar.gz
   ```

   It boots the new pack on a throwaway copy of the world, then deletes the copy. The friends' world and the
   override are not touched.
5. If it passes: `python3 kit/render.py` (now the override names the new file), `docker compose up -d minecraft`,
   `python3 discord/sync.py --apply` so `#setup` carries the new file, then
   `bash deploy/world.sh maintenance off`. Announce it in `#announcements`.
6. If it fails: put the old `pack-custom/mods.toml` and `lock.json` back from the step-1 backup, which holds both
   (`tar -xzf backups/world-<time>.tar.gz pack-custom/mods.toml pack-custom/lock.json`, from the server's folder).
   Then `docker compose up -d minecraft` (the override still names the old pack) and
   `bash deploy/world.sh maintenance off`. Cut the mod that broke, and start again from step 1 another day.

Switching to another pack, or back to Vanilla Extended, means a new world: `kit/render.py` refuses to change
`pack.mode` while a world exists. Archive the old world first (`AGENTS.md` "Retiring a world").
