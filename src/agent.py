"""
Agent 模式模块。

LLM 作为 agent 自主调用工具完成文件整理。
采用模拟 tool calling：在 prompt 中描述工具，从模型文本回复中解析 JSON 工具调用。
所有操作受 config 权限控制，且记录到数据库支持回滚。
"""

import json
import logging
import re
import shutil
from fnmatch import fnmatch
from pathlib import Path

import requests

from src.config import AppConfig
from src.database import Database
from src.scanner import Scanner, compute_file_hash

logger = logging.getLogger("filesquirrel")


# ── 工具描述：用于生成 system prompt 中的工具说明 ──────────────────

TOOL_SCHEMAS = {
    "list_files": {
        "description": "扫描指定目录，返回其中的文件列表。不传参则扫描目标根目录。",
        "parameters": {
            "directory": "要扫描的子目录相对路径（如 '文档'），留空扫描根目录",
        },
        "required": [],
    },
    "get_file_info": {
        "description": "获取文件的详细信息：大小、类型、hash、是否存在。",
        "parameters": {
            "path": "文件的相对路径",
        },
        "required": ["path"],
    },
    "read_file": {
        "description": "读取文本文件的内容预览（最多 max_chars 个字符）。适用于 txt/md/csv/json/py 等文本文件。",
        "parameters": {
            "path": "文件的相对路径",
            "max_chars": "最多读取的字符数，默认 2000",
        },
        "required": ["path"],
    },
    "create_directory": {
        "description": "创建新的子目录。路径相对于目标根目录。",
        "parameters": {
            "path": "要创建的目录相对路径，如 '论文/2024'",
        },
        "required": ["path"],
    },
    "move_file": {
        "description": "将文件移动到新路径（可以同时实现移动和重命名）。",
        "parameters": {
            "src": "源文件相对路径",
            "dst": "目标相对路径（含文件名）",
        },
        "required": ["src", "dst"],
    },
    "rename_file": {
        "description": "重命名文件（不改变所在目录）。",
        "parameters": {
            "path": "文件相对路径",
            "new_name": "新文件名（仅文件名，不含路径）",
        },
        "required": ["path", "new_name"],
    },
    "delete_file": {
        "description": "删除指定文件。此操作不可逆，请谨慎使用。（仅在 allow_delete 开启时可用）",
        "parameters": {
            "path": "要删除的文件相对路径",
        },
        "required": ["path"],
    },
    "get_directory_tree": {
        "description": "获取当前完整的目录结构树概览。",
        "parameters": {},
        "required": [],
    },
    "check_processed": {
        "description": "检查某个文件是否已经被处理整理过。",
        "parameters": {
            "path": "文件相对路径",
        },
        "required": ["path"],
    },
}

# 不受权限控制的工具（始终可用）
ALWAYS_AVAILABLE = {"list_files", "get_file_info", "read_file", "get_directory_tree", "check_processed"}


class FileAgent:
    """基于模拟 tool calling 的文件整理 Agent。"""

    def __init__(self, config: AppConfig, db: Database):
        self.config = config
        self.db = db
        self.target = Path(config.target_directory).resolve()
        self.batch_id: int | None = None
        self.operation_count = 0

    def _get_available_tools(self) -> dict:
        """根据 config 权限过滤可用工具。"""
        tools = {}
        for name, schema in TOOL_SCHEMAS.items():
            # 删除工具仅在 allow_delete 开启时可用
            if name == "delete_file" and not self.config.allow_delete:
                continue
            # 重命名/移动/创建目录受各自权限控制
            if name == "rename_file" and not self.config.allow_rename:
                continue
            if name == "move_file" and not self.config.allow_move:
                continue
            if name == "create_directory" and not self.config.allow_create_dirs:
                continue
            tools[name] = schema
        return tools

    def _build_tools_description(self) -> str:
        """生成工具描述文本，嵌入到 system prompt 中。"""
        tools = self._get_available_tools()
        lines = []
        for name, schema in tools.items():
            params_desc = ""
            if schema["parameters"]:
                param_lines = []
                for pname, pdesc in schema["parameters"].items():
                    required_mark = "（必填）" if pname in schema["required"] else "（可选）"
                    param_lines.append(f"    - \"{pname}\": {pdesc} {required_mark}")
                params_desc = "\n".join(param_lines)
            else:
                params_desc = "    （无参数）"
            lines.append(f"- {name}: {schema['description']}\n{params_desc}")
        return "\n".join(lines)

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

        tools_desc = self._build_tools_description()

        system_prompt = f"""/no_think
你是一个文件整理助手。你需要使用提供的工具来整理文件。

## 用户整理要求
{self.config.organize_requirements}
{correction_text}
## 权限
- {'允许重命名' if self.config.allow_rename else '不允许重命名'}
- {'允许移动' if self.config.allow_move else '不允许移动'}
- {'允许创建新目录' if self.config.allow_create_dirs else '不允许创建新目录'}
- {'允许删除文件' if self.config.allow_delete else '不允许删除文件'}

## 可用工具
{tools_desc}

## 规则
1. 所有路径都是相对于目标目录的相对路径
2. 先用 get_directory_tree 了解当前结构，再决定如何整理
3. 需要时用 read_file 查看文件内容来辅助判断
4. 每次操作后检查结果，确保成功再继续
5. 所有文件处理完后，回复 "DONE" 结束

## 回复格式
每次回复你必须且只能输出一个 JSON 对象来调用工具，格式如下：
{{"tool": "工具名", "args": {{"参数名": "参数值"}}}}

示例：
- 调用 get_directory_tree: {{"tool": "get_directory_tree", "args": {{}}}}
- 移动文件: {{"tool": "move_file", "args": {{"src": "foo.txt", "dst": "文档/foo.txt"}}}}
- 创建目录: {{"tool": "create_directory", "args": {{"path": "论文/2024"}}}}

不要输出任何其他文字，只输出 JSON。整理全部完成后回复 DONE。

## 待整理文件
{file_list}
"""

        # Agent 消息历史
        messages = [{"role": "system", "content": system_prompt}]

        # Agent 循环
        last_call_key = ""      # 上一次工具调用的唯一标识
        repeat_count = 0        # 连续重复次数
        max_repeat = 3          # 同一操作最多重试次数
        skipped_calls: set[str] = set()  # 已跳过的失败操作集合
        consecutive_skips = 0   # 连续命中已跳过操作的次数

        for i in range(self.config.max_iterations):
            logger.debug(f"[Agent] 第 {i + 1} 轮推理")

            # 调用 Ollama
            content = self._call_ollama(messages)

            if not content or not content.strip():
                logger.warning(f"[Agent] 第 {i + 1} 轮: 模型返回空内容，跳过")
                continue

            # 去除 <think/> 标签内容（某些模型仍可能输出）
            content_clean = re.sub(r'<think[\s\S]*?</think\s*>', '', content).strip()

            # 去除 /no_think 可能残留的前缀空白
            content_clean = content_clean.replace('/no_think', '').strip()

            logger.info(f"[Agent] 第 {i + 1} 轮: {content_clean[:300]}")

            # 尝试从回复中解析工具调用 JSON
            tool_call = self._parse_tool_call(content_clean)

            if tool_call:
                tool_name = tool_call["tool"]
                tool_args = tool_call.get("args", {})

                # 生成调用唯一标识
                call_key = f"{tool_name}:{json.dumps(tool_args, sort_keys=True, ensure_ascii=False)}"

                # 已被标记跳过的操作，直接拦截不执行
                if call_key in skipped_calls:
                    consecutive_skips += 1
                    logger.warning(f"[Agent] 已跳过的失败操作 ({consecutive_skips}次): {tool_name}")
                    if consecutive_skips >= 3:
                        logger.warning("[Agent] 模型持续重复已跳过的操作，强制结束本轮整理")
                        break
                    # 不加入消息历史，避免上下文膨胀
                    continue

                consecutive_skips = 0  # 成功解析到新操作，重置跳过计数

                # 检测重复调用
                if call_key == last_call_key:
                    repeat_count += 1
                else:
                    repeat_count = 1
                    last_call_key = call_key

                if repeat_count > max_repeat:
                    logger.warning(f"[Agent] 连续 {repeat_count} 次重复调用 {tool_name}，永久跳过")
                    skipped_calls.add(call_key)
                    messages.append({"role": "assistant", "content": content_clean})
                    messages.append({
                        "role": "user",
                        "content": f"你已连续 {repeat_count} 次执行同一操作且失败，该操作已被跳过。请处理下一个文件，或回复 DONE 结束。",
                    })
                    continue

                logger.info(f"[Agent] 调用: {tool_name}({json.dumps(tool_args, ensure_ascii=False)})")

                # 执行工具
                result = self._execute_tool(tool_name, tool_args)
                logger.info(f"[Agent] 结果: {result[:300]}")

                # 把本轮对话加入历史
                messages.append({"role": "assistant", "content": content_clean})
                messages.append({"role": "user", "content": f"工具执行结果:\n{result}"})
            else:
                # 没有解析到工具调用
                messages.append({"role": "assistant", "content": content_clean})

                if "DONE" in content_clean.upper():
                    logger.info("[Agent] Agent 声明完成")
                    break
                elif i >= self.config.max_iterations - 5:
                    logger.warning("[Agent] 达到最大轮次，强制结束")
                    break
                else:
                    # 模型可能输出了非 JSON 文本，提醒它继续
                    messages.append({
                        "role": "user",
                        "content": "请用 JSON 格式调用工具，或回复 DONE 结束。",
                    })

        # 完成批次
        self.db.complete_batch(self.batch_id, self.operation_count)
        return self.operation_count

    def _call_ollama(self, messages: list) -> str:
        """
        调用 Ollama Chat API。

        Args:
            messages: 对话历史

        Returns:
            模型回复的文本内容
        """
        url = f"{self.config.model.base_url}/api/chat"
        payload = {
            "model": self.config.model.name,
            "messages": messages,
            "stream": False,
            "think": False,
            "options": {
                "temperature": 0.3,
                "num_predict": 4096,
            },
        }

        try:
            resp = requests.post(url, json=payload, timeout=self.config.model.timeout)
            resp.raise_for_status()
            result = resp.json()
            return result.get("message", {}).get("content", "")
        except requests.exceptions.HTTPError as e:
            error_detail = ""
            try:
                error_detail = e.response.json().get("error", e.response.text)
            except Exception:
                error_detail = str(e)
            logger.error(f"[Agent] Ollama 调用失败: {error_detail}")
            return ""
        except Exception as e:
            logger.error(f"[Agent] Ollama 调用失败: {e}")
            return ""

    @staticmethod
    def _parse_tool_call(text: str) -> dict | None:
        """
        从模型回复文本中解析工具调用 JSON。

        支持多种格式：
        - 纯 JSON: {"tool": "...", "args": {...}}
        - 包裹在代码块中: ```json ... ```
        - 夹杂其他文本

        Returns:
            解析成功返回 {"tool": str, "args": dict}，否则 None
        """
        # 先尝试直接解析整个文本
        try:
            data = json.loads(text)
            if "tool" in data:
                return data
        except json.JSONDecodeError:
            pass

        # 尝试从 markdown 代码块中提取
        code_block_match = re.search(r'```(?:json)?\s*(\{[\s\S]*?\})\s*```', text)
        if code_block_match:
            try:
                data = json.loads(code_block_match.group(1))
                if "tool" in data:
                    return data
            except json.JSONDecodeError:
                pass

        # 尝试从文本中找第一个完整的 JSON 对象
        json_match = re.search(r'\{[^{}]*"tool"\s*:\s*"[^"]+?"[^{}]*\}', text)
        if json_match:
            try:
                data = json.loads(json_match.group(0))
                if "tool" in data:
                    return data
            except json.JSONDecodeError:
                pass

        # 更宽松的匹配：嵌套一层 args
        json_match = re.search(r'\{[\s\S]*?"tool"\s*:\s*"[\s\S]*?\}', text)
        if json_match:
            candidate = json_match.group(0)
            # 找到匹配的闭合括号
            depth = 0
            for idx, ch in enumerate(candidate):
                if ch == '{':
                    depth += 1
                elif ch == '}':
                    depth -= 1
                    if depth == 0:
                        candidate = candidate[:idx + 1]
                        break
            try:
                data = json.loads(candidate)
                if "tool" in data:
                    return data
            except json.JSONDecodeError:
                pass

        return None

    def _execute_tool(self, name: str, args: dict) -> str:
        """
        执行单个工具调用。

        Args:
            name: 工具名称
            args: 工具参数

        Returns:
            执行结果字符串（返回给 LLM）
        """
        # 路由到对应的工具实现
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
