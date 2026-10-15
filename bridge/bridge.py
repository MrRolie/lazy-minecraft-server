#!/usr/bin/env python3
"""The Discord bridge: game chat and events <-> the friend guild, through the mcve file contract only.

events.jsonl -> #game-chat / #discoveries / #coords / presence; #game-chat -> inbox `say`;
/whitelist -> inbox `whitelist_add`. No RCON, no server commands, no Minecraft log. The logic lives in
core.py (pure, tested without Discord); this file is the discord.py glue.

Files: /mcve/out (read-only mount; the mod's events.jsonl) and /mcve/in (inbox.jsonl, bridge.offset).
Fence: /run/mc-host (the host's, mounted read-only) must hold bridge-fence.ok, the egress fence's stamp, or the
bridge refuses to start, whatever started it (mc-server.service, a restart policy, a hand-run `up`).
Health: /tmp/ready is touched on every poll once ready; the compose healthcheck wants it fresh.
Env: DISCORD_BRIDGE_TOKEN (never logged), DISCORD_GUILD_ID; MCVE_DIR (default /mcve);
FATAL_HOLD_SECONDS (default 600): how long a fatal start error waits before exiting, so a restart loop
under `restart: unless-stopped` can't burn Discord's 1000-logins-a-day budget.
"""
import asyncio
import logging
import os
import signal
import sys
import time

import discord
from discord import app_commands

import core
from core import esc

log = logging.getLogger("bridge")
discord.VoiceClient.warn_nacl = discord.VoiceClient.warn_dave = False  # text only: no voice, no warning
CHANNELS = ("game-chat", "discoveries", "coords", "ops-log")
POLL = 1.0
FENCE_STAMP = "/run/mc-host/bridge-fence.ok"
READY = "/tmp/ready"


class Fatal(Exception):
    """A start condition the operator must fix (missing channel, permission, scope)."""


def intents():
    i = discord.Intents.none()
    i.guilds = True
    i.guild_messages = True
    i.message_content = True  # privileged: Developer Portal → Bot → Message Content Intent
    return i


def activity(p):
    kind, text = p
    return discord.Game(name=text) if kind == "playing" else discord.CustomActivity(name=text)


def resolve_channels(guild):
    """Name -> text channel for CHANNELS, each one the bot can view and send in; else Fatal naming all gaps."""
    if guild is None:
        raise Fatal("the bot is not in guild DISCORD_GUILD_ID (invite it with the URL in bridge/README.md)")
    found, problems = {}, []
    for name in CHANNELS:
        ch = discord.utils.get(guild.text_channels, name=name)
        if ch is None:
            problems.append(f"#{name} is missing (run discord/sync.py)")
            continue
        perms = ch.permissions_for(guild.me)
        lacking = [p for p in ("view_channel", "send_messages") if not getattr(perms, p)]
        if lacking:
            problems.append(f"#{name}: bot lacks {', '.join(lacking)}"
                            + (" (add its user id to ADMIN extra_bots, then sync)" if name == "ops-log" else ""))
        found[name] = ch
    if problems:
        raise Fatal("; ".join(problems))
    return found


class Bridge(discord.Client):
    def __init__(self, guild_id, out_dir, in_dir, clock=time.time, ready_path=READY):
        """out_dir: the mod's files, only ever read; in_dir: the bridge's own (inbox + offset). Never mixed."""
        super().__init__(intents=intents(), allowed_mentions=discord.AllowedMentions.none())
        self.guild_id, self.clock, self.ready_path = guild_id, clock, ready_path
        self.events_path = os.path.join(out_dir, "events.jsonl")
        self.tailer = core.Tailer(self.events_path, os.path.join(in_dir, "bridge.offset"))
        self.inbox = core.Inbox(os.path.join(in_dir, "inbox.jsonl"))
        self.whitelister = core.Whitelister(self.inbox)
        self.say_limit = core.RateLimiter(1, 2)
        self.watchdog = core.Watchdog(started=clock())
        self.channels = {}
        self.server_state = None      # last `server` event: "started" | "stopping" | None (unknown)
        self.status_presence = None   # what the last `status` said, restored when the watchdog recovers
        self.shown_presence = None
        self.fatal = None
        self._pump = None
        self.tree = app_commands.CommandTree(self)

    # ── lifecycle ──
    async def setup_hook(self):
        guild = discord.Object(id=self.guild_id)

        @app_commands.command(name="whitelist", description="Put your Minecraft name on the server whitelist")
        @app_commands.describe(minecraft_name="Your Minecraft Java username")
        @app_commands.guild_only()
        async def whitelist(interaction: discord.Interaction, minecraft_name: str):
            await self.on_whitelist(interaction, minecraft_name)

        self.tree.add_command(whitelist, guild=guild)

    async def on_ready(self):
        if self.channels or self.fatal:  # on_ready fires again after a reconnect
            return
        try:
            channels = resolve_channels(self.get_guild(self.guild_id))
        except Fatal as e:
            await self.die(str(e))
            return
        try:  # after the channel check, so a permissions fatal never touches the command API
            await self.tree.sync(guild=discord.Object(id=self.guild_id))  # guild-scoped: appears at once
        except discord.Forbidden as e:
            await self.die(f"registering /whitelist failed ({e.text}): invite the bot with the "
                           "applications.commands scope (bridge/README.md)")
            return
        self.channels = channels
        log.info("ready as %s; channels %s", self.user, ", ".join(f"#{n}" for n in self.channels))
        self._pump = asyncio.create_task(self.pump())

    async def die(self, msg):
        self.fatal = msg  # main() logs it and holds before exiting
        await self.close()

    async def pump(self):
        while not self.is_closed():
            try:
                await self.pump_once()
            except Exception:
                log.exception("pump iteration failed")
            await asyncio.sleep(POLL)

    async def pump_once(self):
        events = await asyncio.to_thread(self.tailer.read_new)
        await asyncio.to_thread(self.tailer.save)  # BEFORE posting: a crash may drop a line, never re-post one
        await self.handle_batch(events)
        await self.tick_watchdog()
        with open(self.ready_path, "a"):  # the healthcheck's heartbeat: ready, and the pump is turning
            os.utime(self.ready_path)

    # ── events -> Discord ──
    async def handle_batch(self, events):
        """Post a batch. Backlog chat/game/server (older than STALE_EVENT, e.g. after an outage) is not
        posted; late whitelist results are summed into ONE #ops-log line."""
        now, late, stale = self.clock(), [], 0
        for ev in events:
            try:
                backlog = core.is_backlog(ev, now)
                stale += backlog
                r = await self.handle_event(ev, post=not backlog)
                if r is not None:
                    late.append(r)
            except Exception:
                log.exception("event failed: %.200r", ev)
        if stale:
            log.info("dropped %d backlog chat/game/server events older than %ds", stale, core.STALE_EVENT)
        if late:
            shown = ", ".join(f"`{esc(a['name'])}` {'ok' if a['ok'] else 'refused'} ({esc(a['detail'])})"
                              for a in late[:10])
            more = f" and {len(late) - 10} more" if len(late) > 10 else ""
            await self.post("ops-log", f"{len(late)} late whitelist result(s), no request waiting: {shown}{more}")

    async def handle_event(self, ev, post=True):
        """Act on one event; returns an unmatched whitelist_result (for the batch summary), else None.
        post=False (backlog) still updates state (server up/down, presence) and sends nothing but the #ops-log
        line for an unclean restart."""
        r = core.route(ev)
        if r is None:
            return None
        kind, a, *rest = r
        if kind == "post":
            if post:
                await self.post(a, rest[0])
        elif kind == "server":
            # Posted even from a backlog: a crash is the event most likely to sit in one, and #ops-log is the record.
            if core.unclean_restart(self.server_state, a):
                await self.post("ops-log", core.unclean_restart_text(ev))
            self.server_state = a
            if post:
                await self.post("game-chat", core.SERVER_TEXT[a])
        elif kind == "presence":
            self.status_presence = a
            if not self.watchdog.offline:
                await self.set_presence(a)
        elif kind == "whitelist_result" and not self.whitelister.resolve(a):
            return a
        return None

    async def post(self, channel, text):
        await self.channels[channel].send(core.clip(text), allowed_mentions=discord.AllowedMentions.none())

    async def set_presence(self, p):
        if p != self.shown_presence:  # presence updates are rate-limited; send changes only
            self.shown_presence = p
            await self.change_presence(activity=activity(p) if p else None)

    async def tick_watchdog(self):
        # Mid-rotation the live file is briefly absent: the Watchdog keeps the last mtime it saw for that.
        st = core.lstat_regular(self.events_path)
        change = self.watchdog.check(self.clock(), st.st_mtime if st else None)
        if change == "offline":
            await self.set_presence(core.OFFLINE)
            await self.post("ops-log", f"⚠️ events.jsonl silent for over {core.STALE_AFTER // 60} min: "
                                       f"server offline? (last server event: {self.server_state or 'none seen'})")
        elif change == "recovered":
            await self.set_presence(self.status_presence)

    def server_down(self):
        return self.watchdog.offline or self.server_state == "stopping"

    # ── Discord -> game ──
    async def on_message(self, message):
        if (message.author.bot or message.webhook_id or message.guild is None
                or not self.channels or message.channel.id != self.channels["game-chat"].id
                or message.type not in (discord.MessageType.default, discord.MessageType.reply)):
            return
        if self.server_down():
            log.info("server down: not relaying a message from %s", message.author.id)
            return
        if not self.say_limit.allow(message.author.id):
            log.info("say rate limit: dropped a message from %s", message.author.id)
            return
        rec = core.say_record(message.author.display_name, message.clean_content,
                              [a.content_type for a in message.attachments])
        if rec:
            await asyncio.to_thread(self.inbox.append, rec)

    async def on_whitelist(self, interaction, minecraft_name):
        user = interaction.user
        if interaction.guild_id != self.guild_id:
            await interaction.response.send_message("This only works in the friend server.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)  # the answer can take up to 30 s
        try:
            reply, outcome = await self.whitelister.request(
                user.id, minecraft_name, f"{user.name} ({user.id})", server_down=self.server_down())
        except OSError as e:
            reply, outcome = "⚠️ Something broke on the bridge. The admin has been told.", f"inbox write failed: {e}"
        await interaction.followup.send(reply, ephemeral=True)
        await self.post("ops-log", f"/whitelist “{esc(minecraft_name[:40])}” by {user.mention}: {outcome}")


# ── entry point ──────────────────────────────────────────────────────────────

def paths(mcve):
    """(out_dir, in_dir): out/ is the mod's (mounted read-only), in/ is the bridge's."""
    return os.path.join(mcve, "out"), os.path.join(mcve, "in")


def hold_then(code, msg):
    log.error("FATAL: %s", msg)
    hold = int(os.environ.get("FATAL_HOLD_SECONDS", "600"))
    if hold:
        log.error("holding %ds before exit (restart-loop guard)", hold)
        time.sleep(hold)
    return code


def main():
    discord.utils.setup_logging(level=logging.INFO)
    if not os.path.isfile(FENCE_STAMP):  # no env override: .env.bridge must not be able to waive the fence
        return hold_then(2, f"no egress fence: {FENCE_STAMP} is missing (mc-bridge-fence.service on the host); "
                            "refusing to start unfenced")
    token = os.environ.get("DISCORD_BRIDGE_TOKEN", "").strip()
    gid = os.environ.get("DISCORD_GUILD_ID", "").strip()
    out_dir, in_dir = paths(os.environ.get("MCVE_DIR", "/mcve"))
    if not token:
        return hold_then(2, "DISCORD_BRIDGE_TOKEN is not set (it comes from .env.bridge via compose env_file)")
    if not gid.isdigit():
        return hold_then(2, "DISCORD_GUILD_ID is not set to a numeric guild id (.env.bridge)")
    if not (os.path.isdir(in_dir) and os.access(in_dir, os.W_OK)):
        return hold_then(2, f"{in_dir} is not a writable directory (the bind mount of mc_data/mcve/in)")
    if not os.path.isdir(out_dir):
        return hold_then(2, f"{out_dir} is not a directory (the read-only bind mount of mc_data/mcve/out)")
    try:
        client = Bridge(int(gid), out_dir, in_dir)
    except core.StateError as e:  # an empty or corrupt bridge.offset: loud, and held like any fatal
        return hold_then(2, str(e))

    async def run():
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, lambda: asyncio.ensure_future(client.close()))
        async with client:
            await client.start(token)

    try:
        asyncio.run(run())
    except discord.LoginFailure as e:
        return hold_then(3, f"Discord login failed: {e} (check DISCORD_BRIDGE_TOKEN in .env.bridge)")
    except discord.PrivilegedIntentsRequired:
        return hold_then(3, "Message Content intent is off: Developer Portal → Bot → Message Content Intent")
    if client.fatal:
        return hold_then(2, client.fatal)
    return 0


if __name__ == "__main__":
    sys.exit(main())
