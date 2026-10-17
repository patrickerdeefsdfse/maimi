# 两个真实公司账号的原生 DSH 入口

这是长期保留个人数据的两账号试用部署，不是离线探针。现有DSH镜像和已验收界面继续使用；默认模型是TokenKey的deepseek-flash-v4.1。

运行start-pilot.sh时在服务器输入两个真实员工工号，输入不回显，不输入密码或Token。工号绑定现有platform.users内部UUID；不存在才插入工号记录。已有用户、历史、姓名不覆盖。以后重跑复用两账号、卷和限用模型凭据，不重新分配。

独立Compose位于/data/megmeet-ai-platform-new/personal-entry-pilot/pilot.compose.json。入口与两个Host均受Docker重启策略监管；每个Host只在自己的内网，只挂自己的数据和凭据。公司TokenKey仅挂入口。固定两Host常驻，没有部署大规模调度/闲置回收。

后台依次执行：版本/归属/资源检查→启动入口→SSO TLS连通性→两个Host初始化默认工作区→各自真实模型请求→精确替换原生Nginx路由→匿名拒绝与内部模型接口不公开检查→停止两个明确的共享DSH测试容器，保留它们的卷。旧Gateway、SSO、门户、会议/8D等业务不随该脚本停止。Nginx修改先备份；配置检查或公网路径验证失败会恢复原配置。无需重做R4离线探针。

SSO无Cookie上游可能返回500，此项只检查带TLS验证的网络连接，不当作真实认证通过。真实SSO、浏览器自动选区、聊天/历史和两账号隔离必须在部署后验收。

两个账号分别在不同浏览器/电脑登录已有公司门户，再打开：
https://platform-ai.megmeet.com/megmeet-ai-native/

身份状态页：
https://platform-ai.megmeet.com/megmeet-ai-native/__megmeet/identity

状态页仅给authenticated及内部用户/工作区ID，不给工号或凭据。未登录首次访问显示公司门户登录指引；完整自动登录回跳、SSO续期/退出与容量仍待接入/验收，不能把此次两账号分流上线记为这些已完成。

验证每人首次直接聊天、两轮回复、刷新后各自历史保留、看不到另一个账号的会话。共享测试Host的旧历史不会自动移动到新个人卷。不要向助手回传Cookie、Token或真实业务对话；回传固定测试标记和成功/失败即可。

日志：tail -70 "$(cat /tmp/megmeet-personal-pilot-current-log.txt)"
失败只输出阶段和固定错误码，不打印原始日志/密钥。个人卷不自动删除，禁止down -v。原生Host镜像独立固定，后续升级需经过兼容检查再更新其独立镜像引用。
