package net.vanillaextended.vecommands;

import java.io.IOException;
import java.nio.file.Path;
import java.util.LinkedHashSet;
import java.util.List;
import java.util.Set;
import net.fabricmc.api.DedicatedServerModInitializer;
import net.fabricmc.fabric.api.command.v2.CommandRegistrationCallback;
import net.fabricmc.fabric.api.event.lifecycle.v1.ServerLifecycleEvents;
import net.fabricmc.fabric.api.message.v1.ServerMessageEvents;
import net.fabricmc.fabric.api.networking.v1.ServerPlayConnectionEvents;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.level.ServerPlayer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Server-only half of the mcve file contract (mod/CONTRACT.md): writes &lt;server dir&gt;/mcve/events.jsonl, reads
 * &lt;server dir&gt;/mcve/inbox.jsonl. All file I/O stays in that directory and runs off the server thread.
 */
public final class VeCommands implements DedicatedServerModInitializer {
	static final Logger LOG = LoggerFactory.getLogger("ve-commands");
	private EventLog events;
	private Jobs jobs;
	private InboxActions actions;

	@Override
	public void onInitializeServer() {
		CommandRegistrationCallback.EVENT.register((dispatcher, context, selection) ->
				PlayerCommands.register(dispatcher, () -> events));

		ServerLifecycleEvents.SERVER_STARTED.register(this::start);
		ServerLifecycleEvents.SERVER_STOPPING.register(this::stopping);
		ServerLifecycleEvents.SERVER_STOPPED.register(server -> {
			if (events != null) events.close();
		});

		ServerMessageEvents.CHAT_MESSAGE.register((message, sender, params) -> {
			if (events == null) return;
			String player = sender.getGameProfile().name();
			String text = message.signedContent();
			events.emit("chat", Events.chat(player, text));
		});
		ServerMessageEvents.GAME_MESSAGE.register((server, message, overlay) -> {
			// Our own [Discord] broadcast is a game message too; echoing it back would loop it into Discord.
			if (events == null || overlay || InboxActions.BROADCASTING.get()) return;
			String text = message.getString();
			events.emit("game", Events.game(text));
		});
		// /say and /me arrive here, not as game messages. Contract v1 has no event for them: logged, not forwarded.
		ServerMessageEvents.COMMAND_MESSAGE.register((message, source, params) ->
				LOG.info("COMMAND_MESSAGE from {} not forwarded (no contract event): {}",
						source.getTextName(), message.signedContent()));

		ServerPlayConnectionEvents.JOIN.register((handler, sender, server) -> status(server, handler.player, true));
		ServerPlayConnectionEvents.DISCONNECT.register((handler, server) -> status(server, handler.player, false));
	}

	private void start(MinecraftServer server) {
		// out/ is the mod's (the bridge mounts it read-only); in/ is the bridge's (the mod only reads inbox.jsonl).
		Path mcve = server.getServerDirectory().resolve("mcve").toAbsolutePath().normalize();
		Path out = mcve.resolve("out");
		Path in = mcve.resolve("in");
		try {
			if (!McveDirs.prepare(mcve)) {
				LOG.error("mcve/, mcve/out/ and mcve/in/ must be real directories; ve-commands is disabled");
				return;
			}
		} catch (IOException e) {
			LOG.error("cannot create {}; ve-commands is disabled: {}", mcve, e.toString());
			return;
		}
		long rotate = Long.getLong("vecommands.rotateBytes", EventLog.DEFAULT_ROTATE_BYTES);
		events = new EventLog(out, rotate);
		events.emit("server", Events.server("started"));

		actions = new InboxActions(server, events);
		Inbox inbox = new Inbox(in, out, actions);
		// The status beat has its own Ticker (Jobs), so a slow Mojang lookup on the inbox thread never delays it. Not a
		// tick listener: an empty server pauses ticking (pause-when-empty-seconds) but still runs its task queue, and
		// the bridge needs the heartbeat most while nobody is on.
		jobs = Jobs.start(inbox::poll, 1000, () -> server.execute(() -> status(server, null, false)), 60_000);
		LOG.info("mcve contract v1 at {} (inbox offset {}, rotate at {} bytes)", mcve, inbox.offset(), rotate);
	}

	private void stopping(MinecraftServer server) {
		if (jobs != null) jobs.stop();
		if (actions != null) actions.stop();
		if (events != null) events.emit("server", Events.server("stopping"));
	}

	/**
	 * Emits a status event (server thread). On a join or leave, {@code who} is forced in or out of the list, since
	 * the player list may not have been updated yet when the connection event fires.
	 */
	private void status(MinecraftServer server, ServerPlayer who, boolean joined) {
		if (events == null) return;
		Set<String> online = new LinkedHashSet<>();
		for (ServerPlayer p : server.getPlayerList().getPlayers()) online.add(p.getGameProfile().name());
		if (who != null) {
			if (joined) online.add(who.getGameProfile().name());
			else online.remove(who.getGameProfile().name());
		}
		events.emit("status", Events.status(List.copyOf(online), server.getMaxPlayers()));
	}
}
