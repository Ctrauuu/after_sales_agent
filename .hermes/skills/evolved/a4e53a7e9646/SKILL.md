---
name: query_cron_jobs
description: 查询当前 Agent 配置的 cron/定时任务列表，包括任务名、ID、调度表达式、内容、投递方式、上次/下次运行时间等。当用户询问是否设置了 cron 任务、查看定时任务列表、有哪些定时任务时使用。
---

# query_cron_jobs

查询当前 Agent 配置的 cron/定时任务列表，包括任务名、ID、调度表达式、内容、投递方式、上次/下次运行时间等。当用户询问是否设置了 cron 任务、查看定时任务列表、有哪些定时任务时使用。

此文件由 Skill 自进化引擎维护。模型匹配后按下列工作流执行，执行前必须检查依赖 MCP 工具可用。

```json
{
  "avg_quality_score": 0.0,
  "created_at": "2026-08-23T12:21:45.144759+00:00",
  "description": "查询当前 Agent 配置的 cron/定时任务列表，包括任务名、ID、调度表达式、内容、投递方式、上次/下次运行时间等。当用户询问是否设置了 cron 任务、查看定时任务列表、有哪些定时任务时使用。",
  "enabled": true,
  "name": "query_cron_jobs",
  "output_template": "当前配置了 {cron_count} 个 cron 任务： | 任务名 | 任务ID | 调度 | 内容 | 投递方式 | 上次运行 | 下次运行 | |---|---|---|---|---|---|---| {cron_jobs_table} 说明：{summary_note}",
  "required_mcp_tools": [
    "cronjob"
  ],
  "skill_id": "a4e53a7e9646",
  "trigger_patterns": [
    "你设置了cron任务吗",
    "当前有定时任务吗",
    "查看cron任务列表",
    "列出所有定时任务",
    "有哪些cron任务"
  ],
  "updated_at": "2026-08-23T12:21:45.144759+00:00",
  "usage_count": 1,
  "version": 1,
  "workflow": [
    {
      "output_key": "cron_jobs",
      "params": {
        "action": "list"
      },
      "step": 1,
      "tool": "cronjob"
    }
  ]
}
```
