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
import urllib.parse
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, BotCommand
from telegram.constants import ParseMode
from telegram.ext import (Application, CommandHandler, MessageHandler, CallbackQueryHandler,
                          filters, ContextTypes)

# --- Configuration via environment variables ---
TELEGRAM_TOKEN = os.environ["TELEGRAM_TOKEN"]
CLAUDE_PATH = os.environ.get("CLAUDE_PATH", os.path.expanduser("~/.local/bin/claude"))
ALLOWED_USERS = [int(uid) for uid in os.environ.get("ALLOWED_USERS", "").split(",") if uid.strip()]
ALLOWED_TOOLS = os.environ.get("ALLOWED_TOOLS", "Bash,Read,Glob,Grep,Edit,Write")
LOG_FILE = os.environ.get("LOG_FILE", "/var/log/claude-telegram-bot.log")
SESSION_DIR = os.environ.get("SESSION_DIR", os.path.expanduser("~/.claude/projects"))
IDLE_TIMEOUT = int(os.environ.get("IDLE_TIMEOUT", "300"))
WORKING_DIR = os.environ.get("WORKING_DIR", os.path.expanduser("~"))
UPLOAD_DIR = os.environ.get("UPLOAD_DIR", "/tmp/claude-telegram-uploads")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

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


# --- 영속 claude 프로세스 + stdin/stdout stream-json (OAuth 유지) ---
# 사용자당 claude 프로세스 1개를 띄워 stdin으로 턴을 흘려보낸다.
# 셸 조립/_shell_quote 불필요(인젝션 안전), 같은 프로세스라 매 턴 --resume 불필요.
def _encode_user_turn(text: str) -> bytes:
    """stdin용 NDJSON 한 줄. ⚠️ 미문서화 포맷(claude-code#24594) — 여기만 고치면 됨."""
    obj = {"type": "user",
           "message": {"role": "user", "content": [{"type": "text", "text": text}]}}
    return (json.dumps(obj, ensure_ascii=False) + "\n").encode()


class ClaudeSession:
    def __init__(self):
        self.proc = None
        self.session_id = None
        self.last_activity = 0.0
        self._lock = asyncio.Lock()  # 한 프로세스에 턴이 겹치지 않게 직렬화

    async def _ensure_proc(self):
        if self.proc and self.proc.returncode is None:
            return
        cmd = [CLAUDE_PATH, "-p",
               "--input-format", "stream-json",
               "--output-format", "stream-json",
               "--verbose",
               "--allowedTools", ALLOWED_TOOLS]
        if self.session_id:  # kill 후 재spawn이면 디스크 세션 복원
            cmd += ["--resume", self.session_id]
        self.proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            # stream-json은 이벤트 1개 = 1줄(NDJSON). 큰 파일 Read / 대용량 Bash
            # 출력이 통째로 한 줄에 담기면 readline()이 버퍼 한계를 넘어
            # "Separator is not found, and chunk exceed the limit"로 터진다.
            limit=64 * 1024 * 1024,
            cwd=WORKING_DIR)

    async def send(self, text, on_tool=None) -> str:
        async with self._lock:
            await self._ensure_proc()
            self.proc.stdin.write(_encode_user_turn(text))
            await self.proc.stdin.drain()
            self.last_activity = asyncio.get_event_loop().time()
            result = ""
            while True:
                line = await self.proc.stdout.readline()
                if not line:
                    self.proc = None  # 프로세스 종료
                    break
                self.last_activity = asyncio.get_event_loop().time()
                line = line.decode(errors="replace").strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                etype = event.get("type")
                if etype == "system" and event.get("subtype") == "init":
                    self.session_id = event.get("session_id") or self.session_id
                if etype == "assistant" and on_tool:
                    for block in event.get("message", {}).get("content", []) or []:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            inp = block.get("input", {})
                            desc = (inp.get("description") or inp.get("file_path")
                                    or inp.get("command") or inp.get("pattern") or "")
                            on_tool(block.get("name", ""), str(desc))
                if etype == "result":
                    self.session_id = event.get("session_id") or self.session_id
                    result = event.get("result", "") or ""
                    break
            return result

    def is_idle(self):
        return self.proc is not None and \
            (asyncio.get_event_loop().time() - self.last_activity) > IDLE_TIMEOUT

    async def close(self):
        if self.proc and self.proc.returncode is None:
            try:
                self.proc.stdin.close()
                self.proc.kill()
                await self.proc.wait()
            except Exception:
                pass
        self.proc = None


class SessionManager:
    def __init__(self):
        self.sessions = {}

    def get(self, chat_id) -> ClaudeSession:
        if chat_id not in self.sessions:
            self.sessions[chat_id] = ClaudeSession()
        return self.sessions[chat_id]

    async def reset(self, chat_id):
        if chat_id in self.sessions:
            await self.sessions[chat_id].close()
            self.sessions[chat_id].session_id = None

    async def reaper(self):
        while True:
            await asyncio.sleep(30)
            for cs in list(self.sessions.values()):
                if cs.is_idle():
                    await cs.close()  # 프로세스만 정리, session_id는 보존


claude_sessions = SessionManager()

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


async def extract_and_send_images(chat_id, text, bot):
    """Find markdown images in text, send them as photos to Telegram, and replace them with placeholder text."""
    img_pattern = re.compile(r'!\[(.*?)\]\(([^)]+)\)')
    matches = img_pattern.findall(text)
    
    cleaned_text = text
    for caption, path in matches:
        resolved_path = path.strip()
        if resolved_path.startswith("file://"):
            resolved_path = resolved_path[7:]
        resolved_path = urllib.parse.unquote(resolved_path)
        
        # Check if local file exists
        if os.path.exists(resolved_path) and os.path.isfile(resolved_path):
            try:
                with open(resolved_path, 'rb') as photo_file:
                    await bot.send_photo(
                        chat_id=chat_id,
                        photo=photo_file,
                        caption=caption if caption else None
                    )
                img_tag = f"![{caption}]({path})"
                cleaned_text = cleaned_text.replace(img_tag, f"🖼️ *{caption or 'Image'}*")
            except Exception as img_err:
                logger.error(f"Failed to send local photo {resolved_path}: {img_err}")
        elif resolved_path.startswith(("http://", "https://")):
            try:
                await bot.send_photo(
                    chat_id=chat_id,
                    photo=resolved_path,
                    caption=caption if caption else None
                )
                img_tag = f"![{caption}]({path})"
                cleaned_text = cleaned_text.replace(img_tag, f"🖼️ *{caption or 'Image'}*")
            except Exception as img_err:
                logger.error(f"Failed to send remote photo {resolved_path}: {img_err}")
                
    return cleaned_text


async def send_md(chat_id, text, bot):
    """Send with MarkdownV2, fallback to plain text on failure"""
    try:
        text = await extract_and_send_images(chat_id, text, bot)
    except Exception as e:
        logger.error(f"Failed to extract/send images: {e}")

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
            limit=64 * 1024 * 1024,
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
    await claude_sessions.reset(user_id)
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
    lines.append("\nTap a session below to resume it.")
    # 각 세션을 탭-투-resume 인라인 버튼으로. callback_data는 64바이트 제한이라 idx만 싣는다.
    keyboard = [[InlineKeyboardButton(f"{s['idx']}. [{s['date']}] {s['msg'][:30]}",
                                      callback_data=f"resume:{s['idx']}")]
                for s in sess_list]
    await update.message.reply_text("\n".join(lines),
                                    reply_markup=InlineKeyboardMarkup(keyboard))


def _resume_by_idx(user_id, idx):
    """캐시된 세션 목록에서 idx(1-based) 세션을 활성화. (성공여부, 메시지) 반환."""
    sess_list = cached_session_list.get(user_id)
    if not sess_list:
        return False, "Run /sessions first."
    if idx < 1 or idx > len(sess_list):
        return False, f"Enter a number between 1 and {len(sess_list)}."
    selected = sess_list[idx-1]
    sessions[user_id] = selected["sid"]
    return True, (f"Session restored: [{selected['date']}] {selected['msg']}\n\n"
                  "Messages will now continue in this session.")


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
    _, msg = _resume_by_idx(user_id, idx)
    await update.message.reply_text(msg)


async def on_resume_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/sessions 인라인 버튼(resume:<idx>) 탭 처리."""
    query = update.callback_query
    if not is_authorized(query.from_user.id):
        await query.answer("Not authorized.")
        return
    await query.answer()
    try:
        idx = int(query.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
    ok, msg = _resume_by_idx(query.from_user.id, idx)
    await query.message.reply_text(msg)


async def process_claude_message(chat_id, user_id, message, bot, cleanup_paths=None):
    """영속 claude 프로세스에 stdin으로 한 턴 전송 + 진행상황 중계"""
    cleanup_paths = cleanup_paths or []
    cs = claude_sessions.get(user_id)

    # /resume 으로 사용자가 다른 세션을 골랐으면 라이브 프로세스를 재바인딩
    desired = sessions.get(user_id)
    if desired and desired != cs.session_id:
        await cs.close()
        cs.session_id = desired

    logger.info(f"STREAM user={user_id} sid={cs.session_id} msg={message[:80]!r}")

    progress = {"msg": None, "lock": asyncio.Lock(), "done": False}
    typing_task = None

    def on_tool(tool_name, desc):
        if tool_name in ("Read", "Edit", "Write") and desc:
            desc = os.path.basename(desc)
        status = f"{random.choice(PROGRESS_MESSAGES)} [{tool_name}"
        status += f": {desc[:30]}]" if desc else "]"
        # 콜백은 동기 → 텔레그램 호출은 태스크로 띄움
        asyncio.create_task(_update_progress(bot, chat_id, progress, status))

    try:
        async def keep_typing():
            while True:
                await bot.send_chat_action(chat_id=chat_id, action="typing")
                await asyncio.sleep(5)

        typing_task = asyncio.create_task(keep_typing())

        last_result = await cs.send(message, on_tool=on_tool)

        if cs.session_id:
            sessions[user_id] = cs.session_id

        # 완료 표시를 락 안에서 세워 뒤늦은 on_tool 태스크가 새 메시지를 못 만들게 한다.
        async with progress["lock"]:
            progress["done"] = True
        if progress["msg"]:
            try:
                await bot.delete_message(chat_id=chat_id, message_id=progress["msg"].message_id)
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
        if typing_task:
            typing_task.cancel()
        for path in cleanup_paths:
            try:
                os.remove(path)
            except FileNotFoundError:
                pass
            except Exception as cleanup_err:
                logger.warning(f"Failed to remove temp file {path}: {cleanup_err}")


async def _update_progress(bot, chat_id, progress, status):
    async with progress["lock"]:
        if progress["done"]:  # 턴이 끝난 뒤 도착한 태스크는 새 메시지를 만들지 않는다
            return
        try:
            if progress["msg"]:
                await bot.edit_message_text(chat_id=chat_id,
                                            message_id=progress["msg"].message_id, text=status)
            else:
                progress["msg"] = await bot.send_message(chat_id=chat_id, text=status)
        except Exception:
            pass


async def queue_worker(user_id, bot):
    """Per-user message queue worker for sequential processing"""
    queue = user_queues[user_id]
    while True:
        chat_id, message, cleanup_paths = await queue.get()
        try:
            await process_claude_message(chat_id, user_id, message, bot, cleanup_paths)
        except Exception as e:
            logger.error(f"Queue worker error: {e}")
        finally:
            queue.task_done()


async def enqueue_message(update: Update, context: ContextTypes.DEFAULT_TYPE, message, cleanup_paths=None):
    """공용 큐 적재 헬퍼 — 사용자별 워커 보장 + cleanup 경로 전달"""
    user_id = update.effective_user.id
    chat_id = update.message.chat_id

    if user_id not in user_queues:
        user_queues[user_id] = asyncio.Queue()
        asyncio.create_task(queue_worker(user_id, context.bot))

    queue = user_queues[user_id]
    if queue.qsize() > 0:
        await update.message.reply_text(f"{queue.qsize()} message(s) queued. Will respond in order.")

    await queue.put((chat_id, message, cleanup_paths or []))


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return

    user_id = update.effective_user.id
    message = update.message.text
    logger.info(f"User {user_id}: {message[:100]}")
    await enqueue_message(update, context, message)


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return

    user_id = update.effective_user.id
    caption = (update.message.caption or "Analyze this image").strip()
    logger.info(f"User {user_id} sent photo: {caption[:100]}")

    photo = update.message.photo[-1]
    file = await context.bot.get_file(photo.file_id)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    img_path = os.path.join(UPLOAD_DIR, f"photo_{time.time_ns()}.jpg")
    await file.download_to_drive(img_path)
    logger.info(f"Photo saved: {img_path}")

    message = f"{caption}\n\nImage file path: {img_path}"
    await enqueue_message(update, context, message, [img_path])


def safe_upload_name(filename, fallback="attachment") -> str:
    name = os.path.basename(filename or fallback)
    name = re.sub(r"[^A-Za-z0-9._-]+", "_", name).strip("._")
    return name or fallback


async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return

    user_id = update.effective_user.id
    document = update.message.document
    caption = (update.message.caption or "Review this attached file").strip()
    safe_name = safe_upload_name(document.file_name)
    os.makedirs(UPLOAD_DIR, exist_ok=True)
    local_path = os.path.join(UPLOAD_DIR, f"doc_{time.time_ns()}_{safe_name}")

    tg_file = await context.bot.get_file(document.file_id)
    await tg_file.download_to_drive(local_path)

    suffix = os.path.splitext(local_path)[1].lower()
    mime_type = document.mime_type or ""
    is_image = suffix in IMAGE_EXTENSIONS or mime_type.startswith("image/")
    kind = "이미지 파일" if is_image else "첨부 파일"
    message = (
        f"{caption}\n\n"
        f"Claude가 확인할 수 있도록 {kind}을 로컬에 저장했습니다.\n"
        f"Local file path: {local_path}\n"
        f"Original filename: {document.file_name or safe_name}\n"
        f"MIME type: {mime_type or 'unknown'}\n\n"
        "위 경로를 Read 도구로 읽어 내용을 확인하세요."
    )

    logger.info(f"User {user_id} sent document: {safe_name}")
    await enqueue_message(update, context, message, [local_path])


async def handle_location(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return

    user_id = update.effective_user.id
    location = update.message.location
    message = (
        "User shared a Telegram location.\n\n"
        f"Latitude: {location.latitude}\n"
        f"Longitude: {location.longitude}\n"
        f"Map: https://maps.google.com/?q={location.latitude},{location.longitude}"
    )
    if location.horizontal_accuracy is not None:
        message += f"\nHorizontal accuracy: {location.horizontal_accuracy} meters"
    if location.live_period is not None:
        message += f"\nLive period: {location.live_period} seconds"
    if location.heading is not None:
        message += f"\nHeading: {location.heading}"
    if location.proximity_alert_radius is not None:
        message += f"\nProximity alert radius: {location.proximity_alert_radius} meters"

    logger.info(f"User {user_id} sent location: {location.latitude},{location.longitude}")
    await enqueue_message(update, context, message)


async def handle_venue(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update.effective_user.id):
        return

    user_id = update.effective_user.id
    venue = update.message.venue
    location = venue.location
    message = (
        "User shared a Telegram venue.\n\n"
        f"Title: {venue.title}\n"
        f"Address: {venue.address}\n"
        f"Latitude: {location.latitude}\n"
        f"Longitude: {location.longitude}\n"
        f"Map: https://maps.google.com/?q={location.latitude},{location.longitude}"
    )

    logger.info(f"User {user_id} sent venue: {venue.title}")
    await enqueue_message(update, context, message)


def _shell_quote(s: str) -> str:
    return "'" + s.replace("'", "'\\''") + "'"


def main():
    async def _post_init(application):
        asyncio.create_task(claude_sessions.reaper())  # idle 프로세스 정리
        # ☰ 메뉴 버튼에 명령어 목록 등록
        await application.bot.set_my_commands([
            BotCommand("start", "Show help"),
            BotCommand("sessions", "List recent sessions"),
            BotCommand("resume", "Resume a session by number"),
            BotCommand("reset", "Start a new session"),
        ])

    app = Application.builder().token(TELEGRAM_TOKEN).post_init(_post_init).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("sessions", cmd_sessions))
    app.add_handler(CommandHandler("resume", cmd_resume))
    app.add_handler(CallbackQueryHandler(on_resume_button, pattern=r"^resume:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))
    app.add_handler(MessageHandler(filters.VENUE, handle_venue))
    app.add_handler(MessageHandler(filters.LOCATION, handle_location))

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
