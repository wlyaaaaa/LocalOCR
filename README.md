# LocalOCR

本地高质量中文 OCR 系统，基于 **PaddleOCR 3.7.0** + **PaddlePaddle GPU 3.3.1 (CUDA 12.9)**，
面向 **RTX 5090D（Blackwell sm_120）** + WSL2 Ubuntu 24.04。

## 特性

- **中文优先**：默认 PP-OCRv6_medium 检测+识别，保留方向检测和文本行旋转纠正；普通截图/平面扫描默认不做 UVDoc 形变矫正，避免把原本清晰的文字和坐标拉坏。
- **复杂文档用 VL**：论文、表格、公式、多栏排版等复杂 PDF/图片可自动或显式走 **PaddleOCR-VL-1.6**。
- **结构化高配可选**：表格、版面块、公式、印章、区域检测可显式走 **PP-StructureV3 + PP-OCRv5**（`-Engine structure` / `--engine structure`）。
- **Smart Router v3 自动分流**：图片和普通扫描 PDF / 表单先走 PP-OCRv6_medium；空文本或明显低置信结果自动升级到本地 PaddleOCR-VL-1.6；复杂文件名信号仍可直接进入 VL。每次结果返回 `route.reason` / `route.signals` / `route.confidence`，自动首轮 OCR 还返回 `route.difficulty` / `route.escalated`。
- **客观结果与空文本语义**：每个完成结果增加 `objective_outcome=text_detected|no_text_detected|indeterminate`、`execution_status`、`coverage`、`quality` 和 `failure`。模型返回空 block/空文本不会被当成“确实无文字”；只有完整覆盖、无排除范围且独立像素检测或 adapter telemetry 生成的规范负向证据才会是 `no_text_detected`。内存结果的 `evidence.verification_status` 保持 `not_persisted`，写入 sidecar 后才为 `verified`。规范 `media.objective-result.v1` sidecar 按请求 hash 隔离并在 cache hit 时校验 schema、尺寸、哈希和输入/模型身份。
- **GPU 加速**：强制 GPU 探针，Blackwell sm_120 原生支持，不静默回退 CPU。
- **离线运行**：所有模型预下载到本地，断网可用。
- **模型 profile 解耦**：`localocr/model_profiles.json` 声明默认模型、能力标签和 adapter；`--model` / `-Model` 可指定具体 profile。
- **多格式输出**：TXT / Markdown / JSON，保留文字坐标、置信度、表格、阅读顺序。
  JSON 在保留旧 `bbox` 的同时增加 `rect` / `polygon` / `coordinate_space=image_pixels`；Structure 结果另保留
  JSON-native `structure_details`、独立 `text_lines` 和非文字区域 `excluded_regions`。
- **拖拽即用**：把图片、文件夹或 PDF 拖到 `start.bat` 上即可自动识别。
- **可恢复的本地 API**：API 不加载 Paddle；所有模型由一个受监督的工作进程运行。同模型热复用，换模型重建；执行期限、取消、租约丢失和服务退出都会结束工作进程树。
- **任务级缓存/去重**：API 会按源文件、请求语义、路由策略、模型 profile 和输出目录生成 `job_key`；相同任务完成后返回 `cache_status=cache_hit`，运行中重复提交会返回 `status=active_localocr_task` 而不是再启动一个 OCR。
- **Codex 防卡入口**：`ocr_smart.ps1` 以 `/health.active_jobs` 判断忙碌，保留 HTTP 错误正文与任务定位；不再以进程名探测代替任务状态。默认整个请求执行期限 300 秒，默认调用端等待 330 秒。

## 环境

| 项 | 值 |
|---|---|
| OS（运行） | WSL2 Ubuntu 24.04 LTS |
| GPU | RTX 5080 / 5090D（Blackwell，sm_120，CUDA 12.9 原生） |
| PaddlePaddle | 3.3.1 GPU，cu129 构建（wheel 自带 CUDA/cuDNN/NCCL） |
| PaddleOCR | 3.7.0 |
| Python | 3.12（WSL venv） |

> 当前支持并实机验证的是 Linux cu129 wheel，包含 sm_120；不与 Windows 或 CPU wheel 混装。
> 详见 [当前架构](docs/ARCHITECTURE.md)。

## AI / Codex 默认入口

给 AI 助手调用时，默认先用 bounded smart wrapper，不要直接拉长时间阻塞 PowerShell：

```powershell
.\ocr_smart.ps1 "E:\path\file-or-folder" -Engine auto -ExecutionTimeoutSec 300
```

默认决策：

- 普通图片、截图、普通扫描 PDF、法律表单、空白表格、送达地址确认书：用 `-Engine auto`，由 Smart Router v3 先走 OCR；空文本或明显低置信时自动在同一任务内升级到本地 VL。
- 复杂表格、公式、多栏、论文、课件、整页复杂版面：显式 `-Engine vl`，或让带复杂文件名信号的 PDF 由 `auto` 路由到 VL。
- 需要表格 HTML、版面块、公式、印章、区域检测、坐标：显式 `-Engine structure`。
- 需要指定或替换具体模型：用 `-Model <profile-id>` / `--model <profile-id>`，并先改 `localocr/model_profiles.json`，不要把模型名硬编码进 wrapper 或服务层。
- 遇到 `status=active_localocr_task`、`status=client_timeout`、`job_key`、`cache_status=cache_hit` 时，先查 `/jobs/<job_key>`、输出目录和后台任务，不要盲目重复提交同一文件。

## 最小验收

文档、wrapper、路由或模型 profile 改动后，优先跑轻量验收；不要把真实 OCR + `-StopAfter` 当成默认 smoke。

```powershell
# PowerShell / Markdown 格式检查
git diff --check

# 不加载真实模型的路由和 Windows wrapper 回归
wsl -d Ubuntu -e bash -lc "cd /mnt/e/Projects/Tools/LocalOCR && scripts/run_in_wsl.sh -m unittest tests.test_smart_router tests.test_windows_wrappers"

# 改过 model_profiles.json 或 adapter 后，再重启 API 做一个小图 smoke
.\stop_server.ps1
.\ocr_smart.ps1 "E:\Projects\Tools\LocalOCR\tests\samples\probe_text.png" -Engine auto -ExecutionTimeoutSec 300 -OuterTimeoutSec 330
```

常规验收不要加 `-StopAfter`；它会释放常驻服务并让下一次 OCR 冷启动，可能把短检查拖到 1-2 分钟。只有要切换到 Ollama、本地大模型、游戏或其他重 GPU 任务前，才用 `release_resources.ps1` / `-StopAfter`。

## 常见误用

- 所有服务/Windows入口只允许 `127.0.0.1`；这不是带认证的远程 OCR API，不接受任意 `HostAddress`。
- `cache_status=cache_hit` 是成功复用已校验的输出，不是失败；直接读 `results[].output_files`。其中 `objective` sidecar 是客观结果的校验依据。
- 高平均分、`quality=sufficient` 或 hash 校验通过不代表每行文字都正确。极小/浅色关键文字要回原图核对；任务允许原生视觉时，可另附绑定原图 hash 与区域的视觉校正，明确区分其与未改写的模型输出。看不清仍保留未知，不用反复换参数制造“正确”。
- `results[].objective_outcome=indeterminate` 表示引擎完成但没有足够证据判断无文字；它不是 `no_text_detected`，也不等价于图片/事件无意义。`execution_status=corrupt|unsupported|failed`、`coverage.status=partial|unknown` 和 `quality.status=low_confidence|unknown` 要分别处理。只有 sidecar 的 `evidence.verification_status=verified` 才可作为持久化负向证据。
- 调用端超时不等于服务端失败；先查 `/health.active_jobs`、返回的 `job_key` 和 `/jobs/<job_key>`，不要盲目重发。服务端到执行期限会返回 504 并清理工作进程。
- `/health.gpu_status=not_probed` 只表示新 API 尚未执行带租约的 GPU 探针；`loaded_models` 表示当前唯一热工作进程中的模型，不是可用模型清单。
- `start_server.ps1` 报 `non-LocalOCR service` 时，说明端口上是别的服务；不要继续等冷启动。查询 `E:\PCConfig` 的端口注册并确认空闲端口后，再显式传入 `-Port`。`18666` 属于 ChineseASR，不是 LocalOCR 的回退端口。
- Word / PPT / Excel / 数字 PDF 不应先丢给 OCR；先用原生文档/PDF解析，只有扫描件、截图、拍照页、嵌入图片文字才用 LocalOCR。

## 快速开始

### 1. 安装（一次性）

在 **Windows PowerShell** 里：

```powershell
wsl -d Ubuntu -e bash /mnt/e/Projects/Tools/LocalOCR/scripts/install_wsl.sh
```

脚本会：创建 venv → 装 paddlepaddle-gpu cu129 → 装 paddleocr 3.7.0 → 预下载所有模型。
约 20-40 分钟，取决于网速和 PP-StructureV3 组件缓存状态。完成后 WSL 缓存里都有模型，后续完全离线。

### 2. 使用

**方式 A — 拖拽（最简单）**：把图片/PDF/文件夹拖到 `E:\Projects\Tools\LocalOCR\start.bat` 上，松手即跑。
结果出现在 `E:\Projects\Tools\LocalOCR\outputs\` 下，每个输入文件产出兼容的 `.txt` / `.md` / `.json` 展示投影，另有按请求 hash 隔离的 `.txt` / `.md` / `.json` canonical 投影和 `.objective.json` 客观结果 sidecar。

**方式 B — 命令行**：

```powershell
.\start.ps1 "C:\path\to\图片或文件夹或pdf"
```

等价于在 WSL 里：

```bash
cd /mnt/e/Projects/Tools/LocalOCR
scripts/run_in_wsl.sh -m localocr.cli "图片或文件夹或pdf" --engine auto --out-dir outputs
```

参数：

- `--engine auto|ocr|vl|structure`：`auto`（默认）按类型自动分流；`ocr` 强制 PP-OCRv6_medium；`vl` 强制 VL-1.6；`structure` 强制 PP-StructureV3。
- `--model <profile-id>`：指定具体模型 profile，例如 `ppocrv6-medium`、`paddleocr-vl-1.6` 或 `pp-structure-v3`；不传则使用该 engine 的默认 profile。
- `--out-dir`：输出目录，默认 `outputs`。
- `--recursive`：输入为文件夹时递归子目录。
- `--timeout-sec`：整个请求执行期限，默认 300 秒，上限 7200 秒。

**方式 C — 常驻本地 API（推荐给 AI 助手/高频 OCR）**：

```powershell
# Codex / AI 助手默认入口：所有引擎共享可取消、有期限的执行路径
.\ocr_smart.ps1 "E:\Projects\Tools\LocalOCR\tests\samples\sample_scan.pdf" -Engine auto

# 只做轻量预检，不提交 OCR 任务
.\ocr_smart.ps1 "E:\Projects\Tools\LocalOCR\tests\samples\sample_scan.pdf" -TriageOnly

# 启动本机 API，只监听 127.0.0.1:18665
.\start_server.ps1

# 通过 API 调一次 OCR；如果服务未启动，会自动拉起
.\ocr_once.ps1 "E:\Projects\Tools\LocalOCR\tests\samples\sample_chat_screenshot.png" -Engine ocr

# 指定具体模型 profile；适合未来新增/切换模型时做验收
.\ocr_once.ps1 "E:\Projects\Tools\LocalOCR\tests\samples\probe_text.png" -Engine auto -Model ppocrv6-medium

# 确实较长的任务：执行期限与客户端等待分别设置
.\ocr_smart.ps1 "E:\path\scan.pdf" -Engine vl -ExecutionTimeoutSec 600 -OuterTimeoutSec 630 -TimeoutSec 660

# 表格/版面块/公式/印章等需要结构化坐标和块类型时，用 PP-StructureV3
.\ocr_once.ps1 "E:\Projects\Tools\LocalOCR\tests\samples\sample_table.png" -Engine structure -TimeoutSec 3600

# 查询某个 job_key 的状态或缓存可用性
Invoke-RestMethod "http://127.0.0.1:18665/jobs/<job_key>"

# 首次冷启动服务较慢时，可单独放宽服务启动等待时间
.\ocr_once.ps1 "E:\Projects\Tools\LocalOCR\tests\samples\sample_chat_screenshot.png" -Engine ocr -StartupTimeoutSec 900

# 一次性 OCR 后立即释放 API/GPU 资源
.\ocr_once.ps1 "E:\Projects\Tools\LocalOCR\tests\samples\sample_chat_screenshot.png" -Engine ocr -StopAfter

# 启动本地大模型、游戏或其他重 GPU 任务前，手动释放 LocalOCR
.\release_resources.ps1

# 停止服务
.\stop_server.ps1
```

HTTP 入口：

- `GET http://127.0.0.1:18665/health`
- `POST http://127.0.0.1:18665/jobs/<job_key>/cancel`
- `GET http://127.0.0.1:18665/jobs/<job_key>`
- `POST http://127.0.0.1:18665/ocr/path`
- `POST http://127.0.0.1:18665/ocr/file`

说明：`/health` 的 `active_jobs` 给出任务、阶段、工作进程和期限，`loaded_engines` 表示当前工作进程中的模型。`engine=auto` 会先经过
Smart Router；`results[].route` 会解释首轮选择、难度评估和最终引擎。所有模型共享同一受监督工作进程协议，
同模型热复用、换模型重建；`loaded_models` 返回该工作进程中的具体 profile id。

`/ocr/path` 请求示例：

```json
{
  "path": "E:\\Projects\\Tools\\LocalOCR\\tests\\samples\\sample_chat_screenshot.png",
  "engine": "ocr",
  "model": "ppocrv6-medium",
  "recursive": false,
  "write_outputs": true,
  "timeout_sec": 300
}
```

`ocr_smart.ps1` 成功时返回兼容 `ocr_once.ps1` 的 API JSON，并附加 `smart` 路由元数据；每个输入文件的输出路径位于
`results[].output_files`，规范路径带有请求 hash；无 hash 的同名文件只是兼容展示。最终路由看
`results[].route.effective_engine`、`results[].route.reason`、`results[].route.signals` 和
`results[].route.confidence`；自动首轮 OCR 还会返回 `route.initial_engine`、`route.escalated` 和
`route.difficulty`。`smart.preview_*` 只是 PowerShell 预检预测。API 还会给每个写盘任务返回
`job_key` / `job_id` / `cache_status`；同一源文件、同一请求语义与路由策略、同一输出目录再次提交时，若输出文件仍存在，会直接返回
`cache_status=cache_hit`。若任务正在运行，API 返回 `status=active_localocr_task` 和 `recommendation=do_not_blindly_retry`，
不要盲目重复提交；可用 `GET /jobs/<job_key>` 查询状态。
若外层等待超时或发现已有重 OCR 子任务，`ocr_smart.ps1` 会返回短 JSON，例如 `status=client_timeout`
或 `status=active_localocr_task`，并给出 `recommendation=do_not_blindly_retry`。
如果刚改过 `model_profiles.json` 或 adapter，先重启 LocalOCR API 再验收，避免常驻进程继续使用旧 registry。

坐标契约：JSON 的 `bbox` 保持历史形状，新增 `rect`（`[x1,y1,x2,y2]`）、`polygon` 和
`coordinate_space=image_pixels`。Structure 页面另外提供 `structure_details`（表格/公式/印章/区域的
JSON-native 原始结果）、独立的 `text_lines`（`overall_ocr_res` 逐行结果）和 `excluded_regions`。
Structure/VL 中的 `face/person/human/portrait/figure/image` 只按非文字区域标签排除，不执行人脸识别或身份判断。
PDF 页面会标注 `rendered_pdf_pixels=true`、`render_scale=2.0`、`rendered_width`/`rendered_height`；这些坐标是
渲染图像像素，不是原始 PDF 点坐标。

资源策略：教练/批量 OCR 时可以保持 API 常驻以复用 PP-OCR；切换到 Ollama、本地大模型
或其他重 GPU 工作负载前，调用 `release_resources.ps1` 或使用 `ocr_once.ps1 -StopAfter`。
也可以把 `release_resources.ps1` 接到本机 Ollama / 本地大模型启动脚本的前置步骤。

## 目录

```
localocr/        源码
  cli.py         命令行入口
  model_registry.py / model_profiles.json
                 模型 profile 注册表；把模型选择与推理实现解耦
  router.py      扩展名和文件收集基础工具
  smart_router.py Smart Router v3 的低成本预路由
  difficulty.py  OCR 结果级困难判定；仅 auto 首轮 OCR 可触发本地 VL 升级
  engines/       PP-OCRv6、VL 与 PP-StructureV3 adapter，实现统一 predict_image 输出协议
  job_registry.py 文件型任务缓存、去重和 job 状态 manifest
  outputs.py     TXT/MD/JSON 兼容投影输出
  objective_result.py  客观结果 schema、负向证据和 cache sidecar 校验
  service.py     轻量协调、任务快照、期限和原子提交
  runtime.py     单个可终止的热工作进程；所有模型共享监督协议
  server.py      FastAPI 本地 API，提供 health/job/OCR 端点
  gpu_probe.py   GPU 强制探针
scripts/         安装/下载/WSL 运行脚本
tests/           合成样本与回归测试；重型报告按需在本地生成
docs/            当前架构、模型清单、故障排除
start.bat/ps1    Windows 一次性 CLI 入口
start_server.ps1 Windows API 启动入口
ocr_smart.ps1    Windows Codex/AI 防卡智能入口
ocr_once.ps1     Windows API 一次性调用入口
release_resources.ps1 Windows 释放 LocalOCR API/GPU 资源入口
stop_server.ps1  Windows API 停止入口
```

## 文档

- [架构说明](docs/ARCHITECTURE.md)
- [模型清单与来源](docs/MODELS.md)
- [故障排除](docs/TROUBLESHOOTING.md)
- [AI 助手快速上手](docs/QUICKSTART_FOR_AI.md)

## 验证与报告

普通回归不加载模型。明确运行 `tests/run_tests.py --allow-heavy` 后，报告写入本地
`tests/TEST_REPORT.md`，不再把旧报告或实施计划当作现行结果提交；历史保留在 Git。
真实需求仍须回读实际结果与原件，不能用测试通过、模型加载成功或高平均分替代验收。
