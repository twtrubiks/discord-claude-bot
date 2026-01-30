"""Cron 指令處理模組

處理 Discord 的排程相關指令：
- /cron list|info|remove|toggle|test
- /remind <時間> <訊息>
- /every <間隔> <訊息>
- /daily <HH:MM> <提示>
"""

import re
from datetime import datetime, timedelta
from typing import Optional

from cron_scheduler import (
    cron_scheduler,
    CronJob,
    ScheduleConfig,
    ScheduleKind,
    generate_job_id,
    MIN_INTERVAL_SECONDS,
)


def parse_duration(duration_str: str) -> Optional[int]:
    """解析時間長度字串

    支援格式：
    - 30s, 30sec, 30秒
    - 5m, 5min, 5分, 5分鐘
    - 2h, 2hr, 2hour, 2小時
    - 1d, 1day, 1天

    Returns:
        秒數，如果解析失敗則回傳 None
    """
    duration_str = duration_str.strip().lower()

    patterns = [
        # 秒
        (r"^(\d+)\s*(s|sec|秒)$", 1),
        # 分鐘
        (r"^(\d+)\s*(m|min|分鐘?)$", 60),
        # 小時
        (r"^(\d+)\s*(h|hr|hour|小時)$", 3600),
        # 天
        (r"^(\d+)\s*(d|day|天)$", 86400),
    ]

    for pattern, multiplier in patterns:
        match = re.match(pattern, duration_str)
        if match:
            value = int(match.group(1))
            return value * multiplier

    return None


def parse_time_of_day(time_str: str) -> Optional[tuple[int, int]]:
    """解析時間字串

    支援格式：
    - HH:MM (如 09:00, 14:30)
    - H:MM (如 9:00)

    Returns:
        (hour, minute) 元組，如果解析失敗則回傳 None
    """
    match = re.match(r"^(\d{1,2}):(\d{2})$", time_str.strip())
    if not match:
        return None

    hour = int(match.group(1))
    minute = int(match.group(2))

    if 0 <= hour <= 23 and 0 <= minute <= 59:
        return (hour, minute)

    return None


def format_schedule(schedule: ScheduleConfig) -> str:
    """格式化排程配置為可讀字串"""
    if schedule.kind == ScheduleKind.AT:
        run_time = datetime.fromtimestamp(schedule.at_timestamp / 1000)
        return f"一次性: {run_time.strftime('%Y-%m-%d %H:%M:%S')}"

    elif schedule.kind == ScheduleKind.EVERY:
        seconds = schedule.every_seconds
        if seconds >= 86400:
            return f"每 {seconds // 86400} 天"
        elif seconds >= 3600:
            return f"每 {seconds // 3600} 小時"
        elif seconds >= 60:
            return f"每 {seconds // 60} 分鐘"
        else:
            return f"每 {seconds} 秒"

    elif schedule.kind == ScheduleKind.CRON:
        return f"Cron: {schedule.cron_expr} ({schedule.timezone})"

    return "未知"


def format_job_info(job: CronJob) -> str:
    """格式化任務詳細資訊"""
    status = "啟用" if job.enabled else "停用"
    schedule_str = format_schedule(job.schedule)
    invoke_str = "觸發 Claude" if job.invoke_claude else "純訊息"

    return f"""**任務 ID**: `{job.id}`
**狀態**: {status}
**類型**: {invoke_str}
**排程**: {schedule_str}
**描述**: {job.description or '無'}
**訊息**: {job.message[:100]}{'...' if len(job.message) > 100 else ''}
**建立時間**: {job.created_at.strftime('%Y-%m-%d %H:%M:%S')}"""


def format_job_list_item(job: CronJob) -> str:
    """格式化任務列表項目"""
    status = "✓" if job.enabled else "✗"
    schedule_str = format_schedule(job.schedule)
    msg_preview = job.message[:30] + "..." if len(job.message) > 30 else job.message

    return f"`{job.id}` [{status}] {schedule_str} - {msg_preview}"


async def handle_cron_command(
    command: str, args: list[str], channel_id: int, user_id: int
) -> str:
    """處理 /cron 指令

    子指令：
    - list: 列出所有任務
    - info <id>: 查看任務詳情
    - remove <id>: 刪除任務
    - toggle <id>: 切換啟用狀態
    - test <id>: 立即執行測試
    """
    if not args:
        return """**Cron 排程指令：**
• `/cron list` - 列出所有排程任務
• `/cron info <id>` - 查看任務詳情
• `/cron remove <id>` - 刪除任務
• `/cron toggle <id>` - 啟用/停用任務
• `/cron test <id>` - 立即執行測試"""

    subcommand = args[0].lower()

    if subcommand == "list":
        jobs = cron_scheduler.list_jobs()
        if not jobs:
            return "目前沒有任何排程任務"

        lines = ["**排程任務列表：**"]
        for job in jobs:
            lines.append(format_job_list_item(job))
        return "\n".join(lines)

    elif subcommand == "info":
        if len(args) < 2:
            return "請指定任務 ID：`/cron info <id>`"

        job_id = args[1]
        job = cron_scheduler.get_job(job_id)
        if not job:
            return f"找不到任務：`{job_id}`"

        return format_job_info(job)

    elif subcommand == "remove":
        if len(args) < 2:
            return "請指定任務 ID：`/cron remove <id>`"

        job_id = args[1]
        success = await cron_scheduler.remove_job(job_id)
        if success:
            return f"✓ 已刪除任務：`{job_id}`"
        else:
            return f"找不到任務：`{job_id}`"

    elif subcommand == "toggle":
        if len(args) < 2:
            return "請指定任務 ID：`/cron toggle <id>`"

        job_id = args[1]
        new_state = await cron_scheduler.toggle_job(job_id)
        if new_state is None:
            return f"找不到任務：`{job_id}`"

        status = "啟用" if new_state else "停用"
        return f"✓ 任務 `{job_id}` 已{status}"

    elif subcommand == "test":
        if len(args) < 2:
            return "請指定任務 ID：`/cron test <id>`"

        job_id = args[1]
        success = await cron_scheduler.test_job(job_id)
        if success:
            return f"✓ 已執行任務：`{job_id}`"
        else:
            return f"找不到任務：`{job_id}`"

    else:
        return f"未知的子指令：`{subcommand}`。輸入 `/cron` 查看可用指令。"


async def handle_remind_command(
    args: list[str], channel_id: int, user_id: int
) -> str:
    """處理 /remind 指令

    格式：/remind <時間> <訊息>
    範例：/remind 30m 開會
    """
    if len(args) < 2:
        return "格式：`/remind <時間> <訊息>`\n範例：`/remind 30m 開會`"

    duration_str = args[0]
    message = " ".join(args[1:])

    seconds = parse_duration(duration_str)
    if not seconds:
        return f"無法解析時間格式：`{duration_str}`\n支援格式：30s, 5m, 2h, 1d"

    # 計算執行時間
    run_time = datetime.now() + timedelta(seconds=seconds)
    timestamp_ms = int(run_time.timestamp() * 1000)

    # 建立任務
    job = CronJob(
        id=generate_job_id(),
        channel_id=channel_id,
        user_id=user_id,
        message=message,
        schedule=ScheduleConfig(
            kind=ScheduleKind.AT,
            at_timestamp=timestamp_ms,
        ),
        invoke_claude=True,
        description=f"提醒: {message[:30]}",
    )

    await cron_scheduler.add_job(job)

    return f"✓ 已設定提醒：{run_time.strftime('%Y-%m-%d %H:%M:%S')}\n任務 ID：`{job.id}`"


async def handle_every_command(
    args: list[str], channel_id: int, user_id: int
) -> str:
    """處理 /every 指令

    格式：/every <間隔> <訊息>
    範例：/every 1h 喝水！
    """
    if len(args) < 2:
        return "格式：`/every <間隔> <訊息>`\n範例：`/every 1h 喝水！`"

    interval_str = args[0]
    message = " ".join(args[1:])

    seconds = parse_duration(interval_str)
    if not seconds:
        return f"無法解析間隔格式：`{interval_str}`\n支援格式：30s, 5m, 2h, 1d"

    if seconds < MIN_INTERVAL_SECONDS:
        return f"間隔時間不能少於 {MIN_INTERVAL_SECONDS} 秒"

    # 建立任務
    job = CronJob(
        id=generate_job_id(),
        channel_id=channel_id,
        user_id=user_id,
        message=message,
        schedule=ScheduleConfig(
            kind=ScheduleKind.EVERY,
            every_seconds=seconds,
        ),
        invoke_claude=True,
        description=f"定期: {message[:30]}",
    )

    await cron_scheduler.add_job(job)

    return f"✓ 已設定定期訊息：{format_schedule(job.schedule)}\n任務 ID：`{job.id}`"


async def handle_daily_command(
    args: list[str], channel_id: int, user_id: int
) -> str:
    """處理 /daily 指令

    格式：/daily <HH:MM> <提示>
    範例：/daily 09:00 今日新聞
    """
    if len(args) < 2:
        return "格式：`/daily <HH:MM> <提示>`\n範例：`/daily 09:00 今日新聞`"

    time_str = args[0]
    prompt = " ".join(args[1:])

    time_parts = parse_time_of_day(time_str)
    if not time_parts:
        return f"無法解析時間格式：`{time_str}`\n支援格式：HH:MM（如 09:00, 14:30）"

    hour, minute = time_parts

    # 建立 Cron 表達式
    cron_expr = f"{minute} {hour} * * *"

    # 建立任務
    job = CronJob(
        id=generate_job_id(),
        channel_id=channel_id,
        user_id=user_id,
        message=prompt,
        schedule=ScheduleConfig(
            kind=ScheduleKind.CRON,
            cron_expr=cron_expr,
            timezone="Asia/Taipei",
        ),
        invoke_claude=True,  # daily 指令預設觸發 Claude
        description=f"每日 {time_str}: {prompt[:20]}",
    )

    await cron_scheduler.add_job(job)

    return f"✓ 已設定每日任務：每天 {time_str} 觸發 Claude\n提示詞：{prompt[:50]}{'...' if len(prompt) > 50 else ''}\n任務 ID：`{job.id}`"
