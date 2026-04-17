"""
FileSquirrel 入口文件。

提供 CLI 命令来启动文件整理、回滚等操作。
"""

import argparse
import sys


def main():
    parser = argparse.ArgumentParser(
        prog="filesquirrel",
        description="FileSquirrel - 基于本地多模态模型的自动文件整理工具",
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

    # 各模块尚未实现，先打印占位信息
    print(f"[FileSquirrel] 命令 '{args.command}' 的实现尚未完成，敬请期待。")


if __name__ == "__main__":
    main()
