# 第三方组件

本仓库包含麦米自有适配代码和发布时使用的少量第三方DSH/社区插件覆盖文件。覆盖文件的路径、来源哈希和发布代次保存在SOURCE-PROVENANCE.json；依赖版本与integrity保存在各package-lock及runtime source pins中。

`third_party/runtime-notices` 保存了从受哈希约束的发布运行时归档中取得的相应包许可原文。社区插件的原始LICENSE还随`dev-local/dsh/vendor-community`保存。Office组件的许可与取得方式见`office-engine/THIRD-PARTY.md`。

没有在该运行时归档中找到许可原文的组件，应在重新分发或建立制品镜像前从相同版本的上游包补全其许可说明。当前快照不声称拥有第三方DSH同版本完整原始源码；不以旧版研究源码代替当前依赖。
