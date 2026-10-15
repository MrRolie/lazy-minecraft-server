package net.vanillaextended.vecommands;

import java.util.List;
import java.util.concurrent.CopyOnWriteArrayList;
import org.apache.logging.log4j.Level;
import org.apache.logging.log4j.LogManager;
import org.apache.logging.log4j.core.LogEvent;
import org.apache.logging.log4j.core.Logger;
import org.apache.logging.log4j.core.appender.AbstractAppender;
import org.apache.logging.log4j.core.config.Property;

/** Captures what the "ve-commands" logger emits (fully formatted), so tests can pin the log at the call sites. */
final class LogCapture implements AutoCloseable {
	final List<String> lines = new CopyOnWriteArrayList<>();
	private final Logger logger = (Logger) LogManager.getLogger("ve-commands");
	private final Level before = logger.getLevel();
	private final AbstractAppender appender = new AbstractAppender("capture-" + System.nanoTime(), null, null, true,
			new Property[0]) {
		@Override
		public void append(LogEvent e) {
			lines.add(e.getLevel() + " " + e.getMessage().getFormattedMessage());
		}
	};

	LogCapture() {
		appender.start();
		logger.addAppender(appender);
		logger.setLevel(Level.ALL);
	}

	List<String> matching(String s) {
		return lines.stream().filter(l -> l.contains(s)).toList();
	}

	long bytes() {
		return lines.stream().mapToLong(l -> l.getBytes(java.nio.charset.StandardCharsets.UTF_8).length + 1).sum();
	}

	@Override
	public void close() {
		logger.removeAppender(appender);
		logger.setLevel(before);
		appender.stop();
	}
}
