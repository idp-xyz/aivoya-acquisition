# AIVOYA Engineering Decisions — Acquisition Fabric 1.0

## ADR-01：Source Registry 是采集唯一的政策权威

来源导入不等于启用。所有来源 `review_status=pending` + `enabled=false`。审批需要 `reviewer` + `basis` + 使用权等级，写入不可随意覆盖的 policy events。审批版本增量；任务持有政策版本，版本不匹配则不得继续归档。

## ADR-02：Go 编排，Python 负责抓取

控制层 Go + PostgreSQL16，Worker 通过版本化 JSON API + Bearer 密钥领取租约。Worker 无法直接改数据库；不在业务 SQL 里嵌入 Scraper。使用 `FOR UPDATE SKIP LOCKED` 支持多 Worker 竞争；租约 token 防止旧 Worker 重复完成。Worker 进程意外终止时，租约到期后可重领；3 次失败后成为终态。数据库是阶段性任务队列，不再引入额外 MQ。

## ADR-03：内容使用权与采集许可分离

`metadata_only`：存储标题、URL、字段等；受限原始响应最多保留 7 日。`extract_only`：仅用于内部处理与验证，不持久化正文，同样保持 7 日原始快照限期。`full_text_licensed`：在核实合同授权后才能持久化正文，原始响应默认 30 日清除。`prohibited`：拒绝采集或立刻删除并停用相关来源。授权撤销时立即清理来源关联的内容记录与原始响应。

**剩余风险**：当前来源权限仍由管理员人工输入，不能替代法律审核、版权合同、个别网页的版权例外分析或 robots.txt 的动态更新；原始快照以 DB BYTEA 存储，生产应改成带生命周期策略的对象存储，避免数据库膨胀。

## ADR-04：默认最小攻击面

所有原始数据来源必须是可解析的公网 HTTP(S) URL，禁止带凭证、私有 IP、非标准端口，Worker 在出站前检查 DNS 和 robots.txt。逐站设置保守访问频率；禁止默认验证码/付费墙绕过。Scrapling 使用时仍有**DNS 重绑定和请求内部资源**的残余风险：当前示例 Python 层的预解析检查并不足以彻底替代网络层白名单，因此无法批准直接暴露在公网生产网络。需要容器出站代理/防火墙、链接和浏览器资源请求域名限制、DNS 绑定验证及安全回归后才能启用。

## ADR-05：不将抓取技术分数等同于内容真实性

质量门禁目前仅为**确定性的启发式评分**（标题长度、正文字数、反验证码关键字），不代表事实正确或正文完整。该分数不能对外作为“真实性”或“Benchmark 可靠性”指标。Signal / Intelligence / Decision 后续需要来源可信度、重复传播识别、证据交叉验证、事实级 Claim/Evidence 关联等独立模块。

## ADR-06：119 来源数据版本

可读取的 AIVOYA_Source_Registry_v1.xlsx（来源总表）共 77 行，含 45 条栏目监测、10 条数据页监测、10 条 RSS/Atom、9 条人工操作类、3 条官方 API 类；实际 119 条最终表未取得完整原始字节。只随工程带入 77 条，不虚构缺失的 42 条。授权条款、RSS URL 可用性、栏目链接结构等都必须逐源验证。

## ADR-07：不实施自动反爬绕过

Scrapling 的浏览器获取与访问控制绕过是不同概念。程序明确没有开启 Cloudflare challenge solving、代理轮换或绕过站点防护。遇到登录限制、403、验证码、隐私边界和 robots 禁止，则输出 blocked 或人工处理。


## ADR-08（V1.1）：官方结构化连接器不通过浏览器抓取模拟

GitHub Releases 使用官方 REST API，限定同一 owner/repo 的发布元数据，禁止将其视作通用 API 抓取或全文转载授权。候选站点的 3 个迁移 URL 需人工核验后才能启用。

## ADR-09（V1.1）：主机允许范围明确列举

RSS/Atom 数据源允许当前已注册采集端点主机与原始出版站点主机，除此之外不得推断同一级域或父级域可用。重定向始终要求与初始 URL 相同主机。后续如需跨域，应单独登记并审批，不以字符串后缀规则猜测信任关系。

## ADR-10（V1.1）：部署与数据库迁移为两类验收

SQL 迁移已经版本化；V1.1 的离线编译与 Python 本地测试不能代替实际 PostgreSQL 16 数据迁移、pgx 驱动兼容性和 Docker Compose 启动验证。生产变更必须先在影子数据库升级并完成回滚演练。
