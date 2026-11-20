# AIVOYA Cloud Verification Plan (执行准备就绪，非已经验收通过)

本工程已加入 `.github/workflows/verification.yml`。它要求选择专用的 GitHub 仓库上传后，通过 GitHub Actions 的 **Acquisition Fabric - Verified Acceptance** 手工运行。

## 验证矩阵

1. `postgres16-integration`：真实 `postgres:16-alpine` 服务，Go 1.23，真实 `pgx`，`go mod tidy/verify`，`go test ./...`、`go vet`，生成 `go.sum` 和 `go-tests.log` 证据文件。
2. `python-offline`：Worker 单元测试、语法检查、完整私有来源表注册表及 SHA-256 完整性核验。
3. `official-github-api`：**手动 opt-in**，调用 `https://api.github.com/repos/D4Vinci/Scrapling/releases?per_page=3`，运行真实 Worker 的 robots/rights/endpoint 校验、JSON 提取。不采集第三方全文。出站或政策被拒绝时判定失败而非悄悄绕过。
4. `scrapling-static`：**手动 opt-in**，安装 pinned `scrapling[fetchers]==0.5.1`，仅请求 `https://example.com/` 一次并验证 HTML `h1`。
5. `scrapling-browser`：**手动 opt-in**，只对 127.0.0.1 上由 CI 启动的受控 HTML fixture 验证 JavaScript 渲染。不是对第三方站点绕过反爬，也不能代替生产 Worker 的网络隔离验收。

各在线 job 都将结果保存为机器可读 JSON Artifact。通过 `workflow_dispatch` 输入选择需要跑的部分；`push`/`pull_request` 只执行前两项离线与 PG 任务，避免对外部来源无授权或无意的定期访问。

## 使用边界与下一步

- 源码包当前还没有 `go.sum`：在云端执行 `go mod tidy`，下载 artifact 中的 `go.sum`，把它提交到仓库并触发复测。**没有锁文件不属于通过生产发布门禁**。
- 首轮在线状态与端到端状态必须由真实 Workflow run/artifact 判定；脚本创建不等于测试通过。
- 20 来源×7 天 PoC 需要补齐经审核的来源 URL 和运行环境（并逐日收集真实结果）。当前 `poc_targets.example.json` 仍是空数组，不能宣称对照试验完成。
- 严禁将 CI 测试凭证改成生产凭证，严禁在 GitHub Actions 中运行未审批的任意 URL 抓取任务。
- 在本地 Docker Compose 上做生产预演，还需要启动 API+Worker+PG16 并执行 `tools/smoke.py`，再验证重启恢复、任务重复提交、授权撤销后的数据清除和调度行为。

## Github 操作

本聊天会话已经可读取用户授权的 GitHub 仓库，但尚未确定 AIVOYA 应放在哪个仓库。为了不污染其他产品的主仓库，没有进行任何仓库写入。选择新的私有 AIVOYA 仓库或指定现有仓库后，创建工作分支并提交代码，再运行 `Acquisition Fabric - Verified Acceptance`。
