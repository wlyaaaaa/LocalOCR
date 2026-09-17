#!/usr/bin/env bash
# Install an isolated candidate; never overwrite or automatically activate production.
set -euo pipefail
PROJECT=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
NAME=${1:-paddle-3.4.0-$(date +%Y%m%d-%H%M%S)}
[[ "$NAME" =~ ^[a-zA-Z0-9._-]+$ && "$NAME" != current && "$NAME" != previous ]] || { echo "Invalid candidate name" >&2; exit 2; }
CANDIDATE=/root/localocr-runtimes/$NAME
[[ ! -e "$CANDIDATE" && ! -L "$CANDIDATE" ]] || { echo "Candidate already exists; inspect instead of overwriting" >&2; exit 2; }
export PIP_CACHE_DIR=${PIP_CACHE_DIR:-/mnt/e/Downloads/localocr-pip-cache}
mkdir -p /root/localocr-runtimes "$PIP_CACHE_DIR"
python3.12 -m venv "$CANDIDATE"
"$CANDIDATE/bin/python" -m pip install --timeout 90 --retries 3 --index-url https://pypi.org/simple --extra-index-url https://www.paddlepaddle.org.cn/packages/stable/cu129/ -r "$PROJECT/requirements/runtime-paddle-cu129.lock.txt"
"$CANDIDATE/bin/python" -m pip install --no-deps --no-build-isolation -e "$PROJECT"
"$CANDIDATE/bin/python" -m pip check
"$CANDIDATE/bin/python" "$PROJECT/scripts/manage_runtime.py" freeze
printf 'Candidate installed, not activated: %s\n' "$CANDIDATE"
printf 'Validate: LOCALOCR_RUNTIME=%q %q scripts/manage_runtime.py validate --allow-heavy\n' "$CANDIDATE" "$PROJECT/scripts/run_in_wsl.sh"
printf 'Activate only after passing: LOCALOCR_RUNTIME=%q %q scripts/manage_runtime.py activate\n' "$CANDIDATE" "$PROJECT/scripts/run_in_wsl.sh"