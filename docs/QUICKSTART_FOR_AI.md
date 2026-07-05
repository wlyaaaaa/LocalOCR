# AI 助手快速上手 LocalOCR

> 本文件写给 AI 助手（和人类）看：如何在本机启动这套本地 OCR。

## 一句话

在 Windows 里把图片/PDF/文件夹拖到 `E:\LocalOCR\start.bat` 上即可。结果在 `E:\LocalOCR\outputs\`（每个文件产出 `.txt`/`.md`/`.json`）。

## 环境已就绪

- 运行环境：WSL2 Ubuntu 24.04，venv 在 `/root/localocr-venv`
- PaddlePaddle GPU 3.3.1 (cu129) + PaddleOCR 3.7.0 已装
- 模型已下载到 `/root/.paddlex/official_models/`（PP-OCRv6_medium + PaddleOCR-VL-1.6），可离线
- GPU：RTX 5090D，Blackwell sm_120，已验证可用

## 命令行

在 Windows PowerShell：

```powershell
# 自动分流（图片→PP-OCRv6_medium，PDF→VL-1.6）
.\start.ps1 "E:\某文件夹"
.\start.ps1 "E:\某图片.png"
.\start.ps1 "E:\某文档.pdf"

# 强制引擎
.\start.ps1 "E:\某文件" --engine ocr   # 强制 PP-OCRv6_medium
.\start.ps1 "E:\某文件" --engine vl    # 强制 PaddleOCR-VL-1.6
```

等价的 WSL 命令：

```bash
cd /mnt/e/LocalOCR
scripts/run_in_wsl.sh -m localocr.cli "路径" --engine auto --out-dir outputs
```

## 常驻 API（推荐）

高频 OCR、Codex 调用、批量读取课程图片/PDF 时，优先启动常驻服务：

```powershell
E:\LocalOCR\start_server.ps1
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:8765/health
```

识别一个路径：

```powershell
E:\LocalOCR\ocr_once.ps1 "E:\LocalOCR\tests\samples\sample_chat_screenshot.png" -Engine ocr
```

VL、PDF、公式或首次冷启动可能较慢，调用时保留默认 `-TimeoutSec 3600`，或显式传入：

```powershell
E:\LocalOCR\ocr_once.ps1 "E:\LocalOCR\tests\samples\sample_table.png" -Engine vl -TimeoutSec 3600
```

注意：API 进程只常驻缓存 PP-OCR；VL/PDF 由隔离子进程执行。`/health` 里没有 `vl`
不代表 VL 不可用，以一次 `-Engine vl` 实际调用结果为准。

停止服务：

```powershell
E:\LocalOCR\stop_server.ps1
```

API 请求体：

```json
{
  "path": "E:\\LocalOCR\\tests\\samples\\sample_chat_screenshot.png",
  "engine": "auto",
  "recursive": false,
  "write_outputs": true
}
```

## 路由规则（auto 模式）

| 输入 | 引擎 |
|---|---|
| 图片（png/jpg/webp/bmp/tif） | PP-OCRv6_medium |
| PDF | PaddleOCR-VL-1.6 |
| 文件夹 | 按每个文件类型分别路由 |

## 输出

拖拽和一次性 CLI：每个输入文件 → `outputs/文件名.txt` + `.md` + `.json`

常驻 API / `ocr_once.ps1`：返回 JSON 的 `results[].output_files` 记录输出路径，
默认写到 `outputs/api/文件名.txt` + `.md` + `.json`

- TXT：纯文本按页
- MD：带标题层级，表格/公式保留结构
- JSON：含坐标(bbox/polygon)、置信度(score)、阅读顺序(order)、块类型(type)

## 重装/预热模型

```powershell
wsl -d Ubuntu -e bash /mnt/e/LocalOCR/scripts/install_wsl.sh
```

## 测试

```bash
scripts/run_in_wsl.sh tests/run_tests.py   # 跑4项测试，更新 TEST_REPORT.md
```

## 故障

GPU 报错 / libcuda 找不到 → 见 `docs/TROUBLESHOOTING.md`。
