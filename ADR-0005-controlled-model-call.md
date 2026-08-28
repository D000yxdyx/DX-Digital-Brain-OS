# ADR-0005：受控模型调用与两阶段账本

- 状态：Proposed（已完成一次低风险 DeepSeek 试点，长期启用仍待确认）
- 日期：2026-08-26
- 范围：Provider、Governance、Audit、Cost、Control UI

## 决定

模型调用只能在以下条件全部满足时执行：

1. `DX_LLM_LIVE_ENABLED=true`；
2. provider 已配置且通过适配器合同；
3. proposal 存在未过期 approval；
4. approval scope 明确包含 `model.call`；
5. 调用目标是低风险、无工具、无外部业务副作用的 planner 请求。

调用采用两阶段账本：

```text
prepare（事务内写入 prepared + event/outbox/audit）
  -> 外部 transport 单次尝试
  -> finalize（事务内写入 succeeded/failed + usage/cost + event/outbox/audit）
```

## 约束

- 默认 `DX_LLM_LIVE_ENABLED=false`。
- 初始 transport 不自动重试，避免重复计费和重复请求。
- 不把 API key 写入数据库、日志、响应或控制台。
- 提示词只传递 proposal 的目标、理由、范围和能力，不传完整聊天历史。
- 模型输出不直接执行工具、不改变 Constitution、Permission 或 Memory。
- provider 未返回美元费用时，`cost_usd` 保持为空，不估算或伪造。
- 外部调用与数据库事务分离；若调用成功但 finalize 失败，仍可能产生重复请求，后续需增加供应商 request id 或幂等键。

## 本次验证

已完成一次 DeepSeek `deepseek-chat` 低风险 planner 调用：输入 115、输出 512、总计 627 tokens，耗时约 5.36 秒；调用结果已 finalized 并写入事件/审计链路。该结果只作为合同验证，不代表已批准长期自主调用。

Claude Code 第三方网关仍不属于本 ADR 的 provider；其 `ANTHROPIC_AUTH_TOKEN` 和网关协议需要单独适配与验证。

## 后续决策

在开启更广泛的 live 调用前，需要补充：provider 价格表或账单同步、响应结构化解析、供应商级幂等键、超时分类、人工取消、提示词隐私策略和调用失败后的补偿流程。
