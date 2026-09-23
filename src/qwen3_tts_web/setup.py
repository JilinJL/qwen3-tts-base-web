"""Idempotent installation into a separate inference environment."""

import os
import subprocess
import sys
from pathlib import Path

from filelock import FileLock

from .environment import platform_profile, probe_runtime, system_install_command
from .process import Cancelled, Runner


def uv_command():
    return [sys.executable, "-m", "uv"]


def install(settings, install_system=False, skip_download=False, log=print):
    if settings.offline:
        raise RuntimeError("离线模式不安装依赖；可运行 doctor 检查已安装环境。")
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError("请使用 scripts/bootstrap 入口获取 Python 3.11。")
    profile = platform_profile(settings)
    runner = Runner(log)
    env = settings.environment()
    # Scope package indexes to each subprocess, never mutate user pip/uv configuration.
    for key in list(env):
        if key.startswith(("PIP_", "UV_INDEX", "UV_DEFAULT_INDEX", "UV_EXTRA_INDEX")):
            env.pop(key)
    env["UV_NO_CONFIG"] = "1"
    env["PIP_CONFIG_FILE"] = os.devnull
    env["UV_HTTP_TIMEOUT"] = "120"
    env["UV_LINK_MODE"] = "copy"
    with FileLock(str(settings.root / ".runtime.lock"), timeout=0):
        if install_system:
            command = system_install_command()
            if command:
                # Inherit the terminal for sudo; a TUI task reports an actionable error instead.
                if command[0] == "sudo":
                    command.insert(1, "-n")
                if os.name == "nt":
                    command = ["powershell", "-NoProfile", "-Command", *command]
                runner.run(command, env=env, cwd=settings.root)
        venv = settings.root / ".venv"
        if venv.exists():
            if not settings.runtime_python.exists():
                raise RuntimeError(
                    "现有 .venv 已损坏；不会删除，请手动更名备份后重跑 setup。"
                )
            result = subprocess.run(
                [
                    str(settings.runtime_python),
                    "-c",
                    "import sys; print('%d.%d' % sys.version_info[:2])",
                ],
                capture_output=True,
                text=True,
                timeout=15,
            )
            if result.returncode or result.stdout.strip() != "3.11":
                raise RuntimeError(
                    "现有 .venv 不是 Python 3.11；不会删除，请更名备份后重跑 setup。"
                )
        else:
            runner.run(
                [*uv_command(), "venv", "--python", sys.executable, venv], env=env
            )
        constraints = Path(__file__).parent / "constraints" / f"{profile}.txt"
        requirement = (
            f"{settings.root}[server]"
            if (settings.root / "pyproject.toml").is_file()
            else "qwen3-tts-web[server]==3.0.0"
        )
        package_index = settings.pip_index or (
            "https://pypi.tuna.tsinghua.edu.cn/simple"
            if settings.source == "domestic"
            else "https://pypi.org/simple"
        )
        indexes = [package_index]
        if package_index != "https://pypi.org/simple":
            indexes.append("https://pypi.org/simple")
        torch_index = settings.torch_index or (
            f"https://mirror.sjtu.edu.cn/pytorch-wheels/{profile}"
            if settings.source == "domestic"
            else f"https://download.pytorch.org/whl/{profile}"
        )
        for attempt, index in enumerate(indexes):
            command = [
                *uv_command(),
                "pip",
                "install",
                "--python",
                settings.runtime_python,
                "--constraint",
                constraints,
                "--index-url",
                index,
            ]
            if profile != "macos":
                url = (
                    torch_index
                    if attempt == 0
                    else f"https://download.pytorch.org/whl/{profile}"
                )
                command += ["--index", url]
            command += [requirement]
            try:
                runner.run(command, env=env, cwd=settings.root)
                break
            except Cancelled:
                raise
            except RuntimeError:
                if attempt == len(indexes) - 1:
                    raise
                log("[回退] 镜像安装失败，使用官方源重试")
        check = probe_runtime(settings)
        if check.status != "ok":
            raise RuntimeError(check.detail)
        log(f"[OK] 推理环境验证通过: {check.detail}")
        if not skip_download:
            from .models import ensure_model

            ensure_model(settings, log=log)
        log("[OK] 部署完成")
