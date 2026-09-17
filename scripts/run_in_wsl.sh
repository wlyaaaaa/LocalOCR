#!/usr/bin/env bash
set -euo pipefail
export LD_LIBRARY_PATH=/usr/lib/wsl/lib:${LD_LIBRARY_PATH:-}
export PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT=0
export PADDLE_PDX_DISABLE_DEV_MODEL_WL=true
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=true
export PADDLE_PDX_MODEL_SOURCE=modelscope
PROJECT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
if [[ -n "${LOCALOCR_RUNTIME:-}" ]]; then
    RUNTIME=$LOCALOCR_RUNTIME
elif [[ -L /root/localocr-runtimes/current || -e /root/localocr-runtimes/current ]]; then
    RUNTIME=/root/localocr-runtimes/current
else
    RUNTIME=/root/localocr-venv
fi
[[ -x "$RUNTIME/bin/python" ]] || { echo "LocalOCR runtime is missing or broken: $RUNTIME" >&2; exit 2; }
export LOCALOCR_PROJECT_ROOT="$PROJECT"
if [[ -d "$RUNTIME/app/localocr" ]]; then
    APP=$(cd "$RUNTIME/app" && pwd)
    export PYTHONPATH="$APP${PYTHONPATH:+:$PYTHONPATH}"
    if [[ $# -gt 0 && "$1" != -* && "$1" != /* && -f "$APP/$1" ]]; then
        FIRST=$1; shift; set -- "$APP/$FIRST" "$@"
    fi
    cd "$PROJECT"
    exec "$RUNTIME/bin/python" -P "$@"
fi
cd "$PROJECT"
exec "$RUNTIME/bin/python" "$@"