# BASELINE_FREEZE_REPORT — Phase C Finalization / Baseline Freeze

本文件是 **V0.2 Agent Execution Bridge 开始前的最终 baseline evidence**。
所有 SHA / 版本号 / 测试结果均来自实际执行，未做任何推断或补写。

---

## 1. Freeze timestamp

```
2026-09-21 11:53:46 +0800
```

最终 regression 执行时刻。

---

## 2. Branch before freeze

```
master
```

---

## 3. Master HEAD SHA

```
eba90112efc639f550cbb594960ff0a5925bb0f8
```

（即下述 Commit 3。）

---

## 4. 三个 baseline commit

| # | SHA | commit message | 文件 |
|---|---|---|---|
| Commit 1 | `47b0171bbc3af3622e3946bdc93b5cf445cab37c` | `chore: harden local workspace exclusions` | `.gitignore` |
| Commit 2 | `63a226f083b992825a72a508b8bd1af4969db7e4` | `docs: add agent development constraints` | `AGENTS.md` |
| Commit 3 | `eba90112efc639f550cbb594960ff0a5925bb0f8` | `docs: record pre-execution baseline` | `PRE_IMPLEMENTATION_REPORT.md`<br>`TEST_BASELINE_REPORT.md` |

执行方式：逐文件 `git add <file>` + 分别 commit。
**未使用** `git add .` / `git add -A` / `git commit -a`。
每次 commit 后执行 `git show --stat --oneline HEAD` 核验，无意外文件进入。

冻结前 master 历史：

```
eba9011 docs: record pre-execution baseline
63a226f docs: add agent development constraints
47b0171 chore: harden local workspace exclusions
9b6eb20 Add concurrency regression test for execute_prepared locking fix
7ac0cd7 Fix race in execute_prepared: hold an advisory lock across the outbound call
```

---

## 5. PostgreSQL version

```
PostgreSQL 18.6 (Debian 18.6-1.pgdg13+2) on x86_64-pc-linux-gnu, compiled by gcc (Debian 14.2.0-19) 14.2.0, 64-bit
```

容器：`dxdigitalbrainos-postgres-1`，状态 `Up (healthy)`。
服务定义来自项目既有 `compose.yaml`，**未修改、未自建第二套环境、未 prune、未删卷**。

---

## 6. Database names

```
dxbrain
dxbrain_test
```

两个库在最终 regression 后均确认存在且可用。角色 `dx_api` / `dx_bootstrap` / `dx_migration` / `dx_worker` 齐全。

---

## 7. Alembic current / head

```
current = 20260826_0012
head    = 20260826_0012
```

| 检查 | 结果 |
|---|---|
| `dxbrain_test` current | `20260826_0012 (head)` |
| `dxbrain`（主库）current | `20260826_0012 (head)` |
| `alembic heads` | `20260826_0012 (head)` —— **single head** |
| status | **IN_SYNC** |

**未执行 `alembic upgrade`** —— current 与 head 已一致。

---

## 8. pytest final result

命令：

```
DX_RUN_CONTAINER_RESTART_TEST=1 pytest -o addopts=""
```

结果：

```
collected = 28
passed    = 28
failed    = 0
skipped   = 0
errors    = 0
duration  = 7.31s
```

> `-o addopts=""` 是必需的：`pyproject.toml` 的 `addopts = "-q"` 会压掉 pytest 末尾的汇总行。

**这是 FINAL PRE-EXECUTION BASELINE。**

Phase B 的 `27 passed + 1 skipped` 仅作为 **historical / intermediate baseline** 记录于
`TEST_BASELINE_REPORT.md` 第 5.1 节，**不是**最终基线。

---

## 9. Critical concurrency regression result

```
tests/test_model_calls.py::test_execute_prepared_is_race_safe_under_concurrent_retries
```

**PASS**

---

## 10. Container restart persistence result

```
tests/test_kernel.py::test_container_restart_persistence
```

**PASS**

（该测试自身执行 `docker compose restart postgres`，为仓库内置 opt-in 门禁测试，
需 `DX_RUN_CONTAINER_RESTART_TEST=1`。重启后 volume `dxdigitalbrainos_postgres_data` 完好，
两个库 alembic 仍为 head。）

---

## 11. Git working tree status

```
$ git status --short
（空）

$ git diff
（空）

$ git diff --cached
（空）
```

**working tree clean**（在 `BASELINE_FREEZE_REPORT.md` 创建之前）。

---

## 12. local-only exclusions

| 路径 | 排除机制 | 是否进入正式 commit |
|---|---|---|
| `.workbuddy/` | `.gitignore:14` | 否 |
| `memory_vault/` | `.gitignore:13` | 否 |
| `clutch_workflow_demo/` | `.git/info/exclude:10` | 否（local-only） |

`git check-ignore -v` 实测三条规则全部命中。`clutch_workflow_demo/` 保持在
`.git/info/exclude`，**未写入项目级 `.gitignore`**。

`GIT_HYGIENE_REPORT.md` 已移至 `.workbuddy/reports/GIT_HYGIENE_REPORT.md`，
未跟踪、未提交、未删除（本地审计记录）。

---

## 13. no Git remote / no push performed

```
$ git remote -v
（空）
```

**无 remote，未创建 remote，未执行 push。**

---

## 14. no tag created

**未创建任何 Git tag。**

---

## 15. no V0.2 runtime implementation started

本轮**未做**（且上一轮亦未做）：

- 未编写 ADR-0010
- 未设计 / 创建 database migration
- 未创建 `agent_executions` 表
- 未修改 SQLAlchemy model
- 未修改 FastAPI runtime / 路由
- 未修改 outbox worker
- 未修改 `tests/conftest.py`
- 未安装 Multica、未接入 Multica、未 dispatch 任何 Agent
- 未安装 Redis / pgvector / Qdrant / LangGraph / CrewAI / AutoGen / n8n
- 未添加任何新依赖
- 未调用 Codex / Claude（仅此前的 `command -v` 存在性探测）

---

## 16. Post-freeze branch creation note

- Feature branch creation was attempted in the WorkBuddy environment after the baseline freeze.
- The requested slash-containing branch ref did not persist correctly and the branch was not established.
- The repository was restored to `master` and verified intact.
- `master` remained intact throughout; no history was rewritten, no force operation was performed.
- Regression after recovery passed **28/28**.
- **The feature branch was NOT created.** It is to be created separately by the Owner in an interactive shell.

---

## 17. Freeze verdict

| 项 | 判定 |
|---|---|
| 三个 baseline commit | **PASS** |
| 最终测试 28/28 | **PASS** |
| PostgreSQL healthy | **PASS** |
| Alembic IN_SYNC | **PASS** |
| 无 unexpected source modification | **PASS** |
| BASELINE_FREEZE_REPORT 已生成 | **PASS** |
| **创建 `feat/agent-execution-bridge-v02`** | **NO**（见第 16 节） |

**BASELINE FREEZE = PASS**
**FEATURE_BRANCH_READY = NO**
