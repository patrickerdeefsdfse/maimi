# 会话工具组件

当前协议为 `megmeet-connectors-v1`。`session-tools.mjs` 实现按客户端和原生会话选择工具、30分钟选择期限、一次性确认与停止。它不依赖DSH包；唯一DSH调用适配位于 `adapter/index.mjs`。发行时 `stage.mjs` 固定复制依赖并生成逐文件摘要，桌面启动核对 `toolkit-policy.json`。

钉钉与搜索使用Gateway独立连接器，配置见 [连接器接入](../docs/ENTERPRISE-CONNECTORS.md)。模型只能调用当前会话明确选择的功能；企业写操作先展示内容，用户确认入口不注册为模型工具。公开网页按外部不可信数据处理，不自动附带对话、IAM或员工TokenKey。

`desktop.mjs` 提供桌面本机工具，每次启用授权10分钟。浏览器用单独Edge上下文，支持打开、读取、点击、填写、返回与截图，不读取日常浏览器资料。Windows驱动按明确窗口句柄提供截图、鼠标、Unicode输入、限定按键及滚动；图片可供当前支持视觉的模型读取。启用和实际修改分别确认，界面可停止。纯网页与云Host不获得员工电脑操作能力。

工具参数按固定schema验证，不能用参数覆盖实际动作；授权期间保留不可变参数。停止取消运行、待选与待确认操作，等待目录/授权的迟到响应不能再启动工具。多选中途取消会撤回本次已给出的本机权限。浏览器和电脑实操只用本轮自建测试页面/窗口验证，未读取员工业务画面。

验证：`node --test toolkit/tests/*.test.mjs` 包含选择隔离、确认绑定、停止竞争和专用浏览器实操。`tests/owned-window-smoke.py` 仅创建并操作自身Windows测试窗口；结果见 `output/toolkit-20260920/owned-window/result.json`。真实原生DSH模型调用与可见确认按钮点击证据在 `output/upgrade-20260920/resume-connectors-ui`。公司凭据、正式IAM、实际钉钉发送和Brave外网搜索仍须正式环境配置后联调。
