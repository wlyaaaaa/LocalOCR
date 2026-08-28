# 架构说明

## 执行边界

```text
Windows wrapper / CLI
  → API/CLI coordinator：输入、期限、任务状态、输出提交
  → LocalGpuBroker：全机 GPU 排他租约
  → 一个可终止的 warm worker：GPU 探针、模型、PDF 渲染、推理
  → 请求绑定的输出文件 + 最后提交的 job manifest
```

不新增服务、队列或数据库。API 不导入 Paddle，也不持有模型；`runtime.py` 用私有 Pipe
与一个 spawn 工作进程通信。同模型连续请求热复用，切换模型时结束旧进程再创建新进程，
避免多套模型和 native allocator 长期叠加。CLI 使用同一 `OCRService`，不另维护一条推理路径。

## 任务与资源生命周期

- 整个请求默认执行期限 300 秒，可用 API `timeout_sec`、CLI `--timeout-sec` 或
  wrapper `-ExecutionTimeoutSec` 明确调整，最大 7200 秒；目录内的每个文件不会重置期限。
- 服务只允许一个在途计算请求。相同已完成请求可复用哈希有效的缓存；计算忙碌时返回
  HTTP 409、`active_localocr_task` 与任务定位，不隐式排入第二个队列。
- API 持有短 TTL 租约并续租；worker 在 GPU 探针和模型导入前验证父租约的真实 token。
  不再接受“父已持租约”的未验证布尔标志。续租失败必须传播到监督器。
- deadline、取消、租约丢失、worker 崩溃和服务关闭都会停止进程组。WSL worker 的
  parent-death 保护和同组小 guard 同时覆盖父进程硬退出及普通子孙进程。
- RSS 上限为服务及其子进程合计 30 GB，不是整机限制，也不是 GPU VRAM 指标。
  超限只结束自己的工作进程，保留其它程序。OOM 等 native 错误明确失败。
- `/health` 返回 `active_jobs`、阶段、PID、期限、当前模型与服务内存峰值。
  `gpu_status=not_probed` 表示 API 就绪但尚未在合法租约内执行 GPU 探针，不冒充 GPU 已验证。

Windows 启动器仅把显式允许的 NUL/独立日志句柄传给长驻 `wsl.exe`，不能继承调用者的
输出管道。没有 PowerShell scriptblock 异步流回调。停止入口校验目标端口、服务 PID、
启动时间、命令及工作目录，再结束该服务子树；不按模糊进程名批量终止。

## 输入、状态与输出

选定文件先生成源哈希，然后只为该文件创建临时不可变输入快照。快照哈希和识别完成时
原件哈希必须一致；原件变化时不发布结果。临时快照和 PDF 页属于该请求，退出时清理。

`JobRegistry` 仍使用 `_server/jobs/<job_key>.json` 与原子 `.lock`。锁记录 execution id、
PID 和进程启动时间；启动时可以立即恢复已死亡/被复用 PID 的任务，不等待 24 小时。
活进程的锁不会因年龄而被抢占。进度仅更新现有 manifest，不新增状态数据库。
目录内一个 `.job-registry.guard` 仅串行化元数据提交；每个 claim/terminal 都校验 execution id，
Windows 不会在锁住数据文件时重开/删除它。终态落盘失败保留可恢复锁，协调者明确 not-ready。
`write_files=False` 不落任务或输出文件。

`job_key` 绑定源路径/hash/大小、请求语义、规范化设备、路由策略、模型 profile、配置和输出目录。
所有输出采用临时文件加原子替换，completed manifest 是最终提交点。缓存只依赖
请求 hash 隔离的 canonical 投影和 objective sidecar；同名 stem 的兼容展示文件被覆盖
不会使其它请求失去有效缓存。

auto 首轮 OCR 有结果而 VL 升级失败时，初步文字保存在明确的 `partial/<job_key>` 路径，
失败 manifest/HTTP 回应给出 `partial_output_files`；它不作为成功缓存或已完成 VL 的证明。

## 模型和结果合同

默认 profile 仍是 `ppocrv6-medium`、`paddleocr-vl-1.6`、`pp-structure-v3`。
`engine=auto` 用低成本规则选首轮模型，普通图片/扫描件先 OCR，明确困难结果再升级 VL；
显式 `engine` 或 `model` 保持调用者选择，结构化任务使用 `structure`。
VL 的 `use_queues=False` 与当前单文件/逐页调用匹配，避免不必要的内部异步队列；模型能力不降级。
安装预热也复用同一个监督路径和 profile，需显式 `--allow-heavy`，不在安装脚本另留无租约的 GPU 探针。

JSON 保留 `bbox`、`rect`、`polygon`、阅读顺序、表格/公式/印章和结构细节。
PDF 坐标明确是渲染像素，并记录 `render_scale` 与尺寸，不伪装为原 PDF 点坐标。
`face/person` 等非文字区域只记录区域标签，OCR 不作人脸身份识别。

`media.objective-result.v1` 继续区分 `text_detected`、`no_text_detected`、`indeterminate`。
空文本不能证明没有文字；负向结论必须有完整覆盖、质量与独立证据，且 source/request/model/config
以及所有输出 size/hash 绑定有效。失败、低置信度与部分覆盖不被改写成成功。

## 入口与模块

| 模块/入口 | 责任 |
|---|---|
| `ocr_smart.ps1` / `ocr_once.ps1` | health-only 预检、HTTP、期限、结构化错误和 job 定位 |
| `server.py` / `cli.py` | 薄调用入口；共同使用 `OCRService` |
| `service.py` | 单在途协调、快照、路由、提交与取消 |
| `runtime.py` | warm worker、模型切换、期限/失联/崩溃/内存监督 |
| `gpu_broker.py` | 全机租约客户端与父 token 验证 |
| `job_registry.py` | 原子任务锁、进度、终态与缓存校验 |
| `model_registry.py` / `model_profiles.json` | profile 和配置，不把具体模型写死在 wrapper |
| `engines/` / `pdf_utils.py` | 模型 adapter 与临时逐页渲染 |
| `outputs.py` / `objective_result.py` | 原子投影、客观结果和哈希绑定 |

| HTTP 入口 | 结果 |
|---|---|
| `GET /health` | API 身份、active_jobs、worker/模型与资源状态 |
| `GET /jobs/{job_key}` | 持久任务状态及 cache 可用性 |
| `POST /jobs/{job_key}/cancel` | 取消准确的在途任务；不存在/已终态不影响其它任务 |
| `POST /ocr/path` / `POST /ocr/file` | 路径/上传识别；上传识别也不阻塞异步健康接口 |

HTTP 409 表示忙碌/冲突，503 表示 broker 不可用或租约丢失，504 表示执行期限，
400/422 表示请求参数，404 表示原件/任务不存在，500 表示实际运行异常。
错误的 `error_code`、`detail` 和已有 `job_key` 位于 JSON 顶层，Windows wrapper 原样保留。
