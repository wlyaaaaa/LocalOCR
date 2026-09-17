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
每个请求固定解析后的 profile；工作进程热复用同一执行版本，模型或执行指纹变化会重建。正式版本读取其固定源码快照内的配置。修改 profile 或 adapter 后，创建并验收新的版本快照，再按升级指南切换；单纯重启不会把开发代码发布到正式快照。

VL 明确关闭 `use_queues`：本项目单文件、逐页调用，不需要上游为大量图片/多页输入提供的内部异步队列。
这是运行方式适配，不替换或弱化模型；具体语义见 [PaddleX 官方说明](https://github.com/PaddlePaddle/PaddleX/blob/release/3.7/docs/pipeline_usage/tutorials/ocr_pipelines/PaddleOCR-VL.en.md)。

普通 OCR 默认 `use_doc_unwarping=false`。真实平面 UI 截图在 UVDoc 开启时出现错读/裁切并触发不必要升级，
关闭后主体文字及原图位置恢复；极小浅字仍可能误读，关键内容须回原图复核。
弯曲纸张仍可通过明确的 profile 配置启用矫正，不把它当作所有图片的通用增强。
VL 的 `cuda_module_loading=EAGER` 只在其 worker 内、任何 Paddle import/probe 前设置，并纳入 profile/缓存身份。
这是本机原生 CUDA 路径的有界初始化选择，不修改整机环境或宣称所有输入都会更快；参见
[NVIDIA 模块加载说明](https://docs.nvidia.com/cuda/cuda-programming-guide/04-special-topics/lazy-loading.html)。

## 已下载模型（本地缓存：`/root/.paddlex/official_models/`）

### PP-OCRv6_medium（图片/截图/聊天记录/网页图/纯文字扫描件）

| 组件 | 模型名 | 来源 |
|---|---|---|
| 文本检测 | `PP-OCRv6_medium_det` | ModelScope `PaddlePaddle/PP-OCRv6_medium_det` |
| 文本识别 | `PP-OCRv6_medium_rec` | ModelScope `PaddlePaddle/PP-OCRv6_medium_rec` |
| 文档方向分类 | `PP-LCNet_x1_0_doc_ori` | ModelScope |
| 文档矫正(UVDoc) | `UVDoc` | ModelScope |
| 文本行方向 | `PP-LCNet_x1_0_textline_ori` | ModelScope |

- 触发方式：`PaddleOCR(ocr_version="PP-OCRv6", lang="ch", use_doc_orientation_classify=True, use_doc_unwarping=False, use_textline_orientation=True)`
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
预热入口：`scripts/run_in_wsl.sh scripts/download_models.py --allow-heavy`。
它对三个现有 profile 串行预热，沿用生产租约、期限、内存和进程回收，不同时常驻三套模型；不是普通健康检查。

## 当前默认选择（不是未来候选的永久黑名单）

- ❌ RapidOCR
- ❌ PP-OCRv3/v4/v5 作为普通 OCR 默认模型
- ❌ tiny / small / mobile 变体
- 只用 PP-OCRv6_medium（非 server 也非 mobile，是 medium 质量档）
- 例外：`PP-StructureV3` 当前只支持 `PP-OCRv3/v4/v5`，因此结构化 profile 内部使用 `PP-OCRv5_server_det/rec`。

## 可替换版本合同

Profile 同时声明 `runtime_backend`、`revision`、`artifact_paths`、`preprocessing`。权重/配置/分词器等实际文件 SHA-256、关键包版本、实现和预处理共同构成执行身份；文件未变时复用哈希结果，不对每张图重新读取大权重。自动路线也绑定实际 VL 升级目标，避免修改默认 VL 后仍复用旧结果。

同家族兼容升级优先新增 profile，并把新权重放入独立目录；不得覆盖回滚版本仍引用的权重。将实际模型目录传给 adapter 的正式 options，并使 `artifact_paths` 一致。新框架才新增 adapter 和隔离环境；GPU 探针按 backend 选择，非 Paddle 后端不先初始化 Paddle。缺失分数或几何不伪造。

GLM-OCR 等模型可以进入同一对照流程，但本次未把它的安装或准确率比较标为通过。官方 SDK 存在云端调用路线，严格本地任务不得照搬云端默认模式。新增模型不自动晋升为默认，先用代表性材料与标准答案验收。详见 [UPGRADING.md](UPGRADING.md)。
