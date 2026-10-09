# WebController v1

扩展：支持 Windows/Linux 纯源码运行，使用 `--mode source` 初始化及启动（部署根目录默认取包所在仓库，可用 `--root` 覆盖），监听回环地址。源码模式不依赖 Docker/Compose/QQ/数据库。与 Docker 共用五类 Bot TOML 配置管理，包含基础表单、插件/群聊开关、完整 TOML、离线校验、版本冲突、按文件备份恢复。保存只写入文件，需手动重启 Bot，不提供进程管理或热重载。原有 Compose API 和无参数容器入口保持兼容。

独立 FastAPI 面板管理一个 Linux/WSL Docker Compose 部署。常用配置使用表单，其余使用完整 YAML；独立于 Bot、QQ 登录和数据库。默认仅绑定宿主机 127.0.0.1:7001，使用单管理员登录与 CSRF 验证。Docker socket 只供可信管理员使用。

配置保存必须校验 YAML 与实际 Compose 上下文、检查版本冲突、备份后原子替换。面板服务、项目身份及面板网络/卷受保护。面板配置升级在宿主机执行。应用使用固定项目名和同路径部署目录，运行非面板服务的 `up -d`，不执行 down、卷删除或孤立容器清理。

实施顺序：独立入口和鉴权 → 文档校验与备份 → 表单/YAML 同步 → 持久化应用任务和状态 → 镜像、初始化、发布与文档。每步执行相关测试。

成功标准：Bot 不可用时仍能访问面板；编辑不丢失未知字段或注释；无效配置和外部修改不被覆盖；应用目标为原部署；网页断开后任务继续；失败及中断可见。备份恢复仅恢复配置，不回滚数据库或卷数据。

验证：`uv run pytest`、`uv run ruff check .`、`uv run ruff format --check .`，隔离 Docker 集成测试与真实浏览器交互。Docker 不可用时记录未完成的环境验证，不操作真实部署。

## v2 方向：消除部署目录变量（已在 VM 验证可行）

v1 用 `THERESA_DEPLOY_DIR` 把部署目录以相同绝对路径挂进面板容器，`compose.yaml` 的 `${THERESA_DEPLOY_DIR:?}` 造成"先初始化写 .env、有 .env 才能起容器"的引导依赖。替代方案（2026-10-09 在 Ubuntu VM 的 docker:cli 镜像内验证）：webcontroller 服务改用 `source: .` 挂到容器内固定路径 `/deploy`（相对挂载源由 compose 解析，无需变量）；面板启动时 `docker inspect` 自身容器，从 Mounts 反查宿主机部署目录；所有 compose 调用改为 `docker compose -p <项目名> --project-directory <宿主机路径> --env-file /deploy/.env -f /deploy/compose.yaml …`。Compose v2 接受 CLI 侧不存在的 `--project-directory`，相对 bind 源按其解析为宿主机真实路径并发给 daemon，项目名按其目录名推导、与宿主机一致。

该方案下 `compose.yaml` 无必填变量，首次部署收敛为一条 `docker compose up -d webcontroller`：入口脚本 init-if-needed 后 exec 服务进程，`setup_webcontroller.sh` 转为宿主机维护命令入口（基于 `docker compose run`），`.env` 身份固定退役，项目名靠容器标签发现，初始密码文件仍落在宿主机 `.webcontroller/initial-password`。约束与风险：仅支持 Linux/WSL Docker Engine（Docker Desktop 的挂载源是 VM 内部路径）；`include`/`extends` 仍不支持；面板的 compose 调用、config 校验、身份校验与 Docker 集成测试需整体重写。v2 已在 feature/webcontroller 分支实施。
