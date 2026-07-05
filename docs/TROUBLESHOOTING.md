# 故障排除

## 1. GPU 探针失败 / libcuda.so 找不到

**现象**：`The third-party dynamic library (libcuda.so) is not configured correctly`

**原因**：WSL 的 libcuda 在 `/usr/lib/wsl/lib/`，不在默认搜索路径。

**解决**：确认 `scripts/run_in_wsl.sh` 里有 `export LD_LIBRARY_PATH=/usr/lib/wsl/lib:$LD_LIBRARY_PATH`。
手动验证：
```bash
wsl -d Ubuntu -e bash -c "export LD_LIBRARY_PATH=/usr/lib/wsl/lib:\$LD_LIBRARY_PATH; python -c 'import paddle; print(paddle.device.cuda.get_device_capability(0))'"
```
应输出 `(12, 0)`。

## 2. 模型下载失败 / No available model hosting platforms

**原因**：默认走 HuggingFace，国内不可达，连带把所有源判失败。

**解决**：设置环境变量（已在 run_in_wsl.sh 配好）：
```bash
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=true
export PADDLE_PDX_MODEL_SOURCE=modelscope
```

## 3. VL 报 DependencyError: requires additional dependencies

**解决**：装 VL 依赖（已含在 install_wsl.sh）：
```bash
pip install beautifulsoup4 einops ftfy Jinja2 latex2mathml lxml openpyxl \
    premailer regex safetensors scikit-learn scipy sentencepiece tiktoken tokenizers
```
验证：`python -c "from paddlex.utils.deps import is_extra_available; print(is_extra_available('ocr'))"`
应输出 `True`。

## 4. 显存不足

5080 16GB 跑 VL 公式类长文档时显存可能紧张（峰值约 15.8GB）。
5090D 32GB 无此问题。临时缓解：减小图片分辨率或分页处理。

## 5. Windows 原生 Paddle GPU 在 Blackwell 上不可用

这是已知限制：Paddle 官方 Windows GPU wheel 仅 cu118/cu126，不含 sm_120 cubin。
**必须用 WSL2 + Linux cu129 wheel**。本项目已采用此方案。

## 6. PaddleOCR 报 oneDNN / PIR 错误

设置 `export PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT=0`（已在 run_in_wsl.sh 配好）。

## 7. pip 装包超时

清华/阿里云镜像偶有波动。install_wsl.sh 用阿里云 + `--retries 5 --timeout 90`。
可手动换源重试。

## 8. API 服务启动后健康检查超时

**现象**：`start_server.ps1` 等待 `/health` 超时。

**排查**：

```powershell
wsl -d Ubuntu -e bash -lc "ps aux | grep localocr.server | grep -v grep"
wsl -d Ubuntu -e bash -lc "cd /mnt/e/LocalOCR && scripts/run_in_wsl.sh -m localocr.server"
```

常见原因：

- venv 中缺 `fastapi` / `uvicorn` / `python-multipart`，重新运行 `scripts/install_wsl.sh` 或手动安装依赖。
- GPU 探针失败，按本文第 1 节检查 WSL libcuda。
- 端口 `8765` 被占用，使用 `.\start_server.ps1 -Port 8766`。
- 首次冷启动超过启动等待时间。`ocr_once.ps1 -TimeoutSec` 只控制 OCR 请求等待，不控制 API 服务启动；冷启动慢但重试成功时，改用 `-StartupTimeoutSec 900` 或先单独运行 `.\start_server.ps1 -StartupTimeoutSec 900`。`start_server.ps1` 会用启动锁和 `wsl-server.pid` 识别正在启动的 API 进程，重试时等待现有进程变为 ready，而不是再启动第二个服务。

停止残留服务：

```powershell
E:\LocalOCR\stop_server.ps1
```

## 9. `ocr_once.ps1` 长任务请求超时

**现象**：VL、扫描 PDF 或公式图片首次识别时报
`The request was aborted: The operation has timed out.`

**原因**：VL 冷启动会加载模型权重，复杂版面推理也可能较久；客户端等待时间必须大于模型加载和推理时间。

**解决**：

```powershell
E:\LocalOCR\ocr_once.ps1 "E:\path\scan.pdf" -Engine vl -TimeoutSec 3600
```

当前脚本默认 `TimeoutSec=3600`。如果仍超时，先看服务是否还活着：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
Get-Content E:\LocalOCR\_server\localocr-api.log -Tail 80
```

`/health` 的 `loaded_engines` 不一定出现 `vl`：VL/PDF 由隔离子进程执行，不常驻在
API 进程中。判断 VL 是否可用应以一次 `-Engine vl` 调用是否成功为准。

## 10. 启动 Ollama / 本地大模型前释放显存

LocalOCR API 可能常驻 PP-OCR 模型。启动 Ollama、本地大模型或其他重 GPU 任务前：

```powershell
E:\LocalOCR\release_resources.ps1
```

如果是一次性 OCR，也可以直接：

```powershell
E:\LocalOCR\ocr_once.ps1 "E:\path\image.png" -StopAfter
```
