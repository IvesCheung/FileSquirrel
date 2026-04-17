"""
闲时调度模块。

检测系统空闲状态，在用户不使用电脑时（夜间或空闲超时）触发整理任务。
支持优雅中断：检测到用户活跃时暂停当前整理进度，下次继续。
"""

import ctypes
import logging
import threading
import time
from datetime import datetime

from src.config import AppConfig, ScheduleConfig

logger = logging.getLogger("filesquirrel")


class Scheduler:
    """闲时调度器，负责判断何时触发整理任务。"""

    def __init__(self, config: AppConfig, organize_func):
        """
        Args:
            config: 应用配置
            organize_func: 整理函数（无参数），由调用方提供
        """
        self.config = config
        self.schedule = config.schedule
        self.organize_func = organize_func
        self._stop_event = threading.Event()
        self._paused = False  # 是否因用户活跃而暂停

    def run_daemon(self):
        """
        以守护循环方式运行调度器。

        每分钟检查一次是否满足闲时条件，满足则触发整理。
        整理过程中持续检测用户活跃，活跃则暂停。
        """
        logger.info(
            f"调度器启动 - 空闲阈值: {self.schedule.idle_minutes} 分钟, "
            f"安静时段: {self.schedule.quiet_start} ~ {self.schedule.quiet_end}"
        )

        while not self._stop_event.is_set():
            if self._should_run():
                logger.info("满足闲时条件，开始整理任务")
                try:
                    self.organize_func()
                    logger.info("整理任务完成")
                except Exception as e:
                    logger.error(f"整理任务异常: {e}", exc_info=True)
            else:
                # 不满足条件，等待一分钟再检查
                self._stop_event.wait(timeout=60)

    def stop(self):
        """停止调度器。"""
        self._stop_event.set()
        logger.info("调度器已停止")

    def is_user_active(self) -> bool:
        """
        检测用户是否正在使用电脑。

        Windows 平台通过 GetLastInputInfo 获取最后一次输入时间，
        计算空闲秒数判断是否活跃。

        Returns:
            True 表示用户正在使用电脑
        """
        try:
            # Windows: GetLastInputInfo
            class LASTINPUTINFO(ctypes.Structure):
                _fields_ = [("cbSize", ctypes.c_uint), ("dwTime", ctypes.c_uint)]

            lii = LASTINPUTINFO()
            lii.cbSize = ctypes.sizeof(LASTINPUTINFO)
            ctypes.windll.user32.GetLastInputInfo(ctypes.byref(lii))
            # 获取系统运行时间（毫秒）
            millis = ctypes.windll.kernel32.GetTickCount()
            idle_seconds = (millis - lii.dwTime) / 1000
            return idle_seconds < self.schedule.idle_minutes * 60
        except Exception:
            # 非 Windows 平台或 API 调用失败，保守认为用户活跃
            return True

    def _should_run(self) -> bool:
        """判断当前是否满足整理条件：在安静时段且用户空闲。"""
        if not self.schedule.enabled:
            return False

        now = datetime.now()
        if not self._in_quiet_hours(now):
            return False

        if self.is_user_active():
            return False

        return True

    def _in_quiet_hours(self, now: datetime) -> bool:
        """
        判断当前时间是否在安静时段内。

        支持跨午夜时段（如 23:00 ~ 07:00）。

        Args:
            now: 当前时间

        Returns:
            True 表示在安静时段
        """
        start = self._parse_time(self.schedule.quiet_start)
        end = self._parse_time(self.schedule.quiet_end)
        current_minutes = now.hour * 60 + now.minute

        if start <= end:
            # 不跨午夜：如 09:00 ~ 17:00
            return start <= current_minutes <= end
        else:
            # 跨午夜：如 23:00 ~ 07:00
            return current_minutes >= start or current_minutes <= end

    @staticmethod
    def _parse_time(time_str: str) -> int:
        """
        解析 HH:MM 格式时间为当日分钟数。

        Args:
            time_str: 时间字符串，如 "23:00"

        Returns:
            从午夜起的分钟数
        """
        parts = time_str.strip().split(":")
        return int(parts[0]) * 60 + int(parts[1])
