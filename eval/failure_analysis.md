# 评估失败分析

> 自动生成于 2026-08-08 21:36:30
> 评估结果：96.6% 行动准确率，100.0% 分类准确率

## 总体情况

- 总用例 59 条，失败 3 条
- 分类错误 0 条，行动错误 2 条
- 工具覆盖偏差 2 条（共 25 条 auto 用例评估了工具）

---

### 失败用例 1: TC-011

**用户消息**: 我看到同款手机降价了，我买的ORD-1006能退差价吗，9号买的8999现在8499了

**期望 vs 实际**:
- 期望分类: 订单 | 实际分类: 订单 ✓
- 期望操作: auto | 实际操作: escalate ✗
- 期望工具: ['query_order', 'refund_price_diff'] | 实际工具: ['query_order', 'refund_price_diff'] (True)
- 转人工原因: 工具执行失败——refund_price_diff: 退差价金额 8999 元超出自动处理限额（最高 500 元），需转人工审批。

**失败原因分析**:
- **误转人工**：本应自动处理的用例被错误转人工。可能原因：(1) LLM 未选出正确工具导致 tool_results 为空；(2) 工具调用参数错误（如金额传了 null）使工具返回 success=false；(3) classify_node 遗漏关键字段。

**改进建议**:
- **加强 classify_node 信息提取**: 确保订单号/运单号/金额等关键字段被正确提取，为空时给 handle_node 明确的信号而非直接放弃

### 失败用例 2: TC-039

**用户消息**: 我下单时没注意看，ORD-1003这个手表想退了重新买个别的颜色

**期望 vs 实际**:
- 期望分类: 售后 | 实际分类: 售后 ✓
- 期望操作: auto | 实际操作: auto ✓
- 期望工具: ['query_order', 'process_refund'] | 实际工具: ['query_order', 'process_exchange'] (False)
- 转人工原因: 

**失败原因分析**:
- **工具选择偏差**: 缺少工具: {'process_refund'}; 多余工具: {'process_exchange'}。可能原因：(1) LLM 未严格按 category + extracted_info 选择工具；(2) prompt 中工具描述不够明确；(3) 需要更显式的规则引导。

**改进建议**:
- **细化 handle_node prompt**: 添加更多 few-shot 示例，让 LLM 更准确理解 query_order 与 query_logistics 的对应关系

### 失败用例 3: TC-H2

**用户消息**: 我的换货申请现在什么状态

**期望 vs 实际**:
- 期望分类: 售后 | 实际分类: 售后 ✓
- 期望操作: auto | 实际操作: escalate ✗
- 期望工具: ['query_order'] | 实际工具: [] (False)
- 转人工原因: 无法确定需要调用的工具，信息不足，转人工处理。

**失败原因分析**:
- **误转人工**：本应自动处理的用例被错误转人工。可能原因：(1) LLM 未选出正确工具导致 tool_results 为空；(2) 工具调用参数错误（如金额传了 null）使工具返回 success=false；(3) classify_node 遗漏关键字段。
- **工具选择偏差**: 缺少工具: {'query_order'}。可能原因：(1) LLM 未严格按 category + extracted_info 选择工具；(2) prompt 中工具描述不够明确；(3) 需要更显式的规则引导。

**改进建议**:
- **加强 classify_node 信息提取**: 确保订单号/运单号/金额等关键字段被正确提取，为空时给 handle_node 明确的信号而非直接放弃
- **细化 handle_node prompt**: 添加更多 few-shot 示例，让 LLM 更准确理解 query_order 与 query_logistics 的对应关系


## 总结

- 分类错误 0 条 → 优先优化 classify_node 的 prompt 和 few-shot 示例
- 行动错误 2 条 → 重点排查 decide_node 的边界逻辑和敏感词检测
- 工具偏差 2 条 → 细化 handle_node 中的工具选择指引
