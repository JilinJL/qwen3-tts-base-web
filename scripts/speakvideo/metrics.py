"""Best-effort Windows worker resource metrics.

The gateway uses this data for display only. Missing optional system tools are
reported explicitly instead of being replaced with made-up values.
"""

from __future__ import annotations

from datetime import datetime, timezone
import subprocess
from typing import Iterable


def _number(value: str):
    value = value.strip()
    if value.upper() in {"", "N/A", "NA", "[N/A]"}:
        return None
    try:
        number = float(value)
    except ValueError:
        return None
    return int(number) if number.is_integer() else number


def _gpu_snapshot() -> tuple[list[dict], str | None]:
    command = [
        "nvidia-smi",
        "--query-gpu=name,utilization.gpu,memory.used,memory.total,temperature.gpu",
        "--format=csv,noheader,nounits",
    ]
    try:
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except FileNotFoundError:
        return [], "nvidia-smi not found"
    except subprocess.TimeoutExpired:
        return [], "nvidia-smi timed out"
    except (OSError, subprocess.CalledProcessError) as exc:
        return [], f"nvidia-smi failed: {exc}"

    gpus = []
    for line in completed.stdout.splitlines():
        fields = [part.strip() for part in line.split(",", 4)]
        if len(fields) != 5:
            continue
        name, utilization, used, total, temperature = fields
        gpus.append(
            {
                "name": name,
                "utilization_percent": _number(utilization),
                "memory_used_mb": _number(used),
                "memory_total_mb": _number(total),
                "temperature_c": _number(temperature),
            }
        )
    return gpus, None if gpus else "nvidia-smi returned no GPUs"


def collect_metrics(
    *, queue_depth: int, current_job_id: str | None, logs: Iterable[str]
) -> dict:
    """Collect one JSON-safe resource snapshot without exposing job content."""

    result = {
        "timestamp": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "queue_depth": queue_depth,
        "current_job_id": current_job_id or "",
        "cpu_percent": None,
        "memory_used_mb": None,
        "memory_total_mb": None,
        "gpus": [],
        "logs": list(logs)[-50:],
    }
    try:
        import psutil

        result["cpu_percent"] = psutil.cpu_percent(interval=0.05)
        memory = psutil.virtual_memory()
        result["memory_used_mb"] = round(memory.used / 1024 / 1024)
        result["memory_total_mb"] = round(memory.total / 1024 / 1024)
    except ImportError:
        result["cpu_memory_status"] = "psutil not installed"
    except Exception as exc:  # pragma: no cover - platform-specific failure
        result["cpu_memory_status"] = f"psutil failed: {exc}"

    gpus, status = _gpu_snapshot()
    result["gpus"] = gpus
    if status:
        result["gpu_status"] = status
    return result
