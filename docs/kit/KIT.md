# KIT.md: setting up the friend server on a fresh laptop (for Claude)

You are Claude Code, running **on the laptop that will be the server** (design decision, 2026-10-14: one machine).
This page is the procedure, phase by phase: what to run, the gate each phase must pass, where to stop for the
person, and what to tell them. The person's side is `docs/kit/HUMAN-STEPS.md`.

**Status: run end to end once.** The proof run on a fresh VM on the dev machine (2026-10-16, run
`20261016T030748Z`) went through every phase; what it found, and what fixed each finding, is `docs/kit/2026-10-16_proof-run-findings.md`.
The fixes from that run (the pack card after P0, the token tool, P6's `clean`, P9's `world.sh done`, P11's
look-back) are built and tested against fakes, but no run has gone through them yet: they are **[verify live]**
until the next one. When a step here proves wrong, fix this page in the same change.

## The rules that bind you here

- `CLAUDE.md` binds from the first command. From P8 on there is a world, so: a fresh `deploy/backup.sh` before any
  server-side change, and no stop or restart while anyone is online (`rcon-cli list` first).
- **Never a bare `docker compose up -d`**, and never `docker compose restart`. Minecraft starts with
  `docker compose up -d minecraft`, the bridge with `docker compose up -d --no-build bridge` after
  `docker compose build bridge` (AGENTS.md "Discord bridge": a failed bridge build must never stop Minecraft).
- **Each Bash tool call is a fresh shell.** No variable, function or `cd` survives into the next call. Run every
  command from the repo root, and put whatever a command needs (a variable, a loop, a wait) in the same call.
  Nothing here relies on a helper defined earlier.
- **Secrets never enter the chat or a file in git.** Tokens arrive through a hidden prompt the person runs in a
  **second terminal window** (the one they opened at human step 7). Never ask for a password, a Discord user token
  or a login. Never `cat`, `grep` or print `.env`, `.env.bridge`, `discord/.env`, `/etc/mc-host/webhook`, the staged
  webhook or the Tailscale token file. The kit's tools read them and print only what you need.
- **Docker prints those secrets too.** Compose resolves `.env` and `.env.bridge` into every container, so never run
  `docker inspect`, `docker compose exec ... env` (or `printenv`), or `docker compose config` with its output
  showing: only ever `docker compose config -q 2>/dev/null`, as the gates do. Its parse error can quote a token. When
  compose cannot parse, find the cause with `python3 kit/render.py --check` (it names the key) and check.sh's
  shape-only `.env.bridge` lines, never by reading compose's error.
- **A gate is a command that can fail**, run through `kit/record.py gate`. You never pass a phase by saying so:
  `record.py end` derives the status from the recorded gates. A gate only a person can see (chat showing up in
  game) is `record.py observe`, which the record labels as observed.
- **Talk to the person plainly.** One thing at a time: what you need, where to click (quote the step from
  HUMAN-STEPS.md), and roughly how long it takes. Say when they can walk away (the build, the world pre-generation)
  and when you need them back.
- `kit.toml` is the instance's one config file (`kit/kit.example.toml`, schema `kit/kitconfig.py`). You fill it in
  phase by phase (P0 host and network, P1 Discord, P3 Tailscale, P4 the server); after any change to it once it is
  complete, `python3 kit/render.py`. Never edit `host/kit.env`, `host/rendered/` or the rendered `.env` keys by hand.

## The run record

`kit/record.py` keeps the record of this run in `kit/runs/<run id>/` (gitignored): the commit at start and finish,
a timestamp for each phase, every gate with its exit code, and every human step as it happens.

```sh
python3 kit/record.py begin P4
python3 kit/record.py gate P4 -- python3 kit/render.py --check
python3 kit/record.py human <id>                     # the moment the person finishes that HUMAN-STEPS step
python3 kit/record.py human --unplanned "Discord terms-of-service tick"   # a step the list does not have
python3 kit/record.py end P4                         # pass only if every gate exited 0
```

- Phases run in order. `begin` refuses while an earlier phase has not passed. A phase that fails can be begun
  again after the fix. When a gate fails for a reason the person accepts, the order is: `end Pn` (it fails and
  says why), ask the person, and on their yes
  `end Pn --accept "<the reason>"`. That records the failure as accepted, with its gates as they ran (nothing is
  re-run), and the record shows it. It works only right after the failed `end`, before anything else begins, and
  once.
- Log a human step **when it happens**, not later from memory: each one has its `python3 kit/record.py human <id>`
  line below, at the point it happens. Steps done before you were running (1–6) are logged with `--retro` at the
  start. `python3 kit/record.py status` names the required steps not logged yet. `record.py finish` refuses while
  one is missing, and the run stays open: log a step the person did with `--note "logged late"`, then finish. Only a
  run that really misses one is sealed with `python3 kit/record.py finish --incomplete`, and its N is then
  "INCOMPLETE, at least n", never the measured N.
- N counts the host owner's one-time steps (and the unplanned ones). A friend's step (F10 in P11) is logged too,
  but the record lists it on its own line, outside N.
- Notes, observations and gate commands that look like a token, a share link, a webhook URL or a long random
  string (the RCON password) are refused: the record is handed back.

## What exists at this commit, and what is still open

Every phase below runs a built, tested tool. What is still open, and what you do about it:

| Open | What you do |
|---|---|
| A uid or gid other than 1000 (the bridge image and itzg write the mailbox as 1000) | `kit/probe.sh` refuses; the kit does not run there |
| A maintenance window other than 04:00 (`schedule.maint_start`) | `kit.toml` refuses it; keep 04:00 |
| SSH on a one-machine install: Ubuntu Desktop has no SSH server and the person has no key on it | `setup.sh` step 8 refuses key-only SSH; P7 asks, and with no SSH wanted the gate is `check.sh --no-ssh` (below) |
| Rotating the share link after launch | HUMAN-STEPS R1, in the Tailscale console (the token was used once) |
| An off-host backup | none yet: backups are on the laptop's own disk |
| A pack other than Vanilla Extended (design decision, 2026-10-14) | On the person's choice at the pack card (P0): `docs/kit/CUSTOM-PACK.md`. Modrinth only, Fabric 26.1.2 / loader 0.19.5, sized to the RAM. Claim only "passes the build/lint gates and booted once in a recorded test; not the path the kit's proof run tested". `#welcome`'s shared text still describes Vanilla Extended: tell the person |
| The battery charge cap | `mc-charge-cap` writes it at every boot; it is reported "written", never "verified" (drivers round it, and asus-wmi reads back its own cache) |

## Before P0: get the repo onto the laptop

The person has done human steps 1–6 (Ubuntu; `curl`, `git` and Claude Code installed; logged in) and asked you to
set up the server.

1. `git clone https://github.com/MrRolie/lazy-minecraft-server ~/minecraft-ai-host`. Everything below runs from
   there (`cd` into it in every call).
2. Read `CLAUDE.md`, this page, and `docs/kit/HUMAN-STEPS.md`.
3. Ask them roughly how long steps 1–6 took (the Ubuntu install is likely the longest part). Then start the record
   and log what they already did, before anything else, with their answer as step 6's note:

   ```sh
   cd ~/minecraft-ai-host && python3 kit/record.py start && for s in 1 2 4 5; do python3 kit/record.py human "$s" --retro || exit 1; done && python3 kit/record.py human 6 --retro --note "steps 1-6 took about <their answer>"
   ```

   `--unplanned` is only for a step the list lacks (`python3 kit/record.py human --unplanned "<what>"`), never for
   the time a listed step took: an unplanned step adds 1 to N.
4. Step 3 is optional (power on after a power cut, a BIOS setting). Ask whether they did it: if yes,
   `python3 kit/record.py human 3 --retro`; if not, tell them it is worth doing the next time they are in the BIOS,
   and go on. The kit does not need it.
5. Step 7, the sudo permission: give them this command for the **second terminal** (the path is the checkout's):

   ```sh
   sudo bash ~/minecraft-ai-host/kit/sudoers.sh install
   ```

   When they say it's done, check it without a password, then log it:

   ```sh
   cd ~/minecraft-ai-host && sudo -n true && sudo bash kit/sudoers.sh check && python3 kit/record.py human 7
   ```

   If `sudo -n true` asks for a password, the drop-in is not in place: read `sudo bash kit/sudoers.sh check`'s
   answer with them (it refuses a file it did not write) before anything else.

Tell them: "I'll check the laptop first and ask you which game you want on it, then we'll set up Discord together.
That part needs you for about 15 minutes; after that it's mostly me."

## P0. Probe

Read-only. Learn the machine, and refuse early on anything the kit cannot run on. `kit/probe.sh` reads the machine
and `kit/probe.py` judges it; the facts and the values for `kit.toml` land in `kit/probe.json` (gitignored).

```sh
python3 kit/record.py begin P0
python3 kit/record.py gate P0 -- bash kit/probe.sh
python3 kit/record.py end P0
```

- **A REFUSE line** (root, uid or gid ≠ 1000, not Ubuntu, no NetworkManager connection, a route into
  `10.213.37.0/24`, under 50 GB free, or a fact it could not read): stop. Tell the person plainly why the kit can't
  run here as it stands; do not work around it.
- **Under 16 GB of RAM** (the probe's floor is 14 GiB of MemTotal, a nominal 16 GB machine): only 16 GB is proven.
  Tell them: "This laptop has N GB. The kit is proven on 16 GB; on less it may run out of memory with several
  friends on, and nobody has measured where the floor is. Do you want to go ahead anyway?" Only on a clear yes,
  begin P0 again and gate `bash kit/probe.sh --accept-unproven-ram` (the acceptance is written into probe.json),
  and set `server.memory` to about RAM − 7 GiB in P4.
- A WARN (a codename other than `noble`, under 4 threads, a battery without a charge-cap file): say it, and go on.

Then start `kit.toml` from the example and fill in `[host]`, `[net]` and `[battery]` from `kit/probe.json`
(`derived`) and the system:

```sh
cd ~/minecraft-ai-host && cp -n kit/kit.example.toml kit.toml && python3 -c 'import json; print(json.dumps(json.load(open("kit/probe.json"))["derived"], indent=1))'
```

- `[host]`: `name` from `hostname`, `user`, `home` and `repo` from the system, `tz` from
  `timedatectl show -p Timezone --value`.
- `[net]`: `mode = "dhcp"` (the default: the router gives the address). `wired_conn` is `derived.net_wired_conn`.
  A Wi-Fi connection goes in `wifi_conn` only for a Wi-Fi-only laptop (`derived.wifi_only`) or when the person asks
  for a Wi-Fi backup: that needs human step 19 (P4). A static address only on request: human step 20 (P4).
- `[battery]`: `charge_cap = 80` when `derived.suggested_charge_cap` is 80, else leave the section out.

### The pack (human step 23)

Now the RAM is known, ask which pack the server runs. Ask with the **AskUserQuestion** tool, so the person gets a
card, not a wall of text. One question ("Which game do you want on the server?"), two options, in this order:

1. **Vanilla Extended (Recommended).** Description: "Minecraft as if Mojang had kept adding structures, bosses and
   places to explore. It still feels like Minecraft, and it's the pack the kit's test run went through end to end."
2. **Something different.** Description: "We talk about what you want the game to feel like, and I find mods for
   it on Modrinth, sized to this laptop. I can promise it passes the kit's checks and starts once in a test, nothing
   more."

Log it once, the moment they answer. The note is the option they picked, one word: `vanilla-extended` or `custom`.

```sh
python3 kit/record.py human 23 --note "<vanilla-extended or custom>"
```

- **Vanilla Extended:** keep `pack.mode = "vanilla-extended"` in `kit.toml` and go on to P1.
- **Something different:** follow `docs/kit/CUSTOM-PACK.md` steps 1–4 now (it ends with `pack.mode = "custom"`).
  Step 23 is already logged: do not log it again. The pack's name reaches the record in P5, whose spike line
  names its `dist/` file. If the conversation ends in Vanilla Extended after all, keep that `pack.mode` and say so
  in P5's spike observe line.

## P1. Discord (human steps 9–16)

First, because it needs nothing from the host. Tell them: "Now Discord. You'll make a server and two bots; I'll
tell you each click. About 15 minutes."

1. `python3 kit/record.py begin P1`.
2. Steps 9 (if needed), 10 and 11: walk them through, logging each as they finish it:
   `python3 kit/record.py human 9` (only if they had to make a Discord account), `python3 kit/record.py human 10`,
   `python3 kit/record.py human 11`.
3. Step 12: give them this for the **second terminal**, and when they say it's done, `python3 kit/record.py human 12`:

   ```sh
   cd ~/minecraft-ai-host && python3 kit/secret.py discord-admin
   ```

   Tell them to paste **only the token** at its hidden prompt, then Enter. It refuses a pasted command, or anything
   not shaped like a bot token, and then writes nothing. Running it again replaces the token; it never adds a
   second line.

4. Step 14, the bridge bot: walk them through it, then `python3 kit/record.py human 14`. Step 15, in the second
   terminal, and when they say it's done, `python3 kit/record.py human 15`:

   ```sh
   cd ~/minecraft-ai-host && python3 kit/secret.py discord-bridge
   ```

5. `python3 discord/bootstrap.py discover`. It checks both tokens belong to two different bots, and prints the
   authorize link for whichever bot is in no guild yet: the admin bot's is step 13, the bridge bot's (pinned to
   their guild) is step 16. Log each when they finish it (`python3 kit/record.py human 13`,
   `python3 kit/record.py human 16`), and run `discover` again. A captcha on **Authorize** is part of that step,
   never an unplanned one: add `--note "captcha"` to that step's line. Once both bots are in the
   one guild, it prints the `[discord]` lines: put `guild_id` and `bridge_bot_id` into `kit.toml` (keep
   `profile = "kit"`), and run `discover` once more: exit 0 means `kit.toml` agrees.
6. Message Content for the bridge: `python3 discord/bootstrap.py intents --apply`. If it prints a HUMAN STEP (the
   API refused, or the read-back lacks the flag), ask for the 2 clicks in step 14's note and log them as
   `python3 kit/record.py human --unplanned "Message Content by hand"`; then run `intents` again.

Gates (`discover` and a dry-run `intents` exit 0 only when nothing is left):

```sh
python3 kit/record.py gate P1 -- python3 discord/bootstrap.py discover
python3 kit/record.py gate P1 -- python3 discord/bootstrap.py intents
python3 kit/record.py end P1
```

## P2. Packages

`kit/packages.sh` installs Docker Engine from Docker's apt repository (setup.sh's unattended-upgrades rules assume
`docker-ce`, not the snap), Tailscale from its apt repository, and python3, each repository key checked against a
pinned fingerprint, and adds the kit user to the `docker` group. It refuses, changing nothing, on a conflicting
Docker install or a vendor apt source it did not write. Its own last step is the gate, run as root: apt reads and
fetches every source, Docker answers, and the compose plugin and Tailscale report their versions.

```sh
python3 kit/record.py begin P2
python3 kit/record.py gate P2 -- sudo bash kit/packages.sh
python3 kit/record.py gate P2 -- sg docker -c 'docker info'
python3 kit/record.py end P2
```

**The docker group does not reach your own shell** until the person next logs in (it will, after the P10 reboot).
Until then, run every docker command, and every script that calls docker, as `sg docker -c '<command>'` (it
worked this way on the proof run). Systemd units are not affected.

## P3. Tailscale (human steps 17, 18 and 21)

Tell them: "Now the private network your friends will use to reach the server. You'll sign in once and make one
token; I'll use it once and then ask you to cancel it."

1. `python3 kit/record.py begin P3`, then `sudo tailscale up --qr`: they scan it (step 17). The command waits until
   the login is done, so run it under `timeout 600` and poll; if your tool call times out first, the login is still
   pending, not failed. Once they are signed in, `python3 kit/record.py human 17`.
2. `python3 kit/tailscale.py status`: it refuses until the laptop is logged in with MagicDNS on, then prints the
   `[tailscale]` lines (`host_name`, `server_address`). Put them into `kit.toml`.
3. Step 18, in the second terminal, and when they say it's done, `python3 kit/record.py human 18`:

   ```sh
   cd ~/minecraft-ai-host && python3 kit/secret.py tailscale
   ```

   Tell them to paste **only the token** (it starts with `tskey-api-`), not the command again.

4. `python3 kit/tailscale.py setup`: a dry run. It reads the tailnet's policy and devices, validates the merged
   policy, and prints every change it would make; it writes nothing. Read it. **A fresh tailnet's policy form can
   vary** (on the proof run it was the grants form, with one allow-all grant): record what the dry run showed with
   `python3 kit/record.py observe P3 "<the policy changes it planned>"`.
5. The gate below runs `setup --apply`: the policy (the default allow-all narrowed to members and tagged devices,
   `tag:mc-server` in `tagOwners`, and friends limited to `tcp:25565` on that tag; it refuses any other rule that
   would let shared users in), the tag, key expiry off, and the share link written into `discord/.env`, never
   printed. It reads each result back live, and only then deletes the token file. Its last lines give
   `device_id` for `kit.toml`.
6. Ask for step 21 (revoke the token in the console), and when they say it's done,
   `python3 kit/record.py human 21`.

Gates:

```sh
python3 kit/record.py gate P3 -- python3 kit/tailscale.py setup --apply
python3 kit/record.py gate P3 -- python3 -c 'import datetime, sys; sys.path.insert(0, "discord"); import sync; sync.share_link(sync.read_env(), datetime.date.today())'
python3 kit/record.py gate P3 -- test ! -e "$HOME/.config/mc-kit/tailscale-api-token"
python3 kit/record.py end P3
```

If `setup --apply` refuses, the token file is kept for a re-run: fix what it names and gate it again. If you stop
for good, delete the file and have them revoke the token anyway.

## P4. Render the config

1. Ask for their Minecraft name (`server.owner_name`, made op on first start) and, optionally, a seed. Ask human
   step 8 (the EULA) in these words: "Minecraft's server needs you to accept its EULA:
   https://aka.ms/MinecraftEULA. Do you accept it?" Only a clear yes sets `server.eula_accepted = true`; then
   `python3 kit/record.py human 8`.
2. Step 20, only when the person wants a fixed address: ask for the router's DHCP range, set
   `net.mode = "static"`, and pick `net.static_addr` outside it (with `gateway` and `dns`, from
   `ip -j route show default`). Then `python3 kit/record.py human 20`.
3. Step 19, only when `kit.toml` names a `wifi_conn` and P0 found no saved Wi-Fi connection for it. List the
   networks (`nmcli -t -f SSID device wifi list`), ask which one is theirs, and give them this for the
   **second terminal** (it asks for the Wi-Fi password there, never in this chat):

   ```sh
   sudo nmcli device wifi connect '<their network>' --ask
   ```

   When they say it's done, `python3 kit/record.py human 19`. NetworkManager names the new connection after the
   network: check it with `nmcli -t -f NAME,TYPE connection show`, and that NAME is `net.wifi_conn`.
4. Finish `kit.toml`: every key of `kit/kit.example.toml` holds this machine's value. `server.memory` is 8G on
   16 GB (about RAM − 7 GiB below that). Leave `server.open_date` out until launch.
5. Create the compose `.env` once, with a generated RCON password that is never shown, then render:

   ```sh
   cd ~/minecraft-ai-host && { [ -e .env ] || (umask 077; printf 'RCON_PASSWORD=%s\nLEVEL=world\n' "$(python3 -c 'import secrets; print(secrets.token_urlsafe(24))')" > .env); } && python3 kit/render.py
   ```

Gates (compose's parse errors can quote `.env.bridge`, so its output is dropped; on a failure, `render --check`
names the key):

```sh
python3 kit/record.py begin P4
python3 kit/record.py gate P4 -- python3 kit/render.py --check
python3 kit/record.py gate P4 -- sg docker -c 'docker compose config -q 2>/dev/null'
python3 kit/record.py end P4
```

`kit/kitconfig.py` refuses what the kit cannot honour yet (uid or gid other than 1000, `maint_start` other than
04:00, a static address without its gateway, an overlap with `10.213.37.0/24`, an all-zero guild id). A refusal
names the key; fix `kit.toml`, never the schema.

## P5. Build the mod and the pack

Tell them: "Now I build the server's files. This takes a few minutes and needs nothing from you."

```sh
python3 kit/record.py begin P5
sg docker -c 'bash mod/build.sh'     # builds ve-commands in Docker and checks it against mod/RELEASE.sha256
python3 pack/build.py               # the friend pack (pack/, or pack-custom/ when kit.toml says custom), with this server's address in its server list
python3 kit/record.py gate P5 -- sg docker -c 'bash mod/build.sh --check'
python3 kit/record.py gate P5 -- sh -c 'test -f "dist/$(python3 pack/build.py --which)"'
python3 kit/record.py gate P5 -- python3 -c 'import subprocess, tomllib, zipfile
addr = tomllib.load(open("kit.toml", "rb"))["tailscale"]["server_address"]
name = subprocess.run(["python3", "pack/build.py", "--which"], capture_output=True, text=True, check=True).stdout.strip()
dat = zipfile.ZipFile("dist/" + name).read("client-overrides/servers.dat")
assert addr.encode() in dat, "servers.dat does not list " + addr
print("servers.dat lists", addr)'
```

`pack/build.py --which` names the `.mrpack` compose runs: `docker-compose.yml`'s, or the override `kit/render.py`
writes for a custom pack.

Then the spike gate, on every kit. For a custom pack (`docs/kit/CUSTOM-PACK.md` step 6) it is a throwaway boot of
the pack before P8's real first start, up to 15 minutes. For Vanilla Extended it prints `spike: skipped` and passes
at once: P8's first start is that pack's boot. Log what it printed either way, the record keeps only the exit code:

```sh
python3 kit/record.py gate P5 -- sg docker -c 'bash pack/spike.sh --if-custom'
python3 kit/record.py observe P5 "spike: <its last line: skipped, or the PASS line and the mods Fabric loaded>"
python3 kit/record.py end P5
```

`mod/build.sh` never deletes or replaces a jar in `extras/`. If it refuses because `extras/<jar>` exists with other
bytes, stop and find out why; never move it aside on your own.

## P6. Guild bootstrap

This is safe before launch: no friends are in the guild yet. With `discord.profile = "kit"`, the guild gets the
kit's own content: `#setup` from `discord/content/kit/setup.md` (this server's address and share link) and the
shared welcome. A kit guild has no `#the-story`. **sync refuses, before any API call,** to give a kit guild the
original server's text (its story, its `#setup`, `discord/posts/`), its guild id or its address, and the
`asus-node` profile syncs only that server's guild. A refusal here is a stop: never change the profile to get past
it.

First, `clean` clears out the channels Discord's template made (`Text Channels`, `#general`, `Voice Channels`,
`General`, named in the person's language), so the guild does not end up with two `#general`s. It deletes every
channel and category that `discord/layout.toml` does not list. It is for a guild that is minutes old: it refuses,
and deletes nothing, unless the only members are the person and the two bots, every channel it would delete was
made with the guild (a channel made more than a minute later is the person's, even an empty one; an empty one made
within that minute is taken for the template's, so the dry run's list is the check), and each holds nothing but the
bots' "joined the server" messages and no thread. Show the person the dry run's list before `--apply`, and ask them
not to type in those channels while it runs: `--apply` checks each one again just before deleting it. If it refuses,
read what it names. Usually someone typed something in `#general`: ask the person to delete that message (or the
whole channel) in Discord, log it with
`python3 kit/record.py human --unplanned "template channel by hand"`, and run `clean` again. Never run it on a
guild friends already use.

```sh
python3 kit/record.py begin P6
python3 discord/bootstrap.py clean            # dry run: lists every channel it would delete, and what the layout keeps
python3 discord/bootstrap.py clean --apply    # deletes them, then reads the channel list back
python3 discord/sync.py              # dry run: read every action, including "~ role @everyone: Create Invite off"
python3 discord/sync.py --apply
python3 discord/bootstrap.py webhook --apply   # the alert webhook in #ops-log, staged for setup.sh step 7
```

The webhook URL is staged at `~/.config/mc-kit/webhook` (mode 600, never printed); `setup.sh` installs it in P7 and
removes the staged copy.

Gates (a dry-run `clean` exits 0 only when nothing outside the layout is left; a dry-run sync exits 0 only at
`0 actions`, the invite lock included; a dry-run `webhook` exits 0 only once the URL is staged or installed and its
own GET answers):

```sh
python3 kit/record.py gate P6 -- python3 discord/bootstrap.py clean
python3 kit/record.py gate P6 -- python3 discord/sync.py
python3 kit/record.py gate P6 -- python3 discord/bootstrap.py webhook
python3 kit/record.py end P6
```

## P7. Host maintenance

Tell them: "Now I set the laptop up to run unattended: never sleep, survive network trouble, update itself at
night, back up the world. A few minutes."

```sh
python3 kit/record.py begin P7
sudo bash host/setup.sh                 # then read its "next steps" list
python3 kit/record.py gate P7 -- sh -c 'log="$HOME/.cache/mc-kit-setup-rerun.log"; mkdir -p "${log%/*}"; sudo bash host/setup.sh >"$log" 2>&1; rc=$?; cat "$log" >&2; [ "$rc" -eq 0 ] && ! grep -E "^   (installed|created|enabled|fixed|masked|reloaded|removed|restarted|set |armed|confirmed|applying|systemctl daemon-reload|loginctl enable-linger|mc-charge-cap: |rfkill: unblocked|nmcli: wifi radio on)|^   .* -> |: changed$" "$log"'
python3 kit/record.py gate P7 -- sudo bash host/check.sh --no-ssh     # without --no-ssh when they want SSH (below)
python3 kit/record.py gate P7 -- sh -c '/usr/local/sbin/mc-alert "kit: the alert path works" 2>&1 | grep "posted (HTTP 204)"'
python3 kit/record.py gate P7 -- sh -c 'systemd-analyze verify /etc/systemd/system/mc-*.service /etc/systemd/system/mc-*.timer'
python3 kit/record.py end P7
```

- The first run installs everything: the units and scripts, linger (step 12), the charge cap when `kit.toml` has one
  (step 13), and the webhook P6 staged (step 7). It never arms the server: that is P10's `--stand-up`.
- The second `setup.sh` (the first gate) must change nothing. Its change words are taken from `setup.sh`'s log
  lines at this commit; if the gate trips on a line that is not a change, fix the gate here, and if it is a change,
  that is a bug: stop.
- `setup.sh` and `check.sh` refuse unless the hostname is `kit.toml` `host.name` and the checkout is `host.repo`.
- **SSH, asked before the `check.sh` gate:** `setup.sh` step 8 refuses key-only SSH while
  `~/.ssh/authorized_keys` is empty (it could lock the person out), and Ubuntu Desktop has no SSH server. Ask
  whether they want to reach the server from another computer. If no (the usual answer, and the proof run's), the
  gate is `sudo bash host/check.sh --no-ssh`: it skips the key-only lines and FAILs if an SSH server is running, so
  it passes with no `--accept`, and any FAIL it shows is a real failure. If yes,
  `sudo apt-get install -y openssh-server`, add their key, re-run setup, and gate `sudo bash host/check.sh` (no
  flag) here and in P10.
- **A Wi-Fi password stored only for the person's account** (the desktop's default for a network joined from its
  menu): `setup.sh` step 2 aborts on a Wi-Fi-only laptop ("its password is not stored system-wide"), and on a
  laptop with a cable it skips the Wi-Fi backup with the same reason in its next steps. That is human step 19: give
  them the command `setup.sh` printed for the **second terminal** (it asks for the password there, never in this
  chat, and keeps the connection's name), and when they say it's done, `python3 kit/record.py human 19`. Then run
  `sudo bash host/setup.sh` again, ungated, until it finishes with no abort, and only then the gates above (the
  first gate needs a run that changes nothing).
- The charge cap is reported "written", never "verified": tell them that is the most a laptop driver allows.

## P8. First start

Tell them: "Starting Minecraft for the first time. It takes about two minutes."

```sh
python3 kit/record.py begin P8
sg docker -c 'docker compose up -d minecraft'
python3 kit/record.py gate P8 -- sh -c 'for i in $(seq 180); do sg docker -c "docker compose logs minecraft" | grep -q "Done (" && exit 0; sleep 5; done; exit 1'
python3 kit/record.py gate P8 -- sg docker -c 'docker compose exec minecraft rcon-cli list'
python3 kit/record.py gate P8 -- python3 -c 'import json, tomllib
name = tomllib.load(open("kit.toml", "rb"))["server"]["owner_name"]
assert any(o["name"].lower() == name.lower() for o in json.load(open("mc_data/ops.json"))), name + " is not op"'
python3 kit/record.py end P8
```

A boot is about 108 s to `Done` on the original laptop (AGENTS.md "Hard-won server facts"), but the first start also downloads
the server and every mod, so it takes longer; the gate waits up to 15 min. The container is new here, so its whole
log is this boot's.

## P9. The world

From here on there is a world, and `CLAUDE.md`'s rules bind: **a fresh backup before every server-side change**
(each one is a gate below), and no restart while anyone is online. Nobody can be on yet (no friend has the
address), but check anyway: `sg docker -c 'docker compose exec minecraft rcon-cli list'`.

```sh
python3 kit/record.py begin P9
python3 kit/record.py gate P9 -- sg docker -c 'bash deploy/backup.sh'        # the first backup: before world.sh init
python3 kit/record.py gate P9 -- sh -c 'tar -tzf "$(ls -t backups/world-*.tar.gz | head -1)" >/dev/null'
python3 kit/record.py gate P9 -- sg docker -c 'bash deploy/world.sh init'
```

Pre-generation (AGENTS.md "Hard-won server facts": a paused server never unloads chunks, so it OOMs):

1. Pausing off: set `server.pause_when_empty_seconds = -1` in `kit.toml` and `python3 kit/render.py`. A changed
   `.env` reaches the server only through a new container, so: a fresh backup, then `up -d minecraft` and wait for
   this start's `Done (` (the restart is the server-side change):

   ```sh
   python3 kit/record.py gate P9 -- sg docker -c 'bash deploy/backup.sh'
   python3 kit/record.py gate P9 -- sh -c 't=$(date +%s); sg docker -c "docker compose up -d minecraft" || exit 1; for i in $(seq 180); do sg docker -c "docker compose logs --since $t minecraft" | grep -q "Done (" && exit 0; sleep 5; done; exit 1'
   ```

2. The rings, one after another (each sets the maintenance flag itself):
   `sg docker -c 'bash deploy/world.sh ring overworld 3000'`, then the same with `the_nether 1500`, then
   `the_end 2000` (each a radius: the borders become 6000, 3000 and 4000).
   `sg docker -c 'bash deploy/world.sh status'` shows progress. Watch for `Pause Full (G1 Compaction` (AGENTS.md
   "Running a ring").
3. Afterwards, pausing back on: `server.pause_when_empty_seconds = 60` in `kit.toml`, `python3 kit/render.py`, then
   the same two gates again (a fresh backup, then `up -d minecraft` and its `Done (`), then
   `sg docker -c 'bash deploy/world.sh maintenance off'`.

Tell them: "Now the world pre-generates, so exploring never stutters. On the original laptop this took about 8
hours. You can leave it; I'll check on it." The rings are the longest phase by far.

```sh
python3 kit/record.py gate P9 -- sh -c 's="$(sg docker -c "bash deploy/world.sh status")" && echo "$s" && echo "$s" | grep -qx "overworld: border 6000" && echo "$s" | grep -qx "the_nether: border 3000" && echo "$s" | grep -qx "the_end: border 4000"'
python3 kit/record.py gate P9 -- sg docker -c 'bash deploy/world.sh done overworld=3000 the_nether=1500 the_end=2000'
python3 kit/record.py gate P9 -- sh -c '! test -e maintenance'
python3 kit/record.py gate P9 -- python3 -c 'import tomllib; assert tomllib.load(open("kit.toml", "rb"))["server"]["pause_when_empty_seconds"] == 60, "pausing is still off"'
python3 kit/record.py gate P9 -- python3 kit/render.py --check --env-only
python3 kit/record.py end P9
```

`world.sh done` passes only when Chunky has finished all three rings at those radii. It reads Chunky's own task
files and log lines, and only those written since `ring` started each ring on this world (`ring` leaves a stamp in
the world's folder), so a ring cut short fails it, and says which one and how far it got. If the person chooses to stop early (a
test machine), that failure is theirs to accept: `end P9`, then `end P9 --accept "<why>"`.

The border gate is there for a reason too. On the proof run the Nether and End borders had gone back to the
default after a restart, and that gate is what caught it (`docs/kit/2026-10-16_proof-run-findings.md` #10). It
has not happened since and the cause is not known. If the gate fails, record what you saw with
`python3 kit/record.py observe P9 "<what status showed>"` and stop to find out why. On the proof run the fix was a
fresh backup, the two borders set again by rcon, and a restart to prove they held.

## P10. Arm, and a supervised reboot (human step 22)

```sh
python3 kit/record.py begin P10
python3 kit/record.py gate P10 -- sg docker -c 'bash deploy/backup.sh' && python3 kit/record.py gate P10 -- sudo bash host/setup.sh --stand-up
```

The fresh backup is a gate (`CLAUDE.md`: a fresh backup before any server-side change), and it runs under
`sg docker` because your shell has no docker group until the reboot. `setup.sh --stand-up` runs every step again
(all "unchanged") and then step 14: it enables `mc-server.service` and `mc-backup.timer`, and refuses while the
world, its pack or a backup is missing, or when any earlier step aborted. If either gate fails, stop: no reboot.

The bridge image does not exist until P11, so `mc-bridge-up` posts a "bridge did not start" alert in `#ops-log`;
that one is expected.

Tell them: "Last check of this part: I'll restart the laptop to prove the server comes back by itself. Claude
closes too. When the laptop is back, log in, open a terminal, and run `claude --continue`." Then, only if both
gates passed, `sudo systemctl reboot`. When you are back:

```sh
python3 kit/record.py human 22     # they logged back in and ran claude --continue
python3 kit/record.py gate P10 -- sh -c 'boot=$(date -d "$(uptime -s)" +%s); for i in $(seq 60); do docker compose logs --since "$boot" minecraft | grep -q "Done (" && exit 0; sleep 5; done; exit 1'
python3 kit/record.py gate P10 -- docker compose exec minecraft rcon-cli list
python3 kit/record.py gate P10 -- sudo bash host/check.sh --no-ssh     # the same check.sh gate as P7's
python3 kit/record.py gate P10 -- sh -c 'systemctl list-timers mc-backup.timer | grep -q mc-backup.timer'
python3 kit/record.py end P10
```

Plain `docker` works from here on: the login gave your shell the docker group.

## P11. The chat bridge

```sh
python3 kit/record.py begin P11
docker compose build bridge
docker compose up -d --no-build bridge
python3 kit/record.py gate P11 -- sh -c 'sleep 20; docker compose logs bridge | grep -q "ready as"'
```

Then the round trip, both ways, without anyone in the game (`mod/CONTRACT.md`: the bridge relays what the mod
writes, which is player chat and game messages, not a console `say`):

1. **Discord → game → Discord** (proven on the original server): ask the person to run
   `/whitelist <their Minecraft name>` in Discord. The bridge passes it to the mod, the mod does the Mojang lookup
   and adds them, and the answer comes back in Discord. Log it as `python3 kit/record.py human F10 --note "the host, as the bridge check"`.
2. **Discord chat → game** (proven once, on the proof run): pick `kit-check-<4 random digits>` yourself and give
   the person that exact phrase to send in `#game-chat`. Tell them it has to be that phrase, as written: it is what
   the gate looks for in the server log, and any other text fails it. The gate reads the log since this phase
   began, so it does not matter how long they take.
3. **Game → Discord, checked by eye:** ask the person to look in Discord, not in the server log. A server stop and
   start shows in `#game-chat` (the mod's `server` event). If they can join the game, ask them to join, type a line
   of chat, save a place with `/coords <name>`, note a find with `/discovery <text>`, and leave. Each of those has
   to show up in Discord: the join, the chat and the leave in `#game-chat` (a death too, if one happens), the
   place in `#coords`, the find in `#discoveries`. Ask them what they saw in each channel. If they cannot join the game, check the server
   line only, and say so in the record.

```sh
python3 kit/record.py gate P11 -- sh -c 'docker compose exec minecraft rcon-cli whitelist list | grep -qiF "$(python3 -c "import tomllib; print(tomllib.load(open(\"kit.toml\", \"rb\"))[\"server\"][\"owner_name\"])")"'
python3 kit/record.py gate P11 -- sh -c 's=$(python3 kit/record.py began P11) && docker compose logs --since "$s" minecraft | grep -F "kit-check-"'
python3 kit/record.py observe P11 "<what the person saw in Discord: the /whitelist answer, and in #game-chat, #coords and #discoveries>"
python3 kit/record.py end P11
```

Keep the `&&` in the second gate: `--since ""` would read the whole log.

## P12. Launch

1. Set `server.open_date` in `kit.toml` to today, `python3 kit/render.py`, and sync: `python3 discord/sync.py`
   (dry run: the `#welcome` status line becomes "open"), then `python3 discord/sync.py --apply`.
2. The friend invite: `python3 discord/bootstrap.py invite --apply`. It refuses while `@everyone` can still create
   invites (the lock is P6's), makes a 7-day invite into `#welcome`, checks it with a GET, and prints the
   `https://discord.gg/<code>` link. Give it to the person: with the lock on it is the one way in, so they share it
   with friends only; when it runs out, the same command makes a fresh one.
3. The sudoers drop-in **stays** (design decision, 2026-10-14). Tell them how to remove it if they ever want to:
   `sudo bash kit/sudoers.sh remove`.

```sh
python3 kit/record.py begin P12
python3 kit/record.py gate P12 -- python3 discord/sync.py
python3 kit/record.py gate P12 -- python3 discord/bootstrap.py invite
python3 kit/record.py end P12
python3 kit/record.py finish
```

`finish` refuses while a required step was never logged, and names it: if the person did it, log it with
`--note "logged late"` and finish again; `finish --incomplete` is only for a run that really misses one.

Tell them what they have, in plain words: the server, the invite link to send friends, what each friend does
(HUMAN-STEPS "Each friend"), and the two things that come back now and then (R1, R2). Give them the path of
`record.md`. **For a test run of the kit itself,** its maintainer takes the record from the run directory; never write
anywhere outside this checkout yourself.

## After launch

Chapters, fixes, Docker upgrades, restores: `AGENTS.md`, which is this repo's operational truth for any instance.
The friend-server doctrine is `CLAUDE.md`.
