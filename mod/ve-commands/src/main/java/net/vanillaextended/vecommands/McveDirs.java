package net.vanillaextended.vecommands;

import java.io.IOException;
import java.nio.file.Files;
import java.nio.file.Path;

/** Sets up mcve/, mcve/out/ and mcve/in/, refusing a linked parent before anything is created through it. */
final class McveDirs {
	private McveDirs() {}

	/** True once all three are real directories (created as needed); false, with nothing created, otherwise. */
	static boolean prepare(Path mcve) throws IOException {
		if (SafeFiles.lstat(mcve) == null) Files.createDirectories(mcve);
		if (!SafeFiles.isRealDir(mcve)) return false; // checked before any child is created through it
		for (String child : new String[] {"out", "in"}) {
			Path dir = mcve.resolve(child);
			if (SafeFiles.lstat(dir) == null) Files.createDirectory(dir);
			if (!SafeFiles.isRealDir(dir)) return false;
		}
		return true;
	}
}
