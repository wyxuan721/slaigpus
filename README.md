# slaigpus

`slaigpus` 是面向深圳河套学院（SLAI）商汤大装置环境的命令行自动化工具，主要用于：

- 查询 CCI 状态和剩余运行时间；
- 在 CCI 运行满 `3h50m` 时自动执行“保存私有镜像 → 切换镜像 → 等待重启”；
- 打开带持久登录态的可见 Chrome；
- 查询 ACP 固定硬件配置、提交任务和读取容器日志。

项目要求 Python 3.9+ 和官方 Google Chrome，支持 macOS 以及 Ubuntu 22.04/24.04 amd64。

## Quick Start

### 1. 安装

先安装 Python 3.9+、`pipx` 和官方 Google Chrome，随后执行：

```bash
pipx ensurepath
pipx install \
  https://github.com/wyxuan721/slaigpus/releases/download/v0.6.0/slaigpus-0.6.0-py3-none-any.whl
```

首次执行 `pipx ensurepath` 后应重新打开终端。验证命令已经进入 PATH：

```bash
slaigpus --help
```

Ubuntu 和 macOS 的完整依赖安装方法见[安装与更新](#安装与更新)。

### 2. 首次配置

```bash
slaigpus configure
```

配置助手会依次询问：

1. SenseCore IAM 账号和密码；
2. 身份：正式学生或 RA；
3. 使用直连还是 `~/.ssh/config` 中的 SSH `Host` 代理别名。

账号和密码按项目约定在输入时明文显示。密码保存在权限为 `0600` 的私有 JSON 中，不写入 TOML，也不使用系统钥匙串。

完成后先做只读检查：

```bash
slaigpus credentials status
slaigpus cci status --cci 'CCI_NAME_OR_DISPLAY_NAME'
slaigpus cci remaining --cci 'CCI_NAME_OR_DISPLAY_NAME'
```

### 3. 启动 CCI 自动续期

`--cci` 可以填写 CCI 的内部名称或页面显示名称：

```bash
slaigpus cci auto-renew on
slaigpus controller --cci 'CCI_NAME_OR_DISPLAY_NAME'
```

控制器会常驻前台，每 30 秒检查一次。目标 CCI 本轮运行达到 `3h50m` 后，控制器保存当前容器镜像、切换到新镜像并等待平台重启。长期运行建议配置为 [systemd 用户服务](#使用-systemd-常驻运行)。

常用操作：

```bash
# 查询开关和剩余时间
slaigpus cci auto-renew status
slaigpus cci remaining --cci 'CCI_NAME_OR_DISPLAY_NAME'

# 立即主动续期一次
slaigpus cci renew --cci 'CCI_NAME_OR_DISPLAY_NAME'

# 暂停或恢复后续自动续期
slaigpus cci auto-renew off
slaigpus cci auto-renew on
```

### 4. 提交 ACP 任务

先执行 dry-run；没有 `--apply` 时不会创建任务：

```bash
slaigpus acp submit \
  --name new-job-name \
  --image 'PRIVATE_REGISTRY/NAMESPACE/IMAGE:TAG' \
  --command 'python train.py'
```

确认 Worker、副本数、硬件配置和资源池后，在原命令末尾增加 `--apply`：

```bash
slaigpus acp submit \
  --name new-job-name \
  --image 'PRIVATE_REGISTRY/NAMESPACE/IMAGE:TAG' \
  --command 'python train.py' \
  --apply
```

正式学生默认使用标准资源，也可以显式选择闲时资源；RA 只能使用闲时资源，显式选择 `standard` 会在任何 ACP 请求前被拒绝。通过 `--resource-profile` 选择固定硬件配置。

## 固定环境与命令概览

登录地址、企业标识、区域和 workspace 均按 SLAI 环境内置，无需自行配置：

| 项目 | 固定值 |
|---|---|
| 企业登录入口 | `https://zhicheng.signin.sensecore.cn/` |
| 企业标识 | `zhicheng` |
| 区域 | `cn-sh-01` |
| workspace | `share-space-01e` |
| 自动续期点 | CCI 本轮运行满 `3h50m` |
| 强制过期时间 | `4h` |

主要命令：

| 命令 | 功能 |
|---|---|
| `slaigpus configure` | 首次配置或修改账号、身份和网络方式 |
| `slaigpus viewer` | 打开可见工作 Chrome |
| `slaigpus controller` | 运行 headless CCI 续期控制器 |
| `slaigpus open` | 同时启动可见 Chrome 和独立的 CCI 续期任务 |
| `slaigpus credentials set/status/delete` | 管理自动登录凭据 |
| `slaigpus cci status` | 查询 CCI、实例、容器和运行时间 |
| `slaigpus cci remaining` | 查询距离续期点和强制过期还有多久 |
| `slaigpus cci renew` | 保存镜像、切换镜像并等待重启 |
| `slaigpus cci auto-renew on/off/status` | 持久开关或查询自动续期 |
| `slaigpus cci watch` | 在前台等待并按时续期 |
| `slaigpus acp profiles` | 列出固定的非 Debug ACP 硬件配置 |
| `slaigpus acp submit` | dry-run 或提交 ACP 任务 |
| `slaigpus acp logs` | 查询或持续读取 ACP 容器日志 |

`--json` 不是必需参数。人在终端使用时可省略；agent 或脚本需要稳定字段时再添加。

## 安装与更新

### 系统依赖

Ubuntu 22.04/24.04 amd64：

```bash
sudo apt update
sudo apt install -y curl ca-certificates pipx
python3 --version
pipx --version
```

只有使用 SSH 代理时才需要 OpenSSH 客户端：

```bash
sudo apt install -y openssh-client
```

安装官方 Google Chrome：

```bash
curl -fLO https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb
sudo apt install -y ./google-chrome-stable_current_amd64.deb
google-chrome-stable --version
```

macOS：

```bash
brew install pipx
brew install --cask google-chrome
python3 --version
pipx --version
```

如果 Python 版本低于 3.9，应先升级 Python。不要使用 `sudo pip install`，否则容易出现权限和 PATH 问题。

无人值守自动登录只向受信任的系统级 Google Chrome 提供凭据。Snap/Flatpak Chromium、自定义 `SLAIGPUS_CHROME` 或权限异常的浏览器可以用于人工浏览，但不能自动填充账号密码。

### 安装、更新和卸载

安装当前 Release：

```bash
pipx ensurepath
pipx install \
  https://github.com/wyxuan721/slaigpus/releases/download/v0.6.0/slaigpus-0.6.0-py3-none-any.whl
```

强制更新或重新安装当前版本：

```bash
pipx install --force \
  https://github.com/wyxuan721/slaigpus/releases/download/v0.6.0/slaigpus-0.6.0-py3-none-any.whl
systemctl --user restart slaigpus-controller.service
```

卸载：

```bash
pipx uninstall slaigpus
```

卸载不会删除配置、凭据、Chrome profile 或 CCI 恢复状态。

### 从源码安装

只有开发时需要 Git 和 editable 安装：

```bash
git clone https://github.com/wyxuan721/slaigpus.git
cd slaigpus
pipx install --editable .
```

## 配置、凭据和网络

### 引导式配置

安装后首次运行需要配置；后续修改时执行同一命令：

```bash
slaigpus configure
```

可选参数：

| 参数 | 默认值 | 作用 |
|---|---|---|
| `--config PATH` | `~/.config/slaigpus/config.toml` | 指定要维护的 TOML |
| `--credentials-file PATH` | `~/.config/slaigpus/credentials.json` | 指定绝对路径的私有凭据 JSON |

交互示例：

```text
$ slaigpus configure
SenseCore 账号: YOUR_IAM_ACCOUNT
SenseCore 密码: YOUR_PASSWORD
请选择身份（1=正式学生/标准资源，2=RA/闲时资源）: 2
是否使用 SSH 代理？[y/N]: y
请输入 SSH Host 别名: sensecore-proxy
```

身份决定 ACP 的默认资源类别：

- 正式学生：标准资源 `standard`；
- RA：闲时资源 `spot`。

交互式命令发现配置缺失时会自动启动助手。agent、systemd 或管道等非交互环境不会等待输入，而会提示先运行 `slaigpus configure`。

### 凭据文件

`configure` 将账号和密码保存到私有 JSON；也可单独管理：

```bash
slaigpus credentials set
slaigpus credentials status
slaigpus credentials delete
```

`configure` 使用明文输入且不二次确认；`credentials set` 使用隐藏输入并要求确认。两者写入同一种文件。slaigpus 不使用系统钥匙串。

默认路径在 macOS 和 Linux 上均为：

```text
~/.config/slaigpus/credentials.json
```

凭据目录必须是当前用户拥有的真实 `0700` 目录；凭据文件必须是当前用户拥有、权限为 `0600` 的普通单链接文件。符号链接、硬链接、异常所有者和宽松权限都会被拒绝。

若在多台机器上使用，应分别运行 `slaigpus configure`，不要复制凭据、Cookie 或 Chrome profile。

### 直连与 SSH 代理

默认使用直连。选择代理时，先在 `~/.ssh/config` 中配置标准 OpenSSH `Host`：

```sshconfig
Host sensecore-proxy
    HostName SSH_GATEWAY_HOST
    User SSH_USER
    IdentityFile ~/.ssh/id_ed25519
```

随后在 `slaigpus configure` 中只填写别名 `sensecore-proxy`。slaigpus 不保存 SSH 地址、用户或密钥内容。

临时覆盖持久配置：

```bash
slaigpus controller --direct --cci CCI_NAME
slaigpus controller --ssh-host sensecore-proxy --cci CCI_NAME
```

`--direct` 与 `--ssh-host` 互斥。运行 systemd 服务的账号必须能非交互使用该 SSH 别名。修改网络模式后应重启已运行的 Chrome 或 CCI 续期控制器。

## 浏览器与 CCI 续期控制器

slaigpus 使用两个相互独立的持久 Chrome profile：

- work profile：供可见工作 Chrome 使用；
- automation profile：供 CCI 续期控制器、CCI 命令和 ACP 后台 API 使用。

两者不共享 Cookie，也不会争抢 profile 锁。

### 可见工作 Chrome

```bash
slaigpus viewer
```

`viewer` 先在可见窗口中完成登录，再打开普通工作窗口。验证码、MFA 或未知页面不会触发重复密码提交，而会保留窗口供人工处理。

需要本地脚本连接时可启用 loopback DevTools：

```bash
slaigpus viewer --cdp
```

默认地址为 `127.0.0.1:9222`，不要把该端口转发给其他机器。

### CCI 续期控制器

```bash
slaigpus controller --cci 'CCI_NAME_OR_DISPLAY_NAME'
```

`controller` 是专门负责 CCI 续期的常驻进程。它使用 headless automation Chrome 自动登录并循环执行：

1. 精确选择目标 CCI，从服务端 `last_started_time` 计算运行时间；
2. 每 30 秒检查状态和本地自动续期开关；
3. 达到 `3h50m` 后保存当前容器为私有镜像；
4. 快照成功后更新 CCI 镜像并等待平台重启；
5. 验证新实例、镜像和启动时间，然后进入下一轮。

关闭自动续期开关后，控制器仍可保持登录态和查询状态，但不会开始下一次自动续期。登录失败时 headless 控制器会退出，不会弹出人工窗口。

控制器必须运行在不会随目标 CCI 一起销毁的机器上，否则无法在目标重启期间完成验收和恢复。

组合模式会同时启动可见 Chrome 和独立的 CCI 续期任务：

```bash
slaigpus open --cci 'CCI_NAME_OR_DISPLAY_NAME'
slaigpus open --no-cci-watch
```

## 使用 systemd 常驻运行

先在前台确认控制器能够登录并正确选择目标：

```bash
slaigpus controller --cci 'CCI_NAME_OR_DISPLAY_NAME'
```

然后创建 `~/.config/systemd/user/slaigpus-controller.service`：

```ini
[Unit]
Description=slaigpus CCI renewal controller
After=network-online.target
Wants=network-online.target
StartLimitIntervalSec=15min
StartLimitBurst=3

[Service]
Type=simple
ExecStart=%h/.local/bin/slaigpus controller --cci CCI_INTERNAL_NAME
UMask=0077
Restart=on-failure
RestartSec=60s

[Install]
WantedBy=default.target
```

将 `CCI_INTERNAL_NAME` 和 `ExecStart` 路径替换为实际值。仓库也提供 [examples/slaigpus-controller.service](examples/slaigpus-controller.service)。

```bash
sudo loginctl enable-linger "$USER"
systemctl --user daemon-reload
systemctl --user enable --now slaigpus-controller.service
journalctl --user -u slaigpus-controller.service -f
```

修复故障后可重新启动：

```bash
systemctl --user reset-failed slaigpus-controller.service
systemctl --user restart slaigpus-controller.service
```

## CCI 管理参考

### 常用命令

```bash
# 只读查询
slaigpus cci status --cci CCI_NAME_OR_DISPLAY_NAME
slaigpus cci remaining --cci CCI_NAME_OR_DISPLAY_NAME

# 立即续期，或仅在达到 3h50m 后续期
slaigpus cci renew --cci CCI_NAME_OR_DISPLAY_NAME
slaigpus cci renew --cci CCI_NAME_OR_DISPLAY_NAME --if-due

# 前台持续等待；--once 表示下一次成功续期后退出
slaigpus cci watch --cci CCI_NAME_OR_DISPLAY_NAME
slaigpus cci watch --cci CCI_NAME_OR_DISPLAY_NAME --once

# 持久自动续期开关
slaigpus cci auto-renew status
slaigpus cci auto-renew off
slaigpus cci auto-renew on
```

`status` 返回 app、实例、容器、服务端启动时间、运行时长和续期时间；`remaining` 重点返回距离 `3h50m` 续期点和 `4h` 强制过期的剩余时间。程序按服务端 `last_started_time` 计时，而不是按 slaigpus 启动时间计时。

显式执行 `cci renew` 代表人工授权，不受自动续期开关影响。`renew` 本身就是写操作，不需要额外传 `--apply`。

推荐给 agent 的机器可读命令：

```bash
slaigpus cci remaining --cci CCI_NAME_OR_DISPLAY_NAME --json
slaigpus cci auto-renew status --json
slaigpus cci renew --cci CCI_NAME_OR_DISPLAY_NAME --if-due --json
```

### 参数

以下参数适用于 `controller`、`open` 及相应 CCI 子命令；具体支持范围以 `--help` 为准。

| 参数 | 默认值 | 作用 |
|---|---|---|
| `--cci NAME_OR_DISPLAY_NAME` | 单个 CCI 时可省略 | 按内部 `name` 或页面 `display_name` 精确选择，存在多个 CCI 时必须指定 |
| `--instance NAME_OR_UID` | 自动发现 | 多个运行中实例无法唯一选择时固定旧实例 |
| `--container NAME` | 自动发现 | 多个主容器时指定要保存和替换镜像的容器 |
| `--namespace NAME` | 自动发现 | 私有镜像 namespace 无法安全选择时显式指定 |
| `--renew-after DURATION` | `3h50m` | 自动续期阈值，必须小于 `4h` |
| `--poll-interval DURATION` | `30s` | 状态轮询间隔 |
| `--wait-timeout DURATION` | `15m` | 保存镜像和等待重启的上限 |
| `--direct` | 使用 TOML | 本次临时强制直连 |
| `--ssh-host ALIAS` | 使用 TOML | 本次临时使用 SSH `Host` 别名，与 `--direct` 互斥 |
| `--credentials-file PATH` | 配置助手保存的文件 | 使用指定绝对路径的凭据 JSON |
| `--headless` | 否 | 临时浏览器只允许无头登录，失败时直接退出 |
| `--cdp-port PORT` | 自动发现或新建浏览器 | 连接已经运行的 slaigpus Chrome |
| `--no-probe` | 否 | 跳过启动前网络连通性检查 |
| `--json` | 否 | 输出机器可读 JSON |

`cci renew --if-due` 只在达到阈值后续期；`cci watch --once` 在下一次成功续期后退出。时间参数接受 `3h50m`、`230m`、`30s` 等格式。

### 镜像保存和目标选择

续期严格按以下顺序执行：

1. 选择唯一 CCI、运行中实例、主容器和私有镜像 namespace；
2. 创建当前实例的私有镜像快照；
3. 仅在快照状态为 `SUCCESS` 且返回完整 `uri` 后继续；
4. 重新读取并保留完整 CCI template，只替换目标容器的 `image_path`；
5. PATCH CCI，等待新实例 `RUNNING`、容器 ready、启动时间变新且镜像匹配；
6. 验收成功后清理恢复状态并重新计时。

快照保存在 SenseCore 私有镜像 namespace，不会下载到本机：

- 仓库名：`slaigpus-<app>`，转换为合法小写字符并限制为 63 字符；
- 显示名：`slaigpus-auto-YYYYMMDD-HHMMSS-<8位随机串>`，时间使用 UTC；
- 历史快照默认保留，不自动删除。

namespace 选择顺序：

1. 显式 `--namespace`；
2. 该 CCI 唯一的历史成功快照 namespace；
3. 唯一可用 namespace；
4. 首次保存且存在多个 namespace 时，选择实时剩余容量唯一最大的 ACTIVE namespace。

容量按 `storageLimit - storageUsed` 计算。字段无效、已用量大于总量或最大值并列时，程序会在创建快照前停止并要求传入 `--namespace`。

app、实例或容器有多个候选时同样会停止写操作并列出候选，避免保存或修改错误目标。

### 恢复和并发保护

CCI 状态默认保存在：

- macOS：`~/Library/Application Support/slaigpus/cci/`；
- Linux：`$XDG_STATE_HOME/slaigpus/cci/`，未设置时为 `~/.local/state/slaigpus/cci/`。

目录权限必须为 `0700`，状态和锁文件必须为 `0600`。本地文件锁会阻止两个 watcher 同时修改同一目标。

如果进程在保存镜像、PATCH 或等待重启期间退出，下次运行会先与服务端对账。幂等 GET 可在 401 后刷新认证并重试；结果不确定的 POST/PATCH 不会自动重放。

## ACP 任务参考

### 固定硬件配置

GPU 类型、GPU 卡数、CPU 类型、vCPU 和内存是不可拆分的完整配置。查看内置配置不需要登录：

```bash
slaigpus acp profiles
slaigpus acp profiles --resource-class standard
slaigpus acp profiles --resource-class spot --json
```

所有配置均使用 `NVIDIA N6lS-80G-SXM5` GPU 和 `Intel 8468-2.1GHz` CPU，同时支持标准和闲时资源：

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

`--resource-profile` 只能选择固定库中的完整配置，不提供独立的 GPU、CPU 或内存参数。Debug 集群永远不会进入候选。

资源类别：

- `standard`：标准资源，API 使用 `RESERVED`，仅正式学生可提交；
- `spot`：闲时资源，API 使用 `SPOT`，正式学生和 RA 均可提交；
- 不指定时，正式学生默认 `standard`，RA 默认 `spot`；
- RA 显式选择 `standard` 会在启动浏览器或发送 ACP 请求前失败；资源类别不会自动回退。

### 提交参数

三个必填业务参数：

| 参数 | 要求 |
|---|---|
| `--name JOB_NAME` | 小写字母、数字和连字符，长度 1–63，首尾必须为字母或数字 |
| `--image PRIVATE_IMAGE` | Worker 使用的完整私有镜像地址和 tag |
| `--command 'START_COMMAND'` | Worker 启动命令；`--startup` 是等价别名，包含空格时需要引号 |

常用可选参数：

| 参数 | 默认值 | 作用 |
|---|---|---|
| `--display-name TEXT` | 与 `--name` 相同 | 控制台显示名称，最长 128 个字符 |
| `--resource-profile PROFILE` | `n6ls-80g-sxm5-2x-28c-396g` | 选择一整组固定硬件配置 |
| `--resource-class standard\|spot` | 由身份决定 | 选择标准或闲时资源 |
| `--replicas N` | `1` 或模板值 | Worker 副本数，允许 1–10000 |
| `--worker-config FILE` | 无 | 从私有 JSON 读取副本、挂载、环境变量和 barrier |
| `--template-job JOB_NAME` | 无 | 显式继承已有任务中的安全白名单字段 |
| `--no-template` | 否 | 显式声明使用 portable defaults，与 `--template-job` 互斥 |
| `--clear-mounts` | 否 | 将模板挂载替换为空列表 |
| `--clear-env` | 否 | 将模板环境变量替换为空列表 |
| `--direct` / `--ssh-host ALIAS` | 使用 TOML | 临时覆盖网络方式，二者互斥 |
| `--headless` | 否 | 临时浏览器只允许无头登录 |
| `--json` | 否 | 输出脱敏的机器可读计划或结果 |
| `--apply` | 否 | 真正创建任务；不传时始终为 dry-run |

`--apply` 只发送一次创建 POST。认证过期、传输失败或结果不确定时不会自动重放，以免创建重复任务。

dry-run 和 `--json` 不回显完整提交 body、私有镜像、启动命令或创建响应体。启动命令仍会进入 shell history 和进程参数，不要在其中写密码或 token。

### Portable defaults 与 Worker 配置

默认不读取任何已有任务，使用可分发的 portable defaults：

- framework：`PYTORCH`；
- Worker：1 个；
- 挂载：空；
- 环境变量：`NCCL_IB_TIMEOUT=22`、`NCCL_IB_RETRY_CNT=13`、`NCCL_IB_AR_THRESHOLD=0`。

只有显式传入 `--template-job` 时才读取已有任务。模板读取失败不会静默降级。

挂载、环境变量和多副本 barrier 可通过权限为 `0600` 的绝对路径 JSON 提供：

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
chmod 600 ~/.config/slaigpus/acp-worker.json
slaigpus acp submit \
  --name new-job-name \
  --image PRIVATE_IMAGE \
  --command 'START_COMMAND' \
  --worker-config ~/.config/slaigpus/acp-worker.json
```

`--replicas` 优先于 JSON。模板中的 AFS UUID 属于账号，其他用户应提供自己的 mount 对象。多副本任务必须继承或显式提供已验证的 `barrier`。

### 资源池选择

候选池必须处于 `ACTIVE`、属于所选资源类别、不是 Debug、支持所选 profile，并满足全部 Worker 副本的容量要求。

闲时资源根据当前 `spot_quota` 计算可容纳副本数并选择相对余量最大的池：

```text
min(GPU / 每副本 GPU, CPU / 每副本 CPU, 内存 / 每副本内存)
```

标准资源 API 没有实时使用量，只能根据预留上限选择相对额度最大的池，因此不保证立即调度。JSON 输出中的 `resource_pool.capacity_basis` 会明确返回计算依据。

### ACP 容器日志

日志查询需要完整 telemetry station resource ID。登录 Console 后，在 Monitor 日志页面的 DevTools Network 中找到成功的：

```text
.../telemetryStations/<name>/logStream/products
```

只复制 URL path 中从 `/subscriptions/` 到 `/telemetryStations/<name>` 的部分，不要复制 Cookie、Authorization 或 “Copy as cURL”。

```bash
slaigpus acp logs \
  --job JOB_NAME \
  --telemetry-station \
  '/subscriptions/SUBSCRIPTION/resourceGroups/GROUP/zones/ZONE/telemetryStations/STATION'
```

持续查看和筛选：

```bash
slaigpus acp logs \
  --job JOB_NAME \
  --telemetry-station TELEMETRY_STATION_RID \
  --pod POD_NAME \
  --container CONTAINER_NAME \
  --since 1h \
  --follow \
  --poll-interval 5s
```

`--pod`、`--container` 和 `--host` 可重复。`--follow` 使用分页轮询并跨轮去重，按 `Ctrl-C` 停止。单次 `--json` 返回完整页面；与 `--follow` 同用时输出 JSON Lines。

默认查询最近 1 小时、每页 40 条、时间倒序。Monitor 请求虽然使用 HTTP POST，但只读取日志，不创建或修改 ACP 任务。

## 登录、安全与可靠性

自动登录从精确地址 `https://zhicheng.signin.sensecore.cn/` 开始，并验证 Console/OAuth/IAM challenge 链：

1. challenge 必须保留非空 `login_challenge`、`platform=console` 和大写空值 `IAM` 标记；
2. 只在验证过的 IAM 表单中填写固定企业标识 `zhicheng`、账号和密码；
3. 到达可信 Console 后，从同一受控 session 的只读 IAM 请求获取短期 Bearer；
4. Bearer 只用于 allowlist 内的 CCI、ACP 和 Monitor API，不写入文件或日志。

程序不读取或解密 Cookie、`localStorage` 或 `sessionStorage`。一次登录最多提交一次凭据；错误密码、验证码、MFA、Passkey、改密页或未知跳转都会失败闭合。

凭据 JSON 是权限受限的明文 secret 文件，无法抵御同一 Unix 账号下的恶意进程、root、备份系统或磁盘取证。需要更强保护时，应由主机 secret manager 下发 `0600` 文件。

`credentials delete` 只禁止以后自动填充，不会删除 Chrome profile 中已有 Cookie；立即注销需要在网页中完成。

CCI 快照、CCI PATCH 和 ACP 创建任务的响应如果丢失，操作可能已经成功，因此程序不会自动重放非幂等请求。CCI 会保留恢复状态并通过后续 GET 对账。

## 排错

### `slaigpus: command not found`

重新打开终端并运行 `pipx ensurepath`。也可先检查 `~/.local/bin/slaigpus --help`。

### SSH 代理无法连接

确认 `Host` 别名存在于当前账号的 `~/.ssh/config`，并且该账号能够非交互连接。systemd 服务不会等待 SSH 密码输入。

### Headless 自动登录失败

运行 `slaigpus credentials status`，检查凭据目录为 `0700`、文件为 `0600`，并确认使用受信任的系统级 Google Chrome。验证码、MFA、错误密码或登录页变化都会使无头操作退出。

### 无法安全选择 CCI 目标或 namespace

根据错误输出使用 `--cci`、`--instance`、`--container` 或 `--namespace` 固定目标。消除歧义前程序不会保存镜像或 PATCH CCI。

### 镜像保存失败或 PATCH 结果未知

快照失败时原 CCI 不会被修改。PATCH 结果未知时程序不会发送第二次 PATCH；等待控制台状态刷新后重新运行 `slaigpus cci renew` 完成对账。

### ACP dry-run 找不到资源池

运行 `slaigpus acp profiles` 检查固定 profile，并核对 `standard` / `spot`、Worker 副本数和当前账号的资源额度。Debug 池不会被选中。

### ACP 日志找不到 telemetry station

必须从 Monitor 页面成功请求的 URL path 复制完整 telemetry station RID；不能使用 workspace RID，也不要猜测 station 名称。

## 开发与测试

```bash
pip install pytest
python -m pytest tests/ -v
```

测试使用离线替身，不访问真实 SenseCore 环境。覆盖登录状态机、CCI 发现/续期/恢复、ACP 固定资源与提交边界、Monitor 日志分页和 CLI 参数。
