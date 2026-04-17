"""
FileSquirrel 入口文件。

提供 CLI 命令来启动文件整理、回滚、守护进程和历史查看。
所有子命令通过统一的模块协作完成。
"""

import argparse
import signal
import sys

from src.config import load_config
from src.database import Database
from src.logger import setup_logger
from src.rollback import RollbackManager
from src.scheduler import Scheduler
from src.scanner import Scanner
from src.analyzer import LLMAnalyzer
from src.organizer import Organizer


def run_organize(config_path: str = "config.yaml"):
    """
    执行一次完整的整理流程：
    加载配置 → 扫描增量文件 → LLM 分析 → 执行整理

    Args:
        config_path: 配置文件路径
    """
    logger = setup_logger()
    logger.info("="*50)
    logger.info("FileSquirrel 整理任务开始")

    # 加载配置
    config = load_config(config_path)
    logger.info(f"目标目录: {config.target_directory}")

    # 初始化模块
    db = Database()
    scanner = Scanner(config, db)
    analyzer = LLMAnalyzer(config, db)
    organizer = Organizer(config, db)

    # 扫描增量文件
    logger.info("扫描增量文件...")
    new_files = scanner.scan_incremental()
    if not new_files:
        logger.info("没有新文件需要整理")
        db.close()
        return

    logger.info(f"发现 {len(new_files)} 个新文件待整理")

    # 获取当前目录结构，供 LLM 参考
    dir_structure = scanner.get_current_structure()

    # 构建 file_hash → FileInfo 映射
    files_map = {f.file_hash: f for f in new_files}

    # LLM 批量分析
    logger.info("调用本地模型分析文件...")
    decisions = analyzer.analyze_batch(new_files, dir_structure)

    # 执行整理
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


def run_daemon(config_path: str = "config.yaml"):
    """
    以守护进程模式运行，闲时自动整理。

    Args:
        config_path: 配置文件路径
    """
    logger = setup_logger()
    config = load_config(config_path)

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

    subparsers = parser.add_subparsers(dest="command", help="可用命令")

    # organize 命令：执行一次文件整理
    subparsers.add_parser("organize", help="执行一次文件整理")

    # rollback 命令：回滚到指定批次
    rb_parser = subparsers.add_parser("rollback", help="回滚操作")
    rb_parser.add_argument(
        "--batch", "-b", type=int, help="回滚到指定批次 ID（不指定则回滚最近一次）"
    )

    # daemon 命令：以守护进程方式运行，闲时自动整理
    subparsers.add_parser("daemon", help="以守护进程模式运行，闲时自动整理")

    # history 命令：查看操作历史
    subparsers.add_parser("history", help="查看整理操作历史")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(1)

    # 路由到对应命令
    if args.command == "organize":
        run_organize(args.config)
    elif args.command == "rollback":
        run_rollback(args.batch, args.config)
    elif args.command == "daemon":
        run_daemon(args.config)
    elif args.command == "history":
        run_history(args.config)


if __name__ == "__main__":
    main()
