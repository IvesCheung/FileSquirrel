"""
日志模块。

提供统一的日志配置，同时输出到控制台和日志文件。
日志文件按日期轮转，保留在 logs/ 目录下。
"""

import logging
import sys
from pathlib import Path

# 默认日志目录
DEFAULT_LOG_DIR = Path("logs")


def setup_logger(name: str = "filesquirrel", log_dir: str | Path | None = None) -> logging.Logger:
    """
    创建并配置 logger。

    同时输出到：
    - 控制台（INFO 级别，简洁格式）
    - 日志文件（DEBUG 级别，详细格式）

    Args:
        name: logger 名称
        log_dir: 日志文件目录，默认为 logs/

    Returns:
        配置好的 Logger 对象
    """
    log_path = Path(log_dir) if log_dir else DEFAULT_LOG_DIR
    log_path.mkdir(parents=True, exist_ok=True)

    logger = logging.getLogger(name)

    # 避免重复添加 handler（多次调用 setup_logger 时）
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    # ── 控制台 handler：INFO 级别，简洁格式 ──────────────
    console_handler = logging.StreamHandler(sys.stdout)
    console_handler.setLevel(logging.INFO)
    console_fmt = logging.Formatter(
        "[%(asctime)s] %(levelname)s - %(message)s",
        datefmt="%H:%M:%S",
    )
    console_handler.setFormatter(console_fmt)
    logger.addHandler(console_handler)

    # ── 文件 handler：DEBUG 级别，详细格式 ──────────────
    from logging.handlers import TimedRotatingFileHandler

    file_handler = TimedRotatingFileHandler(
        filename=log_path / "filesquirrel.log",
        when="midnight",       # 每天轮转
        backupCount=30,        # 保留 30 天
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    file_handler.setFormatter(file_fmt)
    logger.addHandler(file_handler)

    return logger
