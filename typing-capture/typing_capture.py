#!/usr/bin/env python3
"""
typing_capture.py — macOS keystroke capture daemon.

Captures text the user TYPES (not screen content) via CGEventTap.
Accumulates keystrokes per app/window into a session buffer.
Flushes on: Enter, Tab, app-switch, periodic (60 s), or exit.
Logs to ~/typing-logs/YYYY-MM-DD.json.
"""

import sys
import json
import signal
import datetime
import threading
import time
from pathlib import Path
from typing import Optional

if sys.platform != "darwin":
    sys.exit("Error: macOS only.")

try:
    import Quartz
    from Quartz import (
        CGEventTapCreate, CGEventTapEnable,
        CGEventGetIntegerValueField, CGEventGetFlags,
        CGEventKeyboardGetUnicodeString,
        CFRunLoopAddSource, CFRunLoopGetMain,
        CFMachPortCreateRunLoopSource, kCFRunLoopDefaultMode,
        kCGHIDEventTap, kCGEventKeyDown, kCGEventFlagsChanged,
        kCGHeadInsertEventTap, kCGEventTapOptionListenOnly,
        kCGKeyboardEventKeycode,
        kCGEventFlagMaskCommand, kCGEventFlagMaskControl,
    )
    from ApplicationServices import (
        AXIsProcessTrustedWithOptions,
        AXUIElementCreateSystemWide,
        AXUIElementCreateApplication,
        AXUIElementCopyAttributeValue,
        kAXFocusedUIElementAttribute, kAXRoleAttribute,
        kAXFocusedWindowAttribute, kAXTitleAttribute,
    )
    from AppKit import NSWorkspace
    from Foundation import NSRunLoop, NSDate
except ImportError as exc:
    sys.exit(
        f"Missing dependency: {exc}\n"
        "Run: pip install pyobjc-core pyobjc-framework-Quartz "
        "pyobjc-framework-Cocoa pyobjc-framework-ApplicationServices"
    )

# ── Key codes ──────────────────────────────────────────────────────────────

KEY_RETURN  = 36
KEY_TAB     = 48
KEY_DEL     = 51    # Backspace
KEY_FWD_DEL = 117   # Forward delete (fn+delete)
KEY_ESCAPE  = 53
KEY_LEFT    = 123;  KEY_RIGHT  = 124
KEY_DOWN    = 125;  KEY_UP     = 126
KEY_PGUP    = 116;  KEY_PGDOWN = 121
KEY_HOME    = 115;  KEY_END    = 119

FN_KEYS = {
    122, 120, 99, 118, 96, 97, 98, 100, 101, 109, 103, 111,  # F1-F12
    105, 107, 113, 106, 64, 79, 80, 90,                        # F13-F20
}
NAV_KEYS = {
    KEY_ESCAPE, KEY_LEFT, KEY_RIGHT, KEY_DOWN, KEY_UP,
    KEY_PGUP, KEY_PGDOWN, KEY_HOME, KEY_END,
}
SKIP_KEYS = FN_KEYS | NAV_KEYS

# ── Config ─────────────────────────────────────────────────────────────────

LOG_DIR         = Path.home() / "typing-logs"
MIN_CHARS       = 3
LOG_BUFFER_SIZE = 1
PERIODIC_SECS   = 60
APP_POLL_SECS   = 0.5


class TypingCapture:
    def __init__(self) -> None:
        self._lock     = threading.Lock()
        self._ctx_lock = threading.Lock()

        # "bundle|window" → accumulated typed text
        self._buf: dict = {}
        # entries pending disk write
        self._log_q: list = []
        # last saved entry (for consecutive dedup)
        self._last: Optional[dict] = None

        # App context — written by watcher thread, read by main thread
        self._app    = ""
        self._bundle = ""
        self._window = ""
        self._is_pw  = False

        LOG_DIR.mkdir(parents=True, exist_ok=True)

    # ── Context accessors ──────────────────────────────────────────────────

    def get_ctx(self):
        with self._ctx_lock:
            return self._app, self._bundle, self._window

    def set_ctx(self, app: str, bundle: str, window: str) -> None:
        with self._ctx_lock:
            self._app, self._bundle, self._window = app, bundle, window

    def is_password(self) -> bool:
        with self._ctx_lock:
            return self._is_pw

    def set_password(self, val: bool) -> None:
        was_pw = False
        with self._ctx_lock:
            was_pw, self._is_pw = self._is_pw, val
        if val and not was_pw:
            # Just switched into a password field — discard partial buffer
            app, bundle, window = self.get_ctx()
            with self._lock:
                self._buf.pop(bundle, None)

    # ── Keystroke handler ─────────────────────────────────────────────────

    def on_keydown(self, keycode: int, char: str, flags: int) -> None:
        if keycode in SKIP_KEYS:
            return

        app, bundle, window = self.get_ctx()
        k = bundle  # key by app only — window title changes must not split sentences

        if keycode in (KEY_DEL, KEY_FWD_DEL):
            with self._lock:
                buf = self._buf.get(k, "")
                if buf:
                    self._buf[k] = buf[:-1]
            return

        if keycode == KEY_RETURN:
            self._flush(k, "enter", app, bundle, window, clear=True)
            return

        if keycode == KEY_TAB:
            self._flush(k, "tab", app, bundle, window, clear=True)
            return

        # Skip command / control shortcuts (not typing)
        if flags & (kCGEventFlagMaskCommand | kCGEventFlagMaskControl):
            return

        if not char or not char.isprintable():
            return

        if self.is_password():
            return

        with self._lock:
            self._buf[k] = self._buf.get(k, "") + char

    # ── Flush ─────────────────────────────────────────────────────────────

    def _flush(
        self, key: str, trigger: str,
        app: str, bundle: str, window: str,
        *, clear: bool,
    ) -> None:
        with self._lock:
            text = self._buf.get(key, "")
            if clear:
                self._buf.pop(key, None)

        text = text.strip()
        if len(text) < MIN_CHARS:
            return

        entry = {
            "ts":      datetime.datetime.now().isoformat(timespec="seconds"),
            "trigger": trigger,
            "app":     app,
            "bundle":  bundle,
            "window":  window,
            "text":    text,
        }

        last = self._last
        if (
            last
            and last["text"]   == text
            and last["app"]    == app
            and last["window"] == window
        ):
            return  # consecutive duplicate

        self._last = entry
        print(f"[capture] {trigger}: [{app}] {text[:60]!r}", file=sys.stderr)

        to_write: Optional[list] = None
        with self._lock:
            self._log_q.append(entry)
            if len(self._log_q) >= LOG_BUFFER_SIZE:
                to_write = list(self._log_q)
                self._log_q.clear()

        if to_write:
            self._write(to_write)

    def on_app_switch(self, prev_app: str, prev_bundle: str, prev_window: str) -> None:
        k = prev_bundle
        self._flush(k, "app_switch", prev_app, prev_bundle, prev_window, clear=True)

    def on_periodic(self) -> None:
        """Snapshot current buffer without clearing it (long-form writing)."""
        app, bundle, window = self.get_ctx()
        k = bundle
        with self._lock:
            text = self._buf.get(k, "")
        text = text.strip()
        if len(text) < MIN_CHARS:
            return
        entry = {
            "ts":      datetime.datetime.now().isoformat(timespec="seconds"),
            "trigger": "periodic",
            "app": app, "bundle": bundle, "window": window,
            "text": text,
        }
        last = self._last
        if last and last["text"] == text and last["app"] == app:
            return
        self._last = entry
        with self._lock:
            self._log_q.append(entry)
            to_write = list(self._log_q)
            self._log_q.clear()
        self._write(to_write)

    def flush_all(self, trigger: str = "flush") -> None:
        """Flush every buffer — called on exit."""
        with self._lock:
            keys = list(self._buf.keys())
        app, bundle, window = self.get_ctx()
        for k in keys:
            b, _, w = k.partition("|")
            a = app if b == bundle else b
            self._flush(k, trigger, a, b, w, clear=True)
        with self._lock:
            to_write = list(self._log_q)
            self._log_q.clear()
        self._write(to_write)

    # ── Disk I/O ──────────────────────────────────────────────────────────

    def _write(self, entries: list) -> None:
        if not entries:
            return
        path = LOG_DIR / f"{datetime.date.today().isoformat()}.json"
        try:
            data: list = []
            if path.exists():
                try:
                    with open(path) as f:
                        data = json.load(f)
                except (json.JSONDecodeError, OSError):
                    data = []
            data.extend(entries)
            tmp = path.with_suffix(".json.tmp")
            with open(tmp, "w") as f:
                json.dump(data, f, indent=2, ensure_ascii=False)
            tmp.rename(path)
        except Exception as e:
            print(f"[capture] write error: {e}", file=sys.stderr)


# ── Singleton ──────────────────────────────────────────────────────────────

capture = TypingCapture()


# ── Character extraction ───────────────────────────────────────────────────

def _get_char(event) -> str:
    """Extract the unicode character produced by a keyboard event."""
    try:
        result = CGEventKeyboardGetUnicodeString(event, 10, None, None)
        if isinstance(result, tuple) and len(result) >= 2:
            ch = result[1]
            return str(ch) if ch else ""
        if isinstance(result, str):
            return result
        return ""
    except Exception:
        return ""


# ── Event tap callback ─────────────────────────────────────────────────────

def _tap_callback(proxy, event_type, event, refcon):
    try:
        if event_type == kCGEventKeyDown:
            keycode = int(CGEventGetIntegerValueField(event, kCGKeyboardEventKeycode))
            flags   = int(CGEventGetFlags(event))
            char    = _get_char(event)
            capture.on_keydown(keycode, char, flags)
    except Exception as e:
        print(f"[capture] tap error: {e}", file=sys.stderr)
    return event


# ── AX / NSWorkspace helpers ───────────────────────────────────────────────

def _frontmost():
    """Return (name, bundle, pid) of the frontmost application."""
    try:
        ws  = NSWorkspace.sharedWorkspace()
        app = ws.frontmostApplication()
        if not app:
            return "", "", 0
        return (
            str(app.localizedName()    or ""),
            str(app.bundleIdentifier() or ""),
            int(app.processIdentifier()),
        )
    except Exception:
        return "", "", 0


def _window_title(pid: int) -> str:
    if not pid:
        return ""
    try:
        ax  = AXUIElementCreateApplication(pid)
        err, win = AXUIElementCopyAttributeValue(ax, kAXFocusedWindowAttribute, None)
        if err or win is None:
            return ""
        err, title = AXUIElementCopyAttributeValue(win, kAXTitleAttribute, None)
        return str(title) if not err and title else ""
    except Exception:
        return ""


def _is_password_field() -> bool:
    try:
        sys_ax = AXUIElementCreateSystemWide()
        err, focused = AXUIElementCopyAttributeValue(
            sys_ax, kAXFocusedUIElementAttribute, None
        )
        if err or focused is None:
            return False
        err, role = AXUIElementCopyAttributeValue(focused, kAXRoleAttribute, None)
        return not err and str(role) == "AXSecureTextField"
    except Exception:
        return False


# ── Background threads ────────────────────────────────────────────────────

def _app_watcher() -> None:
    """Polls frontmost app every APP_POLL_SECS. Flushes only on bundle (app) switch."""
    prev_bundle = ""
    prev_name   = ""
    while True:
        time.sleep(APP_POLL_SECS)
        try:
            name, bundle, pid = _frontmost()
            window = _window_title(pid)
            # Always update window title in context (for metadata) without flushing
            capture.set_ctx(name, bundle, window)
            # Only flush when the actual app (bundle) changes
            if bundle != prev_bundle:
                if prev_bundle:
                    capture.on_app_switch(prev_name, prev_bundle, window)
                prev_bundle = bundle
                prev_name   = name
            # Refresh password-field cache on every poll (catches intra-app focus moves)
            capture.set_password(_is_password_field())
        except Exception as e:
            print(f"[capture] watcher: {e}", file=sys.stderr)


def _periodic() -> None:
    while True:
        time.sleep(PERIODIC_SECS)
        try:
            capture.on_periodic()
        except Exception as e:
            print(f"[capture] periodic: {e}", file=sys.stderr)


# ── Startup ────────────────────────────────────────────────────────────────

def _check_perms() -> None:
    if not AXIsProcessTrustedWithOptions({"AXTrustedCheckOptionPrompt": True}):
        print(
            "Accessibility permission required.\n"
            "System Settings → Privacy & Security → Accessibility → + →\n"
            f"  {sys.executable}"
        )
        sys.exit(1)


def main() -> None:
    _check_perms()

    # Bootstrap context so buffers have a key from the first keystroke
    name, bundle, pid = _frontmost()
    capture.set_ctx(name, bundle, _window_title(pid))
    capture.set_password(_is_password_field())

    # Create listen-only CGEventTap on the HID event stream
    tap = CGEventTapCreate(
        kCGHIDEventTap,
        kCGHeadInsertEventTap,
        kCGEventTapOptionListenOnly,
        (1 << kCGEventKeyDown) | (1 << kCGEventFlagsChanged),
        _tap_callback,
        None,
    )
    if tap is None:
        print(
            "Input Monitoring permission required.\n"
            "System Settings → Privacy & Security → Input Monitoring → + →\n"
            f"  {sys.executable}\n"
            "Then restart the daemon."
        )
        sys.exit(1)

    src = CFMachPortCreateRunLoopSource(None, tap, 0)
    CFRunLoopAddSource(CFRunLoopGetMain(), src, kCFRunLoopDefaultMode)
    CGEventTapEnable(tap, True)

    def _exit(sig, _frame):
        print("[capture] shutting down…", file=sys.stderr)
        capture.flush_all()
        sys.exit(0)

    signal.signal(signal.SIGTERM, _exit)
    signal.signal(signal.SIGINT,  _exit)

    threading.Thread(target=_app_watcher, daemon=True, name="app-watcher").start()
    threading.Thread(target=_periodic,    daemon=True, name="periodic").start()

    print(f"[capture] started  →  {LOG_DIR}", file=sys.stderr)
    print(f"[capture] python   →  {sys.executable}", file=sys.stderr)

    # Drive both the CF (event tap) and Cocoa run loops with 0.5 s ticks
    rl = NSRunLoop.currentRunLoop()
    while True:
        rl.runUntilDate_(NSDate.dateWithTimeIntervalSinceNow_(0.5))


if __name__ == "__main__":
    main()
