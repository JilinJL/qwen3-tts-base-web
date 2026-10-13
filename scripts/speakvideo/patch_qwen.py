"""Add memory-only reference cloning / voice design to the existing Qwen API.

Run with the Windows inference Python, then restart Qwen. Keeps a backup and
rejects unknown source layouts. No model, configuration or existing audio is removed.
"""

import argparse
import ast
import hashlib
from pathlib import Path


def patch(source: str) -> str:
    if "# SpeakVideo memory reference v1" in source:
        return source
    design = "    allow_download: bool = False\n"
    prompt = "    ref_audio: str\n"
    check = "    if not os.path.exists(req.ref_audio):\n"
    kwargs = '    kwargs = {"ref_audio": req.ref_audio}\n'
    save = "def save_generation(req, wavs, sr, generated):\n"
    for token in (design, prompt, check, kwargs, save):
        if source.count(token) != 1:
            raise ValueError("Qwen source layout changed; review the patch before applying")
    source = source.replace(design, design + "    persist: bool = True\n")
    source = source.replace(prompt, '    ref_audio: str = ""\n    ref_audio_base64: str = ""\n')
    source = source.replace(
        check, "    if not req.ref_audio_base64 and not os.path.exists(req.ref_audio):\n"
    )
    source = source.replace(
        kwargs,
        """    # SpeakVideo memory reference v1
    if req.ref_audio_base64:
        import base64
        import soundfile as sf

        try:
            raw = base64.b64decode(req.ref_audio_base64, validate=True)
            if not raw or len(raw) > 32 * 1024 * 1024:
                raise ValueError("reference exceeds 32 MiB")
            audio, sample_rate = sf.read(io.BytesIO(raw), dtype="float32")
            if audio.ndim == 2:
                audio = audio.mean(axis=1)
            if not len(audio) or len(audio) / sample_rate > 120:
                raise ValueError("reference must contain 0..120 seconds of audio")
            reference = (audio, sample_rate)
        except Exception as exc:
            raise HTTPException(400, detail=f"invalid memory reference: {exc}")
    else:
        reference = req.ref_audio
    kwargs = {"ref_audio": reference}
""",
    )
    source = source.replace(
        save,
        save
        + """    if not getattr(req, "persist", True):
        import numpy as np
        import soundfile as sf

        buffer = io.BytesIO()
        sf.write(buffer, np.asarray(wavs[0]), sr, format="WAV")
        return Response(content=buffer.getvalue(), media_type="audio/wav",
                        headers={"X-Audio-Sample-Rate": str(sr)})
""",
    )
    ast.parse(source)
    return source


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    path = parser.parse_args().source
    original = path.read_bytes()
    text = original.decode("utf-8").replace("\r\n", "\n")
    modified = patch(text)
    if modified == text:
        print("Qwen memory adapter already installed")
        return
    backup = path.with_name(
        path.name + ".speakvideo-" + hashlib.sha256(original).hexdigest()[:12] + ".bak"
    )
    if not backup.exists():
        backup.write_bytes(original)
    temporary = path.with_suffix(".speakvideo.tmp")
    temporary.write_text(modified, encoding="utf-8", newline="\n")
    temporary.replace(path)
    print("Qwen memory adapter installed; backup:", backup.name)


if __name__ == "__main__":
    main()
