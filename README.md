# Claude CLI Telegram Bridge

Bridge between [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) and Telegram. Send messages to your Telegram bot, get responses from Claude — with real-time progress updates, session management, and image analysis.

## Highlights

- Use Telegram as a remote interface for Claude Code CLI
- Stream progress while Claude reads files, runs commands, and edits code
- Resume prior CLI sessions from chat
- Send photos for image-aware prompts
- Restrict access with a Telegram user allowlist

## Features

- **Claude CLI integration** — Uses `claude -p` with `stream-json` for real-time streaming
- **Session management** — Resume previous conversations with `/sessions` and `/resume`
- **Progress updates** — See which tools Claude is using as it works
- **Image analysis** — Send photos for Claude to analyze
- **Markdown rendering** — Claude's markdown auto-converted to Telegram MarkdownV2
- **Message queue** — Sequential processing per user, no dropped messages
- **User allowlist** — Restrict access to authorized Telegram user IDs
- **Activity-based timeout** — 5-minute idle watchdog (configurable), resets on each output

## Prerequisites

- Python 3.10+
- [Claude Code CLI](https://docs.anthropic.com/en/docs/claude-code) installed and authenticated
- A Telegram bot token from [@BotFather](https://t.me/BotFather)

## Setup

### 1. Clone and install dependencies

```bash
git clone https://github.com/joonofafa/claude-cli-telegram-bridge.git
cd claude-cli-telegram-bridge
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
```

Edit `.env` with your values:

```env
TELEGRAM_TOKEN=your-telegram-bot-token
ALLOWED_USERS=your-telegram-user-id
```

Get your Telegram user ID by messaging [@userinfobot](https://t.me/userinfobot).

### 3. Run

```bash
python bot.py
```

### Run as systemd service (optional)

```bash
sudo cp claude-telegram-bridge.service /etc/systemd/system/
# Edit the service file to match your paths if needed
sudo systemctl daemon-reload
sudo systemctl enable --now claude-telegram-bridge
```

## Configuration

| Variable | Required | Default | Description |
|---|---|---|---|
| `TELEGRAM_TOKEN` | Yes | — | Telegram bot token |
| `ALLOWED_USERS` | Yes | — | Comma-separated Telegram user IDs |
| `CLAUDE_PATH` | No | `~/.local/bin/claude` | Path to Claude CLI binary |
| `ALLOWED_TOOLS` | No | `Bash,Read,Glob,Grep,Edit,Write` | Tools Claude can use |
| `LOG_FILE` | No | `/var/log/claude-telegram-bot.log` | Log file path |
| `SESSION_DIR` | No | `~/.claude/projects` | Claude session directory |
| `IDLE_TIMEOUT` | No | `300` | Seconds of inactivity before timeout |
| `WORKING_DIR` | No | `~` | Working directory for Claude CLI |

## Bot Commands

| Command | Description |
|---|---|
| `/start` | Show help |
| `/sessions` | List recent Claude sessions |
| `/resume <n>` | Resume session by number |
| `/reset` | Start a new session |

Any other text message is sent directly to Claude. Photos are saved and passed to Claude for analysis.

## How It Works

```
Telegram → Bot (python-telegram-bot) → Claude CLI (stream-json) → Bot → Telegram
```

1. User sends a message in Telegram
2. Bot queues the message (per-user sequential processing)
3. Bot spawns `claude -p` with `--output-format stream-json --verbose`
4. Bot reads the stream, sends progress updates as Claude uses tools
5. On completion, the final result is sent back with MarkdownV2 formatting
6. Session ID is stored for conversation continuity

## License

MIT
