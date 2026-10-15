package net.vanillaextended.vecommands;

import com.mojang.brigadier.CommandDispatcher;
import com.mojang.brigadier.arguments.StringArgumentType;
import com.mojang.brigadier.context.CommandContext;
import java.util.function.Supplier;
import net.minecraft.ChatFormatting;
import net.minecraft.commands.CommandSourceStack;
import net.minecraft.commands.Commands;
import net.minecraft.core.BlockPos;
import net.minecraft.network.chat.Component;
import net.minecraft.server.level.ServerLevel;
import net.minecraft.server.level.ServerPlayer;

/**
 * /discovery &lt;text...&gt; and /coords &lt;name...&gt;, open to every player. Both use only vanilla argument types
 * (brigadier greedy string), so vanilla clients get them through the server's command tree.
 */
final class PlayerCommands {
	private static final Cooldowns COOLDOWNS = new Cooldowns(10_000, System::nanoTime);

	private PlayerCommands() {}

	static void register(CommandDispatcher<CommandSourceStack> dispatcher, Supplier<EventLog> events) {
		dispatcher.register(Commands.literal("discovery")
				.requires(Commands.hasPermission(Commands.LEVEL_ALL))
				.then(Commands.argument("text", StringArgumentType.greedyString())
						.executes(c -> run(c, events, "discovery", "text", 200, "📌 Posted to #discoveries"))));
		dispatcher.register(Commands.literal("coords")
				.requires(Commands.hasPermission(Commands.LEVEL_ALL))
				.then(Commands.argument("name", StringArgumentType.greedyString())
						.executes(c -> run(c, events, "coords", "name", 60, "📌 Saved to #coords"))));
	}

	private static int run(CommandContext<CommandSourceStack> c, Supplier<EventLog> events, String command,
			String field, int max, String confirmation) {
		CommandSourceStack source = c.getSource();
		String value = Text.clean(StringArgumentType.getString(c, field));
		if (value.isEmpty() || Text.length(value) > max) {
			source.sendFailure(Component.literal("/" + command + " takes 1 to " + max + " characters."));
			return 0;
		}
		EventLog log = events.get();
		if (log == null) {
			source.sendFailure(Component.literal("Not ready yet, try again in a moment."));
			return 0;
		}
		ServerPlayer player = source.getPlayer();
		String name = player != null ? player.getGameProfile().name() : "Server";
		String who = player != null ? player.getStringUUID() : source.getTextName();
		long wait = COOLDOWNS.tryAcquire(command + ":" + who);
		if (wait > 0) {
			source.sendSuccess(() -> Component.literal("Easy there! You can use /" + command + " again in " + wait + "s.")
					.withStyle(ChatFormatting.GRAY), false);
			return 0;
		}
		ServerLevel level = source.getLevel();
		BlockPos pos = BlockPos.containing(source.getPosition());
		String dim = level.dimension().identifier().toString();
		String biome = level.getBiome(pos).unwrapKey().map(k -> k.identifier().toString()).orElse("unknown");
		log.emit(command, Events.place(name, dim, pos.getX(), pos.getY(), pos.getZ(), biome, field, value));
		source.sendSuccess(() -> Component.literal(confirmation), false);
		return 1;
	}
}
