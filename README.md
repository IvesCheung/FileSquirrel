# FileSquirrel 🐿️

**[English](README_EN.md)** | 中文

基于本地大模型的自动文件整理工具。通过 Ollama 调用本地 LLM，自动对指定目录下的文件进行分类、重命名、归档。

## Quick Start

```bash
# 1. 安装 Ollama 并启动
# https://ollama.com/download

# 2. 拉取模型
ollama pull qwen3.5:4b

# 3. 安装依赖
pip install -r requirements.txt

# 4. 复制并编辑配置
cp config.yaml.example config.yaml
# 编辑 config.yaml 中的 target_directory

# 5. 执行一次整理
python -m src.main organize

# 或带 debug 日志
python -m src.main --debug organize
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `organize` | 执行一次文件整理 |
| `daemon` | 守护进程模式，闲时自动整理 |
| `daemon --now` | 立即整理一次后进入守护模式 |
| `rollback` | 回滚最近一次操作 |
| `rollback -b <id>` | 回滚指定批次 |
| `history` | 查看操作历史 |

Global flags: `--config <path>`, `--debug`

## Config

```yaml
# 监控的目标目录
target_directory: 'C:/Users/xxx/Downloads'

# Ollama 模型
model:
  name: "qwen3.5:4b"
  base_url: "http://localhost:11434"
  timeout: 120

# 操作权限
allow_rename: true
allow_move: true
allow_create_dirs: true
allow_delete: false

# 整理模式
use_agent: true          # true=agent模式, false=线性模式
max_iterations: 200      # agent 最大推理轮次

# 忽略文件
ignore_patterns:
  - "*.tmp"
  - "*.part"

# 整理要求（注入到 LLM prompt 中）
organize_requirements: |
  请按照文件类型和用途进行分类整理。
  目录名使用中文，文件名保持可读性。

# 闲时调度
schedule:
  enabled: true
  idle_minutes: 30
  quiet_hours:
    start: "23:00"
    end: "07:00"
```

### Config Fields

| Field | Default | Description |
|-------|---------|-------------|
| `target_directory` | (必填) | 要整理的目标目录 |
| `model.name` | `qwen3.5:4b` | Ollama 模型名称 |
| `model.base_url` | `http://localhost:11434` | Ollama 服务地址 |
| `model.timeout` | `120` | 单次请求超时（秒） |
| `allow_rename` | `true` | 允许重命名文件 |
| `allow_move` | `true` | 允许移动文件 |
| `allow_create_dirs` | `true` | 允许创建新目录 |
| `allow_delete` | `false` | 允许删除文件 |
| `use_agent` | `false` | agent 模式（LLM 自主调用工具） |
| `max_iterations` | `200` | agent 最大推理轮次 |
| `whitelist_dirs` | `[]` | 只整理这些子目录，空则整理全部 |
| `ignore_patterns` | `["*.tmp", "*.part"]` | 忽略的文件模式 |
| `organize_requirements` | (内置默认) | 自定义整理要求，注入 LLM prompt |
| `schedule.enabled` | `true` | 是否启用闲时调度 |
| `schedule.idle_minutes` | `30` | 空闲多少分钟后触发 |
| `schedule.quiet_hours` | `23:00-07:00` | 允许运行的时间段 |

## Agent Mode Tools

Agent 模式下，LLM 可自主调用以下工具：

| Tool | Description | Key Args |
|------|-------------|----------|
| `list_files` | 扫描目录返回文件列表 | `directory` |
| `get_file_info` | 获取文件详细信息 | `path` |
| `read_file` | 读取文本文件预览 | `path`, `max_chars` |
| `get_directory_tree` | 获取完整目录结构 | (无参数) |
| `create_directory` | 创建子目录 | `path` |
| `move_file` | 移动文件 | `src`, `dst` |
| `rename_file` | 重命名文件 | `path`, `new_name` |
| `delete_file` | 删除文件（需 allow_delete） | `path` |
| `check_processed` | 检查文件是否已处理过 | `path` |

## Modes

- **Linear 模式** (`use_agent: false`): 逐文件调用 LLM 分析 → 生成决策 → 批量执行
- **Agent 模式** (`use_agent: true`): LLM 自主决策，可查看目录、读取文件、创建目录、移动/重命名，支持多步推理

## Rollback

所有操作记录到 SQLite 数据库，支持按批次回滚：

```bash
# 回滚最近一次
python -m src.main rollback

# 回滚指定批次
python -m src.main rollback -b 3
```

## License

MIT
