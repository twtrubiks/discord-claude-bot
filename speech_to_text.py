"""
語音轉文字模組（使用 Groq Whisper API）

使用方式:
  1. 設定環境變數 GROQ_API_KEY（或在下方填入）
  2. 執行:
     - 轉錄既有音訊: python speech_to_text.py transcribe <音訊檔路徑>
     - 錄音再轉錄:   python speech_to_text.py record [秒數]

支援格式: mp3, mp4, mpeg, mpga, m4a, wav, webm, ogg, flac
免費方案檔案大小上限: 25 MB
"""

import os
import subprocess
import sys

from groq import Groq

# ============================
# 在這裡填入你的 Groq API Key
# 取得方式: https://console.groq.com/keys
# ============================
GROQ_API_KEY = ""
# ============================

DEFAULT_RECORD_FILE = "recording.ogg"


def get_audio_duration(audio_path: str) -> str:
    """使用 ffprobe 取得音訊時間長度"""
    try:
        result = subprocess.run(
            [
                "ffprobe",
                "-v",
                "quiet",
                "-show_entries",
                "format=duration",
                "-of",
                "default=noprint_wrappers=1:nokey=1",
                audio_path,
            ],
            capture_output=True,
            text=True,
        )
        seconds = float(result.stdout.strip())
        minutes = int(seconds // 60)
        secs = seconds % 60
        if minutes > 0:
            return f"{minutes} 分 {secs:.1f} 秒"
        return f"{secs:.1f} 秒"
    except Exception:
        return "未知"


def record_audio(output_path: str, duration: int = None):
    """使用 ffmpeg 錄製 ogg 音訊（64kbps mono，語音辨識最佳）"""
    cmd = ["ffmpeg", "-y", "-f", "pulse", "-i", "default", "-ac", "1", "-b:a", "64k"]

    if duration:
        cmd.extend(["-t", str(duration)])

    cmd.append(output_path)

    print(f"錄音中... {'按 Ctrl+C 停止' if not duration else f'將錄製 {duration} 秒'}")
    print(f"輸出檔案: {output_path}")
    print("格式: OGG 64kbps mono\n")

    try:
        subprocess.run(cmd, check=True)
    except KeyboardInterrupt:
        print("\n錄音已停止")
    except FileNotFoundError:
        print("錯誤: 找不到 ffmpeg，請先安裝: sudo apt install ffmpeg")
        sys.exit(1)


def transcribe(audio_path: str, language: str = "zh"):
    """將音訊檔轉為文字"""
    api_key = os.environ.get("GROQ_API_KEY") or GROQ_API_KEY
    if not api_key:
        raise ValueError("請設定 GROQ_API_KEY 環境變數或在程式碼中填入 API Key")
    client = Groq(api_key=api_key)

    with open(audio_path, "rb") as f:
        result = client.audio.transcriptions.create(
            file=(os.path.basename(audio_path), f.read()),
            model="whisper-large-v3",
            language=language,
            prompt="以下是繁體中文的語音內容",  # 引導模型輸出繁體中文
            response_format="verbose_json",  # 可取得時間戳等詳細資訊
        )

    return result


def print_result(result):
    """輸出轉錄結果"""
    print("=== 轉錄結果 ===")
    print(result.text)

    if hasattr(result, "segments") and result.segments:
        print("\n=== 分段明細 ===")
        for seg in result.segments:
            start = seg.get("start", seg.start if hasattr(seg, "start") else 0)
            end = seg.get("end", seg.end if hasattr(seg, "end") else 0)
            text = seg.get("text", seg.text if hasattr(seg, "text") else "")
            print(f"[{start:.1f}s - {end:.1f}s] {text}")


def main():
    if len(sys.argv) < 2:
        print("用法:")
        print("  python speech_to_text.py transcribe <音訊檔路徑>")
        print("  python speech_to_text.py record [秒數]")
        print()
        print("範例:")
        print("  python speech_to_text.py transcribe recording.ogg")
        print("  python speech_to_text.py record        # 按 Ctrl+C 停止")
        print("  python speech_to_text.py record 10     # 錄 10 秒")
        sys.exit(1)

    command = sys.argv[1]

    if command == "record":
        duration = int(sys.argv[2]) if len(sys.argv) > 2 else None
        output_path = DEFAULT_RECORD_FILE

        # 錄音
        record_audio(output_path, duration)

        if not os.path.exists(output_path):
            print("錄音失敗")
            sys.exit(1)

        file_size_mb = os.path.getsize(output_path) / (1024 * 1024)
        duration_str = get_audio_duration(output_path)
        print(f"\n錄音完成: {output_path} ({file_size_mb:.1f} MB, {duration_str})")

        # 轉錄
        print("轉錄中...\n")
        result = transcribe(output_path)
        print_result(result)

    elif command == "transcribe":
        if len(sys.argv) < 3:
            print("請提供音訊檔路徑")
            sys.exit(1)

        audio_path = sys.argv[2]

        if not os.path.exists(audio_path):
            print(f"檔案不存在: {audio_path}")
            sys.exit(1)

        file_size_mb = os.path.getsize(audio_path) / (1024 * 1024)
        duration_str = get_audio_duration(audio_path)
        print(f"檔案: {audio_path} ({file_size_mb:.1f} MB, {duration_str})")

        if file_size_mb > 25:
            print("警告: 免費方案檔案上限為 25 MB")

        print("轉錄中...\n")
        result = transcribe(audio_path)
        print_result(result)

    else:
        print(f"未知指令: {command}")
        print("可用指令: transcribe, record")
        sys.exit(1)


if __name__ == "__main__":
    main()
