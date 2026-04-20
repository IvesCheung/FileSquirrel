# FileSquirrel 🐿️

English | **[中文](README.md)**

An automatic file organizer powered by local LLMs. Uses Ollama to call a local model to classify, rename, and archive files in a target directory.

## Quick Start

```bash
# 1. Install and start Ollama
# https://ollama.com/download

# 2. Pull a model
ollama pull qwen3.5:4b

# 3. Install dependencies
pip install -r requirements.txt

# 4. Copy and edit config
cp config.yaml.example config.yaml
# Edit target_directory in config.yaml

# 5. Run once
python -m src.main organize

# Or with debug logs
python -m src.main --debug organize
```

## CLI Commands

| Command | Description |
|---------|-------------|
| `organize` | Run file organization once |
| `daemon` | Daemon mode, auto-organize during idle time |
| `daemon --now` | Run once immediately, then enter daemon mode |
| `rollback` | Undo the most recent operation |
| `rollback -b <id>` | Undo a specific batch |
| `history` | View operation history |

Global flags: `--config <path>`, `--debug`

## Config

```yaml
# Target directory to organize
target_directory: 'C:/Users/xxx/Downloads'

# Ollama model
model:
  name: "qwen3.5:4b"
  base_url: "http://localhost:11434"
  timeout: 120

# Permissions
allow_rename: true
allow_move: true
allow_create_dirs: true
allow_delete: false

# Organization mode
use_agent: true          # true=agent mode, false=linear mode
max_iterations: 200      # max agent reasoning rounds

# Ignore patterns
ignore_patterns:
  - "*.tmp"
  - "*.part"

# Custom requirements (injected into LLM prompt)
organize_requirements: |
  Classify files by type and purpose.
  Use readable directory and file names.

# Idle schedule
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
| `target_directory` | (required) | Directory to organize |
| `model.name` | `qwen3.5:4b` | Ollama model name |
| `model.base_url` | `http://localhost:11434` | Ollama server URL |
| `model.timeout` | `120` | Request timeout in seconds |
| `allow_rename` | `true` | Allow renaming files |
| `allow_move` | `true` | Allow moving files |
| `allow_create_dirs` | `true` | Allow creating directories |
| `allow_delete` | `false` | Allow deleting files |
| `use_agent` | `false` | Agent mode (LLM calls tools autonomously) |
| `max_iterations` | `200` | Max agent reasoning rounds |
| `whitelist_dirs` | `[]` | Only organize these subdirs, empty = all |
| `ignore_patterns` | `["*.tmp", "*.part"]` | File patterns to ignore |
| `organize_requirements` | (built-in default) | Custom instructions injected into LLM prompt |
| `schedule.enabled` | `true` | Enable idle scheduling |
| `schedule.idle_minutes` | `30` | Minutes of idle before triggering |
| `schedule.quiet_hours` | `23:00-07:00` | Allowed time window |

## Agent Mode Tools

In agent mode, the LLM can autonomously call these tools:

| Tool | Description | Key Args |
|------|-------------|----------|
| `list_files` | Scan directory for file listing | `directory` |
| `get_file_info` | Get file details | `path` |
| `read_file` | Read text file preview | `path`, `max_chars` |
| `get_directory_tree` | Get full directory tree | (none) |
| `create_directory` | Create subdirectory | `path` |
| `move_file` | Move file | `src`, `dst` |
| `rename_file` | Rename file | `path`, `new_name` |
| `delete_file` | Delete file (requires allow_delete) | `path` |
| `check_processed` | Check if file was already processed | `path` |

## Modes

- **Linear mode** (`use_agent: false`): Analyze each file via LLM → generate decisions → batch execute
- **Agent mode** (`use_agent: true`): LLM autonomously decides — browse directories, read files, create folders, move/rename, supports multi-step reasoning

## Rollback

All operations are logged in SQLite and can be rolled back by batch:

```bash
# Undo latest
python -m src.main rollback

# Undo specific batch
python -m src.main rollback -b 3
```

## License

MIT
