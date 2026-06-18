# typing-capture

Lightweight macOS daemon that captures every text you type, in every app,
to a local rotating JSON log. No screen recording. No cloud. ~30 MB RAM.

## How it works

Uses the macOS **Accessibility API** (not a raw keylogger) to read the actual
value of focused text fields. This means:

- ✅ Captures typed text, pasted text, and autocomplete completions
- ✅ Works in Notes, Word, Pages, Keynote, Notion, all browsers, Cursor, Claude
- ✅ Automatically skips password fields (`AXSecureTextField`)
- ✅ All data stays local — `~/typing-logs/YYYY-MM-DD.json`
- ✅ Starts at login, restarts on crash (launchd)

Capture triggers:
| Trigger | When |
|---------|------|
| `enter` | You press Return/Enter |
| `tab` | You press Tab (leaving a field) |
| `app_switch` | You switch to a different app |
| `periodic` | Every 60s (for long-form writing in Notes, docs) |
| `flush` | On daemon shutdown |

## Installation

```bash
# 1. Clone / copy this folder somewhere permanent (e.g. ~/tools/typing-capture)
# 2. Run the installer
bash install.sh
```

The installer:
- Creates a Python virtualenv with required dependencies
- Writes a launchd plist to `~/Library/LaunchAgents/`
- Starts the daemon immediately

### Permissions (required)

After running `install.sh`, grant two permissions in
**System Settings → Privacy & Security**:

1. **Accessibility** → add the Python binary shown by the installer
2. **Input Monitoring** → add the same Python binary

The daemon won't capture anything until both are granted.
Restart the daemon after granting: `launchctl stop com.user.typing-capture`
(launchd will restart it automatically).

## Log format

Each day creates `~/typing-logs/YYYY-MM-DD.json`:

```json
[
  {
    "ts": "2026-05-29T09:14:32",
    "trigger": "enter",
    "app": "Notes",
    "bundle": "com.apple.Notes",
    "window": "Staffing thesis",
    "text": "The key insight is that small staffing firms under 50M revenue..."
  },
  {
    "ts": "2026-05-29T10:03:11",
    "trigger": "enter",
    "app": "Google Chrome",
    "bundle": "com.google.Chrome",
    "window": "claude.ai",
    "text": "What are the main compliance pain points for EOR providers in Europe?"
  }
]
```

## Reading logs

```bash
# Summary of today
python read_today.py

# All entries for a specific date
python read_today.py --date 2026-05-28

# Filter to one app
python read_today.py --app "Notes"
python read_today.py --app "Chrome"

# Only substantial entries (50+ chars)
python read_today.py --min-chars 50

# Raw JSON
python read_today.py --json

# LLM-ready context block (for daily summary agent)
python read_today.py --agent
```

## Using with your daily agent

In your daily summary pipe, call:
```bash
python ~/tools/typing-capture/read_today.py --agent --min-chars 20
```

This outputs a grouped, de-duplicated, truncated block ready to inject into
your Claude API prompt as the "what I typed today" context.

## Service management

```bash
# Check status
launchctl list | grep typing-capture

# View live logs
tail -f ~/typing-logs/daemon.log

# Stop
launchctl unload ~/Library/LaunchAgents/com.user.typing-capture.plist

# Start
launchctl load -w ~/Library/LaunchAgents/com.user.typing-capture.plist

# Restart
launchctl stop com.user.typing-capture
# (launchd restarts it automatically because KeepAlive=true)

# Uninstall completely
launchctl unload ~/Library/LaunchAgents/com.user.typing-capture.plist
rm ~/Library/LaunchAgents/com.user.typing-capture.plist
```

## Troubleshooting

**Nothing is being captured:**
- Check permissions: System Settings → Privacy & Security → Accessibility
  and Input Monitoring. The virtualenv Python must be listed.
- Check daemon is running: `launchctl list | grep typing-capture`
- Check error log: `cat ~/typing-logs/daemon.err`

**Only some apps are captured:**
- Apps that render their own UI without accessibility support (games, some
  Electron apps with accessibility disabled) won't work. All Apple apps,
  browsers, Office, and most productivity apps work fine.

**Duplicate entries:**
- The dedup logic compares text content per (bundle_id, window_title) key.
  If you see duplicates, the window title is changing between captures.
  This is expected for apps that update the title dynamically.

**High CPU:**
- This shouldn't happen. The daemon is event-driven (no polling loops).
  If you see >5% sustained CPU, check `~/typing-logs/daemon.log` for errors
  and file an issue.

## Privacy notes

- Passwords are never captured (`AXSecureTextField` check in `get_focused_text`)
- All data is stored in `~/typing-logs/` — only you have access
- No network connections are made by this daemon
- To delete all captured data: `rm -rf ~/typing-logs/`
- To exclude specific apps, edit the `SKIP_BUNDLE_IDS` set in `typing_capture.py`
  (add any bundle ID you want ignored, e.g. `"com.1password.1password"`)
