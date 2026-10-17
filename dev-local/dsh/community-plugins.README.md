# 固定社区插件候选

上游来源为 `Yinxe/deepseek-harness-plugins`，固定提交 `b9ea3d7625a7ec40cf191094e7b83571ba5c0a79`。`vendor-community` 保存发行代码、每包 MIT 许可证、原始说明和 SHA256 清单；不在员工机器执行 npm 安装或跟踪滚动 latest。上游文档中的安装命令只是原始资料，不由本项目执行。

`stageCommunity(candidateRuntime)` 只接受 DSH `0.1.6-alpha.1`，校验固定四包文件后复制至 `node_modules/@dshp`，生成 `community.patch.json`，并在各包根目录放置受控 `megmeet-guarded-host.mjs`。原始发行文件不一致时停止；上游文件不作修改。调用方将返回的 commit、manifestSha256、包版本、guardSha256 和 patchSha256 纳入兼容性清单。启动适配器校验这些文件后，在账户私有目录生成固定映射到该次安装目录的绝对 wrapper 路径的 patch；不能直接传递裸包名（原生 profile fallback 不包含这些额外包），也不能将开发机绝对路径写进公开发行。

默认候选：

| 插件 | 固定版本 | 默认行为与边界 |
|---|---|---|
| file-change-viewer | 0.2.0 | 展示原生文件差异；`patchTool:false`，不默认新增批量改文件工具。员工若在设置里开启测试版工具，会增加模型写文件能力，仍需原生沙箱与授权。 |
| web-style | 0.2.0 | `themeId:''`、原生圆角；只增加可选外观设置，上传图片取色在本机，不默认替换界面主题。 |
| widget-kit | 0.11.0 | `referenceWidgets:false`，不默认显示诊断/示例组件；为组件提供界面容器，不授予企业项目权限。 |
| token-meter | 0.8.0 | `vendors:[]`，仅本 Host 会话日志派生统计；不是公司结算账单，不代表精确 TokenKey 余额。 |

Token-meter 的余额适配器只有 DeepSeek、OpenCode、Command Code 和手动账本，没有公司 TokenKey。绝不能把个人 TokenKey、IAM token 或本机模型代理令牌填进第三方余额字段。插件允许用户自行填写外部供应商凭据；当前只是安全默认值，并非强制禁止所有后续用户配置。云端 Host 每人独立，统计也仅为该 Host，不能据此汇总同部门或其他员工。

暂不默认安装 skill-manager：它直接管理个人全局及工作区技能文件，写入内容会影响模型提示词，不能替代公司审核的项目插件和后台权限。也不默认安装 MCP/search/vision，以免自动引入外部命令、网络和不同供应商凭据。

验证：`node --test community-plugins.test.mjs` 验证哈希、版本、四包前端注册壳及候选幂等性。`node community-plugins-probe.mjs` 创建独立临时运行目录/DSH_HOME，复用只读原生依赖、复制四包候选，真实启动原生 Host；网络守卫禁止非 loopback TCP。验证登录页面和四包状态，最终只结束本次子进程，不触碰员工资料。探针证据明确区分 Host 加载与尚未进行的实际浏览器绘制和模型调用。

鉴权边界：上游自定义 `/ext` 路由没有原生 Cookie 鉴权，`sameOrigin` 只用于有 Origin 的请求，不能作为员工身份校验；file-change-viewer 的 `/locate` 还能读取路径对应的文件上下文，不能当作纯静态装饰。我们的独立 wrapper 对属性、`ctx.get('webServer')` 和注入子上下文的所有扩展路由统一调用原生 `connection.requestRejection`，检查 Host/Origin 及签名 Cookie；缺少服务、鉴权异常时拒绝，不回退到裸接口。四包实际内层请求已验证匿名 401、跨 Origin 403、有效 Cookie 200，并验证四包客户端模块仍被原生发现和提供。原生子进程继续只绑定 loopback，不直接发布端口；Windows 还有 `runtime-host.cjs` 的 browserCookie/Origin，云端还有 Gateway 员工校验和 worker 私有 routeToken。

`community-plugins-desktop-probe.cjs` 使用本机合成 IAM、真实 `startHost` 与独立资料目录，检查外层匿名及跨来源请求拒绝、本人带 Cookie 请求成功；它强制 GATEWAY 为 `127.0.0.1:28090`，不允许访问公司网络。同一 Windows 登录账户能读取自己的应用文件及原生凭据，本应用不声称对该账户下恶意进程提供独立操作系统安全边界。

上游 `compat.json` 没有固定历史兼容矩阵，前端依赖声明仍引用 primitives `0.1.5-rc.1`。四包能够加载不等于每个界面都已兼容；进入发行前还必须在固定 DSH 前端真实测试，随后与桌面/云运行时一起冻结。
