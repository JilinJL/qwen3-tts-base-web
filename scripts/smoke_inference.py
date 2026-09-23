"""Explicit real-device acceptance test; never fetches a model or changes system packages."""

import argparse
import gc
import json
import time
from dataclasses import replace
from pathlib import Path

import numpy as np
import soundfile as sf
import torch

from qwen3_tts_web.config import load_settings
from qwen3_tts_web.runtime import free, load


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", required=True, type=Path)
    parser.add_argument("--reference-text", required=True)
    parser.add_argument("--text", default="Hello, this is a local voice cloning test.")
    parser.add_argument("--language", default="English")
    args = parser.parse_args()
    settings = replace(load_settings(), offline=True)
    output = settings.root / "output" / "hardware-smoke"
    output.mkdir(parents=True, exist_ok=True)
    model = load(settings)
    device = str(model.device)
    report = {
        "device": device,
        "model": settings.model,
        "torch": torch.__version__,
        "cases": [],
    }
    try:
        for x_only in (True, False):
            start = time.monotonic()
            kwargs = {"ref_audio": str(args.reference), "x_vector_only_mode": x_only}
            if not x_only:
                kwargs["ref_text"] = args.reference_text
            prompt = model.create_voice_clone_prompt(**kwargs)
            prompt_path = output / f"prompt-{x_only}.pt"
            torch.save(prompt, prompt_path)
            prompt = torch.load(prompt_path, map_location="cpu", weights_only=False)
            wavs, sr = model.generate_voice_clone(
                text=args.text,
                language=args.language,
                voice_clone_prompt=prompt,
                max_new_tokens=256,
            )
            wav = np.asarray(wavs[0])
            assert (
                wav.size > 0 and np.isfinite(wav).all() and np.max(np.abs(wav)) > 1e-5
            )
            sf.write(output / f"clone-{x_only}.wav", wav, sr)
            report["cases"].append(
                {
                    "x_vector_only": x_only,
                    "samples": wav.size,
                    "sample_rate": sr,
                    "seconds": time.monotonic() - start,
                }
            )
            del prompt
    finally:
        del model
        gc.collect()
        free()
        if device.startswith("mps"):
            report["allocated_after_free"] = torch.mps.current_allocated_memory()
        else:
            report["allocated_after_free"] = torch.cuda.memory_allocated(device)
    (output / "report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
