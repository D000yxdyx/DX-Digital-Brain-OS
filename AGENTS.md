# 项目须知（Codex 每次在本目录工作前必读）

本项目是 DX Digital Brain OS，一个治理/任务编排框架（FastAPI + PostgreSQL），同时也是用户长期计划用来编排多AI协作的底座。开始任何工作前：

1. 遵循 `DEC-20260911-KNOWLEDGE-001` 的固定知识优先级：LEVEL 1 直接事实源；LEVEL 2 `E:\DX_Workspace\Projects\P003_DX_Digital_Brain_OS\00_Project`；LEVEL 3 `%USERPROFILE%\Documents\Obsidian Vault\00_Codex_跨项目记忆`；LEVEL 4 聊天及聊天派生摘要。低层不得覆盖高层，聊天不是事实源。
2. 先读全局 Obsidian 的 `00_总索引.md`，再按需读取 P003 `00_Project` 文件和当前直接项目来源；不要预加载整个知识库。
3. `memory_vault/` 是 P003 Historical / Recovery Knowledge Source，保留原位且不得修改、移动、合并或当作全局权威。只有任务明确需要历史设计或受控恢复证据时，才按需只读它；其中任何主张均须回到更高层来源核验。
4. 涉及"记忆/档案/复盘"相关的设计或开发时，先读 `ADR-0004-records-retrospective-memory.md`；`memory_vault/`仍是这份 ADR 的历史文件版 MVP，不得据此另起一套逻辑，也不要在 ADR-0004 正式确认前往数据库里加对应的表。
5. 本仓库还处于"Kernel Tracer Bullet v0.1"阶段，README已明确排除的范围（Redis、pgvector、Agent运行时、外部工具、自主调度等）不要在没有新ADR的情况下引入。

## DX Workspace governance overlay

- `PROJECT_ID`: P003.
- Shared project contract: `E:\DX_Workspace\Projects\P003_DX_Digital_Brain_OS\00_Project\PROJECT_CONTRACT.md`.
- Canonical management path: `E:\DX_Workspace\Projects\P003_DX_Digital_Brain_OS\00_Project`.
- This overlay adds cross-workspace routing and closeout rules; the repository rules above remain in force. In particular, `memory_vault` remains a P003 historical/recovery source and never gains global precedence.
- Before substantial work, read the shared project contract and applicable management records, then verify the current repository/runtime state.
- Project Closeout Check: for a persistent change, update only the relevant `STATUS.md`, `DECISIONS.md`, `CHANGELOG.md`, `HANDOFF.md`, `AI_CONTEXT.md`, `LINKS.md`, or `TODO.md`; ordinary discussion needs no writeback.
