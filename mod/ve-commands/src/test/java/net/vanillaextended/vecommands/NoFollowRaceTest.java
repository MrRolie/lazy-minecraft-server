package net.vanillaextended.vecommands;

import static org.junit.jupiter.api.Assertions.*;

import com.google.gson.JsonElement;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.AfterEach;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/**
 * The second layer: a link swapped in AFTER the lstat check passed (through SafeFiles.raceHook, exactly between the
 * check and the open) must still not be followed. This pins O_NOFOLLOW / O_EXCL at each open, which SymlinkTest
 * cannot, because there the lstat check refuses first.
 */
class NoFollowRaceTest {
	@TempDir
	Path root;
	Path in;
	Path out;
	Path world;
	Path canary;
	final List<String> calls = new ArrayList<>();
	final Inbox.Handler handler = new Inbox.Handler() {
		@Override
		public void say(JsonElement id, String author, String text) {
			calls.add("say " + text);
		}

		@Override
		public void whitelistAdd(JsonElement id, String name, String requestedBy) {}

		@Override
		public void whitelistRateLimited(JsonElement id, String name) {}
	};

	@BeforeEach
	void layout() throws Exception {
		in = Files.createDirectory(root.resolve("in"));
		out = Files.createDirectory(root.resolve("out"));
		world = Files.createDirectory(root.resolve("world"));
		canary = Files.writeString(world.resolve("canary"), "{\"type\":\"say\",\"id\":\"w\",\"author\":\"W\",\"text\":\"world bytes\"}\n");
	}

	@AfterEach
	void unhook() {
		SafeFiles.raceHook = p -> {};
	}

	/** At the first open of {@code target}, replace it with a link to the canary. */
	void swapAtOpen(Path target) {
		SafeFiles.raceHook = p -> {
			if (!p.equals(target)) return;
			SafeFiles.raceHook = q -> {};
			try {
				Files.deleteIfExists(target);
				Files.createSymbolicLink(target, canary);
			} catch (Exception e) {
				throw new AssertionError(e);
			}
		};
	}

	String canaryNow() throws Exception {
		return Files.readString(canary);
	}

	@Test
	void theInboxReadDoesNotFollowALinkSwappedInAfterTheCheck() throws Exception {
		Files.writeString(in.resolve("inbox.jsonl"), "");
		Inbox inbox = new Inbox(in, out, handler);
		Files.writeString(in.resolve("inbox.jsonl"), "{\"type\":\"say\",\"id\":\"1\",\"author\":\"A\",\"text\":\"real\"}\n");
		swapAtOpen(in.resolve("inbox.jsonl"));
		inbox.poll();
		assertEquals(List.of(), calls, "the world bytes behind the link were not read as inbox lines");
	}

	@Test
	void theOffsetReadDoesNotFollowALinkSwappedInAfterTheCheck() throws Exception {
		Files.writeString(canary, "0");
		Files.writeString(in.resolve("inbox.jsonl"), "{\"type\":\"say\",\"id\":\"old\",\"author\":\"A\",\"text\":\"old\"}\n");
		Files.writeString(out.resolve("inbox.offset"), "57");
		swapAtOpen(out.resolve("inbox.offset"));
		Inbox inbox = new Inbox(in, out, handler);
		inbox.poll();
		assertEquals(List.of(), calls, "the link's \"0\" was not read as the offset (that would replay)");
	}

	@Test
	void theOffsetWriteDoesNotFollowALinkPlantedBetweenDeleteAndCreate() throws Exception {
		Inbox inbox = new Inbox(in, out, handler);
		String before = canaryNow();
		Files.writeString(in.resolve("inbox.jsonl"), "{\"type\":\"say\",\"id\":\"1\",\"author\":\"A\",\"text\":\"t\"}\n");
		swapAtOpen(out.resolve("inbox.offset.tmp"));
		inbox.poll();
		assertEquals(before, canaryNow(), "the offset was not written through the link");
	}

	@Test
	void theEventsAppendDoesNotFollowALinkSwappedInAfterTheCheck() throws Exception {
		String before = canaryNow();
		swapAtOpen(out.resolve("events.jsonl"));
		new EventLog(out, 1 << 20).append("{\"v\":1}\n");
		assertEquals(before, canaryNow(), "the event was not appended through the link");
	}
}
