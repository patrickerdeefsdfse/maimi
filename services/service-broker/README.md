# service 会话连接 broker

broker 于 r59 部署，目前应用版本为 r62。当前用户界面设计为在对话内填写 **IP、密码** 并点击连接；内部请求仍由前端补齐 root、端口 22，维持四字段协议。r62 连接区域显示尚未通过用户业务验收，r63 修复候选单独保存在 experimental。

用户连接后可以在同一对话让模型执行获授权的远程任务。密码只走人工凭据端点，不进入模型聊天。本版本取代未发布的 r58 固定共享服务器方案，不读取或复用旧 root 密码。

## 组成与启动

生产源文件只有 `main.py`、`protocol.py`、`registry.py`、`journal.py`、`service.py`、`ssh_transport.py`、`remote_program.py`。纯 Python wheel 固定为 `asyncssh-2.24.1-py3-none-any.whl`，SHA256 为 `fc560b4f43be0f0c602d184783e5e3876f5d24d933a25359d86e5a50a5f46fe5`。发布依赖既有基础镜像中的 FastAPI、uvicorn、cryptography 和 typing_extensions；本源码快照不附镜像或 wheel。`requirements.txt` 只列新增 asyncssh 版本，完整开发环境还需网关/基础镜像依赖，不新增模型客户端。

容器入口为 `python -B /app/main.py`，进程监听内部 8090。把源目录及 wheel 加入 PYTHONPATH；不要加 `-I`，因为它会忽略 PYTHONPATH。代码和私有配置以只读方式挂载，运行身份 UID10001；持久状态目录 `/var/lib/maimi-service` 由 UID10001 拥有、权限0700。SQLite 文件 `operations.sqlite3` 权限0600。无 Docker socket、主机管理端口或公开 HTTP 端口。

`/run/secrets/maimi-service-broker.json` 的完整形状：

```json
{
  "schema": "maimi-service-broker-v2",
  "enabled": true,
  "policy_generation": 1,
  "token_file": "/run/secrets/maimi-service-broker-token",
  "allowed_users": ["M00003307", "M00001069", "M00096389"]
}
```

配置和内部 token 都是0600单链接普通文件，其父目录不能有非所有者写权限或符号链接。配置没有服务器地址、SSH密码或私钥字段。v1固定主机配置会被拒绝。`/ready` 只确认本地配置和进程，没有 SSH、模型或业务请求。

## 人工连接与模型操作的边界

内部控制端点要求单一 Bearer token，身份和作用域来自网关：

| 方法及路径 | 请求 | 返回 |
| --- | --- | --- |
| POST `/v1/connections` | protocol、employee_no、scope、request_id、fields | SSH认证成功后200，返回连接状态 |
| GET `/v1/connections` | employee_no、session_id、generation 查询参数 | 当前对话连接状态，不延长闲置时间 |
| DELETE `/v1/connections` | 同上 | 断开当前代际并清理凭据；同时阻止该代际尚未完成的认证重新激活连接 |

protocol 为 `maimi-service-session-v2`。scope 为 session_id、invocation_id、job_id、generation、expires_at；request_id 必须等于 job_id。generation 是网关提供的、在同一选择与运行实例有效期间跨轮稳定的连接代际，不是每轮变化的任务编号。

fields 精确包含 host、port、username、password。host 仅接受标准十进制 RFC1918 IPv4 字面量；无 DNS、URL、localhost、环回、链路本地、公开地址或代理。port 是1至65535整数，username 是最多64字符的 Linux 登录名，password 只在人工控制端点接受，最长4096字符。每个请求最多一次 SSH 认证，最长15秒；不重试。最多两个并行认证、64个在内存中的连接。短期控制回执最多1000条，到原始作用域过期后释放，不包含密码或密码摘要。

成功响应的精确结构：

```json
{
  "protocol": "maimi-service-session-v2",
  "connected": true,
  "server": {
    "host_id": "一个不含地址的规范UUID",
    "label": "用户名@用户输入的IP:端口",
    "actions": ["inspect_server", "read_file", "write_file", "run_command"],
    "high_privilege": true
  }
}
```

断开时 server 为 null。high_privilege 表示当前登录名是 root，不是运行时 sudo 权限审计。所有工具执行仍受该 SSH 账号实际权限控制；不是沙箱。

模型使用原 `/v1/operations` 提交、轮询和取消协议，只能传不透明 host_id 和动作参数，不能传 IP、用户名、密码、连接参数或任意 HTTP 地址。一个员工的另一个对话、另一个员工、不同代际都不能使用该连接。新连接替换旧连接后，旧 host_id 作废。延迟到达的旧代际 GET/DELETE 只能看到断开，不会删除新代际连接。

所有非200响应只返回已定义的 error_code，不包含异常文本、地址、用户名或密码。HTTP禁止缓存，不启用请求访问日志和 AsyncSSH 调试日志。内部网关 token 以及当前 SSH 密码的精确字符串会从执行结果中脱敏；已知登录密码不能作为模型操作参数提交。

## 主机身份与凭据寿命

第一次遇到某员工的地址和端口时，由 AsyncSSH 主机公钥验证回调在密码认证开始前记录公钥。该公钥按员工、IP、端口保存在私有 SQLite 的 public_host_keys 表中。随后遇到变更公钥就拒绝，不能自动覆盖。没有独立管理员或控制台验真，因此这是首次使用信任，不是独立身份证明。公钥在认证失败时也可能已记录；切换主机密钥应由管理员按已核实身份另行处理。

`known_hosts=()` 使 AsyncSSH 必须调用该严格验证回调，绝不设置 `known_hosts=None`。拒绝主机证书额外信任、用户 SSH 配置、SSH agent、端口代理、X11、PTY、键盘交互、GSS和隐式客户端私钥。只使用此次人工输入的密码。

密码和活跃 SSH 连接仅在 broker 内存中，按员工+原生会话+代际隔离。两小时无远程操作即关闭；界面状态轮询不会续期。断开、替换、网络失联、超时结果不明或进程退出会移除连接和密码引用。进程重启要求重新填写。Python和SSH库的内存不能承诺法证级覆盖清零，本实现承诺不把密码写入配置、作业、聊天、日志、SQLite或请求摘要。

## 远程任务与结果不明的处理

连接阶段只认证 SSH，不执行目标命令、不创建目录、不要求预装 Python。`run_command` 通过 `/bin/sh` 从登录账号 HOME 起始，可指定已存在的相对工作目录；命令不会因前置 cd 失败而继续运行。每次命令是独立 shell，不承诺前一轮 cd、环境变量或交互状态自动保留。命令最长8192字符、最长300秒，默认90秒；最多两个并行任务，每连接串行，最多16个待执行任务。

`inspect_server` 使用固定只读命令查看操作系统、资源、运行时、NTP/SSH状态和 Oracle 工具/进程，不读数据库业务行。`read_file` / `write_file` 是 HOME 下的源文件编辑接口，使用已有 `/usr/bin/python3` 发送临时程序，不安装软件。缺少该路径的 Python 时返回 PYTHON_UNAVAILABLE，SSH与普通 shell 命令仍可使用。

文件读写限制为相对路径；拒绝路径穿越、隐藏路径、已知密钥文件、父目录/最终文件符号链接及多硬链接。读取最多32768字节。写入最多16384字节，必须给出已读文件的 SHA256 或 MISSING；新文件采用排他创建，修改先核对旧 SHA 再原子替换。此检查在本 broker 同连接内串行，但其他外部写入者仍可能在最后检查和替换间竞争，不宣称跨进程原子 CAS。远程 shell 具有登录账号权限，可按用户明确任务操作这些目录以外的位置，不受文件接口路径限制。

任务先持久化承诺再执行。重复 request_id 与相同请求只返回原承诺，不执行第二次；内容变化即冲突。接收确认丢失、断线、取消或超时发生在已下发命令之后，一律可能为 unknown，不自动重试、不声称目标进程已停止。SQLite初始化只在 broker启动时执行一次，不会因其他会话连接把正在运行的任务标为重启未知。断开后的迟到结果不进入结果存储。

SSH通道正常退出表示前台命令结束，不能证明该命令启动的后台服务也停止。私有任务日志可能保存用户授权的代码/输出，不能放进源码包或 GitLab。

## 本地验证与上线边界

合成测试仅连接容器/本机 loopback，密码、公钥和文件均由测试生成。源码校验值见 `broker-source-pins.json`；历史测试报告不作为运行时数据入库。Linux测试使用缓存镜像、数字UID10001、只读根目录、无外网网络、32MiB临时文件系统，覆盖真实 SSH 登录、首次公钥固定先于密码、密钥变更、员工/会话/代际隔离、取消、幂等、重启、HTTP形状以及真实 openat 文件操作。

本地测试成功不代表公司目标服务器已经完成实际登录。r59/r62 已部署与 service 两字段界面/连接/查询是否通过员工验收分开记录；后者仍待完成。
