package net.vanillaextended.vecommands;

import java.util.Map;

/**
 * The locator bar, capped (option a2 of a private design note, not in this copy; ruled 2026-11-03): the
 * bar is on, and each player sees dots only for friends within {@link #RECEIVE_RANGE_BLOCKS}. MC-free; the server
 * side is {@link LocatorBar}, which hands this a {@link Target} over one ServerPlayer.
 *
 * <p>Every attribute change is a <b>transient</b> modifier. MC 26.1.2's {@code AttributeInstance.pack()} saves only
 * the base value and the permanent modifiers, so nothing here reaches playerdata: a server without this code is
 * vanilla again at each player's next join. The one durable bit is the {@link #HIDDEN_TAG} entity tag, which vanilla
 * saves ("Tags") and {@code PlayerList.respawn} copies to the new player on every respawn.
 *
 * <p>Vanilla's rule (WaypointTransmitter.doesSourceIgnoreReceiver): a dot shows while the 3D distance is under
 * min(the friend's waypoint_transmit_range, your waypoint_receive_range).
 */
final class Locator {
	/**
	 * How far away a friend's dot shows, in blocks. Why 200: the server's view distance is 8 chunks (128 blocks,
	 * docker-compose.yml VIEW_DISTANCE), so 200 is about one and a half times what you can see. A friend over the
	 * next hill, down the same cave or across the raid shows; a friend across the world does not, so the bar never
	 * becomes "walk to the dot" (option a1, rejected). The a2 design's band is 150 to 250.
	 * {@code -Dvecommands.locatorRange=<blocks>} overrides it, for a spike only (itzg: JVM_DD_OPTS).
	 */
	static final double RECEIVE_RANGE_BLOCKS = 200;

	/** Vanilla's ceiling for both waypoint attributes (RangedAttribute max, and the player default). */
	static final double MAX_RANGE = 6.0E7;

	/** The entity tag that holds /hide. It does nothing on its own: this mod reads it on join and respawn. */
	static final String HIDDEN_TAG = "ve_hidden";

	private Locator() {}

	/** One player, as the locator code sees it. */
	interface Target {
		/** The base value of minecraft:waypoint_receive_range (vanilla 6.0E7). */
		double receiveBase();

		/** Sets the transient ADD_VALUE modifier ve-commands:receive_cap on receive range; 0 removes it. */
		void capReceive(double addValue);

		/** Adds (or removes) the transient x0 modifier ve-commands:hide on transmit range. */
		void hideTransmit(boolean hidden);

		boolean hasTag(String tag);

		void setTag(String tag, boolean on);
	}

	/** The receive cap as an ADD_VALUE amount: brings the base down to {@code range}, never raises it. */
	static double capAmount(double base, double range) {
		return Math.min(0, range - base);
	}

	/** On join and on every respawn (a respawn is a new ServerPlayer, without the old one's transient modifiers). */
	static void apply(Target p, double range) {
		p.capReceive(capAmount(p.receiveBase(), range));
		p.hideTransmit(p.hasTag(HIDDEN_TAG));
	}

	/** /hide: flips the tag and the transmit modifier, and never touches receive. Returns whether now hidden. */
	static boolean toggleHide(Target p) {
		boolean hidden = !p.hasTag(HIDDEN_TAG);
		p.setTag(HIDDEN_TAG, hidden);
		p.hideTransmit(hidden);
		return hidden;
	}

	/** The range in force: the override if it is a number in (0, MAX_RANGE], else the default. */
	static double range(String override) {
		if (override == null) return RECEIVE_RANGE_BLOCKS;
		try {
			double r = Double.parseDouble(override.strip());
			if (r > 0 && r <= MAX_RANGE) return r;
		} catch (NumberFormatException ignored) {
			// falls through to the default
		}
		return RECEIVE_RANGE_BLOCKS;
	}

	static String hideMessage(boolean hidden, double range) {
		return hidden
				? "You're hidden: your dot is off everyone's locator bar, through deaths and relogs, until you type /hide"
						+ " again. You still see theirs, and /here still tells everyone where you are."
				: "You're visible again: friends within " + blocks(range) + " blocks see your dot.";
	}

	private static final Map<String, String> DIMENSIONS = Map.of(
			"minecraft:overworld", "the Overworld",
			"minecraft:the_nether", "the Nether",
			"minecraft:the_end", "the End");

	/** The /here line: who, where, which dimension. Text in chat, nothing on the bar. */
	static String hereLine(String name, String dim, int x, int y, int z) {
		return "📍 " + name + " is at " + x + " " + y + " " + z + " in " + DIMENSIONS.getOrDefault(dim, dim);
	}

	static String blocks(double range) {
		return range == Math.rint(range) ? Long.toString((long) range) : Double.toString(range);
	}
}
