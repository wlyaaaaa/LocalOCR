# 模型清单与来源

## Profile 注册表

模型选择由 `localocr/model_profiles.json` 管理，运行时通过 `localocr/model_registry.py` 解析。
`ocr` / `vl` / `structure` 是默认 profile 别名：

| 别名 | 默认 profile id | 当前 adapter |
|---|---|---|
| `ocr` | `ppocrv6-medium` | `localocr.engines.ppocrv6:PPOCRv6Engine` |
| `vl` | `paddleocr-vl-1.6` | `localocr.engines.vl:VLEngine` |
| `structure` | `pp-structure-v3` | `localocr.engines.structure:StructureV3Engine` |

调用层优先使用 `--engine auto|ocr|vl|structure` 做路由；需要指定具体模型时使用
`--model <profile-id>` 或 Windows wrapper 的 `-Model <profile-id>`。新增或替换模型时，
先新增 profile 和 adapter，再用样本图/PDF 做 smoke test；不要把模型名硬编码到
`cli.py`、`server.py`、`service.py` 或 PowerShell wrapper 里。
常驻 API 会缓存已加载的 registry 和模型实例；修改 profile 或 adapter 后，先执行
`stop_server.ps1` / `start_server.ps1` 或 `release_resources.ps1`，再做验收。

## 已下载模型（本地缓存：`/root/.paddlex/official_models/`）

### PP-OCRv6_medium（图片/截图/聊天记录/网页图/纯文字扫描件）

| 组件 | 模型名 | 来源 |
|---|---|---|
| 文本检测 | `PP-OCRv6_medium_det` | ModelScope `PaddlePaddle/PP-OCRv6_medium_det` |
| 文本识别 | `PP-OCRv6_medium_rec` | ModelScope `PaddlePaddle/PP-OCRv6_medium_rec` |
| 文档方向分类 | `PP-LCNet_x1_0_doc_ori` | ModelScope |
| 文档矫正(UVDoc) | `UVDoc` | ModelScope |
| 文本行方向 | `PP-LCNet_x1_0_textline_ori` | ModelScope |

- 触发方式：`PaddleOCR(ocr_version="PP-OCRv6", lang="ch", use_doc_orientation_classify=True, use_doc_unwarping=True, use_textline_orientation=True)`
- 源码确认：`paddleocr/_pipelines/ocr.py:357` — lang=ch + PP-OCRv6 → medium 模型

### PaddleOCR-VL-1.6（PDF/合同/论文/表格/公式/多栏复杂文档）

| 组件 | 模型名 | 来源 |
|---|---|---|
| VL 识别模型 | `PaddleOCR-VL-1.6` | ModelScope `PaddlePaddle/PaddleOCR-VL-1.6`（约 1.92GB safetensors）|
| 版面检测 | 内置 | ModelScope |
| 文档方向/矫正 | 同上 PP-LCNet/UVDoc | ModelScope |

- 触发方式：`PaddleOCRVL(pipeline_version="v1.6", vl_rec_backend="native")`

### PP-StructureV3 + PP-OCRv5（显式结构化高配）

| 组件 | 模型名 | 来源 |
|---|---|---|
| 版面/区域检测 | `PP-DocBlockLayout` / `PP-DocLayout_plus-L` | ModelScope |
| 文本检测 | `PP-OCRv5_server_det` | ModelScope |
| 文本识别 | `PP-OCRv5_server_rec` | ModelScope |
| 表格方向/结构/单元格 | `PP-LCNet_x1_0_table_cls`、`SLANeXt_wired`、`SLANet_plus`、`RT-DETR-L_*_table_cell_det` | ModelScope |
| 公式识别 | `PP-FormulaNet_plus-L` | ModelScope |
| 印章检测/识别 | `PP-OCRv4_server_seal_det` + PP-OCRv5 rec | ModelScope |
| 文档方向/矫正/文本行方向 | `PP-LCNet_x1_0_doc_ori`、`UVDoc`、`PP-LCNet_x1_0_textline_ori` | ModelScope |

- 触发方式：`PPStructureV3(lang="ch", ocr_version="PP-OCRv5", use_table_recognition=True, use_formula_recognition=True, use_seal_recognition=True, use_region_detection=True)`
- 本机实测：`PPStructureV3(ocr_version="PP-OCRv6")` 会报 `Invalid OCR version`；当前 PaddleOCR 3.7.0 的 PP-StructureV3 只接受 `PP-OCRv3/v4/v5`，因此结构化 profile 固定使用 `PP-OCRv5`。
- 定位：显式高配，不替换 `auto` 默认。适合需要表格 HTML、版面块、公式、印章和区域坐标的图片/PDF；简单 OCR 继续走 `ocr`，复杂整页理解继续走 `vl`。

## 下载来源优先级（需求 8）

1. **ModelScope**（`PADDLE_PDX_MODEL_SOURCE=modelscope`）—— 国内可达，不消耗代理流量，实测 ~25MB/s
2. 百度 BOS（`paddle-model-ecology.bj.bcebos.com`）—— 备用
3. AIStudio —— 备用
4. ❌ HuggingFace —— 国内不可达，已从默认源移除

## 离线运行

模型下载后落在 `/root/.paddlex/official_models/`，PaddleOCR 启动时检测到本地缓存即不再联网。
预热脚本：`scripts/download_models.py`（对三类引擎各跑一次预热推理）。

## 不使用的模型（需求 9）

- ❌ RapidOCR
- ❌ PP-OCRv3/v4/v5 作为普通 OCR 默认模型
- ❌ tiny / small / mobile 变体
- 只用 PP-OCRv6_medium（非 server 也非 mobile，是 medium 质量档）
- 例外：`PP-StructureV3` 当前只支持 `PP-OCRv3/v4/v5`，因此结构化 profile 内部使用 `PP-OCRv5_server_det/rec`。
