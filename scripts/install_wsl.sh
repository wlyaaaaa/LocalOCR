#!/usr/bin/env bash
# LocalOCR 一键安装脚本（在 WSL2 Ubuntu 内运行）。
# 用法（在 Windows PowerShell）：wsl -d Ubuntu -e bash /mnt/e/LocalOCR/scripts/install_wsl.sh
set -e

VENV=/root/localocr-venv
PROJ=/mnt/e/LocalOCR

echo "==== [1/5] 系统依赖 ===="
apt-get update -qq
apt-get install -y -qq python3.12-venv python3-pip curl

echo "==== [2/5] 创建 venv ===="
if [ ! -d "$VENV" ]; then
    python3 -m venv "$VENV"
fi
"$VENV/bin/python" -m pip install -q --upgrade pip setuptools wheel

echo "==== [3/5] 安装 PaddlePaddle GPU (cu129, 支持 Blackwell sm_120) ===="
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
    "$VENV/bin/python" -m pip install -i https://www.paddlepaddle.org.cn/packages/stable/cu129/ \
    "paddlepaddle-gpu==3.3.1"

echo "==== [4/5] 安装 PaddleOCR 3.7.0 + VL 依赖 ===="
PYPI=https://mirrors.aliyun.com/pypi/simple/
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
    "$VENV/bin/python" -m pip install -i $PYPI --timeout 90 --retries 5 \
    "paddleocr==3.7.0"
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
    "$VENV/bin/python" -m pip install -i $PYPI --timeout 90 --retries 5 \
    beautifulsoup4 einops ftfy Jinja2 latex2mathml lxml openpyxl premailer regex \
    safetensors scikit-learn scipy sentencepiece tiktoken tokenizers
env -u http_proxy -u https_proxy -u HTTP_PROXY -u HTTPS_PROXY \
    "$VENV/bin/python" -m pip install -i $PYPI --timeout 90 --retries 5 \
    "fastapi>=0.116" "uvicorn[standard]>=0.35" "python-multipart>=0.0.20"

echo "==== [5/5] 预下载所有模型到本地（ModelScope，离线可用） ===="
export LD_LIBRARY_PATH=/usr/lib/wsl/lib:${LD_LIBRARY_PATH:-}
export PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT=0
export PADDLE_PDX_DISABLE_DEV_MODEL_WL=true
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=true
export PADDLE_PDX_MODEL_SOURCE=modelscope
cd "$PROJ"
"$VENV/bin/python" scripts/download_models.py

echo ""
echo "==== 安装完成 ===="
echo "GPU 探针："
"$VENV/bin/python" -c "from localocr.gpu_probe import probe_gpu, format_probe; print(format_probe(probe_gpu()))" 2>&1 | tail -3 || true
echo ""
echo "用法：在 Windows 里把文件拖到 start.bat 上，或："
echo "  wsl -d Ubuntu -e bash /mnt/e/LocalOCR/scripts/run_in_wsl.sh -m localocr.cli \"路径\""
