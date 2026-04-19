"""
FileSquirrel 入口文件。

提供 CLI 命令来启动文件整理、回滚、守护进程和历史查看。
所有子命令通过统一的模块协作完成。
"""

import argparse
import signal
import subprocess
import sys
from pathlib import Path

import requests


def check_ollama(base_url: str = "http://localhost:11434") -> bool:
    """
    检查 Ollama 服务是否可用。

    如果不可用，尝试自动启动（Windows）。

    Returns:
        True 表示 Ollama 可用
    """
    try:
        resp = requests.get(f"{base_url}/api/tags", timeout=5)
        resp.raise_for_status()
        return True
    except requests.ConnectionError:
        pass

    # 尝试自动启动 Ollama
    print("[FileSquirrel] Ollama 未运行，尝试自动启动...")
    try:
        subprocess.Popen(
            ["ollama", "serve"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        # 等待 Ollama 启动，最多等 30 秒
        for _ in range(15):
            import time
            time.sleep(2)
            try:
                resp = requests.get(f"{base_url}/api/tags", timeout=3)
                resp.raise_for_status()
                print("[FileSquirrel] Ollama 启动成功")
                return True
            except requests.ConnectionError:
                continue
    except FileNotFoundError:
        pass

    print("[FileSquirrel] 错误: Ollama 未安装或无法启动")
    print("  请先安装 Ollama: https://ollama.com/download")
    return False


def check_config(config_path: str) -> bool:
    """
    检查配置文件是否存在，不存在则从示例复制。

    Returns:
        True 表示配置文件就绪
    """
    if Path(config_path).exists():
        return True

    example = Path("config.yaml.example")
    if example.exists():
        print(f"[FileSquirrel] 配置文件 {config_path} 不存在，已从示例复制")
        print(f"  请编辑 {config_path} 修改 target_directory 等配置后重新运行")
        import shutil
        shutil.copy(example, config_path)
        return False
    else:
        print(f"[FileSquirrel] 错误: 找不到 {config_path} 和 config.yaml.example")
        return False


def run_organize(config_path: str = "config.yaml", debug: bool = False):
    """
    执行一次完整的整理流程：
    加载配置 → 扫描增量文件 → LLM 分析 → 执行整理

    Args:
        config_path: 配置文件路径
        debug: 是否在控制台输出 DEBUG 级别日志
    """
    from src.config import load_config
    from src.database import Database
    from src.logger import setup_logger
    from src.scanner import Scanner
    from src.analyzer import LLMAnalyzer
    from src.organizer import Organizer

    logger = setup_logger(debug=debug)
    logger.info("=" * 50)
    logger.info("FileSquirrel 整理任务开始")

    # 加载配置
    config = load_config(config_path)
    logger.info(f"目标目录: {config.target_directory}")
    mode_label = "Agent" if config.use_agent else "Linear"
    logger.info(f"整理模式: {mode_label}")

    # 初始化公共模块
    db = Database()
    scanner = Scanner(config, db)

    # 扫描增量文件
    logger.info("扫描增量文件...")
    new_files = scanner.scan_incremental()
    if not new_files:
        logger.info("没有新文件需要整理")
        db.close()
        return

    logger.info(f"发现 {len(new_files)} 个新文件待整理")

    # 根据配置选择整理模式
    if config.use_agent:
        from src.agent import FileAgent
        logger.info("启动 Agent 模式...")
        agent = FileAgent(config, db)
        count = agent.run(new_files)
    else:
        analyzer = LLMAnalyzer(config, db)
        organizer = Organizer(config, db)
        dir_structure = scanner.get_current_structure()
        files_map = {f.file_hash: f for f in new_files}
        logger.info("调用本地模型分析文件...")
        decisions = analyzer.analyze_batch(new_files, dir_structure)
        logger.info("执行整理操作...")
        count = organizer.execute_batch(decisions, files_map)

    logger.info(f"整理完成，成功处理 {count} 个文件")

    db.close()


def run_rollback(batch_id: int | None = None, config_path: str = "config.yaml"):
    """
    执行回滚操作。

    Args:
        batch_id: 指定批次 ID，None 则回滚最近一次
        config_path: 配置文件路径
    """
    from src.config import load_config
    from src.database import Database
    from src.logger import setup_logger
    from src.rollback import RollbackManager

    logger = setup_logger()
    config = load_config(config_path)
    db = Database()
    rb = RollbackManager(config, db)

    if batch_id:
        success = rb.rollback_batch(batch_id)
    else:
        success = rb.rollback_latest()

    if not success:
        print("回滚失败，请检查批次 ID 是否正确。")

    db.close()


def run_daemon(config_path: str = "config.yaml", run_now: bool = False, debug: bool = False):
    """
    以守护进程模式运行，闲时自动整理。

    Args:
        config_path: 配置文件路径
        run_now: 是否立即执行一次整理（跳过闲时检测）
        debug: 是否在控制台输出 DEBUG 级别日志
    """
    from src.config import load_config
    from src.logger import setup_logger
    from src.scheduler import Scheduler

    logger = setup_logger(debug=debug)
    config = load_config(config_path)

    # --now 模式：立即执行一次，之后进入正常守护循环
    if run_now:
        logger.info("--now 模式：立即执行一次整理")
        try:
            run_organize(config_path, debug=debug)
        except Exception as e:
            logger.error(f"立即整理异常: {e}", exc_info=True)

    # 整理函数闭包
    def organize_job():
        run_organize(config_path)

    scheduler = Scheduler(config, organize_job)

    # 注册信号处理，支持 Ctrl+C 优雅退出
    def handle_signal(signum, frame):
        logger.info("收到停止信号，正在退出...")
        scheduler.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    logger.info("FileSquirrel 守护进程模式启动")
    scheduler.run_daemon()


def run_history(config_path: str = "config.yaml"):
    """
    显示操作历史。

    Args:
        config_path: 配置文件路径
    """
    from src.config import load_config
    from src.database import Database

    config = load_config(config_path)
    db = Database()
    batches = db.get_batch_history(limit=20)

    if not batches:
        print("暂无操作历史。")
        db.close()
        return

    print(f"{'批次ID':<8}{'状态':<12}{'文件数':<8}{'创建时间':<22}{'完成时间':<22}")
    print("-" * 72)
    for b in batches:
        print(
            f"{b['id']:<8}"
            f"{b['status']:<12}"
            f"{b['file_count']:<8}"
            f"{b['created_at']:<22}"
            f"{b.get('finished_at', '—'):<22}"
        )

    db.close()


def main():
    """CLI 入口。"""
    parser = argparse.ArgumentParser(
        prog="filesquirrel",
        description="FileSquirrel - 基于本地多模态模型的自动文件整理工具",
    )
    parser.add_argument(
        "--config", "-c", default="config.yaml",
        help="配置文件路径（默认: config.yaml）",
    )
    parser.add_argument(
        "--debug", action="store_true",
        help="在控制台输出 DEBUG 级别日志，显示每个文件的 LLM 决策详情",
    )

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # organize 命令：执行一次文件整理
    subparsers.add_parser("organize", help="执行一次文件整理")

    # rollback 命令：回滚到指定批次
    rb_parser = subparsers.add_parser("rollback", help="回滚操作")
    rb_parser.add_argument(
        "--batch", "-b", type=int, help="回滚到指定批次 ID（不指定则回滚最近一次）"
    )

    # daemon 命令：以守护进程方式运行，闲时自动整理
    daemon_parser = subparsers.add_parser("daemon", help="以守护进程模式运行，闲时自动整理")
    daemon_parser.add_argument(
        "--now", action="store_true",
        help="跳过闲时检测，立即执行一次整理（方便测试）",
    )

    # history 命令：查看操作历史
    subparsers.add_parser("history", help="查看整理操作历史")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    # 启动前检查：配置文件
    if not check_config(args.config):
        sys.exit(1)

    # 启动前检查：Ollama（history 和 rollback 不需要）
    if args.command in ("organize", "daemon"):
        from src.config import load_config
        config = load_config(args.config)
        if not check_ollama(config.model.base_url):
            sys.exit(1)

    # 路由到对应命令
    if args.command == "organize":
        run_organize(args.config, debug=args.debug)
    elif args.command == "rollback":
        run_rollback(args.batch, args.config)
    elif args.command == "daemon":
        run_daemon(args.config, run_now=args.now, debug=args.debug)
    elif args.command == "history":
        run_history(args.config)


if __name__ == "__main__":
    main()
