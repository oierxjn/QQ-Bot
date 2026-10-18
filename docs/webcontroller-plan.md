# WebController 设计记录

安装与使用说明见 [webcontroller.md](webcontroller.md)。本文按版本记录设计演变，v4 为当前设计。

## v1：初始设计

扩展：支持 Windows/Linux 纯源码运行，使用 `--mode source` 初始化及启动（部署根目录默认取包所在仓库，可用 `--root` 覆盖），监听回环地址。源码模式不依赖 Docker/Compose/QQ/数据库。与 Docker 共用五类 Bot TOML 配置管理，包含基础表单、插件/群聊开关、完整 TOML、离线校验、版本冲突、按文件备份恢复。保存只写入文件，需手动重启 Bot，不提供进程管理或热重载。原有 Compose API 和无参数容器入口保持兼容。

独立 FastAPI 面板管理一个 Linux/WSL Docker Compose 部署。常用配置使用表单，其余使用完整 YAML；独立于 Bot、QQ 登录和数据库。默认仅绑定宿主机 127.0.0.1:7001，使用单管理员登录与 CSRF 验证。面板容器挂载 Docker socket，权限接近宿主机管理员，仅供可信管理员使用。

配置保存必须校验 YAML 与实际 Compose 上下文、检查版本冲突、备份后原子替换。面板服务、项目身份及面板网络/卷受保护。面板配置升级在宿主机执行。应用使用固定项目名和同路径部署目录（v2 已改为固定 `/deploy` 挂载，见下节），运行非面板服务的 `up -d`，不执行 down、卷删除或孤立容器清理。

实施顺序：独立入口和鉴权 → 文档校验与备份 → 表单/YAML 同步 → 持久化应用任务和状态 → 镜像、初始化、发布与文档。每步执行相关测试。

成功标准：Bot 不可用时仍能访问面板；编辑不丢失未知字段或注释；无效配置和外部修改不被覆盖；应用目标为原部署；网页断开后任务继续；失败及中断可见。备份恢复仅恢复配置，不回滚数据库或卷数据。

验证：`uv run pytest`、`uv run ruff check .`、`uv run ruff format --check .`，隔离 Docker 集成测试与真实浏览器交互。Docker 不可用时记录未完成的环境验证，不操作真实部署。

## v2：消除部署目录变量（已被 v3 取代）

v1 用 `THERESA_DEPLOY_DIR` 把部署目录以相同绝对路径挂进面板容器，`compose.yaml` 的 `${THERESA_DEPLOY_DIR:?}` 造成"先初始化写 .env、有 .env 才能起容器"的引导依赖。替代方案（2026-10-09 在 Ubuntu VM 的 docker:cli 镜像内验证）：webcontroller 服务改用 `source: .` 挂到容器内固定路径 `/deploy`（相对挂载源由 compose 解析，无需变量）；面板启动时 `docker inspect` 自身容器，从 Mounts 反查宿主机部署目录；所有 compose 调用改为 `docker compose -p <项目名> --project-directory <宿主机路径> --env-file /deploy/.env -f /deploy/compose.yaml …`。Compose v2 接受 CLI 侧不存在的 `--project-directory`，相对 bind 源按其解析为宿主机真实路径并发给 daemon，项目名按其目录名推导、与宿主机一致。

该方案下 `compose.yaml` 无必填变量，首次部署收敛为一条 `docker compose up -d webcontroller`：服务进程启动时自动初始化（源码模式相同），`setup_webcontroller.sh` 转为宿主机维护命令入口（基于 `docker compose run`），`.env` 身份固定退役，项目名靠容器标签发现，初始密码文件仍落在宿主机 `.webcontroller/initial-password`。约束与风险：仅支持 Linux/WSL Docker Engine（Docker Desktop 的挂载源是 VM 内部路径）；`include`/`extends` 仍不支持；面板的 compose 调用、config 校验、身份校验与 Docker 集成测试需整体重写。v2 已在 feature/webcontroller 分支实施。

## v3：移除 Docker daemon 访问（已被 v4 取代）

一键应用此前已移除；2026-10-13 起面板不再挂载或访问 Docker socket，容器状态展示随 daemon 访问一并删除，面板收敛为对 daemon 零访问的配置编辑器：

- 校验保留客户端的 `docker compose config`，以部署目录本身为 `--project-directory`。相对 bind 挂载源的存在性检查直接对照部署目录视图，报错中的路径为容器视图（`/deploy/...`）；绝对路径挂载源无法验证，仅透传。
- Compose 项目名改为静态推断：`compose.yaml` 的 `name` → `.env` 的 `COMPOSE_PROJECT_NAME` → 目录名清洗；`--project-name` 可覆盖。
- 宿主机路径与容器路径的双路径模型坍缩为单一部署目录，`settings.json` 的 `root` 记录面板可见的路径。旧版本的状态记录含宿主机路径，升级后需删除 `.webcontroller/settings.json` 重新初始化，密码和备份保留。
- 面板镜像保留 Docker CLI 与 Compose 插件，仅用于客户端校验；`compose.yaml` 不再挂载 socket。
- 验证状态：Windows 全量测试通过（Linux Docker 集成与 Chromium 浏览器测试按环境跳过）；容器内部署流程待 Linux/WSL 验证。

## v4：面板作为容器主进程（已实施，当前）

面板镜像没有独立存在的必要：bot 镜像 `COPY . .` 天然包含面板代码与全部依赖，第二个镜像却带来发布、版本对齐与拉取失败问题；daemon 零访问（v3）落地后，合并的安全顾虑不再成立。中间方案（Bot 进程内以 task 启动面板）有一个真实缺陷：entrypoint 在拉起 Bot 前会无限等待 QQ 扫码登录，面板要等到登录成功才可用——恰恰是部署最需要面板的阶段。最终方案反转主从：

- 面板是 theresa 容器的主进程（镜像 CMD 为 `python -m src.deployment`），先就绪、先可用；以子进程拉起完整启动链（数据库初始化、等待 QQ 登录、`main.py`），退出后按 1 秒起步、指数延长至最长 10 分钟的退避自动重启（稳定运行 60 秒后重新从 1 秒开始）。部署形态只经 `/deploy` 挂载探测一次判定（compose 或源码），无 `--mode` 参数、无环境变量开关：两种形态下面板都拉起并守护 Bot，容器经 entrypoint 链，源码直接守护 `main.py` 并替代手动运行。
- 双向解耦：面板失败（端口占用、状态记录不匹配）只记录日志，Bot 继续运行；Bot 崩溃不影响面板。SIGTERM 由面板转发给子进程；作为 PID 1，面板在 uvicorn 信号捕获之外自行安装信号处理器。
- `src/deployment/runtime.py` 承载组装与监督（探测 `/deploy` 挂载判定模式、自动初始化、构建应用、子进程守护），CLI 与容器共用。
- compose.yaml 删除 webcontroller 服务与 entrypoint/command 覆盖；theresa 服务增加 `/deploy` 挂载（`source: .`）和 `127.0.0.1:7001:7001` 端口映射。`configs/` 同时挂载到 `/app/theresa/configs` 与 `/deploy/configs`，面板配置编辑直接落到 Bot 读取的同一目录。
- Dockerfile 合并 docker CLI 与 Compose 插件（仅客户端校验）；`Dockerfile.webcontroller`、面板镜像 CI/发布任务删除；`setup_webcontroller.sh` 改为 `docker compose run --entrypoint python3 theresa`。
- 保护基线收敛为 Compose 项目名：面板所在的服务即 theresa，改动其 env/镜像是正常需求；服务全部可从网页编辑。
- "重启 Bot"动作（`/api/bot/restart`，经登录与 CSRF）：通过监督者立即重启子进程并归零退避，使保存的配置即时应用，替代旧设计中"重启面板/重启容器"的应用方式。
- Docker 集成测试为纯宿主机侧（runner 校验 + `up -d --wait` + profiles 行为）；supervisor 的重启与停机终止有进程级测试；容器内部署流程待 Linux/WSL 验证。
