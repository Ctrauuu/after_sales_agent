-- 将已初始化环境中的旧 MCP Tool 名称更新为带 NL2SQL 后缀的新名称。
UPDATE mcp_tool_registry
SET tool_name = 'query_return_stats_nl2sql'
WHERE server_id = 'mcp-aftersale'
  AND tool_name = 'query_return_stats';
