#!/usr/bin/env python3
"""Claude CLI Telegram Bridge — Connect Claude Code CLI to Telegram"""

import asyncio
import subprocess
import json
import re
import os
import logging
import random
import time
from telegram import Update
from telegram.constants import ParseMode
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes

# --- Configuration via environment variables ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CLAUDE_PATH = os.environ.get("CLAUDE_PATH", os.path.expanduser("~/.local/bin/claude"))
ALLOWED_USERS = [int(uid) for uid in os.environ.get("ALLOWED_USERS", "").split(",") if uid.strip()]
ALLOWED_TOOLS = os.environ.get("ALLOWED_TOOLS", "Bash,Read,Glob,Grep,Edit,Write")
LOG_FILE = os.environ.get("LOG_FILE", "/var/log/claude-telegram-bot.log")
SESSION_DIR = os.environ.get("SESSION_DIR", os.path.expanduser("~/.claude/projects"))
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "300"))
WORKING_DIR = os.environ.get("WORKING_DIR", os.path.expanduser("~"))

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

sessions = {}
cached_session_list = {}
user_queues = {}

PROGRESS_MESSAGES = [
    "Analyzing...",
    "Checking code...",
    "Working on it...",
    "Just a moment...",
    "Almost there...",
    "Reading files...",
    "Running command...",
    "Wrapping up...",
]


def is_authorized(user_id: int) -> bool:
    return not ALLOWED_USERS or user_id in ALLOWED_USERS


def md_to_tg(text: str) -> str:
    """Convert Claude markdown to Telegram MarkdownV2"""
    code_blocks = []
    def save_code_block(m):
        code_blocks.append(m.group(0))
        return f"\x00CODEBLOCK{len(code_blocks)-1}\x00"
    text = re.sub(r'```[\s\S]*?```', save_code_block, text)

    inline_codes = []
    def save_inline_code(m):
        inline_codes.append(m.group(0))
        return f"\x00INLINE{len(inline_codes)-1}\x00"
    text = re.sub(r'`[^`]+`', save_inline_code, text)

    bold_parts = []
    def save_bold(m):
        bold_parts.append(m.group(1))
        return f"\x00BOLD{len(bold_parts)-1}\x00"
    text = re.sub(r'\*\*(.+?)\*\*', save_bold, text)

    italic_parts = []
    def save_italic(m):
        italic_parts.append(m.group(1))
        return f"\x00ITALIC{len(italic_parts)-1}\x00"
    text = re.sub(r'\*(.+?)\*', save_italic, text)

    strike_parts = []
    def save_strike(m):
        strike_parts.append(m.group(1))
        return f"\x00STRIKE{len(strike_parts)-1}\x00"
    text = re.sub(r'~~(.+?)~~', save_strike, text)

    link_parts = []
    def save_link(m):
        link_parts.append((m.group(1), m.group(2)))
        return f"\x00LINK{len(link_parts)-1}\x00"
    text = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', save_link, text)

    header_parts = []
    def save_header(m):
        header_parts.append(m.group(2))
        return f"\x00HEADER{len(header_parts)-1}\x00"
    text = re.sub(r'^(#{1,6})\s+(.+)$', save_header, text, flags=re.MULTILINE)

    text = re.sub(r'^- ', '• ', text, flags=re.MULTILINE)

    lines = text.split('\n')
    new_lines = []
    for line in lines:
        if re.match(r'^\|[\s\-:|]+\|$', line):
            continue
        if line.startswith('|') and line.endswith('|'):
            cells = [c.strip() for c in line.strip('|').split('|')]
            new_lines.append('  '.join(cells))
        else:
            new_lines.append(line)
    text = '\n'.join(new_lines)

    text = re.sub(r'^---+$', '', text, flags=re.MULTILINE)

    escape_chars = r'_[]()~>#+=|{}.!-'
    escaped = ''
    for ch in text:
        if ch in escape_chars and not ch == '\x00':
            escaped += '\\' + ch
        else:
            escaped += ch
    text = escaped

    for i, (link_text, url) in enumerate(link_parts):
        esc_text = re.sub(r'([_\[\]()~`>#+=|{}.!-])', r'\\\1', link_text)
        esc_url = url.replace(')', '\\)').replace('(', '\\(')
        text = text.replace(f'\x00LINK{i}\x00', f'[{esc_text}]({esc_url})')

    for i, inner in enumerate(header_parts):
        esc_inner = re.sub(r'([_\[\]()~`>#+=|{}.!-])', r'\\\1', inner)
        text = text.replace(f'\x00HEADER{i}\x00', f'*{esc_inner}*')

    for i, inner in enumerate(bold_parts):
        esc_inner = re.sub(r'([_\[\]()~`>#+=|{}.!-])', r'\\\1', inner)
        text = text.replace(f'\x00BOLD{i}\x00', f'*{esc_inner}*')

    for i, inner in enumerate(italic_parts):
        esc_inner = re.sub(r'([_\[\]()~`>#+=|{}.!-])', r'\\\1', inner)
        text = text.replace(f'\x00ITALIC{i}\x00', f'_{esc_inner}_')

    for i, inner in enumerate(strike_parts):
        esc_inner = re.sub(r'([_\[\]()~`>#+=|{}.!-])', r'\\\1', inner)
        text = text.replace(f'\x00STRIKE{i}\x00', f'~{esc_inner}~')

    for i, code in enumerate(inline_codes):
        raw = code.strip('`')
        text = text.replace(f'\x00INLINE{i}\x00', f'`{raw}`')

    for i, code in enumerate(code_blocks):
        text = text.replace(f'\x00CODEBLOCK{i}\x00', code)

    return text


async def send_md(chat_id, text, bot):
    """Send with MarkdownV2, fallback to plain text on failure"""
    try:
        md_text = md_to_tg(text)
        if len(md_text) > 4000:
            for i in range(0, len(md_text), 4000):
                await bot.send_message(chat_id=chat_id, text=md_text[i:i+4000], parse_mode=ParseMode.MARKDOWN_V2)
        else:
            await bot.send_message(chat_id=chat_id, text=md_text, parse_mode=ParseMode.MARKDOWN_V2)
    except Exception as e:
        logger.warning(f"MarkdownV2 failed: {e}, falling back to plain text")
        if len(text) > 4000:
            for i in range(0, len(text), 4000):
                await bot.send_message(chat_id=chat_id, text=text[i:i+4000])
        else:
            await bot.send_message(chat_id=chat_id, text=text)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    await update.message.reply_text(
        "Claude CLI Telegram Bridge\n\n"
        "Commands:\n"
        "/sessions - List sessions\n"
        "/resume <number> - Resume session\n"
        "/reset - Reset session\n\n"
        "Send any message to chat with Claude."
    )


async def run_command(cmd: str, timeout: int = 30) -> str:
    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            limit=1024 * 1024,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        output = stdout.decode().strip()
        if not output and stderr:
            output = stderr.decode().strip()
        return output[:4000] if output else "(no output)"
    except asyncio.TimeoutError:
        try:
            proc.kill()
        except Exception:
            pass
        return "Command timed out"
    except Exception as e:
        return f"Error: {e}"


async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    user_id = update.effective_user.id
    if user_id in sessions:
        del sessions[user_id]
    await update.message.reply_text("Session reset.")


def get_session_list(limit=10):
    """Extract recent sessions from session files"""
    import glob
    files = sorted(glob.glob(f"{SESSION_DIR}/*.jsonl"), key=os.path.getmtime, reverse=True)
    files = [f for f in files if os.path.getsize(f) > 5000]
    result = []
    for f in files[:limit]:
        sid = os.path.basename(f).replace(".jsonl", "")
        mtime = os.path.getmtime(f)
        from datetime import datetime as dt
        date = dt.fromtimestamp(mtime).strftime("%m/%d %H:%M")
        first_msg = ""
        try:
            with open(f) as fp:
                for line in fp:
                    try:
                        d = json.loads(line)
                        if d.get("type") == "user" and d.get("message", {}).get("role") == "user":
                            content = d["message"].get("content", "")
                            if isinstance(content, str):
                                first_msg = content[:50]
                            elif isinstance(content, list):
                                for c in content:
                                    if isinstance(c, dict) and c.get("type") == "text":
                                        first_msg = c["text"][:50]
                                        break
                            break
                    except json.JSONDecodeError:
                        continue
        except Exception:
            pass
        if first_msg and not first_msg.startswith("<") and os.path.getsize(f) > 5000:
            result.append({"idx": len(result)+1, "sid": sid, "date": date, "msg": first_msg})
    return result


async def cmd_sessions(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    sess_list = get_session_list()
    cached_session_list[update.effective_user.id] = sess_list
    if not sess_list:
        await update.message.reply_text("No saved sessions.")
        return
    lines = ["Recent sessions:\n"]
    for s in sess_list:
        lines.append(f"{s['idx']}. [{s['date']}] {s['msg']}")
    lines.append("\nUse /resume <number> to continue a session.")
    await update.message.reply_text("\n".join(lines))


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return
    user_id = update.effective_user.id
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /resume <number>\nUse /sessions to see the list.")
        return
    try:
        idx = int(args[0])
    except ValueError:
        await update.message.reply_text("Please enter a number.")
        return
    sess_list = cached_session_list.get(user_id)
    if not sess_list:
        await update.message.reply_text("Run /sessions first.")
        return
    if idx < 1 or idx > len(sess_list):
        await update.message.reply_text(f"Enter a number between 1 and {len(sess_list)}.")
        return
    selected = sess_list[idx-1]
    sessions[user_id] = selected["sid"]
    await update.message.reply_text(f"Session restored: [{selected['date']}] {selected['msg']}\n\nMessages will now continue in this session.")


async def process_claude_message(chat_id, user_id, message, bot):
    """Run Claude CLI with stream-json and send progress updates"""
    session_id = sessions.get(user_id)
    base_cmd = f'{CLAUDE_PATH} -p {_shell_quote(message)} --output-format stream-json --verbose --max-turns 0 --allowedTools "{ALLOWED_TOOLS}"'
    if session_id:
        base_cmd += f' --resume {session_id}'
    cmd = f'{base_cmd} 2>/dev/null'

    logger.info(f"CMD: {cmd[:200]}")

    progress_msg = None
    turn_count = 0
    last_activity = asyncio.get_event_loop().time()
    last_result = ""
    new_session_id = None

    try:
        proc = await asyncio.create_subprocess_shell(
            cmd,
            stdout=asyncio.subprocess.PIPE,
            limit=1024 * 1024,
            stderr=asyncio.subprocess.PIPE,
            cwd=WORKING_DIR
        )

        async def keep_typing():
            while True:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
                await asyncio.sleep(5)

        typing_task = asyncio.create_task(keep_typing())

        async def read_stream():
            nonlocal turn_count, last_result, new_session_id, progress_msg, last_activity
            while True:
                line = await proc.stdout.readline()
                last_activity = asyncio.get_event_loop().time()
                if not line:
                    break
                line = line.decode().strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue

                etype = event.get("type", "")

                if etype == "assistant" and event.get("message", {}).get("role") == "assistant":
                    content = event.get("message", {}).get("content", [])
                    for block in content if isinstance(content, list) else []:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            turn_count += 1
                            tool_name = block.get("name", "")
                            desc = ""
                            tool_input = block.get("input", {})
                            if tool_name == "Bash":
                                desc = tool_input.get("description", tool_input.get("command", "")[:40])
                            elif tool_name in ("Read", "Edit", "Write"):
                                desc = tool_input.get("file_path", "")
                                if desc:
                                    desc = os.path.basename(desc)
                            elif tool_name in ("Glob", "Grep"):
                                desc = tool_input.get("pattern", "")

                            status = f"{random.choice(PROGRESS_MESSAGES)} [{tool_name}"
                            if desc:
                                status += f": {desc[:30]}]"
                            else:
                                status += "]"

                            try:
                                if progress_msg:
                                    await bot.edit_message_text(
                                        chat_id=chat_id,
                                        message_id=progress_msg.message_id,
                                        text=status
                                    )
                                else:
                                    progress_msg = await bot.send_message(chat_id=chat_id, text=status)
                            except Exception:
                                pass

                if etype == "result":
                    last_result = event.get("result", "")
                    new_session_id = event.get("session_id")
                    errors = event.get("errors", [])
                    if errors:
                        logger.warning(f"Claude errors: {errors}")
                        last_result = errors[0]
                    if not last_result and event.get("subtype") == "error_max_turns":
                        last_result = "Turn limit reached. Send another message to continue."
                    elif not last_result:
                        logger.warning(f"Empty result. subtype={event.get('subtype')}")

        async def watchdog():
            while True:
                await asyncio.sleep(10)
                elapsed = asyncio.get_event_loop().time() - last_activity
                if elapsed > IDLE_TIMEOUT:
                    try:
                        proc.kill()
                    except Exception:
                        pass
                    return

        watchdog_task = asyncio.create_task(watchdog())
        await read_stream()
        watchdog_task.cancel()

        if proc.returncode is None:
            await proc.wait()
        await proc.wait()

        if new_session_id:
            sessions[user_id] = new_session_id

        if progress_msg:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=progress_msg.message_id)
            except Exception:
                pass

        if last_result:
            await send_md(chat_id, last_result, bot)
        else:
            await bot.send_message(chat_id=chat_id, text="Failed to generate response.")

    except Exception as e:
        logger.error(f"Claude error: {e}")
        await bot.send_message(chat_id=chat_id, text=f"Error: {e}")
    finally:
        typing_task.cancel()


async def queue_worker(user_id, bot):
    """Per-user message queue worker for sequential processing"""
    queue = user_queues[user_id]
    while True:
        chat_id, message = await queue.get()
        try:
            await process_claude_message(chat_id, user_id, message, bot)
        except Exception as e:
            logger.error(f"Queue worker error: {e}")
        finally:
            queue.task_done()


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return

    user_id = update.effective_user.id
    chat_id = update.message.chat_id
    message = update.message.text
    logger.info(f"User {user_id}: {message[:100]}")

    if user_id not in user_queues:
        user_queues[user_id] = asyncio.Queue()
        asyncio.create_task(queue_worker(user_id, context.bot))

    queue = user_queues[user_id]
    if queue.qsize() > 0:
        await update.message.reply_text(f"{queue.qsize()} message(s) queued. Will respond in order.")

    await queue.put((chat_id, message))


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return

    user_id = update.effective_user.id
    chat_id = update.message.chat_id
    caption = update.message.caption or "Analyze this image"
    logger.info(f"User {user_id} sent photo: {caption[:100]}")

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    img_path = f"/tmp/telegram_img_{int(time.time())}.jpg"
    await file.download_to_drive(img_path)
    logger.info(f"Photo saved: {img_path}")

    message = f"{caption}\n\nImage file path: {img_path}"

    if user_id not in user_queues:
        user_queues[user_id] = asyncio.Queue()
        asyncio.create_task(queue_worker(user_id, context.bot))

    queue = user_queues[user_id]
    if queue.qsize() > 0:
        await update.message.reply_text(f"{queue.qsize()} message(s) queued. Will respond in order.")

    await queue.put((chat_id, message))


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def main():
    app = Application.builder().token(TELEGRAM_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))

    async def unknown_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
        if not is_authorized(update.effective_user.id):
            return
        await update.message.reply_text(
            "Unknown command.\n\n"
            "Available commands:\n"
            "/sessions - List sessions\n"
            "/resume <number> - Resume session\n"
            "/reset - Reset session"
        )
    app.add_handler(MessageHandler(filters.COMMAND, unknown_command))

    logger.info("Claude CLI Telegram Bridge started")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
