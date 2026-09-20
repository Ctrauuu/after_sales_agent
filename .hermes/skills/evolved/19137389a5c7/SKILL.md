---
name: 退单量维度聚合与最大项定位
description: 当用户基于近N天退单统计进一步询问“哪个维度项最多/最高”时，使用 NL2SQL 按指定分组维度聚合退单量，识别并输出最大值及占比。维度可为品类、区域、原因等，时间为用户指定的近N天。
---

# 退单量维度聚合与最大项定位

当用户基于近N天退单统计进一步询问“哪个维度项最多/最高”时，使用 NL2SQL 按指定分组维度聚合退单量，识别并输出最大值及占比。维度可为品类、区域、原因等，时间为用户指定的近N天。

此文件由 Skill 自进化引擎维护。模型匹配后按下列工作流执行，执行前必须检查依赖 MCP 工具可用。

```json
{
  "avg_quality_score": 0.0,
  "created_at": "2026-08-28T07:24:40.026783+00:00",
  "description": "当用户基于近N天退单统计进一步询问“哪个维度项最多/最高”时，使用 NL2SQL 按指定分组维度聚合退单量，识别并输出最大值及占比。维度可为品类、区域、原因等，时间为用户指定的近N天。",
  "enabled": true,
  "name": "退单量维度聚合与最大项定位",
  "output_template": "近 {date_range_days} 天退单量（按 {group_by} 聚合）： {result_1} 结论：退单量最多的 {group_by} 是 **{top_item}**（{top_value} 单，占 {top_percent}%）。 注意：实际统计范围可能受 RBAC 权限限制，需说明口径。",
  "required_mcp_tools": [
    "query_return_stats_nl2sql"
  ],
  "skill_id": "19137389a5c7",
  "trigger_patterns": [
    "哪个品类最多",
    "哪个类目退单最多",
    "按品类统计退单量并找出最高",
    "最近{n}天哪个{维度}退得最多"
  ],
  "updated_at": "2026-08-28T07:24:40.026783+00:00",
  "usage_count": 1,
  "version": 1,
  "workflow": [
    {
      "output_key": "result_1",
      "params": {
        "date_range_days": "{date_range_days}",
        "group_by": "{group_by}"
      },
      "step": 1,
      "tool": "query_return_stats_nl2sql"
    }
  ]
}
```
