import asyncio
import json
import logging
import os
import random
import re
import subprocess
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional
from zoneinfo import ZoneInfo

import discord
from dotenv import load_dotenv

from claude_cli import build_claude_command
from cron_commands import (
    handle_cron_command,
    handle_daily_command,
    handle_every_command,
    handle_remind_command,
)
from cron_scheduler import cron_scheduler
from speech_to_text import transcribe

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
ALLOWED_USER_IDS = os.environ.get("ALLOWED_USER_IDS", "")

# 可配置的對話管理參數
MAX_MESSAGES_BEFORE_COMPRESS = int(os.environ.get("MAX_MESSAGES_BEFORE_COMPRESS", "16"))

# 系統 prompt
SAFETY_GUARDRAILS = """你是一個 Discord 上的個人 AI 助手。

## 回應規則
- 使用繁體中文回應
- 簡潔直接，省略開場白和結尾客套話
- 程式碼用 markdown code block
- 不確定的事情直接說不確定，不要猜測或編造
- 執行有風險的操作前先說明你要做什麼

## 使用者背景
- 軟體開發者，熟悉 Python、Docker
- 技術問題可以直接給進階回答
"""


def get_current_timestamp(timezone: str = "Asia/Taipei") -> str:
    """取得格式化的當前時間"""
    tz = ZoneInfo(timezone)
    now = datetime.now(tz)
    days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
    return f"Current Date: {now.strftime('%Y-%m-%d')} {days[now.weekday()]} {now.strftime('%H:%M')} ({timezone})"


# Discord 訊息分塊設定
DISCORD_CHAR_LIMIT = 2000
FENCE_PATTERN = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$", re.MULTILINE)

# 執行緒池：用於執行阻塞的 subprocess 呼叫，避免阻塞 asyncio 事件循環
executor = ThreadPoolExecutor(max_workers=4)

# Per-user lock：確保同一使用者的 ask_claude() 不會同時執行
user_locks: dict[int, asyncio.Lock] = {}


def chunk_message(text: str, max_chars: int = DISCORD_CHAR_LIMIT) -> list[str]:
    """智能分塊，保持代碼塊完整

    當訊息超過 Discord 字數限制時，會在適當的位置分割，
    並確保代碼塊（```）在分割處正確關閉和重新開啟。
    """
    if len(text) <= max_chars:
        return [text]

    chunks = []
    current_chunk = ""
    inside_fence = False
    fence_marker = ""
    fence_lang = ""

    lines = text.split("\n")

    for line in lines:
        # 檢測圍欄開始/結束
        fence_match = FENCE_PATTERN.match(line)
        if fence_match:
            marker = fence_match.group(2)
            if not inside_fence:
                inside_fence = True
                fence_marker = marker[0]
                fence_lang = fence_match.group(3).strip()
            elif line.strip().startswith(fence_marker * 3):
                inside_fence = False
                fence_marker = ""
                fence_lang = ""

        # 計算加入這行後的長度
        new_line = line + "\n"
        potential_length = len(current_chunk) + len(new_line)

        # 如果在代碼塊內，需要預留關閉標記的空間
        reserve = len(fence_marker * 3 + "\n") if inside_fence else 0

        if potential_length + reserve > max_chars:
            # 需要分塊
            if inside_fence:
                # 關閉當前代碼塊
                current_chunk += fence_marker * 3 + "\n"

            chunks.append(current_chunk.rstrip("\n"))

            # 開始新塊
            if inside_fence:
                # 重新開啟代碼塊
                current_chunk = fence_marker * 3 + fence_lang + "\n" + new_line
            else:
                current_chunk = new_line
        else:
            current_chunk += new_line

    if current_chunk:
        chunks.append(current_chunk.rstrip("\n"))

    return chunks


# 對話歷史設定
MAX_CONTEXT_CHARS = 8000  # 上下文最大字符數
HISTORY_FILE = Path("conversation_history.json")

# 長期記憶設定
MEMORY_FILE = Path("memory.json")
MAX_MEMORY_FACTS = 20  # 每用戶最多保留的事實數量
MAX_MEMORY_CHARS = 1500  # 記憶注入上下文的最大字符數

# 語音訊息儲存目錄
VOICE_DIR = Path("voice_messages")
VOICE_DIR.mkdir(exist_ok=True)


@dataclass
class Message:
    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime


@dataclass
class ConversationState:
    summary: str = ""  # AI 生成的摘要
    messages: list[Message] = field(default_factory=list)  # 最近的對話


# 每個用戶的對話狀態
conversation_states: dict[int, ConversationState] = {}

# 每個用戶的長期記憶
user_memories: dict[int, list[str]] = {}


def get_user_memory(user_id: int) -> list[str]:
    """取得用戶的長期記憶"""
    return user_memories.get(user_id, [])


def get_conversation_state(user_id: int) -> ConversationState:
    """取得用戶的對話狀態，如果不存在則創建"""
    if user_id not in conversation_states:
        conversation_states[user_id] = ConversationState()
    return conversation_states[user_id]


# AI 摘要設定
MESSAGES_TO_SUMMARIZE = 10  # 壓縮最舊的 5 輪
MAX_SUMMARY_CHARS = 2000  # 摘要最大字符數

SUMMARY_PROMPT = """請將以下對話處理成兩個部分：

## PART 1: 對話摘要
保留：
- 討論的主題和結論
- 待辦事項和承諾
- 關鍵資訊（名字、日期、數字等）

## PART 2: 長期記憶
從對話中提取值得長期記住的用戶事實（跨對話仍有用的資訊），例如：
- 用戶的偏好和習慣
- 用戶的技術背景或專長
- 用戶提到的重要個人資訊
- 用戶做出的重要決策

如果沒有值得記住的事實，PART 2 留空即可。

對話內容：
{conversation}

請嚴格按照以下格式輸出：

===SUMMARY===
（繁體中文摘要，約 200-300 字）

===FACTS===
- 事實1
- 事實2
（每行一個事實，用 - 開頭。沒有則留空）"""


def parse_summary_and_facts(output: str) -> tuple[str, list[str]]:
    """解析 Claude 輸出的摘要和事實

    Returns:
        (summary, facts) tuple
    """
    summary = ""
    facts: list[str] = []

    if "===SUMMARY===" in output and "===FACTS===" in output:
        parts = output.split("===FACTS===")
        summary_part = parts[0].split("===SUMMARY===")[-1].strip()
        facts_part = parts[1].strip() if len(parts) > 1 else ""

        summary = summary_part

        for line in facts_part.split("\n"):
            line = line.strip()
            if line.startswith("- ") and len(line) > 2:
                facts.append(line[2:].strip())
    else:
        # 格式不符，整個輸出當作摘要（向後相容）
        summary = output.strip()

    return summary, facts


def merge_memory_facts(user_id: int, new_facts: list[str]):
    """合併新事實到用戶的長期記憶，去重並限制數量"""
    existing = user_memories.get(user_id, [])

    for fact in new_facts:
        fact = fact.strip()
        if not fact:
            continue
        is_duplicate = fact in existing
        if not is_duplicate:
            existing.append(fact)

    # 保留最新的 MAX_MEMORY_FACTS 個事實
    if len(existing) > MAX_MEMORY_FACTS:
        existing = existing[-MAX_MEMORY_FACTS:]

    user_memories[user_id] = existing


def save_memory():
    """儲存長期記憶到檔案"""
    data = {
        str(uid): {
            "facts": facts,
            "updated_at": datetime.now().isoformat(),
        }
        for uid, facts in user_memories.items()
        if facts
    }
    try:
        MEMORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.error(f"Failed to save memory: {e}")


def load_memory():
    """從檔案載入長期記憶"""
    if not MEMORY_FILE.exists():
        return
    try:
        data = json.loads(MEMORY_FILE.read_text())
        for uid, memory_data in data.items():
            if isinstance(memory_data, dict):
                user_memories[int(uid)] = memory_data.get("facts", [])
            elif isinstance(memory_data, list):
                user_memories[int(uid)] = memory_data
        logger.info(f"Loaded memory for {len(data)} users")
    except Exception as e:
        logger.error(f"Failed to load memory: {e}")


def save_history():
    """儲存歷史到檔案"""
    data = {
        str(uid): {
            "summary": state.summary,
            "messages": [
                {
                    "role": m.role,
                    "content": m.content,
                    "timestamp": m.timestamp.isoformat(),
                }
                for m in state.messages
            ],
        }
        for uid, state in conversation_states.items()
    }
    try:
        HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
    except Exception as e:
        logger.error(f"Failed to save history: {e}")


def load_history():
    """從檔案載入歷史"""
    if not HISTORY_FILE.exists():
        return
    try:
        data = json.loads(HISTORY_FILE.read_text())
        for uid, state_data in data.items():
            # 相容舊格式（純 list）和新格式（dict with summary）
            if isinstance(state_data, list):
                # 舊格式：直接是 messages list
                messages = [
                    Message(
                        m["role"], m["content"], datetime.fromisoformat(m["timestamp"])
                    )
                    for m in state_data
                ]
                conversation_states[int(uid)] = ConversationState(
                    summary="", messages=messages
                )
            else:
                # 新格式：包含 summary 和 messages
                messages = [
                    Message(
                        m["role"], m["content"], datetime.fromisoformat(m["timestamp"])
                    )
                    for m in state_data.get("messages", [])
                ]
                conversation_states[int(uid)] = ConversationState(
                    summary=state_data.get("summary", ""), messages=messages
                )
        logger.info(f"Loaded conversation history for {len(data)} users")
    except Exception as e:
        logger.error(f"Failed to load history: {e}")


def get_allowed_users() -> set[int]:
    if not ALLOWED_USER_IDS:
        return set()
    return {int(uid.strip()) for uid in ALLOWED_USER_IDS.split(",") if uid.strip()}


def is_authorized(user_id: int) -> bool:
    allowed = get_allowed_users()
    if not allowed:
        return True
    return user_id in allowed


COMPRESS_SUMMARY_PROMPT = """以下是多段對話摘要的累積，請將它們整合成一份精簡的摘要，保留最重要的資訊：
- 用戶的核心偏好和設定
- 重要的決策和結論
- 仍然有效的待辦事項
- 關鍵資訊（名字、日期、數字等）

原始摘要：
{summary}

請用繁體中文輸出整合後的摘要（約 300-500 字）："""


def compress_summary(summary: str) -> str:
    """壓縮過長的摘要（同步函數，會阻塞）"""
    prompt = COMPRESS_SUMMARY_PROMPT.format(summary=summary)

    try:
        result = subprocess.run(
            build_claude_command(prompt),
            capture_output=True,
            text=True,
            timeout=60,
        )
        # 檢查返回碼
        if result.returncode != 0:
            logger.error(f"Summary compression CLI error: {result.stderr.strip()}")
            return summary
        compressed = result.stdout.strip()
        if compressed:
            logger.info("Compressed long summary")
            return compressed
        return summary  # 壓縮失敗則保留原摘要
    except Exception as e:
        logger.error(f"Summary compression failed: {e}")
        return summary  # 壓縮失敗則保留原摘要


def generate_summary(messages: list[Message]) -> tuple[str, list[str]]:
    """用 Claude 生成對話摘要並提取長期事實（同步函數，會阻塞）

    Returns:
        (summary, new_facts) tuple
    """
    conversation_text = "\n".join(
        f"{m.role.capitalize()}: {m.content}" for m in messages
    )
    prompt = SUMMARY_PROMPT.format(conversation=conversation_text)

    try:
        result = subprocess.run(
            build_claude_command(prompt),
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            logger.error(f"Summary generation CLI error: {result.stderr.strip()}")
            return "", []
        output = result.stdout.strip()
        if output:
            return parse_summary_and_facts(output)
        return "", []
    except Exception as e:
        logger.error(f"Summary generation failed: {e}")
        return "", []


def maybe_compress_history(user_id: int):
    """檢查並在需要時壓縮歷史（同步函數，會阻塞）"""
    state = get_conversation_state(user_id)

    if len(state.messages) >= MAX_MESSAGES_BEFORE_COMPRESS:
        # 取出最舊的訊息來摘要
        to_summarize = state.messages[:MESSAGES_TO_SUMMARIZE]
        to_keep = state.messages[MESSAGES_TO_SUMMARIZE:]

        # 生成新摘要並提取事實
        old_summary = state.summary
        new_summary, new_facts = generate_summary(to_summarize)

        if new_summary:
            if old_summary:
                # 合併新舊摘要
                combined = f"{old_summary}\n\n---\n\n{new_summary}"
                # 如果合併後太長，重新壓縮整個摘要
                if len(combined) > MAX_SUMMARY_CHARS:
                    state.summary = compress_summary(combined)
                else:
                    state.summary = combined
            else:
                state.summary = new_summary

            state.messages = to_keep
            save_history()
            logger.info(f"Compressed history for user {user_id}")

        # 合併新事實到長期記憶
        if new_facts:
            merge_memory_facts(user_id, new_facts)
            save_memory()
            logger.info(f"Extracted {len(new_facts)} facts for user {user_id}")


def build_context(user_id: int) -> str:
    """組合摘要 + 最近對話為上下文"""
    state = get_conversation_state(user_id)
    parts = []

    # 加入時間戳
    parts.append(get_current_timestamp())

    # 加入安全護欄
    parts.append(SAFETY_GUARDRAILS)

    # 加入長期記憶
    memory = get_user_memory(user_id)
    if memory:
        memory_text = "\n".join(f"- {fact}" for fact in memory)
        if len(memory_text) > MAX_MEMORY_CHARS:
            memory_text = memory_text[:MAX_MEMORY_CHARS] + "\n..."
        parts.append(f"[Long-term memory about this user]\n{memory_text}")

    # 加入摘要
    if state.summary:
        parts.append(f"[Previous conversation summary]\n{state.summary}")

    # 加入最近對話
    if state.messages:
        context_parts: list[str] = []
        total_chars = 0

        # 從最新往回取，確保不超過字符限制
        for msg in reversed(state.messages):
            entry = f"{msg.role.capitalize()}: {msg.content}"
            if total_chars + len(entry) > MAX_CONTEXT_CHARS:
                break
            context_parts.insert(0, entry)
            total_chars += len(entry)

        if context_parts:
            recent = "\n\n".join(context_parts)
            parts.append(f"[Recent conversation]\n{recent}")

    return "\n\n---\n\n".join(parts)


async def ask_claude(
    user_id: int, message: str, max_retries: int = 3, timeout: int = 600
) -> str:
    """調用 Claude CLI，包含對話歷史和重試機制

    使用 ThreadPoolExecutor 執行 subprocess，避免阻塞 asyncio 事件循環，
    確保 Discord heartbeat 正常運作。

    Args:
        user_id: 用戶 ID
        message: 用戶訊息
        max_retries: 最大重試次數（預設 3 次）
        timeout: 單次執行超時秒數（預設 600 秒 = 10 分鐘）

    Returns:
        Claude 的回應或錯誤訊息
    """
    # 組合上下文
    context = build_context(user_id)

    if context:
        full_prompt = f"""先前的對話紀錄：
{context}

使用者目前的訊息：
{message}

請根據以上對話紀錄，回應使用者目前的訊息。"""
    else:
        full_prompt = message

    def run_claude_sync() -> subprocess.CompletedProcess:
        """同步執行 Claude CLI（在執行緒池中執行）"""
        return subprocess.run(
            build_claude_command(full_prompt),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    last_error: Optional[str] = None
    loop = asyncio.get_running_loop()

    for attempt in range(max_retries):
        try:
            # 使用執行緒池執行阻塞呼叫，不會阻塞 asyncio 事件循環
            result = await loop.run_in_executor(executor, run_claude_sync)

            # 檢查返回碼
            if result.returncode != 0:
                error_msg = result.stderr.strip() or "未知錯誤"
                logger.error(
                    f"Claude CLI error (code {result.returncode}): {error_msg}"
                )
                return f"Claude 執行失敗: {error_msg}"

            output = result.stdout.strip()

            if output:
                # 儲存對話歷史
                state = get_conversation_state(user_id)
                state.messages.append(Message("user", message, datetime.now()))
                state.messages.append(Message("assistant", output, datetime.now()))

                # 儲存到檔案
                save_history()

                # 檢查是否需要壓縮（在執行緒池中執行，避免阻塞事件循環）
                await loop.run_in_executor(executor, maybe_compress_history, user_id)

            return (
                output or f"Claude returned no output.\nstderr: {result.stderr.strip()}"
            )

        except subprocess.TimeoutExpired:
            last_error = "timeout"
            if attempt < max_retries - 1:
                # 指數退避 + 隨機抖動
                delay = (2**attempt) + random.uniform(0, 1)
                logger.warning(
                    f"Claude timeout, retrying in {delay:.1f}s (attempt {attempt + 1}/{max_retries})"
                )
                await asyncio.sleep(delay)
            else:
                logger.error(f"Claude timeout after {max_retries} attempts")
                return f"Claude 多次超時（{max_retries} 次），請稍後再試"

        except FileNotFoundError:
            return "claude CLI not found, please make sure Claude Code is installed."
        except Exception as e:
            return f"Error: {e}"

    # 這裡理論上不會執行到，但為了完整性
    return f"Claude 執行失敗: {last_error or '未知錯誤'}"


async def ask_claude_with_lock(
    user_id: int, prompt: str, message: discord.Message
) -> None:
    """取得 per-user lock 後呼叫 ask_claude()，排隊時顯示 ⏳ reaction。"""
    lock = user_locks.setdefault(user_id, asyncio.Lock())
    queued = lock.locked()
    if queued:
        try:
            await message.add_reaction("\u23f3")
        except discord.HTTPException:
            pass

    async with lock:
        if queued:
            try:
                await message.remove_reaction("\u23f3", client.user)
            except discord.HTTPException:
                pass
        async with message.channel.typing():
            response = await ask_claude(user_id, prompt)
        for chunk in chunk_message(response):
            await message.channel.send(chunk)


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


async def send_channel_message(channel_id: int, message: str):
    """發送訊息到指定頻道"""
    channel = client.get_channel(channel_id)
    if channel:
        for chunk in chunk_message(message):
            await channel.send(chunk)


async def invoke_claude_for_channel(
    channel_id: int, _user_id: int, prompt: str, timeout: int = 600
) -> str:
    """為頻道觸發 Claude 回應（不帶對話歷史）

    使用 ThreadPoolExecutor 執行 subprocess，避免阻塞 asyncio 事件循環。
    """
    channel = client.get_channel(channel_id)
    if not channel:
        return ""

    def run_claude_sync() -> subprocess.CompletedProcess:
        """同步執行 Claude CLI（在執行緒池中執行）"""
        return subprocess.run(
            build_claude_command(prompt),
            capture_output=True,
            text=True,
            timeout=timeout,
        )

    async with channel.typing():
        # 使用執行緒池執行阻塞呼叫，不會阻塞 asyncio 事件循環
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(executor, run_claude_sync)

            if result.returncode != 0:
                response = f"Claude 執行失敗: {result.stderr.strip()}"
            else:
                response = result.stdout.strip() or "Claude 無回應"
        except subprocess.TimeoutExpired:
            response = "Claude 執行超時"
        except Exception as e:
            response = f"錯誤: {e}"

    for chunk in chunk_message(response):
        await channel.send(chunk)
    return response


@client.event
async def on_ready():
    logger.info(f"Bot logged in as {client.user}")

    # 設定排程器回調並啟動
    cron_scheduler.set_callbacks(
        message_sender=send_channel_message, claude_invoker=invoke_claude_for_channel
    )
    await cron_scheduler.start()
    logger.info("Cron scheduler started")


@client.event
async def on_message(message: discord.Message):
    # ignore bot's own messages
    if message.author == client.user:
        return

    if not is_authorized(message.author.id):
        await message.channel.send("You are not authorized to use this bot.")
        logger.warning(f"Unauthorized access attempt from user_id={message.author.id}")
        return

    # 語音訊息：儲存 → 轉錄 → Claude 回應
    if message.flags.voice and message.attachments:
        attachment = message.attachments[0]
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = VOICE_DIR / f"voice_{message.author.name}_{ts}.ogg"
        await attachment.save(filename)
        duration = f"（{attachment.duration:.1f} 秒）" if attachment.duration else ""
        logger.info(f"Voice message saved: {filename}")

        # 檢查是否有 GROQ_API_KEY
        if not os.environ.get("GROQ_API_KEY"):
            await message.channel.send(
                f"語音已儲存：`{filename}`{duration}\n（未設定 GROQ_API_KEY，無法轉錄語音）"
            )
            return

        # 轉錄語音
        await message.channel.send(f"語音已儲存{duration}，轉錄中...")
        try:
            loop = asyncio.get_running_loop()
            result = await loop.run_in_executor(executor, transcribe, str(filename))
            transcribed_text = result.text.strip()
        except Exception as e:
            logger.error(f"Voice transcription failed: {e}")
            await message.channel.send(f"語音轉錄失敗：{e}\n語音檔已保留：`{filename}`")
            return

        if not transcribed_text:
            await message.channel.send("無法辨識語音內容，請重新錄製")
            return

        for chunk in chunk_message(f"**語音轉錄：** {transcribed_text}"):
            await message.channel.send(chunk)

        # 送給 Claude 回應（per-user lock 確保不會同時執行）
        await ask_claude_with_lock(message.author.id, transcribed_text, message)
        return

    user_message = message.content.strip()

    # 忽略空訊息
    if not user_message:
        return

    # 特殊命令：顯示說明
    if user_message.lower() in ["/help", "說明", "幫助"]:
        help_text = """**對話指令：**
• `/help` 或 `說明` - 顯示此說明
• `/new` 或 `新對話` - 保存記憶並開始新對話
• `/clear` 或 `清除歷史` - 清除對話歷史和摘要（保留長期記憶）
• `/context` 或 `上下文` - 查看上下文狀態
• `/summarize` - 手動生成摘要
• `/summary` - 查看當前摘要

**記憶指令：**
• `/memory` 或 `記憶` - 查看長期記憶
• `/forget` 或 `忘記` - 清除所有長期記憶
• `/forget <編號>` - 刪除特定一條記憶

**排程指令：**
• `/cron list` - 列出所有排程任務
• `/cron info <id>` - 查看任務詳情（含完整提示詞）
• `/cron remove <id>` - 刪除任務
• `/cron toggle <id>` - 啟用/停用任務
• `/cron test <id>` - 立即執行測試
• `/remind <時間> <訊息>` - 一次性提醒（如 `/remind 30m 開會`）
• `/every <間隔> <訊息>` - 定期訊息（如 `/every 1h 喝水`）
• `/daily <HH:MM> <提示>` - 每日觸發 Claude（如 `/daily 09:00 今日新聞`）

**使用方式：**
直接輸入訊息即可與 Claude 對話，Bot 會記住對話歷史。
切換話題時建議使用 `/new`，會自動保存重要資訊到長期記憶。"""
        await message.channel.send(help_text)
        return

    # 特殊命令：清除歷史和摘要（保留長期記憶）
    if user_message.lower() in ["/clear", "/reset", "清除歷史"]:
        conversation_states[message.author.id] = ConversationState()
        save_history()
        await message.channel.send("✓ 對話歷史和摘要已清除（長期記憶已保留）")
        return

    # 特殊命令：新對話（保留長期記憶，提取事實後清除當前對話）
    if user_message.lower() in ["/new", "新對話"]:
        state = get_conversation_state(message.author.id)

        # 至少 2 輪對話才值得提取
        if len(state.messages) >= 4:
            await message.channel.send("正在保存重要資訊到長期記憶...")
            async with message.channel.typing():
                loop = asyncio.get_running_loop()

                def extract_and_clear():
                    _, new_facts = generate_summary(state.messages)
                    if new_facts:
                        merge_memory_facts(message.author.id, new_facts)
                        save_memory()
                        return len(new_facts)
                    return 0

                fact_count = await loop.run_in_executor(executor, extract_and_clear)

            conversation_states[message.author.id] = ConversationState()
            save_history()

            if fact_count > 0:
                await message.channel.send(f"✓ 已提取 {fact_count} 條記憶並開始新對話")
            else:
                await message.channel.send("✓ 新對話已開始（長期記憶已保留）")
        else:
            conversation_states[message.author.id] = ConversationState()
            save_history()
            await message.channel.send("✓ 新對話已開始（長期記憶已保留）")
        return

    # 特殊命令：查看長期記憶
    if user_message.lower() in ["/memory", "記憶"]:
        memory = get_user_memory(message.author.id)
        if memory:
            memory_lines = [f"{i + 1}. {fact}" for i, fact in enumerate(memory)]
            memory_text = "\n".join(memory_lines)
            if len(memory_text) > 1800:
                memory_text = memory_text[:1800] + "\n..."
            await message.channel.send(
                f"**長期記憶 ({len(memory)} 條)：**\n\n{memory_text}"
            )
        else:
            await message.channel.send("目前沒有長期記憶")
        return

    # 特殊命令：清除特定記憶（帶參數，如 /forget 3）
    if user_message.lower().startswith("/forget "):
        arg = user_message[8:].strip()
        memory = get_user_memory(message.author.id)

        if not memory:
            await message.channel.send("目前沒有長期記憶")
            return

        try:
            idx = int(arg) - 1  # 用戶看到的是 1-based
            if 0 <= idx < len(memory):
                removed = memory.pop(idx)
                save_memory()
                await message.channel.send(f"✓ 已刪除記憶：{removed}")
            else:
                await message.channel.send(f"無效的編號，範圍是 1-{len(memory)}")
        except ValueError:
            if arg.lower() in ["all", "全部"]:
                count = len(memory)
                del user_memories[message.author.id]
                save_memory()
                await message.channel.send(f"✓ 已清除 {count} 條長期記憶")
            else:
                await message.channel.send(
                    "用法：`/forget` 清除全部，`/forget 3` 刪除第 3 條"
                )
        return

    # 特殊命令：清除全部長期記憶
    if user_message.lower() in ["/forget", "忘記"]:
        if message.author.id in user_memories and user_memories[message.author.id]:
            count = len(user_memories[message.author.id])
            del user_memories[message.author.id]
            save_memory()
            await message.channel.send(f"✓ 已清除 {count} 條長期記憶")
        else:
            await message.channel.send("目前沒有長期記憶需要清除")
        return

    # 特殊命令：查看上下文狀態
    if user_message.lower() in ["/context", "上下文"]:
        state = get_conversation_state(message.author.id)
        history_len = len(state.messages)
        has_summary = "有" if state.summary else "無"
        memory_count = len(get_user_memory(message.author.id))
        await message.channel.send(
            f"目前上下文：{history_len // 2} 輪對話，摘要：{has_summary}，長期記憶：{memory_count} 條"
        )
        return

    # 特殊命令：手動觸發摘要
    if user_message.lower() == "/summarize":
        state = get_conversation_state(message.author.id)
        if not state.messages:
            await message.channel.send("目前沒有對話需要摘要")
            return

        await message.channel.send("正在生成摘要...")
        async with message.channel.typing():
            loop = asyncio.get_running_loop()
            new_summary, new_facts = await loop.run_in_executor(
                executor, generate_summary, state.messages
            )

        if new_summary:
            if state.summary:
                state.summary = f"{state.summary}\n\n---\n\n{new_summary}"
            else:
                state.summary = new_summary
            state.messages = []  # 清空已摘要的對話

            # 合併新事實到長期記憶
            if new_facts:
                merge_memory_facts(message.author.id, new_facts)
                save_memory()

            save_history()
            summary_preview = (
                new_summary[:500] + "..." if len(new_summary) > 500 else new_summary
            )
            fact_msg = f"\n\n提取了 {len(new_facts)} 條長期記憶" if new_facts else ""
            await message.channel.send(f"✓ 摘要已生成：\n\n{summary_preview}{fact_msg}")
        else:
            await message.channel.send("摘要生成失敗")
        return

    # 特殊命令：查看目前摘要
    if user_message.lower() == "/summary":
        state = get_conversation_state(message.author.id)
        if state.summary:
            summary_preview = (
                state.summary[:1800] + "..."
                if len(state.summary) > 1800
                else state.summary
            )
            await message.channel.send(f"📝 目前摘要：\n\n{summary_preview}")
        else:
            await message.channel.send("目前沒有摘要")
        return

    # Cron 排程指令
    if user_message.lower().startswith("/cron"):
        args = user_message.split()[1:]
        response = await handle_cron_command(
            "cron", args, message.channel.id, message.author.id
        )
        for chunk in chunk_message(response):
            await message.channel.send(chunk)
        return

    if user_message.lower().startswith("/remind"):
        args = user_message.split()[1:]
        response = await handle_remind_command(
            args, message.channel.id, message.author.id
        )
        for chunk in chunk_message(response):
            await message.channel.send(chunk)
        return

    if user_message.lower().startswith("/every"):
        args = user_message.split()[1:]
        response = await handle_every_command(
            args, message.channel.id, message.author.id
        )
        for chunk in chunk_message(response):
            await message.channel.send(chunk)
        return

    if user_message.lower().startswith("/daily"):
        args = user_message.split()[1:]
        response = await handle_daily_command(
            args, message.channel.id, message.author.id
        )
        for chunk in chunk_message(response):
            await message.channel.send(chunk)
        return

    logger.info(f"User {message.author.id}: {user_message[:50]}...")

    # Per-user lock：同一使用者的請求排隊執行，避免 race condition
    await ask_claude_with_lock(message.author.id, user_message, message)


def main():
    if not DISCORD_BOT_TOKEN:
        raise ValueError("Please set DISCORD_BOT_TOKEN environment variable.")
    # 啟動時載入歷史和記憶
    load_history()
    load_memory()
    client.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
