# Contracts

集中维护版本化 OpenAPI、事件、错误码和 DTO，避免前后端各自猜接口。

`chat.ts` 保留已退役 React 原型中的纯聊天请求、路由上下文和历史摘要类型；字段对照 `services/gateway/app/main.py` 与 `history_store.py`，数据库可空字段明确允许 `null`。目前没有活动前端调用方，也不代表原生 DSH 已接入这些 Gateway API。

这里不包含网络请求、Cookie、SSO 刷新、自动重放或跳转逻辑。运行时请求校验、模型目录和项目/插件授权仍由服务端执行；客户端类型不能替代这些检查。
