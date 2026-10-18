# DSH 0.2.1-rc.2 网页迁移候选

这是独立本地候选，不是公司服务器更新脚本。来源锁见 `../upstream/github-release-evidence.json`。官方 npm `next` 对应本次候选；不得使用未固定的 `latest` 替代。

`prepare-runtime.py` 从精确 npm 安装树与当前已归档源码生成全新目录，拒绝覆盖现有目录。上游 `node_modules` 不施加旧版 bundle 覆盖；公司界面成为单独的 `@megmeet/dsh-company-ui` 插件，工作区路径限制通过原生 controller 子类承接。service 使用 r63 的真实会话连接区域，不把模型文字当作输入框。

旧 token-meter、widget-kit、file-change-viewer、web-style 适配不进入新默认 profile。迁移只删除它们明确拥有的配置行，保留字节备份和其他用户偏好。新版 DSH 的原生界面、统计、文件变更和插件能力保持完整。企业 IAM、模型授权、业务连接器、文件上传与 Office 服务仍由公司桥接承担。

尚需完成真实运行时启动、会话历史升级、业务桥接与浏览器验证。候选不得因构建成功就标为可部署。跨登录定时任务仍受企业许可限制；保留上游能力不等于可以绕过公司任务授权。
