package net.vanillaextended.vecommands;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.LinkOption;
import java.nio.file.NoSuchFileException;
import java.nio.file.Path;
import java.nio.file.attribute.BasicFileAttributes;
import java.util.Map;
import java.util.Set;
import java.util.concurrent.ConcurrentHashMap;
import java.util.function.Consumer;
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

/**
 * Every mcve path goes through here before use. The bridge can write mcve/in/, so a path there (or, on a
 * misconfigured mount, anywhere under mcve/) may be a symlink planted to point at the world. Nothing is followed:
 * paths are checked with lstat semantics (NOFOLLOW_LINKS), opened with O_NOFOLLOW, and a refusal is logged when a path
 * turns bad. Those transitions are throttled to one log line a minute per path, so a bridge flipping a path between a
 * link and a file every poll cannot amplify itself into the server log.
 */
final class SafeFiles {
	private static final Logger LOG = LoggerFactory.getLogger("ve-commands");
	private static final Set<Path> REFUSED = ConcurrentHashMap.newKeySet();
	private static final Map<Path, LogThrottle> TURNED_BAD = new ConcurrentHashMap<>();
	private static final Map<Path, LogThrottle> TURNED_GOOD = new ConcurrentHashMap<>();
	/**
	 * Test hook, a no-op in production: called with the path about to be opened, after its checks passed, so a test
	 * can swap in a link exactly there and prove that the O_NOFOLLOW open (not only the lstat check) refuses it.
	 */
	static volatile Consumer<Path> raceHook = p -> {};

	private SafeFiles() {}

	/** The attributes of {@code p} itself (never a link target), or null if nothing is there. */
	static BasicFileAttributes lstat(Path p) throws IOException {
		try {
			return Files.readAttributes(p, BasicFileAttributes.class, LinkOption.NOFOLLOW_LINKS);
		} catch (NoSuchFileException e) {
			return null;
		}
	}

	/** True if {@code p} is a real directory (not a symlink to one). */
	static boolean isRealDir(Path p) throws IOException {
		BasicFileAttributes a = lstat(p);
		return ok(p, a != null && a.isDirectory(), kind(a), "directory");
	}

	/** True if {@code p} is absent or a regular file; false (logged, throttled) for a symlink, directory, fifo, device. */
	static boolean absentOrRegular(Path p) throws IOException {
		BasicFileAttributes a = lstat(p);
		return ok(p, a == null || a.isRegularFile(), kind(a), "regular file");
	}

	private static boolean ok(Path p, boolean good, String is, String want) {
		if (good) {
			if (REFUSED.remove(p) && flips(TURNED_GOOD, p) >= 0) LOG.info("{} is usable again", p);
			return true;
		}
		long s;
		if (REFUSED.add(p) && (s = flips(TURNED_BAD, p)) >= 0) {
			LOG.error("refusing {}: it is {}, not a {}; links are never followed ({} more refusals since the last report)",
					p, is, want, s);
		}
		return false;
	}

	/** -1 to stay quiet. The keys are the handful of fixed mcve paths, so the maps stay that small. */
	private static long flips(Map<Path, LogThrottle> m, Path p) {
		return m.computeIfAbsent(p, k -> new LogThrottle(60_000, System::nanoTime)).next();
	}

	private static String kind(BasicFileAttributes a) {
		if (a == null) return "missing";
		if (a.isSymbolicLink()) return "a symlink";
		if (a.isDirectory()) return "a directory";
		if (a.isOther()) return "a special file";
		return "a regular file";
	}
}
