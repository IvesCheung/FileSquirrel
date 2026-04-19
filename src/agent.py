"""
Agent 模式模块。

LLM 作为 agent 自主调用工具完成文件整理。
通过 Ollama 的 tool calling 接口，LLM 能：
  - 查看目录结构和文件信息
  - 读取文件内容预览
  - 创建目录、移动、重命名、删除文件
  - 根据操作结果调整后续策略

所有操作受 config 权限控制，且记录到数据库支持回滚。
"""

import json
import logging
import shutil
from fnmatch import fnmatch
from pathlib import Path

import requests

from src.config import AppConfig
from src.database import Database
from src.scanner import Scanner, compute_file_hash

logger = logging.getLogger("filesquirrel")


# ── Tool 定义：告诉 LLM 有哪些工具可用 ──────────────────

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "扫描指定目录，返回其中的文件列表。不传参则扫描目标根目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "directory": {
                        "type": "string",
                        "description": "要扫描的子目录相对路径（如 '文档'），留空扫描根目录",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_file_info",
            "description": "获取文件的详细信息：大小、类型、hash、是否存在。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件的相对路径",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文本文件的内容预览（最多 max_chars 个字符）。适用于 txt/md/csv/json/py 等文本文件。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件的相对路径",
                    },
                    "max_chars": {
                        "type": "integer",
                        "description": "最多读取的字符数，默认 2000",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_directory",
            "description": "创建新的子目录。路径相对于目标根目录。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要创建的目录相对路径，如 '论文/2024'",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": "将文件移动到新路径（可以同时实现移动和重命名）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {
                        "type": "string",
                        "description": "源文件相对路径",
                    },
                    "dst": {
                        "type": "string",
                        "description": "目标相对路径（含文件名）",
                    },
                },
                "required": ["src", "dst"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "rename_file",
            "description": "重命名文件（不改变所在目录）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件相对路径",
                    },
                    "new_name": {
                        "type": "string",
                        "description": "新文件名（仅文件名，不含路径）",
                    },
                },
                "required": ["path", "new_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_file",
            "description": "删除指定文件。此操作不可逆，请谨慎使用。（仅在 allow_delete 开启时可用）",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "要删除的文件相对路径",
                    },
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_directory_tree",
            "description": "获取当前完整的目录结构树概览。",
            "parameters": {
                "type": "object",
                "properties": {},
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_processed",
            "description": "检查某个文件是否已经被处理整理过。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "文件相对路径",
                    },
                },
                "required": ["path"],
            },
        },
    },
]


class FileAgent:
    """基于 Ollama tool calling 的文件整理 Agent。"""

    def __init__(self, config: AppConfig, db: Database):
        self.config = config
        self.db = db
        self.target = Path(config.target_directory).resolve()
        self.batch_id: int | None = None
        self.operation_count = 0

        # 根据权限过滤可用工具
        self.tools = self._filter_tools()

    def _filter_tools(self) -> list[dict]:
        """根据 config 权限过滤工具列表。"""
        available = []
        for tool in TOOL_DEFINITIONS:
            name = tool["function"]["name"]
            # 删除工具仅在 allow_delete 开启时注册
            if name == "delete_file" and not self.config.allow_delete:
                continue
            available.append(tool)
        return available

    def run(self, new_files: list) -> int:
        """
        执行 agent 整理流程。

        Args:
            new_files: 待处理的 FileInfo 列表（来自 scanner）

        Returns:
            成功处理的文件数
        """
        self.batch_id = self.db.create_batch()
        self.operation_count = 0

        # 构建初始消息：告诉 agent 有哪些文件需要整理
        file_list = "\n".join(
            f"  - {f.relative_path} ({f.size} bytes, {f.suffix})"
            for f in new_files
        )

        # 获取用户修正历史作为提示
        corrections = self.db.get_corrections_for_hint(limit=10)
        correction_text = ""
        if corrections:
            lines = []
            for c in corrections:
                lines.append(f"  - 我建议放: {c['llm_decision']} → 用户实际放: {c['user_correction']}")
            correction_text = (
                "\n以下是我过去做过的决策，但用户不满意并手动修正了，请参考这些偏好：\n"
                + "\n".join(lines) + "\n"
            )

        system_prompt = f"""你是一个文件整理助手。你需要使用提供的工具来整理文件。

## 用户整理要求
{self.config.organize_requirements}
{correction_text}
## 权限
- {'允许重命名' if self.config.allow_rename else '不允许重命名'}
- {'允许移动' if self.config.allow_move else '不允许移动'}
- {'允许创建新目录' if self.config.allow_create_dirs else '不允许创建新目录'}
- {'允许删除文件' if self.config.allow_delete else '不允许删除文件'}

## 规则
1. 所有路径都是相对于目标目录的相对路径
2. 先用 get_directory_tree 了解当前结构，再决定如何整理
3. 需要时用 read_file 查看文件内容来辅助判断
4. 每次操作后检查结果，确保成功再继续
5. 所有文件处理完后，回复 "DONE" 结束

## 待整理文件
{file_list}
"""

        # Agent 消息历史
        messages = [{"role": "system", "content": system_prompt}]

        # Agent 循环
        for i in range(self.config.max_iterations):
            logger.debug(f"[Agent] 第 {i + 1} 轮推理")

            # 调用 Ollama
            response = self._call_ollama(messages)

            # 打印 LLM 的思考内容
            msg = response.get("message", {})
            thinking = msg.get("thinking", "")
            content = msg.get("content", "")
            if thinking:
                logger.debug(f"[Agent] 思考: {thinking[:500]}")
            if content:
                logger.info(f"[Agent] 第 {i + 1} 轮: {content[:300]}")

            # 检查是否返回 tool_calls
            if response.get("tool_calls"):
                # 把 assistant 的 tool_calls 消息加入历史
                messages.append(msg)

                # 逐个执行 tool
                for tool_call in response["tool_calls"]:
                    tool_name = tool_call["function"]["name"]
                    tool_args = tool_call["function"]["arguments"]
                    logger.info(f"[Agent] 调用: {tool_name}({tool_args})")

                    result = self._execute_tool(tool_call)
                    # 把 tool 结果加入消息历史
                    messages.append({
                        "role": "tool",
                        "name": tool_name,
                        "content": result,
                    })
                    logger.info(f"[Agent] 结果: {result[:300]}")
            else:
                # 没有 tool_calls，检查是否完成
                messages.append({"role": "assistant", "content": content})

                if "DONE" in content.upper() or i > self.config.max_iterations - 5:
                    logger.debug("[Agent] Agent 声明完成")
                    break

        # 完成批次
        self.db.complete_batch(self.batch_id, self.operation_count)
        return self.operation_count

    def _call_ollama(self, messages: list) -> dict:
        """
        调用 Ollama chat API（原生 tool calling）。

        Args:
            messages: 对话历史

        Returns:
            Ollama 响应 dict
        """
        url = f"{self.config.model.base_url}/api/chat"
        payload = {
            "model": self.config.model.name,
            "messages": messages,
            "tools": self.tools,
            "stream": False,
            "options": {
                "temperature": 0.3,
                "num_predict": 4096,      # 足够的 token 预算给 tool calling
            },
            "think": False,               # 关闭 thinking，避免思考占满输出
        }

        try:
            resp = requests.post(url, json=payload, timeout=self.config.model.timeout)
            resp.raise_for_status()
            return resp.json()
        except requests.exceptions.HTTPError as e:
            error_detail = ""
            try:
                error_detail = e.response.json().get("error", e.response.text)
            except Exception:
                error_detail = str(e)
            logger.error(f"[Agent] Ollama 调用失败: {error_detail}")
            return {"message": {"content": f"ERROR: {error_detail}"}}
        except Exception as e:
            logger.error(f"[Agent] Ollama 调用失败: {e}")
            return {"message": {"content": f"ERROR: {e}"}}

    def _execute_tool(self, tool_call: dict) -> str:
        """
        执行单个 tool 调用。

        Args:
            tool_call: Ollama 返回的 tool_call 对象

        Returns:
            执行结果字符串（返回给 LLM）
        """
        name = tool_call["function"]["name"]
        try:
            args = json.loads(tool_call["function"]["arguments"])
        except json.JSONDecodeError:
            return "ERROR: 参数解析失败"

        # 路由到对应的 tool 实现
        handler = {
            "list_files": self._tool_list_files,
            "get_file_info": self._tool_get_file_info,
            "read_file": self._tool_read_file,
            "create_directory": self._tool_create_directory,
            "move_file": self._tool_move_file,
            "rename_file": self._tool_rename_file,
            "delete_file": self._tool_delete_file,
            "get_directory_tree": self._tool_get_directory_tree,
            "check_processed": self._tool_check_processed,
        }.get(name)

        if not handler:
            return f"ERROR: 未知工具 '{name}'"

        try:
            return handler(args)
        except Exception as e:
            return f"ERROR: {e}"

    # ── Tool 实现 ──────────────────────────────────────

    def _tool_list_files(self, args: dict) -> str:
        """扫描指定目录返回文件列表。"""
        directory = args.get("directory", "")
        scan_dir = (self.target / directory).resolve() if directory else self.target

        if not scan_dir.exists():
            return f"ERROR: 目录不存在 '{directory}'"

        # 安全检查
        if not str(scan_dir).startswith(str(self.target)):
            return "ERROR: 路径超出目标目录范围"

        files = []
        for f in sorted(scan_dir.rglob("*")):
            if not f.is_file():
                continue
            if self._should_ignore(f):
                continue
            rel = str(f.relative_to(self.target))
            files.append(f"{rel} ({f.stat().st_size} bytes)")

        if not files:
            return "目录为空"
        return "\n".join(files)

    def _tool_get_file_info(self, args: dict) -> str:
        """获取文件详细信息。"""
        path = args["path"]
        full = (self.target / path).resolve()

        if not full.exists():
            return f"ERROR: 文件不存在 '{path}'"

        stat = full.stat()
        try:
            file_hash = compute_file_hash(full)
        except Exception:
            file_hash = "无法计算"

        return json.dumps({
            "path": path,
            "size": stat.st_size,
            "suffix": full.suffix.lower(),
            "hash": file_hash[:16] + "...",
            "exists": True,
        }, ensure_ascii=False)

    def _tool_read_file(self, args: dict) -> str:
        """读取文件内容预览。"""
        path = args["path"]
        max_chars = args.get("max_chars", 2000)
        full = (self.target / path).resolve()

        if not full.exists():
            return f"ERROR: 文件不存在 '{path}'"

        try:
            content = full.read_text(encoding="utf-8", errors="ignore")[:max_chars]
            return content
        except Exception as e:
            return f"ERROR: 无法读取文件: {e}"

    def _tool_create_directory(self, args: dict) -> str:
        """创建新目录。"""
        if not self.config.allow_create_dirs:
            return "ERROR: 未开启 allow_create_dirs 权限"

        path = args["path"]
        full = (self.target / path).resolve()

        if not str(full).startswith(str(self.target)):
            return "ERROR: 路径超出目标目录范围"

        full.mkdir(parents=True, exist_ok=True)
        logger.info(f"[Agent] 创建目录: {path}")
        return f"OK: 目录已创建 '{path}'"

    def _tool_move_file(self, args: dict) -> str:
        """移动文件。"""
        if not self.config.allow_move:
            return "ERROR: 未开启 allow_move 权限"

        src_rel = args["src"]
        dst_rel = args["dst"]
        src = (self.target / src_rel).resolve()
        dst = (self.target / dst_rel).resolve()

        # 安全检查
        if not str(dst).startswith(str(self.target)):
            return "ERROR: 目标路径超出范围"
        if not src.exists():
            return f"ERROR: 源文件不存在 '{src_rel}'"

        # 确保目标目录存在
        dst.parent.mkdir(parents=True, exist_ok=True)

        # 处理冲突
        if dst.exists() and dst != src:
            dst = self._resolve_conflict(dst)

        shutil.move(str(src), str(dst))
        actual_dst = str(dst.relative_to(self.target))

        # 记录操作到数据库
        file_hash = compute_file_hash(dst)
        self.db.record_file(file_hash, src_rel, actual_dst, "moved")
        self.db.log_operation(self.batch_id, file_hash, "move", src_rel, actual_dst)
        self.operation_count += 1

        logger.info(f"[Agent] 移动: {src_rel} → {actual_dst}")
        return f"OK: 已移动 '{src_rel}' → '{actual_dst}'"

    def _tool_rename_file(self, args: dict) -> str:
        """重命名文件。"""
        if not self.config.allow_rename:
            return "ERROR: 未开启 allow_rename 权限"

        path_rel = args["path"]
        new_name = args["new_name"]
        src = (self.target / path_rel).resolve()
        dst = src.parent / new_name

        if not src.exists():
            return f"ERROR: 文件不存在 '{path_rel}'"
        if dst.exists():
            dst = self._resolve_conflict(dst)

        shutil.move(str(src), str(dst))
        actual_name = dst.name

        # 记录操作
        file_hash = compute_file_hash(dst)
        self.db.record_file(file_hash, path_rel, str(dst.relative_to(self.target)), "renamed")
        self.db.log_operation(self.batch_id, file_hash, "rename", path_rel,
                              str(dst.relative_to(self.target)))
        self.operation_count += 1

        logger.info(f"[Agent] 重命名: {path_rel} → {actual_name}")
        return f"OK: 已重命名为 '{actual_name}'"

    def _tool_delete_file(self, args: dict) -> str:
        """删除文件（仅在 allow_delete 开启时注册此工具）。"""
        path_rel = args["path"]
        full = (self.target / path_rel).resolve()

        if not str(full).startswith(str(self.target)):
            return "ERROR: 路径超出目标目录范围"
        if not full.exists():
            return f"ERROR: 文件不存在 '{path_rel}'"

        file_hash = compute_file_hash(full)
        full.unlink()

        self.db.log_operation(self.batch_id, file_hash, "delete", path_rel, "")
        self.operation_count += 1

        logger.info(f"[Agent] 删除: {path_rel}")
        return f"OK: 已删除 '{path_rel}'"

    def _tool_get_directory_tree(self, args: dict) -> str:
        """获取当前目录结构树。"""
        lines = []
        for item in sorted(self.target.rglob("*")):
            if self._should_ignore(item):
                continue
            rel = str(item.relative_to(self.target))
            depth = rel.count("/")
            prefix = "  " * depth
            if item.is_dir():
                lines.append(f"{prefix}{item.name}/")
            else:
                lines.append(f"{prefix}{item.name}")

        if not lines:
            return "目录为空"
        return "\n".join(lines)

    def _tool_check_processed(self, args: dict) -> str:
        """检查文件是否已处理过。"""
        path = args["path"]
        full = (self.target / path).resolve()

        if not full.exists():
            return "文件不存在"

        try:
            file_hash = compute_file_hash(full)
        except Exception:
            return "无法计算文件 hash"

        if self.db.is_file_processed(file_hash):
            return "该文件已被处理过"
        return "该文件尚未处理"

    # ── 辅助方法 ──────────────────────────────────────

    def _should_ignore(self, path: Path) -> bool:
        """判断文件是否匹配忽略模式。"""
        for pattern in self.config.ignore_patterns:
            if fnmatch(path.name, pattern):
                return True
        return False

    @staticmethod
    def _resolve_conflict(dst: Path) -> Path:
        """文件名冲突时添加数字后缀。"""
        stem = dst.stem
        suffix = dst.suffix
        parent = dst.parent
        counter = 1
        while True:
            new_path = parent / f"{stem}_{counter}{suffix}"
            if not new_path.exists():
                return new_path
            counter += 1
