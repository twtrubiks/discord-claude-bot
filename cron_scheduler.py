"""Cron 排程核心模組

支援三種排程類型：
- at: 一次性任務（指定時間戳）
- every: 定期任務（指定間隔秒數）
- cron: Cron 表達式任務
"""

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional, Callable, Awaitable
import uuid

from apscheduler import AsyncScheduler
from apscheduler.triggers.date import DateTrigger
from apscheduler.triggers.interval import IntervalTrigger
from apscheduler.triggers.cron import CronTrigger

logger = logging.getLogger(__name__)

JOBS_FILE = Path("cron_jobs.json")
MIN_INTERVAL_SECONDS = 60  # 最小間隔時間（秒）


class ScheduleKind(str, Enum):
    AT = "at"  # 一次性任務
    EVERY = "every"  # 定期任務
    CRON = "cron"  # Cron 表達式


@dataclass
class ScheduleConfig:
    """排程配置"""

    kind: ScheduleKind
    at_timestamp: Optional[int] = None  # 毫秒時間戳（kind=at）
    every_seconds: Optional[int] = None  # 間隔秒數（kind=every）
    cron_expr: Optional[str] = None  # Cron 表達式（kind=cron）
    timezone: str = "Asia/Taipei"


@dataclass
class CronJob:
    """排程任務"""

    id: str
    channel_id: int
    user_id: int
    message: str
    schedule: ScheduleConfig
    invoke_claude: bool = False  # 是否觸發 Claude 回應
    enabled: bool = True
    created_at: datetime = field(default_factory=datetime.now)
    description: str = ""  # 任務描述

    def to_dict(self) -> dict:
        """轉換為可序列化的字典"""
        return {
            "id": self.id,
            "channel_id": self.channel_id,
            "user_id": self.user_id,
            "message": self.message,
            "schedule": {
                "kind": self.schedule.kind.value,
                "at_timestamp": self.schedule.at_timestamp,
                "every_seconds": self.schedule.every_seconds,
                "cron_expr": self.schedule.cron_expr,
                "timezone": self.schedule.timezone,
            },
            "invoke_claude": self.invoke_claude,
            "enabled": self.enabled,
            "created_at": self.created_at.isoformat(),
            "description": self.description,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "CronJob":
        """從字典建立任務"""
        schedule_data = data["schedule"]
        schedule = ScheduleConfig(
            kind=ScheduleKind(schedule_data["kind"]),
            at_timestamp=schedule_data.get("at_timestamp"),
            every_seconds=schedule_data.get("every_seconds"),
            cron_expr=schedule_data.get("cron_expr"),
            timezone=schedule_data.get("timezone", "Asia/Taipei"),
        )
        return cls(
            id=data["id"],
            channel_id=data["channel_id"],
            user_id=data["user_id"],
            message=data["message"],
            schedule=schedule,
            invoke_claude=data.get("invoke_claude", False),
            enabled=data.get("enabled", True),
            created_at=datetime.fromisoformat(data["created_at"]),
            description=data.get("description", ""),
        )


class CronScheduler:
    """排程器核心類別"""

    def __init__(self):
        self._scheduler: Optional[AsyncScheduler] = None
        self._jobs: dict[str, CronJob] = {}
        self._message_sender: Optional[Callable[[int, str], Awaitable[None]]] = None
        self._claude_invoker: Optional[
            Callable[[int, int, str], Awaitable[str]]
        ] = None

    def set_callbacks(
        self,
        message_sender: Callable[[int, str], Awaitable[None]],
        claude_invoker: Callable[[int, int, str], Awaitable[str]],
    ):
        """設定回調函數

        Args:
            message_sender: 發送訊息的函數 (channel_id, message) -> None
            claude_invoker: 觸發 Claude 的函數 (channel_id, user_id, prompt) -> response
        """
        self._message_sender = message_sender
        self._claude_invoker = claude_invoker

    async def start(self):
        """啟動排程器"""
        self._load_jobs()
        self._scheduler = AsyncScheduler()

        # APScheduler 4.x 需要先進入 context manager 再啟動
        await self._scheduler.__aenter__()
        await self._scheduler.start_in_background()

        # 註冊所有啟用的任務
        for job in self._jobs.values():
            if job.enabled:
                await self._register_job(job)

        logger.info(f"Cron scheduler started with {len(self._jobs)} jobs")

    async def stop(self):
        """停止排程器"""
        if self._scheduler:
            await self._scheduler.__aexit__(None, None, None)
            self._scheduler = None
        logger.info("Cron scheduler stopped")

    def _load_jobs(self):
        """從 JSON 載入任務"""
        if not JOBS_FILE.exists():
            return

        try:
            data = json.loads(JOBS_FILE.read_text())
            for job_data in data:
                job = CronJob.from_dict(job_data)
                self._jobs[job.id] = job
            logger.info(f"Loaded {len(self._jobs)} cron jobs from file")
        except Exception as e:
            logger.error(f"Failed to load cron jobs: {e}")

    def _save_jobs(self):
        """儲存任務到 JSON"""
        try:
            data = [job.to_dict() for job in self._jobs.values()]
            JOBS_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2))
        except Exception as e:
            logger.error(f"Failed to save cron jobs: {e}")

    def _create_trigger(self, schedule: ScheduleConfig):
        """根據配置建立觸發器"""
        if schedule.kind == ScheduleKind.AT:
            # 一次性任務
            run_time = datetime.fromtimestamp(schedule.at_timestamp / 1000)
            return DateTrigger(run_time=run_time)

        elif schedule.kind == ScheduleKind.EVERY:
            # 定期任務
            return IntervalTrigger(seconds=schedule.every_seconds)

        elif schedule.kind == ScheduleKind.CRON:
            # Cron 表達式
            parts = schedule.cron_expr.split()
            if len(parts) == 5:
                minute, hour, day, month, day_of_week = parts
                return CronTrigger(
                    minute=minute,
                    hour=hour,
                    day=day,
                    month=month,
                    day_of_week=day_of_week,
                    timezone=schedule.timezone,
                )
            else:
                raise ValueError(f"Invalid cron expression: {schedule.cron_expr}")

        raise ValueError(f"Unknown schedule kind: {schedule.kind}")

    async def _register_job(self, job: CronJob):
        """註冊任務到排程器"""
        if not self._scheduler:
            return

        try:
            trigger = self._create_trigger(job.schedule)
            await self._scheduler.add_schedule(
                self._execute_job,
                trigger,
                id=job.id,
                args=[job.id],
            )
            logger.info(f"Registered job {job.id}: {job.description or job.message[:30]}")
        except Exception as e:
            logger.error(f"Failed to register job {job.id}: {e}")

    async def _unregister_job(self, job_id: str):
        """從排程器取消註冊任務"""
        if not self._scheduler:
            return

        try:
            await self._scheduler.remove_schedule(job_id)
            logger.info(f"Unregistered job {job_id}")
        except Exception as e:
            logger.debug(f"Job {job_id} not found in scheduler: {e}")

    async def _execute_job(self, job_id: str):
        """執行任務"""
        logger.info(f"[CRON] _execute_job called with job_id={job_id}")

        job = self._jobs.get(job_id)
        if not job:
            logger.warning(f"[CRON] Job {job_id} not found in _jobs")
            return

        if not job.enabled:
            logger.info(f"[CRON] Job {job_id} is disabled, skipping")
            return

        logger.info(f"[CRON] Executing job {job_id}: {job.message[:50]}...")
        logger.info(f"[CRON] channel_id={job.channel_id}, invoke_claude={job.invoke_claude}")
        logger.info(f"[CRON] _message_sender set: {self._message_sender is not None}")

        try:
            if job.invoke_claude:
                # 觸發 Claude 回應
                if self._claude_invoker:
                    logger.info(f"[CRON] Invoking Claude...")
                    await self._claude_invoker(
                        job.channel_id, job.user_id, job.message
                    )
                else:
                    logger.warning(f"[CRON] _claude_invoker is None!")
            else:
                # 發送純訊息
                if self._message_sender:
                    logger.info(f"[CRON] Sending message to channel {job.channel_id}...")
                    await self._message_sender(job.channel_id, job.message)
                    logger.info(f"[CRON] Message sent successfully")
                else:
                    logger.warning(f"[CRON] _message_sender is None!")

            # 一次性任務執行後自動刪除
            if job.schedule.kind == ScheduleKind.AT:
                del self._jobs[job_id]
                self._save_jobs()
                logger.info(f"[CRON] One-time job {job_id} completed and removed")

        except Exception as e:
            logger.error(f"[CRON] Failed to execute job {job_id}: {e}", exc_info=True)

    async def add_job(self, job: CronJob) -> str:
        """新增任務

        Returns:
            任務 ID
        """
        self._jobs[job.id] = job
        self._save_jobs()

        if job.enabled:
            await self._register_job(job)

        return job.id

    async def remove_job(self, job_id: str) -> bool:
        """刪除任務

        Returns:
            是否成功刪除
        """
        if job_id not in self._jobs:
            return False

        await self._unregister_job(job_id)
        del self._jobs[job_id]
        self._save_jobs()
        return True

    async def toggle_job(self, job_id: str) -> Optional[bool]:
        """切換任務啟用狀態

        Returns:
            新的啟用狀態，如果任務不存在則回傳 None
        """
        job = self._jobs.get(job_id)
        if not job:
            return None

        job.enabled = not job.enabled

        if job.enabled:
            await self._register_job(job)
        else:
            await self._unregister_job(job_id)

        self._save_jobs()
        return job.enabled

    def get_job(self, job_id: str) -> Optional[CronJob]:
        """取得任務"""
        return self._jobs.get(job_id)

    def list_jobs(self, user_id: Optional[int] = None) -> list[CronJob]:
        """列出任務

        Args:
            user_id: 如果指定，只列出該用戶的任務

        Returns:
            任務列表
        """
        jobs = list(self._jobs.values())
        if user_id is not None:
            jobs = [j for j in jobs if j.user_id == user_id]
        return sorted(jobs, key=lambda j: j.created_at)

    async def test_job(self, job_id: str) -> bool:
        """立即執行任務（測試用）

        Returns:
            是否成功執行
        """
        if job_id not in self._jobs:
            return False

        await self._execute_job(job_id)
        return True


def generate_job_id() -> str:
    """生成唯一的任務 ID"""
    return uuid.uuid4().hex[:8]


# 全域排程器實例
cron_scheduler = CronScheduler()
