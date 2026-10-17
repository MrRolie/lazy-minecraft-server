# Kit proof run, 2026-10-16: findings

The first end-to-end run of `docs/kit/KIT.md`, on a fresh Ubuntu 24.04 VM (`mc-test`, 16 GB, wired,
no battery), driven by Claude Code with the host owner doing the human steps. The run record itself
(`kit/runs/20261016T030748Z/record.md`) is gitignored and stays on that host; this page carries what matters for
the repo.

**Outcome:** complete, with accepted failures. **N = 20** one-time human steps (18 from HUMAN-STEPS.md, plus 2
not on the list: a captcha on each bot's Authorize). Steps 3, 9, 19 and 20 were not needed. Every phase P0–P12
ran; P9 was cut short at the host's request and P11 ended in a failure the host accepted (both below).

Proven live for the first time: the whole Discord bootstrap (`discover`, `intents --apply`: Message Content set
through the API, no hand step), the Tailscale setup against a fresh tailnet, the supervised reboot (`mc-server`
came back unattended, `check.sh --no-ssh` all pass), and **Discord -> game chat**
(`[Discord] Mike: hello from discord` in the server log), which KIT.md P11 had as unproven even on the original server.

## From the host owner (their words, 2026-10-16)

> "I don't think it should include the story channel and the server should be fully cleaned out by you before
> setting up the rest."

1. **Drop `#the-story` from kit guilds.** On a kit instance it is created empty (KIT.md "What is still open": no
   home outside git for a kit story), so friends see an empty channel with no purpose. Suggest: `profile = "kit"`
   leaves it out of `discord/layout.toml`.
   **Fixed:** `discord/sync.py` `PROFILES` gives the kit profile `drop = ("the-story",)`, so a kit guild never gets
   `#the-story` (asus-node drops nothing and is unchanged). `discord/content/kit/story.md` is gone.
2. **Clean the fresh guild before P6's sync.** Discord's "For me and my friends" template makes `Text Channels` /
   `Voice Channels` categories, `#general` and a `General` voice channel. sync leaves unlisted channels alone, so
   the guild ends up with two `#general`s. Suggest: P6 (or `bootstrap.py`) deletes the template's channels in a
   guild that is minutes old and empty (only the owner and the two bots), as a dry-run-first, listed action,
   refusing if any channel has messages.
   **Fixed, not yet run live:** `python3 discord/bootstrap.py clean [--apply]`, first in KIT.md P6. It deletes what
   `discord/layout.toml` does not list, and refuses unless the only members are the owner and the two bots and the
   channels hold only their join messages. Every API path is checked against Discord's docs, not against a real
   guild.

## Seen during the run

3. **Tailscale `GET /device/{id}/device-invites` returns `null`, not `[]`,** for a device with no invites.
   `kit/tailscale.py` refused ("not a list"). Fixed in the same change as this page (null -> []) with a test
   (`test_a_device_with_no_invites_answers_null_and_one_is_made`); 76/76 pass.
   **Fixed:** the same one-line change and test, carried onto the fix branch.
4. **The token hand-off commands for steps 12 and 15 append (`>>`).** The person re-ran step 12 after a bad first
   paste, expecting an overwrite; `discord/.env` got a mangled `DISCORD_TOKEN` line plus a second one, and
   `bootstrap.py discover` refused. Claude kept the last clean line. Suggest: the commands replace the key's line
   (or the tool takes the last one and says so).
   **Fixed:** `python3 kit/secret.py discord-admin | discord-bridge | tailscale` (KIT.md P1, P3; HUMAN-STEPS 12, 15,
   18). It replaces the key's line, never appends, so running it again after a bad paste is safe.
5. **Step 18: the command itself got pasted at the hidden prompt** (the file held the command text, no token).
   `tailscale.py` refused without showing it. Suggest: HUMAN-STEPS says "paste only the token (it starts with
   tskey-api-)" next to the command, and the prompt text says the same.
   **Fixed:** `kit/secret.py` refuses anything not shaped like the token (whitespace or a shell character means a
   command was pasted) and writes nothing. Its prompt and HUMAN-STEPS step 18 say "paste only the token (it starts
   with `tskey-api-`)".
6. **Captcha on Authorize: both bots** (steps 13 and 16); logged as unplanned.
   **Fixed (the page):** HUMAN-STEPS steps 13 and 16 now include the captcha as one more click. Under the page's
   rule (one step = one task in one place) it is not a step of its own, so N for this run is 18.
7. **Step 18's expiry picker offers 1-90 days** (the person chose 1 day).
   **Fixed (the page):** HUMAN-STEPS step 18 says so.
8. **Test runs need a non-root user:** pytest in a root container fails 4 redaction tests because `tailscale.py`
   refuses root first (correct behaviour). The host has no pytest/venv; run tests with `--user 1000`.
   **Fixed (the page):** `kit/README.md` says to run the tests as a normal user (`--user 1000` in a container).
9. **P9 cut short at the host's request** (proof VM). The overworld reached 46% in about 60 min (~1100 chunks/min,
   steady, no G1 Compaction pauses); the Nether and End were never pre-generated. The P9 gates still pass
   (they prove the borders, not 100%), so the record says `pass-observed`; the observe line has the detail.
   Suggest: a P9 gate on Chunky's completion, so a run that is cut short ends in a failure the person accepts.
   **Fixed:** `deploy/world.sh done overworld=3000 the_nether=1500 the_end=2000` is a P9 gate. It reads Chunky's own
   task files and passes only when all three rings finished, so a run cut short now ends in a failure the person
   accepts (`end P9 --accept`). Not yet run on a real world.
10. **The Nether and End borders reverted after a restart.** They were set by `world.sh ring` and Chunky was then
    cancelled within seconds (`chunky cancel` + `confirm`). The backup taken next still had the old Nether
    border, and after `up -d minecraft` both borders were 59999968 again; the overworld's 6000 survived. Not
    reproduced: a border set by rcon (`execute in ... run worldborder set`) was captured by `backup.sh`'s flush,
    and survived a `docker compose stop` (clean save in 3 s) and the next start. The border gate caught it.
    Worth a rehearsal on a copy: set a ring, cancel Chunky at once, flush, restart.
    **Open:** not reproduced, and no code change. P9's border gate still catches it, as it did here. The rehearsal
    on a copy (set a ring, cancel Chunky at once, flush, restart) is still owed.
11. **P11's `kit-check-` gate looks back only 10 minutes** (`docker compose logs --since 10m`). The host came back
    to P11 about 12 hours after the bridge started, and then chose their own text instead of the phrase; the
    gate failed and the host accepted it, with a second gate proving their actual message arrived. Suggest: the
    gate reads since the phase's `begin`, and KIT.md tells the person the exact phrase is what gets checked.
    **Fixed:** the gate reads since the phase's own begin (`python3 kit/record.py began P11`), and KIT.md P11 tells
    the person that the exact phrase is what gets checked.
12. **Game -> Discord was not confirmed by eye.** The mod wrote the host's join, chat, coords, discovery, death
    and leave to `events.jsonl`; the host was not asked to confirm they showed up in `#game-chat`, `#coords` and
    `#discoveries`.
    **Fixed (the page), not yet seen live:** KIT.md P11 now asks the person to look in `#game-chat`, `#coords` and
    `#discoveries` for their join, chat, saved place, find and death, and records what they saw with `observe`.

## After the run

Two more came from the host owner and the run record, and are fixed in the same change:

- **The pack was never offered.** "the claude session never offered to research mods for a different experience it
  went straight to vanilla extended". **Fixed:** right after P0, KIT.md has Claude ask with a card: Vanilla
  Extended (recommended, the pack this run tested) or something different (`docs/kit/CUSTOM-PACK.md`). It is
  HUMAN-STEPS step 23, which `finish` requires. Not yet run live.
- **HUMAN-STEPS step 5 failed on the fresh install** (attempt 1): `apt-get install` hit a 404 because the package
  lists were stale. **Fixed:** step 5 runs `sudo apt-get update` first.
