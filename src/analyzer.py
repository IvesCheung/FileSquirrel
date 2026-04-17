"""
LLM 分析模块。

通过 Ollama 调用本地多模态模型，分析文件内容并生成整理建议。
支持文本文件和图片文件的多模态分析。
"""

import base64
import json
import mimetypes
from dataclasses import dataclass
from pathlib import Path

import requests

from src.config import AppConfig, ModelConfig
from src.database import Database
from src.scanner import FileInfo

# 支持多模态分析的图片扩展名
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp"}

# 文本文件扩展名（可以直接读取内容给 LLM）
TEXT_EXTENSIONS = {
    ".txt", ".md", ".csv", ".json", ".xml", ".html", ".css", ".js",
    ".py", ".java", ".c", ".cpp", ".h", ".go", ".rs", ".ts",
    ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".log", ".sh", ".bat", ".ps1", ".sql",
}

# 文档扩展名（元数据分析）
DOCUMENT_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".xls", ".xlsx", ".ppt", ".pptx",
}

# 音视频扩展名（仅元数据分析）
MEDIA_EXTENSIONS = {
    ".mp3", ".mp4", ".avi", ".mkv", ".flac", ".wav", ".mov",
    ".wmv", ".m4a", ".aac", ".flv",
}

# 压缩包扩展名
ARCHIVE_EXTENSIONS = {
    ".zip", ".rar", ".7z", ".tar", ".gz", ".bz2", ".xz",
}


@dataclass
class OrganizeDecision:
    """LLM 对单个文件的整理决策。"""
    file_hash: str           # 文件 hash
    original_path: str       # 原始相对路径
    target_path: str         # 建议的目标相对路径
    should_rename: bool      # 是否需要重命名
    new_name: str | None     # 新文件名（如果需要重命名）
    reason: str              # LLM 给出的理由
    confidence: float        # 置信度 0.0 ~ 1.0


class LLMAnalyzer:
    """调用 Ollama 本地模型分析文件内容并给出整理建议。"""

    def __init__(self, config: AppConfig, db: Database):
        self.config = config
        self.db = db
        self.model = config.model

    def analyze_file(self, file_info: FileInfo, dir_structure: dict) -> OrganizeDecision:
        """
        分析单个文件，返回整理建议。

        Args:
            file_info: 扫描得到的文件信息
            dir_structure: 当前目录结构概览

        Returns:
            LLM 的整理决策
        """
        # 构建上下文
        file_context = self._build_file_context(file_info)
        corrections = self.db.get_corrections_for_hint(limit=10)

        # 构建 prompt
        prompt = self._build_prompt(file_info, file_context, dir_structure, corrections)

        # 调用 Ollama
        response_text = self._call_ollama(prompt)

        # 解析响应
        return self._parse_response(file_info, response_text)

    def analyze_batch(self, files: list[FileInfo], dir_structure: dict) -> list[OrganizeDecision]:
        """
        批量分析文件。逐个调用 LLM 避免上下文过长。

        Args:
            files: 待分析的文件列表
            dir_structure: 当前目录结构

        Returns:
            整理决策列表
        """
        decisions = []
        for f in files:
            try:
                decision = self.analyze_file(f, dir_structure)
                decisions.append(decision)
            except Exception as e:
                # 单个文件分析失败不影响整体流程
                decisions.append(OrganizeDecision(
                    file_hash=f.file_hash,
                    original_path=f.relative_path,
                    target_path=f.relative_path,  # 保持原位
                    should_rename=False,
                    new_name=None,
                    reason=f"分析失败: {e}",
                    confidence=0.0,
                ))
        return decisions

    def _build_file_context(self, file_info: FileInfo) -> dict:
        """
        提取文件上下文信息，供 LLM 理解文件内容。

        根据文件类型采用不同策略：
        - 文本文件：读取前 2000 字符
        - 图片文件：base64 编码（多模态）
        - 其他文件：仅使用文件名和元数据
        """
        suffix = file_info.suffix
        context: dict = {
            "name": file_info.path.name,
            "suffix": suffix,
            "size": file_info.size,
            "relative_path": file_info.relative_path,
        }

        if suffix in TEXT_EXTENSIONS:
            # 文本文件：尝试读取前 2000 字符
            try:
                content = file_info.path.read_text(encoding="utf-8", errors="ignore")[:2000]
                context["text_preview"] = content
            except Exception:
                context["text_preview"] = "[无法读取文件内容]"

        elif suffix in IMAGE_EXTENSIONS:
            # 图片文件：base64 编码供多模态模型使用
            try:
                image_data = file_info.path.read_bytes()
                context["image_base64"] = base64.b64encode(image_data).decode("utf-8")
                mime_type = mimetypes.guess_type(str(file_info.path))[0] or "image/png"
                context["image_mime"] = mime_type
            except Exception:
                context["text_preview"] = "[无法读取图片]"

        elif suffix in DOCUMENT_EXTENSIONS:
            context["type_hint"] = "文档文件，请根据文件名判断类型和用途"
        elif suffix in MEDIA_EXTENSIONS:
            context["type_hint"] = "音视频文件，请根据文件名判断类型和用途"
        elif suffix in ARCHIVE_EXTENSIONS:
            context["type_hint"] = "压缩包文件，请根据文件名判断内容"
        else:
            context["type_hint"] = "未知文件类型，请根据文件名和路径判断"

        return context

    def _build_prompt(self, file_info: FileInfo, file_context: dict,
                      dir_structure: dict, corrections: list[dict]) -> str:
        """构建发给 LLM 的完整 prompt。"""
        corrections_text = ""
        if corrections:
            lines = []
            for c in corrections:
                lines.append(f"  - 我建议放: {c['llm_decision']} → 用户实际放: {c['user_correction']}")
            corrections_text = (
                "\n以下是我过去做过的决策，但用户不满意并手动修正了，请参考这些偏好：\n"
                + "\n".join(lines) + "\n"
            )

        # 将目录结构转为简洁的缩进文本
        structure_text = self._format_dir_structure(dir_structure)

        # 文件内容摘要
        content_text = ""
        if "text_preview" in file_context:
            content_text = f"\n文件内容预览:\n```\n{file_context['text_preview']}\n```"
        elif "type_hint" in file_context:
            content_text = f"\n文件类型提示: {file_context['type_hint']}"

        prompt = f"""你是一个文件整理助手。请分析以下文件信息，决定如何整理它。

## 用户整理要求
{self.config.organize_requirements}

## 当前目录结构
{structure_text}
{corrections_text}
## 待整理文件
- 文件名: {file_context['name']}
- 扩展名: {file_context['suffix']}
- 大小: {file_context['size']} 字节
- 相对路径: {file_context['relative_path']}
{content_text}

## 权限
- {'允许重命名' if self.config.allow_rename else '不允许重命名'}
- {'允许移动' if self.config.allow_move else '不允许移动'}
- {'允许创建新目录' if self.config.allow_create_dirs else '不允许创建新目录'}

请严格按照以下 JSON 格式回复，不要包含其他文字:
{{
  "target_path": "建议的相对路径（含文件名）",
  "should_rename": true或false,
  "new_name": "新文件名或null",
  "reason": "整理理由",
  "confidence": 0.0到1.0的置信度
}}"""
        return prompt

    def _call_ollama(self, prompt: str, image_base64: str | None = None,
                     image_mime: str = "image/png") -> str:
        """
        调用 Ollama API 生成响应。

        Args:
            prompt: 文本 prompt
            image_base64: 可选的图片 base64 编码（多模态）
            image_mime: 图片 MIME 类型

        Returns:
            模型的文本响应
        """
        url = f"{self.model.base_url}/api/generate"

        # 构建请求体
        payload = {
            "model": self.model.name,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.3,    # 低温度，保证决策稳定
                "num_predict": 512,    # 限制输出长度
            },
        }

        # 如果有图片，添加到 prompt 中
        if image_base64:
            payload["images"] = [image_base64]

        response = requests.post(url, json=payload, timeout=self.model.timeout)
        response.raise_for_status()

        result = response.json()
        return result.get("response", "")

    def _call_ollama_multimodal(self, prompt: str, image_base64: str,
                                image_mime: str = "image/png") -> str:
        """
        调用 Ollama 多模态接口（图片 + 文本）。

        Args:
            prompt: 文本 prompt
            image_base64: 图片 base64 编码
            image_mime: 图片 MIME 类型

        Returns:
            模型的文本响应
        """
        return self._call_ollama(prompt, image_base64, image_mime)

    def _parse_response(self, file_info: FileInfo, response: str) -> OrganizeDecision:
        """
        解析 LLM 的 JSON 响应为 OrganizeDecision。

        如果解析失败，返回保持原位的默认决策。
        """
        # 尝试从响应中提取 JSON
        try:
            # 处理响应中可能包含的 markdown 代码块
            json_str = response.strip()
            if "```json" in json_str:
                json_str = json_str.split("```json")[1].split("```")[0].strip()
            elif "```" in json_str:
                json_str = json_str.split("```")[1].split("```")[0].strip()

            data = json.loads(json_str)

            return OrganizeDecision(
                file_hash=file_info.file_hash,
                original_path=file_info.relative_path,
                target_path=data.get("target_path", file_info.relative_path),
                should_rename=data.get("should_rename", False),
                new_name=data.get("new_name"),
                reason=data.get("reason", ""),
                confidence=float(data.get("confidence", 0.5)),
            )
        except (json.JSONDecodeError, KeyError, ValueError):
            # JSON 解析失败，保持原位
            return OrganizeDecision(
                file_hash=file_info.file_hash,
                original_path=file_info.relative_path,
                target_path=file_info.relative_path,
                should_rename=False,
                new_name=None,
                reason=f"LLM 响应解析失败，原始响应: {response[:200]}",
                confidence=0.0,
            )

    @staticmethod
    def _format_dir_structure(structure: dict, indent: int = 0) -> str:
        """将目录结构字典格式化为缩进文本。"""
        lines = []
        prefix = "  " * indent
        for name, children in structure.items():
            lines.append(f"{prefix}{name}/")
            if children:
                lines.append(LLMAnalyzer._format_dir_structure(children, indent + 1))
        return "\n".join(lines)
