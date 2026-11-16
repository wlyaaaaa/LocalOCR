# LocalOCR

在本地把图片、截图和扫描 PDF 转成文字、Markdown 或 JSON。普通页面先用 OCR 识别，逐页检查空文本、低置信和表格公式信号，只把有问题的页升级给本地视觉语言模型；需要表格和版面信息时可选结构化识别。模型在本机离线运行，默认显卡被占时 OCR 改用 CPU，并标记未完成的 VL 升级。

三个入口：把文件拖到 `start.bat`、命令行 `start.ps1`、常驻本地接口 `start_server.ps1`；结果在 `outputs/`。

VL 的 GPU 推理使用 CUDA 延迟加载与数学注意力后端，减少工作进程常驻内存；配置说明见 [模型清单](docs/MODELS.md)。

- **这是什么：** 本机识别图片、扫描 PDF 和截图里的文字。
- **我怎么用：** 把文件拖到 `start.bat`，在 `outputs/` 看结果。
- **怎么知道正常：** 能看到识别出的文字；重要内容对照原图核对。
- **坏了怎么提醒我：** 已配置只读[每晚自检](E:/PCConfig/docs/runtime/nightly-project-checks.md)，仅覆盖登记的检查命令；其他问题仍直接跟 AI 说。
- **让 AI 做什么：** 让 AI 识别指定文件、解释结果和读不清的地方。
- 详细用法见 [AI 使用说明](docs/QUICKSTART_FOR_AI.md)，排查见 [故障说明](docs/TROUBLESHOOTING.md)。

实况接入单在 `docs/live.contract.json`。用 `python -B -m localocr.live_snapshot --data-root <运行数据根> --output docs/live.snapshot.json --runtime-output <仓库外运行目录/live.json> --owner-output <私人配套快照> --sources-output <私人来源登记>` 按需重取；只读任务元数据和文件存在，不加载模型或读取识别正文。静态值没变就保留源时刻和文件；实时模型小输出存到被忽略的 `_server/` 或仓库外。

批量读复用 ChineseASR P08 的 `read_latest_complete_results`（`localocr/retained_results.py`）：一次读取，每原件取最新完整结果，新失败或缺产物不会盖掉旧完整稿。完整仅指 TXT/MD/JSON/客观结果四文件仍在，不代表本次复验内容哈希。原件名和本机打开目标只放私人配套；公开只给聚合数和匿名日期、类型。来源登记随最近二十份产物变化一并更新。

本仓无独占计划任务；现有 PCConfig 共用夜检只跑测试，尚未调用这份按需输出。每晚采样和模型变动触发须由共享接线方接上，手工运行不算已自动刷新。没有自动触发前，按需静态数不设陈旧阈值；接上每晚采样后可配置两天阈值。模型实测七天标旧。

模型无关验证：`python -B -m unittest discover -s tests -v`。Windows wrapper 行为用本机 Windows 或 WSL 的 Windows 互操作；不运行 GPU 验收或私人样例。
