---
name: memory-extractor
description: 判断一轮苏宁售后对话是否值得形成长期业务记忆，并输出结构化提取结果。
---

# 长期记忆提取规则

仅在需要手工检查或调试长期记忆提取时使用本 Skill。正常每轮沉淀由
`suning-rbac-bridge` 的 `post_llm_call` Hook 自动调用同一套规则。

## 应记录

- 可跨会话复用的退单、售后原因或趋势性分析结论。
- 有关键事实或数据支撑的主要问题及处置结论。
- 已确认的订单链路异常、定位结果和处理结论。

## 不应记录

- 单次查询产生、很快失效的临时数字。
- 只对当前会话有效的信息或没有形成结论的普通问答。
- 无法确定主题、缺少基本依据或主要来自猜测的内容。

## 主题白名单

- `return_analysis`：退单、售后原因和趋势分析。
- `order_trace`：订单链路、状态异常和处理结论。

`return_case` 归一化为 `return_analysis`，`order_query` 归一化为
`order_trace`。不得生成其他长期记忆主题。

## 输出结构

只输出下列结构，不附加说明：

```json
{
  "should_record": true,
  "topic": "return_analysis",
  "entities": {},
  "filters": {},
  "conclusion": "可复用的业务结论",
  "evidence": {},
  "confidence": 0.9
}
```

`confidence` 范围为 `0` 到 `1`。只有 `should_record=true`、主题在白名单、
`confidence >= 0.7` 且 `conclusion` 非空时，运行时才会持久化。

## 手工调试

向 Agent 明确要求“使用 memory-extractor 检查下面这轮问答”，并同时提供用户问题、
助手回答和当时生效的 topic/filters。检查输出是否满足门禁即可；手工调用不会直接写入
SQLite 或 Milvus。
