"""Runs only inside the runtime interpreter; never downloads models."""

import importlib.metadata
import json
import sys
from pathlib import Path

from .config import load_settings
from .environment import platform_profile
from .runtime import pick_dtype, select_device


def main():
    if sys.version_info[:2] != (3, 11):
        raise RuntimeError(
            "推理环境必须使用 Python 3.11；请保留旧环境并手动更名后重建。"
        )
    settings = load_settings()
    profile = platform_profile(settings)
    constraints = Path(__file__).parent / "constraints" / f"{profile}.txt"
    for line in constraints.read_text().splitlines():
        package, version = line.split("==")
        actual = importlib.metadata.version(package)
        if actual != version:
            raise RuntimeError(f"{package}: {actual} != {version}；请运行 setup 修复")
    import fastapi  # noqa: F401
    import python_multipart  # noqa: F401
    import soundfile  # noqa: F401
    import torch
    import torchaudio  # noqa: F401
    import torchvision  # noqa: F401
    import uvicorn  # noqa: F401
    from qwen_tts import Qwen3TTSModel  # noqa: F401
    from transformers import AutoProcessor  # noqa: F401

    device = select_device(settings.device)
    # A real kernel launch catches wheels that import successfully but lack GPU support.
    tensor = torch.ones((16, 16), device=device)
    value = float((tensor @ tensor).sum().cpu())
    if value != 4096:
        raise RuntimeError("设备计算自检失败")
    if device == "mps":
        # This asymmetric-head shape reproduces the MPS GQA crash in torch 2.6.
        query = torch.ones((1, 16, 10, 128), device=device)
        key = torch.ones((1, 8, 10, 128), device=device)
        attention = torch.nn.functional.scaled_dot_product_attention(
            query, key, key, is_causal=True, enable_gqa=True
        )
        if not torch.isfinite(attention).all().item():
            raise RuntimeError("MPS 分组注意力自检失败")
    print(
        json.dumps(
            {
                "device": device,
                "dtype": str(pick_dtype(device)),
                "torch": torch.__version__,
                "profile": profile,
            }
        )
    )


if __name__ == "__main__":
    main()
