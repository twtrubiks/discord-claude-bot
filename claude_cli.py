"""Claude CLI command helpers."""

import os

CLAUDE_PERMISSION_MODE = "bypassPermissions"
CLAUDE_DISALLOWED_TOOLS = "AskUserQuestion,ExitPlanMode,EnterPlanMode"


def build_claude_command(prompt: str, light: bool = False) -> list[str]:
    """Build a consistent Claude CLI command for this project.

    light=True 用於輕量任務（摘要壓縮、標題生成），改用 CLAUDE_LIGHT_MODEL。
    """
    cmd = [
        "claude",
        "-p",
        prompt,
        "--permission-mode",
        CLAUDE_PERMISSION_MODE,
        "--disallowedTools",
        CLAUDE_DISALLOWED_TOOLS,
    ]
    # 模型由 .env 控制（在呼叫時讀取，確保 load_dotenv 已生效）
    # 輕量任務優先用 CLAUDE_LIGHT_MODEL，未設定時退回 CLAUDE_MODEL
    # 兩者皆未設定時不帶 --model，沿用 CLI 預設模型
    model = ""
    if light:
        model = os.environ.get("CLAUDE_LIGHT_MODEL", "").strip()
    if not model:
        model = os.environ.get("CLAUDE_MODEL", "").strip()
    if model:
        cmd += ["--model", model]
    return cmd


def build_claude_stream_command(prompt: str) -> list[str]:
    """Build a Claude CLI command with token-level streaming (NDJSON output)."""
    return build_claude_command(prompt) + [
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
    ]
