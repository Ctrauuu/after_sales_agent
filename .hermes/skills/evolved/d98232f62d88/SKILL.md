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
  "output_template": "预热完成，两次 query_return_stats_nl2sql 调用均成功（success=true）：1. group_by={group_by_1}, date_range_days={date_range_days_1} — 返回 {row_count_1} 行，数据范围 {data_scope_1}；2. group_by={group_by_2}, date_range_days={date_range_days_2} — 返回 {row_count_2} 行，数据范围 {data_scope_2}。按指令仅报告完成状态，未做结果分析。",
  "required_mcp_tools": [
    "tool_describe",
    "query_return_stats_nl2sql"
  ],
  "skill_id": "d98232f62d88",
  "trigger_patterns": [
    "预热 query_return_stats_nl2sql",
    "调用 query_return_stats_nl2sql 两次",
    "warm up query_return_stats_nl2sql",
    "[IMPORTANT: You are running as a scheduled cron job. DELIVERY: Your final response will be automatically delivered to the user — do NOT use send_message or try to deliver the output yourself. Just produce your report/output as your final response and the system handles the rest. SILENT: If there is",
    "scheduled cron job to prewarm query_return_stats_nl2sql",
    "warm up return stats nl2sql queries",
    "调用 query_return_stats_nl2sql 预热",
    "执行 query_return_stats_nl2sql 标准预热",
    "[IMPORTANT: You are running as a scheduled cron job. DELIVERY: Your final response will be automatically delivered to the user — do NOT use send_message or try to deliver the output yourself. Just produce your report/output as your final response and the system handles the rest. SILENT: If there is "
  ],
  "updated_at": "2026-09-01T04:36:51.528826+00:00",
  "usage_count": 2,
  "version": 2,
  "workflow": [
    {
      "output_key": "describe_result",
      "params": {
        "name": "query_return_stats_nl2sql"
      },
      "step": 1,
      "tool": "tool_describe"
    },
    {
      "output_key": "warmup_result_1",
      "params": {
        "category": "{category_filter}",
        "date_range_days": "{date_range_days_1}",
        "group_by": "{group_by_1}"
      },
      "step": 2,
      "tool": "query_return_stats_nl2sql"
    },
    {
      "output_key": "warmup_result_2",
      "params": {
        "category": "{category_filter}",
        "date_range_days": "{date_range_days_2}",
        "group_by": "{group_by_2}"
      },
      "step": 3,
      "tool": "query_return_stats_nl2sql"
    }
  ]
}
```
