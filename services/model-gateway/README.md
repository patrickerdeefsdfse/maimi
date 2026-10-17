# Model Gateway

维护 TokenKey provider/model 路由和能力标签，使用服务器 secret 引用。默认模型为 `deepseek-flash-v4.1`；企业项目和会话可选择已登记模型。阿里百炼、DeepSeek 的上游 Key 只由 TokenKey 服务持有，新系统不保存这些直连 Key，API key 永不返回浏览器。
