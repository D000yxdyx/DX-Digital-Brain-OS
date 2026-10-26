# PRE_IMPLEMENTATION_REPORT — Agent Execution Bridge v0.2

- 日期：2026-09-21
- 安全编辑说明（2026-10-26）：此工作树版本已移除本机账户标识、绝对路径、CLI 安装位置及主机监听端口清单；旧提交未改写，历史版本仍可能可见。
- 阶段：Phase 0 只读检查（未修改任何文件，未执行任何写操作）
- 执行角色：本地 DevOps / 平台工程师
- 结论先行：**当时存在 3 个阻塞项，须由 Owner 决策或处理，才能进入 Phase 1。** 详见第 6 节。

---

> ## 文档状态声明（2026-09-21，Phase C Finalization 追加）
>
> **本文是 Phase 0 的历史只读检查记录，不是当前基线。**
>
> 文中「Docker daemon 未运行」「测试全部 skipped」「工作区脏」等描述，均为 **Historical Phase 0 observation**，属于 2026-09-21 早期时点的真实事实，**予以保留、不做覆盖或删除**。
>
> **已被取代的部分**：第 1.2、1.5、2、6 节的时点状态已经过时 ——
>
> **Superseded by `TEST_BASELINE_REPORT.md` and Phase C final baseline.**
>
> 当前权威基线（Phase C，2026-09-21）：Docker READY / PostgreSQL 18.6 READY / Alembic `20260826_0012` IN_SYNC / pytest **28 collected, 28 passed, 0 failed, 0 skipped, 0 errors** / container restart persistence PASS / critical concurrency regression PASS。
>
> 本文第 1.1、1.3、1.4、3、4、5、7、8 节为架构事实与设计约束，仍然有效，是 ADR-0010 的输入依据。

---

## 1. 当前系统事实（已确认，来自仓库文件与命令输出）

### 1.1 仓库与 Git

| 项 | 实测值 |
|---|---|
| 路径 | local project checkout（machine-specific path omitted） |
| 当前分支 | **`master`**（不是 `main`） |
| 远端 | **无 remote 配置**（`git remote -v` 为空） |
| 提交数 | 5，最新 `9b6eb20 Add concurrency regression test for execute_prepared locking fix` |
| 提交历史 | `f66ce8c` initial → `f89c721` archive research → `bdba050` split app.py → `7ac0cd7` fix race → `9b6eb20` concurrency test |

工作区状态（`git status --porcelain`）：

```text
 M .gitignore                    # 新增一行 memory_vault/
MM tests/test_model_calls.py     # 暂存区删除了并发回归测试；工作区又把它加回来
?? AGENTS.md                     # 未跟踪
?? clutch_workflow_demo/         # 未跟踪（8 个 md）
```

**判定：working tree 不干净。** 按 Phase 1 规则，此处停止、等待指示。

> 补充事实：`tests/test_model_calls.py` 的 `MM` 含义是——索引（staged）比 HEAD 少了 `test_execute_prepared_is_race_safe_under_concurrent_retries`，而工作区文件内容与 HEAD 一致（该文件被加回来了）。即 **工作区文件本身没问题，但索引是脏的**，`git stash` / `git checkout` 之前必须先确认这个测试的取舍。

### 1.2 运行时环境

主机软件版本、服务状态、网络监听和工具安装位置均为机器专属信息，已从此副本移除。本节不描述当前运行环境；通用前置条件见 README.md。

### 1.3 CLI 工具检查（历史记录）

CLI 可用性和安装路径均已省略；本节不代表当前工具状态。

### 1.4 架构现状

**Schema（9 个）**

`dx_core`(tasks, idempotency_requests) · `dx_state`(entity_states, state_transitions) · `dx_events`(domain_events, event_outbox, event_inbox) · `dx_audit`(audit_events) · `dx_governance`(intent_proposals, review_decisions, approval_grants, execution_reports, plan_drafts, plan_draft_revisions, workflow_handoffs) · `dx_archive`(project_dossiers, retrospectives, lessons) · `dx_model`(model_calls) · `dx_workflow`(workflow_runs, workflow_steps, workflow_commands) · `dx_agent`(agents, agent_lifecycle_commands)

**迁移**：12 个，head = `20260826_0012`（Agent contract）。命名规则 `YYYYMMDD_NNNN_slug.py`，revision = `YYYYMMDD_NNNN`。

**角色边界**：`dx_bootstrap`（仅容器初始化）· `dx_migration`（DDL 所有者）· `dx_api`（写入，但对 `dx_audit.audit_events` 无 UPDATE/DELETE/TRUNCATE）· `dx_worker`（outbox/inbox + 只读）。

**服务层**：`TaskService` / `GovernanceService` / `ArchiveService` / `ModelCallService` / `PlanDraftService` / `WorkflowService` / `AgentService`。

**统一的命令写法（必须沿用）**：

```text
session.begin()
  → pg_advisory_xact_lock(hashtextextended(actor:actor:domain:key))
  → 幂等重放检查（request_hash 比对，不一致 → *PayloadMismatch）
  → 业务行写入 + flush
  → DomainEvent(aggregate_type, aggregate_id=task_id, event_type, payload)
  → EventOutbox(event_id, status='pending')
  → AuditEvent(actor, operation, task_id, event_id, detail)
  → commit
```

**Provider 层**：`ModelProviderRegistry` + `DeepSeekAdapter` / `AnthropicAdapter` + `HttpxTransport`。适配器需显式注入 transport，未注入即 fail-closed；密钥不出进程、不进 `repr`、不进 `/v1/control/model-providers`。

**现有开关**：`DX_LLM_LIVE_ENABLED=false`（默认关闭）。这是 `DX_AGENT_EXECUTION_ENABLED` 应当照抄的模式。

**API**：`create_app(settings)` 工厂 + `routes/*.py` 的 `register_*_routes(app, require_actor=..., get_*_service=...)`；根密钥 `hmac.compare_digest`；`Idempotency-Key` 头。

**Control UI**：`apps/control-ui`，React 19 + TS + Vite，源码仅 `main.tsx` / `styles.css` / `vite-env.d.ts`，单文件巨型组件，GET-only。

**ADR 现状**：0001 Accepted、0002 Accepted、0003 Proposed、0004 Proposed、**0005 = 受控模型调用（已占用）**、0006/0007/0008/0009 Proposed。

### 1.5 测试结果（已执行）

> **Historical Phase 0 observation** —— 本节的「全部 skipped」是 Phase 0 时点的真实结果（Docker daemon 未运行，集成测试无法执行）。
> **Superseded by `TEST_BASELINE_REPORT.md` and Phase C final baseline**（28 collected / 28 passed / 0 failed / 0 skipped / 0 errors）。
> 保留原值，不因后续建立基线而改写当时事实。

```
.venv\Scripts\python.exe -m pytest -q
sssssssssssssssssssssssssssss                                        [100%]
```

**28 collected，28 skipped，0 passed，0 failed，exit 0。**

> **勘误（2026-09-21 Phase B）**：本节原文写的是「30 collected，30 skipped」，该数字错误。Phase B 用 `pytest --collect-only` 精确核对，实际为 **28 条**（`grep -c "def test_" tests/*.py` 亦为 28）。原因是我在 Phase 0 时按进度条里的 `s` 字符目测计数，数错了。结论不变（当时全部 skip），但数字以 28 为准。

原因：`tests/conftest.py` 的 `_integration_urls()` 要求 `DX_TEST_DATABASE_URL` / `DX_TEST_WORKER_DATABASE_URL` / `DX_TEST_MIGRATION_DATABASE_URL` 三者齐备，否则 `pytest.skip`。当前 shell 未加载 `.env`，且 PostgreSQL 本身也因 Docker daemon 未运行而不可用。

**这条很重要**：`exit code 0` 不代表测试通过。**当前没有任何一条测试被真正执行过**，"所有现有测试必须通过"这条验收标准目前无法证实。

---

## 2. 当前状态判定

> **Historical Phase 0 observation** —— 「数据库不可达」「测试可验证性不具备」「工作区脏」均为 Phase 0 时点判定。
> **Superseded by `TEST_BASELINE_REPORT.md` and Phase C final baseline**：数据库 READY、28/28 真实通过、工作区经 Phase A/C 处理已干净。
> 保留原判定，不改写当时事实。

| 维度 | 判定 |
|---|---|
| 代码基线 | 稳定，5 次提交，无远端 |
| 工作区 | **脏**（4 处，其中索引脏 1 处） |
| 数据库 | **不可达**（Docker daemon 未运行） |
| 测试可验证性 | **不具备**（全部 skip） |
| Multica | **未安装** |
| 集成风险 | 中：新增 schema + migration 无法在本轮用真实 PostgreSQL 跑 downgrade/upgrade |

---

## 3. 已有边界（本轮必须遵守，来自仓库而非推断）

1. DX Digital Brain OS = System of Record / Governance / Audit / Memory；外部系统只能是被调用的执行后端。
2. `dx_api` 对 `dx_audit.audit_events` 无 UPDATE/DELETE/TRUNCATE —— 审计追加写不可削弱。
3. 迁移所有权归 `dx_migration`；运行时角色不得 DDL。
4. `AgentService.check_permission` 只做两件本地判断（agent 是否 active、capability 是否声明），**它不是审批授权**。
5. `WorkflowService` 只做记账式推进（`ready`/`running`/`completed` + pause/cancel），**不执行外部能力**。
6. Model call 已经是「prepare → transport → finalize」两阶段账本 + 四个前置条件（flag、provider、未过期 approval、scope 含 `model.call`）。
7. Control UI 只读；不得新增 RUN AGENT / AUTO EXECUTE 类控件。
8. README 明确排除：Redis、pgvector、Qdrant、Ollama、LangGraph、Agent runtime、外部工具、自主调度。
9. ADR-0004 明确：`memory_vault/` 是 P003 历史/恢复知识源，**不得修改、移动、合并，不得当作全局权威**；ADR-0004 正式确认前不得新增对应表。
10. `SUCCEEDED != TASK DONE` —— 只有既有的 Governance / Human Review 能决定任务完成。

---

## 4. 风险

| # | 风险 | 等级 | 说明 |
|---|---|---|---|
| R1 | 工作区脏 | 高 | 直接违反 Phase 1 门禁；且 `test_model_calls.py` 索引与工作区不一致，盲目 `git stash`/`checkout` 可能丢掉并发回归测试 |
| R2 | Docker daemon 未运行 | 高 | 集成测试无法真实执行；新增 migration 无法在真实 PG 上验证 downgrade/upgrade |
| R3 | WSL 被沙箱拦截 | 中 | 无法核实 WSL2 后端状态。`wsl.exe` 在 Program Blacklist，**我不会重试或绕行** |
| R4 | ADR-0005 编号已被占用 | 中 | 任务书写"新增 `ADR-0005-agent-execution-gateway.md`"，但 0005 = 受控模型调用。硬改会破坏既有 ADR 引用链（ARCHITECTURE.md 已引用 ADR-0005） |
| R5 | `codex` / `claude` CLI 已安装 | 中 | 存在误调用消耗额度的可能。本轮全程不调用，且 MulticaAdapter 不得包装它们 |
| R6 | `.env` 含真实密码 | 中 | 已被 `.gitignore` 忽略，但绝不能写入任何被提交的文件或报告 |
| R7 | `tests/conftest.py` 的 TRUNCATE 列表是硬编码 | 中 | 新增 `agent_executions` 若带 FK 到 `dx_core.tasks`，不加入该列表会导致所有集成测试在清理阶段报 FK 错误 |
| R8 | Multica 安装需联网执行远程脚本 | 中 | `irm ... \| iex` 属于外部执行；且 `multica setup` 可能触发浏览器登录 / OAuth / 验证码 |
| R9 | 报告中含绝对路径与端口 | 低 | 本文件会进 Git，路径本身不含密钥；但仍需确认无敏感值 |

---

## 5. 本轮建议修改 / 新增的文件

| 阶段 | 文件 | 动作 |
|---|---|---|
| P0 | `PRE_IMPLEMENTATION_REPORT.md` | 新增（本文件） |
| P1 | （无文件，仅分支操作） | 创建 `feat/agent-execution-bridge-v02` |
| P2 | `ADR-000N-agent-execution-gateway.md` | 新增，编号待 Owner 确认（建议 `ADR-0010`） |
| P2 | `AGENT_EXECUTION_DESIGN.md` | 新增 |
| P2 | `MULTICA_INTEGRATION.md` | 新增（含 Phase 7 self-host 调研） |
| P3 | `src/dxbrain/core/executions.py`（或 `execution/`） | 新增：ExecutionRequest / ExecutionResult / Provider Protocol |
| P4 | `migrations/versions/20260921_0013_agent_executions.py` | 新增：`dx_agent.agent_executions` + `execution_events`，含 GRANT |
| P4 | `src/dxbrain/core/models.py` | 追加 2 个映射类（不改动既有类） |
| P4 | `tests/conftest.py` | **追加** TRUNCATE 列表项（不改既有项） |
| P5 | `src/dxbrain/config.py` | 追加 `agent_execution_enabled` + Multica 占位字段（默认关闭） |
| P5 | `.env.example` | 追加注释化占位键（**不含真实值**） |
| P8 | `src/dxbrain/providers/execution/multica.py` 或 `src/dxbrain/execution/adapters/multica.py` | 新增 MulticaAdapter（仅 health/readiness） |
| P8 | `src/dxbrain/execution/registry.py` 或复用 `providers/registry.py` | 新增 provider-neutral registry |
| P9 | `tests/test_agent_executions.py` | 新增 |
| P10 | `apps/control-ui/src/main.tsx` | 追加只读展示区（不改既有面板） |
| P11 | `IMPLEMENTATION_REPORT.md`、`README.md` | 新增 / 更新 |

**关于 `Execution Gateway` 的落位建议（待确认）**：现有 `dxbrain/providers/` 已专用于 **model provider**（DeepSeek / Anthropic）。把 execution provider 塞进去会混淆两条完全不同语义的通道。倾向新建 `src/dxbrain/execution/`（gateway + adapters + registry），与 `providers/` 平行。这需要 Owner 决策，因为它决定了后续所有 import 路径。

---

## 6. 阻塞项 — 需要 Owner 决策（三个）

### 阻塞 1：工作区不干净（Phase 1 门禁）

需要的动作，请三选一：

- **A（推荐）**：`git add -A && git commit -m "chore: capture AGENTS.md, .gitignore and concurrency test before bridge work"` —— 把现有 4 处改动作为独立提交固化在 `master` 上，再切分支。不丢任何东西。
- **B**：`git reset`（仅把索引退回 HEAD，工作区不动）—— 消除 `MM` 的暂存差异，然后把 `AGENTS.md` 与 `clutch_workflow_demo/` 留作未跟踪继续存在（仍不"干净"）。
- **C**：由你手动处理后再通知我继续。

我不擅自执行 A 或 B —— 提交会影响 `master` 历史，属于你列的"需要提高确认门槛的操作"。

### 阻塞 2：Docker Desktop 未运行 → 测试无法真实执行

需要你：**手动启动 Docker Desktop**（我不启动 GUI 进程，也不改 WSL / 网络 / 防火墙全局设置）。启动后我会：

1. `docker compose ps` 确认 `postgres` 容器 healthy；
2. 从本地 `.env` 读入 `DX_TEST_*_DATABASE_URL` 等变量（只读进进程环境，不打印、不写文件）；
3. `pytest` 跑真实集成测试，得到真实的 passed/failed 数字作为 Phase 4/9 的基线。

若你不启动，我将只能交付"逻辑正确但未在真实 PG 上验证"的迁移，并在 IMPLEMENTATION_REPORT 中明确标注为**未验证项**，不谎报通过。

### 阻塞 3：ADR 编号冲突

- 现状：`ADR-0005-controlled-model-call.md` 已存在，且 `ARCHITECTURE.md` 第 69 行已引用它。
- 任务书要求：`ADR-0005-agent-execution-gateway.md`。
- 建议：改用 **`ADR-0010-agent-execution-gateway.md`**（0009 是最后一个）。文件名与编号同步，README / ARCHITECTURE 引用链一致。
- 或者：你确认要占用 0005，我则重命名既有文件并同步所有引用 —— 但这会改动既有 ADR 文件名，风险更高，不推荐。

---

## 7. 明确不修改的内容

| 对象 | 理由 |
|---|---|
| `main` / `master` 历史 | 禁止 force push、禁止改写；新工作只在特性分支 |
| `memory_vault/` | ADR-0004 / AGENTS.md：历史恢复知识源，原位保留，不修改/移动/合并 |
| `dx_audit.audit_events` 权限模型 | 不得给 `dx_api` UPDATE/DELETE/TRUNCATE |
| 既有 ADR-0001 ~ ADR-0009 内容 | 只新增，不改写 |
| 既有 governance / workflow / model-call 定义与状态机 | 本轮只在其之上叠加，不重写 |
| `dx_migration` 的 DDL 独占权 | 新迁移仍归 migration 角色执行 |
| `codex` / `claude` 的登录状态与配置 | 本轮不碰 |
| `.env` | 不写入 Git；本轮不修改真实值 |
| PostgreSQL 数据卷 / 数据库 | 不 reset、不 prune、不删卷、不跑破坏性迁移 |
| Control UI 的写入能力 | 不新增 RUN AGENT / AUTO EXECUTE / 直连 DB |
| Redis / pgvector / Qdrant / LangGraph / CrewAI / AutoGen / n8n | 明确排除，不安装 |
| `DX_LLM_LIVE_ENABLED` | 保持 `false`，不打开 |

---

## 8. 本轮未做的事（声明）

- 未创建分支、未提交、未 `git stash`、未 `git reset`。
- 未启动 Docker Desktop，未执行任何 `docker` 写操作。
- 未执行 `wsl.exe`（被沙箱拦截后不再重试）。
- 未安装 Multica、未访问其安装脚本。
- 未调用 `codex`、`claude`、`ollama`，未产生任何模型额度消耗。
- 未修改任何源码、迁移、测试或配置。

---

## 9. 下一步

等待 Owner 对第 6 节的三个阻塞项给出决策后，进入 Phase 1。
