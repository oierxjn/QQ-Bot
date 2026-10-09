# 独立部署面板

面板独立于 Bot 启动，QQ 未登录、Bot 配置错误或 Theresa 停止时仍可使用。源码和 Docker 模式均提供 TOML 配置管理、校验、差异及备份恢复；Docker 模式另外提供 Compose 表单/YAML、一键应用、任务日志和容器状态。

## 纯源码部署（Windows / Linux）

需要 uv 和 Python 3.13 或以上版本，不需要 Docker、Compose、QQ 登录或数据库连接。在项目根目录执行：

```bash
uv sync --no-dev
uv run -m web.deployment --mode source
```

首次运行自动初始化：生成随机管理员密码文件、从 `configs/*.toml.template` 补齐缺失配置，然后启动服务；也可先执行 `uv run -m web.deployment --mode source --init` 显式初始化后退出。

初始化自动生成随机管理员密码，写入 `.webcontroller/initial-password`（仅当前用户可读），终端只提示文件路径，不显示密码内容；首次成功登录后面板自动删除该文件，请尽快用 `--reset-password` 设置自己的密码。也可在 `--init` 或 `--reset-password` 时用 `--password` 非交互指定密码，此时不生成密码文件。密码长度不限制，仅以 scrypt 哈希存储，强度由使用者自行负责。初始化从 `configs/*.toml.template` 补齐五类缺失配置，不覆盖已有文件；重复执行保留密码。设置与密码哈希存放于 `.webcontroller/`，不写入 `.env`。源码与 Compose 模式不能混用同一个已初始化的状态目录。

默认监听 `127.0.0.1:7001`，可使用 `--port 7002` 指定其他端口。可用 `--bg` 以后台进程运行（`--mode source --bg`），PID 与日志分别记录在 `.webcontroller/panel.pid`、`panel.log`，用 `--mode source --stop` 停止；长期运行仍建议使用下文的 systemd 服务。远程访问使用下文的 SSH 转发。旧 Bot 配置中的 `web_controller_address` 不控制独立面板，仍保留以兼容 Bot 配置加载。

面板与 `uv run main.py` 分开运行。面板不会启动、停止或自动重启 Bot，也不执行数据库初始化。忘记密码时执行：

```bash
uv run -m web.deployment --mode source --reset-password
```

重启面板后使用新密码登录。Linux 状态目录与文件限制为当前用户访问；Windows 使用用户目录的 Windows ACL，请将项目放在可信用户目录内，不与不可信用户共享配置和备份。

Linux 可使用独立 systemd 服务保持面板运行，以下为参考（替换用户名、uv 路径和项目路径，使用 `command -v uv` 查找路径）：

```ini
[Unit]
Description=Theresa WebController
After=network.target

[Service]
User=your-user
WorkingDirectory=/home/your-user/QQ-Bot
ExecStart=/home/your-user/.local/bin/uv run --frozen --no-dev -m web.deployment --mode source
Restart=on-failure
UMask=0077

[Install]
WantedBy=multi-user.target
```

将服务保存为 `/etc/systemd/system/theresa-webcontroller.service` 后，由管理员执行 `sudo systemctl daemon-reload` 和 `sudo systemctl enable --now theresa-webcontroller`。Windows 可在独立终端运行或使用 `--bg`；本版不自动安装系统服务。

## Bot TOML 配置管理（两种部署共用）

源码模式仅显示 Bot 配置，Docker 模式可切换到“Bot 配置”。只允许管理 `bot.toml`、`plugins.toml`、`groups.toml`、`ai.toml`、`scheduler.toml`，不能编辑任意宿主机文件。

Bot 基础配置提供表单，密码和令牌默认遮蔽；插件提供全局启用开关，群聊提供群配置增删与插件开关。复杂参数、AI 与定时任务使用完整 TOML。表单与文本共享草稿，保留注释和未知字段。缺失或语法错误的配置可以通过完整 TOML 修复；切换文件和重新读取前会提示未保存草稿。

校验覆盖 TOML 语法、Bot 必需字段及类型、地址端口、插件/群聊开关和基本配置表结构，不导入插件或测试外部连接，不保证所有插件自定义参数均有效。配置版本冲突会拒绝覆盖。保存和恢复前备份当前文件，并原子替换；备份按文件存于 `.webcontroller/config-backups/`，与 Compose 备份隔离。无效原始文件也会保留备份，但恢复时仍必须通过校验。

**保存只写入磁盘，页面不确认运行中的 Bot 已加载新配置。** 源码部署手动重启 Bot；Docker 部署使用 `docker compose restart theresa`。多个文件分别保存，不提供跨文件事务。Compose 应用期间拒绝 TOML 保存/恢复；TOML 修改不会自动触发 Compose 应用或容器重建。

完整 TOML、差异、下载及备份可能含密码或 API Key，仅向已登录管理员开放。修改监听端口需手动同步 LLBot；数据库配置修改不会修改现有数据库账号密码。

## 新部署

支持 Linux Docker Engine 和 WSL 内的 Linux Docker Engine。宿主机需要 Docker 与 Compose 插件，不需要 Python/uv。

执行发布包中的 `bash scripts/docker_compose_init.sh` 生成 `configs/*.toml` 配置模板，手动填写后执行：

```bash
docker compose up -d
```

面板服务随 compose 启动并在容器内自动初始化（识别已有容器标签确定 Compose 项目名），初始面板密码自动生成，存于 `.webcontroller/initial-password`，首次成功登录后自动删除；与 LLBot WebUI 密码独立。面板把部署目录以 `source: .` 挂载到容器内固定路径 `/deploy`，启动时通过 Docker socket 反查宿主机路径，`compose.yaml` 不需要任何部署目录变量。

访问 `http://127.0.0.1:7001`。远程服务器使用 SSH 转发：

```bash
ssh -L 7001:127.0.0.1:7001 user@server
```

再打开本机的 `http://127.0.0.1:7001`。第一版仅接受 localhost/回环地址的 Host，不支持公网反向代理或局域网直接访问。面板的 Compose 端口只绑定宿主机回环地址。

**Docker socket 权限接近宿主机管理员。** 仅供可信管理员使用；不要将端口绑定到公网。面板没有公开注册入口。

## 已有部署升级

不要重新执行 `docker_compose_init.sh`，它用于首次初始化。保留当前 `compose.yaml` 与配置，在 Compose 的 services 中加入新版提供的 `webcontroller` 服务块（`source: .` 挂载到 `/deploy` 加 docker socket，无环境变量），然后执行：

```bash
docker compose up -d webcontroller
```

面板容器首次启动时自动初始化，从已有容器标签识别原 Compose 项目名称。目录对应多个项目时，在宿主机明确指定：

```bash
docker compose run --rm --no-deps webcontroller python -m web.deployment --init --project-name existing-project
```

`scripts/setup_webcontroller.sh` 保留为宿主机维护入口（重置密码、登记保护配置等），内部通过 `docker compose run` 执行同一命令。密码哈希、会话密钥、受保护配置、备份和任务保存在 `.webcontroller/`；文件权限为 0600、状态目录为 0700。这些文件不应提交到 Git 或打包进镜像。

面板把部署目录挂载到容器内 `/deploy`，并通过 Docker API 反查宿主机真实路径用于 `--project-directory`，因此业务服务的相对 bind 挂载源仍按宿主机路径解析。绝对路径挂载源无法在容器内验证存在性，仅部署目录下的相对源做存在性校验。移动部署目录后需重新初始化面板身份。

## 编辑与应用

1. 在服务表单编辑镜像、重启策略、环境变量、端口、挂载、依赖与健康检查。结构字段接受 JSON 列表/对象；留空删除字段。点击“更新草稿”或切换到完整 YAML，同步到同一份草稿。
2. 完整 YAML 可以配置其他 Compose 字段和增加业务服务。第一版管理单个 `compose.yaml`，不支持 `include`、`extends` 或多个文件叠加。
3. 点击“校验与差异”检查草稿。校验把部署目录的宿主机路径作为 `--project-directory`、部署目录内的 `.env`（如存在）作为 `--env-file` 传给 Compose，先执行 `docker compose config --quiet`，再检查规范化的依赖与挂载。部署目录内的相对 bind 源必须已存在，防止误挂载空目录；绝对路径挂载源无法在面板容器内验证，仅透传。
4. 点击“校验并保存”，检查差异后确认。保存备份后原子替换文件，不会立即更新容器。若文件已被其他操作修改，返回冲突并保留草稿；可下载草稿后重新读取比较。
5. 点击“应用已保存配置”。面板执行固定项目的 `docker compose up -d --wait --wait-timeout 120`，只选择默认 profile 的非面板服务。可关闭页面，再次登录查看任务。

第一版只应用没有 profiles 的服务，不使用 `.env` 中的 `COMPOSE_PROFILES`。有 profile 的服务在宿主机显式启动。镜像按 Compose 默认策略拉取，不强制更新所有 latest 镜像；修改镜像版本可明确触发升级。

应用不执行 `down`、卷删除或 `--remove-orphans`。删除服务配置后，旧容器仍可能存在，需在宿主机确认后处理。变更端口、挂载或环境变量通常需要重建容器，仅执行 `restart` 不会应用这些变化。

`--wait` 最多等待健康状态 120 秒；整个操作最多 15 分钟。超时、失败或面板重启后，可能已经有部分容器更新。查看实际服务状态后决定是否重试。没有健康检查的服务只能确认容器正在运行，不能确认应用功能正常。

Compose、Bot TOML、LLBot JSON 暂不联动。修改监听端口后要同步检查相关文件；已有 PostgreSQL 数据卷时，修改 `POSTGRES_PASSWORD` 不会修改现有用户密码，需要另外执行数据库改密。

## 恢复与面板维护

页面中预览备份并确认恢复；恢复当前配置前也会自动备份。恢复只替换配置文件，需再次应用，不回滚数据库、卷数据或已经执行的容器操作。

面板服务、Compose 项目名以及面板依赖的网络/卷不能从网页修改。升级面板镜像或修改其部署配置时，在宿主机编辑后重新登记保护配置：

```bash
bash scripts/setup_webcontroller.sh --accept-panel-upgrade
docker compose up -d webcontroller
```

忘记密码时：

```bash
bash scripts/setup_webcontroller.sh --reset-password
docker compose restart webcontroller
```

会话 8 小时后过期，面板重启使旧会话失效；失败登录超过每分钟 5 次会暂时限速。恢复密码不会在终端输出密码。任务输出过滤 Compose 环境变量和 `.env` 中的值；完整 YAML、差异、下载与备份仍可能包含明文密码，仅对登录管理员开放。

## 本地构建与验证

首次发布前，或从源码部署时，先在 Linux/WSL 构建面板镜像：

```bash
docker build -f Dockerfile.webcontroller -t heai/theresa-webcontroller:latest .
docker compose up -d webcontroller
```

面板容器首次启动时自动初始化；镜像版本变更直接修改 `compose.yaml` 中 webcontroller 服务的 `image` 字段。

如果构建机器依赖 HTTP 代理，Docker 服务代理负责镜像拉取；BuildKit 的认证请求也可能由 Docker CLI 进程发起，需要同时给构建进程设置代理。构建阶段的 `uv sync` 则使用 build args。例如（将地址替换为构建机器可达的代理）：

```bash
BUILD_PROXY=http://192.168.134.1:10809
sudo env HTTP_PROXY="$BUILD_PROXY" HTTPS_PROXY="$BUILD_PROXY" \
  docker build -f Dockerfile.webcontroller \
  --build-arg HTTP_PROXY="$BUILD_PROXY" \
  --build-arg HTTPS_PROXY="$BUILD_PROXY" \
  -t heai/theresa-webcontroller:latest .
```

```bash
uv run pytest
uv run ruff check .
uv run ruff format --check .
uv run --extra Schedule playwright install chromium
RUN_WEB_BROWSER_TESTS=1 uv run --extra Schedule pytest tests/test_deployment_browser.py
docker build -f Dockerfile.webcontroller -t theresa-webcontroller:test .
RUN_WEB_DOCKER_TESTS=1 uv run pytest tests/test_deployment_docker.py
```

浏览器测试只使用临时配置和受控 Docker 替身；Docker 集成测试使用随机命名的隔离项目、Alpine 容器和临时挂载，结束后仅清理该测试项目及其卷。

如果测试进程本身也在 Linux 容器中运行，需要挂载 Docker socket、将测试目录以相同绝对路径挂载，并使用 `--network host`。集成测试通过宿主机回环地址访问面板的随机端口；默认 bridge 网络中的 `127.0.0.1` 指向测试容器自身。

2026-10-06 已在 Ubuntu 24.04 x86_64 VM 上完成真实验证：Docker Engine 29.6.1，构建出的面板镜像使用 Python 3.13.16、Docker CLI 28.5.2、Compose 2.40.3。配置、API、初始化及 Docker 集成测试共 34 项通过，包含实际面板登录、保存、一键应用及业务容器重建；面板容器保持不变，测试容器和卷已清理。验证未启动 LLBot/PMHQ，也未测试真实 QQ 登录、消息或生产数据库。

同日完成源码模式验证：Windows 全量测试及真实 Chromium 共 214 项通过，2 项真实 Docker 测试因本机缺少 Docker Engine 跳过；DevelopVM 临时 uv 虚拟环境中配置、API、初始化及独立命令启动共 49 项通过，1 项 Windows junction 测试按平台跳过。浏览器覆盖源码表单/TOML、无效草稿保留、保存恢复、群配置、文件切换及 Docker 共用 TOML 页。Linux 临时目录已清理，未启动 Bot、LLBot 或数据库；本轮未重新运行真实 Docker 集成测试。

实现依据：[Compose up](https://docs.docker.com/reference/cli/docker/compose/up/)、[Compose config](https://docs.docker.com/reference/cli/docker/compose/config/)、[Profiles](https://docs.docker.com/compose/how-tos/profiles/)、[项目名称](https://docs.docker.com/compose/how-tos/project-name/)、[Docker socket 风险](https://docs.docker.com/engine/security/#docker-daemon-attack-surface)。
