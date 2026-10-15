package net.vanillaextended.vecommands;

import static org.junit.jupiter.api.Assertions.*;

import org.junit.jupiter.api.Test;

class TextTest {
	@Test
	void stripsFormattingCodesAndControls() {
		assertEquals("red bold", Text.clean("§cred §lbold§r"));
		assertEquals("a b c", Text.clean("a\nb\tc\u0000\u007f\u009b"));
		assertEquals("x", Text.clean("  x  "));
		assertEquals("", Text.clean("§"));
	}

	@Test
	void stripsBidiControlsAndSeparators() {
		assertEquals("abcdcba", Text.clean("abc\u202edcba\u202c"));
		assertEquals("xyz", Text.clean("\u2066x\u2067y\u2068z\u2069"));
		assertEquals("a b c", Text.clean("a\u2028b\u2029c"));
		assertEquals("lr", Text.clean("\u200el\u200fr\u061c"));
		for (int cp = 0x202a; cp <= 0x202e; cp++) assertTrue(Text.isBidiControl(cp));
		for (int cp = 0x2066; cp <= 0x2069; cp++) assertTrue(Text.isBidiControl(cp));
		assertFalse(Text.isBidiControl('a'));
	}

	@Test
	void collapsesWhitespaceRuns() {
		assertEquals("hello Steve joined the game", Text.clean("hello" + " ".repeat(200) + "Steve joined the game"));
		assertEquals("a b", Text.clean("\u00a0 a \t\r\n\u2028\u3000 b \u00a0"));
		assertEquals("a b", Text.clean("a\u0007 \u0000 b"), "a stripped control does not split a run");
		assertEquals("x", Text.clean("\u2000\u200a\u202f\u205f\u1680\u0085x\u000b\u000c"));
		assertEquals("", Text.clean(" \t\u00a0 "));
		for (int cp : new int[] {0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x20, 0x85, 0xa0, 0x1680, 0x2000, 0x2005, 0x200a, 0x2028,
				0x2029, 0x202f, 0x205f, 0x3000}) {
			assertTrue(Text.isWhitespace(cp), Integer.toHexString(cp));
		}
		assertFalse(Text.isWhitespace(0x200b), "zero-width space is not White_Space");
	}

	@Test
	void blankFillersAreRemoved() {
		assertEquals("a b", Text.clean("a\u2800\u3164\u115f\u1160\uffa0\u200b\ufeff b"));
		assertEquals("", Text.clean("\u2800\u3164\u115f\u1160\uffa0\u200b\ufeff"));
		assertEquals("hello <Notch> hi", Text.clean("hello " + "\u2800".repeat(200) + " <Notch> hi"));
	}

	@Test
	void sectionSignSkipsAWholeCodePoint() {
		assertEquals("x", Text.clean("\u00a7\ud83d\ude00x"));
	}

	@Test
	void forLogEscapesEverythingThatCouldForgeOrHideALine() {
		assertEquals("a\\u001b[31mb\\u000d\\u000a[00:00:00] fake", Text.forLog("a\u001b[31mb\r\n[00:00:00] fake", 100));
		assertEquals("\\u202eevil\\u2028\\u0085", Text.forLog("\u202eevil\u2028\u0085", 100));
		assertEquals("abc…", Text.forLog("abcdef", 3));
		assertEquals("a\\u007fb\\u0080c\\u009fd", Text.forLog("a\u007fb\u0080c\u009fd", 100), "DEL and C1 are escaped");
		assertEquals("é😀", Text.forLog("é😀", 10));
		assertFalse(Text.forLog("x\n\ry", 10).chars().anyMatch(c -> c < 0x20));
	}

	@Test
	void keepsUnicode() {
		assertEquals("café 📌 日本", Text.clean("café 📌 日本"));
	}

	@Test
	void truncatesByCodePoint() {
		assertEquals("ab", Text.truncate("abc", 2));
		assertEquals("📌📌", Text.truncate("📌📌📌", 2));
		assertEquals("abc", Text.truncate("abc", 5));
		assertEquals(3, Text.length("📌ab"));
	}

	@Test
	void playerNameRule() {
		assertTrue(Text.PLAYER_NAME.matcher("Notch").matches());
		assertTrue(Text.PLAYER_NAME.matcher("a_1").matches());
		assertFalse(Text.PLAYER_NAME.matcher("ab").matches());
		assertFalse(Text.PLAYER_NAME.matcher("seventeen_chars_x").matches());
		assertFalse(Text.PLAYER_NAME.matcher("bad name").matches());
		assertFalse(Text.PLAYER_NAME.matcher("Notch\n").matches());
	}
}
