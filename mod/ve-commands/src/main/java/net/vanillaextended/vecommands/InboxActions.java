package net.vanillaextended.vecommands;

import com.google.gson.JsonElement;
import java.util.Optional;
import java.util.concurrent.CompletableFuture;
import java.util.concurrent.ExecutionException;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.TimeoutException;
import net.minecraft.ChatFormatting;
import net.minecraft.network.chat.Component;
import net.minecraft.server.MinecraftServer;
import net.minecraft.server.players.NameAndId;
import net.minecraft.server.players.UserWhiteList;
import net.minecraft.server.players.UserWhiteListEntry;

/**
 * The only two things the inbox can make happen: a [Discord] broadcast and a whitelist add. Both world-facing
 * steps run on the server thread via the server executor; the Mojang profile lookup runs on the inbox thread.
 */
final class InboxActions implements Inbox.Handler {
	/** True while our own broadcast is in flight, so the GAME_MESSAGE listener does not echo it back. */
	static final ThreadLocal<Boolean> BROADCASTING = ThreadLocal.withInitial(() -> false);

	private final MinecraftServer server;
	private final EventLog events;
	private volatile boolean stopped;

	InboxActions(MinecraftServer server, EventLog events) {
		this.server = server;
		this.events = events;
	}

	void stop() {
		stopped = true;
	}

	@Override
	public void say(JsonElement id, String author, String text) {
		Component msg = Component.empty()
				.append(Component.literal("[Discord] ").withStyle(ChatFormatting.BLUE))
				.append(Component.literal(author + ": " + text));
		onServer(() -> {
			BROADCASTING.set(true);
			try {
				server.getPlayerList().broadcastSystemMessage(msg, false);
			} finally {
				BROADCASTING.set(false);
			}
		});
	}

	@Override
	public void whitelistAdd(JsonElement id, String name, String requestedBy) {
		WhitelistFlow.Outcome outcome = WhitelistFlow.decide(name, this::listedName, this::lookup);
		if (outcome instanceof WhitelistFlow.Done(WhitelistFlow.Result r)) {
			result(id, r.name(), r.ok(), r.detail());
			return;
		}
		NameAndId who = (NameAndId) ((WhitelistFlow.Add<?>) outcome).profile();
		onServer(() -> {
			UserWhiteList list = server.getPlayerList().getWhiteList();
			if (list.isWhiteListed(who)) {
				result(id, who.name(), true, "already");
				return;
			}
			server.services().nameToIdCache().add(who);
			list.add(new UserWhiteListEntry(who)); // StoredUserList.add saves whitelist.json
			VeCommands.LOG.info("{}", whitelistedLine(who.name(), who.id(), requestedBy));
			result(id, who.name(), true, "added");
		});
	}

	/** The audit line for an add. {@code requestedBy} comes from the bridge: escaped, so it cannot forge log lines. */
	static String whitelistedLine(String name, Object id, String requestedBy) {
		return "whitelisted " + name + " (" + id + ") at the request of " + Text.forLog(requestedBy, 64);
	}

	/** The stored whitelist name equal to {@code name} ignoring case, read on the server thread (bounded wait). */
	private Optional<String> listedName(String name) {
		CompletableFuture<Optional<String>> found = new CompletableFuture<>();
		onServer(() -> {
			try {
				String hit = null;
				for (String listed : server.getPlayerList().getWhiteListNames()) {
					if (listed.equalsIgnoreCase(name)) hit = listed;
				}
				found.complete(Optional.ofNullable(hit));
			} catch (Throwable t) {
				found.completeExceptionally(t);
			}
		});
		try {
			return found.get(10, TimeUnit.SECONDS);
		} catch (InterruptedException e) {
			Thread.currentThread().interrupt();
			return Optional.empty();
		} catch (ExecutionException | TimeoutException e) {
			VeCommands.LOG.warn("whitelist name check failed ({}); asking Mojang instead", e.toString());
			return Optional.empty();
		}
	}

	/** Online mode: the server's own Mojang profile repository (an HTTP call, so never on the server thread). */
	private Optional<NameAndId> lookup(String name) {
		return server.usesAuthentication()
				? server.services().profileRepository().findProfileByName(name).map(NameAndId::new)
				: Optional.of(NameAndId.createOffline(name));
	}

	@Override
	public void whitelistRateLimited(JsonElement id, String name) {
		result(id, Text.truncate(name, 32), false, "rate limited");
	}

	private void onServer(Runnable r) {
		if (!stopped) server.execute(r);
	}

	private void result(JsonElement id, String name, boolean ok, String detail) {
		events.emit("whitelist_result", Events.whitelistResult(id, name, ok, detail));
	}
}
