# 架构说明

## 运行架构

```
Windows (E:\LocalOCR)                WSL2 Ubuntu 24.04
┌─────────────────┐                 ┌──────────────────────────┐
│ start.bat/ps1   │  拖入文件/参数   │ run_in_wsl.sh            │
│ (Windows入口)   │ ──────────────▶ │ (设 LD_LIBRARY_PATH 等)  │
└─────────────────┘                 │   ↓                      │
                                    │ venv python -m localocr   │
                                    │   cli.py                 │
                                    │   ├─ gpu_probe.py        │
                                    │   ├─ router.py           │
                                    │   ├─ service.py          │
                                    │   ├─ server.py           │
                                    │   ├─ engines/            │
                                    │   │   ├─ ppocrv6.py      │
                                    │   │   └─ vl.py           │
                                    │   ├─ pdf_utils.py        │
                                    │   └─ outputs.py          │
                                    │        ↓                 │
                                    │   PaddlePaddle GPU cu129 │
                                    │        ↓                 │
                                    │   /usr/lib/wsl/lib/      │
                                    │   libcuda.so.1 (驱动透传) │
                                    └──────────────────────────┘
                                              ↓
                                    ┌──────────────────────────┐
                                    │  RTX 5080/5090D (sm_120) │
                                    └──────────────────────────┘
```

## 模块职责

| 模块 | 职责 |
|---|---|
| `gpu_probe.py` | 启动时强制验证 GPU 可用（sm_120+、算子执行），失败即退出，不回退 CPU |
| `router.py` | 按扩展名分流：图片→ocr，PDF→vl；`--engine` 可覆盖 |
| `service.py` | 常驻 OCR 运行时，缓存 PP-OCRv6；VL/PDF 使用隔离子进程并串行化推理调用 |
| `server.py` | FastAPI 本地 API，提供 `/health`、`/ocr/path`、`/ocr/file` |
| `engines/ppocrv6.py` | PP-OCRv6_medium（det+rec），方向检测/矫正/文本行旋转全开 |
| `engines/vl.py` | PaddleOCR-VL-1.6，native 后端本地推理 |
| `pdf_utils.py` | PDF→PNG（pypdfium2），供逐页送引擎 |
| `outputs.py` | 统一产出 TXT/Markdown/JSON，保留坐标/置信度/表格/阅读顺序 |
| `cli.py` | argparse 入口，编排探针→收集→路由→识别→输出 |

## 关键环境变量

| 变量 | 作用 |
|---|---|
| `LD_LIBRARY_PATH=/usr/lib/wsl/lib` | 让 Paddle 找到 WSL 透传的 libcuda.so |
| `PADDLE_PDX_MODEL_SOURCE=modelscope` | 国内用 ModelScope 下载模型（HuggingFace 不可达）|
| `PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=true` | 跳过 HuggingFace 连通性检查（否则全部判失败）|
| `PADDLE_PDX_DISABLE_DEV_MODEL_WL=true` | 跳过设备-模型白名单检查 |
| `PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT=0` | 关闭 oneDNN（GPU 模式不需要）|

均在 `scripts/run_in_wsl.sh` 中设置。

## 本地 API

`start_server.ps1` 通过 Windows `Start-Process` 启动隐藏的 `wsl.exe` 会话，并在
其中以前台进程运行 `python -m localocr.server`，默认只监听 `127.0.0.1:8765`。
服务启动时执行 GPU 探针；PP-OCR 图片请求在 API 进程内加载并复用模型实例。
PaddleOCR-VL/PDF/复杂版面请求通过隔离子进程执行，避免 VL 超大模型与 Uvicorn
生命周期、信号处理或显存释放互相影响。Windows 侧启动进程 PID 记录在
`_server/wsl-server.pid`，`stop_server.ps1` 停止 WSL 内服务后会清理该文件。

| 端点 | 作用 |
|---|---|
| `GET /health` | 返回 GPU 摘要和已加载引擎 |
| `POST /ocr/path` | 识别 Windows/WSL 路径，支持文件或文件夹 |
| `POST /ocr/file` | 上传单个文件并识别 |
