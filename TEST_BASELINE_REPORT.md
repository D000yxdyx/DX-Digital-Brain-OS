# TEST_BASELINE_REPORT v3 — FINAL PRE-EXECUTION BASELINE

- 日期：2026-09-21 11:13 → 11:2x（Phase B）→ 11:29–11:4x（Phase C）
- 分支：`master`（HEAD = `9b6eb20`），无 remote
- 项目：DX Digital Brain OS / Agent Execution Bridge v0.2
- **本文件取代同文件 v1（`BLOCKED`，Docker daemon 不可用）与 v2（`PASS`，27 passed + 1 skipped）**
- **v3 = FINAL PRE-EXECUTION BASELINE**，是 ADR-0010 与 Execution Bridge 实现的验收对照基线

> 版本关系说明（不要混淆）：
>
> | 版本 | 阶段 | pytest 结果 | 性质 |
> |---|---|---|---|
> | v1 | Phase B 首次尝试 | 28 collected / 28 skipped | **BLOCKED**（Docker daemon 不可用） |
> | v2 | **Phase B intermediate baseline** | 28 collected / **27 passed / 1 skipped** | 中间验证记录；skip 项是仓库内置 opt-in 门禁 |
> | **v3** | **Phase C final baseline** | 28 collected / **28 passed / 0 failed / 0 skipped / 0 errors** | **FINAL PRE-EXECUTION BASELINE** |
>
> Phase B 的 27+1 **不是**最终基线 —— 它缺的那一条（`test_container_restart_persistence`）已在 Phase C 启用并通过。

---

## Baseline verdict：**PASS**

解释见第 9 节。

---

## 1. Docker: **READY**

```
Client:  Docker 29.7.2        API 1.55        Context desktop-linux
Server:  Docker Desktop 4.88.1 (237512)
 Engine: 29.7.2  API 1.55  containerd v2.3.3  runc 1.4.3  linux/amd64
docker compose version: v5.4.0
```

daemon 实际可连接（npipe 正常）。

**未执行**：`docker system prune` / `volume prune` / 删 container / 删 image / 删 volume / reset Docker / 改 Docker Desktop 设置。

---

## 2. PostgreSQL: **READY**

| 项 | 值 |
|---|---|
| 容器 | `dxdigitalbrainos-postgres-1`（**既有容器**，此前处于停止状态，本次仅 `up -d` 启动，**数据卷未动**） |
| 状态 | `healthy` —— 使用项目 `compose.yaml` 自带的 `pg_isready` healthcheck，未另造检查 |
| 服务定义 | 项目已有 `compose.yaml`，镜像 `postgres:18.6`，端口 `127.0.0.1:5432`，卷 `postgres_data` |
| **版本** | **PostgreSQL 18.6** (Debian 18.6-1.pgdg13+2) |
| 数据库 | `dxbrain`（主库）、`dxbrain_test`（测试库）、`postgres`、`template0`、`template1` |
| 角色 | `dx_api`、`dx_bootstrap`、`dx_migration`、`dx_worker`（4/4 齐全） |
| 可达性 | **yes** |

**未创建第二套 PostgreSQL 环境**，未修改 `compose.yaml`。

---

## 3. Test DB configuration: **COMPLETE**

| 变量 | 状态 |
|---|---|
| `DX_TEST_DATABASE_URL` | **SET**（非空） |
| `DX_TEST_WORKER_DATABASE_URL` | **SET**（非空） |
| `DX_TEST_MIGRATION_DATABASE_URL` | **SET**（非空） |

`grep -c "replace-with-" .env` → **0**，即 `.env` 已是真实配置，不是 `.env.example` 占位副本。

三个变量仅通过 `set -a; . ./.env` 读入当前进程环境。**未打印任何值，未写入任何文件，未提交 Git。**

---

## 4. Alembic

```
current = 20260826_0012
head    = 20260826_0012
status  = IN_SYNC
```

| 检查 | 结果 |
|---|---|
| `alembic heads` | `20260826_0012 (head)` —— **单一 head，无 multiple heads** |
| `alembic history` 条目数 | 12（与 `migrations/versions/` 下 12 个迁移文件一致） |
| `dxbrain_test` current | `20260826_0012 (head)` |
| `dxbrain`（主库）current | `20260826_0012 (head)` |

**未执行 `alembic upgrade head`** —— current 与 head 已一致，无升级需求，因此不触发审批门槛。

测试运行后复核（证明迁移真实执行过，而非连了个空库）：

- `dxbrain_test.alembic_version` = `20260826_0012`
- `dxbrain_test` 含 9 个 schema：`dx_agent` / `dx_archive` / `dx_audit` / `dx_core` / `dx_events` / `dx_governance` / `dx_model` / `dx_state` / `dx_workflow`
- `dxbrain_test` 的 `dx*` 表数 = **24**
- `dxbrain`（主库）的 `dx*` 表数 = **24**（对照值，测试未触及主库）

---

## 5. Pytest — FINAL（Phase C，restart test 已启用）

```
collected = 28
passed    = 28
failed    = 0
skipped   = 0
errors    = 0
duration  = 8.09s        (pytest -o addopts="" 覆盖 pyproject 的 -q 后取得)
```

命令：`DX_RUN_CONTAINER_RESTART_TEST=1 pytest -o addopts=""`

| 文件 | 测试数 |
|---|---|
| `tests/test_kernel.py` | 9 |
| `tests/test_provider_adapters.py` | 5 |
| `tests/test_governance.py` | 3 |
| `tests/test_model_calls.py` | 3 |
| `tests/test_providers.py` | 3 |
| `tests/test_archive.py` | 2 |
| `tests/test_plan_drafts.py` | 2 |
| `tests/test_agents.py` | 1 |
| **合计** | **28**（与 `grep -c "def test_" tests/*.py` = 28 一致） |

> **勘误**：Phase 0 / v1 报告中写的「30 collected」是错的，实际为 **28**。原因是当时目测进度条里的 `s` 字符数错了。`PRE_IMPLEMENTATION_REPORT.md` 已就地修正并标注勘误。

### 5.1 Phase B intermediate baseline（中间记录，非最终）

```
collected = 28
passed    = 27
failed    = 0
skipped   = 1
errors    = 0
duration  = 3.47s
```

唯一 skip：`tests/test_kernel.py::test_container_restart_persistence`
`SKIPPED: set DX_RUN_CONTAINER_RESTART_TEST=1 to restart the Compose database`

这是仓库**内置的显式 opt-in 门禁**（该测试会 `docker compose restart postgres`），不属于 environment / database / migration / permission 失败。该项已在 Phase C 启用并通过，故 v3 的 `skipped = 0`。

### 5.2 数据库集成测试确已执行（证据，非推断）

- `tests/conftest.py` 的 `migrate_from_empty_database`（session 级 autouse）在 `dxbrain_test` 上真实执行了 `alembic downgrade base` → `upgrade head`；
- 测试后 `dxbrain_test` 落在 head 且 24 张表齐全（见第 4 节）——若未真实执行，schema 不会是这个状态；
- 测试套件使用真实角色连接（`dx_api` / `dx_worker` / `dx_migration`），权限边界测试是真实 SQL 权限断言；
- 最慢调用 `test_execute_prepared_is_race_safe_under_concurrent_retries` 耗时 0.51s（真实线程 + 真实事务竞争），跳过的测试不可能有此耗时。

### 5.3 无失败，因此无需分类

`failed = 0`、`errors = 0`，六类失败分类（`ENVIRONMENT_FAILURE` / `DATABASE_SETUP_FAILURE` / `MIGRATION_FAILURE` / `PERMISSION_FAILURE` / `EXISTING_TEST_FAILURE` / `UNKNOWN_FAILURE`）**均不适用**。未修改任何业务代码。

唯一提示（非失败）：`StarletteDeprecationWarning: Using httpx with starlette.testclient is deprecated; install httpx2 instead.` —— 依赖库告警，不影响结果，本轮不动。

---

## 6. Critical concurrency regression: **PASS**

```
tests/test_model_calls.py::test_execute_prepared_is_race_safe_under_concurrent_retries
```

| 维度 | 结果 |
|---|---|
| collected | **是**（`--collect-only` 中 `test_model_calls.py` 计 3 条，含此条） |
| executed | **是**（完整运行中耗时 0.51s，为全场最慢调用） |
| 结果 | **PASSED** |
| 单独运行 | `1 passed, 1 warning in 1.09s` |

这条测试在 Phase A 中是重点保护对象（当时 index 里曾被删除、工作区被加回）。现确认：文件为 360 行、函数在第 242 行、三个 blob 均为 `c3a2849e...`，且**真实通过**。

---

## 7. Container restart persistence: **PASS**（Phase C 新增）

```
DX_RUN_CONTAINER_RESTART_TEST=1
pytest tests/test_kernel.py::test_container_restart_persistence
→ 1 passed in 4.61s
```

该测试自身会执行 `docker compose restart postgres`（仓库已有测试明确要求的行为，仅作用于测试库 `dxbrain_test`，主库不参与）。

### 测试前状态

| 项 | 值 |
|---|---|
| 容器 | `Up 8 minutes (healthy)` |
| alembic（`dxbrain_test` / `dxbrain`） | `20260826_0012` / `20260826_0012` |
| `dxbrain` 主库 | 存在，24 表 |
| `dxbrain_test` | 存在，24 表 |

### 测试后复核

| 项 | 结果 |
|---|---|
| 容器 | `Up 24 seconds (healthy)` |
| alembic（test / main） | `20260826_0012 (head)` / `20260826_0012 (head)` |
| `dxbrain` 主库 | **仍存在**，24 表 |
| `dxbrain_test` | **可用**，24 表 |
| PostgreSQL persistence | **PASS** |
| volume | `dxdigitalbrainos_postgres_data` **完好** |

**未执行**：`docker compose down -v` / `docker volume rm` / `docker system prune` / `docker volume prune` / 删库 / 删 volume / reset Docker Desktop。

---

## 8. 汇总 — FINAL PRE-EXECUTION BASELINE

```text
Docker:                              READY
PostgreSQL:                          READY      (version = 18.6)
Test DB configuration:               COMPLETE
Alembic:                             current  = 20260826_0012
                                     head     = 20260826_0012
                                     single head = yes
                                     status   = IN_SYNC
Pytest (restart test enabled):       collected = 28
                                     passed    = 28
                                     failed    = 0
                                     skipped   = 0
                                     errors    = 0
                                     duration  = 8.09s
Container restart persistence:       PASS
Critical concurrency regression:     PASS
Baseline verdict:                    PASS
```

---

## 9. Verdict 解释：为什么是 PASS 而不是 FAIL / BLOCKED / NEEDS_APPROVAL

| 判定 | 是否适用 | 理由 |
|---|---|---|
| **BLOCKED** | 否 | Docker、PostgreSQL、测试库配置、migration 四项全部就绪；不再是「跑不起来」 |
| **FAIL** | 否 | `failed = 0`、`errors = 0`、`skipped = 0` |
| **NEEDS_APPROVAL** | 否 | 未遇到需要破坏性操作的岔路：migration 已在 head（无需 upgrade）、容器是既有容器（无需重建）、`.env` 已配置完整（无需猜测） |
| **PASS** | **是** | 28 条全部真实 collected 并真实执行，28 条全部通过；restart persistence 与 concurrency regression 均 PASS |

**PASS 的边界声明（不夸大）**：

- 这个 PASS 覆盖的是「当前 `master` 上 Kernel 的既有 28 条集成测试，在真实 PostgreSQL 18.6 上全部通过，容器重启后数据持久化成立」。
- 它是**未来 Execution Bridge 工作的对照基线**：后续新增 `agent_executions` / `execution_events` / Execution Gateway / MulticaAdapter 后，基线不得劣化 —— 仍须 `failed = 0`、`skipped = 0`，且 concurrency regression 与 restart persistence 仍须 PASS。

---

## 10. 两个可复用的工程事实（踩过的坑）

1. **`pyproject.toml` 的 `addopts = "-q"` 会压掉 pytest 末尾的汇总行。** 要取精确四元组必须 `pytest -o addopts=""`，否则只能看到进度条，容易目测数错（v1 的「30」错误即由此而来）。
2. **`tests/conftest.py` 的 TRUNCATE 表清单是硬编码的。** 未来新增带 FK 的表（如 `agent_executions`）必须把表名同步加进该清单，否则所有集成测试会在清理阶段报 FK 错误。**migration + model + conftest cleanup 必须作为同一个 implementation slice 一起验证。**

---

## 11. 停止点确认

按指示，本轮及此前**未做**：

- 未创建 ADR-0010
- 未创建 migration
- 未创建 `agent_executions` 表
- 未修改 SQLAlchemy model（`src/dxbrain/core/models.py`）
- 未修改 worker
- 未修改 FastAPI 路由
- 未修改 `tests/conftest.py`
- 未安装 Multica
- 未调用 Codex、未调用 Claude（仅 `command -v` 存在性探测）
- 未创建 feature branch（本阶段结束时才创建）
- 未 push、未创建 remote
- 未修改 WSL policy、未修改 Docker 配置
- WSL runtime status 仍为 **UNVERIFIED**

下一步（ADR-0010 → Execution Gateway contracts → persistence → outbox → worker → offline/fake adapter → tests）等待 Owner 批准。
