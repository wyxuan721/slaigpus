# slaigpus

`slaigpus` 是面向深圳河套学院（SLAI）商汤大装置环境的命令行自动化工具。工具提供：

- 可见 Chrome 工作窗口和持久登录态；
- 常驻的 headless CCI 续期控制器；
- CCI 状态查询、剩余时间计算、私有镜像续期和自动续期开关；
- ACP 固定硬件规格查询、任务 dry-run/提交和容器日志查询。

所有功能可以在同一台机器上运行，也可以按需在多台机器上独立运行。程序不会跨机器控制 Chrome；每台机器都使用自己的凭据文件、Chrome profile 和本地恢复状态。

项目要求 Python 3.9 或更高版本。支持 macOS，以及 Ubuntu 22.04/24.04 amd64。

## 功能与命令

| 命令 | 功能 |
|---|---|
| `slaigpus viewer` | 打开可见工作 Chrome，不启动 CCI 续期任务 |
| `slaigpus controller` | 运行常驻的 headless CCI 续期控制器 |
| `slaigpus open` | 同时打开可见工作 Chrome 和独立的后台 CCI 续期任务 |
| `slaigpus configure` | 启动或重新启动引导式配置助手 |
| `slaigpus credentials set/status/delete` | 配置、检查或删除自动登录凭据 |
| `slaigpus cci status` | 查询 CCI、实例状态、运行时长和续期时间 |
| `slaigpus cci remaining` | 查询距离 3h50m 续期点和 4h 强制过期还有多久 |
| `slaigpus cci renew` | 执行“保存私有镜像 → 加载镜像 → 等待重启” |
| `slaigpus cci auto-renew on/off/status` | 持久开关或查询自动续期 |
| `slaigpus cci watch [--once]` | 前台等待并在到期时续期 |
| `slaigpus acp profiles` | 本地列出固定的非 Debug ACP 硬件配置库 |
| `slaigpus acp submit` | 规划或提交 ACP 任务；默认只做 dry-run |
| `slaigpus acp logs` | 查询或持续轮询 ACP 容器日志 |

`--json` 不是必需参数。人在终端使用时可省略；agent 或脚本需要稳定字段时再添加。

## 最短执行路径

1. 首次配置账号、密码、正式学生/RA 身份以及直连/SSH 代理：

   ```bash
   slaigpus configure
   ```

2. 指定 CCI 并启动 CCI 续期控制器；`--cci` 可以填内部名称或页面显示名称：

   ```bash
   slaigpus cci auto-renew on
   slaigpus controller --cci 'CCI_NAME_OR_DISPLAY_NAME'
   ```

3. ACP 先做 dry-run：

   ```bash
   slaigpus acp submit \
     --name new-job-name \
     --image 'PRIVATE_REGISTRY/NAMESPACE/IMAGE:TAG' \
     --command 'python train.py'
   ```

4. 确认计划正确后，在同一命令末尾增加 `--apply` 正式创建任务：

   ```bash
   slaigpus acp submit \
     --name new-job-name \
     --image 'PRIVATE_REGISTRY/NAMESPACE/IMAGE:TAG' \
     --command 'python train.py' \
     --apply
   ```

下面各章节列出这三个流程的完整参数、默认值和安全边界。

## SenseCore 环境

登录地址、企业标识、区域和 workspace 都按 SLAI 环境固定，首次使用只需配置账号、身份和网络方式：

- 默认网络模式：直连，不启动 SSH
- 企业登录入口：`https://zhicheng.signin.sensecore.cn/`
- 企业标识：`zhicheng`
- 区域：`cn-sh-01`
- workspace：`share-space-01e`（固定完整 resource ID 已内置）
- CCI 自动续期：实例运行满 `3h50m`
- SenseCore 强制过期：`4h`

临时指定 SSH `Host` 别名：

```bash
slaigpus open --ssh-host sensecore-proxy
```

`--direct` 可临时覆盖配置并强制直连。

## 安装教程

推荐使用 `pipx` 安装。它会为 slaigpus 创建独立的 Python 环境，同时把 `slaigpus` 命令加入用户 PATH，因此安装后可在任意终端使用。

### 1. 安装系统依赖

slaigpus 需要 Python 3.9 或更高版本和官方 Google Chrome。只有开发源码安装才需要 Git。

Ubuntu 22.04/24.04 amd64：

```bash
sudo apt update
sudo apt install -y curl ca-certificates pipx
python3 --version
pipx --version
```

只有选择 SSH 代理时才需要安装 OpenSSH 客户端：

```bash
sudo apt install -y openssh-client
```

macOS：

```bash
brew install pipx
python3 --version
pipx --version
```

如果 `python3 --version` 低于 `3.9`，应先升级 Python，再继续安装。

### 2. 安装 Google Chrome

Ubuntu amd64 安装官方系统级 Chrome：

```bash
curl -fLO https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y ./google-chrome-stable_current_amd64.deb
google-chrome-stable --version
```

macOS 可使用 Homebrew 安装：

```bash
brew install --cask google-chrome
```

macOS 的默认受信任路径是：

```text
/Applications/Google Chrome.app/Contents/MacOS/Google Chrome
```

无人值守自动登录只向受信任的系统级 Google Chrome 提供凭据。Snap/Flatpak Chromium、自定义 `SLAIGPUS_CHROME` 或权限异常的浏览器可以用于人工浏览，但不能用于自动填充账号密码。

### 3. 安装 slaigpus

先让 `pipx` 把命令目录加入 PATH：

```bash
pipx ensurepath
```

首次执行后应关闭并重新打开终端，再安装固定版本的 GitHub Release wheel：

```bash
pipx install \
  https://github.com/wyxuan721/slaigpus/releases/download/v0.6.0/slaigpus-0.6.0-py3-none-any.whl
```

不要使用 `sudo pip install`，也不要只安装到临时虚拟环境，否则容易遇到权限问题或在新终端中找不到命令。

### 4. 验证安装并完成首次配置

```bash
command -v slaigpus
slaigpus --help
pipx list
```

`command -v slaigpus` 应输出一个可执行文件路径，通常是 `~/.local/bin/slaigpus`；`slaigpus --help` 应显示 `viewer`、`controller`、`cci` 和 `acp` 等命令。

随后在交互式终端完成账号、身份和网络配置：

```bash
slaigpus configure
slaigpus credentials status
```

最后执行只读检查；它会验证自动登录和 CCI 状态读取，但不会保存镜像或修改 CCI：

```bash
slaigpus cci status --headless --no-probe
```

如果提示 `slaigpus: command not found`，重新打开终端并再次执行 `pipx ensurepath`。也可以先用 `~/.local/bin/slaigpus --help` 判断是否只是 PATH 尚未刷新。

### 5. 更新或卸载

升级 Release 时，将 URL 中的标签和文件版本同时替换成目标版本。例如重新安装 `v0.6.0`：

```bash
pipx install --force \
  https://github.com/wyxuan721/slaigpus/releases/download/v0.6.0/slaigpus-0.6.0-py3-none-any.whl
```

若 systemd 正在运行 CCI 续期控制器，更新后重启：

```bash
systemctl --user restart slaigpus-controller.service
```

卸载程序：

```bash
pipx uninstall slaigpus
```

卸载不会自动删除本地凭据、配置、Chrome profile 或 CCI 恢复状态，避免误删仍需保留的数据。

### 6. 开发源码安装（可选）

需要直接修改源码时可使用 editable 安装：

```bash
# Ubuntu: sudo apt install -y git
# macOS:  brew install git
git clone https://github.com/wyxuan721/slaigpus.git
cd slaigpus
pipx install --editable .
```

## 首次配置

安装后应先在交互式终端运行一次：

```bash
slaigpus configure
```

可选的命令行参数：

| 参数 | 是否必需 | 作用与默认值 |
|---|---|---|
| `--config PATH` | 否 | 指定要维护的 TOML；默认 `~/.config/slaigpus/config.toml` |
| `--credentials-file PATH` | 否 | 指定绝对路径的私有凭据 JSON；默认 `~/.config/slaigpus/credentials.json` |

助手会依次要求输入以下内容：

| 交互输入 | 是否必需 | 说明 |
|---|---|---|
| SenseCore 账号 | 是 | IAM 用户账号，不能为空 |
| SenseCore 密码 | 是 | 使用普通 `input()`，会在终端明文显示，不需要再次确认 |
| 身份 | 是 | 输入 `1` 选择正式学生，或输入 `2` 选择 RA |
| 是否使用 SSH 代理 | 是 | 输入 `y` 使用代理；直接回车或输入 `n` 使用直连 |
| SSH Host 别名 | 仅代理模式 | 必须是已经配置在 `~/.ssh/config` 中的 `Host` 别名，例如 `sensecore-proxy` |

完整交互示例：

```text
$ slaigpus configure
SenseCore 账号: YOUR_IAM_ACCOUNT
SenseCore 密码: YOUR_PASSWORD
请选择身份（1=正式学生/标准资源，2=RA/闲时资源）: 2
是否使用 SSH 代理？[y/N]: y
请输入 SSH Host 别名: sensecore-proxy
```

按照本项目的交互约定，账号和密码都会明文显示；但密码不会进入命令行参数、shell history、环境变量或 TOML。身份决定 ACP 默认资源：

- 正式学生：默认标准资源 `standard`；
- RA：默认闲时资源 `spot`。

配置成功后会生成或更新 TOML 和权限为 `0600` 的私有凭据 JSON。`viewer`、`open`、`controller`、需要登录的 CCI 命令以及 ACP 提交/日志命令发现配置不完整时，也会在交互式终端自动启动同一个助手。agent、systemd 或管道等非交互环境不会等待输入，而会提示先运行 `slaigpus configure`。后续修改账号、密码、身份或代理时再次运行该命令即可。

### 1. 选择直连或 SSH 代理

在助手中选择不使用代理会写入直连配置：

```toml
[sensecore]
account_type = "student"

[sensecore.network]
mode = "direct"
```

选择使用代理时，助手会提示先配置 `~/.ssh/config`，然后只要求输入 OpenSSH `Host` 别名，生成：

```toml
[sensecore]
account_type = "ra"

[sensecore.network]
mode = "ssh"
ssh_host = "sensecore-proxy"
```

具体连接信息写在标准的 `~/.ssh/config` 中，slaigpus 只保存 `Host` 别名：

```sshconfig
Host sensecore-proxy
    HostName SSH_GATEWAY_HOST
    User SSH_USER
    IdentityFile ~/.ssh/id_ed25519
```

运行 CCI 续期控制器的系统账号必须能非交互使用该别名。SSH 密码、密钥内容和 SenseCore 密码都不能写入 `config.toml`。

临时覆盖配置：

```bash
slaigpus controller --direct
slaigpus controller --ssh-host sensecore-proxy
```

`--direct` 与 `--ssh-host` 互斥。修改持久网络模式后应重启已运行的 viewer 或 CCI 续期控制器，因为 Chrome 的代理参数只能在启动时确定。

### 2. 保存自动登录凭据

`slaigpus configure` 会把刚才明文输入的账号和密码保存到私有凭据 JSON，不会写入 TOML。也可以单独使用旧的凭据管理命令：

```bash
slaigpus credentials set
slaigpus credentials status
```

`credentials set` 仍使用隐藏输入并要求确认；`configure` 则按引导式助手的约定使用明文输入、不要求二次确认。两者写入同一种私有文件。slaigpus 不使用系统钥匙串。

macOS 和 Linux 的默认文件都是：

```text
~/.config/slaigpus/credentials.json
```

可指定其他私有文件：

```bash
slaigpus credentials set --file /absolute/private/credentials.json
slaigpus credentials status --file /absolute/private/credentials.json --json
slaigpus credentials delete --file /absolute/private/credentials.json
```

凭据目录必须是当前用户拥有的真实 `0700` 目录；凭据必须是当前用户拥有、权限严格为 `0600` 的普通单链接文件。符号链接、硬链接、异常所有者和宽松权限都会被拒绝。写入使用同目录原子替换。

只有绝对路径的 `$XDG_CONFIG_HOME` 会覆盖默认目录。相对路径会被忽略，避免把 secret 写到当前目录。不要手工创建、复制、提交或上传凭据文件。

若在多台机器上使用，应分别运行 `credentials set`，不要复制凭据、Cookie 或 Chrome profile。

### 3. 只读 smoke test

```bash
slaigpus cci status --headless --no-probe
slaigpus cci remaining --headless --json
```

这些命令不会保存镜像、修改 CCI 或创建 ACP 任务。

## 浏览器和 CCI 续期控制器

slaigpus 维护两个互相独立的持久 Chrome profile：

- work profile：供 `viewer` 的可见工作 Chrome 使用；
- automation profile：供 CCI 续期控制器、CCI 命令和 ACP 后台 API 使用。

两个 profile 不共享 Cookie，也不会互相复制登录态，因此可以在同一台机器上并存而不争抢 profile 锁。

### 可见工作窗口

```bash
slaigpus viewer
```

`viewer` 使用配置的直连或 SSH 代理打开可见 Chrome。若配置了凭据，它先用 work profile 启动短暂、可见且禁用扩展的登录 bootstrap；确认到达 Console 后关闭 bootstrap，再启动普通工作窗口。

遇到验证码、MFA、未知页面或自动登录失败时，不会反复提交账号密码，而是保留可见窗口供人工处理。

如需本地脚本连接可见 Chrome：

```bash
slaigpus viewer --cdp
```

这会启用配置的 loopback DevTools 端口，默认 `127.0.0.1:9222`。不要把该端口转发给其他机器。

### CCI 续期控制器

```bash
slaigpus controller

# workspace 中有多个 CCI 时，按内部名称或页面显示名称指定自动续期目标
slaigpus controller --cci 'CCI_NAME_OR_DISPLAY_NAME'
```

本文把该进程统一称为“CCI 续期控制器”；为保持命令兼容，CLI 子命令仍叫 `controller`。

`slaigpus controller` 是专门负责 CCI 续期的常驻进程，不是普通网页浏览器，也不会仅因进程正在运行就修改 CCI。直接在终端执行时它会占用前台；交给 systemd 后才作为后台服务运行。

启动后，CCI 续期控制器会根据 `[sensecore.network]` 直连或通过配置的 SSH `Host` 别名启动 headless automation Chrome，自动登录，然后循环执行以下工作：

1. 按 `--cci` 选择目标 CCI，并从服务端 `last_started_time` 计算本轮运行时间；
2. 默认每 30 秒检查一次状态和本地自动续期开关；
3. 运行达到 `3h50m` 时，保存当前容器为私有镜像；
4. 快照成功后，把 CCI 的容器镜像更新为新镜像，等待平台自动重启；
5. 核对新实例、镜像和启动时间，成功后继续监控下一轮。

`--cci` 对 API 的内部 `name` 和控制台显示的 `display_name` 做精确匹配；也可用 `--direct` / `--ssh-host ALIAS` 临时覆盖网络。关闭 `slaigpus cci auto-renew` 后，控制器仍可运行和保持登录态，但不会开始下一轮自动续期。登录失败时它不会弹出人工窗口；错误密码、验证码、MFA 或未知登录页会使其失败闭合并退出。

运行 CCI 续期控制器的机器必须在目标 CCI 重启期间保持在线。如果控制器本身运行在每 4 小时销毁的目标 CCI 内，它无法在自身重启期间完成镜像核对与恢复，应改放到不会随目标 CCI 一起销毁的机器上。

同机执行 `cci status/remaining/renew/watch` 会优先连接 CCI 续期控制器已有的 automation Chrome。没有运行控制器时，这些命令也可临时启动浏览器；加 `--headless` 可要求全程不出现可见窗口。

### 组合模式

```bash
slaigpus open
```

`open` 同时启动可见工作 Chrome 和独立的后台 CCI 续期任务，仍使用两个互不冲突的 profile。只想打开浏览器：

```bash
slaigpus open --no-cci-watch
```

## 使用 systemd 常驻运行 CCI 续期控制器

先在终端前台运行 `slaigpus controller --cci 'CCI_NAME_OR_DISPLAY_NAME'`，确认登录、目标选择和状态查询正常，再创建 `~/.config/systemd/user/slaigpus-controller.service`：

```ini
[Unit]
Description=slaigpus CCI renewal controller
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=15min
StartLimitBurst=3

[Service]
Type=simple
# 建议始终明确目标：controller --cci CCI_INTERNAL_NAME
ExecStart=%h/.local/bin/slaigpus controller --cci CCI_INTERNAL_NAME
UMask=0077
Restart=on-failure
RestartSec=60s

[Install]
WantedBy=default.target
```

把 `CCI_INTERNAL_NAME` 替换为 CCI 的实际内部名称。

若 `command -v slaigpus` 不是 `%h/.local/bin/slaigpus`，请替换 `ExecStart`。仓库也提供 [examples/slaigpus-controller.service](examples/slaigpus-controller.service)。

```bash
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now slaigpus-controller.service
journalctl --user -u slaigpus-controller.service -f
```

`enable-linger` 允许服务在退出 SSH 后继续运行并随机器启动。短暂网络或代理故障按 60 秒间隔恢复；15 分钟内快速失败最多三次，避免错误密码、验证码或页面变化造成无限快速重试。

修复故障后：

```bash
systemctl --user reset-failed slaigpus-controller.service
systemctl --user restart slaigpus-controller.service
```

## CCI 管理

### 续期所需参数和完整命令

执行 CCI 续期前必须已经完成 `slaigpus configure`。建议始终传入 `--cci`，避免 workspace 中出现多个 CCI 后目标变得不明确。

先确认目标和剩余时间，这两个命令只读：

```bash
slaigpus cci status --cci 'CCI_NAME_OR_DISPLAY_NAME'
slaigpus cci remaining --cci 'CCI_NAME_OR_DISPLAY_NAME'
```

立即执行一次“保存私有镜像 → 加载新镜像 → 等待重启”：

```bash
slaigpus cci renew \
  --cci 'CCI_NAME_OR_DISPLAY_NAME'
```

只在运行时间已经达到 `3h50m` 时续期，未到时间则成功退出且不修改 CCI：

```bash
slaigpus cci renew \
  --cci 'CCI_NAME_OR_DISPLAY_NAME' \
  --if-due
```

持续自动续期：

```bash
# 默认开关本来就是 on；如果以前关闭过，先重新打开
slaigpus cci auto-renew on

# 前台持续运行
slaigpus cci watch \
  --cci 'CCI_NAME_OR_DISPLAY_NAME' \
  --renew-after 3h50m

# 或使用专门的 CCI 续期控制器；生产使用建议交给 systemd
slaigpus controller \
  --cci 'CCI_NAME_OR_DISPLAY_NAME' \
  --renew-after 3h50m
```

目标和时间参数如下。这些参数用于 `controller`、`open` 以及 `cci status/remaining/renew/watch`；`open` 的旧 `--cci-app` 等别名仍兼容。

| 参数 | 是否必需 | 作用与默认值 |
|---|---|---|
| `--cci NAME_OR_DISPLAY_NAME` | 多个 CCI 时必需；其他情况强烈建议 | 按 API 内部 `name` 或页面 `display_name` 精确选择 CCI，支持中文显示名称 |
| `--workspace RESOURCE_ID` | 否 | 高级临时覆盖；默认使用内置 SLAI workspace，日常无需传入 |
| `--instance NAME_OR_UID` | 通常否 | 多个运行中实例无法唯一选择时指定旧实例；重启后会自动释放该旧实例选择器 |
| `--container NAME` | 通常否 | CCI template 有多个主容器时指定要保存和替换镜像的容器 |
| `--namespace NAME` | 通常否 | 私有镜像 namespace 无法自动唯一发现时指定 |
| `--renew-after DURATION` | 否 | 自动续期阈值，默认 `3h50m`，必须小于 `4h` |
| `--poll-interval DURATION` | 否 | 状态轮询间隔，默认 `30s` |
| `--wait-timeout DURATION` | 否 | 保存镜像和重启的等待上限，默认 `15m` |

网络、登录和输出参数：

| 参数 | 适用命令 | 作用 |
|---|---|---|
| `--direct` | CCI 续期控制器、CCI 命令 | 临时强制直连，覆盖 TOML |
| `--ssh-host ALIAS` | CCI 续期控制器、CCI 命令 | 临时使用 `~/.ssh/config` 中的代理别名；与 `--direct` 互斥 |
| `--credentials-file PATH` | CCI 续期控制器、CCI 命令 | 临时使用指定绝对路径的私有凭据 JSON；默认使用配置助手保存的文件 |
| `--headless` | `cci status/remaining/renew/watch` | 没有运行中的 CCI 续期控制器时，只允许无头登录；登录失败直接退出，不弹窗口 |
| `--cdp-port PORT` | `cci status/remaining/renew/watch` | 连接已经运行的 slaigpus Chrome，不再启动临时浏览器 |
| `--no-probe` | CCI 续期控制器、CCI 命令 | 跳过启动前的网络连通性检查 |
| `--json` | status、remaining、renew | 输出适合 agent/脚本解析的 JSON |

命令专用参数：

| 参数 | 适用命令 | 作用 |
|---|---|---|
| `--if-due` | `cci renew` | 只在达到 `--renew-after` 后执行续期 |
| `--once` | `cci watch` | 下一次成功续期后退出；不传则持续运行 |
| `--apply` | 不适用于 CCI | CCI 的 `renew` 本身就是明确写操作，不需要额外传 `--apply` |

### 状态和剩余时间

```bash
slaigpus cci status
slaigpus cci remaining

slaigpus cci status --json
slaigpus cci remaining --json
```

`status` 返回选中的 app、实例、容器、服务端 `last_started_time`、运行时长、续期点和 4 小时过期点。

`remaining` 重点返回：

- 距离 `3h50m` 自动续期还有多久；
- 距离 `4h` 强制过期还有多久；
- 对应绝对时间和剩余秒数。

程序按服务端 `last_started_time` 计时，不按 slaigpus 启动时间计时。

所有 CCI 查询、续期和 watcher 命令都支持 `--workspace`，并可用 `--cci NAME_OR_DISPLAY_NAME`、`--instance`、`--container`、`--namespace` 消除目标歧义。`--cci` 同时接受 CCI 内部名称和页面显示名称，支持中文显示名称；旧的 `--app` 仍兼容。没有运行中的 CCI 续期控制器时，可用 `--credentials-file` 指定临时自动化浏览器的凭据；`--cdp-port` 只用于连接现有 slaigpus Chrome。

### 手动续期和前台 watcher

```bash
# 立即续期
slaigpus cci renew

# 只在达到 3h50m 后续期
slaigpus cci renew --if-due

# 前台持续等待并自动续期
slaigpus cci watch

# 只监控并自动续期指定 CCI
slaigpus cci watch --cci 'CCI_NAME_OR_DISPLAY_NAME'

# 下一次成功续期后退出
slaigpus cci watch --once
```

高级时间参数接受 `3h50m`、`230m`、`30s` 等格式：

```bash
slaigpus cci watch \
  --renew-after 3h50m \
  --poll-interval 30s \
  --wait-timeout 15m
```

`--renew-after` 必须小于 4 小时。默认续期点距离强制过期只有 10 分钟，请确认镜像保存和重启通常能在该时间内完成。

### 自动续期开关

```bash
slaigpus cci auto-renew status
slaigpus cci auto-renew off
slaigpus cci auto-renew on
```

开关持久保存在本地，不需要打开 Chrome。关闭后不会开始下一轮自动续期，但不会粗暴中断已开始的镜像 POST/PATCH 或恢复对账。显式执行 `cci renew` 始终代表人工授权，不受该开关影响。

`auto-renew on/off/status` 只控制开关；目标由 `controller --cci ...`、`open --cci ...` 或 `cci watch --cci ...` 指定。

推荐给 agent 的机器可读命令：

```bash
slaigpus cci remaining --json
slaigpus cci auto-renew status --json
slaigpus cci renew --if-due --json
```

### 自动续期流程

自动化严格复现控制台上的人工顺序：

1. 从 workspace 列出 CCI，解析唯一 app、运行中实例、主容器和私有镜像 namespace；首次保存且没有成功快照时，读取各 namespace 的实时剩余容量并选择唯一最大者。
2. 读取实例的 `last_started_time`。
3. 到达续期时间后，以当前实例和主容器创建私有镜像快照。
4. 轮询快照；只有 `SUCCESS` 且返回完整 `uri` 时才继续。
5. 重新 GET app，保留完整 template，只把目标容器的 `image_path` 替换成快照 `uri`。
6. PATCH 同一个 CCI，等待新实例 `RUNNING`、目标容器 ready、启动时间严格变新，并确认当前镜像就是快照 `uri`。
7. 验收成功后清理恢复状态并开始下一轮计时。

`FAIL`、`INVALID`、超时、认证失败、目标变化或歧义都不会继续修改 CCI。

镜像保存在当前 workspace 选中的 SenseCore 私有镜像 namespace，不下载到本机：

- 仓库名：`slaigpus-<app>`，转为合法小写字符并限制为 63 字符；
- 显示名：`slaigpus-auto-YYYYMMDD-HHMMSS-<8位随机串>`，时间为 UTC；
- 历史快照默认保留，不自动删除。

namespace 的选择优先级是：显式 `--namespace`、该 CCI 唯一的历史成功快照 namespace、唯一可用 namespace。若这是首次保存、没有历史成功快照且存在多个可用 namespace，程序会先通过 management API 列出 ACTIVE 的 CCR namespace，再读取控制台使用的每个 namespace `/info` 接口，以 `storageLimit - storageUsed`（字节）计算实时剩余容量并选择唯一最大者。当前容器的基础镜像路径不再被误当作首次快照的保存位置。

若任一容量字段缺失、不是非负整数字节数、已用量大于总量，或最大剩余容量并列，程序会在创建快照前停止并要求显式传入 `--namespace`，不会猜测。

### 固定目标

若 app、实例或容器有多个候选，程序会停止写操作并列出候选。namespace 在无法根据历史成功快照或唯一最大剩余容量安全决定时也会停止。可显式固定：

```bash
slaigpus controller \
  --cci CCI_NAME_OR_DISPLAY_NAME \
  --container CONTAINER \
  --namespace NAMESPACE

slaigpus cci status \
  --cci CCI_NAME_OR_DISPLAY_NAME \
  --instance INSTANCE \
  --container CONTAINER \
  --namespace NAMESPACE
```

`--cci` 在 `controller`、`open` 和全部 CCI 查询/续期命令中名称相同。旧的 `--app`（controller/CCI 子命令）与 `--cci-app`（open）继续兼容。

`--instance` 只固定续期开始时的旧实例。PATCH 后平台会生成新实例，验收阶段会释放旧实例选择器，再要求新的运行中实例唯一，并继续核对 app、container、镜像和启动时间。

### 恢复状态和并发保护

本地状态目录：

- macOS：`~/Library/Application Support/slaigpus/cci/`
- Linux：`$XDG_STATE_HOME/slaigpus/cci/`
- Linux 默认：`~/.local/state/slaigpus/cci/`

目录必须是当前用户拥有的真实 `0700` 目录；状态、控制和锁文件必须是当前用户拥有的普通单链接 `0600` 文件。本地文件锁阻止两个 watcher 同时修改同一 workspace。

进程在保存镜像、PATCH 或等待重启期间退出后，下次运行会先与服务端对账。未知 POST/PATCH 结果不会盲目重放。只有幂等 GET 可以在 401 后刷新认证并自动重试；POST/PATCH 的未知结果会保留状态，等待后续 GET 核对。

## ACP 任务

ACP 命令和 CCI 管理使用同一套登录配置与 automation profile。当前 Console、ACP/AEC2 API 和认证 allowlist 只验证过 `cn-sh-01`；其他区域会在启动浏览器前被拒绝。

### 固定硬件配置库

GPU 类型、GPU 卡数、CPU 类型、vCPU 和内存是不可拆分的原子配置。程序内置 11 项从标准资源和闲时资源的非 Debug 计算池确认的配置：

```bash
slaigpus acp profiles
slaigpus acp profiles --resource-class spot
slaigpus acp profiles --resource-class standard --json
```

查看配置库不需要 Chrome 或登录。

默认 profile：

```text
n6ls-80g-sxm5-2x-28c-396g
NVIDIA N6lS-80G-SXM5 / 2 卡
Intel 8468-2.1GHz / 28 vCPU / 396 GiB
API spec: N6lS.Iu.I10.2.28c396g
```

全部可选配置如下。每项都是不可拆分的完整配置，均使用 `NVIDIA N6lS-80G-SXM5` GPU、`Intel 8468-2.1GHz` CPU，同时支持标准资源和闲时资源：

| `--resource-profile` | ACP API spec | GPU 卡数 | vCPU | 内存（GiB） |
|---|---|---:|---:|---:|
| `n6ls-80g-sxm5-1x-22c-230g` | `N6lS.Iu.I10.1` | 1 | 22 | 230 |
| `n6ls-80g-sxm5-2x-44c-460g` | `N6lS.Iu.I10.2` | 2 | 44 | 460 |
| `n6ls-80g-sxm5-4x-88c-920g` | `N6lS.Iu.I10.4` | 4 | 88 | 920 |
| `n6ls-80g-sxm5-8x-176c-1840g` | `N6lS.Iu.I10.8` | 8 | 176 | 1840 |
| `n6ls-80g-sxm5-1x-8c-128g` | `N6lS.Iu.I10.1.8c128g` | 1 | 8 | 128 |
| `n6ls-80g-sxm5-1x-14c-198g` | `N6lS.Iu.I10.1.14c198g` | 1 | 14 | 198 |
| `n6ls-80g-sxm5-2x-28c-396g`（默认） | `N6lS.Iu.I10.2.28c396g` | 2 | 28 | 396 |
| `n6ls-80g-sxm5-4x-56c-792g` | `N6lS.Iu.I10.4.56c792g` | 4 | 56 | 792 |
| `n6ls-80g-sxm5-6x-84c-1188g` | `N6lS.Iu.I10.6.84c1188g` | 6 | 84 | 1188 |
| `n6ls-80g-sxm5-8x-64c-1024g` | `N6lS.Iu.I10.8.64c1024g` | 8 | 64 | 1024 |
| `n6ls-80g-sxm5-8x-112c-1584g` | `N6lS.Iu.I10.8.112c1584g` | 8 | 112 | 1584 |

也可以直接从当前安装版本查询相同列表：

```bash
slaigpus acp profiles
slaigpus acp profiles --resource-class standard
slaigpus acp profiles --resource-class spot
```

`--resource-profile` 只能从固定库选择。不存在独立的 `--gpus`、`--cpus`、`--memory-gib` 或任意 `--resource-spec` 参数。

资源类别：

- `--resource-class spot`：闲时资源，创建 API 使用 `SPOT`；
- `--resource-class standard`：标准资源，创建 API 使用 `RESERVED`；
- 不传 `--resource-class` 时，正式学生默认 `standard`，RA 默认 `spot`；
- 显式传入 `--resource-class` 始终覆盖身份默认值；
- Debug 集群永远不进入候选。

### dry-run 和提交

提交前必须已经完成 `slaigpus configure`。ACP 提交只有三个必填业务参数：

| 参数 | 是否必需 | 要求 |
|---|---|---|
| `--name JOB_NAME` | 是 | 新任务的 API 名称；只能使用小写字母、数字和连字符，长度 `1–63`，开头和结尾必须是字母或数字 |
| `--image PRIVATE_IMAGE` | 是 | Worker 使用的完整私有镜像地址和 tag |
| `--command 'START_COMMAND'` | 是 | Worker 启动命令；`--startup` 是等价别名，包含空格时必须加引号 |

最小 dry-run 命令如下。没有 `--apply` 时只读取模板、资源池和配额并生成计划，不创建任务：

```bash
slaigpus acp submit \
  --name new-job-name \
  --image 'PRIVATE_REGISTRY/NAMESPACE/IMAGE:TAG' \
  --command 'python train.py'
```

确认 dry-run 输出中的 Worker、副本数、资源类别、固定 profile 和资源池后，原命令增加 `--apply` 才会真正创建：

```bash
slaigpus acp submit \
  --name new-job-name \
  --image 'PRIVATE_REGISTRY/NAMESPACE/IMAGE:TAG' \
  --command 'python train.py' \
  --resource-profile n6ls-80g-sxm5-2x-28c-396g \
  --resource-class spot \
  --apply
```

完整可选参数：

| 参数 | 默认值 | 作用 |
|---|---|---|
| `--display-name TEXT` | 与 `--name` 相同 | 控制台显示名称，最长 128 个字符，可与 API 名称不同 |
| `--resource-profile PROFILE` | `n6ls-80g-sxm5-2x-28c-396g` | 从本文固定硬件库选择一整组 GPU/CPU/内存配置 |
| `--resource-class standard\|spot` | 正式学生为 `standard`；RA 为 `spot` | 选择标准资源或闲时资源；显式值覆盖首次配置中的身份默认值 |
| `--workspace RESOURCE_ID` | 内置 SLAI workspace | 高级临时覆盖；日常无需传入 |
| `--template-job JOB_NAME` | 无 | 显式从指定已有任务继承安全白名单字段 |
| `--no-template` | 否 | 显式要求 portable defaults；与 `--template-job` 互斥，不传模板时本来就是 portable 模式 |
| `--worker-config FILE` | 无 | 读取副本、挂载、环境变量和 barrier；必须是绝对路径、普通单链接、权限 `0600` 的私有 JSON |
| `--replicas N` | 模板值或 portable 的 `1` | 覆盖 Worker 副本数，允许 `1–10000`；别名为 `--worker-replicas` |
| `--clear-mounts` | 否 | 即使模板中有挂载，也显式替换为空列表 |
| `--clear-env` | 否 | 即使模板中有环境变量，也显式替换为空列表 |
| `--direct` | 使用 TOML | 本次提交临时强制直连 |
| `--ssh-host ALIAS` | 使用 TOML | 本次提交临时使用 `~/.ssh/config` 的代理别名；与 `--direct` 互斥 |
| `--credentials-file PATH` | 配置助手保存的凭据 | 使用指定绝对路径的私有登录凭据 JSON |
| `--headless` | 否 | 需要临时浏览器时禁止显示登录窗口，自动登录失败即退出 |
| `--no-probe` | 否 | 跳过提交前的网络连通性检查 |
| `--json` | 否 | 输出脱敏的机器可读计划或提交结果 |
| `--apply` | 否 | 真正创建任务；不传时永远只是 dry-run |

`--apply` 只对本次刚生成的计划发送一次创建 POST。认证过期、传输失败或结果不确定时不会自动重放，避免重复任务。再次执行命令会重新读取资源绑定并生成新计划。

dry-run 和 `--json` 只输出白名单摘要，不回显完整提交 body、模板内容、私有镜像、启动命令或创建响应体。启动命令会进入 shell history 和进程参数，也可能由平台展示，不要在其中写密码或 token。

### 模板和 portable 模式

默认不读取任何已有任务，直接使用 portable defaults，因此仓库和新账号都不依赖某个私有模板。只有显式传入 `--template-job` 时，才从该任务继承允许的 Worker 副本数、挂载、环境变量和其他白名单字段。

```bash
# 显式使用当前账号可访问的模板
slaigpus acp submit \
  --name NEW_JOB_NAME \
  --image PRIVATE_IMAGE \
  --command 'START_COMMAND' \
  --template-job THEIR_JOB

# 与默认行为相同，但显式声明完全不读取模板
slaigpus acp submit \
  --name NEW_JOB_NAME \
  --image PRIVATE_IMAGE \
  --command 'START_COMMAND' \
  --no-template
```

portable defaults：

- framework：`PYTORCH`
- Worker：1 个
- 挂载：空
- 环境变量：`NCCL_IB_TIMEOUT=22`、`NCCL_IB_RETRY_CNT=13`、`NCCL_IB_AR_THRESHOLD=0`

模板读取失败或名称错误不会静默降级。

### Worker 副本、挂载和环境变量

`--replicas N`（别名 `--worker-replicas N`）可覆盖 Worker 副本数。挂载、环境变量和多副本 barrier 通过 `0600` 私有 JSON 配置：

```json
{
  "version": 1,
  "replicas": 1,
  "mounts": [],
  "env": [
    {"key": "NCCL_IB_TIMEOUT", "value": "22"},
    {"key": "NCCL_IB_RETRY_CNT", "value": "13"},
    {"key": "NCCL_IB_AR_THRESHOLD", "value": "0"}
  ]
}
```

```bash
chmod 700 ~/.config/slaigpus
chmod 600 ~/.config/slaigpus/acp-worker.json

slaigpus acp submit \
  --name NEW_JOB_NAME \
  --image PRIVATE_IMAGE \
  --command 'START_COMMAND' \
  --no-template \
  --worker-config ~/.config/slaigpus/acp-worker.json
```

`--worker-config` 必须是绝对路径；`~` 会展开，`./worker.json` 会被拒绝。`--replicas` 优先于 JSON；`--clear-mounts`、`--clear-env` 可显式清空对应列表。

模板中的 AFS UUID 属于账号，不能写死在可分发代码中。其他账号应提供自己的完整 mount 对象。多副本任务必须继承或显式提供已验证的 `barrier`，程序不会猜协议或端口。

### 资源池选择语义

候选必须状态为 `ACTIVE`、属于所选类别、不是 Debug、提供所选固定 profile，并通过 Worker 总副本数容量检查。

闲时资源使用当前 `spot_quota` 计算：

```text
min(GPU / 每副本 GPU, CPU / 每副本 CPU, 内存 / 每副本内存)
```

程序选择相对余量最大的闲时池。

标准资源 API 只返回 `reserved_number`、`reserved_cpu`、`reserved_memory` 分配额度，没有 member usage 或实时空闲字段。程序只能检查预留上限并选择相对额度最大的池，不能保证立即调度，也不会称其为实时剩余。

JSON 的 `resource_pool.capacity_basis` 返回：

- 闲时：`current_spot_quota`
- 标准：`reserved_entitlement_without_usage`

`standard` 和 `spot` 不互相回退；模板中的 `ON_DEMAND` 不进入新提交。

### ACP 容器日志

日志通过 Monitor 的 PRIVATE telemetry station 查询。必须显式提供完整 telemetry station resource id；程序不会根据 workspace 猜测，也不会从 Cookie 或 localStorage 寻找。

在已登录的 `https://console.sensecore.cn/cn-sh-01/monitor/log/list` 打开 DevTools Network，找到成功的：

```text
.../telemetryStations/<name>/logStream/products
```

只复制 URL path 中从 `/subscriptions/` 到 `/telemetryStations/<name>` 的部分。不要复制请求头、Cookie、Authorization 或 “Copy as cURL”。

```bash
slaigpus acp logs \
  --job NEW_JOB_NAME \
  --telemetry-station \
  '/subscriptions/SUBSCRIPTION/resourceGroups/GROUP/zones/ZONE/telemetryStations/STATION'
```

筛选和持续查看：

```bash
slaigpus acp logs \
  --job NEW_JOB_NAME \
  --telemetry-station TELEMETRY_STATION_RID \
  --pod POD_NAME \
  --container CONTAINER_NAME \
  --host HOST_IP \
  --since 1h \
  --follow \
  --poll-interval 5s
```

`--pod`、`--container`、`--host` 可重复。产品只允许 `product.lepton-acp` 和 `product.lepton-acp-new`；未指定 `--product` 时优先后者。

`--follow` 是分页轮询，不是 SSE/WebSocket。程序跨轮去重；按 `Ctrl-C` 停止。follow 要求 `offset=0`。单次 `--json` 返回完整页面；与 `--follow` 同用时输出 JSON Lines。

默认查询最近 `1h`、每页 `40` 条、按时间倒序。`--page-size`、`--offset`、`--order` 控制分页和排序，`--filter` 提供 Monitor 全文筛选。ACP 的 `submit` 和 `logs` 都支持 `--workspace`、`--headless`、`--credentials-file` 和 `--no-probe`。

Monitor 查询虽然使用 HTTP POST，但只是读取日志和筛选值，不创建或修改 ACP 任务。

## 自动登录和安全边界

自动登录只对 slaigpus 自己启动、使用受信任系统 Chrome 的内建 SenseCore 流程生效。网络方式不会改变可信登录域名、企业标识或 API allowlist。外部 `--cdp-port`、自定义 Chrome 参数/路径和 `SLAIGPUS_CHROME` 不会收到凭据；显式传入凭据但不满足边界时直接拒绝。

登录流程：

1. 从精确入口 `https://zhicheng.signin.sensecore.cn/` 开始。
2. 只接受由该入口产生的 Console、OAuth、IAM 和 shared-signin challenge 链。
3. challenge 必须保持非空 `login_challenge` 和 `platform=console`。
4. IAM 用户页面必须保持同一 challenge，并包含空的大写 `IAM` 标记。
5. 只在验证过的 IAM 表单中把 `tenant_code` 固定填为 `zhicheng`，再填写账号密码。
6. 到达可信 Console 终点后，只从同一受控 session 对精确只读 IAM `myRegionAndAzs` 请求捕获成功的 2xx Bearer。
7. 在同一个 automation Chrome 隔离环境中请求 allowlist 内的 CCI、ACP 和 Monitor API。

程序不会直接以 CCI 页面作为登录入口，也不依赖 CCI 微前端先渲染。它不读取或解密 Cookie、`localStorage`、`sessionStorage`，不把 Bearer 写入配置、恢复状态、日志或命令行。

一次登录阶段最多提交一次凭据。错误密码、验证码、MFA、Passkey、改密页、未知字段、异常跳转或页面结构变化都会失败闭合。

可见登录 bootstrap 和 automation Chrome 的 DevTools 都只绑定 `127.0.0.1`。loopback 不是 Unix 用户 ACL；不要转发 DevTools 端口，也不要允许不受信任的本地用户扫描端口。

私有 JSON 是权限受限的明文 secret 文件。它能防止其他普通系统用户和误提交读取，但不能防止同一 Unix 账号下的恶意进程、root、备份系统或磁盘取证。需要更强边界时，应由主机 secret manager 以 `0600` 文件下发。

`credentials delete` 只禁止未来自动填充，不清除 Chrome profile 中已有 Cookie；立即注销仍需在网页完成。`credentials status` 只检查安全元数据，不显示账号或密码。

## 设计说明

**为什么 profile 持久化。** Cookie 和登录会话需要跨进程保留。不同用途使用不同 profile，防止锁冲突和权限扩大。

**为什么 POST/PATCH 不自动重放。** CCI 快照、CCI PATCH 和 ACP 创建任务可能在响应丢失时已经成功。程序保留恢复状态或返回不确定结果，不冒险重复创建、重复重启。

## 排错

**启用 SSH 代理后无法连接**

确认配置的 `Host` 别名存在于当前系统账号的 `~/.ssh/config`，并且该账号可以非交互连接。

**无法安全选择 CCI app、实例、容器或 namespace**

根据错误候选使用 `--app`、`--instance`、`--container`、`--namespace` 固定目标。首次保存时会自动选择唯一剩余容量最大的 namespace；若 CCR `/info` 容量无效或最大值并列，仍需显式指定。消除歧义前不会保存镜像或 PATCH。

**headless 自动登录失败**

运行 `slaigpus credentials status`，确认凭据目录为 `0700`、文件为 `0600`，并且使用受信任系统 Chrome。SSH 模式还需确认别名可非交互使用。验证码、MFA、错误密码或登录页变化都会使 headless 操作退出。修正前不要让服务管理器无限重启。

**镜像保存失败或超时**

原 CCI 不会被修改。检查控制台快照列表、namespace 容量和本地恢复状态后重试 `slaigpus cci renew`。

**`CCI PATCH has an unknown outcome`**

程序不会发送第二次 PATCH。等待控制台状态刷新后重试 `slaigpus cci renew` 完成对账；必要时可在控制台人工选择错误中给出的镜像，再重试命令清理状态。

**ACP dry-run 找不到资源池**

运行 `slaigpus acp profiles` 确认 profile，并检查 `standard` / `spot`、Worker 副本数和 workspace 资源。Debug 池不会被选中。

**ACP 日志找不到 telemetry station**

必须从 Monitor 页面成功请求的 URL path 复制完整 telemetry station RID。不要传 workspace RID，也不要猜 station 名称。

## 测试

```bash
pip install pytest
python -m pytest tests/ -v
```

测试使用离线替身，不会访问真实 SenseCore 环境。覆盖登录状态机、CCI 发现/续期/恢复、ACP 固定资源与提交边界、Monitor 日志分页和 CLI 参数。
