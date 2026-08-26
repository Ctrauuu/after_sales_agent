-- 注册跨系统订单全链路聚合 MCP；可重复执行，不覆盖已有服务状态。
SET NAMES utf8mb4;

INSERT INTO mcp_server_registry (
    server_id, server_name, host, port, status, last_heartbeat
) VALUES (
    'mcp-order-timeline', '订单全链路聚合服务', '10.0.1.15', 8106, 'online', NOW()
)
ON DUPLICATE KEY UPDATE
    server_name = VALUES(server_name),
    host = VALUES(host),
    port = VALUES(port);

INSERT INTO mcp_tool_registry (
    tool_id, server_id, tool_name, description, param_schema, is_enabled
) VALUES (
    'tool-009',
    'mcp-order-timeline',
    'trace_order_timeline',
    '根据订单 ID 并行聚合订单、售后工单、逆向物流和退款数据，返回标准化时间线、当前卡点、SLA 状态及部分失败来源',
    '{"order_id":"bigint"}',
    1
)
ON DUPLICATE KEY UPDATE
    server_id = VALUES(server_id),
    description = VALUES(description),
    param_schema = VALUES(param_schema),
    is_enabled = VALUES(is_enabled);
