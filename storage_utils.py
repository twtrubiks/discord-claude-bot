"""共用的安全寫檔工具。"""

import json
import os
import tempfile
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, text: str) -> None:
    """原子寫入：先寫同目錄 temp 檔，再 os.replace 置換。

    os.replace 在 POSIX 上是原子操作 —— 讀者永遠只會看到
    「舊的完整檔」或「新的完整檔」，絕不會讀到寫一半的內容。
    寫一半當機時，原檔也完好無損（temp 檔被丟棄）。
    """
    path = Path(path)
    # 同目錄建 temp，確保 os.replace 是同檔系統內的原子 rename
    fd, tmp = tempfile.mkstemp(
        dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())  # 確保落盤再 replace
        os.replace(tmp, path)  # ← 原子置換
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_json(path: Path, data: Any) -> None:
    """以原子方式把 data 序列化成 JSON 寫入 path。"""
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2))
