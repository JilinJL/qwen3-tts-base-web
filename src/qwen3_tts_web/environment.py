"""Read-only checks and platform-specific installation policy."""

import csv
import io
import os
import platform
import shutil
import socket
import subprocess
import sys
from dataclasses import dataclass

import psutil


@dataclass
class Check:
    name: str
    status: str
    detail: str


def platform_profile(settings, system=None, machine=None, gpu_rows=None):
    system = system or platform.system()
    machine = machine or platform.machine()
    if system == "Darwin":
        if machine != "arm64":
            raise RuntimeError(
                "仅支持原生 Apple Silicon arm64；Intel Mac / Rosetta Python 不在支持范围。"
            )
        if settings.device.startswith("cuda"):
            raise RuntimeError("macOS 不支持 CUDA，请选择 auto 或 mps。")
        return "macos"
    if system not in ("Windows", "Linux") or machine.lower() not in ("x86_64", "amd64"):
        raise RuntimeError(
            "本版本支持 Apple Silicon macOS 或 x86_64 Windows/Linux NVIDIA。"
        )
    if settings.device == "mps":
        raise RuntimeError("MPS 仅用于 Apple Silicon macOS。")
    if gpu_rows is None:
        try:
            result = subprocess.run(
                [
                    "nvidia-smi",
                    "--query-gpu=index,uuid,name,memory.total,compute_cap",
                    "--format=csv,noheader,nounits",
                ],
                capture_output=True,
                text=True,
                check=True,
                timeout=15,
            )
            gpu_rows = list(
                csv.reader(io.StringIO(result.stdout), skipinitialspace=True)
            )
        except (OSError, subprocess.SubprocessError) as exc:
            raise RuntimeError(
                "无法读取 NVIDIA GPU，请检查驱动和 nvidia-smi。"
            ) from exc
    visible = os.environ.get("CUDA_VISIBLE_DEVICES")
    if visible is not None:
        filtered = []
        for identifier in visible.split(","):
            filtered.extend(
                row
                for row in gpu_rows
                if row[0] == identifier.strip() or row[1] == identifier.strip()
            )
        gpu_rows = filtered
    index = int(settings.device.partition(":")[2] or 0)
    try:
        row = gpu_rows[index]
        memory, capability = float(row[3]), float(row[4])
    except (IndexError, ValueError) as exc:
        raise RuntimeError(
            "无法解析选定 GPU 的显存/计算能力，或设备编号超出范围。"
        ) from exc
    minimum = 6144 if settings.model == "1.7B" else 4096
    if memory < minimum:
        raise RuntimeError(
            f"{row[2]} 显存 {memory:.0f} MiB，当前模型要求至少 {minimum} MiB；可选择 0.6B。"
        )
    if capability < 6.1 or capability >= 10:
        raise RuntimeError(
            f"GPU sm_{capability:g} 不在当前固定 PyTorch 分支的支持范围；需要单独验证新版本。"
        )
    return "cu118" if capability < 7.5 else "cu126"


def check_port(host, port):
    try:
        infos = socket.getaddrinfo(
            host, port, type=socket.SOCK_STREAM, flags=socket.AI_PASSIVE
        )
        for family, socktype, proto, _, address in infos:
            with socket.socket(family, socktype, proto) as sock:
                sock.bind(address)
        return None
    except OSError as exc:
        return f"{host}:{port} 不可用: {exc}；请修改端口，不会终止其他进程。"


def probe_runtime(settings):
    if not settings.runtime_python.exists():
        return Check("runtime", "error", "尚未安装 .venv；请运行 setup")
    try:
        result = subprocess.run(
            [str(settings.runtime_python), "-m", "qwen3_tts_web.probe"],
            cwd=settings.root,
            env=settings.environment(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=90,
        )
        if result.returncode:
            return Check("runtime", "error", (result.stderr or result.stdout).strip())
        return Check("runtime", "ok", result.stdout.strip())
    except (OSError, subprocess.SubprocessError) as exc:
        return Check("runtime", "error", str(exc))


def doctor(settings):
    from .models import model_path, validate_model

    checks = []
    try:
        profile = platform_profile(settings)
        checks.append(
            Check(
                "platform",
                "ok",
                f"{platform.system()} {platform.machine()} / {profile}",
            )
        )
    except RuntimeError as exc:
        checks.append(Check("platform", "error", str(exc)))
    checks.append(
        Check(
            "python",
            "ok" if sys.version_info[:2] == (3, 11) else "error",
            sys.version.split()[0],
        )
    )
    memory = psutil.virtual_memory()
    checks.append(
        Check(
            "memory",
            "warn" if memory.available < 8 * 1024**3 else "ok",
            f"总量 {memory.total / 1024**3:.1f} GiB，可用 {memory.available / 1024**3:.1f} GiB",
        )
    )
    free = shutil.disk_usage(settings.root).free
    errors = validate_model(model_path(settings), settings.model_key)
    checks.append(
        Check(
            "disk",
            "warn" if free < (10 if errors else 2) * 1024**3 else "ok",
            f"剩余 {free / 1024**3:.1f} GiB；首次部署建议至少 10 GiB",
        )
    )
    for tool in ("ffmpeg", "sox"):
        path = shutil.which(tool)
        checks.append(
            Check(
                tool,
                "ok" if path else "warn",
                path or "未安装；完整音频格式支持需要此工具",
            )
        )
    checks.append(probe_runtime(settings))
    checks.append(
        Check(
            "model",
            "error" if errors else "ok",
            "; ".join(errors) if errors else model_path(settings).name,
        )
    )
    port_error = check_port(settings.host, settings.port)
    checks.append(
        Check(
            "port",
            "error" if port_error else "ok",
            port_error or f"{settings.host}:{settings.port} 可用",
        )
    )
    return checks


def system_install_command():
    missing = [name for name in ("ffmpeg", "sox") if not shutil.which(name)]
    if not missing:
        return []
    if platform.system() == "Darwin" and shutil.which("brew"):
        return ["brew", "install", *missing]
    if platform.system() == "Windows" and shutil.which("scoop"):
        return ["scoop", "install", *missing]
    if platform.system() == "Linux":
        prefix = [] if hasattr(os, "geteuid") and os.geteuid() == 0 else ["sudo"]
        if shutil.which("apt-get"):
            return [*prefix, "apt-get", "install", "-y", *missing, "libsndfile1"]
        if shutil.which("dnf"):
            return [*prefix, "dnf", "install", "-y", *missing, "libsndfile"]
    raise RuntimeError(
        "缺少 ffmpeg/sox。请先安装已有平台包管理器：macOS 用 Homebrew，Windows 用 Scoop，Linux 用 apt/dnf。"
    )
