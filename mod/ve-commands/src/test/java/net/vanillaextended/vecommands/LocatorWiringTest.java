package net.vanillaextended.vecommands;

import static org.junit.jupiter.api.Assertions.*;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import java.util.stream.Stream;
import org.junit.jupiter.api.Test;

/**
 * The locator code's server side, pinned from its source (it needs a live MinecraftServer, so it cannot run here;
 * the JobsTest pattern). LocatorTest proves the rules; this proves the server calls them where it must, touches
 * only what it may, and leaves nothing in playerdata.
 */
class LocatorWiringTest {
	static final Path MAIN = Path.of("src/main/java/net/vanillaextended/vecommands");

	static String src(String file) throws IOException {
		return Files.readString(MAIN.resolve(file));
	}

	@Test
	void theCapGoesOnAtEveryJoinAndOnTheNewPlayerAfterEveryRespawn() throws IOException {
		String s = src("VeCommands.java");
		String join = body(s, "ServerPlayConnectionEvents.JOIN.register(");
		assertTrue(join.contains("LocatorBar.apply(handler.player);"), join);
		String respawn = call(s, "ServerPlayerEvents.AFTER_RESPAWN.register(");
		assertEquals("(oldPlayer, newPlayer, alive) -> LocatorBar.apply(newPlayer)", respawn,
				"the respawned player is a new ServerPlayer: the cap goes on it, not on the old one");
		assertEquals(2, count(s, "LocatorBar.apply("), "join and respawn, nowhere else");
	}

	@Test
	void hideTouchesTransmitOnlyAndTheCapTouchesReceiveOnly() throws IOException {
		String s = src("LocatorBar.java");
		String hide = body(s, "public void hideTransmit(boolean hidden)");
		assertTrue(hide.contains("transmit."), hide);
		assertFalse(hide.contains("receive"), "/hide never touches what you see");
		for (String m : List.of("public double receiveBase()", "public void capReceive(double addValue)")) {
			String b = body(s, m);
			assertTrue(b.contains("receive"), b);
			assertFalse(b.contains("transmit"), m + " never touches your own dot");
		}
		String toggle = body(src("Locator.java"), "static boolean toggleHide(Target p)");
		assertFalse(toggle.contains("Receive") || toggle.contains("apply("), toggle);
	}

	/**
	 * The one line LocatorBarTest cannot run (it needs a ServerPlayer): the player's own receive and transmit range,
	 * in that order, and its own tag set and tag calls. Pinned whole, so swapping the two ranges, a constant tag
	 * answer or a dropped removeTag fails here; Waypoints itself runs on real AttributeInstances in LocatorBarTest.
	 */
	@Test
	void ofHandsWaypointsThePlayersOwnRangesAndTags() throws IOException {
		assertEquals("return new Waypoints(player.getAttribute(Attributes.WAYPOINT_RECEIVE_RANGE),"
				+ " player.getAttribute(Attributes.WAYPOINT_TRANSMIT_RANGE),"
				+ " player.entityTags()::contains, player::addTag, player::removeTag);",
				body(src("LocatorBar.java"), "static Locator.Target of(ServerPlayer player)").strip().replaceAll("\\s+", " "));
	}

	@Test
	void nothingReachesPlayerdataButTheHideTag() throws IOException {
		String s = src("LocatorBar.java");
		assertEquals(2, count(s, "addOrUpdateTransientModifier("), "the cap and the hide are transient modifiers");
		assertEquals(2, count(s, "new AttributeModifier("), "only the two modifiers LocatorBarTest runs");
		for (String all : mainSources()) {
			for (String persistent : List.of("setBaseValue(", "addPermanentModifier(", "addOrReplacePermanentModifier(",
					"addPermanentModifiers(")) {
				assertFalse(all.contains(persistent), persistent + " would leave the cap in playerdata");
			}
		}
	}

	/** The a2 line not to cross: any living entity can transmit, so an armor stand at the spire would be a place. */
	@Test
	void dotsMarkPlayersNeverPlaces() throws IOException {
		for (String all : mainSources()) {
			for (String place : List.of("addFreshEntity(", "ArmorStand", "EntityType.", "summon", "trackWaypoint(",
					"WaypointCommand", "waypoint modify", "Marker")) {
				assertFalse(all.contains(place), "the mod never makes a place transmit: " + place);
			}
		}
		long transmitters = mainFiles().filter(f -> read(f).contains("WAYPOINT_TRANSMIT_RANGE")).count();
		assertEquals(1, transmitters, "only LocatorBar touches transmit range");
		assertTrue(src("LocatorBar.java").contains("static Locator.Target of(ServerPlayer player)"),
				"the locator code takes players, never any other entity");
	}

	@Test
	void hereIsAChatLineThatStaysInTheGame() throws IOException {
		String here = body(src("PlayerCommands.java"), "private static int here(");
		// Every player online, one by one (a ServerPlayer list cannot be built here, so the line is pinned whole).
		assertEquals(1, count(here,
				"for (ServerPlayer to : source.getServer().getPlayerList().getPlayers()) to.sendSystemMessage(line);"),
				here);
		assertEquals(1, count(here, "sendSystemMessage("), "the line goes to the players online, nowhere else");
		for (String no : List.of("broadcastSystemMessage", "WAYPOINT", "addTag", "LocatorBar", "emit(")) {
			assertFalse(here.contains(no), "/here: " + no);
		}
		assertTrue(here.contains("COOLDOWNS.tryAcquire(\"here:\""), "the 10 s cooldown");
	}

	static Stream<Path> mainFiles() throws IOException {
		return Files.list(MAIN).filter(f -> f.toString().endsWith(".java"));
	}

	static List<String> mainSources() throws IOException {
		return mainFiles().map(LocatorWiringTest::read).toList();
	}

	static String read(Path f) {
		try {
			return Files.readString(f);
		} catch (IOException e) {
			throw new IllegalStateException(e);
		}
	}

	static int count(String s, String what) {
		int n = 0;
		for (int i = s.indexOf(what); i >= 0; i = s.indexOf(what, i + 1)) n++;
		return n;
	}

	/** The text between the first '{' after {@code head} and its matching '}'. */
	static String body(String s, String head) {
		int at = s.indexOf(head);
		assertTrue(at >= 0, "missing: " + head);
		int open = s.indexOf('{', at);
		int depth = 0;
		for (int i = open; i < s.length(); i++) {
			char c = s.charAt(i);
			if (c == '{') depth++;
			if (c == '}' && --depth == 0) return s.substring(open + 1, i);
		}
		throw new AssertionError("unbalanced: " + head);
	}

	/** The single argument of the call that {@code head} opens, whitespace collapsed. */
	static String call(String s, String head) {
		int at = s.indexOf(head);
		assertTrue(at >= 0, "missing: " + head);
		int depth = 1;
		for (int i = at + head.length(); i < s.length(); i++) {
			char c = s.charAt(i);
			if (c == '(') depth++;
			if (c == ')' && --depth == 0) return s.substring(at + head.length(), i).strip().replaceAll("\\s+", " ");
		}
		throw new AssertionError("unbalanced: " + head);
	}
}
