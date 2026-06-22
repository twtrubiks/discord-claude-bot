"""Claude CLI command helpers."""

import os

CLAUDE_PERMISSION_MODE = "bypassPermissions"
CLAUDE_DISALLOWED_TOOLS = "AskUserQuestion,ExitPlanMode,EnterPlanMode"
# claude --effort 支援 low/medium/high/xhigh/max；未設定 CLAUDE_EFFORT 時的預設值
CLAUDE_DEFAULT_EFFORT = "xhigh"


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

    # 推理強度由 CLAUDE_EFFORT 控制，未設定時預設 xhigh
    # 統一轉小寫，避免 .env 寫成 xHigh / MAX 之類大小寫導致 CLI 不認
    # 注意：Haiku 不支援 effort，帶了也無效（CLI 會默默忽略），因此 Haiku 模型不帶 --effort
    if "haiku" not in model.lower():
        effort = (
            os.environ.get("CLAUDE_EFFORT", "").strip().lower() or CLAUDE_DEFAULT_EFFORT
        )
        cmd += ["--effort", effort]
    return cmd


def build_claude_stream_command(prompt: str) -> list[str]:
    """Build a Claude CLI command with token-level streaming (NDJSON output)."""
    return build_claude_command(prompt) + [
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
    ]
