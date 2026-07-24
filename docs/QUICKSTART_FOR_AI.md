# AI 助手快速上手 LocalOCR

> 本文件写给 AI 助手（和人类）看：如何在本机启动这套本地 OCR。

## 一句话

在 Windows 里把图片/PDF/文件夹拖到 `E:\Projects\Tools\LocalOCR\start.bat` 上即可。结果在 `E:\Projects\Tools\LocalOCR\outputs\`（每个文件产出 `.txt`/`.md`/`.json`）。

## 环境已就绪

- 运行环境：WSL2 Ubuntu 24.04，venv 在 `/root/localocr-venv`
- PaddlePaddle GPU 3.3.1 (cu129) + PaddleOCR 3.7.0 已装
- 模型已下载到 `/root/.paddlex/official_models/`（PP-OCRv6_medium + PaddleOCR-VL-1.6 + PP-StructureV3 组件），可离线
- 模型选择已通过 `localocr/model_profiles.json` 解耦；`ocr` / `vl` / `structure` 是默认 profile 别名，可用 `--model` / `-Model` 指定具体 profile
- GPU：RTX 5090D，Blackwell sm_120，已验证可用

## 命令行

在 Windows PowerShell：

```powershell
# Smart Router v2 自动分流（图片/普通扫描 PDF→OCR，复杂表格/公式/多栏 PDF→VL）
.\start.ps1 "E:\某文件夹"
.\start.ps1 "E:\某图片.png"
.\start.ps1 "E:\某文档.pdf"

# 强制引擎
.\start.ps1 "E:\某文件" --engine ocr   # 强制 PP-OCRv6_medium
.\start.ps1 "E:\某文件" --engine vl    # 强制 PaddleOCR-VL-1.6
.\start.ps1 "E:\某文件" --engine structure  # 强制 PP-StructureV3 + PP-OCRv5
```

等价的 WSL 命令：

```bash
cd /mnt/e/Projects/Tools/LocalOCR
scripts/run_in_wsl.sh -m localocr.cli "路径" --engine auto --model ppocrv6-medium --out-dir outputs
```

## 常驻 API（推荐）

高频 OCR、Codex 调用、批量读取课程图片/PDF 时，优先启动常驻服务：

```powershell
E:\Projects\Tools\LocalOCR\start_server.ps1
```

Codex / AI 助手默认先用 smart wrapper，避免 PowerShell 长时间卡住当前回合：

```powershell
E:\Projects\Tools\LocalOCR\ocr_smart.ps1 "E:\path\scan.pdf" -Engine auto -OuterTimeoutSec 120
```

`ocr_smart.ps1` 会先查后台 `localocr.cli` / `vl_subprocess` / `structure_subprocess`，再决定是否提交任务。
真正的 `auto` 分流由 API Smart Router v2 执行：简单扫描 PDF、法律表单、送达地址确认书、空白表格和纯文字 PDF
默认走 `ocr`；文件名提示 `table/formula/layout/multi/论文/公式/表格/多栏/课件` 等复杂材料时走 `vl`。
复杂版面、表格、公式、多栏材料也可以显式传 `-Engine vl`。
需要表格 HTML、版面块、公式、印章和区域坐标时显式传 `-Engine structure`。
如果用户指定具体模型，用 `-Model <profile-id>`；显式模型始终优先，不会被 Smart Router 改写。

只想省 token 做预检，不提交 OCR：

```powershell
E:\Projects\Tools\LocalOCR\ocr_smart.ps1 "E:\path\scan.pdf" -TriageOnly
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:18665/health
```

查询某个任务：

```powershell
Invoke-RestMethod "http://127.0.0.1:18665/jobs/<job_key>"
```

底层 wrapper 仍可直接识别一个路径：

```powershell
E:\Projects\Tools\LocalOCR\ocr_once.ps1 "E:\Projects\Tools\LocalOCR\tests\samples\sample_chat_screenshot.png" -Engine ocr
```

VL、PDF、公式或首次冷启动可能较慢，调用时保留默认 `-TimeoutSec 3600`，或显式传入：

```powershell
E:\Projects\Tools\LocalOCR\ocr_once.ps1 "E:\Projects\Tools\LocalOCR\tests\samples\sample_table.png" -Engine vl -TimeoutSec 3600
E:\Projects\Tools\LocalOCR\ocr_once.ps1 "E:\Projects\Tools\LocalOCR\tests\samples\sample_table.png" -Engine structure -TimeoutSec 3600
```

注意：`-TimeoutSec` 是 OCR HTTP 请求等待时间；服务首次冷启动等待时间由
`-StartupTimeoutSec` 控制，默认 600 秒。若首轮冷启动超时但重试成功，优先显式加：

```powershell
E:\Projects\Tools\LocalOCR\ocr_once.ps1 "E:\某图片.png" -Engine auto -StartupTimeoutSec 900
```

注意：API 进程只常驻缓存 PP-OCR；VL 和 Structure 由隔离子进程执行。`/health` 里没有 `vl`
或 `structure` 不代表不可用，以一次显式 `-Engine vl` / `-Engine structure` 实际调用结果为准。

如果只是一次性读取图片/PDF，或马上要启动 Ollama/本地大模型，可以让调用结束后自动释放：

```powershell
E:\Projects\Tools\LocalOCR\ocr_once.ps1 "E:\某图片.png" -Engine auto -StopAfter
```

启动其他重 GPU 任务前，也可以显式释放：

```powershell
E:\Projects\Tools\LocalOCR\release_resources.ps1
```

停止服务：

```powershell
E:\Projects\Tools\LocalOCR\stop_server.ps1
```

API 请求体：

```json
{
  "path": "E:\\Projects\\Tools\\LocalOCR\\tests\\samples\\sample_chat_screenshot.png",
  "engine": "auto",
  "model": "ppocrv6-medium",
  "recursive": false,
  "write_outputs": true
}
```

API 写盘任务会按源文件路径、文件内容、模型 profile 和输出目录生成 `job_key`。首次完成时结果里会出现
`cache_status=stored`；同一任务再次提交且输出文件仍在时返回 `cache_status=cache_hit`，不会重新加载模型或重复 OCR。
如果同一任务正在运行，API 会返回 `status=active_localocr_task`、`job_key` 和
`recommendation=do_not_blindly_retry`；此时先查 `/jobs/<job_key>`、输出目录或后台进程，不要马上再提交一次。
每个结果还包含 `results[].route`，其中 `effective_engine` 是最终引擎，`reason` 是路由原因，
`signals` 是命中的低成本信号，`confidence` 是规则置信度。

## 路由规则（auto 模式）

| 输入 | 引擎 |
|---|---|
| 图片（png/jpg/webp/bmp/tif） | PP-OCRv6_medium |
| 普通扫描 PDF / 表单 / 纯文字 PDF | PP-OCRv6_medium |
| 文件名提示表格、公式、多栏、论文、课件等复杂 PDF | PaddleOCR-VL-1.6 |
| 文件夹 | 按每个文件类型分别路由 |

`--engine` / `-Engine` 决定路由族；`--model` / `-Model` 决定具体 profile。未指定 `model`
时，`ocr` 默认 `ppocrv6-medium`，`vl` 默认 `paddleocr-vl-1.6`，`structure` 默认 `pp-structure-v3`。新增模型时优先新增
`localocr/model_profiles.json` 条目和对应 adapter，不要把模型名写死在调用层。
如果刚改过 profile 或 adapter，先重启 LocalOCR API 再验收，否则旧常驻进程可能仍使用旧 registry。

`structure` 不参与默认 `auto` 分流。它是显式高配：表格、版面块、公式、印章和区域检测需要结构化输出时使用。
当前 PP-StructureV3 在 PaddleOCR 3.7.0 中只接受 `PP-OCRv3/v4/v5`，所以 LocalOCR 的结构化 profile 使用 `PP-OCRv5`，普通 OCR 仍使用 `PP-OCRv6_medium`。

看到“无输出 + exit code 124”时，先检查后台 `localocr.cli` / `vl_subprocess` / `structure_subprocess`、`results[].route`
和输出目录，不要盲目重复提交同一份 PDF。若上一轮已经进入 API，重复请求可能直接返回 `active_localocr_task`
或在完成后返回 `cache_hit`；优先读取返回的 `job_key` 和 `results[].output_files`。

## 输出

拖拽和一次性 CLI：每个输入文件 → `outputs/文件名.txt` + `.md` + `.json`

常驻 API / `ocr_smart.ps1` / `ocr_once.ps1`：返回 JSON 的 `results[].output_files` 记录输出路径，
默认写到 `outputs/api/文件名.txt` + `.md` + `.json`

- TXT：纯文本按页
- MD：带标题层级，表格/公式保留结构
- JSON：含坐标(bbox/polygon)、置信度(score)、阅读顺序(order)、块类型(type)

## 重装/预热模型

```powershell
wsl -d Ubuntu -e bash /mnt/e/Projects/Tools/LocalOCR/scripts/install_wsl.sh
```

## 测试

```bash
scripts/run_in_wsl.sh tests/run_tests.py --allow-heavy
```

`tests/run_tests.py` 是会依次加载 OCR、VL、Structure 模型并写入测试输出与
`TEST_REPORT.md` 的重型 GPU 集成测试。它不是普通健康检查，只有用户明确授权本地
重型集成测试时才可添加 `--allow-heavy`；默认稳定性检查使用 `/health`、单元测试和
单样例 smoke。运行时会通过 LocalGpuBroker 与 Ollama、ChineseASR 排他。

## 故障

GPU 报错 / libcuda 找不到 → 见 `docs/TROUBLESHOOTING.md`。
