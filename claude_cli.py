"""Claude CLI command helpers."""

CLAUDE_PERMISSION_MODE = "bypassPermissions"
CLAUDE_DISALLOWED_TOOLS = "AskUserQuestion,ExitPlanMode,EnterPlanMode"


def build_claude_command(prompt: str) -> list[str]:
    """Build a consistent Claude CLI command for this project."""
    return [
        "claude",
        "-p",
        prompt,
        "--permission-mode",
        CLAUDE_PERMISSION_MODE,
        "--disallowedTools",
        CLAUDE_DISALLOWED_TOOLS,
    ]


def build_claude_stream_command(prompt: str) -> list[str]:
    """Build a Claude CLI command with token-level streaming (NDJSON output)."""
    return build_claude_command(prompt) + [
        "--output-format",
        "stream-json",
        "--verbose",
        "--include-partial-messages",
    ]
