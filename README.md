# LocalOCR

本地高质量中文 OCR 系统，基于 **PaddleOCR 3.7.0** + **PaddlePaddle GPU 3.3.1 (CUDA 12.9)**，
面向 **RTX 5090D（Blackwell sm_120）** + WSL2 Ubuntu 24.04。

## 特性

- **中文优先**：默认 PP-OCRv6_medium 检测+识别，方向检测 / 文档矫正 / 文本行旋转纠正全开。
- **复杂文档用 VL**：PDF、合同、论文、表格、公式、多栏排版默认走 **PaddleOCR-VL-1.6**。
- **结构化高配可选**：表格、版面块、公式、印章、区域检测可显式走 **PP-StructureV3 + PP-OCRv5**（`-Engine structure` / `--engine structure`）。
- **自动分流**：图片类 → PP-OCRv6_medium；PDF → PaddleOCR-VL-1.6，无需手动选模型。
- **GPU 加速**：强制 GPU 探针，Blackwell sm_120 原生支持，不静默回退 CPU。
- **离线运行**：所有模型预下载到本地，断网可用。
- **模型 profile 解耦**：`localocr/model_profiles.json` 声明默认模型、能力标签和 adapter；`--model` / `-Model` 可指定具体 profile。
- **多格式输出**：TXT / Markdown / JSON，保留文字坐标、置信度、表格、阅读顺序。
- **拖拽即用**：把图片、文件夹或 PDF 拖到 `start.bat` 上即可自动识别。
- **常驻本地 API**：`start_server.ps1` 启动后 PP-OCR 常驻内存；VL/PDF 长任务由隔离子进程执行，适合 Codex/脚本频繁调用且避免 Web 服务被超大模型拖垮。
- **Codex 防卡入口**：`ocr_smart.ps1` 先做轻量分流和后台任务探测，再用外层超时包住 `ocr_once.ps1`，避免 PowerShell 长时间占住 AI 回合。

## 环境

| 项 | 值 |
|---|---|
| OS（运行） | WSL2 Ubuntu 24.04 LTS |
| GPU | RTX 5080 / 5090D（Blackwell，sm_120，CUDA 12.9 原生） |
| PaddlePaddle | 3.3.1 GPU，cu129 构建wheel 自带 CUDA/cuDNN/NCCL） |
| PaddleOCR | 3.7.0 |
| Python | 3.12（WSL venv） |

> 为何不用 Windows 原生：Paddle 官方 Windows GPU wheel 仅 cu118/cu126，不含 sm_120 cubin，
> 在 Blackwell 上不可靠。Linux cu129 wheel 原生支持 sm_120。详见 `docs/design-spec.md`。

## 快速开始

### 1. 安装（一次性）

在 **Windows PowerShell** 里：

```powershell
wsl -d Ubuntu -e bash /mnt/e/LocalOCR/scripts/install_wsl.sh
```

脚本会：创建 venv → 装 paddlepaddle-gpu cu129 → 装 paddleocr 3.7.0 → 预下载所有模型。
约 20-40 分钟，取决于网速和 PP-StructureV3 组件缓存状态。完成后 WSL 缓存里都有模型，后续完全离线。

### 2. 使用

**方式 A — 拖拽（最简单）**：把图片/PDF/文件夹拖到 `E:\LocalOCR\start.bat` 上，松手即跑。
结果出现在 `E:\LocalOCR\outputs\` 下，每个输入文件产出 `.txt` / `.md` / `.json` 三份。

**方式 B — 命令行**：

```powershell
.\start.ps1 "C:\path\to\图片或文件夹或pdf"
```

等价于在 WSL 里：

```bash
cd /mnt/e/LocalOCR
scripts/run_in_wsl.sh python -m localocr.cli "图片或文件夹或pdf" --engine auto --out-dir outputs
```

参数：
- `--engine auto|ocr|vl|structure`：`auto`（默认）按类型自动分流；`ocr` 强制 PP-OCRv6_medium；`vl` 强制 VL-1.6；`structure` 强制 PP-StructureV3。
- `--model <profile-id>`：指定具体模型 profile，例如 `ppocrv6-medium`、`paddleocr-vl-1.6` 或 `pp-structure-v3`；不传则使用该 engine 的默认 profile。
- `--out-dir`：输出目录，默认 `outputs`。
- `--recursive`：输入为文件夹时递归子目录。

**方式 C — 常驻本地 API（推荐给 AI 助手/高频 OCR）**：

```powershell
# Codex / AI 助手默认入口：简单 PDF 会自动改走 OCR，且外层最多等待 120 秒
.\ocr_smart.ps1 "E:\LocalOCR\tests\samples\sample_scan.pdf" -Engine auto

# 只做轻量预检，不提交 OCR 任务
.\ocr_smart.ps1 "E:\LocalOCR\tests\samples\sample_scan.pdf" -TriageOnly

# 启动本机 API，只监听 127.0.0.1:8765
.\start_server.ps1

# 通过 API 调一次 OCR；如果服务未启动，会自动拉起
.\ocr_once.ps1 "E:\LocalOCR\tests\samples\sample_chat_screenshot.png" -Engine ocr

# 指定具体模型 profile；适合未来新增/切换模型时做验收
.\ocr_once.ps1 "E:\LocalOCR\tests\samples\probe_text.png" -Engine auto -Model ppocrv6-medium

# VL / PDF / 公式等长任务可显式放宽客户端等待时间
.\ocr_once.ps1 "E:\LocalOCR\tests\samples\sample_table.png" -Engine vl -TimeoutSec 3600

# 表格/版面块/公式/印章等需要结构化坐标和块类型时，用 PP-StructureV3
.\ocr_once.ps1 "E:\LocalOCR\tests\samples\sample_table.png" -Engine structure -TimeoutSec 3600

# 首次冷启动服务较慢时，可单独放宽服务启动等待时间
.\ocr_once.ps1 "E:\LocalOCR\tests\samples\sample_chat_screenshot.png" -Engine ocr -StartupTimeoutSec 900

# 一次性 OCR 后立即释放 API/GPU 资源
.\ocr_once.ps1 "E:\LocalOCR\tests\samples\sample_chat_screenshot.png" -Engine ocr -StopAfter

# 启动本地大模型、游戏或其他重 GPU 任务前，手动释放 LocalOCR
.\release_resources.ps1

# 停止服务
.\stop_server.ps1
```

HTTP 入口：

- `GET http://127.0.0.1:8765/health`
- `POST http://127.0.0.1:8765/ocr/path`
- `POST http://127.0.0.1:8765/ocr/file`

说明：`/health` 的 `loaded_engines` 只表示 API 进程内已缓存的轻量 OCR 引擎。`engine=vl` / `engine=structure`
会按请求启动隔离子进程完成识别，结果仍通过 API 返回并写入输出目录。
新字段 `loaded_models` 返回 API 进程内已加载的具体 profile id。

`/ocr/path` 请求示例：

```json
{
  "path": "E:\\LocalOCR\\tests\\samples\\sample_chat_screenshot.png",
  "engine": "ocr",
  "model": "ppocrv6-medium",
  "recursive": false,
  "write_outputs": true
}
```

`ocr_smart.ps1` 成功时返回兼容 `ocr_once.ps1` 的 API JSON，并附加 `smart` 路由元数据；每个输入文件的输出路径位于
`results[].output_files`，默认写到 `outputs/api/<文件名>.txt|.md|.json`。
若外层等待超时或发现已有重 OCR 子任务，`ocr_smart.ps1` 会返回短 JSON，例如 `status=client_timeout`
或 `status=active_localocr_task`，并给出 `recommendation=do_not_blindly_retry`。
如果刚改过 `model_profiles.json` 或 adapter，先重启 LocalOCR API 再验收，避免常驻进程继续使用旧 registry。

资源策略：教练/批量 OCR 时可以保持 API 常驻以复用 PP-OCR；切换到 Ollama、本地大模型
或其他重 GPU 工作负载前，调用 `release_resources.ps1` 或使用 `ocr_once.ps1 -StopAfter`。
也可以把 `release_resources.ps1` 接到本机 Ollama / 本地大模型启动脚本的前置步骤。

## 目录

```
localocr/        源码
  cli.py         命令行入口
  model_registry.py / model_profiles.json
                 模型 profile 注册表；把模型选择与推理实现解耦
  router.py      自动分流
  engines/       PP-OCRv6、VL 与 PP-StructureV3 adapter，实现统一 predict_image 输出协议
  outputs.py     TXT/MD/JSON 输出
  service.py     常驻服务层，缓存轻量 OCR，引擎重任务走隔离子进程
  server.py      FastAPI 本地 API
  gpu_probe.py   GPU 强制探针
scripts/         安装/下载/WSL 运行脚本
tests/           合成样本与测试脚本，见 TEST_REPORT.md
docs/            架构、模型清单、故障排除、设计文档
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
- [设计文档](docs/design-spec.md)
- [AI 助手快速上手](docs/QUICKSTART_FOR_AI.md)
- [测试报告](tests/TEST_REPORT.md)

## 测试结果摘要

见 `tests/TEST_REPORT.md`。包含实际使用模型、GPU 生效情况、显存占用、速度与输出片段。
