"""Download complete snapshots; validate locally without importing torch."""

import json
import os
import struct
import sys
from dataclasses import replace
from pathlib import Path

from filelock import FileLock

MODELS = {
    "base": "Qwen3-TTS-12Hz-1.7B-Base",
    "base-small": "Qwen3-TTS-12Hz-0.6B-Base",
    "tokenizer": "Qwen3-TTS-Tokenizer-12Hz",
    "customvoice": "Qwen3-TTS-12Hz-1.7B-CustomVoice",
    "voicedesign": "Qwen3-TTS-12Hz-1.7B-VoiceDesign",
}


def model_path(settings, key=None):
    return settings.root / "models" / MODELS[key or settings.model_key]


def prepare_model(settings, log=print):
    """Keep downloader imports and online flags out of the inference process."""
    target = model_path(settings)
    if validate_model(target, settings.model_key) and not settings.offline:
        from .process import Runner

        Runner(log).run(
            [sys.executable, "-m", "qwen3_tts_web", "download", "--yes"],
            env=settings.environment(),
            cwd=settings.root,
        )
    return ensure_model(replace(settings, offline=True), log=log)


def _json(path):
    with path.open(encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict) or not value:
        raise ValueError("empty or invalid JSON object")
    return value


def _safetensors(path):
    with path.open("rb") as handle:
        length = struct.unpack("<Q", handle.read(8))[0]
        if not 0 < length <= 100_000_000:
            raise ValueError("invalid safetensors header")
        header = json.loads(handle.read(length))
    if not isinstance(header, dict):
        raise ValueError("invalid safetensors metadata")
    offsets = [v["data_offsets"] for k, v in header.items() if k != "__metadata__"]
    if not offsets or any(a < 0 or b <= a for a, b in offsets):
        raise ValueError("invalid tensor offsets")
    if path.stat().st_size != 8 + length + max(end for _, end in offsets):
        raise ValueError("truncated tensor data")


def validate_model(path, key="base"):
    path = Path(path)
    errors = []

    def check(relative, validator):
        try:
            validator(path / relative)
        except (OSError, ValueError, KeyError, TypeError, struct.error) as exc:
            errors.append(f"{relative}: {exc}")

    def weights(folder):
        index = folder / "model.safetensors.index.json"
        if (path / index).exists():
            try:
                mapping = _json(path / index)["weight_map"]
                if not isinstance(mapping, dict) or not mapping:
                    raise ValueError("empty weight map")
                names = set(mapping.values())
                for name in names:
                    target = (path / folder / name).resolve()
                    if not target.is_relative_to((path / folder).resolve()):
                        raise ValueError("weight path escapes model directory")
                    check(folder / name, _safetensors)
            except (OSError, ValueError, KeyError, TypeError) as exc:
                errors.append(f"{index}: {exc}")
        else:
            check(folder / "model.safetensors", _safetensors)

    for filename in ("config.json", "preprocessor_config.json"):
        check(Path(filename), _json)
    weights(Path("."))
    if key != "tokenizer":
        for filename in (
            "generation_config.json",
            "tokenizer_config.json",
            "vocab.json",
        ):
            check(Path(filename), _json)

        def nonempty(file):
            if file.stat().st_size == 0:
                raise ValueError("empty file")

        check(Path("merges.txt"), nonempty)
        for filename in ("config.json", "preprocessor_config.json"):
            check(Path("speech_tokenizer") / filename, _json)
        weights(Path("speech_tokenizer"))
    return errors


def _download(provider, repo, target, endpoint, repair=False):
    # SDKs read offline flags during import, so management and serving run separately.
    os.environ["HF_HUB_OFFLINE"] = "0"
    os.environ["TRANSFORMERS_OFFLINE"] = "0"
    os.environ.setdefault("TQDM_MININTERVAL", "1")
    if provider == "modelscope":
        from modelscope.hub.snapshot_download import snapshot_download

        snapshot_download(repo, local_dir=str(target))
    else:
        from huggingface_hub import snapshot_download

        snapshot_download(
            repo,
            local_dir=str(target),
            endpoint=endpoint,
            force_download=repair,
            max_workers=4,
        )


def ensure_model(settings, key=None, log=print):
    key = key or settings.model_key
    target = model_path(settings, key)
    errors = validate_model(target, key)
    if not errors:
        log(f"[OK] 模型完整: {target.name}")
        return target
    if settings.offline:
        raise RuntimeError("离线模式，模型缺失或不完整:\n" + "\n".join(errors))
    target.parent.mkdir(parents=True, exist_ok=True)
    with FileLock(str(target.parent / f".{target.name}.lock"), timeout=0):
        if not validate_model(target, key):
            return target
        endpoint = settings.hf_endpoint or (
            "https://hf-mirror.com"
            if settings.source == "domestic"
            else "https://huggingface.co"
        )
        providers = [("huggingface", endpoint)]
        if settings.source == "domestic":
            providers.insert(0, ("modelscope", ""))
            if endpoint != "https://huggingface.co":
                providers.append(("huggingface", "https://huggingface.co"))
        failures = []
        for provider, endpoint in providers:
            log(f"[下载] Qwen/{MODELS[key]} via {provider} {endpoint}")
            try:
                _download(provider, f"Qwen/{MODELS[key]}", target, endpoint)
                errors = validate_model(target, key)
                if errors and provider == "huggingface":
                    log("[修复] 文件校验失败，重新获取快照")
                    _download(
                        provider, f"Qwen/{MODELS[key]}", target, endpoint, repair=True
                    )
                    errors = validate_model(target, key)
                if errors:
                    raise RuntimeError("\n".join(errors))
                receipt = target / ".qwen3-complete.json"
                temp = receipt.with_suffix(".tmp")
                temp.write_text(
                    json.dumps({"model": MODELS[key], "provider": provider}),
                    encoding="utf-8",
                )
                temp.replace(receipt)
                log(f"[OK] 下载与完整性校验完成: {target.name}")
                return target
            except Exception as exc:
                failures.append(f"{provider}: {exc}")
                log(f"[失败] {failures[-1]}")
        raise RuntimeError("模型下载失败，可重跑命令续传:\n" + "\n".join(failures))
