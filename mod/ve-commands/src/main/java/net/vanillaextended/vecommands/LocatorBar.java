package net.vanillaextended.vecommands;

import java.util.function.Predicate;
import net.minecraft.resources.Identifier;
import net.minecraft.server.level.ServerPlayer;
import net.minecraft.world.entity.ai.attributes.AttributeInstance;
import net.minecraft.world.entity.ai.attributes.AttributeModifier;
import net.minecraft.world.entity.ai.attributes.Attributes;

/**
 * {@link Locator} on a ServerPlayer (server thread only). It only ever touches players: the receive cap and /hide
 * are modifiers on a player's own two waypoint attributes. Nothing here spawns or tags any other entity, so no
 * place can ever transmit a dot (the a2 design's line not to cross: dots mark players, not places).
 *
 * <p>The work is in {@link Waypoints}, over the player's two real AttributeInstances and its tag calls, so
 * LocatorBarTest runs it on Minecraft's own attribute arithmetic; {@link #of} only hands it the player's.
 */
final class LocatorBar {
	static final Identifier RECEIVE_CAP = Identifier.fromNamespaceAndPath("ve-commands", "receive_cap");
	static final Identifier HIDE = Identifier.fromNamespaceAndPath("ve-commands", "hide");

	/** /hide: the shape of vanilla's own crouch modifier (waypoint_transmit_range_crouch), x0 whatever the base. */
	static final AttributeModifier HIDE_MODIFIER =
			new AttributeModifier(HIDE, -1.0, AttributeModifier.Operation.ADD_MULTIPLIED_TOTAL);

	/** The range in force for this server run (Locator.range: the default unless the spike property is valid). */
	static final double RANGE = Locator.range(System.getProperty("vecommands.locatorRange"));

	private LocatorBar() {}

	/** The receive cap: an ADD_VALUE offset on the base (Locator.capAmount), so base + it = the range. */
	static AttributeModifier capModifier(double addValue) {
		return new AttributeModifier(RECEIVE_CAP, addValue, AttributeModifier.Operation.ADD_VALUE);
	}

	/** On join and after every respawn. A failure is logged, never thrown into the join or respawn. */
	static void apply(ServerPlayer player) {
		try {
			Locator.apply(of(player), RANGE);
		} catch (RuntimeException e) {
			VeCommands.LOG.error("locator cap not applied to {}: {}", player.getGameProfile().name(), e.toString());
		}
	}

	static Locator.Target of(ServerPlayer player) {
		return new Waypoints(player.getAttribute(Attributes.WAYPOINT_RECEIVE_RANGE),
				player.getAttribute(Attributes.WAYPOINT_TRANSMIT_RANGE),
				player.entityTags()::contains, player::addTag, player::removeTag);
	}

	/**
	 * One player's receive and transmit range (either may be null on a player without it: then nothing is done) and
	 * its entity tags. Only transient modifiers: AttributeInstance.pack() never saves them.
	 */
	record Waypoints(AttributeInstance receive, AttributeInstance transmit, Predicate<String> tagged,
			Predicate<String> addTag, Predicate<String> removeTag) implements Locator.Target {
		@Override
		public double receiveBase() {
			return receive == null ? 0 : receive.getBaseValue();
		}

		@Override
		public void capReceive(double addValue) {
			if (receive == null) return;
			if (addValue == 0) receive.removeModifier(RECEIVE_CAP);
			else receive.addOrUpdateTransientModifier(capModifier(addValue));
		}

		@Override
		public void hideTransmit(boolean hidden) {
			if (transmit == null) return;
			if (hidden) transmit.addOrUpdateTransientModifier(HIDE_MODIFIER);
			else transmit.removeModifier(HIDE);
		}

		@Override
		public boolean hasTag(String tag) {
			return tagged.test(tag);
		}

		@Override
		public void setTag(String tag, boolean on) {
			if (on) addTag.test(tag);
			else removeTag.test(tag);
		}
	}
}
