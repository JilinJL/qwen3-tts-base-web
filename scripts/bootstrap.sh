#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
export QWEN3_TTS_ROOT="$ROOT"
export PYTHONUTF8=1
export PYTHONUNBUFFERED=1
cd -- "$ROOT"
YES=0
OFFLINE=0
case "${QWEN3_TTS_OFFLINE:-false}" in true|1) OFFLINE=1 ;; esac
INDEX="${QWEN3_TTS_PIP_INDEX:-https://pypi.tuna.tsinghua.edu.cn/simple}"
previous=""
for argument in "$@"; do
    case "$argument" in
        --yes) YES=1 ;;
        --offline) OFFLINE=1 ;;
        --no-offline) OFFLINE=0 ;;
        --help|-h)
            printf '%s\n' 'Usage: bash scripts/bootstrap.sh [tui|doctor|setup|download|serve] [options]' \
                'Options: --yes --device auto|mps|cuda:N --model 1.7B|0.6B --source domestic|official' \
                '         --offline --host ADDRESS --port PORT --install-system --skip-download' \
                '         --model-key base|base-small|tokenizer|customvoice|voicedesign --json'
            exit 0 ;;
    esac
    if [[ "$previous" == --source && "$argument" == official ]]; then INDEX="https://pypi.org/simple"; fi
    if [[ "$previous" == --pip-index ]]; then INDEX="$argument"; fi
    previous="$argument"
done
if [[ "${QWEN3_TTS_SOURCE:-}" == official && -z "${QWEN3_TTS_PIP_INDEX:-}" ]]; then INDEX="https://pypi.org/simple"; fi
confirm() {
    if [[ "$OFFLINE" == 1 ]]; then
        printf '%s\n' 'Offline: management tools are missing; bootstrap them online first.' >&2; exit 1
    fi
    if [[ "$YES" == 1 ]]; then return; fi
    if [[ ! -t 0 ]]; then printf '%s\n' "$1; use --yes for non-interactive installation." >&2; exit 1; fi
    read -r -p "$1 [y/N] " answer
    [[ "$answer" == y || "$answer" == Y || "$answer" == yes ]] || exit 1
}
PYTHON="$ROOT/.venv-tools/bin/python"
if [[ -x "$PYTHON" ]] && "$PYTHON" -c 'import sys, qwen3_tts_web, textual, uv; assert sys.version_info[:2] == (3,11)' >/dev/null 2>&1; then
    exec "$PYTHON" -m qwen3_tts_web "$@"
fi
confirm 'Install/repair local management tools and acquire Python 3.11 if missing?'
UV="$(command -v uv || true)"
if [[ -z "$UV" && -x "$HOME/.local/bin/uv" ]]; then UV="$HOME/.local/bin/uv"; fi
if [[ -z "$UV" ]]; then
    confirm 'Install uv from astral.sh into your user account (no PATH/profile changes)?'
    installer="$(mktemp)"
    trap 'rm -f "$installer"' EXIT
    curl --proto '=https' --tlsv1.2 --fail --location --max-time 120 https://astral.sh/uv/install.sh -o "$installer"
    UV_NO_MODIFY_PATH=1 sh "$installer"
    rm -f "$installer"
    trap - EXIT
    UV="$HOME/.local/bin/uv"
fi
if [[ -d "$ROOT/.venv-tools" ]]; then
    if [[ ! -x "$PYTHON" ]] || ! "$PYTHON" -c 'import sys; assert sys.version_info[:2] == (3,11)'; then
        printf '%s\n' 'Existing .venv-tools is incompatible. Rename it as a backup; it will not be deleted.' >&2
        exit 1
    fi
else
    "$UV" venv --python 3.11 "$ROOT/.venv-tools"
fi
if ! "$UV" --no-config pip install --python "$PYTHON" --index-url "$INDEX" --editable "$ROOT"; then
    "$UV" --no-config pip install --python "$PYTHON" --index-url https://pypi.org/simple --editable "$ROOT"
fi
exec "$PYTHON" -m qwen3_tts_web "$@"
