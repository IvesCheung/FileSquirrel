"""
文件扫描模块。

负责扫描目标目录，过滤忽略文件，计算 hash，
并与数据库对比找出增量（未处理的）文件。
"""

import hashlib
from dataclasses import dataclass
from fnmatch import fnmatch
from pathlib import Path

from src.config import AppConfig
from src.database import Database


@dataclass
class FileInfo:
    """扫描到的文件信息。"""
    path: Path             # 文件绝对路径
    relative_path: str     # 相对于 target_directory 的路径
    file_hash: str         # SHA256 hash
    size: int              # 文件大小（字节）
    suffix: str            # 文件扩展名（含 .，如 .pdf）


def compute_file_hash(file_path: Path, chunk_size: int = 8192) -> str:
    """
    计算文件的 SHA256 hash。

    Args:
        file_path: 文件路径
        chunk_size: 分块大小，默认 8KB

    Returns:
        文件的 SHA256 十六进制字符串
    """
    hasher = hashlib.sha256()
    with open(file_path, "rb") as f:
        while chunk := f.read(chunk_size):
            hasher.update(chunk)
    return hasher.hexdigest()


class Scanner:
    """文件扫描器，负责发现增量文件。"""

    def __init__(self, config: AppConfig, db: Database):
        self.config = config
        self.db = db
        self.target = Path(config.target_directory).resolve()

    def scan_incremental(self) -> list[FileInfo]:
        """
        扫描目标目录，返回所有未处理过的文件列表。

        Returns:
            增量文件信息列表
        """
        new_files: list[FileInfo] = []

        # 确定要扫描的子目录
        scan_dirs = self._get_scan_dirs()

        for scan_dir in scan_dirs:
            for file_path in scan_dir.rglob("*"):
                # 只处理文件，跳过目录
                if not file_path.is_file():
                    continue

                # 过滤忽略模式
                if self._should_ignore(file_path):
                    continue

                # 计算 hash
                try:
                    file_hash = compute_file_hash(file_path)
                except (PermissionError, OSError):
                    continue

                # 增量检测：已处理过的跳过
                if self.db.is_file_processed(file_hash):
                    continue

                new_files.append(FileInfo(
                    path=file_path,
                    relative_path=str(file_path.relative_to(self.target)),
                    file_hash=file_hash,
                    size=file_path.stat().st_size,
                    suffix=file_path.suffix.lower(),
                ))

        return new_files

    def get_current_structure(self) -> dict:
        """
        获取当前目录结构概览，供 LLM 作为上下文参考。

        Returns:
            目录树结构，形如 {"子目录名": {"子子目录名": {}, ...}, ...}
        """
        structure: dict = {}

        scan_dirs = self._get_scan_dirs()
        for scan_dir in scan_dirs:
            rel_base = scan_dir.relative_to(self.target) if scan_dir != self.target else Path(".")
            for item in sorted(scan_dir.rglob("*")):
                if not item.is_dir():
                    continue
                if self._should_ignore(item):
                    continue

                rel = item.relative_to(self.target)
                # 逐级构建嵌套字典
                node = structure
                for part in rel.parts:
                    if part not in node:
                        node[part] = {}
                    node = node[part]

        return structure

    def _get_scan_dirs(self) -> list[Path]:
        """获取实际需要扫描的目录列表。"""
        if self.config.whitelist_dirs:
            # 白名单模式：只扫描指定子目录
            dirs = []
            for d in self.config.whitelist_dirs:
                full = (self.target / d).resolve()
                if full.exists():
                    dirs.append(full)
            return dirs
        else:
            # 无白名单则扫描整个目标目录
            return [self.target]

    def _should_ignore(self, path: Path) -> bool:
        """判断文件是否匹配忽略模式。"""
        name = path.name
        for pattern in self.config.ignore_patterns:
            if fnmatch(name, pattern):
                return True
        return False
