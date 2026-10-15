package net.vanillaextended.vecommands;

import static org.junit.jupiter.api.Assertions.*;

import java.nio.file.Files;
import java.nio.file.Path;
import java.util.List;
import org.junit.jupiter.api.Test;
import org.junit.jupiter.api.io.TempDir;

/** VeCommands.start's directory setup: a linked mcve/ is refused BEFORE out/ and in/ could be created through it. */
class McveDirsTest {
	@TempDir
	Path root;

	@Test
	void createsAllThreeOnAFreshServer() throws Exception {
		Path mcve = root.resolve("mcve");
		assertTrue(McveDirs.prepare(mcve));
		assertTrue(Files.isDirectory(mcve.resolve("out")) && Files.isDirectory(mcve.resolve("in")));
		assertTrue(McveDirs.prepare(mcve), "and again, idempotently");
	}

	@Test
	void aLinkedMcveIsRefusedWithNothingCreatedThroughIt() throws Exception {
		Path world = Files.createDirectory(root.resolve("world"));
		Path mcve = Files.createSymbolicLink(root.resolve("mcve"), world);
		assertFalse(McveDirs.prepare(mcve));
		try (var s = Files.list(world)) {
			assertEquals(List.of(), s.toList(), "no out/ or in/ inside the link's target");
		}
	}

	@Test
	void aLinkedChildIsRefused() throws Exception {
		Path world = Files.createDirectory(root.resolve("world"));
		Path mcve = Files.createDirectory(root.resolve("mcve"));
		Files.createSymbolicLink(mcve.resolve("out"), world);
		assertFalse(McveDirs.prepare(mcve));
	}
}
