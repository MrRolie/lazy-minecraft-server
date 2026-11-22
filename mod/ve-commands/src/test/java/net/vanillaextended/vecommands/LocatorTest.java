package net.vanillaextended.vecommands;

import static org.junit.jupiter.api.Assertions.*;

import java.util.ArrayList;
import java.util.HashSet;
import java.util.List;
import java.util.Set;
import org.junit.jupiter.api.Test;

/** The locator cap and /hide (Locator), on a fake player that does vanilla's attribute arithmetic. */
class LocatorTest {
	/** A player: receive = base + the ADD_VALUE cap, clamped like RangedAttribute; transmit x0 while hidden. */
	static final class Fake implements Locator.Target {
		double receiveBase = Locator.MAX_RANGE; // the vanilla player default (Player.createAttributes)
		double transmitBase = Locator.MAX_RANGE;
		double cap;
		boolean hideModifier;
		final Set<String> tags = new HashSet<>();
		final List<String> calls = new ArrayList<>();

		double receive() {
			return Math.max(0, Math.min(Locator.MAX_RANGE, receiveBase + cap));
		}

		double transmit() {
			return hideModifier ? 0 : transmitBase;
		}

		/** A respawn: vanilla copies base values and tags to a new player; transient modifiers are gone. */
		Fake respawn() {
			Fake n = new Fake();
			n.receiveBase = receiveBase;
			n.transmitBase = transmitBase;
			n.tags.addAll(tags);
			return n;
		}

		@Override
		public double receiveBase() {
			calls.add("receiveBase");
			return receiveBase;
		}

		@Override
		public void capReceive(double addValue) {
			calls.add("capReceive");
			cap = addValue;
		}

		@Override
		public void hideTransmit(boolean hidden) {
			calls.add("hideTransmit " + hidden);
			hideModifier = hidden;
		}

		@Override
		public boolean hasTag(String tag) {
			return tags.contains(tag);
		}

		@Override
		public void setTag(String tag, boolean on) {
			calls.add("setTag " + tag + " " + on);
			if (on) tags.add(tag);
			else tags.remove(tag);
		}
	}

	@Test
	void theCapIsInTheDesignBand() {
		assertEquals(200, Locator.RECEIVE_RANGE_BLOCKS);
		assertTrue(Locator.RECEIVE_RANGE_BLOCKS >= 150 && Locator.RECEIVE_RANGE_BLOCKS <= 250, "a2: about 150-250");
	}

	@Test
	void joinCapsReceiveToTheRangeExactly() {
		Fake p = new Fake();
		Locator.apply(p, 200);
		assertEquals(200.0, p.receive(), 0.0, "6.0E7 + (200 - 6.0E7) is exact in a double");
		assertEquals(Locator.MAX_RANGE, p.transmit(), "a visible player's own dot is untouched");
		Locator.apply(p, 200);
		assertEquals(200.0, p.receive(), 0.0, "applying again changes nothing");
	}

	@Test
	void theCapNeverRaisesALowerReceive() {
		assertEquals(0, Locator.capAmount(120, 200));
		Fake p = new Fake();
		p.receiveBase = 120;
		Locator.apply(p, 200);
		assertEquals(120, p.receive());
		assertEquals(-59_999_800, Locator.capAmount(Locator.MAX_RANGE, 200));
	}

	@Test
	void respawnReappliesTheCapAndTheHide() {
		Fake p = new Fake();
		Locator.apply(p, 200);
		Locator.toggleHide(p);
		Fake after = p.respawn();
		assertEquals(Locator.MAX_RANGE, after.receive(), "a new player has no cap until it is applied");
		Locator.apply(after, 200);
		assertEquals(200, after.receive());
		assertEquals(0, after.transmit(), "the tag carries /hide through the respawn");
	}

	@Test
	void hideTogglesTransmitOnlyAndNeverTouchesReceive() {
		Fake p = new Fake();
		Locator.apply(p, 200);
		p.calls.clear();
		assertTrue(Locator.toggleHide(p));
		assertEquals(0, p.transmit());
		assertTrue(p.tags.contains(Locator.HIDDEN_TAG));
		assertEquals(200, p.receive(), "you still see friends");
		assertFalse(Locator.toggleHide(p));
		assertEquals(Locator.MAX_RANGE, p.transmit());
		assertFalse(p.tags.contains(Locator.HIDDEN_TAG));
		assertEquals(List.of("setTag ve_hidden true", "hideTransmit true", "setTag ve_hidden false", "hideTransmit false"),
				p.calls, "no receive call at all");
	}

	@Test
	void aHiddenPlayerRejoinsHidden() {
		Fake p = new Fake();
		p.tags.add(Locator.HIDDEN_TAG); // saved in playerdata ("Tags") from an earlier session
		Locator.apply(p, 200);
		assertEquals(0, p.transmit());
		Fake q = new Fake();
		Locator.apply(q, 200);
		assertEquals(Locator.MAX_RANGE, q.transmit(), "no tag, no hide");
	}

	@Test
	void theSpikeOverrideTakesOnlyAUsableRange() {
		assertEquals(200, Locator.range(null));
		assertEquals(150, Locator.range("150"));
		assertEquals(250, Locator.range(" 250 "));
		for (String bad : List.of("", "abc", "0", "-5", "NaN", "Infinity", "1e9", "60000001")) {
			assertEquals(200, Locator.range(bad), bad);
		}
	}

	@Test
	void messagesSayWhatTheCommandsDo() {
		String hidden = Locator.hideMessage(true, 200);
		assertTrue(hidden.contains("off everyone's locator bar") && hidden.contains("until you type /hide again"));
		assertTrue(hidden.contains("You still see theirs"));
		assertTrue(hidden.contains("/here still tells everyone where you are"), "hidden from the bar, not from /here");
		assertEquals("You're visible again: friends within 200 blocks see your dot.", Locator.hideMessage(false, 200));
		assertEquals("📍 Alex is at 120 64 -340 in the Overworld", Locator.hereLine("Alex", "minecraft:overworld", 120, 64, -340));
		assertEquals("📍 Alex is at 1 2 3 in the Nether", Locator.hereLine("Alex", "minecraft:the_nether", 1, 2, 3));
		assertEquals("📍 Alex is at 1 2 3 in the End", Locator.hereLine("Alex", "minecraft:the_end", 1, 2, 3));
		assertEquals("📍 Alex is at 1 2 3 in mod:moon", Locator.hereLine("Alex", "mod:moon", 1, 2, 3));
		assertEquals("150.5", Locator.blocks(150.5));
	}
}
