# IAM 组织与账号目录

## 依据与范围

接口依据仅为本轮根目录 `1.docx`（52,092 bytes；SHA256 `3a77113615712c6a25de51f2bf99989164b29f0494dfd4a1bbdb3a469d979b5f`）第 3 节。没有使用旧 Word 补充缺失协议。

实现位于 `services/gateway/app/iam_directory.py`，是独立的服务端同步与账号映射模块。它不新增浏览器接口，不在导入时联网，不改变现有 DSH 界面、工作区、Gateway 身份中间件或数据库。客户端不得获得这里的 API secret 或目录 JWT。

当前使用本地 JSON 快照供 OAuth/session 模块调用，不代表公司 IAM 已完成部署或实测。单元测试使用 `httpx.MockTransport`、合成账号和合成密钥，未访问公司网络。

## 文档协议

- §3.1.1：HS256 签名，`iss` 为配置的 apiKey，`iat` 为当前 epoch **秒**，`jti` 为新的 UUID v4。Java 示例的 `withIssuedAt` 对应 JWT 的 `iat` 字段，并非名为 `issuedAt` 的自定义字段。
- `Authorization: Bearer <JWT>`。文档要求有效期一分钟且不能重复使用，因此每个分页请求都即时签发新 JWT；不缓存、不重用、不自动重试原请求，不额外猜测 `aud`、`exp` 等字段。
- §3.2：POST `/esc-idm/api/v1/org/listAll`；§3.3：POST `/esc-idm/api/v1/account/listAll`。具体环境完整 URL 必须配置，没有公司测试/生产域名默认值。
- 请求为 JSON，`size`、`page` 是十进制字符串，页码从 `"1"` 开始。当前仅全量，不传 `startTime`。
- 成功响应要求字符串 `code="0"` 和 `msg="操作成功"`，读取毫秒 `timestamp` 及对象 `data` 中的 `total/size/page/list`。组织字段表把 data 写成 String，但同节 JSON 示例为对象；实现明确采用示例对象，不猜测或自动展开字符串形式。
- 数值型分页字段接受 JSON 整数或十进制字符串。页码、每页条数必须匹配请求，同一资源全部页的 total 必须一致，每页条数完整。超过配置界限、缺页或字段不符均拒绝发布。
- 组织按 `idt_org__org_code` 标识；账号按 `app_account__account_no` 标识，其 `idt_user__work_no` 才是稳定员工工号。所属组织使用示例中的 `orgs[]/idt_org__org_code`。
- 组织的 `idt_org__status` 与账号的 `app_account__status` 只有数字 `1` 或字符串 `"1"` 启用。`True`、`1.0`、空值、未知值及所有其他状态均不启用。

文档允许 `startTime` 增量，但未定义分页事务快照、增量删除合并和水位提交的完整规则。因此本实现不执行增量同步，不宣称上游分页具有事务一致性。上游若在读取过程中发生同总数的修改，客户端不能仅凭这份文档证明跨页一致性；上线前需确认稳定排序/版本语义。已检测到的总数变化、重复组织/账号立即使本轮失败。

## 配置

| 变量 | 要求或默认值 |
| --- | --- |
| `IAM_DIRECTORY_ORG_URL` | 必填，组织接口完整 URL |
| `IAM_DIRECTORY_ACCOUNT_URL` | 必填，账号接口完整 URL |
| `IAM_DIRECTORY_API_KEY` | 必填，服务端 JWT issuer；不接受缺省值 |
| `IAM_DIRECTORY_API_SECRET_FILE` | 必填，只读取普通文件，拒绝 symlink、空文件与超过 64 KiB 的文件；不输出内容 |
| `IAM_DIRECTORY_SNAPSHOT_FILE` | 必填，私有持久化目录下的完整文件路径 |
| `IAM_DIRECTORY_CA_FILE` | 可选，受信企业 CA 文件；默认系统信任库，始终验证 TLS |
| `IAM_DIRECTORY_ALLOW_HTTP` | 默认 `false`。仅本地合成 mock 显式置 `true`；公司部署使用 HTTPS |
| `IAM_DIRECTORY_PAGE_SIZE` | 默认 1000，允许 1–10000 |
| `IAM_DIRECTORY_MAX_PAGES` | 默认 1000，允许 1–10000 |
| `IAM_DIRECTORY_MAX_RECORDS` | 默认每类资源 100000 条；应用资源上限，不是 IAM 协议常量 |
| `IAM_DIRECTORY_TIMEOUT_SECONDS` | 默认每请求 10 秒，必须为正有限值 |
| `IAM_DIRECTORY_SYNC_INTERVAL_SECONDS` | `--watch` 每轮结束后等待秒数，默认 120，允许 5–3600 |
| `IAM_DIRECTORY_SNAPSHOT_MAX_AGE_SECONDS` | 读者/健康检查最大允许快照年龄，默认 300，必须正整数 |

同步间隔和最大快照年龄是本应用运行策略，不是文档规定。部署时应让同步间隔加最坏同步耗时小于最大快照年龄。若长时间无法更新，读者将拒绝过期目录，不能无限续用停用前的账号信息。

HTTP 客户端禁用环境代理继承和重定向跟随，避免把签名 JWT 自动送往代理或另一个重定向目的地。需要代理时须另行设计明确受信配置，不能借助环境隐式启用。

## 运行方式

在 Gateway 镜像的 `/app` 中，配置以上变量后：

```bash
python -m app.iam_directory          # 一次全量同步
python -m app.iam_directory --watch  # 独立单实例 worker
python -m app.iam_directory --check  # 仅读快照健康检查；不联网、不签 JWT
```

同一快照只运行一个 writer。worker 可与 Gateway 共享私有命名卷，建议 worker 可写、Gateway 只读挂载；目录在构建/卷初始化时归实际服务用户所有。模块不修改已存在目录的 owner 或权限。新文件用 `mkstemp` 创建，在 Linux 上为 0600；临时文件与最终文件在同一目录。

首次同步失败不会发布空目录，worker 按固定有界间隔继续尝试。后续失败保留上一份完整目录。日志只含固定标签和异常类型，不包含账号、业务行、JWT、secret 或上游响应正文。停止信号会要求 worker 在当前同步结束后退出。

成功顺序为：完整拉取组织 → 完整拉取账号 → 校验和规范化 → 写临时文件并 fsync → 原子 replace。进程中断前读者只看到旧一代，完成后看到新一代，不会看到半写 JSON。这里没有把两套上游接口变成数据库事务；部署仍需按上述上游限制评估同步一致性。

## OAuth/session 对接

```python
from app.iam_directory import resolve_account

# 只接受服务端验证过的 OAuth profile.attributes.username。
# 不从请求 header/浏览器参数取得工号，不用 top-level id 回退。
actor = await resolve_account(account_no)
```

返回例仅为合成账号：

```json
{
  "user_no": "LOCAL-FIXTURE-A",
  "account_no": "app-a",
  "display_name": "Synthetic User",
  "organization_codes": ["DEPT-A", "DEPT-B"],
  "organization_names": {"DEPT-A": "Synthetic A", "DEPT-B": "Synthetic B"}
}
```

匹配区分大小写且不裁剪账号字符串。`app-a` 与 `LOCAL-FIXTURE-A` 不相等：先按应用账号找到目录行，再取得工号。多部门保留完整数组，不随意选择主部门；停用部门不进入授权组织集合，未知组织引用使完整同步失败。这里不计算部门继承权限或插件授权，也不把组织负责人、性别、用户类型当作角色来源。

重复 `account_no` 无论其工号相同或不同均拒绝本轮快照，避免选择任意一行。多个明确账号映射到同一稳定工号允许存在，同一员工后续应由平台工号规则绑定个人 Host。

异常均继承 `DirectoryError`：

- `AccountUnknown`：没有该精确应用账号。
- `AccountDisabled`：账号存在但不是启用状态。
- `AccountAmbiguous`：存在重复账号映射。
- `DirectoryUnavailable` / `DirectoryProtocolError`：未配置、缺少/过期/异常快照、上游故障或协议校验失败。

调用方应拒绝建立或继续使用不能可靠映射的身份；不能回退到把 username 当工号。`resolve_account` 每次读取并检查快照年龄。另有 `resolve_account_from_snapshot(snapshot, account_no)` 供已经持有可信快照的内部调用/测试使用，**该低层函数不自行检查年龄**，调用方必须先用 `JsonDirectoryStore.load` 进行时效检查。

## 快照与验证

快照 schema version 1：`generation` UUID、`fetched_at` epoch 秒、仅供证据记录的 `source_timestamps`、规范化 `organizations[]` 和 `accounts[]`。它不保存 API secret/JWT、性别或任意原始响应字段。目录可识别个人，应放在服务端私有卷中，不能作为前端静态文件。

`tests/test_iam_directory.py` 覆盖逐页 JWT 签名/iat/jti 唯一性、全量分页、非 1 状态拒绝、多部门、账号与工号不同、精确匹配、歧义、最后一页失败不发布部分快照、原子替换失败保留旧快照、停用更新、过期拒绝、初次失败后周期恢复、只读健康检查以及资源上限。全部为离线合成输入。

本模块不证明真实 IAM 可用、员工本人登录成功、生产跨用户隔离或公司接口容量；这些仍需由主流程的联调与部署验证独立确认。
