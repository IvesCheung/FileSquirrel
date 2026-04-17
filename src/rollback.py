"""
回滚模块。

根据操作日志逆序还原文件操作，支持按批次回退。
操作日志在 database 模块中记录，本模块负责执行还原逻辑。
"""

import shutil
from pathlib import Path

from src.config import AppConfig
from src.database import Database
from src.organizer import Organizer


class RollbackManager:
    """整理操作回滚管理器。"""

    def __init__(self, config: AppConfig, db: Database):
        self.config = config
        self.db = db
        self.target = Path(config.target_directory).resolve()

    def rollback_latest(self) -> bool:
        """
        回滚最近一次整理操作。

        Returns:
            True 表示回滚成功，False 表示没有可回滚的批次
        """
        batch_id = self.db.get_latest_batch_id()
        if batch_id is None:
            print("[Rollback] 没有找到可回滚的批次。")
            return False
        return self.rollback_batch(batch_id)

    def rollback_batch(self, batch_id: int) -> bool:
        """
        回滚指定批次的所有操作。

        操作按记录 ID 倒序执行（先撤销最新的操作），确保依赖关系正确。

        Args:
            batch_id: 要回滚的批次 ID

        Returns:
            True 表示回滚成功
        """
        operations = self.db.get_batch_operations(batch_id)
        if not operations:
            print(f"[Rollback] 批次 {batch_id} 没有操作记录。")
            return False

        print(f"[Rollback] 开始回滚批次 {batch_id}，共 {len(operations)} 个操作。")

        for op in operations:
            try:
                self._reverse_operation(op)
            except Exception as e:
                print(f"[Rollback] 操作回滚失败 (op_id={op['id']}): {e}")
                # 继续尝试回滚其余操作

        # 标记批次为已回滚
        self.db.rollback_batch(batch_id)

        # 清理空目录
        Organizer.cleanup_empty_dirs(self.target)

        print(f"[Rollback] 批次 {batch_id} 回滚完成。")
        return True

    def _reverse_operation(self, op: dict):
        """
        逆执行单个操作。

        根据操作类型将文件从 dst_path 移回 src_path。

        Args:
            op: 操作记录字典，包含 op_type, src_path, dst_path
        """
        op_type = op["op_type"]
        src_relative = op["src_path"]   # 原始路径
        dst_relative = op["dst_path"]   # 操作后路径

        src_path = (self.target / src_relative).resolve()
        dst_path = (self.target / dst_relative).resolve()

        if op_type in ("move", "rename"):
            # 把文件从当前位置移回原始位置
            if not dst_path.exists():
                raise FileNotFoundError(f"文件不存在，无法回滚: {dst_path}")

            # 确保原始目录存在
            src_path.parent.mkdir(parents=True, exist_ok=True)

            # 如果原始位置已有文件（被后续操作放回的），添加后缀避免覆盖
            if src_path.exists():
                src_path = self._resolve_conflict(src_path)

            shutil.move(str(dst_path), str(src_path))

        elif op_type == "create_dir":
            # 创建目录的逆操作：删除空目录
            if dst_path.exists() and dst_path.is_dir() and not any(dst_path.iterdir()):
                dst_path.rmdir()

    @staticmethod
    def _resolve_conflict(path: Path) -> Path:
        """路径冲突时添加后缀。"""
        stem = path.stem
        suffix = path.suffix
        parent = path.parent
        counter = 1
        while True:
            new_path = parent / f"{stem}_rollback_{counter}{suffix}"
            if not new_path.exists():
                return new_path
            counter += 1
