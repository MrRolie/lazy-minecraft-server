package net.vanillaextended.vecommands;

import java.util.Optional;
import java.util.function.Function;

/**
 * The whitelist_add decision up to (not including) the add itself. It is MC-free, so the order of the steps can be
 * tested: validate the name, then check the existing whitelist by name, and only then ask Mojang. A name that is
 * already listed never costs a lookup, so throttling or an outage cannot turn "already" into "unknown account".
 */
final class WhitelistFlow {
	record Result(String name, boolean ok, String detail) {}

	sealed interface Outcome permits Done, Add {}

	/** Answer now. */
	record Done(Result result) implements Outcome {}

	/** Resolved: add this profile on the server thread (which re-checks by id). */
	record Add<P>(P profile) implements Outcome {}

	private WhitelistFlow() {}

	/**
	 * @param listedName the whitelist's stored name matching {@code name} case-insensitively, if any
	 * @param lookup     the server's profile lookup (a Mojang HTTP call in online mode)
	 */
	static <P> Outcome decide(String name, Function<String, Optional<String>> listedName,
			Function<String, Optional<P>> lookup) {
		if (!Text.PLAYER_NAME.matcher(name).matches()) {
			return new Done(new Result(Text.truncate(name, 32), false, "invalid name"));
		}
		Optional<String> listed = listedName.apply(name);
		if (listed.isPresent()) return new Done(new Result(listed.get(), true, "already"));
		Optional<P> profile;
		try {
			profile = lookup.apply(name);
		} catch (RuntimeException e) {
			return new Done(new Result(name, false, "lookup failed: " + e.getClass().getSimpleName()));
		}
		if (profile.isEmpty()) return new Done(new Result(name, false, "unknown account"));
		return new Add<>(profile.get());
	}
}
