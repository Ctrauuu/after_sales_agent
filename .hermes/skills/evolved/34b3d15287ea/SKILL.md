---
name: aftersale_work_order_status_query
description: 按时间范围、品类和工单类型查询售后工单数量与状态分布，并生成简要分析结论。适用于维修/退单等售后工单情况的常规探查。
---

# aftersale_work_order_status_query

按时间范围、品类和工单类型查询售后工单数量与状态分布，并生成简要分析结论。适用于维修/退单等售后工单情况的常规探查。

此文件由 Skill 自进化引擎维护。模型匹配后按下列工作流执行，执行前必须检查依赖 MCP 工具可用。

```json
{
  "avg_quality_score": 0.0,
  "created_at": "2026-08-28T07:35:14.329352+00:00",
  "description": "按时间范围、品类和工单类型查询售后工单数量与状态分布，并生成简要分析结论。适用于维修/退单等售后工单情况的常规探查。",
  "enabled": true,
  "name": "aftersale_work_order_status_query",
  "output_template": "## {time_range}{category}品类{order_type}工单情况（实时查询） | 工单状态 | 工单数 | |---|---:| {status_rows} | **合计** | **{total}** | **要点**： - {time_range}窗口内，{category}品类{order_type}类工单共 {total} 单（查询口径：售后记录中 return_type='{order_type_code}' 的工单），状态编码为 {status_codes}； - 可结合同期退单等售后类型对比，判断该窗口内售后结构； - 若工单关联原因码，可与原因分析联动（本次查询未返回关联字段时不推测）。 > 数据口径：受当前账号 RBAC 权限收窄影响，结果可能非全国口径；实际覆盖日期需以返回记录为准。",
  "required_mcp_tools": [
    "query_aftersale_nl2sql"
  ],
  "skill_id": "34b3d15287ea",
  "trigger_patterns": [
    "维修工单情况",
    "{category}品类维修工单",
    "近{time_range}{category}维修工单状态",
    "{order_type}工单数量和状态分布"
  ],
  "updated_at": "2026-08-28T07:35:14.329352+00:00",
  "usage_count": 1,
  "version": 1,
  "workflow": [
    {
      "output_key": "order_status_result",
      "params": {
        "question": "近{time_range}内{category}品类的{order_type}工单数量和状态分布情况"
      },
      "step": 1,
      "tool": "query_aftersale_nl2sql"
    }
  ]
}
```
