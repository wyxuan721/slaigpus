# 通用文件交互

`slaigpus transfer` 将本地、SSH 机器和对象存储之间的文件交互统一成可执行计划。文件存储已挂载时按机器路径处理。项目只提供路径、目标和传输策略，工具不理解数据集、模型、训练分组或实验目录。

## 已实现的入口

- `copy SOURCE DESTINATION`：通用复制。
- `push LOCAL --target DESTINATION` / `pull --source SOURCE --target LOCAL`：上传下载的便捷写法。
- `plan SOURCE DESTINATION`：解析路线并输出 argv，不触网、不写回执。
- `copy ... --dry-run`：让后端执行只读预演；无需固定 plan/apply 审批链。
- `status RECEIPT`：读取历史回执。
- `resume RECEIPT`：重试失败/中断任务，使用新回执保留历史。
- `verify RECEIPT --method size|sha256`：对已完成的单文件执行按需比较。

代码中只需要 `TransferTarget`（连接）、`Endpoint`（路径）、`TransferSpec`（策略）、`TransferPlan`（执行 argv）和一个执行函数，不增加插件框架、常驻传输守护进程或项目 manifest。

## 路线

| 源和目标 | 执行位置 | 后端 |
|---|---|---|
| 本地 ↔ SSH 机器/挂载目录 | 本机 | rsync over SSH |
| 本地 ↔ 对象存储 | 本机 | rclone |
| 对象存储 ↔ 机器上的目录 | `--on MACHINE` | SSH 执行远端 rclone |
| 两个对象存储 | 本机或显式远端机器 | 同一 rclone 配置中的两个 remote |
| 两个 SSH 机器 | 显式分两步 | 不默认经过本机悄悄中转 |

SSH 机器路线与既有浏览器/API SOCKS 路线独立。OpenSSH 负责 host key、key/agent、端口和跳板；对象存储认证由 rclone 配置管理。不存在登录控制台即可自动拥有 AOSS 访问密钥的假设。

## 完成、恢复与校验

执行前落盘 running 回执；退出后记录 completed、dry_run、failed 或 interrupted。进度来自后端 stderr，不复制整个日志或环境到回执。回执原子写入，默认权限 0600。运行时不启动 ACP/CCI 任务、不更改自动续期设置。

默认不覆盖、不删除。已有文件可能被跳过，完成表示后端正常退出，不表示所有旧文件被重新验证。覆盖需显式 `--overwrite`；按需单文件大小/SHA 比较可发现跳过的错误文件。默认不遍历大型目录做全量 SHA，也不新增必须提交的科学 manifest。

rsync 使用 partial-dir 和逐文件临时提交；rclone 的恢复粒度依赖 backend，跨进程重试可跳过已完成文件，但不承诺重用所有未完成 multipart。两腿对象中转显式执行、分别记录，避免把第一腿上传成功当作 AFS 已就绪。

## 本轮选择

官方 ADS 文档推荐 rclone，同时提供 ADS CLI。首版复用 rsync+rclone 即可覆盖文档中的主要自动化路线；不重复增加 SCP、FTP、rz/sz 和 ADS 适配。若需要 ADS 独有功能，再根据具体调用合同增加后端，尤其先验证它不通过公开命令参数传 AK/SK。

平台资料：[ADS](https://www.sensecore.cn/help/docs/cloud-foundation/storage/ads/)、[AOSS](https://www.sensecore.cn/help/docs/cloud-foundation/storage/aoss/)。后端行为：[rsync](https://download.samba.org/pub/rsync/rsync.1)、[rclone copyto](https://rclone.org/commands/rclone_copyto/)、[rclone 配置与传输选项](https://rclone.org/docs/)。
