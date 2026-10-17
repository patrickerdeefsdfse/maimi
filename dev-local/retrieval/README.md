# 本地 Milvus 与 Elasticsearch

本目录为桌面产品的 Linux 开发依赖，不是员工桌面安装包的一部分，也不修改公司服务器。企业正式部署时，检索服务应由公司后端集中托管；桌面通过获授权的企业接口调用。

## 运行与版本

- Milvus **2.6.24**：embedded etcd + local storage + Woodpecker，数据和日志均位于 Linux 命名卷，不需要额外启动 MinIO/etcd 容器。
- Elasticsearch **8.19.21**：沿用现有工程的 8.x 系列；单节点、Basic 许可、身份验证开启。公开测试口令只是本地合成夹具。
- 安装完成后 `images.env` 固定实际镜像 digest，`runtime-manifest.json` 记录运行平台镜像 ID 和源码摘要。以后明确升级才改变版本，不自动追踪 latest。
- 独立 Compose 项目 `megmeet-dev-retrieval`，仅加入 `megmeet-dev-local_local-only` 内部网络；没有发布到 Windows、局域网或公网的端口。开发容器使用 `milvus:19530` 和 `elasticsearch:9200`。

## 资源和启停

当前电脑 16GB，WSL 上限 4GB。Milvus 容器限制1536MiB、ES限制1GiB/堆384MiB，顺序启动，仅用于极小的合成数据开发检查。**Milvus 官方 standalone 最低内存为8GB，本配置低于官方要求，不能作为生产配置或容量证明。** Go内存目标也不包括C++和mmap。后续真实数据/索引构建需独立评估和增加服务器资源。

为避免影响普通桌面开发，这两个服务按需启动，不随每次打开桌面程序自动启动。助手可直接操作，无需用户复制Linux命令：

```powershell
& 'D:\麦格米特\linux\new-system\ops\local-env\dev.ps1' -Action retrieval-start
& 'D:\麦格米特\linux\new-system\ops\local-env\dev.ps1' -Action retrieval-status
& 'D:\麦格米特\linux\new-system\ops\local-env\dev.ps1' -Action retrieval-verify
& 'D:\麦格米特\linux\new-system\ops\local-env\dev.ps1' -Action retrieval-stop
```

先启动一次现有本地核心环境以创建internal网络。完整`dev.ps1 stop`也会停止这两项，数据卷保留。`run.sh`仅允许MegmeetDev与本机Unix Docker socket，不连接公司Docker/SSH。`restart: no`避免后台随Docker重启额外占用内存。

## 合成验收

在MegmeetDev内从本目录运行 `bash run.sh seed`：创建专用测试索引/集合、3条虚构项目记录和4维手写向量，验证ES全文检索以及Milvus向量检索。相同命中向量分属两个项目，用显式过滤检查不返回另一项目记录；不存在的项目返回空。它不验证上层权限，手写向量不是实际embedding。

`bash run.sh recreate`只重建两个检索容器，保留命名卷。随后`bash run.sh verify`只查询现有数据，**不重建索引/集合、不重新插入记录**，用于验证持久化。首次seed遇到已存在集合会失败，不能静默覆盖未知数据；有意清理本脚本固定测试资源可使用`bash run.sh cleanup`。

验证脚本同时检查HTTP状态与Milvus JSON `code==0`，核对ES版本、未鉴权401、查询结果ID、过滤和分片错误。实际结果保存到 `codex-context/local-dev-20260917/retrieval-*.log`；安装成功不代表业务知识库已接通。

2026-09-17 实际验收已完成：两服务 healthy，合成写入/全文/向量/双项目过滤通过；固定 digest 重建后只读原数据通过；原 Gateway 和原生 DSH 回归通过。收尾时两容器 OOM=false 且无宿主机端口绑定。详细记录见仓库 `codex-context/LOCAL-RETRIEVAL-20260917.md`。

## 接下来接企业插件

保持 PostgreSQL 管身份/授权/插件版本/任务记录，ES负责关键词与结构化检索，Milvus负责向量相似度检索。还需要解析切分、embedding、双索引更新/删除、引用与后台授权过滤。现有Gateway的企业执行分支仍为501，当前原生DSH也未连接这两个库。详细选择方式和接口设计见 `codex-context/ENTERPRISE-PLUGIN-SELECTION-DESIGN-20260917.md`。

官方依据：
- https://github.com/milvus-io/milvus/releases/tag/v2.6.24
- https://raw.githubusercontent.com/milvus-io/milvus/v2.6.24/configs/milvus.yaml
- https://milvus.io/docs/v2.6.x/prerequisite-docker.md
- https://www.elastic.co/guide/en/elasticsearch/reference/8.19/docker.html
