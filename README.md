> **Public repository notice:** This is the public code distribution. The proprietary 77-source registry and internal editorial notes are intentionally excluded. All included sample sources are disabled/pending review.

# AIVOYA Acquisition Fabric V1.1

**可运行代码基线 · Go 1.23 + Python 3.12 + PostgreSQL 16 + 可选 Scrapling 0.5.1**

定位：AIVOYA 数据获取系统的控制平面与受控采集执行平面；不是已上线的 Signal/Intelligence/Decision 全平台。项目来源于 V1.0，V1.1 是**兼容已有 PostgreSQL 数据的增量版本**。真实库及目标站点的端到端验收仍未完成，详见 [公开版验收说明](docs/ACCEPTANCE.md)。

## 架构

```text
Source Registry (public sample: 3 disabled entries)
   │  审批 / 许可范围 / 采集计划 / 域名节奏 / 政策版本
   ▼
Go API & Scheduler  ────── PostgreSQL 16
   │   lease + policy fencing      │  jobs / records / snapshots / audit
   ▼                               ▲
Python Worker ──────────────────────┘
   ├── RSS / Atom / HTML (stdlib)
   ├── GitHub Releases 官方 API (new V1.1)
   └── Scrapling Adapter (explicit opt-in; network guard required)
             ↓
Normalized extraction / quality review / evidence trace
             ↓
[Future: cross-source dedupe → entity linking → signal intelligence]
```

## V1.1 交付

- 来源审批版本变化时阻断过期任务，避免旧授权策略下继续采集。
- 官方 GitHub Releases JSON 接口连接器，限定公开 API 与同仓库 Release 链接；不把技术上能抓到的数据等同于转载许可。
- 429/503 的 Retry-After 解析及主机级冷却，支持 GitHub 速率限制恢复信息。
- RSS 允许**登记来源主机 + 采集端点主机**的精确白名单链接，拒绝其他跨主机候选。
- 新增 Schema.org/JSON-LD 文章标题/发布日期回退提取；正文仍为确定性规则，不使用 LLM 猜测。
- 来源健康监测 API 与控制台（24 小时任务量、复核、失败及成功比例）。
- 幂等数据库迁移（`internal/store/migrations/0002_github_releases.sql`）和两项新的真实数据库集成测试源码。
- 公开示例含 3 条禁用来源，完整 77 条运营来源表仅供私有部署使用，不随公共仓库发布。

## 启动

需要：可以下载容器镜像和 Go 依赖的 Docker Compose 环境。当前执行环境没有 PostgreSQL/Docker，以下命令是给你的本地开发机器执行的，**不是已经成功运行的部署结果**。

```bash
cd aivoya-acquisition-v1.1
python tools/init_env.py
# 调整 .env 中的 AIVOYA_USER_AGENT，填入实际运营联系信息。
docker compose up --build -d
python tools/import_sources.py --file data/source_registry.sample.json
python tools/smoke.py
```

Web 控制台：`http://127.0.0.1:8080/console`；健康检查：`/healthz`，就绪检查：`/readyz`。API 管理接口需要独立 Bearer 管理令牌；所有新来源默认 `pending` + `enabled=false`，绝不自动批准。

**从已有 V1.0 升级时**：先备份原 PostgreSQL。API 启动执行版本迁移；批量重新导入**不会**覆盖已有来源的 endpoint/access_mode/rights，想启用新 GitHub API 连接器必须通过 `/configure` 修改入口并重新审批。详见 [工程决策说明](docs/ENGINEERING_DECISIONS.md)。

## 开发和测试

```bash
# 可在无网络环境运行：
PYTHONPATH=worker python -m unittest discover -s worker/tests -v
GOPROXY=off go test ./internal/core
GOPROXY=off go run ./cmd/validate-registry -input data/source_registry.sample.json

# 需要联网安装正式 Go 依赖：
go mod tidy
go mod download
go test ./...
go vet ./...

# 必须是**专门的、空的** PG16 测试数据库，不要对生产 DB 执行：
TEST_DATABASE_URL='postgres://aivoya:...@127.0.0.1:5432/aivoya_test?sslmode=disable' \
  go test -count=1 ./internal/store -run Integration -v
```

CI 模板已提供 `.github/workflows/verification.yml`，配置 PostgreSQL 16 服务以自动执行完整集成测试。`go.sum` 因本地 Go module proxy DNS 不通而未能生成，必须在联网环境生成并提交，才能符合生产依赖锁要求。

## 浏览器采集与合规边界

默认 Worker 完全不需要 `pip` 安装第三方抓取库。只有在完成外部安全出站策略、来源许可核验、机器人规则检查和目标站点 PoC 后才考虑 `INSTALL_SCRAPLING=true`，动态浏览器另需 `INSTALL_BROWSER=true`。请注意：`AIVOYA_EGRESS_GUARD_VERIFIED=true` 是人工标志，**不能替代网络防火墙、DNS pinning 或安全代理**。

架构 ADR 见 [ENGINEERING_DECISIONS](docs/ENGINEERING_DECISIONS.md)，安全边界见 [SECURITY](docs/SECURITY.md)，验收计划见 [CLOUD_VERIFICATION](docs/CLOUD_VERIFICATION.md)。
