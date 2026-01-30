import json
import os
import subprocess
import logging
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

import discord
from dotenv import load_dotenv

load_dotenv()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

DISCORD_BOT_TOKEN = os.environ.get("DISCORD_BOT_TOKEN", "")
ALLOWED_USER_IDS = os.environ.get("ALLOWED_USER_IDS", "")

# 對話歷史設定
MAX_HISTORY_LENGTH = 10  # 保留最近 10 輪對話
MAX_CONTEXT_CHARS = 8000  # 上下文最大字符數
HISTORY_FILE = Path("conversation_history.json")


@dataclass
class Message:
    role: str  # "user" or "assistant"
    content: str
    timestamp: datetime


# 每個用戶的對話歷史
conversation_history: dict[int, list[Message]] = defaultdict(list)


def save_history():
    """儲存歷史到檔案"""
    data = {
        str(uid): [
            {"role": m.role, "content": m.content, "timestamp": m.timestamp.isoformat()}
            for m in messages
        ]
        for uid, messages in conversation_history.items()
    }
    HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))


def load_history():
    """從檔案載入歷史"""
    if not HISTORY_FILE.exists():
        return
    try:
        data = json.loads(HISTORY_FILE.read_text())
        for uid, messages in data.items():
            conversation_history[int(uid)] = [
                Message(m["role"], m["content"], datetime.fromisoformat(m["timestamp"]))
                for m in messages
            ]
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


def build_context(user_id: int) -> str:
    """組合對話歷史為上下文字串"""
    history = conversation_history[user_id]
    if not history:
        return ""

    context_parts = []
    total_chars = 0

    # 從最新往回取，確保不超過字符限制
    for msg in reversed(history):
        entry = f"{msg.role.capitalize()}: {msg.content}"
        if total_chars + len(entry) > MAX_CONTEXT_CHARS:
            break
        context_parts.insert(0, entry)
        total_chars += len(entry)

    return "\n\n".join(context_parts)


def ask_claude(user_id: int, message: str) -> str:
    """調用 Claude CLI，包含對話歷史"""
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

    try:
        result = subprocess.run(
            ["claude", "-p", full_prompt],
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout.strip()

        if output:
            # 儲存對話歷史
            history = conversation_history[user_id]
            history.append(Message("user", message, datetime.now()))
            history.append(Message("assistant", output, datetime.now()))

            # 修剪歷史長度
            if len(history) > MAX_HISTORY_LENGTH * 2:
                conversation_history[user_id] = history[-(MAX_HISTORY_LENGTH * 2):]

            # 儲存到檔案
            save_history()

        return output or f"Claude returned no output.\nstderr: {result.stderr.strip()}"

    except subprocess.TimeoutExpired:
        return "Claude Code timeout (over 120 seconds)."
    except FileNotFoundError:
        return "claude CLI not found, please make sure Claude Code is installed."
    except Exception as e:
        return f"Error: {e}"


intents = discord.Intents.default()
intents.message_content = True
client = discord.Client(intents=intents)


@client.event
async def on_ready():
    logger.info(f"Bot logged in as {client.user}")


@client.event
async def on_message(message: discord.Message):
    # ignore bot's own messages
    if message.author == client.user:
        return

    if not is_authorized(message.author.id):
        await message.channel.send("You are not authorized to use this bot.")
        logger.warning(f"Unauthorized access attempt from user_id={message.author.id}")
        return

    user_message = message.content

    # 特殊命令：清除歷史
    if user_message.lower() in ["/clear", "/reset", "清除歷史"]:
        conversation_history[message.author.id] = []
        save_history()
        await message.channel.send("✓ 對話歷史已清除")
        return

    # 特殊命令：查看歷史長度
    if user_message.lower() in ["/history", "歷史"]:
        history_len = len(conversation_history[message.author.id])
        await message.channel.send(f"目前對話歷史：{history_len // 2} 輪對話")
        return

    logger.info(f"User {message.author.id}: {user_message[:50]}...")

    # show typing indicator
    async with message.channel.typing():
        response = ask_claude(message.author.id, user_message)

    # Discord message limit is 2000 characters
    if len(response) > 2000:
        for i in range(0, len(response), 2000):
            await message.channel.send(response[i : i + 2000])
    else:
        await message.channel.send(response)


def main():
    if not DISCORD_BOT_TOKEN:
        raise ValueError("Please set DISCORD_BOT_TOKEN environment variable.")
    # 啟動時載入歷史
    load_history()
    client.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
