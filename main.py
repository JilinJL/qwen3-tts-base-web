"""Compatibility entry point after installing the project."""

from qwen3_tts_web.server import app as app

if __name__ == "__main__":
    from qwen3_tts_web.cli import main

    raise SystemExit(main(["serve"]))
