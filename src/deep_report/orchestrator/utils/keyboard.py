#!/usr/bin/env python3
"""Keyboard listener for real-time input during execution."""

import os
import sys
import threading
from typing import Callable, Optional


class KeyboardListener:
    """Non-blocking keyboard listener for real-time key detection.

    Works on Unix-like systems (macOS, Linux) using termios.
    Uses /dev/tty directly to work even when stdin is redirected.
    Falls back gracefully on Windows or when no tty is available.
    """

    def __init__(self, on_key: Callable[[str], None]):
        self.on_key = on_key
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._tty_fd: Optional[int] = None
        self._available = False

        # Check if we can use raw terminal mode via /dev/tty
        self._use_stdin = False
        try:
            import tty
            import termios
            # Try to open /dev/tty directly (works even if stdin is redirected)
            if os.path.exists('/dev/tty'):
                try:
                    fd = os.open('/dev/tty', os.O_RDONLY)
                    os.close(fd)
                    self._available = True
                except OSError:
                    # No controlling terminal (running from subprocess, etc.)
                    pass
            # Fallback: check stdin
            if not self._available and sys.stdin.isatty():
                self._available = True
                self._use_stdin = True
        except ImportError:
            pass

    @property
    def available(self) -> bool:
        """Check if keyboard listening is available."""
        return self._available

    def start(self):
        """Start listening for keyboard input in background thread."""
        if not self._available or self._running:
            return

        self._running = True
        self._thread = threading.Thread(target=self._listen, daemon=True)
        self._thread.start()

    def stop(self):
        """Stop the keyboard listener."""
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.0)
            self._thread = None

    def _listen(self):
        """Main listening loop - runs in background thread."""
        import tty
        import termios
        import select

        # Determine input source
        if self._use_stdin:
            fd = sys.stdin.fileno()
            input_file = sys.stdin
            should_close = False
        else:
            # Open /dev/tty directly for reading
            try:
                fd = os.open('/dev/tty', os.O_RDONLY)
                input_file = os.fdopen(fd, 'r', buffering=1)
                should_close = True
            except OSError:
                self._running = False
                return

        old_settings = termios.tcgetattr(fd)

        try:
            tty.setcbreak(fd)  # Use cbreak for better signal handling

            while self._running:
                # Use select to check for input with timeout
                if select.select([fd], [], [], 0.1)[0]:
                    ch = input_file.read(1)
                    if ch:
                        try:
                            self.on_key(ch)
                        except Exception:
                            pass  # Don't crash on callback errors
        except Exception:
            pass  # Handle edge cases during cleanup
        finally:
            # Restore terminal state with error handling
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except Exception:
                pass
            if should_close:
                try:
                    input_file.close()
                except Exception:
                    pass


class VerboseToggle:
    """Manages verbose mode toggle with keyboard listener."""

    def __init__(self, on_toggle: Optional[Callable[[bool], None]] = None):
        self.enabled = False
        self._on_toggle = on_toggle
        self._listener: Optional[KeyboardListener] = None

    def _handle_key(self, ch: str):
        """Handle key press - toggle on 'v' or 'V'."""
        if ch.lower() == 'v':
            self.enabled = not self.enabled
            if self._on_toggle:
                self._on_toggle(self.enabled)

    def start(self):
        """Start listening for toggle key."""
        self._listener = KeyboardListener(self._handle_key)
        if self._listener.available:
            self._listener.start()
            return True
        return False

    def stop(self):
        """Stop listening."""
        if self._listener:
            self._listener.stop()
            self._listener = None
