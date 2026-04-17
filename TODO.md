# FileSquirrel - TODO

基于本地多模态模型的自动文件整理工具。

## Phase 1 - 核心功能

- [ ] 项目骨架：依赖管理、目录结构、入口文件
- [ ] config 模块：config.yaml 加载与校验
- [ ] database 模块：SQLite 增量追踪 + 用户手动修正记录
- [ ] scanner 模块：文件扫描与增量检测（基于 hash）
- [ ] LLM analyzer 模块：通过 Ollama 调用本地多模态模型分析文件
- [ ] organizer 模块：移动 / 重命名 / 创建目录
- [ ] rollback 模块：操作日志与一键回退
- [ ] logger 模块：完善的日志记录

## Phase 2 - 调度与集成

- [ ] scheduler 模块：闲时检测 + 优雅中断（用户活跃时暂停）
- [ ] main.py 主流程集成

## Phase 3 - 增强功能

- [ ] embedding 召回：基于历史决策辅助新文件归类
