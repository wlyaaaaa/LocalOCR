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
| `smart_router.py` | Smart Router v3 的低成本预路由；在不加载模型的前提下，用扩展名、文件名关键词和显式参数生成可解释 `auto` 首轮路由 |
| `difficulty.py` | 对 auto 首轮 PP-OCRv6 结果计算空文本、均值和低置信块占比；达到保守阈值时请求本地 VL 二次识别 |
| `job_registry.py` | 文件型任务登记、缓存命中、运行中去重和 `job_key` 状态 manifest |
| `service.py` | 常驻 OCR 运行时，按具体 profile 缓存轻量模型；VL/Structure 重模型使用隔离子进程；写盘任务先经过 Smart Router v3 和 job registry |
| `server.py` | FastAPI 本地 API，提供 `/health`、`/jobs/{job_key}`、`/ocr/path`、`/ocr/file`，请求体支持 `model` |
| `engines/ppocrv6.py` | PP-OCRv6 adapter，接收 profile 注入的模型名、pipeline 和初始化参数 |
| `engines/vl.py` | PaddleOCR-VL adapter，接收 profile 注入的模型名、pipeline 和初始化参数 |
| `engines/structure.py` | PP-StructureV3 adapter，接收 profile 注入的结构化管线参数并归一成统一 blocks 输出 |
| `pdf_utils.py` | PDF→PNG（pypdfium2），供逐页送引擎 |
| `outputs.py` | 统一产出兼容 TXT/Markdown/JSON 投影，保留坐标/置信度/表格/阅读顺序 |
| `objective_result.py` | 产出 `media.objective-result.v1` 客观结果、独立负向证据和按请求 hash 隔离的 sidecar；cache hit 重验 schema/size/hash/身份 |
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
API 父进程持有 LocalGpuBroker 租约并覆盖隔离子进程的完整生命周期；子进程收到内部
`--broker-lease-held-by-parent` 标记时不重复申请租约。直接 CLI 不带该标记，仍必须自行申请 Broker。
`loaded_engines` 保留兼容字段，返回已缓存 profile 的 engine 族；`loaded_models`
返回具体 profile id，供换模型和验收时确认。

`engine=auto` 先经过 Smart Router v3。显式 `engine` 和 `model` 永远优先；`structure`
不参与自动路由。普通图片和普通扫描 PDF / 表单先走 OCR；空文本或明显低置信结果在同一任务中升级到隔离 VL。文件名含 `table`、`formula`、
`layout`、`multi`、`论文`、`公式`、`表格`、`多栏`、`课件` 等复杂版面信号时走 VL。
API 响应的每个 `results[]` 都包含 `route`，记录 `effective_engine`、`reason`、
`signals`、`confidence` 和 `model_id`；auto 首轮 OCR 还记录 `difficulty`、`initial_engine`、
`escalated` 与必要时的 `escalation`，用于排障和缓存审计。

坐标和结构输出采用加法式契约：旧 `bbox` 保留，所有新 block 增加 `rect`、`polygon` 和
`coordinate_space=image_pixels`。Structure 页面保留 JSON-native `structure_details`，并把
`overall_ocr_res` 逐行结果放在独立 `text_lines`，不与版面 blocks 混合；Structure/VL 的
`face/person/human/portrait/figure/image` 标签进入 `excluded_regions`，不计入正文 OCR 文字。PDF 页面由 service 标注
`rendered_pdf_pixels=true`、`render_scale=2.0` 和渲染宽高，明确坐标仍是渲染图像像素。

每个完成结果还包含正交的客观结果字段：`objective_outcome` 为
`text_detected`、`no_text_detected` 或 `indeterminate`；`execution.status` 单独表示
`completed`、`failed`、`unsupported` 或 `corrupt`；`coverage.status` 表示完整、部分或未知覆盖；
`quality.status` 表示 `sufficient`、`low_confidence` 或 `unknown`。空 block、空文本或零字节不能证明
`no_text_detected`。规范负向证据必须是非空 canonical artifact，并绑定 raw hash、processor/model/version、
config/request hash、实际页/区域、排除范围、阈值和不确定性。PDF 的 `media_kind` 仍为 `image`，容器类型另记为
`source_format=pdf`。内存结果的 `evidence.verification_status` 为 `not_persisted`；只有写入 sidecar 后才提升为 `verified`。
旧 `<stem>.txt|md|json` 仅作为展示兼容投影；写盘任务同时生成带 request hash 的 canonical 投影和
`*.objective.json` sidecar，后者与 canonical 投影一起参与 cache identity 校验。

写盘 OCR 请求在推理前会登记到 `_server/jobs/<job_key>.json`，并用同名 `.lock`
做原子 claim。`job_key` 由源文件路径、文件内容 hash、请求语义、路由策略、模型 profile、
engine 和输出目录决定；因此 auto、显式 engine 与显式 model 不会错误复用彼此的结果。
同一任务完成且所有输出文件均有非空 `size_bytes`/`sha256`，且 objective sidecar 通过 schema、尺寸、hash、source/request/model/config identity
重验时返回 `cache_status=cache_hit`；同一任务仍在运行时返回
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
