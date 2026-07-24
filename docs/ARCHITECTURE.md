# 架构说明

## 运行架构

```
Windows (E:\Projects\Tools\LocalOCR)                WSL2 Ubuntu 24.04
┌─────────────────┐                 ┌──────────────────────────┐
│ start.bat/ps1   │  拖入文件/参数   │ run_in_wsl.sh            │
│ (Windows入口)   │ ──────────────▶ │ (设 LD_LIBRARY_PATH 等)  │
└─────────────────┘                 │   ↓                      │
                                    │ venv python -m localocr   │
                                    │   cli.py                 │
                                    │   ├─ gpu_probe.py        │
                                    │   ├─ model_registry.py   │
                                    │   ├─ model_profiles.json │
                                    │   ├─ router.py           │
                                    │   ├─ smart_router.py     │
                                    │   ├─ service.py          │
                                    │   ├─ server.py           │
                                    │   ├─ job_registry.py     │
                                    │   ├─ engines/            │
                                    │   │   ├─ ppocrv6.py      │
                                    │   │   ├─ vl.py           │
                                    │   │   └─ structure.py    │
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
| `model_profiles.json` | 声明 profile id、默认模型、engine 族、adapter、能力标签和 Paddle 初始化参数 |
| `model_registry.py` | 读取 profile，解析 `ocr/vl/structure` 默认别名，按 `--model` 创建具体 adapter |
| `router.py` | 文件扩展名判断和输入文件收集基础工具 |
| `smart_router.py` | Smart Router v2；在不加载模型的前提下，用扩展名、文件名关键词和显式参数生成可解释 `auto` 路由 |
| `job_registry.py` | 文件型任务登记、缓存命中、运行中去重和 `job_key` 状态 manifest |
| `service.py` | 常驻 OCR 运行时，按具体 profile 缓存轻量模型；VL/Structure 重模型使用隔离子进程；写盘任务先经过 Smart Router v2 和 job registry |
| `server.py` | FastAPI 本地 API，提供 `/health`、`/jobs/{job_key}`、`/ocr/path`、`/ocr/file`，请求体支持 `model` |
| `engines/ppocrv6.py` | PP-OCRv6 adapter，接收 profile 注入的模型名、pipeline 和初始化参数 |
| `engines/vl.py` | PaddleOCR-VL adapter，接收 profile 注入的模型名、pipeline 和初始化参数 |
| `engines/structure.py` | PP-StructureV3 adapter，接收 profile 注入的结构化管线参数并归一成统一 blocks 输出 |
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
其中以前台进程运行 `python -m localocr.server`，默认只监听 `127.0.0.1:18665`。
服务启动时执行 GPU 探针；PP-OCR 图片请求在 API 进程内加载并复用模型实例。
PaddleOCR-VL 和 PP-StructureV3 请求通过隔离子进程执行，避免重模型与 Uvicorn
生命周期、信号处理或显存释放互相影响。Windows 侧启动进程 PID 记录在
`_server/wsl-server.pid`，`stop_server.ps1` 停止 WSL 内服务后会清理该文件。
`loaded_engines` 保留兼容字段，返回已缓存 profile 的 engine 族；`loaded_models`
返回具体 profile id，供换模型和验收时确认。

`engine=auto` 先经过 Smart Router v2。显式 `engine` 和 `model` 永远优先；`structure`
不参与自动路由。普通图片和普通扫描 PDF / 表单默认走 OCR；文件名含 `table`、`formula`、
`layout`、`multi`、`论文`、`公式`、`表格`、`多栏`、`课件` 等复杂版面信号时走 VL。
API 响应的每个 `results[]` 都包含 `route`，记录 `effective_engine`、`reason`、
`signals`、`confidence` 和 `model_id`，用于排障和缓存审计。

写盘 OCR 请求在推理前会登记到 `_server/jobs/<job_key>.json`，并用同名 `.lock`
做原子 claim。`job_key` 由源文件路径、文件内容 hash、模型 profile、engine 和输出目录决定。
同一任务完成且输出文件仍存在时返回 `cache_status=cache_hit`；同一任务仍在运行时返回
`status=active_localocr_task` 和 `recommendation=do_not_blindly_retry`，避免客户端超时后再次拉起相同 OCR。

资源释放入口有两层：`ocr_once.ps1 -StopAfter` 适合一次性 OCR 后立即关停；
`release_resources.ps1` 适合 Ollama、本地大模型、游戏或其他重 GPU 工作负载启动前
统一释放 LocalOCR API 与派生 VL 子进程。

| 端点 | 作用 |
|---|---|
| `GET /health` | 返回 GPU 摘要和已加载引擎 |
| `GET /jobs/{job_key}` | 返回 job manifest、运行状态和缓存可用性 |
| `POST /ocr/path` | 识别 Windows/WSL 路径，支持文件或文件夹 |
| `POST /ocr/file` | 上传单个文件并识别 |
