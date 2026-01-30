import asyncio
import json
import logging
import os
import random
import re
import subprocess
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

import discord
from dotenv import load_dotenv

from cron_scheduler import cron_scheduler
from cron_commands import (
    handle_cron_command,
    handle_remind_command,
    handle_every_command,
    handle_daily_command,
)

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
ALLOWED_USER_IDS = os.environ.get("ALLOWED_USER_IDS", "")

# Discord 訊息分塊設定
DISCORD_CHAR_LIMIT = 2000
FENCE_PATTERN = re.compile(r"^( {0,3})(`{3,}|~{3,})(.*)$", re.MULTILINE)


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


@dataclass
class Message:
    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime


@dataclass
class ConversationState:
    summary: str = ""  # AI 生成的摘要
    messages: list = field(default_factory=list)  # 最近的對話


# 每個用戶的對話狀態
conversation_states: dict[int, ConversationState] = {}


def get_conversation_state(user_id: int) -> ConversationState:
    """取得用戶的對話狀態，如果不存在則創建"""
    if user_id not in conversation_states:
        conversation_states[user_id] = ConversationState()
    return conversation_states[user_id]


# AI 摘要設定
MAX_MESSAGES_BEFORE_COMPRESS = 16  # 超過 8 輪對話時壓縮
MESSAGES_TO_SUMMARIZE = 10  # 壓縮最舊的 5 輪
MAX_SUMMARY_CHARS = 2000  # 摘要最大字符數

SUMMARY_PROMPT = """請將以下對話摘要成重點，保留：
- 用戶的偏好和設定
- 重要的決策和結論
- 待辦事項和承諾
- 關鍵資訊（名字、日期、數字等）

對話內容：
{conversation}

請用繁體中文輸出簡潔的摘要（約 200-300 字）："""


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
    """壓縮過長的摘要"""
    prompt = COMPRESS_SUMMARY_PROMPT.format(summary=summary)

    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
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


def generate_summary(messages: list[Message]) -> str:
    """用 Claude 生成對話摘要"""
    conversation_text = "\n".join(
        f"{m.role.capitalize()}: {m.content}" for m in messages
    )
    prompt = SUMMARY_PROMPT.format(conversation=conversation_text)

    try:
        result = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True,
            text=True,
            timeout=60,
        )
        # 檢查返回碼
        if result.returncode != 0:
            logger.error(f"Summary generation CLI error: {result.stderr.strip()}")
            return ""
        return result.stdout.strip() or ""
    except Exception as e:
        logger.error(f"Summary generation failed: {e}")
        return ""


def maybe_compress_history(user_id: int):
    """檢查並在需要時壓縮歷史"""
    state = get_conversation_state(user_id)

    if len(state.messages) >= MAX_MESSAGES_BEFORE_COMPRESS:
        # 取出最舊的訊息來摘要
        to_summarize = state.messages[:MESSAGES_TO_SUMMARIZE]
        to_keep = state.messages[MESSAGES_TO_SUMMARIZE:]

        # 生成新摘要（合併舊摘要）
        old_summary = state.summary
        new_summary = generate_summary(to_summarize)

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


def build_context(user_id: int) -> str:
    """組合摘要 + 最近對話為上下文"""
    state = get_conversation_state(user_id)
    parts = []

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


async def ask_claude(user_id: int, message: str, max_retries: int = 3) -> str:
    """調用 Claude CLI，包含對話歷史和重試機制

    Args:
        user_id: 用戶 ID
        message: 用戶訊息
        max_retries: 最大重試次數（預設 3 次）

    Returns:
        Claude 的回應或錯誤訊息
    """
    # 組合上下文
    context = build_context(user_id)

    if context:
        full_prompt = f"""Previous conversation:
{context}

Current message from user:
{message}

Please respond to the current message, taking into account the conversation history above."""
    else:
        full_prompt = message

    last_error: Optional[str] = None

    for attempt in range(max_retries):
        try:
            result = subprocess.run(
                ["claude", "-p", full_prompt],
                capture_output=True,
                text=True,
                timeout=120,
            )
            # 檢查返回碼
            if result.returncode != 0:
                error_msg = result.stderr.strip() or "未知錯誤"
                logger.error(f"Claude CLI error (code {result.returncode}): {error_msg}")
                return f"Claude 執行失敗: {error_msg}"

            output = result.stdout.strip()

            if output:
                # 儲存對話歷史
                state = get_conversation_state(user_id)
                state.messages.append(Message("user", message, datetime.now()))
                state.messages.append(Message("assistant", output, datetime.now()))

                # 儲存到檔案
                save_history()

                # 檢查是否需要壓縮
                maybe_compress_history(user_id)

            return output or f"Claude returned no output.\nstderr: {result.stderr.strip()}"

        except subprocess.TimeoutExpired:
            last_error = "timeout"
            if attempt < max_retries - 1:
                # 指數退避 + 隨機抖動
                delay = (2 ** attempt) + random.uniform(0, 1)
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


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


async def send_channel_message(channel_id: int, message: str):
    """發送訊息到指定頻道"""
    channel = client.get_channel(channel_id)
    if channel:
        for chunk in chunk_message(message):
            await channel.send(chunk)


async def invoke_claude_for_channel(channel_id: int, user_id: int, prompt: str) -> str:
    """為頻道觸發 Claude 回應（不帶對話歷史）"""
    channel = client.get_channel(channel_id)
    if not channel:
        return ""

    async with channel.typing():
        # 直接呼叫 claude -p，不帶對話歷史
        try:
            result = subprocess.run(
                ["claude", "-p", prompt],
                capture_output=True,
                text=True,
                timeout=120,
            )
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
        message_sender=send_channel_message,
        claude_invoker=invoke_claude_for_channel
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

    user_message = message.content.strip()

    # 忽略空訊息
    if not user_message:
        return

    # 特殊命令：顯示說明
    if user_message.lower() in ["/help", "說明", "幫助"]:
        help_text = """**可用指令：**
• `/help` 或 `說明` - 顯示此說明
• `/clear` 或 `清除歷史` - 清除對話歷史和摘要
• `/history` 或 `歷史` - 查看對話狀態
• `/summarize` - 手動生成摘要
• `/summary` - 查看當前摘要

**排程指令：**
• `/cron list` - 列出所有排程任務
• `/cron info <id>` - 查看任務詳情
• `/cron remove <id>` - 刪除任務
• `/cron toggle <id>` - 啟用/停用任務
• `/cron test <id>` - 立即執行測試
• `/remind <時間> <訊息>` - 一次性提醒（如 `/remind 30m 開會`）
• `/every <間隔> <訊息>` - 定期訊息（如 `/every 1h 喝水`）
• `/daily <HH:MM> <提示>` - 每日觸發 Claude（如 `/daily 09:00 今日新聞`）

**使用方式：**
直接輸入訊息即可與 Claude 對話，Bot 會記住對話歷史。"""
        await message.channel.send(help_text)
        return

    # 特殊命令：清除歷史和摘要
    if user_message.lower() in ["/clear", "/reset", "清除歷史"]:
        conversation_states[message.author.id] = ConversationState()
        save_history()
        await message.channel.send("✓ 對話歷史和摘要已清除")
        return

    # 特殊命令：查看歷史長度
    if user_message.lower() in ["/history", "歷史"]:
        state = get_conversation_state(message.author.id)
        history_len = len(state.messages)
        has_summary = "有" if state.summary else "無"
        await message.channel.send(
            f"目前對話歷史：{history_len // 2} 輪對話，摘要：{has_summary}"
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
            new_summary = generate_summary(state.messages)

        if new_summary:
            if state.summary:
                state.summary = f"{state.summary}\n\n---\n\n{new_summary}"
            else:
                state.summary = new_summary
            state.messages = []  # 清空已摘要的對話
            save_history()
            summary_preview = (
                new_summary[:500] + "..." if len(new_summary) > 500 else new_summary
            )
            await message.channel.send(f"✓ 摘要已生成：\n\n{summary_preview}")
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
        response = await handle_cron_command("cron", args, message.channel.id, message.author.id)
        await message.channel.send(response)
        return

    if user_message.lower().startswith("/remind"):
        args = user_message.split()[1:]
        response = await handle_remind_command(args, message.channel.id, message.author.id)
        await message.channel.send(response)
        return

    if user_message.lower().startswith("/every"):
        args = user_message.split()[1:]
        response = await handle_every_command(args, message.channel.id, message.author.id)
        await message.channel.send(response)
        return

    if user_message.lower().startswith("/daily"):
        args = user_message.split()[1:]
        response = await handle_daily_command(args, message.channel.id, message.author.id)
        await message.channel.send(response)
        return

    logger.info(f"User {message.author.id}: {user_message[:50]}...")

    # show typing indicator
    async with message.channel.typing():
        response = await ask_claude(message.author.id, user_message)

    # Discord message limit is 2000 characters
    # 使用智能分塊，保持代碼塊完整
    chunks = chunk_message(response)
    for chunk in chunks:
        await message.channel.send(chunk)


def main():
    if not DISCORD_BOT_TOKEN:
        raise ValueError("Please set DISCORD_BOT_TOKEN environment variable.")
    # 啟動時載入歷史
    load_history()
    client.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
