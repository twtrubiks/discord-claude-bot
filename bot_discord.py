import os
import subprocess
import logging

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



def get_allowed_users() -> set[int]:
    if not ALLOWED_USER_IDS:
        return set()
    return {int(uid.strip()) for uid in ALLOWED_USER_IDS.split(",") if uid.strip()}


def is_authorized(user_id: int) -> bool:
    allowed = get_allowed_users()
    if not allowed:
        return True
    return user_id in allowed


def ask_claude(message: str) -> str:
    try:
        result = subprocess.run(
            ["claude", "-p", message],
            capture_output=True,
            text=True,
            timeout=120,
        )
        output = result.stdout.strip()
        if not output:
            return f"Claude returned no output.\nstderr: {result.stderr.strip()}"
        return output
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
    logger.info(f"User {message.author.id}: {user_message[:50]}...")

    # show typing indicator
    async with message.channel.typing():
        response = ask_claude(user_message)

    # Discord message limit is 2000 characters
    if len(response) > 2000:
        for i in range(0, len(response), 2000):
            await message.channel.send(response[i : i + 2000])
    else:
        await message.channel.send(response)


def main():
    if not DISCORD_BOT_TOKEN:
        raise ValueError("Please set DISCORD_BOT_TOKEN environment variable.")
    client.run(DISCORD_BOT_TOKEN)


if __name__ == "__main__":
    main()
