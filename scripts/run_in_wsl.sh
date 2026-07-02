#!/usr/bin/env bash
# 在 WSL 内用项目 venv 执行 Python，自动设置 Paddle 所需的 LD_LIBRARY_PATH。
set -e
export LD_LIBRARY_PATH=/usr/lib/wsl/lib:${LD_LIBRARY_PATH:-}
export PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT=0
export PADDLE_PDX_DISABLE_DEV_MODEL_WL=true
# 国内环境：HuggingFace 不可达，优先用 ModelScope/AIStudio/BOS，跳过连通性检查。
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=true
export PADDLE_PDX_MODEL_SOURCE=modelscope
cd /mnt/e/LocalOCR
exec /root/localocr-venv/bin/python "$@"
