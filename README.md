# Hypit 内部视频助手

公司视频制作工具：根据文字、图片与可选本地参考视频理解需求、规划、生成、审查与合成视频。

**接手开发请先读：[开发交接文档](DEVELOPMENT_HANDOFF.md)。** 包含项目用途、功能现状、最新用户需求、代码结构、已确认问题、建议改法与验收要求。

**文字要求选填，至少提供文字、图片或参考视频中的一项。** 当前明确要求重点覆盖五种组合：**文字、文字＋图片、文字＋参考视频、文字＋图片＋参考视频、图片＋参考视频（不填文字）**，自动交付可预览下载的完整视频。现有七种非空输入入口继续保留；第一批已完成，第二批预算/素材预检/失败新版及第三批身份基准/全片配乐库已实施；准确旁白、字幕和真实验收仍待完成。详细方案见[完整视频交付改造方案](docs/PRODUCT_DELIVERY_PLAN_2026-09-28.md)。

**本轮执行进度：[开发计划与实施记录](docs/DEVELOPMENT_PLAN_2026-09-28.md)。** 新任务使用workflow_version=4；历史任务不自动升级。

## 当前状态

项目正在进行生产化改造，**尚未完成生产和真实成片稳定性验收**。

- 已有七种输入组合的代码入口、主体理解、参考分析、时长规划、生成和合成流程。
- 本轮加入 PostgreSQL 接入、独立 Worker、错误分类、有限连接恢复、请求去重、并发容量预留、数据库账号、权限与会话撤销。
- 9月28日新任务使用workflow_version=4：先形成统一创作要求，再规划、生成、审查和合成；参考分风格借鉴、改编和明确复刻。保留v3任务的执行租约、回执、版本与原有参考约束。
- 真实生成仍依赖理解模型、视频供应商与可访问素材地址。代码测试通过不代表当前供应商的所有输入组合均已稳定生成合格视频。
- Docker/Linux 渲染、持久素材域名、真实成片矩阵和 72 小时运行观察待部署验收。

## 文档入口

- [当前开发计划与执行记录](docs/DEVELOPMENT_PLAN_2026-09-28.md)
- [素材、预算、失败恢复和全片配乐操作说明](docs/OPERATIONS_AND_MEDIA.md)
- [9月27日历史实施计划](docs/STABILIZATION_PLAN_2026-09-27.md)
- [9月27日改造与剩余验收](docs/STABILIZATION_PROGRESS_2026-09-27.md)
- [同事部署指南](docs/DEPLOYMENT.md)
- [真实验收清单](docs/ACCEPTANCE.md)
- [本轮实现与验证记录](docs/IMPLEMENTATION_STATUS.md)

## 本地启动

需要 Python 3.12、Node.js（本轮验证版本 26.5.0）、FFmpeg/FFprobe；Hypit 与 Harness 由 package-lock.json 固定。Linux/非 Homebrew 环境需通过 FFMPEG_PATH 与 FFPROBE_PATH 指定可执行文件。

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-lock.txt
npm ci
sh scripts/start-local.sh
```

模型密钥由服务器环境 ARK_API_KEY、AUTODL_API_KEY 提供，不交给浏览器。现有开发机兼容读取相邻 OpenMontage/.env 和私有 data/autodl-video-key；新部署应使用环境变量。请先配置密钥再发起真实生成。

本地启动脚本使用 SQLite，分别运行 API、Worker 和签名素材服务；按 Ctrl+C 停止，或在另一个终端执行 `.venv/bin/python -m scripts.stop_local`。素材服务仍只绑定本地地址；参考生成需要另行配置供应商可访问的持久素材地址。本地签名服务启动成功不等于公网通道已可用。

生产要求 PostgreSQL 和独立执行进程。手动启动时，API 与 Worker 共享相同的数据目录和数据库配置：

```sh
export VIDEO_AGENT_EMBEDDED_WORKER=0
.venv/bin/python -m app.worker_main
```

另开终端启动 API。一个数据库目前只允许一个调度进程，内部默认同时执行两个任务；多主机 Worker 集群尚未实现。

## 输入与输出

图片最多 6 张；本地参考视频最长 180 秒、最大 150 MB。人物生成能力尚未完成真实验收；暂不支持社交平台链接解析，指定片段修改入口默认关闭。

成片时长可选自动、8/15/20/30/45/60 秒。新任务自动模式优先遵守明确的文字时长，其余按内容规划；明确要求跟随参考时采用原时长；支持严格时长或约 ±1 秒。单次生成单元遵循当前适配器限制，多单元通过真实衔接检查和合成形成成片。

完整视频必须通过当前版本审核才显示完成；候选可播放不代表质量合格。审片模型也可能判断错误，需要真实样例与人工复核校准。

## 故障恢复与并发

- 理解模型连接故障有限自动恢复；等待远端生成时保存下次查询时间，释放本地执行槽。
- 生成提交前保存意图和预算，取得回执后继续查询原任务。
- 提交结果不明时保留预算和核对记录，阻止该任务再次生成；它不被伪装成已确认运行的任务。未知提交累计到 VIDEO_AGENT_MAX_UNCERTAIN_SUBMISSIONS（默认5条）时停止新增收费生成，需管理员核对。
- 任务领取有租约和执行令牌；旧执行者不能覆盖状态或再次预留提交。新项目固定适配器源码快照，升级不会静默改变在途请求。
- 任意已保存生成版本可立即播放、下载。一个生成单元只能选择一个采用版本，候选合成不会再次收费生成，也不会自动标记质量合格。
- Idempotency-Key 保护网页创建任务，手动继续和回答使用原子状态检查。
- VIDEO_AGENT_CONCURRENT_JOBS 控制本地任务槽，VIDEO_AGENT_USER_CONCURRENT 控制单用户并发，VIDEO_AGENT_REMOTE_CONCURRENT 控制远端生成容量。
- VIDEO_AGENT_DAILY_GENERATION_SECONDS 控制每账号每日预留生成秒数，包含返修与未知提交（公司账号默认600秒，本地默认不限制）；它是生成额度，不是供应商实际人民币账单。

## 验证与代码交付

```sh
.venv/bin/python -m unittest discover -s tests -v
.venv/bin/python -m scripts.doctor --connect
.venv/bin/python -m scripts.export_source
```

PostgreSQL 实测使用专用空验收数据库，通过 HYPIT_TEST_DATABASE_URL 配置，切勿指向生产库。

doctor 只做基础依赖及只读连通检查，不产生收费视频任务。真实验收须单独按费用上限安排。老 accept_core.py 绑定历史开发机素材，不可直接用于新部署。

导出结果为 dist/hypit-source.zip，使用源码白名单，不包含 data、密钥、账号、素材、数据库、本地依赖或备份。只把本目录作为 GitHub 仓库根目录，不要推送上级工作区。
