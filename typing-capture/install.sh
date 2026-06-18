#!/usr/bin/env bash
# install.sh — Set up typing_capture as a persistent macOS launchd service.
# Run once: bash install.sh

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$SCRIPT_DIR/.venv"
LOG_DIR="$HOME/typing-logs"
PLIST_LABEL="com.user.typing-capture"
PLIST="$HOME/Library/LaunchAgents/${PLIST_LABEL}.plist"
PYTHON="$VENV/bin/python3"
SCRIPT="$SCRIPT_DIR/typing_capture.py"

echo "══════════════════════════════════════════"
echo "  typing_capture installer"
echo "══════════════════════════════════════════"

# ── 1. Virtual environment ─────────────────────────────────────────────────
echo ""
echo "[1/4] Creating virtual environment at $VENV …"
python3 -m venv "$VENV"

echo "[1/4] Installing dependencies …"
"$VENV/bin/pip" install --quiet --upgrade pip
"$VENV/bin/pip" install --quiet \
    pyobjc-core \
    pyobjc-framework-Quartz \
    pyobjc-framework-Cocoa \
    pyobjc-framework-ApplicationServices

echo "[1/4] Done. Python: $PYTHON"

# ── 2. Log directory ───────────────────────────────────────────────────────
echo ""
echo "[2/4] Creating log directory $LOG_DIR …"
mkdir -p "$LOG_DIR"

# ── 3. LaunchAgent plist ───────────────────────────────────────────────────
echo ""
echo "[3/4] Writing LaunchAgent plist to $PLIST …"
mkdir -p "$(dirname "$PLIST")"

cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${PLIST_LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${PYTHON}</string>
        <string>${SCRIPT}</string>
    </array>

    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>${LOG_DIR}/capture-stdout.log</string>
    <key>StandardErrorPath</key>
    <string>${LOG_DIR}/capture-stderr.log</string>

    <key>WorkingDirectory</key>
    <string>${SCRIPT_DIR}</string>

    <key>ThrottleInterval</key>
    <integer>10</integer>

    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>${HOME}</string>
        <key>PATH</key>
        <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
    </dict>
</dict>
</plist>
PLIST_EOF

# ── 4. Load service ────────────────────────────────────────────────────────
echo ""
echo "[4/4] Loading LaunchAgent …"
launchctl unload "$PLIST" 2>/dev/null || true
launchctl load -w "$PLIST"

echo ""
echo "══════════════════════════════════════════"
echo "  Done. Daemon is now running."
echo ""
echo "  IMPORTANT — grant both permissions for:"
echo "    $PYTHON"
echo ""
echo "  1. Accessibility"
echo "     System Settings → Privacy & Security → Accessibility → +"
echo ""
echo "  2. Input Monitoring"
echo "     System Settings → Privacy & Security → Input Monitoring → +"
echo ""
echo "  After granting permissions, restart the daemon:"
echo "    launchctl unload $PLIST"
echo "    launchctl load -w $PLIST"
echo ""
echo "  Logs        : $LOG_DIR/YYYY-MM-DD.json"
echo "  Daemon log  : $LOG_DIR/capture-stderr.log"
echo "  Status      : launchctl list | grep typing-capture"
echo "  Stop        : launchctl unload $PLIST"
echo "  Read today  : $PYTHON $SCRIPT_DIR/read_today.py"
echo "  Agent mode  : $PYTHON $SCRIPT_DIR/read_today.py --agent"
echo "══════════════════════════════════════════"
