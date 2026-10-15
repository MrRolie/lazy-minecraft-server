package net.vanillaextended.vecommands;

import static org.junit.jupiter.api.Assertions.*;

import com.google.gson.JsonElement;
import java.nio.file.Files;
import java.nio.file.Path;
import java.util.ArrayList;
import java.util.List;
import org.junit.jupiter.api.BeforeEach;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/** Planted links in mcve/ are refused, never followed: the stand-in world files must come out byte-identical. */
class SymlinkTest {
	@TempDir
	Path root;
	Path in;
	Path out;
	Path levelDat;
	Path regionMca;
	final List<String> calls = new ArrayList<>();
	final Inbox.Handler handler = new Inbox.Handler() {
		@Override
		public void say(JsonElement id, String author, String text) {
			calls.add("say " + text);
		}

		@Override
		public void whitelistAdd(JsonElement id, String name, String requestedBy) {
			calls.add("wl " + name);
		}

		@Override
		public void whitelistRateLimited(JsonElement id, String name) {
			calls.add("limited " + name);
		}
	};

	@BeforeEach
	void layout() throws Exception {
		in = Files.createDirectory(root.resolve("in"));
		out = Files.createDirectory(root.resolve("out"));
		Path world = Files.createDirectory(root.resolve("world"));
		levelDat = Files.writeString(world.resolve("level.dat"), "LEVEL");
		regionMca = Files.writeString(world.resolve("r.0.0.mca"), "{\"type\":\"say\",\"id\":\"1\",\"author\":\"A\",\"text\":\"from the world\"}\n");
	}

	void untouched() throws Exception {
		assertEquals("LEVEL", Files.readString(levelDat));
		assertEquals("{\"type\":\"say\",\"id\":\"1\",\"author\":\"A\",\"text\":\"from the world\"}\n", Files.readString(regionMca));
	}

	@Test
	void eventsLinkIsNotAppendedThrough() throws Exception {
		Files.createSymbolicLink(out.resolve("events.jsonl"), levelDat);
		new EventLog(out, 1 << 20).append("{\"v\":1}\n");
		untouched();
		assertTrue(Files.isSymbolicLink(out.resolve("events.jsonl")));
	}

	@Test
	void rotatedLinkIsNotReplacedThrough() throws Exception {
		Files.createSymbolicLink(out.resolve("events.jsonl.1"), levelDat);
		EventLog log = new EventLog(out, 4);
		log.append("{\"v\":1}\n"); // passes the threshold; rotation refused
		untouched();
		assertTrue(Files.exists(out.resolve("events.jsonl")), "the live file stays put");
	}

	@Test
	void offsetTmpLinkIsRemovedNotWrittenThrough() throws Exception {
		Files.createSymbolicLink(out.resolve("inbox.offset.tmp"), regionMca);
		Inbox inbox = new Inbox(in, out, handler); // records its start offset through the tmp file
		Files.writeString(in.resolve("inbox.jsonl"), "{\"type\":\"say\",\"id\":\"2\",\"author\":\"A\",\"text\":\"t\"}\n");
		inbox.poll();
		untouched();
		assertEquals(List.of("say t"), calls);
		assertFalse(Files.exists(out.resolve("inbox.offset.tmp"), java.nio.file.LinkOption.NOFOLLOW_LINKS));
		assertFalse(Files.isSymbolicLink(out.resolve("inbox.offset")));
	}

	@Test
	void offsetLinkIsNeitherReadNorReplacedThrough() throws Exception {
		Files.writeString(levelDat, "0");
		Files.createSymbolicLink(out.resolve("inbox.offset"), levelDat);
		Files.writeString(in.resolve("inbox.jsonl"), "{\"type\":\"say\",\"id\":\"old\",\"author\":\"A\",\"text\":\"old\"}\n");
		Inbox inbox = new Inbox(in, out, handler);
		inbox.poll();
		assertEquals(List.of(), calls, "the link's '0' was not read as an offset (that would replay)");
		assertEquals("0", Files.readString(levelDat));
		assertTrue(Files.isSymbolicLink(out.resolve("inbox.offset")));
	}

	@Test
	void inboxLinkIsNotRead() throws Exception {
		Inbox inbox = new Inbox(in, out, handler);
		Files.createSymbolicLink(in.resolve("inbox.jsonl"), regionMca);
		inbox.poll();
		assertEquals(List.of(), calls, "world bytes behind a link are never parsed as inbox lines");
		untouched();
	}

	@Test
	void linkedDirectoriesAreRefused() throws Exception {
		Path linkedIn = Files.createSymbolicLink(root.resolve("in2"), regionMca.getParent());
		Path linkedOut = Files.createSymbolicLink(root.resolve("out2"), levelDat.getParent());
		Inbox inbox = new Inbox(linkedIn, linkedOut, handler);
		inbox.poll();
		new EventLog(linkedOut, 1 << 20).append("{\"v\":1}\n");
		assertEquals(List.of(), calls);
		untouched();
		assertTrue(InboxTest.names(levelDat.getParent()).equals(List.of("level.dat", "r.0.0.mca")), "nothing created in world/");
	}

	@Test
	void fifoInboxIsRefusedWithoutBlocking() throws Exception {
		Path fifo = in.resolve("inbox.jsonl");
		Process p = new ProcessBuilder("mkfifo", fifo.toString()).start();
		assumeOk(p.waitFor() == 0);
		Inbox inbox = new Inbox(in, out, handler);
		Thread t = new Thread(inbox::poll, "fifo-poll");
		t.setDaemon(true); // if open() blocks on the fifo, the test fails and the JVM can still exit
		t.start();
		t.join(5000);
		assertFalse(t.isAlive(), "poll() blocked on a fifo inbox (it must be refused, never opened)");
		assertEquals(List.of(), calls);
	}

	static void assumeOk(boolean ok) {
		org.junit.jupiter.api.Assumptions.assumeTrue(ok, "mkfifo unavailable");
	}
}
