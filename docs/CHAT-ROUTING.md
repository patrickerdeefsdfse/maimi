# 对话路由

更新：2026-09-17。桌面使用原生 DSH 界面。旧自写 React 聊天页和 `deploy-ui-r4.sh` 已退役；本机普通对话目前由独立 DSH Host 处理，不经过以下 Gateway REST 接口。

## 现有 Gateway 接口

`POST /v1/chat/route` 对请求的 `route_mode`、`project_key`、`plugin_key`、`file_ids` 做路由决策。普通消息默认 `dsh_chat`；明确企业上下文进入 `enterprise_capability`。服务端还会校验模型目录、可信用户身份与项目/插件权限，前端选择不能授予权限。

`POST /v1/chat/run` 保留普通聊天 Adapter 和历史写入入口。企业分支通过授权后目前仍返回 **501：执行器尚未配置**，不能把路由接口接受请求当作企业任务已执行。

平台默认 provider 为 `tokenkey`，模型为 `deepseek-flash-v4.1`。实际 provider/model 必须出现在启用的模型目录中；项目和插件开发者的模型选择也要经过服务端验证。原生本机环境目前使用模拟模型，未连接公司个人 TokenKey。

`GET /v1/chat/history` 和会话详情接口按服务端解析的用户/组织读取 PostgreSQL 历史。原生 DSH 自身历史与该平台历史目前是两条数据路径，统一展示与企业任务审计需要后续集成；不能声称已经自动同步。

## 原生企业插件接入方向

在原生输入栏扩展点提供已授权插件选择，并随请求传插件/项目标识。Gateway 再次复核身份、资源归属和权限，解析固定插件版本及开发者配置的模型、工具和知识范围后执行。具体设计见 [`企业插件选择方案`](../../codex-context/ENTERPRISE-PLUGIN-SELECTION-DESIGN-20260917.md)。选择器和企业执行器尚未实现。

保留的纯 DTO 位于 [`packages/contracts/chat.ts`](../packages/contracts/chat.ts)，目前没有活动前端调用方。旧客户端的 401/403 自动重放已随原型删除；聊天或工具请求在结果不确定时不能盲目重发。

历史生命周期由 PostgreSQL 策略与 retention worker 管理，Redis 不保存唯一历史；平台策略支持全局、组织、项目、会话范围，部署与容量应分别验收。
