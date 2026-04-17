"""
数据库模块。

使用 SQLite 管理文件追踪记录和用户手动修正记录。
提供三张核心表：
  - files: 已处理文件记录（增量检测依据）
  - operations: 整理操作日志（回滚依据）
  - user_corrections: 用户手动修正记录（辅助 LLM 决策）
"""

import sqlite3
from datetime import datetime
from pathlib import Path

# 默认数据库路径
DEFAULT_DB_PATH = Path("data/filesquirrel.db")


class Database:
    """SQLite 数据库管理器，负责所有持久化操作。"""

    def __init__(self, db_path: str | Path | None = None):
        """
        初始化数据库连接并建表。

        Args:
            db_path: 数据库文件路径，默认为 data/filesquirrel.db
        """
        self.db_path = Path(db_path) if db_path else DEFAULT_DB_PATH
        # 确保数据目录存在
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(str(self.db_path))
        self.conn.row_factory = sqlite3.Row
        self._init_tables()

    def _init_tables(self):
        """创建数据库表（如果不存在）。"""
        cursor = self.conn.cursor()

        # 已处理文件记录：用于增量检测
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS files (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                file_hash   TEXT NOT NULL,              -- 文件内容 SHA256
                original_path TEXT NOT NULL,            -- 原始路径
                current_path  TEXT,                     -- 当前路径（整理后）
                status      TEXT NOT NULL DEFAULT 'processed',  -- processed / moved / renamed
                created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                UNIQUE(file_hash)
            )
        """)

        # 整理操作日志：用于回滚
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS operations (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                batch_id    INTEGER NOT NULL,           -- 整理批次 ID
                file_hash   TEXT NOT NULL,              -- 关联的文件 hash
                op_type     TEXT NOT NULL,              -- 操作类型: move / rename / create_dir
                src_path    TEXT,                       -- 操作前路径
                dst_path    TEXT,                       -- 操作后路径
                created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)

        # 用户手动修正记录：辅助未来决策
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS user_corrections (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                file_hash   TEXT NOT NULL,              -- 文件 hash
                llm_decision  TEXT NOT NULL,            -- LLM 原始决策（目标路径）
                user_correction TEXT NOT NULL,          -- 用户手动修改后的实际路径
                reason      TEXT,                       -- 修正原因（可选）
                created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime'))
            )
        """)

        # 批次记录：每次整理任务为一个批次
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS batches (
                id          INTEGER PRIMARY KEY AUTOINCREMENT,
                status      TEXT NOT NULL DEFAULT 'running',  -- running / completed / rolled_back
                file_count  INTEGER NOT NULL DEFAULT 0,
                created_at  TEXT NOT NULL DEFAULT (datetime('now', 'localtime')),
                finished_at TEXT
            )
        """)

        self.conn.commit()

    # ── 批次操作 ──────────────────────────────────────────

    def create_batch(self) -> int:
        """创建新的整理批次，返回批次 ID。"""
        cursor = self.conn.cursor()
        cursor.execute("INSERT INTO batches (status, file_count) VALUES ('running', 0)")
        self.conn.commit()
        return cursor.lastrowid

    def complete_batch(self, batch_id: int, file_count: int):
        """标记批次为已完成。"""
        self.conn.execute(
            "UPDATE batches SET status = 'completed', file_count = ?, finished_at = ? WHERE id = ?",
            (file_count, datetime.now().isoformat(), batch_id),
        )
        self.conn.commit()

    def rollback_batch(self, batch_id: int):
        """标记批次为已回滚。"""
        self.conn.execute(
            "UPDATE batches SET status = 'rolled_back', finished_at = ? WHERE id = ?",
            (datetime.now().isoformat(), batch_id),
        )
        self.conn.commit()

    # ── 文件记录 ──────────────────────────────────────────

    def is_file_processed(self, file_hash: str) -> bool:
        """检查文件是否已被处理过（增量检测）。"""
        row = self.conn.execute(
            "SELECT 1 FROM files WHERE file_hash = ? AND status != 'rolled_back'",
            (file_hash,),
        ).fetchone()
        return row is not None

    def record_file(self, file_hash: str, original_path: str, current_path: str, status: str = "processed"):
        """记录已处理的文件。"""
        self.conn.execute(
            "INSERT OR REPLACE INTO files (file_hash, original_path, current_path, status) VALUES (?, ?, ?, ?)",
            (file_hash, original_path, current_path, status),
        )
        self.conn.commit()

    def update_file_path(self, file_hash: str, new_path: str):
        """更新文件的当前路径。"""
        self.conn.execute(
            "UPDATE files SET current_path = ? WHERE file_hash = ?",
            (new_path, file_hash),
        )
        self.conn.commit()

    # ── 操作日志 ──────────────────────────────────────────

    def log_operation(self, batch_id: int, file_hash: str, op_type: str,
                      src_path: str, dst_path: str):
        """
        记录一条整理操作。

        Args:
            batch_id: 所属批次
            file_hash: 文件 hash
            op_type: 操作类型 (move / rename / create_dir)
            src_path: 源路径
            dst_path: 目标路径
        """
        self.conn.execute(
            "INSERT INTO operations (batch_id, file_hash, op_type, src_path, dst_path) VALUES (?, ?, ?, ?, ?)",
            (batch_id, file_hash, op_type, src_path, dst_path),
        )
        self.conn.commit()

    def get_batch_operations(self, batch_id: int) -> list[dict]:
        """获取指定批次的所有操作记录，按 ID 倒序（方便逆序回滚）。"""
        rows = self.conn.execute(
            "SELECT * FROM operations WHERE batch_id = ? ORDER BY id DESC",
            (batch_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_latest_batch_id(self) -> int | None:
        """获取最近一个可回滚的批次 ID。"""
        row = self.conn.execute(
            "SELECT id FROM batches WHERE status = 'completed' ORDER BY id DESC LIMIT 1"
        ).fetchone()
        return row["id"] if row else None

    # ── 用户修正 ──────────────────────────────────────────

    def record_user_correction(self, file_hash: str, llm_decision: str,
                               user_correction: str, reason: str = ""):
        """
        记录用户的手动修正。

        Args:
            file_hash: 文件 hash
            llm_decision: LLM 原始建议路径
            user_correction: 用户实际放置路径
            reason: 修正原因
        """
        self.conn.execute(
            "INSERT INTO user_corrections (file_hash, llm_decision, user_correction, reason) VALUES (?, ?, ?, ?)",
            (file_hash, llm_decision, user_correction, reason),
        )
        self.conn.commit()

    def get_corrections_for_hint(self, limit: int = 20) -> list[dict]:
        """
        获取最近的用户修正记录，作为 prompt 提示注入给 LLM。

        Args:
            limit: 返回的最大记录数

        Returns:
            修正记录列表，每条包含 llm_decision 和 user_correction
        """
        rows = self.conn.execute(
            "SELECT llm_decision, user_correction, reason FROM user_corrections ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── 查询 ──────────────────────────────────────────

    def get_batch_history(self, limit: int = 10) -> list[dict]:
        """获取最近 N 个批次的摘要信息。"""
        rows = self.conn.execute(
            "SELECT * FROM batches ORDER BY id DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        """关闭数据库连接。"""
        self.conn.close()
