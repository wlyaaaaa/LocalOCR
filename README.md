# LocalOCR

本地高质量中文 OCR 系统，基于 **PaddleOCR 3.7.0** + **PaddlePaddle GPU 3.3.1 (CUDA 12.9)**，
面向 **RTX 5090D（Blackwell sm_120）** + WSL2 Ubuntu 24.04。

## 特性

- **中文优先**：默认 PP-OCRv6_medium 检测+识别，方向检测 / 文档矫正 / 文本行旋转纠正全开。
- **复杂文档用 VL**：PDF、合同、论文、表格、公式、多栏排版默认走 **PaddleOCR-VL-1.6**。
- **自动分流**：图片类 → PP-OCRv6_medium；PDF → PaddleOCR-VL-1.6，无需手动选模型。
- **GPU 加速**：强制 GPU 探针，Blackwell sm_120 原生支持，不静默回退 CPU。
- **离线运行**：所有模型预下载到本地，断网可用。
- **多格式输出**：TXT / Markdown / JSON，保留文字坐标、置信度、表格、阅读顺序。
- **拖拽即用**：把图片、文件夹或 PDF 拖到 `start.bat` 上即可自动识别。
- **常驻本地 API**：`start_server.ps1` 启动后模型常驻内存，后续 OCR 复用已加载模型，适合 Codex/脚本频繁调用。

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
约 10-20 分钟，取决于网速。完成后 `models/` 与 WSL 缓存里都有模型，后续完全离线。

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
- `--engine auto|ocr|vl`：`auto`（默认）按类型自动分流；`ocr` 强制 PP-OCRv6_medium；`vl` 强制 VL-1.6。
- `--out-dir`：输出目录，默认 `outputs`。
- `--recursive`：输入为文件夹时递归子目录。

**方式 C — 常驻本地 API（推荐给 AI 助手/高频 OCR）**：

```powershell
# 启动本机 API，只监听 127.0.0.1:8765
.\start_server.ps1

# 通过 API 调一次 OCR；如果服务未启动，会自动拉起
.\ocr_once.ps1 "E:\LocalOCR\tests\samples\sample_chat_screenshot.png" -Engine ocr

# 停止服务
.\stop_server.ps1
```

HTTP 入口：

- `GET http://127.0.0.1:8765/health`
- `POST http://127.0.0.1:8765/ocr/path`
- `POST http://127.0.0.1:8765/ocr/file`

`/ocr/path` 请求示例：

```json
{
  "path": "E:\\LocalOCR\\tests\\samples\\sample_chat_screenshot.png",
  "engine": "ocr",
  "recursive": false,
  "write_outputs": true
}
```

## 目录

```
localocr/        源码
  cli.py         命令行入口
  router.py      自动分流
  engines/       PP-OCRv6 与 VL 两个引擎
  outputs.py     TXT/MD/JSON 输出
  service.py     常驻服务层，缓存 OCR/VL 引擎
  server.py      FastAPI 本地 API
  gpu_probe.py   GPU 强制探针
scripts/         安装/下载/WSL 运行脚本
tests/           合成样本与测试脚本，见 TEST_REPORT.md
docs/            架构、模型清单、故障排除、设计文档
start.bat/ps1    Windows 一次性 CLI 入口
start_server.ps1 Windows API 启动入口
ocr_once.ps1     Windows API 一次性调用入口
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
