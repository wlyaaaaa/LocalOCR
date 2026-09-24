# 故障排除

以下 Windows 命令在 LocalOCR 项目根目录执行，仓库可位于任意已挂载盘符；WSL 路径使用 `wslpath` 解析。

## 1. GPU 探针失败 / libcuda.so 找不到

**现象**：`The third-party dynamic library (libcuda.so) is not configured correctly`

**原因**：WSL 的 libcuda 在 `/usr/lib/wsl/lib/`，不在默认搜索路径。

**解决**：确认 `scripts/run_in_wsl.sh` 里有 `export LD_LIBRARY_PATH=/usr/lib/wsl/lib:$LD_LIBRARY_PATH`。
需要实机验证时只运行一个已获准的 OCR 样例；正式入口会先持有 LocalGpuBroker 租约，再在 worker 内验证实际 GPU 算子。
不要绕过租约直接启动另一份 Paddle 推理。`/health.gpu_status=not_probed` 是首次作业前的正常状态。

## 2. 模型下载失败 / No available model hosting platforms

**原因**：默认走 HuggingFace，国内不可达，连带把所有源判失败。

**解决**：设置环境变量（已在 run_in_wsl.sh 配好）：
```bash
export PADDLE_PDX_DISABLE_MODEL_SOURCE_CHECK=true
export PADDLE_PDX_MODEL_SOURCE=modelscope
```

## 3. VL 报 DependencyError: requires additional dependencies

**解决**：在隔离候选中按 lock 文件恢复，不污染 active venv。以下是相关依赖名称，确切版本由发布锁文件管理：
```bash
pip install beautifulsoup4 einops ftfy Jinja2 latex2mathml lxml openpyxl \
    premailer regex safetensors scikit-learn scipy sentencepiece tiktoken tokenizers
```
验证：`python -c "from paddlex.utils.deps import is_extra_available; print(is_extra_available('ocr'))"`
应输出 `True`。

## 4. 显存不足

显存与系统内存分别观察；32GB 显存也不能保证任意长文档不溢出。
服务只保留一个模型 worker，换模型先回收旧 worker；重型任务由 LocalGpuBroker 排他。
服务进程树 RSS 超过 30GB 会返回 `memory_limit_exceeded` 并终止该推理，不要求整机其他程序也低于 30GB。
不要为绕过限制关掉 broker、批量降画质或杀其他服务；按真实任务的分页和分辨率需求做有界调整。

## 5. Windows 原生 Paddle GPU 在 Blackwell 上不可用

本项目经本机验证的运行路径是 WSL2 + Linux Paddle 3.4.0 cu129 候选及发布验收，包含 sm_120；
不要混装 Windows wheel、CPU wheel 或其他 CUDA 构建。版本升级以官方支持信息和本机真实回归为准，不能只看基础探针成功。

## 6. PaddleOCR 报 oneDNN / PIR 错误

设置 `export PADDLE_PDX_ENABLE_MKLDNN_BYDEFAULT=0`（已在 run_in_wsl.sh 配好）。

平面截图文字被裁切/错读而自动升级时，检查普通 OCR 的 `use_doc_unwarping` 是否被改回 `true`。
UVDoc 是纸张形变矫正，不是截图锐化；现行普通 OCR 默认关闭，方向和文本行旋转能力仍保留。

## 7. pip 装包超时

清华/阿里云镜像偶有波动。install_wsl.sh 使用 PyPI 与官方 cu129 索引，`--retries 3 --timeout 90`，仅安装独立候选。
可手动换源重试。

## 8. API 服务启动后健康检查超时

**现象**：`start_server.ps1` 等待 `/health` 超时。

**排查**：读取 `_server/localocr-api.log` 和 `_server/wsl-launcher.log`，再检查目标端口的 `/health`。
`start_server.ps1` 使用启动锁、端口检查及独立标准句柄启动 WSL；不要另开重复的后台启动脚本。

常见原因：

- venv 中缺 `fastapi` / `uvicorn` / `python-multipart`，重新运行 `scripts/install_wsl.sh` 或手动安装依赖。
- API 冷启动不加载 Paddle；依赖错误与模型推理错误分开检查。
- 端口 `18665` 被占用时，先查询 `E:\PCConfig` 的端口注册并确认占用者；需要并存时，只选择已验证的空闲端口并显式传给 `-Port`。`18666` 属于 ChineseASR，不是 LocalOCR 的回退端口。
- 如果 `start_server.ps1` 报 `non-LocalOCR service`，说明该端口上的 `/health` 不是 LocalOCR。不要继续等待启动超时；按上一条确认端口所有者和空闲端口，不要硬编码另一个服务的端口。
- `-TimeoutSec` 只控制 HTTP 等待，`-StartupTimeoutSec` 只控制启动等待。缺 `active_jobs` 的旧 health 是 `readiness_unknown`，不得当空闲继续提交。

确需停止时使用下面的身份核验入口；它核对目标端口、项目 cwd、服务模块、PID 和启动时间，不按模糊进程名杀进程：

```powershell
.\stop_server.ps1
```

## 9. `ocr_once.ps1` 长任务请求超时

**现象**：VL、扫描 PDF 或公式图片首次识别时报
`The request was aborted: The operation has timed out.`

区分执行期限与客户端等待：`ExecutionTimeoutSec` 默认 300 秒，是服务从请求开始到结果发布的硬期限；
`TimeoutSec` 默认 3600 秒，只是 HTTP 传输等待。smart 的外层等待默认 330 秒。

**解决**：

```powershell
.\ocr_smart.ps1 "E:\path\scan.pdf" -Engine vl -ExecutionTimeoutSec 600 -OuterTimeoutSec 630 -TimeoutSec 660
```

只有现实工作确需更长时才提高执行期限（最多 7200 秒）；不要用加长 timeout 掩盖卡死。先检查：

```powershell
Invoke-RestMethod http://127.0.0.1:18665/health
Get-Content .\_server\localocr-api.log -Tail 80
```

`active_jobs` 会给出 `job_id`、`job_key`、`stage`、`worker_pid` 和 `deadline_at`。
HTTP 504 / `execution_timeout` 表示服务已中止该 worker 子树；取消和失租约也会回收。
下一次请求会创建干净 worker，不沿用被中断的 Paddle 状态。

## 10. Codex / shell 显示无输出并以 124 退出

`124` 通常是外层 shell / Codex 工具超时，不是 `ocr_once.ps1 -TimeoutSec` 返回。
如果请求是 PDF 且使用 `-Engine auto`，先看 `results[].route` 或短 JSON 里的 route 预览；
复杂 PDF 可能已经进入 VL 推理，普通扫描 PDF / 表单通常会先走 OCR。

Codex / AI 助手优先改用 smart wrapper，它有自己的外层等待上限，会先返回短 JSON 状态：

```powershell
.\ocr_smart.ps1 "E:\path\scan.pdf" -Engine auto -ExecutionTimeoutSec 300 -OuterTimeoutSec 330
.\ocr_smart.ps1 "E:\path\scan.pdf" -TriageOnly
```

常见短状态：

- `triage_only`：只预检，没有提交 OCR。
- `active_localocr_task`：`/health.active_jobs` 报告执行中；先不要重复提交。
- `readiness_unknown`：无法取得完整任务状态，不能当作空闲。
- `client_timeout`：已停止客户端等待，不等于杀了服务任务；先看返回的 job 定位和 `active_jobs`。
- `client_failed`：底层调用失败，看 `stderr_tail` 和 `/health`。

不要立刻重复提交同一份 PDF。先检查：

```powershell
Invoke-RestMethod http://127.0.0.1:18665/health
Get-ChildItem .\outputs\api | Sort-Object LastWriteTime -Descending | Select-Object -First 10
```

如果短 JSON 或成功结果里有 `job_key`，优先直接查任务状态：

```powershell
Invoke-RestMethod "http://127.0.0.1:18665/jobs/<job_key>"
# 只取消这一项运行中的任务
Invoke-RestMethod "http://127.0.0.1:18665/jobs/<job_key>/cancel" -Method Post
```

API 会把写盘任务登记在 `_server/jobs`。同一源文件、模型 profile 和输出目录正在运行时，
第二个请求返回 `status=active_localocr_task`；已完成且输出文件仍存在时，第二个请求返回
`cache_status=cache_hit`。不要把 `cache_hit` 当成“没跑成功”，它表示结果文件已经可用。

简单扫描 PDF、法律表单、送达地址确认书、空白表格和纯文字 PDF 优先使用 `auto`，默认先走 OCR：

```powershell
.\ocr_smart.ps1 "E:\path\scan.pdf" -Engine auto
```

直接 CLI 与 `ocr_once.ps1` 共用相同的 auto 逻辑。VL 升级失败时，首轮 OCR 的可读产物会保留在
`partial/<job_key>` 并随错误返回；这些是部分结果，不应改名为成功或用于证明没有文字。

原先将 GPU 占用也包装为 HTTP 400 的行为已移除：400 是输入错误，409 是任务/GPU 忙，
503 是 broker 不可用或租约丢失，504 是执行期限。wrapper 保留原始 JSON `detail` 和 job 定位。
不要看到非 2xx 就反复提交；按 `error_code` 和实际任务状态处理。

## 11. API 返回 cache_hit 但文件不存在或内容不是预期

`cache_hit` 要求 manifest 的正式输出大小、hash 和 objective sidecar 绑定全部通过复验。
按返回的 `output_files` 打开 request-hash 隔离文件，不读同名兼容副本猜缓存。若用户手动移动、覆盖或清空输出目录，
下一次相同请求会重新跑 OCR 并刷新 manifest。

确需重新生成时用新的精确 `-OutDir`，保留原有结果。`-Force` 只绕过
`ocr_smart.ps1` 的前置后台任务拦截，不绕过 API 的任务缓存和运行中去重。

## 12. 启动 Ollama / 本地大模型前释放显存

LocalOCR 的温热 worker 可能持有最后使用的模型。启动 Ollama、本地大模型或其他重 GPU 任务前：

```powershell
.\release_resources.ps1
```

如果是一次性 OCR，也可以直接：

```powershell
.\ocr_once.ps1 "E:\path\image.png" -StopAfter
```

只有 stop 的 PID/启动身份与端口核验全部成功，release 才会报告已释放。
`-StopAfter` 清理失败返回 `resource_release_failed` 并保留 `ocr_result`，不能把识别成功冒充资源释放成功。

## 13. PP-StructureV3 报 Invalid OCR version

**现象**：把结构化管线配置成 `ocr_version="PP-OCRv6"` 时，报：

```text
ValueError: Invalid OCR version: PP-OCRv6. Supported values are ['PP-OCRv3', 'PP-OCRv4', 'PP-OCRv5'].
```

**原因**：当前 PaddleOCR 3.7.0 的 `PPStructureV3` 只支持 `PP-OCRv3/v4/v5`。

**解决**：LocalOCR 的 `pp-structure-v3` profile 固定使用 `PP-OCRv5`。普通图片 OCR 仍用 `PP-OCRv6_medium`；
不要为了统一版本把结构化 profile 改成 `PP-OCRv6`。

## 14. 换版、完整性与取消

换模型后仍读旧结果，检查真实 `artifact_paths`、`revision`、`execution_identities` 以及运行中的 source snapshot；不删除全部历史结果掩盖绑定缺陷。`page_coverage_mismatch` 或 alignment error 必须修适配，不补空页/强行错配坐标。`execution_cancelled` / `retryable=false` 必须停止，不换引擎重试。候选失败保持原环境，不把失败结果称为有效回滚。升级、激活与回滚命令见 [UPGRADING.md](UPGRADING.md)。
