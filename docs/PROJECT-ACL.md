> 2026-09-17：下文旧平台Cookie/query-user接入仅作历史记录。新接入以当前根目录1.docx（SHA256 3a77113615712c6a25de51f2bf99989164b29f0494dfd4a1bbdb3a469d979b5f）为准，见[IAM-DIRECT.md](IAM-DIRECT.md)。项目迁移仍在后续阶段。

# 项目成员权限

企业上下文请求必须同时满足 SSO 用户、组织和项目成员关系。Gateway 从认证服务得到用户和组织，再查询 `platform.project_members`；浏览器提交的 `project_key`、角色或插件字段不会授予访问权。

- `GET /v1/projects` 只返回当前用户在当前组织中可进入的启用项目。
- `POST /v1/chat/route` 和 `POST /v1/chat/run` 在项目、插件或文件上下文出现时先执行服务端授权。
- 项目必须启用且用户必须是成员；插件还必须在 `project_plugins` 中启用并且插件本身启用。
- 文件只能是项目知识库中的文档，或当前用户在同一组织拥有的 artifact。
- 未通过授权返回 HTTP 403；普通 DSH 对话不触发项目权限查询。

当前企业 capability runner 仍未接入，授权通过后 `chat/run` 会继续返回 501，避免把权限通过误认为业务执行已完成。

## 范围与验证

这是 Gateway REST 的项目访问基础，不会自动保护原生 DSH Host 的 `/api/*`。原生 Host 的身份绑定、多用户目录隔离、统一历史和独立升级仍是后续工作。

`1.docx` 提供 SSO Cookie/query-user 接口依据；项目成员与资源规则由现有平台表落实，不把文档中可选的 verify-access 接口冒充为已集成。成员角色尚未拆分到操作级权限；本人同组织 artifact 的规则也不是项目专属文件授权。现有历史接口在组织字段缺失时的过滤范围需另行收紧。

本地测试覆盖授权失败、普通聊天和 HTTP 入口；服务器使用 `ops/probe-project-acl.py` 在事务内插入随机测试组织、成员、项目和资源，验证跨用户/跨组织拒绝，结束总是回滚，不保留测试记录。服务器尚未执行时不能将这些数据库验证标记为通过。

部署脚本仅复制 Gateway 的 main.py、access_control.py 和索引迁移，先验证 r2 文件指纹。它不覆盖 Compose、DSH 启动器、模型设置或 secret 文件。失败后保留备份路径和执行输出，停在当前步骤，不继续重建其他服务。
