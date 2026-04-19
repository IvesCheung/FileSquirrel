"""
配置管理模块。

负责加载 config.yaml 并提供校验后的配置对象。
支持默认值填充和类型检查。
"""

import os
from dataclasses import dataclass, field
from pathlib import Path

import yaml


@dataclass
class ModelConfig:
    """Ollama 模型配置。"""
    name: str = "qwen3.5:4b"
    base_url: str = "http://localhost:11434"
    timeout: int = 120


@dataclass
class ScheduleConfig:
    """闲时调度配置。"""
    enabled: bool = True
    idle_minutes: int = 30          # 电脑空闲多少分钟后开始整理
    quiet_start: str = "23:00"      # 允许运行的开始时间
    quiet_end: str = "07:00"        # 允许运行的结束时间


@dataclass
class AppConfig:
    """应用全局配置。"""
    target_directory: str = ""
    model: ModelConfig = field(default_factory=ModelConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    allow_rename: bool = True
    allow_move: bool = True
    allow_create_dirs: bool = True
    allow_delete: bool = False           # 是否允许删除文件（默认关闭）
    use_agent: bool = False              # 使用 agent 模式（默认线性模式）
    max_iterations: int = 200            # agent 模式最大推理轮次
    whitelist_dirs: list = field(default_factory=list)
    ignore_patterns: list = field(default_factory=lambda: ["*.tmp", "*.part"])
    organize_requirements: str = "请按照文件类型和用途进行分类整理。"


def load_config(config_path: str = "config.yaml") -> AppConfig:
    """
    从 YAML 文件加载配置，填充默认值并校验。

    Args:
        config_path: 配置文件路径，默认为工作目录下的 config.yaml

    Returns:
        校验后的 AppConfig 对象

    Raises:
        FileNotFoundError: 配置文件不存在
        ValueError: 配置内容校验失败
    """
    path = Path(config_path)
    if not path.exists():
        raise FileNotFoundError(
            f"配置文件不存在: {config_path}\n"
            f"请复制 config.yaml.example 为 config.yaml 并修改配置。"
        )

    with open(path, "r", encoding="utf-8") as f:
        raw = yaml.safe_load(f) or {}

    # 解析 model 子配置
    model_raw = raw.get("model", {})
    model_cfg = ModelConfig(
        name=model_raw.get("name", ModelConfig.name),
        base_url=model_raw.get("base_url", ModelConfig.base_url),
        timeout=model_raw.get("timeout", ModelConfig.timeout),
    )

    # 解析 schedule 子配置
    sched_raw = raw.get("schedule", {})
    quiet_hours = sched_raw.get("quiet_hours", {})
    sched_cfg = ScheduleConfig(
        enabled=sched_raw.get("enabled", ScheduleConfig.enabled),
        idle_minutes=sched_raw.get("idle_minutes", ScheduleConfig.idle_minutes),
        quiet_start=quiet_hours.get("start", ScheduleConfig.quiet_start),
        quiet_end=quiet_hours.get("end", ScheduleConfig.quiet_end),
    )

    # 组装完整配置（用临时实例获取 field 默认值）
    _defaults = AppConfig()
    config = AppConfig(
        target_directory=raw.get("target_directory", ""),
        model=model_cfg,
        schedule=sched_cfg,
        allow_rename=raw.get("allow_rename", _defaults.allow_rename),
        allow_move=raw.get("allow_move", _defaults.allow_move),
        allow_create_dirs=raw.get("allow_create_dirs", _defaults.allow_create_dirs),
        allow_delete=raw.get("allow_delete", _defaults.allow_delete),
        use_agent=raw.get("use_agent", _defaults.use_agent),
        max_iterations=raw.get("max_iterations", _defaults.max_iterations),
        whitelist_dirs=raw.get("whitelist_dirs", _defaults.whitelist_dirs),
        ignore_patterns=raw.get("ignore_patterns", _defaults.ignore_patterns),
        organize_requirements=raw.get("organize_requirements", _defaults.organize_requirements),
    )

    _validate(config)
    return config


def _validate(config: AppConfig) -> None:
    """
    校验配置的合法性。

    Args:
        config: 待校验的配置对象

    Raises:
        ValueError: 配置不合法
    """
    if not config.target_directory:
        raise ValueError("target_directory 不能为空")

    if not Path(config.target_directory).exists():
        raise ValueError(f"target_directory 不存在: {config.target_directory}")

    if config.model.timeout <= 0:
        raise ValueError("model.timeout 必须大于 0")

    if config.schedule.idle_minutes <= 0:
        raise ValueError("schedule.idle_minutes 必须大于 0")
