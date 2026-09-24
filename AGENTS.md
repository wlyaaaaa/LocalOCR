# LocalOCR 项目约定

- 识别、路由与输出语义以 README 和 `docs/ARCHITECTURE.md` 为准。正常 AI 调用用 `ocr_smart.ps1`；只有结构化需求才选 `structure`，不按文件名猜模型。
- 所有 GPU 工作经过 LocalGpuBroker，保留取消、期限、进程树清理及原始识别证据。空文本不等于“无文字”，局部结果不等于完整覆盖。
- API 只监听本机，保持 CLI、JSON 请求、文件上传和精确任务取消兼容。跨 Windows/WSL 的用户参数以独立参数传递，不拼成 shell 代码；项目路径从入口目录解析。
- 正式运行使用独立源码快照。源码、测试与当前运行版本分别核对；运行时升级/回滚遵循 `docs/UPGRADING.md`，不原地修改 current 或 previous。
- 模型无关验证用 `python -B -m unittest discover -s tests -v`；Windows wrapper 行为测试需要 Windows 或 WSL 的 Windows 互操作，普通 Linux CI 显式跳过。真实 GPU 验收只用公开或合成样例，不读取私人图片作为默认测试集。
- 不把模型权重、输入、OCR 输出、任务状态、运行日志和机器私有配置提交到公开仓库。保留并发改动；含中文的 PowerShell 脚本使用 UTF-8 BOM。
