# Turn an old laptop into a Minecraft server for your friends

Claude Code does the setup. You do the handful of things only a person can do: install Ubuntu, sign in to a few
accounts, click a few buttons in Discord and Tailscale. At the end your friends play a modded Minecraft world on a
server that looks after itself: **Vanilla Extended** (more to explore, still feels like Minecraft), or a different
pack you pick with Claude.

**Status: run end to end once.** The first full test run, on a fresh virtual machine (2026-10-16), went from a bare
Ubuntu to friends' invite link. What it found is fixed (in code with tests, or in the docs), except one open finding
(`docs/kit/2026-10-16_proof-run-findings.md`), and no run has gone through the fixes yet. Most numbers below are still estimates.

## What you need

- **An old laptop with 16 GB of RAM**, an SSD with **50 GB free**, and a network cable to your router (Wi-Fi alone
  is supported but not yet tested). 16 GB is the only size proven so far (the kit's test run was a 16 GB virtual
  machine; the original server, a 16 GB i5-7200U laptop with 2 cores / 4 threads, runs the version the kit came from). Less may work for a few
  friends, but nobody has measured it; Claude will tell you if your laptop is below that and let you decide.
- **A USB stick of 12 GB or more**, and another computer to make it on.
- **A Claude Pro or Max plan.** The free plan doesn't include Claude Code.
- **A Discord account**, and for each friend: Minecraft Java Edition, a Discord account and a free Tailscale account.
- The laptop stays plugged in and switched on. It never sleeps; closing the lid is fine.

## How long it takes

About a day from start to friends playing, most of it the laptop working on its own:

| Part | Your time | Wall clock |
|---|---|---|
| Ubuntu USB stick and install | about 45 min | about 45 min |
| Claude Code, Discord, Tailscale | about 30 min, in short bursts | about 1 h |
| Building and starting the server | nothing | about 30 min |
| Pre-generating the world, so exploring never stutters | nothing | **about 8 hours** on the original laptop |
| A test restart and the chat-bridge check | a few minutes | about 15 min |

Estimates, except the world pre-generation (about 7.7 h on the original laptop). On the test run, the Discord part
took about 15 minutes and the Tailscale part about 12.

## What you do, and what Claude does

**You** do about 20 things once (19 if you already have Discord) (`docs/kit/HUMAN-STEPS.md` lists each one, with where to click). Each friend does 10
things once. After that, two things come back now and then: a fresh share link for a new friend, and logging in to
Claude Code again when it asks.

**Claude** does the rest, and tells you when it needs you:
- checks the laptop can do it, and stops with a plain reason if it can't
- asks which game you want: Vanilla Extended, or a different pack you pick together
- installs Docker and Tailscale, and sets up the private network your friends join through (nothing is opened to
  the internet)
- builds the Discord server's channels, the chat bridge between the game and Discord, and the alerts channel
- sets the laptop up to run unattended: never sleep, survive network trouble, update itself at night, back up the
  world every night, restart the server if it hangs
- starts Minecraft, makes you the server operator, and pre-generates the world
- restarts the laptop once to prove it all comes back by itself
- keeps a record of the run: what it did, when, and every step you did

Claude never asks for your passwords. Tokens (the keys to your bots and your Tailscale network) go through a
hidden prompt in a second terminal window, straight into files only your account can read, and Claude's tools
never print them. Claude runs as your account, so this is a promise the kit's tools keep, not a wall.

## Start

1. Do steps 1–6 in `docs/kit/HUMAN-STEPS.md`: Ubuntu, then `curl`, `git` and Claude Code.
2. In the terminal, run `claude` and say: "set up the Minecraft friend server kit from
   https://github.com/MrRolie/lazy-minecraft-server".

Claude follows `docs/kit/KIT.md` from there; the first thing it asks of you is step 7, one sudo permission.

---

For maintainers, the tools KIT.md runs, phase by phase:

| Phase | Tool |
|---|---|
| step 7 | `kit/sudoers.sh` (the kit user's sudo drop-in: install, check, remove) |
| P0 | `kit/probe.sh` + `kit/probe.py` (read-only probe and its refusals; writes `kit/probe.json`) |
| step 23 (after P0) | the pack card; a custom pack follows `docs/kit/CUSTOM-PACK.md` |
| P1, P6, P12 | `discord/bootstrap.py` (discover, intents, clean, webhook, invite) and `discord/sync.py` (the guild as code) |
| steps 12, 15, 18, R1 | `kit/secret.py` (the token and share-link hand-off: a hidden prompt, a shape check, the key's line replaced under the env files' lock) |
| P2 | `kit/packages.sh` (Docker Engine and Tailscale from their apt repositories, pinned keys) |
| P3 | `kit/tailscale.py` (status; setup: policy, tag, key expiry, share link; the token used once) |
| P4 | `kit/render.py` (`kit.toml`, schema `kit/kitconfig.py`, template `kit/kit.example.toml`) |
| P5 | `mod/build.sh` and `pack/build.py` (pack/, or pack-custom/ for `pack.mode = "custom"`); `pack/spike.sh --if-custom` (every kit: the recorded test boot of a custom pack; Vanilla Extended prints "skipped") |
| P7, P10 | `host/setup.sh` (and `--stand-up`) and `host/check.sh` |
| P9 | `deploy/backup.sh` and `deploy/world.sh` (`done`: Chunky finished every ring) |
| every phase | `kit/record.py` (the run record) |

`kit/fixtures/example.toml` is a filled-in, made-up `kit.toml` (the tests use it). `AGENTS.md` is the operational
truth.

Run the tests as a normal user, not root: `kit/tailscale.py` refuses root before anything else, so in a root
container four of its tests fail. In a container, use `--user 1000`.
