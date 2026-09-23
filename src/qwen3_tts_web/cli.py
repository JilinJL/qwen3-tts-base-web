"""CLI shared by shell launchers, automation and the terminal UI."""

import argparse
import json
import os
import sys
from dataclasses import asdict
from pathlib import Path

from .config import load_settings
from .models import MODELS


def parser():
    result = argparse.ArgumentParser(
        prog="qwen3-tts-web", description="Qwen3-TTS 跨平台部署与服务管理"
    )
    result.add_argument(
        "command",
        choices=["tui", "doctor", "setup", "download", "serve"],
        nargs="?",
        default="tui",
    )
    result.add_argument("--root", type=Path, help="项目/数据根目录")
    result.add_argument("--device", help="auto、mps、cuda:N")
    result.add_argument("--model", choices=["1.7B", "0.6B"])
    result.add_argument("--source", choices=["domestic", "official"])
    result.add_argument("--host")
    result.add_argument("--port", type=int)
    result.add_argument(
        "--offline", action=argparse.BooleanOptionalAction, default=None
    )
    result.add_argument("--pip-index")
    result.add_argument("--torch-index")
    result.add_argument("--hf-endpoint")
    result.add_argument("--yes", action="store_true", help="确认安装/下载，无交互运行")
    result.add_argument(
        "--install-system", action="store_true", help="setup 同时安装缺少的 ffmpeg/sox"
    )
    result.add_argument("--skip-download", action="store_true", help="setup 仅安装依赖")
    result.add_argument(
        "--model-key", choices=list(MODELS), help="download 的目标；默认当前 Base"
    )
    result.add_argument("--json", action="store_true", help="doctor 输出 JSON")
    return result


def confirm(message, yes=False):
    if yes:
        return
    if not sys.stdin.isatty():
        raise RuntimeError(f"{message}；非交互运行需显式传入 --yes。")
    if input(message + " [y/N] ").strip().lower() not in ("y", "yes"):
        raise RuntimeError("操作已取消")


def serve(settings, yes=False):
    from .environment import check_port
    from .models import model_path, prepare_model, validate_model
    from .process import Runner

    python = settings.runtime_python
    if not python.exists():
        raise RuntimeError("推理环境不存在，请先运行 setup。")
    problem = check_port(settings.host, settings.port)
    if problem:
        raise RuntimeError(problem)
    if (
        validate_model(model_path(settings), settings.model_key)
        and not settings.offline
    ):
        confirm(f"将下载 {MODELS[settings.model_key]} 完整快照（数 GB）", yes)
    prepare_model(settings)
    # Compare environment prefixes, not resolved executable symlinks shared by venvs.
    if Path(sys.prefix).absolute() != (settings.root / ".venv").absolute():
        return Runner().run(
            [python, "-m", "qwen3_tts_web", "serve", "--yes"],
            env=settings.environment(),
            cwd=settings.root,
        )
    from .environment import platform_profile

    platform_profile(settings)
    os.environ.update(settings.environment())
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["TRANSFORMERS_OFFLINE"] = "1"
    import uvicorn

    print(f"Web: {settings.url}  API: {settings.url}/docs", flush=True)
    from filelock import FileLock

    with FileLock(str(settings.root / ".runtime.lock"), timeout=0):
        uvicorn.run(
            "qwen3_tts_web.server:app",
            host=settings.host,
            port=settings.port,
            workers=1,
        )
    return 0


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        settings = load_settings(args.root, vars(args))
        if not settings.root.is_dir():
            raise ValueError(f"根目录不存在: {settings.root}")
        if args.command == "tui":
            if not sys.stdin.isatty() or not sys.stdout.isatty():
                parser().print_help()
                print("\n非交互终端：请选择 doctor/setup/download/serve 子命令。")
                return 2
            from .tui import ManagerApp

            ManagerApp(settings).run()
        elif args.command == "doctor":
            from .environment import doctor

            checks = doctor(settings)
            if args.json:
                print(json.dumps([asdict(c) for c in checks], ensure_ascii=False))
            else:
                for check in checks:
                    print(f"[{check.status.upper()}] {check.name}: {check.detail}")
            return int(any(check.status == "error" for check in checks))
        elif args.command == "setup":
            from .setup import install

            message = "将安装/修复项目 .venv 依赖"
            if not args.skip_download:
                message += "，并下载数 GB 模型文件"
            if args.install_system:
                from .environment import system_install_command

                message += "；系统命令：" + " ".join(system_install_command())
            confirm(message, args.yes)
            install(settings, args.install_system, args.skip_download)
        elif args.command == "download":
            from .models import ensure_model, model_path, validate_model

            key = args.model_key or settings.model_key
            if validate_model(model_path(settings, key), key) and not settings.offline:
                confirm(f"将下载 {MODELS[key]} 完整快照（数 GB）", args.yes)
            ensure_model(settings, key)
        elif args.command == "serve":
            return serve(settings, args.yes)
        return 0
    except KeyboardInterrupt:
        print("\n操作已取消", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"[错误] {exc}", file=sys.stderr)
        return 1
