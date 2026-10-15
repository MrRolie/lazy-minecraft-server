package net.vanillaextended.vecommands;

import java.util.regex.Pattern;

/** Pure text rules shared by the commands and the inbox (MC-free, unit-tested). */
final class Text {
	static final Pattern PLAYER_NAME = Pattern.compile("^[A-Za-z0-9_]{3,16}$");

	private Text() {}

	/**
	 * The say and command cleaner (mod/CONTRACT.md "say"; the bridge implements the same rules):
	 * <ul>
	 * <li>'§' and the code point after it (whatever it is) are removed: legacy formatting codes.</li>
	 * <li>Whitespace (the Unicode White_Space set, see {@link #isWhitespace}) collapses: each run becomes one space,
	 * and none is left at either end. A say cannot pad itself onto what looks like a second chat line.</li>
	 * <li>Controls (C0, DEL, C1), bidi controls and blank fillers ({@link #isBlankFiller}) are removed, so a line
	 * cannot read differently from what it says or pad itself out invisibly.</li>
	 * </ul>
	 */
	static String clean(String s) {
		StringBuilder out = new StringBuilder(s.length());
		boolean space = true; // the last thing kept was a space (or nothing yet): drop more whitespace
		for (int i = 0; i < s.length(); ) {
			int cp = s.codePointAt(i);
			i += Character.charCount(cp);
			if (cp == '§') {
				if (i < s.length()) i += Character.charCount(s.codePointAt(i));
			} else if (isWhitespace(cp)) {
				if (!space) out.append(' ');
				space = true;
			} else if (cp >= 0x20 && cp != 0x7f && !(cp >= 0x80 && cp <= 0x9f) && !isBidiControl(cp) && !isBlankFiller(cp)) {
				out.appendCodePoint(cp);
				space = false;
			}
		}
		int n = out.length();
		return n > 0 && out.charAt(n - 1) == ' ' ? out.substring(0, n - 1) : out.toString();
	}

	/** Unicode White_Space: U+0009-000D, U+0020, U+0085, U+00A0, U+1680, U+2000-200A, U+2028/2029, U+202F, U+205F, U+3000. */
	static boolean isWhitespace(int cp) {
		return (cp >= 0x09 && cp <= 0x0d) || cp == 0x20 || cp == 0x85 || cp == 0xa0 || cp == 0x1680
				|| (cp >= 0x2000 && cp <= 0x200a) || cp == 0x2028 || cp == 0x2029 || cp == 0x202f || cp == 0x205f
				|| cp == 0x3000;
	}

	/**
	 * Characters that are not White_Space but render blank (or not at all), so they could pad a line just the same:
	 * U+2800 braille blank, U+3164 / U+115F / U+1160 / U+FFA0 Hangul fillers, U+200B zero-width space, U+FEFF BOM.
	 * They are removed.
	 */
	static boolean isBlankFiller(int cp) {
		return cp == 0x2800 || cp == 0x3164 || cp == 0x115f || cp == 0x1160 || cp == 0xffa0 || cp == 0x200b
				|| cp == 0xfeff;
	}

	/**
	 * Inbox-sourced text made safe for the server log: cut to {@code max} code points, then every control, bidi
	 * control and line/paragraph separator written as a JSON-style escape (backslash, u, four hex digits). The result is one line that cannot
	 * forge another log line, move the cursor, or hide text (ESC, CR, LF are all escaped).
	 */
	static String forLog(String s, int max) {
		String cut = truncate(s, max);
		StringBuilder out = new StringBuilder(cut.length() + 8);
		cut.codePoints().forEach(cp -> {
			if (cp < 0x20 || cp == 0x7f || (cp >= 0x80 && cp <= 0x9f) || isBidiControl(cp) || cp == 0x2028
					|| cp == 0x2029) {
				out.append(String.format("\\u%04x", cp));
			} else {
				out.appendCodePoint(cp);
			}
		});
		return cut.length() < s.length() ? out.append("…").toString() : out.toString();
	}

	static boolean isBidiControl(int cp) {
		return (cp >= 0x202a && cp <= 0x202e) || (cp >= 0x2066 && cp <= 0x2069) || cp == 0x200e || cp == 0x200f
				|| cp == 0x061c;
	}

	/** The first {@code max} code points of {@code s} (never splits a surrogate pair). */
	static String truncate(String s, int max) {
		if (s.codePointCount(0, s.length()) <= max) return s;
		return s.substring(0, s.offsetByCodePoints(0, max));
	}

	static int length(String s) {
		return s.codePointCount(0, s.length());
	}
}
