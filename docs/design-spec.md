# LocalOCR 设计文档

- 日期：2026-07-01
- 状态：已确认，实施中
- 目标硬件：RTX 5090D（Blackwell，sm_120）；当前开发机 RTX 5080 同为 Blackwell sm_120

## 1. 目标

在 Windows + RTX 5090D 上部署最新版、高质量优先的本地 OCR 系统，满足 12 条需求（见 README）。
中文识别准确率第一优先级；普通图片、普通扫描 PDF 和表单优先用 PP-OCRv6_medium；表格/公式/多栏/论文等复杂文档用 VL 模型；
需要表格/版面块/公式/印章/区域坐标时显式用 PP-StructureV3；
自动分流；GPU 加速；离线运行；国内镜像下载；TXT/Markdown/JSON 输出保留坐标/置信度/表格/阅读顺序。

## 2. 环境决策（关键）

### 2.1 为什么选 WSL2 + Ubuntu 而非 Windows 原生

RTX 5080 / 5090D 均为 Blackwell 架构，compute capability = **12.0（sm_120）**。
PaddlePaddle 官方 **Windows** GPU wheel 目前只提供 cu118/cu126 构建，不含 sm_120 cubin，
在 Blackwell 上会失败或退回 PTX-JIT（不可靠）。

PaddlePaddle 官方 **Linux** GPU wheel 提供 **cu129** 构建（CUDA 12.9 原生支持 sm_120），
wheel 自带 CUDA/cuDNN/NCCL 运行库，无需单独装 CUDA toolkit。
本机驱动 610.62（CUDA 13.3）在 WSL 下以 `libcuda.so.1` 透传，满足 cu129 运行时要求。

**结论**：主路径 = WSL2 Ubuntu 24.04 + `paddlepaddle-gpu==3.3.1` cu129 + `paddleocr==3.7.0`。
已实测 `paddle.device.cuda.get_device_capability(0) == (12, 0)`，matmul 在 `Place(gpu:0)` 正常执行。

### 2.2 不静默回退 CPU（需求 12）

启动时强制 GPU 探针：`paddle.is_compiled_with_cuda()` + `device_count()>0` + capability≥(12,0) +
实际算子执行。任一失败则报错退出，不退回 CPU。

### 2.3 WSL 注意事项

- WSL 默认 root，免 sudo，直接 apt-get。
- Paddle 的 `dynamic_loader` 找不到 `libcuda.so`（WSL 在 `/usr/lib/wsl/lib/`），
  需 `export LD_LIBRARY_PATH=/usr/lib/wsl/lib:$LD_LIBRARY_PATH`。
- Python 3.12 系统受 PEP 668 限制，用 venv（`/root/localocr-venv`）。

## 3. 架构

```
E:\LocalOCR\                         (源码放 Windows 端，便于 git/编辑；运行在 WSL)
├─ localocr/
│  ├─ __init__.py
│  ├─ gpu_probe.py      # GPU 强制探针，失败即退出
│  ├─ router.py         # 扩展名判断与输入收集
│  ├─ smart_router.py   # Smart Router v2：可解释 auto 分流
│  ├─ engines/
│  │  ├─ ppocrv6.py     # PaddleOCR(ocr_version="PP-OCRv6", lang="ch", 方向检测/矫正/旋转全开)
│  │  ├─ vl.py          # PaddleOCRVL(pipeline_version="v1.6", vl_rec_backend="native")
│  │  └─ structure.py   # PPStructureV3(ocr_version="PP-OCRv5", 表格/公式/印章/区域检测)
│  ├─ pdf_utils.py      # PDF→图片（PyMuPDF/pypdfium2，已随 paddleocr 安装）
│  ├─ outputs.py        # TXT / Markdown / JSON，保留坐标/置信度/表格/阅读顺序
│  ├─ cli.py            # argparse 入口：单图/文件夹/PDF，--engine auto|ocr|vl
│  └─ launcher.py       # 被 start.ps1 / start.bat 调用
├─ models/              # 预下载模型，离线复用（.gitignore）
├─ tests/
│  ├─ samples/          # 4 份合成中文样本
│  ├─ run_tests.py      # 跑 4 项测试，输出报告
│  └─ TEST_REPORT.md    # 实测报告（模型/GPU/显存/速度/输出片段）
├─ scripts/
│  ├─ install_wsl.sh    # 一键安装（venv + paddle gpu cu129 + paddleocr + 下载模型）
│  ├─ download_models.py# 预下载所有模型到 models/
│  └─ run_in_wsl.sh     # 设置 LD_LIBRARY_PATH 并用 venv python 执行
├─ start.ps1            # Windows 入口：拖入文件或传参，转调 WSL
├─ start.bat            # 同上（拖拽友好）
├─ README.md
├─ docs/
│  ├─ ARCHITECTURE.md
│  ├─ MODELS.md         # 模型清单与来源
│  ├─ TROUBLESHOOTING.md
│  └─ design-spec.md    # 本文件
└─ pyproject.toml       # 依赖钉版本
```

## 4. 路由规则（Smart Router v2）

| 输入类型 | 默认引擎 | 判定依据 |
|---|---|---|
| 单张图片（png/jpg/webp/bmp/tif） | PP-OCRv6_medium | 扩展名 |
| 截图 / 聊天记录 / 网页图 / 纯文字扫描件 | PP-OCRv6_medium | 图片类一律走 OCR |
| 普通扫描 PDF / 表单 / 纯文字 PDF | PP-OCRv6_medium | `.pdf` 且无复杂版面关键词 |
| 表格/公式/多栏/论文/课件等复杂 PDF | PaddleOCR-VL-1.6 | 文件名含 `table/formula/layout/multi/论文/公式/表格/多栏/课件` 等低成本信号 |
| 文件夹 | 按其中每个文件类型分别路由 | 递归 |

- `--engine auto`（默认）：按上表自动分流。
- `--engine ocr`：强制 PP-OCRv6_medium（即使 PDF 也先把每页转图再 OCR）。
- `--engine vl`：强制 PaddleOCR-VL-1.6（即使普通图片也走 VL）。
- `--engine structure`：强制 PP-StructureV3 + PP-OCRv5，适合表格 HTML、版面块、公式、印章和区域坐标；不参与默认 `auto` 路由。

2026-07-07 调整：LocalOCR 已引入 Smart Router v2。`auto` 不再把所有 PDF 一律送入 VL；
普通扫描 PDF 和法律/地址确认等表单默认走 OCR，避免 Codex 外层超时和不必要的重模型加载。
复杂版面仍可由低成本信号自动进入 VL，也可由用户显式 `--engine vl` 指定。
每个 API 结果返回 `route.effective_engine`、`route.reason`、`route.signals` 和 `route.confidence` 便于审计。

## 5. 引擎配置

### 5.1 PP-OCRv6_medium（engines/ppocrv6.py）

```
PaddleOCR(
    ocr_version="PP-OCRv6",      # → PP-OCRv6_medium_det / _rec（ch 默认）
    lang="ch",
    use_doc_orientation_classify=True,   # 方向检测
    use_doc_unwarping=True,              # 文档矫正
    use_textline_orientation=True,       # 文本行旋转纠正
    device="gpu:0",
)
```
（源码 ocr.py:357：lang=ch + PP-OCRv6 → medium 模型，符合需求 2、9）

### 5.2 PaddleOCR-VL-1.6（engines/vl.py）

```
PaddleOCRVL(
    pipeline_version="v1.6",
    vl_rec_backend="native",     # 本地推理，不依赖外部服务器
    use_doc_orientation_classify=True,
    use_doc_unwarping=True,
    device="gpu:0",
)
```
（paddleocr_vl.py:24 默认 pipeline_version="v1.6"，符合需求 3）

### 5.3 PP-StructureV3 + PP-OCRv5（engines/structure.py）

```
PPStructureV3(
    lang="ch",
    ocr_version="PP-OCRv5",
    use_doc_orientation_classify=True,
    use_doc_unwarping=True,
    use_textline_orientation=True,
    use_table_recognition=True,
    use_formula_recognition=True,
    use_chart_recognition=False,
    use_seal_recognition=True,
    use_region_detection=True,
    format_block_content=True,
    device="gpu:0",
)
```

当前 PaddleOCR 3.7.0 的 `PPStructureV3` 只接受 `PP-OCRv3/v4/v5`，本机实测
`ocr_version="PP-OCRv6"` 会报 `Invalid OCR version`。因此普通 OCR 继续使用
PP-OCRv6_medium，结构化 profile 固定使用 PP-OCRv5。

## 6. 输出格式（outputs.py）

每个输入文件产出三份同名输出：`.txt`、`.md`、`.json`。

- **TXT**：纯文本，按阅读顺序拼接，段落用空行分隔。
- **Markdown**：按页/按块组织，含标题层级；表格输出为 Markdown 表格；公式保留 LaTeX。
- **JSON**：结构化，字段：
  - `file`, `engine`, `model`, `device`, `gpu_capability`, `pages`[]
  - 每页：`page_index`, `blocks`[]，每块含 `type`(text/table/figure/formula)、
    `text`、`bbox`（坐标）、`score`（置信度）、`order`（阅读顺序）。
- 坐标、置信度、表格结构、阅读顺序均来自引擎输出，原样保留。

## 7. 模型预下载（scripts/download_models.py）

- 目标：所有模型下载到 `models/`，后续 `ocr_version`/`pipeline_version` 指向本地路径或缓存，完全离线。
- 来源优先级：百度官方源（aistudio/modelscope 百度桶）→ ModelScope → 国内镜像。
- PaddleOCR 默认通过 paddlex 下载到 `~/.paddlex/`（root 下为 `/root/.paddlex`）；
  下载脚本会预热（warmup）一次每类引擎，让模型落到本地缓存，之后断网可用。
- 额外把缓存目录同步/链接到项目 `models/` 以便整体管理与迁移（可选）。

## 8. 启动方式（需求 10）

- **start.bat / start.ps1**：接受拖入的文件/文件夹/PDF（或命令行参数），
  内部调用 `wsl -d Ubuntu -e bash scripts/run_in_wsl.sh python -m localocr.cli <args>`。
- **CLI**：`python -m localocr.cli <文件或文件夹> [--engine auto|ocr|vl|structure] [--out-dir 输出目录] [--recursive]`。
- 自动完成识别，输出落到 `outputs/` 下，三格式齐全。
- 文档写给 AI 助手看：`docs/QUICKSTART_FOR_AI.md` 一页纸说明"如何启动本项目"。

## 9. 测试（需求 11）

`tests/run_tests.py` 对 4 份合成样本各跑一次，记录：
- 实际使用的模型（从引擎实例读出 model_name）
- GPU 是否生效（gpu_probe + 推理后 nvidia-smi）
- 显存占用（nvidia-smi 采样）
- 速度（耗时/页数）
- 输出片段（TXT/MD/JSON 各取前若干行）

结果写入 `tests/TEST_REPORT.md`。合成样本由 `tests/make_samples.py` 生成：
1. 中文截图（模拟微信聊天界面文字）
2. 扫描风 PDF（多页中文段落，带轻微旋转模拟扫描）
3. 表格文档（中英文混排表格）
4. 公式文档（含数学公式的中文说明）

## 10. 依赖钉版本（pyproject.toml）

```
paddlepaddle-gpu==3.3.1   (cu129 索引)
paddleocr==3.7.0
pypdfium2>=5.11           (PDF→图，随 paddleocr 安装)
opencv-contrib-python     (随 paddleocr)
```
不引入 RapidOCR、不用 tiny/small/mobile（需求 9）。

## 11. 风险与兜底

- **sm_120 算子缺失**：已实测基础算子可用；若个别高级算子报 `no kernel`，
  降级该引擎的 `use_tensorrt=False`、`precision=fp32`，仍失败则明确报错（不静默 CPU）。
- **VL native 后端显存**：5090D 32GB 足够；5080 16GB 需监控，必要时减小并发。
- **模型下载失败**：脚本提供百度/ModelScope/镜像多源回退，全失败则报错并列出已尝试源。
