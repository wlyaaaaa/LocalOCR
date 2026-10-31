# 模型与运行版本升级

目标是可复现、可验收、可回滚，不是启动时自动追逐最新版。

## 一次发布包含什么

运行环境目录包含 `bin/python`、锁定的依赖、`app/` 源码快照，以及 `acceptance/` 验收结果。`current` 选择正式版本，`previous` 保留上一版本。源码快照只复制项目代码、操作文档、测试和公开合成样本，不复制用户输入、输出、任务、凭据或 `.git`。

正式运行保持原项目 cwd；通过 Python safe-path 与版本源码的 PYTHONPATH 导入，不从可变开发目录加载代码。`LOCALOCR_PROJECT_ROOT` 指向原项目的数据目录，保留现有端口、任务和输出路径。开发仓库中的修改不会自动进入已激活版本，必须新建候选。

模型文件不重复拷贝到每个 venv。Profile 的实际权重、配置和分词器路径参与指纹；新权重必须放入独立版本目录，并将 adapter 的模型路径 options 和 `artifact_paths` 指向相同实际文件。不得覆盖仍被旧版本引用的权重。

## 安装和验证

先在开发项目中更新 profile/adapter/相关测试。依赖升级在隔离环境中完成，确认版本集合后更新 `requirements/runtime-paddle-cu129.lock.txt`，不要对 current 运行 pip upgrade。

```bash
cd "<LocalOCR 的 WSL 根目录>"
bash scripts/install_wsl.sh <unique-candidate-name>
```

安装器创建独立环境并固定源码，但不加载 GPU 模型、不自动激活。首次缺少模型时，在已获准的重型工作范围内显式预下载：

```bash
export LOCALOCR_RUNTIME=/root/localocr-runtimes/<unique-candidate-name>
bash scripts/run_in_wsl.sh scripts/download_models.py --allow-heavy
bash scripts/run_in_wsl.sh scripts/manage_runtime.py validate --allow-heavy
```

已有隔离候选尚无 `app/` 时，可以一次性执行 `scripts/manage_runtime.py freeze`。目标已存在就停止，不覆盖快照；正式或保留回滚版本需要修改时创建新候选。验收失败且从未激活的候选，可确认无使用者、保留失败回执后丢弃其临时快照并重建，不能借此修改正在使用或已验收的版本。配置、实现或权重在验证中发生变化，会使验证失效。

验收分开检查依赖、模型无关测试与真实 GPU 合成质量。`acceptance/quality.json` 记录每个案例的 CER、关键字段、实际页数、表格单元格、路由、耗时和错误。合成回归并不代表现实所有文档准确，也不是不同厂商模型的通用排行榜。置信度和完整性不是逐字准确率。

质量基准禁用结果缓存；冷启动、热模型复用和缓存命中不能混作同一速度指标。测试失败返回非零，不能只凭进程退出或报告文件存在判定通过。用户取消是不可自动重试的终态。

## 激活、实机回读与回滚

先在 Windows 使用现有身份校验停止入口，确认没有在途工作，不强制杀别的服务：

```powershell
.\stop_server.ps1
```

随后在 WSL 激活当前所选候选：

```bash
LOCALOCR_RUNTIME=/root/localocr-runtimes/<unique-candidate-name> \
  bash scripts/run_in_wsl.sh scripts/manage_runtime.py activate
```

激活必须匹配该候选实际源码、依赖和模型的通过回执，且服务已停止。切换采用原子链接，不回写开发源码。之后正常 `start_server.ps1`，用已知非私人样例经 Windows wrapper 验证真实推理、输出绑定、缓存复用和资源释放。`/health` 成功只是协调器就绪，不等于 GPU 或质量验收。

```bash
bash scripts/run_in_wsl.sh scripts/manage_runtime.py inspect
```

它返回实际解释器、源码根、依赖和执行身份。不要只看磁盘上哪个版本目录最新。

回滚同样先停止服务，再执行：

```bash
bash scripts/run_in_wsl.sh scripts/manage_runtime.py rollback
```

回滚由 previous 版本自己的源码和解释器验证自己的回执；缺失、失败或过期均拒绝切换。旧环境存在但测试未通过，不能称为有效回滚。保留原环境与失败证据，修复后重新验证，不能伪造通过回执或关闭检查。

## 退休环境清理

成功激活后只保留 current、previous 两代已激活环境。原 previous 的目录身份和验收证据先原子写入运行根目录的 `.retirements.json`，切换完成后尝试清理；从未激活的候选不会自动加入。回滚目标仍受 current/previous 保护。清理失败会记录原因并留待下一次激活或显式重试，激活回执仍如实报告已切换。

既有旧版本由操作者核实曾经激活后，可显式收养并成组清理。用 current 的解释器直接执行开发副本脚本，即可使用新入口，不必改动冻结源码或重跑 GPU 验收：

```bash
/root/localocr-runtimes/current/bin/python -P /absolute/development/scripts/manage_runtime.py \
  retire --runtime-root /root/localocr-runtimes --adopt <old-release-a> --adopt <old-release-b>
# 重试已登记的退休项，不再收养候选：
/root/localocr-runtimes/current/bin/python -P /absolute/development/scripts/manage_runtime.py \
  retire --runtime-root /root/localocr-runtimes
```

运行前先用现有停止入口停服务；设置 `LOCALOCR_PROJECT_ROOT` 指向现有项目数据目录，以核对未结束的作业。每个目标均重新核对 current/previous、目录设备号和 inode、venv 配置、源码快照及匹配的通过回执，并检查 Linux `/proc` 的解释器、工作目录、参数、环境、映射及文件句柄。服务、作业或进程占用、链接、越界和同名目录替换均保留并说明原因。删除中途失败时按已登记的目录身份重试，允许已删除的证明文件缺失。清单和锁仅为短命令使用，不新增后台任务。硬链接只移除相应目录项；仍被其他版本引用的数据块保留，不按目录大小承诺磁盘回收量。

## 换模型的最小变更

同家族且接口兼容：新增 profile、实际权重目录和版本，再跑上述流程，通常不必改服务/CLI/Skill。新框架：增加明确 adapter，声明 backend 和真实输出能力，隔离安装；缺分数、几何或表格结构就保留缺失，不伪造字段。当前 default 为 OCR、VL、Structure 三条路线，新增候选不会自动晋升。

PDF/TIFF 的输入页数独立核验；每页渲染受上限约束，超时/取消可保留原子检查点作为部分证据。自动路由不按文件名猜版式，按页升级并保留首轮文字和坐标；非线性矫正坐标不能冒充原图坐标。
