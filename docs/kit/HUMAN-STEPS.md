# The steps only a person can do

These are the things Claude cannot do for you, in the order they come up. Claude does everything else and tells
you when it needs one of these.

**Status: measured once.** The list comes from the kit's design (after the design decisions of 2026-10-14) and
from primary docs, and it has been clicked through end to end once: the proof run on a fresh VM on the dev machine
(run `20261016T030748Z`, `docs/kit/2026-10-16_proof-run-findings.md`). That run logged
**20** one-time steps: 18 from this list, plus a captcha on each bot's **Authorize**. Under the rule below, a
captcha is one more click inside steps 13 and 16, not a step of its own, so the run's N is **18**. Step 23 (choose
the pack) was added after the run, which makes **19** for a person like that one (who already had Discord). Step
23 has not been measured live yet. Nor has a run with **no Discord** (operator ruling 2026-10-20: Discord is now a
choice at step 23): its 12 is counted from this list, not measured. `python3 kit/record.py human <id>` logs each
step as it happens; a step not on this list is logged as `--unplanned`, and both go into the run record.

**One step = one task you do in one place.** Click counts in brackets are detail and do not change N. The `### id.`
headings are the ids `kit/record.py` accepts; keep them stable.

| Who | Steps | Count |
|---|---|---|
| You, once | 1–23 | **20** (19 if you already have Discord: step 9). **12 with no Discord** (steps 9–16 drop). Plus optional step 3, and conditional steps 19 (a Wi-Fi password, only for Wi-Fi) and 20 (a router range, only for a fixed address) |
| Each friend, once | F1–F10 | **10** (most friends already have F1, and some have F2). **9 with no Discord** (no F2) |
| You, now and then | R1–R3 | 2 (**3 with no Discord**: R3, adding each friend to the server) |

**The promise:** "you do about 20 things once (19 if you already have Discord), each friend does 10; Claude does
the rest." Where 20 comes from, in one line: the kit's first design had 17 one-time steps (1, 2 and 4–18, with step
9), plus two from the design decisions and the one-machine set-up (revoking the Tailscale token, 21, and logging back
in after the test restart, 22), make 19 with Discord sign-up and 18 without; the proof run measured the 18 (its host
had Discord). Choosing the pack (23) came after the run: 20 with step 9, 19 without. Steps 19 and 20 (a Wi-Fi
password, a router range) are conditional and were never in the first design's 17. With no Discord (step 23's second
question, one more click on the same card, so no new step), steps 10–16 drop as well as 9: 19 − 7 = 12, and the
promise is "you do about 12 things once, each friend does 9".

**Settled by the proof run:** a captcha on **Authorize** came up for both bots (it is part of steps 13 and 16);
Message Content for the bridge bot went through Discord's API with no clicks from the person (step 14); the
Tailscale token's expiry picker offers 1 to 90 days (step 18).

**Prompts nobody has counted yet [verify live]:** each one a run meets is logged as `--unplanned` and raises N. The
proof run logged none of these, but one run on one account does not settle them.
- Discord's terms-of-service tick when you create the first app
- a password or 2FA prompt on **Reset Token**
- whether the portal makes you set Install Link to None before it lets Public Bot go off
- Tailscale's onboarding screens after sign-up

**Secrets never go into the chat.** Where a step hands Claude a token, Claude gives you one short command. You
paste it into a **second terminal window** (not the Claude chat), and then paste **only the token** at its hidden
prompt. The token goes straight into a file only you can read, and Claude never sees it. If a paste goes wrong, run
the same command again: it replaces the token.

---

## You, once: the laptop

On the proof VM, steps 1–3 are different: the ISO is attached to the VM rather than written to a stick, the
installer runs over VNC, and there is no BIOS step. Steps 1 and 2 still count.

### 1. Make the Ubuntu USB stick

On another computer, download **Ubuntu Desktop 24.04 LTS or newer** from ubuntu.com/download/desktop and write it
to a USB stick of 12 GB or more (for example with balenaEtcher or Rufus). 24.04 is the version the kit was tested
on. A newer one (the download page offers 26.04 first) should work, but nobody has tested it yet. 22.04 or older
won't work: Claude stops at its first check and tells you why. Use Desktop, not Server: the kit's network
handling is built on NetworkManager, which Server does not use.

### 2. Install Ubuntu on the laptop

Plug the stick in, turn the laptop on, and open its boot menu (often F12, F10, F9 or Esc; it depends on the
maker). Boot from the stick and run the installer (about 14 screens; it needs 25 GB of disk).
- The account you create here is the one the server runs as. Make it the **first** account on the laptop (the
  kit needs user id 1000).
- Pick your timezone here; Claude reads it from the system.

### 3. (Optional) Power on after a power cut

In the laptop's BIOS or UEFI setup, turn on the setting that powers the machine back on when power returns
("Restore on AC power loss" or similar). Not every laptop has it, and the name varies. **Unverified.**

## You, once: Claude

### 4. Get a Claude plan

Buy a Claude **Pro** or **Max** plan at claude.ai. The free plan does not include Claude Code.

### 5. Install Claude Code

On the laptop, open a terminal (Ctrl+Alt+T) and paste this (Ubuntu does not come with `curl` or `git`, so the
first part installs them and asks for your password; Claude needs `git` to fetch the kit):

```sh
sudo apt-get update && sudo apt-get install -y curl git && curl -fsSL https://claude.ai/install.sh | bash
```

The `apt-get update` comes first because a fresh install's package lists are out of date: without it, the install
can fail with "404 Not Found" (it did on the proof run's first try).

### 6. Log in to Claude Code

Run `claude` in the terminal and log in. Either the laptop's browser opens, or you open the printed URL on your
phone or another computer and paste the code back. Then tell Claude: "set up the Minecraft friend server kit from
https://github.com/MrRolie/lazy-minecraft-server".

### 7. Let Claude use sudo

Claude never types passwords, so it asks you once for a standing permission. Claude first fetches the kit, then
prints this command. Open a **second terminal window** (Ctrl+Alt+T again; Claude keeps the first one), paste it,
and enter **your** password when sudo asks:

```sh
sudo bash ~/minecraft-ai-host/kit/sudoers.sh install
```

(The kit's own script checks the permission file with `visudo` before it is put in place, and takes it back out if
sudo would not accept it: a broken sudoers file would lock sudo out.)

It applies only to your account. It stays after setup (design decision, 2026-10-14), the same as on the original
server, so updates and fixes later need nothing from you. To take it back at any time:
`sudo bash ~/minecraft-ai-host/kit/sudoers.sh remove`.

### 8. Accept the Minecraft EULA

Read the Minecraft EULA at https://aka.ms/MinecraftEULA. If you agree, answer **yes** when Claude asks. Claude
then sets `eula_accepted = true` in `kit.toml`. It never sets it without your yes.

## You, once: the pack

### 23. Choose the pack, and Discord or not

Right after Claude checks the laptop, it shows you one card with two questions (2 clicks).

Which game:
- **Vanilla Extended** (recommended): Minecraft as if Mojang had kept adding structures, bosses and places to
  explore, and it still feels like Minecraft. It is the pack the kit's proof run went through end to end.
- **Something different:** a conversation. Claude asks what you want the game to feel like and how many people play
  at once, finds mods on Modrinth with you, and sizes the pack to this laptop's memory. Claude tells you plainly what
  it can promise: the pack passes the kit's checks and starts once in a recorded test, nothing more. It is not the
  path the proof run tested (`docs/kit/CUSTOM-PACK.md`).

Discord or not:
- **Discord:** a Discord server for your group. Game chat shows up in Discord and back, friends add themselves to
  the server, and you get a message when something on the server breaks. It costs you steps 9–16 below, about 15
  minutes.
- **No Discord:** skip all of that. Claude writes you one message to send your friends however you already talk,
  and you add each friend by their Minecraft name (R3). The server still restarts and repairs itself, but **nobody
  is told when something fails**: if it seems down, ask Claude to look.

Vanilla Extended takes a minute. Something different is a longer conversation (not measured yet).

## You, once: Discord

Only if you chose Discord at step 23. With no Discord, skip this whole part (steps 9–16) and go on to Tailscale.

Claude needs two bots in your Discord server: an admin bot that builds the channels, and a separate, weaker bot
that carries chat between the game and Discord. Keeping them apart means the bot that talks to the game all day
cannot wreck the server.

### 9. (Most people skip this) Make a Discord account

If you don't have one: sign up at discord.com and verify your email.

### 10. Create the Discord server

In Discord: **+** (Add a Server) → **Create My Own** → **For me and my friends** → type a name → **Create**
(5 clicks). Only a person can do this: bots cannot create servers.

Don't type anything in the new server yet. Claude clears out the channels Discord made, and it only does that while
they are empty.

### 11. Create the admin bot

Open https://discord.com/developers/applications?new_application=true
- Type a name (for example "Server Admin") and click **Create** (3–4 clicks).
- **Installation** page: set **Install Link** to **None**, then **Save** (2).
- **Bot** page: turn **Public Bot** off, then **Save** (2).
- Still on **Bot**: **Reset Token** → confirm → **Copy** (3). A password or 2FA prompt may appear here
  **[verify live]**.

### 12. Hand Claude the admin token

Claude gives you a short command, `python3 kit/secret.py discord-admin`. Run it in your second terminal, then paste
**only the token** at its hidden prompt (1). It is written to `discord/.env` (only you can read it) and never shows
up in the chat. If the paste goes wrong, run the same command again: it replaces the token.

### 13. Add the admin bot to your server

Open the invite link Claude prints: pick your server → **Continue** → **Authorize** → the captcha, if one appears
(4–5). The proof run got a captcha here.

### 14. Create the chat-bridge bot

The same as step 11, with another name (for example "MC Relay") (about 10 clicks).
- Message Content: Claude switches it on through Discord's API (it worked that way on the proof run, with no clicks
  from the person). If that does not work, Claude asks you to do it by hand: **Bot** → **Privileged Gateway
  Intents** → **Message Content Intent** on → **Save** (2).

### 15. Hand Claude the bridge token

The same as step 12, with `python3 kit/secret.py discord-bridge`, into the bridge's own file (`.env.bridge`) (1).

### 16. Add the bridge bot to your server

Open the second invite link Claude prints: **Continue** → **Authorize** → the captcha, if one appears (3–4). It is
pinned to your server, so there is nothing to pick. The proof run got a captcha here too.

## You, once: Tailscale

Tailscale is how your friends reach the server without opening your home network to the internet.

### 17. Sign in to Tailscale

Claude runs `sudo tailscale up --qr` and a QR code appears. Scan it with your phone (or open the printed link).
Sign up with Google, Microsoft, GitHub, Apple or a passkey (there is no email sign-up), and choose **Personal
use**. This creates your Tailscale network and logs the laptop into it.

### 18. Make a Tailscale API token and hand it to Claude

In the Tailscale admin console: **Settings** → **Keys** → generate an **API access token** (the picker offers 1
to 90 days; the shortest is fine). The exact button label is **[verify live]**. Then run the short command Claude
gives you, `python3 kit/secret.py tailscale`, in your second terminal, and paste **only the token** at its hidden
prompt (it starts with `tskey-api-`), not the command again. Claude uses it once, to set up friend access and make
the share link, then deletes it.

### 21. Revoke the Tailscale API token

When Claude says it is done with the token: in the console, **Settings** → **Keys**, find the token and
**Revoke** it (design decision, 2026-10-14: used once, then deleted and revoked). **[verify live]** for the labels.

## You, once: the test restart

### 22. Log back in after the test restart

Near the end, Claude restarts the laptop once to prove the server comes back by itself. Claude Code runs on the
same laptop, so it closes too. When the laptop is back: log in, open a terminal, and run `claude --continue`.

## You, once: network (only if needed)

### 19. (Conditional) Store the Wi-Fi password

Only when the server uses Wi-Fi: a laptop with no network cable, or a cable plus Wi-Fi as a backup (only if you
ask for it). The server must reach Wi-Fi before anyone logs in, so the password has to be stored for the whole
machine, not only for your account. Claude prints a command (`sudo nmcli device wifi connect <name> --ask`); paste
it in your second terminal and type the Wi-Fi password there. If the laptop already joined that Wi-Fi from the
desktop's menu, the password is stored for your account only: Claude then gives you the command the server setup
printed (it stores the same password for the whole machine), again for your second terminal. A laptop on a cable
alone skips this.

### 20. (Conditional) Tell Claude your router's address range

Only if you want the server on a fixed address (`net.mode = "static"`). By default the router hands the laptop its
address and nothing is needed here. A fixed address must sit **outside** the range your router hands out, or it
can clash with another device (this took the original server offline for 8 hours). Open your router's admin page
(often http://192.168.1.1 or http://192.168.0.1; it is on a sticker on most routers), find the **DHCP** settings,
and tell Claude the start and end of the range.

## Each friend, once

With Discord, Claude posts all of this in your Discord's `#setup` channel. With no Discord, it is all in the
message Claude writes for you at the end (`~/minecraft-friend-message.txt`): you send it to your friends however you
talk, with the pack file it names. Most of it is a one-time setup on the friend's own computer.

### F1. Own Minecraft Java Edition

A Microsoft account that owns Minecraft: Java Edition. Most friends already have it. A friend under 16 needs a
parent's account with multiplayer allowed.

### F2. Join the Discord server

Only with Discord; with no Discord, friends skip it. Open the invite link you send them, and make a Discord
account if needed. Claude's invite link lasts 7 days, so a
leaked one stops working; ask Claude for a fresh one when it runs out.

### F3. Install Tailscale

From tailscale.com/download.

### F4. Sign in to Tailscale

With any identity provider. The friend must own their own Tailscale network: a member seat on a work network
cannot accept a share.

### F5. Accept the share link

Open the share link in `#setup` (with no Discord: in your message) and click **Accept**.

### F6. Install a launcher

Prism Launcher or the Modrinth App.

### F7. Sign the launcher into Microsoft

Needed when the game first launches, not at import.

### F8. Import the pack

Download the `.mrpack` from `#setup` (with no Discord: the file you sent with your message), then:
- Prism: **Add Instance** → **Import** → **Browse** → **OK**.
- Modrinth App: **Create new instance** → **Upload a modpack**. It shows an "unknown modpack" warning first;
  that is expected.

### F9. Give the game 4–6 GB of memory

In the launcher's instance settings (about 4 clicks).

### F10. Whitelist yourself

In Discord, run `/whitelist <your Minecraft name>`. With no Discord: send your Minecraft name to the person who
runs the server, and they add you (R3). After that, the server is already in the game's **Multiplayer** list: one
click to join.

## You, now and then

### R1. Make a fresh share link for a new friend

Share links that nobody used expire after 30 days. When a new friend arrives after that: Tailscale admin console
→ **Machines** → the server → **Share** → **Copy share link**. Then, in your second terminal, run
`python3 kit/secret.py share-link` from the server's folder and paste only the link at its prompt (it will not
show). It replaces the old link in `discord/.env`. Claude updates `#setup`; with no Discord, Claude writes your
friend message again (`python3 kit/friends.py`), and you send the new one.

### R2. Log in to Claude Code again

When Claude Code says your login is about to expire (it warns 3 days ahead), run `claude` and log in again as in
step 6.

### R3. (No Discord) Add a friend to the server

With Discord, friends add themselves (F10). With no Discord, the server only lets in names you add. When a friend
sends you their Minecraft name, tell Claude "whitelist <their name>", and it tells you when it is done. Or do it
yourself, in a terminal, with Steve replaced by their Minecraft name:

```sh
cd ~/minecraft-ai-host && docker compose exec minecraft rcon-cli whitelist add Steve
```
