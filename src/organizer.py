"""
文件整理执行模块。

根据 LLM 的决策，执行实际的文件移动、重命名和目录创建操作。
所有操作都通过数据库记录，支持回滚。
"""

import shutil
from pathlib import Path

from src.analyzer import OrganizeDecision
from src.config import AppConfig
from src.database import Database
from src.scanner import FileInfo


class Organizer:
    """文件整理执行器。"""

    def __init__(self, config: AppConfig, db: Database):
        self.config = config
        self.db = db
        self.target = Path(config.target_directory).resolve()

    def execute_batch(self, decisions: list[OrganizeDecision],
                      files_map: dict[str, FileInfo]) -> int:
        """
        批量执行整理决策。

        Args:
            decisions: LLM 给出的整理决策列表
            files_map: file_hash → FileInfo 的映射

        Returns:
            成功处理的文件数
        """
        batch_id = self.db.create_batch()
        success_count = 0

        for decision in decisions:
            # 跳过低置信度决策（保持原位）
            if decision.confidence < 0.3:
                continue

            # 跳过目标路径与原路径相同的（无需操作）
            if decision.target_path == decision.original_path:
                continue

            file_info = files_map.get(decision.file_hash)
            if not file_info:
                continue

            try:
                self._execute_single(batch_id, decision, file_info)
                success_count += 1
            except Exception as e:
                # 单个文件执行失败不中断整个批次
                print(f"[Organizer] 处理失败 {file_info.path.name}: {e}")

        # 标记批次完成
        self.db.complete_batch(batch_id, success_count)
        return success_count

    def _execute_single(self, batch_id: int, decision: OrganizeDecision,
                        file_info: FileInfo):
        """
        执行单个文件的整理操作。

        Args:
            batch_id: 当前批次 ID
            decision: LLM 决策
            file_info: 文件信息
        """
        src_path = file_info.path
        dst_relative = decision.target_path
        dst_path = (self.target / dst_relative).resolve()

        # 安全检查：确保目标路径仍在 target_directory 内
        if not str(dst_path).startswith(str(self.target)):
            raise ValueError(f"目标路径超出范围: {dst_path}")

        # 检查操作权限
        src_relative = str(src_path.relative_to(self.target))
        is_moving = src_path.parent != dst_path.parent
        is_renaming = src_path.name != dst_path.name

        # 创建目标目录（如果需要且被允许）
        if dst_path.parent != src_path.parent:
            if not self.config.allow_move:
                return  # 不允许移动，跳过
            if not self.config.allow_create_dirs:
                # 不允许创建新目录时，只移动到已有目录
                if not dst_path.parent.exists():
                    return
            dst_path.parent.mkdir(parents=True, exist_ok=True)

        # 处理文件名冲突：如果目标已存在，添加后缀
        if dst_path.exists() and dst_path != src_path:
            dst_path = self._resolve_conflict(dst_path)

        # 执行移动/重命名
        shutil.move(str(src_path), str(dst_path))

        # 记录操作到数据库
        op_type = "move" if is_moving else "rename"
        if is_moving and is_renaming:
            op_type = "move"  # 移动 + 重命名记为 move

        self.db.log_operation(
            batch_id=batch_id,
            file_hash=file_info.file_hash,
            op_type=op_type,
            src_path=src_relative,
            dst_path=str(dst_path.relative_to(self.target)),
        )

        # 更新文件记录
        self.db.record_file(
            file_hash=file_info.file_hash,
            original_path=src_relative,
            current_path=str(dst_path.relative_to(self.target)),
            status="moved" if is_moving else "renamed",
        )

    def _resolve_conflict(self, dst_path: Path) -> Path:
        """
        解决文件名冲突，在文件名后添加数字后缀。

        例如: document.pdf → document_1.pdf → document_2.pdf
        """
        if not dst_path.exists():
            return dst_path

        stem = dst_path.stem
        suffix = dst_path.suffix
        parent = dst_path.parent
        counter = 1

        while True:
            new_name = f"{stem}_{counter}{suffix}"
            new_path = parent / new_name
            if not new_path.exists():
                return new_path
            counter += 1

    @staticmethod
    def cleanup_empty_dirs(target: Path):
        """
        清理目标目录下的空文件夹（由整理操作产生的）。

        从最深层开始向上清理，只删除空目录。
        """
        for dirpath in sorted(target.rglob("*"), reverse=True):
            if dirpath.is_dir() and not any(dirpath.iterdir()):
                try:
                    dirpath.rmdir()
                except OSError:
                    pass  # 目录非空或无权限，跳过
