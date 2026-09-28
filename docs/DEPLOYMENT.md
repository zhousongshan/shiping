# 同事部署指南（本地开发交付）

## 当前交付边界

已加入 PostgreSQL 接入、独立 Worker、重复提交保护、队列并发保护、错误分类、有限连接重试、数据库账号、权限验证、生产配置检查和部署模板。

这不是生产验收完成证明。Docker 构建、Linux Chromium 实际合成、供应商七种输入完整成片、长视频质量和 72 小时运行观察仍须在部署环境验收。当前先采用一个调度进程和共享持久磁盘；多主机任务租约、对象存储迁移尚未实现。不要通过增加 Worker 副本规避调度锁。

## 1. 提交源码

只把 `hypit-agent` 目录作为仓库根目录。不要上传上级工作区（包含历史素材、其他项目和备份）。本项目 `.gitignore` 排除 data、.venv、node_modules、真实环境文件、日志、数据库、备份及 dist。

也可运行 `python -m scripts.export_source` 生成 `dist/hypit-source.zip`，使用白名单打包源码与部署说明。压缩包不包含实际素材、API 密钥、已有账号或任务数据。

## 2. 准备运行环境

建议专用 Linux 主机，Docker Engine 和 Compose；固定应用与素材域名都指向该主机，80/443 端口可达。外网必须能够连接模型服务并供供应商读取签名媒体。不要依赖个人电脑代理或临时 Tunnel。

容量没有实测前先保持 2 个本地任务槽、每用户 1 个执行槽、2 个远端生成槽。数据目录必须有持久磁盘与备份，不放临时容器层。CPU、内存和磁盘容量按真实样例压力测试定稿。

## 3. 配置和首次启动

从仓库根目录执行：

```sh
cp .env.example .env
```

填入真实域名、两类模型密钥、数据库密码与至少 32 字符的会话密钥。数据库密码使用足够长度的随机 URL 安全字符，避免未经转义的字符破坏连接 URL。环境文件限制为仅部署用户可读。配置公司账号后无需设置旧版 VIDEO_AGENT_USERS_JSON。

```sh
chmod 600 .env
docker compose --env-file .env -f deploy/compose.yaml build
docker compose --env-file .env -f deploy/compose.yaml up -d db
docker compose --env-file .env -f deploy/compose.yaml run --rm api python -m scripts.manage_users admin --role admin
docker compose --env-file .env -f deploy/compose.yaml up -d
```

账号命令从终端隐藏输入访问令牌（至少 20 字符），数据库只保存 scrypt 摘要。员工账号使用相同命令且不加 `--role admin`。更换令牌或禁用账号会使既有会话失效：

```sh
docker compose --env-file .env -f deploy/compose.yaml run --rm api python -m scripts.manage_users employee1
docker compose --env-file .env -f deploy/compose.yaml run --rm api python -m scripts.manage_users employee1 --disable
```

Compose 中服务命令：API 为 `python -m uvicorn app.server:app`；Worker 为 `python -m app.worker_main`；媒体服务独立监听 4781。只有 HTTPS 代理对外开放。应用数据库、媒体端口不直接公开。

## 4. 就绪检查与现场验收

```sh
docker compose --env-file .env -f deploy/compose.yaml run --rm api python -m scripts.doctor --connect
docker compose --env-file .env -f deploy/compose.yaml ps
```

- `/api/health` 是进程/基础依赖检查，不是生成成功证明。
- `/api/ready` 检查数据库及独立 Worker 心跳；失联返回 503。
- 管理员登录后可访问 `/api/admin/status` 查看异常任务和 Worker 状态。
- doctor 的模型连接探测只 GET 模型列表，不生成视频；HTTP 200 只证明该请求成功。
- `reference.configured=true` 只表示填了地址。须实际上传参考素材并确认模型能读取，不能直接判定通道可用。
- 必须在实际 Linux 环境验证合成浏览器，不以 Docker 镜像构建成功代替视频渲染成功。
- 在确认验收费用上限后，按验收清单提交真实生成；老 `scripts/accept_core.py` 是绑定历史本机素材的脚本，不适用于新部署的验收，也不可直接当作生产验收工具。

## 5. 本地开发

```sh
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
npm ci
.venv/bin/python -m uvicorn app.server:app --host 127.0.0.1 --port 4780
```

默认仍允许 SQLite 与内嵌 Worker 供本地使用。生产设置 `VIDEO_AGENT_ENV=production` 后，缺账号、PostgreSQL、HTTPS、安全 Cookie、持久素材域名或模型密钥会拒绝启动。

独立 Worker 的本地测试需要同时给两个进程设置 `VIDEO_AGENT_EMBEDDED_WORKER=0`，共享同一 VIDEO_AGENT_DATA 和 VIDEO_AGENT_DATABASE_URL。

## 6. 迁移与数据路径

迁移前停止 Web 写请求与 Worker，备份 SQLite 和完整 data 目录。为目标建立空 PostgreSQL 数据库，使用目标 DSN：

```sh
python -m scripts.migrate_sqlite /path/to/tasks.sqlite
```

脚本在一个事务中迁移任务、素材、请求去重和账号等记录；目标非空则拒绝覆盖。历史工程含绝对素材和运行时路径，跨主机不能只复制数据库就启动旧任务：须迁移完整工程、校验文件哈希并处理旧路径，再逐条验证恢复；尚未提供自动跨系统路径重写。新服务器可以先从空数据开始使用，旧任务留在本机。

## 7. 备份、恢复与回退

`sh deploy/backup.sh` 会短暂停止 API/Worker，导出 PostgreSQL 与完整媒体卷，再启动服务。备份包含签名密钥与用户数据，保存在被 Git 忽略的 backups 下，需加密异地保管。此模板仍须在目标环境演练。

恢复时停止 API/Worker，将数据库备份还原到**新的空数据库**，媒体归档还原到**新的空数据卷**，校验任务数量、素材、提交回执与文件哈希，再切换配置并启动。不要直接覆盖仍接受请求的生产数据库。

应用回退不能丢弃升级后产生的远端提交记录。先停止领取、备份并核对外部任务；仅回退应用代码或恢复数据前必须确认新回执已保留。当前脚本没有自动执行破坏性的恢复操作。

## 8. 运维边界

当前账号采用应用独立令牌，尚未接入公司 SSO。已有心跳、结构化失败、工具耗时和管理员状态接口；外部告警渠道、集中监控、定时备份与长期数据清理需在部署现场配置并验收。

提交结果不明会保留原记录并占用远端容量，不自动重新付费。仅本地没有 ID 不能证明供应商未收单；管理员需要核对服务商记录。尚未实现可靠的供应商跨请求任务搜索时，不能把“核对”按钮当作万能恢复。
