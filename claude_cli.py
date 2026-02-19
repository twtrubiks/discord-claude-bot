"""Claude CLI command helpers."""

CLAUDE_PERMISSION_MODE = "bypassPermissions"


def build_claude_command(prompt: str) -> list[str]:
    """Build a consistent Claude CLI command for this project."""
    return [
        "claude",
        "-p",
        prompt,
        "--permission-mode",
        CLAUDE_PERMISSION_MODE,
    ]
