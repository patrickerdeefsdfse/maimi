# 生成双入口候选配置

`render_candidates.py` 从一个站点输入文件生成两个入口节点的 HAProxy、Keepalived、systemd 和 HTTPS 本机健康检查配置。输入示例使用文档保留地址，核验项初始为 false。构建方在自己的站点填入应用域名、两台节点及虚拟地址、网卡、网段、Kubernetes 服务网段和网关 Service IP，再填写证书文件路径与已核实的前置条件。

```powershell
python render_candidates.py --input C:\private\entry.json --check-input
python render_candidates.py --input C:\private\entry.json --output-directory C:\private\rendered --output-label candidate
```

生成器为每个入口优先选本机网关服务，同时保留经对端 NodePort 的备用路径。80 和 443 接收外部请求，私有适配器绑定节点地址。输出目录包含每份候选配置的摘要。运行时证书、独占虚拟地址、VRRP 和 systemd 的实际部署及回读由站点运维负责。
