"""
Qwen3-TTS 公共配置 / 模型加载工具。

三种模式对应三个模型：
  预设音色 (CustomVoice) -> Qwen3-TTS-12Hz-1.7B-CustomVoice
  克隆音色 (Base)        -> Qwen3-TTS-12Hz-1.7B-Base
  制作音色 (VoiceDesign) -> Qwen3-TTS-12Hz-1.7B-VoiceDesign
"""
import os
import sys

# Windows 下避免 HuggingFace 联网校验 & 符号链接权限问题
os.environ.setdefault("HF_HUB_OFFLINE", "1")
os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")

ROOT = os.path.dirname(os.path.abspath(__file__))
MODELS_DIR = os.path.join(ROOT, "models")
OUT_DIR = os.path.join(ROOT, "outputs")
REF_DIR = os.path.join(ROOT, "references")
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(REF_DIR, exist_ok=True)

MODELS = {
    "tokenizer": "Qwen3-TTS-Tokenizer-12Hz",
    "customvoice": "Qwen3-TTS-12Hz-1.7B-CustomVoice",
    "voicedesign": "Qwen3-TTS-12Hz-1.7B-VoiceDesign",
    "base": "Qwen3-TTS-12Hz-1.7B-Base",
}


def model_path(key):
    p = os.path.join(MODELS_DIR, MODELS[key])
    if not os.path.isdir(p):
        sys.exit(
            f"找不到模型目录: {p}\n请先运行:  python download_models.py"
        )
    return p


def pick_dtype(device="cuda:0"):
    """按 GPU 计算能力自动选精度 —— 让同一套脚本能同时跑在不同显卡上。

    sm_80+  (Ampere / Ada / Hopper / Blackwell，如 RTX 30/40/50 系) -> bfloat16
    sm_75 及以下 (Turing / Volta，如 GTX 1660 Ti / RTX 20 系)      -> float16
    CPU                                                            -> float32

    为什么要区分：Turing 及更老的卡**不支持 bfloat16**，
    硬用 bf16 会报 "no kernel image is available" 或者结果错乱。
    torch.cuda.is_bf16_supported() 就是官方判据（sm<8.0 返回 False）。
    """
    import torch

    if not str(device).startswith("cuda") or not torch.cuda.is_available():
        return torch.float32
    try:
        if torch.cuda.is_bf16_supported():
            return torch.bfloat16
    except Exception:
        pass
    return torch.float16


def load(key, device="cuda:0", attn="sdpa", dtype=None):
    """加载模型。不传 dtype 时按显卡自动选（见 pick_dtype）。

    8GB 显存用 sdpa 即可；装了 flash-attn 可传 attn='flash_attention_2'。
    """
    import torch
    from qwen_tts import Qwen3TTSModel

    if dtype is None:
        dtype = pick_dtype(device)
    elif dtype == torch.bfloat16 and str(device).startswith("cuda"):
        # 防止在 Turing 等老卡上误用 bf16
        if torch.cuda.is_available() and not torch.cuda.is_bf16_supported():
            print("[warn] 当前显卡不支持 bfloat16，自动改用 float16")
            dtype = torch.float16
    kw = dict(device_map=device, dtype=dtype)
    # sdpa 显式传入，避免 transformers 默认去 import flash_attn
    if attn:
        kw["attn_implementation"] = attn
    print(f"[load] {MODELS[key]}  device={device} dtype={dtype} attn={attn or 'default'}")
    return Qwen3TTSModel.from_pretrained(model_path(key), **kw)


def free():
    import gc

    import torch

    gc.collect()
    if torch.cuda.is_available():
        torch.cuda.empty_cache()


def save(wavs, sr, name):
    import soundfile as sf

    paths = []
    for i, w in enumerate(wavs):
        p = os.path.join(OUT_DIR, name if len(wavs) == 1 else f"{name}_{i}")
        p = p if p.endswith(".wav") else p + ".wav"
        sf.write(p, w, sr)
        print(f"[save] {p}  ({len(w)/sr:.2f}s @ {sr}Hz)")
        paths.append(p)
    return paths


def gpu_info():
    import torch

    if not torch.cuda.is_available():
        return "CUDA 不可用（将回退 CPU，速度很慢）"
    i = torch.cuda.current_device()
    props = torch.cuda.get_device_properties(i)
    total = props.total_memory / 1024**3
    used = torch.cuda.memory_allocated(i) / 1024**3
    return (
        f"{props.name} | 算力 sm_{props.major}{props.minor} | "
        f"总显存 {total:.1f} GB (当前已分配 {used:.2f} GB) | torch {torch.__version__}"
    )


def vram_peak(reset=False):
    """返回本进程 CUDA 峰值显存占用（GB）。reset=True 时先清零重新统计。"""
    import torch

    if not torch.cuda.is_available():
        return 0.0
    if reset:
        torch.cuda.reset_peak_memory_stats()
    return torch.cuda.max_memory_allocated() / 1024**3
