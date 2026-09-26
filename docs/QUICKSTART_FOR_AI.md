# AI 助手快速上手 LocalOCR

以下 Windows 命令在 LocalOCR 项目根目录执行，仓库可位于任意已挂载盘符；WSL 路径使用 `wslpath` 解析。

> 本文件写给 AI 助手（和人类）看：如何在本机启动这套本地 OCR。

## 一句话

在 Windows 里把图片/PDF/文件夹拖到 `.\start.bat` 上即可。结果在 `.\outputs\`（每个文件产出 `.txt`/`.md`/`.json`）。

## 环境已就绪

- 运行环境：WSL2 Ubuntu 24.04，通过 `/root/localocr-runtimes/current` 选择已验收环境；旧 `/root/localocr-venv` 保留为兼容/回滚入口
- 目标发布为 PaddlePaddle GPU 3.4.0 (cu129) + PaddleOCR 3.7.0，实际激活状态以 release inspect 和 PCConfig 为准
- 模型已下载到 `/root/.paddlex/official_models/`（PP-OCRv6_medium + PaddleOCR-VL-1.6 + PP-StructureV3 组件），可离线
- 模型选择已通过 `localocr/model_profiles.json` 解耦；`ocr` / `vl` / `structure` 是默认 profile 别名，可用 `--model` / `-Model` 指定具体 profile
- GPU：RTX 5090D，Blackwell sm_120，已验证可用

## 命令行

在 Windows PowerShell：

```powershell
# 单次 CLI（与 API 共用路由、监督执行和结果绑定）
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
cd "<LocalOCR 的 WSL 根目录>"
scripts/run_in_wsl.sh -m localocr.cli "路径" --engine auto --out-dir outputs --timeout-sec 300
```

## 常驻 API（推荐）

高频 OCR、Codex 调用、批量读取课程图片/PDF 时，优先启动常驻服务：

```powershell
.\start_server.ps1
```

Codex / AI 助手默认先用 smart wrapper，避免 PowerShell 长时间卡住当前回合：

当前自动入口使用 Smart Router v5；普通页面先走 OCR，只在逐页结果需要时升级。

```powershell
.\ocr_smart.ps1 "E:\path\scan.pdf" -Engine auto -ExecutionTimeoutSec 300 -OuterTimeoutSec 330
```

`ocr_smart.ps1` 只以 `/health.active_jobs` 判断 API 是否忙；字段缺失或无法读取是 `readiness_unknown`，不是空闲。
CLI 与 API 共用 `auto` 分流：简单扫描 PDF、法律表单、送达地址确认书、空白表格和纯文字 PDF
先走 `ocr`；逐页判断空文本、低置信及表格/公式内容信号，仅升级问题页到本地 `vl`，不按文件名猜版式。
复杂版面、表格、公式、多栏材料也可以显式传 `-Engine vl`。
需要表格 HTML、版面块、公式、印章和区域坐标时显式传 `-Engine structure`。
如果用户指定具体模型，用 `-Model <profile-id>`；显式模型始终优先，不会被 Smart Router 改写。

普通 OCR 不默认启用 UVDoc：平面截图会被不必要的形变矫正误伤，进而触发低置信升级。
不要为提高“配置档位”把它重新全开；真正弯曲纸张才考虑显式 profile 矫正，并复核坐标对应的图像空间。

只想省 token 做预检，不提交 OCR：

```powershell
.\ocr_smart.ps1 "E:\path\scan.pdf" -TriageOnly
```

健康检查：

```powershell
Invoke-RestMethod http://127.0.0.1:18665/health
```

查询某个任务：

```powershell
Invoke-RestMethod "http://127.0.0.1:18665/jobs/<job_key>"
# 只取消这个执行中的任务，不停止其他服务
Invoke-RestMethod "http://127.0.0.1:18665/jobs/<job_key>/cancel" -Method Post
```

底层 wrapper 仍可直接识别一个路径：

```powershell
.\ocr_once.ps1 "tests/samples/sample_chat_screenshot.png" -Engine ocr
```

默认执行期限是 300 秒，覆盖加载模型、识别和同一请求中的所有文件；超时会终止整个推理子树并释放租约。
确有更长任务时，同时给执行和客户端等待足够时间（执行上限 7200 秒）：

```powershell
.\ocr_smart.ps1 "E:\path\scan.pdf" -Engine vl -ExecutionTimeoutSec 600 -OuterTimeoutSec 630 -TimeoutSec 660
```

`-TimeoutSec` 仅是 HTTP 传输等待；`-OuterTimeoutSec` 是 smart 的客户端总等待；
`-StartupTimeoutSec` 是 API 冷启动等待（默认 600 秒）。客户端退出不等于服务任务终止，先回查 job，勿盲目重交。

API 本身不导入 Paddle；OCR/VL/Structure 共用一个可替换的温热 worker，同模型复用、换模型回收。
`gpu_status=not_probed` 是尚未有 GPU 作业的正常状态，不代表 CPU 降级。`loaded_models` 只表示当前驻留模型。
`active_jobs` 给出 job、stage、model、source、deadline、worker PID；服务进程树 RSS 上限为 30GB，不限制整机其他程序。

如果只是一次性读取图片/PDF，或马上要启动 Ollama/本地大模型，可以让调用结束后自动释放：

```powershell
.\ocr_once.ps1 "E:\某图片.png" -Engine auto -StopAfter
```

启动其他重 GPU 任务前，也可以显式释放：

```powershell
.\release_resources.ps1
```

停止服务：

```powershell
.\stop_server.ps1
```

API 请求体：

```json
{
  "path": "E:\\Projects\\Tools\\LocalOCR\\tests\\samples\\sample_chat_screenshot.png",
  "engine": "auto",
  "model": "ppocrv6-medium",
  "recursive": false,
  "write_outputs": true,
  "timeout_sec": 300
}
```

API 写盘任务会按源文件路径、文件内容、请求语义、路由策略、模型 profile 和输出目录生成 `job_key`。首次完成时结果里会出现
`cache_status=stored`；同一任务再次提交且输出文件仍在时返回 `cache_status=cache_hit`，不会重新加载模型或重复 OCR。
新结果同时带有 `objective_outcome`、`execution_status`、`coverage`、`quality` 和 `failure`；空文本返回
`objective_outcome=indeterminate`，不能当作 `no_text_detected`。`display_summary` 把这些客观字段、文字块数量、
平均置信度和自动升级状态投影成一条人话说明；它不改变 objective 结论，也不替代原始文字与坐标。写盘时会生成按 `job_key` 隔离的
`*.objective.json` sidecar，只有 sidecar 的 schema、`size_bytes`、hash、raw/request/model/config 身份及全部输出的
非空 `size_bytes`/`sha256` 均通过复验才可报告 cache hit；内存结果的负向证据仍是 `not_persisted`。
如果同一任务正在运行，API 会返回 `status=active_localocr_task`、`job_key` 和
`recommendation=do_not_blindly_retry`（HTTP 409）；此时先查 `/jobs/<job_key>` 与 `/health`，不要马上再提交一次。
GPU 冲突也返回 409；broker 不可用/失租约为 503；执行超时为 504。读取完整 `error_code`、`detail` 和 job 定位，不能统称为 HTTP 400。
`write_outputs=false` 不登记任务或写正式结果。auto 升级失败时，首轮 OCR 只保存在 `partial/<job_key>`，仍返回失败且不能当成正式成功缓存。
每个结果还包含 `results[].route`，其中 `effective_engine` 是最终引擎，`reason` 是路由原因，
`signals` 是命中的信号，`confidence` 在自动路线上为 `null`，不是校准正确率。auto 首轮 OCR 还包含 `difficulty` 和
`escalated`；发生升级时 `escalation` 会记录原模型与最终 VL 模型。

## 路由规则（auto 模式）

| 输入 | 引擎 |
|---|---|
| 图片（png/jpg/webp/bmp/tif） | PP-OCRv6_medium |
| 普通扫描 PDF / 表单 / 纯文字 PDF | PP-OCRv6_medium |
| 只有文件名变化 | 不改变路由；已知复杂版面可显式选择 VL |
| auto 中有问题或表格/公式信号的页面 | 仅这些页面本地升级到 PaddleOCR-VL-1.6 |
| 文件夹 | 按每个文件类型分别路由 |

`--engine` / `-Engine` 决定路由族；`--model` / `-Model` 决定具体 profile。未指定 `model`
时，`ocr` 默认 `ppocrv6-medium`，`vl` 默认 `paddleocr-vl-1.6`，`structure` 默认 `pp-structure-v3`。新增模型时优先新增
`localocr/model_profiles.json` 条目和对应 adapter，不要把模型名写死在调用层。
修改开发源中的 profile 或 adapter 后，必须创建并验证新候选，切换后重启服务；正式版本从固定源码快照运行，不能只改开发文件就声称生效。

`structure` 不参与默认 `auto` 分流。它是显式高配：表格、版面块、公式、印章和区域检测需要结构化输出时使用。
当前 PP-StructureV3 在 PaddleOCR 3.7.0 中只接受 `PP-OCRv3/v4/v5`，所以 LocalOCR 的结构化 profile 使用 `PP-OCRv5`，普通 OCR 仍使用 `PP-OCRv6_medium`。

看到“无输出 + exit code 124”时，先检查 `/health.active_jobs`、`results[].route`
和输出目录，不要盲目重复提交同一份 PDF。若上一轮已经进入 API，重复请求可能直接返回 `active_localocr_task`
或在完成后返回 `cache_hit`；优先读取返回的 `job_key` 和 `results[].output_files`。

## 输出

CLI 和 API 都以返回的 `results[].output_files` 为准，正式结果按源文件和 request hash 隔离。
无 hash 的同名 TXT/MD/JSON 只是兼容显示副本，不能用来判断缓存或原件身份。

- TXT：纯文本按页
- MD：带标题层级，表格/公式保留结构
- JSON：含坐标(bbox/polygon)、置信度(score)、阅读顺序(order)、块类型(type)

## 重装/预热模型

```powershell
$projectWsl = wsl.exe -d Ubuntu -e wslpath -a -u $PWD.Path
wsl.exe -d Ubuntu -e bash "$projectWsl/scripts/install_wsl.sh"
```

## 测试

```bash
scripts/run_in_wsl.sh -m unittest discover -s tests -q
# 明确需要跨模型实机验收时才运行：
scripts/run_in_wsl.sh tests/run_tests.py --allow-heavy
```

`tests/run_tests.py` 是会依次加载 OCR、VL、Structure 模型并写入测试输出与
`quality.json` 的重型 GPU 集成测试。它不是普通健康检查，只有用户明确授权本地
重型集成测试时才可添加 `--allow-heavy`；默认稳定性检查使用 `/health`、单元测试和
单样例 smoke。运行时会通过 LocalGpuBroker 与 Ollama、ChineseASR 排他。

## 故障

GPU 报错 / libcuda 找不到 → 见 `docs/TROUBLESHOOTING.md`。

## 发布与结果边界

遵循 [UPGRADING.md](UPGRADING.md)。测试使用非私人合成标准答案并禁用结果缓存；CER 只描述所测样本。每页模型和坐标参照必须保留，`first_pass_evidence` 不应被第二模型覆盖。`execution_cancelled`、`status=cancelled`、`retryable=false` 必须停止处理。中断检查点和失败升级首轮输出只是部分证据；批量跳过项查看 `skipped_inputs` 与 `batch_coverage`。
