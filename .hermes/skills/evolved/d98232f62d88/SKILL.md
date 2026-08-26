---
name: query_return_stats_nl2sql_warmup
description: 对 query_return_stats_nl2sql 工具执行两次预热查询，按指定分组和时间范围调用，不分析结果，仅简短报告完成状态。
---

# query_return_stats_nl2sql_warmup

对 query_return_stats_nl2sql 工具执行两次预热查询，按指定分组和时间范围调用，不分析结果，仅简短报告完成状态。

此文件由 Skill 自进化引擎维护。模型匹配后按下列工作流执行，执行前必须检查依赖 MCP 工具可用。

```json
{
  "avg_quality_score": 0.0,
  "created_at": "2026-08-25T12:18:09.462105+00:00",
  "description": "对 query_return_stats_nl2sql 工具执行两次预热查询，按指定分组和时间范围调用，不分析结果，仅简短报告完成状态。",
  "enabled": true,
  "name": "query_return_stats_nl2sql_warmup",
  "output_template": "预热完成 ✅ - 第 1 次调用：`query_return_stats_nl2sql(group_by={first_group_by}, date_range_days={first_date_range_days}, category=\"{category_filter}\")` — 成功。 - 第 2 次调用：`query_return_stats_nl2sql(group_by={second_group_by}, date_range_days={second_date_range_days}, category=\"{category_filter}\")` — 成功。 两次调用均正常完成，未做结果分析。",
  "required_mcp_tools": [
    "tool_describe",
    "query_return_stats_nl2sql"
  ],
  "skill_id": "d98232f62d88",
  "trigger_patterns": [
    "预热 query_return_stats_nl2sql",
    "调用 query_return_stats_nl2sql 两次",
    "warm up query_return_stats_nl2sql",
    "[IMPORTANT: You are running as a scheduled cron job. DELIVERY: Your final response will be automatically delivered to the user — do NOT use send_message or try to deliver the output yourself. Just produce your report/output as your final response and the system handles the rest. SILENT: If there is "
  ],
  "updated_at": "2026-08-25T12:18:09.462105+00:00",
  "usage_count": 1,
  "version": 1,
  "workflow": [
    {
      "output_key": "tool_info",
      "params": {
        "name": "query_return_stats_nl2sql"
      },
      "step": 1,
      "tool": "tool_describe"
    },
    {
      "output_key": "result_1",
      "params": {
        "category": "{category_filter}",
        "date_range_days": "{first_date_range_days}",
        "group_by": "{first_group_by}"
      },
      "step": 2,
      "tool": "query_return_stats_nl2sql"
    },
    {
      "output_key": "result_2",
      "params": {
        "category": "{category_filter}",
        "date_range_days": "{second_date_range_days}",
        "group_by": "{second_group_by}"
      },
      "step": 3,
      "tool": "query_return_stats_nl2sql"
    }
  ]
}
```
