package net.vanillaextended.vecommands;

import static org.junit.jupiter.api.Assertions.*;

import java.util.ArrayList;
import java.util.List;
import java.util.Optional;
import java.util.function.Function;
import org.junit.jupiter.api.Test;

/** The whitelist_add order: validate, then the existing whitelist by name, and only then Mojang. */
class WhitelistFlowTest {
	final List<String> lookups = new ArrayList<>();
	final List<String> checks = new ArrayList<>();
	final Function<String, Optional<String>> listed = n -> {
		checks.add(n);
		return n.equalsIgnoreCase("notch") ? Optional.of("Notch") : Optional.empty();
	};
	final Function<String, Optional<String>> mojang = n -> {
		lookups.add(n);
		return n.equals("jeb_") ? Optional.of("jeb_-profile") : Optional.empty();
	};

	static WhitelistFlow.Result done(WhitelistFlow.Outcome o) {
		return assertInstanceOf(WhitelistFlow.Done.class, o).result();
	}

	@Test
	void anAlreadyListedNameIsAnsweredWithItsStoredNameAndNoLookup() {
		WhitelistFlow.Result r = done(WhitelistFlow.decide("nOtCh", listed, mojang));
		assertEquals(new WhitelistFlow.Result("Notch", true, "already"), r);
		assertEquals(List.of(), lookups, "Mojang is never asked about a listed name (throttling cannot break it)");
	}

	@Test
	void anUnlistedNameIsLookedUpOnce() {
		WhitelistFlow.Outcome o = WhitelistFlow.decide("jeb_", listed, mojang);
		assertEquals("jeb_-profile", assertInstanceOf(WhitelistFlow.Add.class, o).profile());
		assertEquals(List.of("jeb_"), lookups);
		assertEquals(new WhitelistFlow.Result("zz_nobody_here", false, "unknown account"),
				done(WhitelistFlow.decide("zz_nobody_here", listed, mojang)));
	}

	@Test
	void anInvalidNameIsAnsweredBeforeAnyCheckOrLookup() {
		assertEquals(new WhitelistFlow.Result("bad name!", false, "invalid name"),
				done(WhitelistFlow.decide("bad name!", listed, mojang)));
		assertEquals(List.of(), checks);
		assertEquals(List.of(), lookups);
	}

	@Test
	void aThrowingLookupIsAnsweredLookupFailed() {
		WhitelistFlow.Result r = done(WhitelistFlow.decide("Someone", listed, n -> {
			throw new IllegalStateException("network");
		}));
		assertEquals(new WhitelistFlow.Result("Someone", false, "lookup failed: IllegalStateException"), r);
	}
}
