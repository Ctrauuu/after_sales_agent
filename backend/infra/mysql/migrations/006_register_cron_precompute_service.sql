-- 注册 Hermes Cron 预热的固定内部主体。该主体复用已有全量售后运营角色，
-- 但只有携带 platform=cron 且 subject 与 SUNING_CRON_SERVICE_SUBJECT 相同的
-- HMAC 凭证才能在应用层通过验证；本迁移不为普通 IM 用户扩大权限。
INSERT INTO user_identity (
    hermes_user_id, employee_id, display_name, role,
    permissions, is_active, created_at
) VALUES (
    'U-SVC-CRON', 'SVC-CRON', 'Cron 预计算服务', 'aftersale_ops',
    JSON_OBJECT('data_scope', 'full'), 1, NOW()
)
ON DUPLICATE KEY UPDATE
    display_name = VALUES(display_name),
    role = VALUES(role),
    permissions = VALUES(permissions),
    is_active = 1;

-- 使用不存在判断而不是覆盖既有绑定：若相同服务主体被错误绑定，部署应显式排查。
INSERT INTO user_platform_binding (
    hermes_user_id, platform, platform_user_id, platform_token, bound_at
)
SELECT 'U-SVC-CRON', 'cron', 'precompute-daily', '', NOW()
WHERE NOT EXISTS (
    SELECT 1
    FROM user_platform_binding
    WHERE platform = 'cron'
      AND platform_user_id = 'precompute-daily'
);
