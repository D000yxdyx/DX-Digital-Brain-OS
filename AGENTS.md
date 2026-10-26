# 项目须知（Codex 每次在本目录工作前必读）

本项目是 DX Digital Brain OS，一个治理/任务编排框架（FastAPI + PostgreSQL），同时也是用户长期计划用来编排多AI协作的底座。开始任何工作前：

1. 遵循 `DEC-20260911-KNOWLEDGE-001` 的固定知识优先级：LEVEL 1 直接事实源；LEVEL 2 外部 P003 项目管理目录（the external P003 project-management directory configured for the local environment，其下为 `00_Project`）；LEVEL 3 V2 共享 canonical router（`00_System/AI_ENTRY.md`，解析到 `02_Projects/Pxxx/PROJECT.md`、`STATE.md`、`NEXT.md`），`00_Codex_跨项目记忆` 作为 cold/historical evidence，不得覆盖 V2 canonical routing 或直接来源；LEVEL 4 聊天及聊天派生摘要。低层不得覆盖高层，聊天不是事实源。
2. 首先在 Owner 本地配置的Owner 配置的共享知识库入口 中读取 `00_System/AI_ENTRY.md`，通过 `PROJECT_REGISTRY` 解析本项目 canonical project：`P003 — DX Digital Brain OS`；默认读取 `PROJECT.md`、`STATE.md`、`NEXT.md`；仅在当前任务需要历史证据且 V2 context 不足时，才查询 `00_Codex_跨项目记忆`。不要将本机 Vault 绝对路径写入仓库；若入口不可访问，说明读取受阻，不要猜测替代。
3. `memory_vault/` 是 P003 Historical / Recovery Knowledge Source，保留原位且不得修改、移动、合并或当作全局权威。只有任务明确需要历史设计或受控恢复证据时，才按需只读它；其中任何主张均须回到更高层来源核验。
4. 涉及"记忆/档案/复盘"相关的设计或开发时，先读 `ADR-0004-records-retrospective-memory.md`；`memory_vault/`仍是这份 ADR 的历史文件版 MVP，不得据此另起一套逻辑，也不要在 ADR-0004 正式确认前往数据库里加对应的表。
5. 本仓库还处于"Kernel Tracer Bullet v0.1"阶段，README已明确排除的范围（Redis、pgvector、Agent运行时、外部工具、自主调度等）不要在没有新ADR的情况下引入。

## WorkBuddy memory authority

For WorkBuddy sessions in this repository:

1. Project identity, canonical management state, routing, and Owner decisions must come from the V2 shared canonical context at `00_System/AI_ENTRY.md` in the Owner-configured local Vault. Keep the local Vault path out of this repository.
2. Resolve this repository as: `P003 — DX Digital Brain OS`.
3. Read `PROJECT.md` → `STATE.md` → `NEXT.md` via `PROJECT_REGISTRY` before relying on remembered project facts.
4. Injected WorkBuddy user memory / `MEMORY.md` is advisory retrieval context only; it must not override V2 canonical project identity, management state, routing, or Owner decisions.
5. `.workbuddy/memory/*.md` is non-canonical working-log/cache material; it must not be treated as authoritative project state.
6. Legacy `00_Codex_跨项目记忆` is cold/historical evidence only, consulted only when V2 context is insufficient.
7. Current repository/runtime/deployment facts must be verified directly from the repository/runtime when the task requires current-state claims.
8. Do not scan the entire Vault by default.

Write boundary:
- V2 canonical = read-only by default.
- Legacy = read-only.
- WorkBuddy user memory = non-canonical (advisory).
- `.workbuddy/memory` = non-canonical working log.
- Completing a task is **not** authorization to update V2 `STATE.md`.
- Any future canonical update must go candidate/proposal first, explicit promotion later (no candidate is created in this change; do not write `08_Inbox/wb` here).

## DX Workspace governance overlay

- `PROJECT_ID`: P003.
- Shared project contract: `PROJECT_CONTRACT.md` inside the external P003 project-management directory configured for the local environment.
- Canonical management path: the external P003 project-management directory configured for the local environment (its `00_Project` folder).
- This overlay adds cross-workspace routing and closeout rules; the repository rules above remain in force. In particular, `memory_vault` remains a P003 historical/recovery source and never gains global precedence.
- Before substantial work, read the shared project contract and applicable management records, then verify the current repository/runtime state.
- Project Closeout Check: for a persistent change, update only the relevant `STATUS.md`, `DECISIONS.md`, `CHANGELOG.md`, `HANDOFF.md`, `AI_CONTEXT.md`, `LINKS.md`, or `TODO.md`; ordinary discussion needs no writeback.
