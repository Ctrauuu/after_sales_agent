"""校验 LLM 生成的聚合 SQL，并注入服务端 RBAC 条件。"""

from __future__ import annotations

import re
from typing import Any, Mapping

from mcp_suning.auth_middleware import build_scope_clause
from nl2sql.schema_registry import SCHEMA_REGISTRY


FORBIDDEN_KEYWORDS = (
    "DROP", "DELETE", "INSERT", "UPDATE", "ALTER", "TRUNCATE", "CREATE",
    "REPLACE", "GRANT", "REVOKE", "CALL", "LOAD_FILE", "OUTFILE",
    "DUMPFILE", "SLEEP", "BENCHMARK", "FOR UPDATE", "LOCK IN SHARE MODE",
)
ALLOWED_TABLES = set(SCHEMA_REGISTRY)
REQUIRED_TABLES = {"t_aftersale_return", "t_order_main", "t_product_sku"}
ALLOWED_COLUMNS = {
    str(table["alias"]): set(table["columns"])
    for table in SCHEMA_REGISTRY.values()
}
BASE_JOINS = (
    (
        "t_order_main",
        "o",
        r"(?:o\.order_id\s*=\s*r\.order_id|r\.order_id\s*=\s*o\.order_id)",
    ),
    (
        "t_product_sku",
        "s",
        r"(?:s\.sku_code\s*=\s*r\.sku_code|r\.sku_code\s*=\s*s\.sku_code)",
    ),
)
OPTIONAL_JOINS = (
    (
        "t_product_category",
        "c",
        r"(?:c\.category_code\s*=\s*s\.category_l3_code|s\.category_l3_code\s*=\s*c\.category_code)",
    ),
    (
        "t_product_brand",
        "b",
        r"(?:b\.brand_id\s*=\s*s\.brand_id|s\.brand_id\s*=\s*b\.brand_id)",
    ),
)


class SQLValidator:
    def __init__(
        self,
        default_limit: int = 500,
        max_limit: int = 1000,
        default_days: int = 30,
    ) -> None:
        """输入：默认行数、最大行数和默认时间范围天数。

        输出：初始化后的 SQL 校验器实例状态。
        功能：保存当前售后聚合查询使用的安全边界参数。
        """
        self.default_limit = default_limit
        self.max_limit = max_limit
        self.default_days = default_days

    @staticmethod
    def _clean(sql: str) -> str:
        """输入：模型原始 SQL 文本 ``sql``。

        输出：移除外层 Markdown 围栏、末尾分号和空白后的 SQL。
        功能：只清理展示格式，不改变查询内部语义。
        """
        sql = sql.strip()
        if sql.startswith("```"):
            sql = re.sub(r"^```(?:sql)?\s*", "", sql, flags=re.IGNORECASE)
            sql = re.sub(r"\s*```$", "", sql)
        return sql.strip().rstrip(";").strip()

    @staticmethod
    def _insert_before_tail(sql: str, content: str) -> str:
        """输入：单层查询 ``sql`` 和待插入条件 ``content``。

        输出：在 GROUP BY、ORDER BY 或 LIMIT 前插入内容的新 SQL。
        功能：为查询安全追加默认时间条件和服务端 RBAC 条件。
        """
        tail = re.search(
            r"\b(GROUP\s+BY|ORDER\s+BY|LIMIT)\b",
            sql,
            re.IGNORECASE,
        )
        position = tail.start() if tail else len(sql)
        return (
            f"{sql[:position].rstrip()} {content.strip()} "
            f"{sql[position:].lstrip()}"
        ).strip()

    @staticmethod
    def _used_tables(sql: str) -> set[str]:
        """输入：待检查的 SQL ``sql``。

        输出：FROM 和 JOIN 中出现的小写表名集合。
        功能：为允许表白名单和必需关联表校验提取表依赖。
        """
        return {
            match.group(1).strip("`").lower()
            for match in re.finditer(
                r"\b(?:FROM|JOIN)\s+(`?[A-Za-z_]\w*`?)",
                sql,
                re.IGNORECASE,
            )
        }

    @staticmethod
    def _has_join(sql: str, table: str, alias: str, condition: str) -> bool:
        """输入：SQL、目标表名、固定别名和允许的关联条件正则。

        输出：SQL 中存在匹配 JOIN 时返回 ``True``。
        功能：检查模型是否按 Schema Registry 的固定关系关联目标表。
        """
        return bool(
            re.search(
                rf"JOIN\s+{table}\s+(?:AS\s+)?{alias}\s+ON\s+{condition}",
                sql,
                re.IGNORECASE,
            )
        )

    def _join_error(self, sql: str) -> str | None:
        """输入：待检查的 SQL ``sql``。

        输出：首个 JOIN 错误说明；全部合法时返回 ``None``。
        功能：强制退单、订单、SKU 基础关联，并验证可选品类和品牌关联。
        """
        if not all(self._has_join(sql, *join) for join in BASE_JOINS):
            return "订单或SKU的JOIN关系不符合Schema Registry"
        for table, alias, condition in OPTIONAL_JOINS:
            if re.search(rf"\bJOIN\s+{table}\b", sql, re.IGNORECASE) and not self._has_join(
                sql, table, alias, condition
            ):
                return f"{table} 的JOIN关系不符合Schema Registry"
        return None

    @staticmethod
    def _unknown_column(sql: str) -> str | None:
        """输入：待检查的 SQL ``sql``。

        输出：首个未登记的 ``别名.字段``；全部登记时返回 ``None``。
        功能：校验固定别名和显式声明的子查询别名，拒绝未知表字段引用。
        """
        columns_by_alias = {
            alias: set(columns)
            for alias, columns in ALLOWED_COLUMNS.items()
        }
        for table_name, alias in re.findall(
            r"\b(?:FROM|JOIN)\s+`?([A-Za-z_]\w*)`?\s+AS\s+([A-Za-z_]\w*)",
            sql,
            re.IGNORECASE,
        ):
            table = SCHEMA_REGISTRY.get(table_name.lower())
            if table is not None:
                columns_by_alias[alias.lower()] = set(table["columns"])

        for alias, column in re.findall(r"\b([A-Za-z_]\w*)\.([A-Za-z_]\w*)\b", sql):
            if column.lower() not in columns_by_alias.get(alias.lower(), set()):
                return f"{alias}.{column}"
        return None

    @staticmethod
    def _aggregate_error(sql: str) -> str | None:
        """输入：基础结构已通过检查的 SQL ``sql``。

        输出：聚合或敏感字段错误说明；合法时返回 ``None``。
        功能：限制 NL2SQL 只返回安全聚合，并阻止人员标识和业务明细泄露。
        """
        match = re.search(r"^SELECT\s+(.*?)\s+FROM\s+", sql, re.IGNORECASE | re.DOTALL)
        if match is None:
            return "无法识别 SELECT 字段列表"
        select_clause = match.group(1)
        if not re.search(r"\b(?:COUNT|SUM|AVG)\s*\(", select_clause, re.IGNORECASE):
            return "NL2SQL 只允许 COUNT、SUM 或 AVG 聚合分析"

        for field in (
            "order_id", "user_id", "customer_id", "employee_id",
            "engineer_id", "operator_id", "phone", "mobile",
        ):
            if re.search(
                rf"\b(?:[A-Za-z_]\w*\.)?{field}\b",
                select_clause,
                re.IGNORECASE,
            ):
                return f"聚合查询禁止返回敏感字段: {field}"

        dimensions = re.sub(
            r"\b(?:COUNT|SUM|AVG|MIN|MAX)\s*\([^()]*\)",
            "",
            select_clause,
            flags=re.IGNORECASE,
        )
        for field in ("return_id", "return_amount", "order_amount", "refund_amount"):
            if re.search(
                rf"\b(?:[A-Za-z_]\w*\.)?{field}\b",
                dimensions,
                re.IGNORECASE,
            ):
                return f"聚合查询禁止直接返回明细字段: {field}"
        if re.search(r"\b[A-Za-z_]\w*\.[A-Za-z_]\w*\b", dimensions) and not re.search(
            r"\bGROUP\s+BY\b", sql, re.IGNORECASE
        ):
            return "包含维度字段的聚合查询必须使用 GROUP BY"
        return None

    def validate_and_fix(self, sql: str) -> tuple[bool, str, str | None]:
        """输入：模型生成的候选 SQL ``sql``。

        输出：``(是否通过, 最终 SQL, 错误说明)`` 三元组。
        功能：执行单语句、只读、表字段、JOIN、聚合、时间和 LIMIT 校验与修正。
        """
        sql = self._clean(sql)
        if not sql:
            return False, sql, "SQL 不能为空"
        if ";" in sql:
            return False, sql, "只允许执行一条 SQL"
        if re.search(r"(--|/\*|#)", sql):
            return False, sql, "SQL 不允许包含注释"

        for keyword in FORBIDDEN_KEYWORDS:
            if re.search(rf"\b{re.escape(keyword)}\b", sql, re.IGNORECASE):
                return False, sql, f"禁止使用 {keyword}"
        if not re.match(r"^SELECT\b", sql, re.IGNORECASE):
            return False, sql, "只允许 SELECT 查询"
        if re.search(r"\bSELECT\s+(?:[A-Za-z_]\w*\.)?\*", sql, re.IGNORECASE):
            return False, sql, "禁止 SELECT *"
        if re.search(r"\bCROSS\s+JOIN\b", sql, re.IGNORECASE):
            return False, sql, "禁止 CROSS JOIN"

        tables = self._used_tables(sql)
        unknown_tables = tables - ALLOWED_TABLES
        if unknown_tables:
            return False, sql, f"包含未登记的数据表: {sorted(unknown_tables)}"
        if not REQUIRED_TABLES.issubset(tables):
            return False, sql, "退单分析必须关联退单、订单和SKU表"
        if error := self._join_error(sql):
            return False, sql, error
        if column := self._unknown_column(sql):
            return False, sql, f"包含未登记的字段: {column}"
        if error := self._aggregate_error(sql):
            return False, sql, error
        if not re.search(r"\bWHERE\b", sql, re.IGNORECASE):
            return False, sql, "缺少WHERE子句，无法安全注入时间和权限条件"

        if not re.search(
            r"\br\.(?:create_time|update_time)\s*(?:>=|>|BETWEEN)",
            sql,
            re.IGNORECASE,
        ):
            sql = self._insert_before_tail(
                sql,
                "AND r.create_time >= (SELECT MAX(create_time) - "
                f"({self.default_days} * 86400) FROM t_aftersale_return)",
            )

        limit = re.search(r"\bLIMIT\s+(\d+)\s*$", sql, re.IGNORECASE)
        if limit:
            if int(limit.group(1)) > self.max_limit:
                sql = sql[:limit.start(1)] + str(self.max_limit) + sql[limit.end(1):]
        elif re.search(r"\bLIMIT\b", sql, re.IGNORECASE):
            return False, sql, "LIMIT只允许使用一个整数"
        else:
            sql += f" LIMIT {self.default_limit}"
        return True, sql, None

    def inject_rbac(
        self,
        sql: str,
        safe_filters: Mapping[str, Any],
    ) -> tuple[str, dict[str, Any]]:
        """输入：已通过首次校验的 SQL 和授权后的 ``safe_filters``。

        输出：注入行级权限后的 SQL 及对应绑定参数。
        功能：在聚合前追加区域、城市、品类和角色最大时间范围条件。
        """
        scope_sql, params = build_scope_clause(
            safe_filters,
            region_column="o.region_code",
            city_column="o.city_code",
            category_column="s.category_l3_code",
        )
        try:
            max_days = int(safe_filters.get("date_range_days", self.default_days))
        except (TypeError, ValueError) as exc:
            raise ValueError("安全查询时间范围必须是整数") from exc
        if max_days < 1:
            raise ValueError("安全查询时间范围必须大于或等于 1")

        params["rbac_date_range_days"] = max_days
        time_scope = (
            " AND r.create_time >= (SELECT MAX(create_time) - "
            "(:rbac_date_range_days * 86400) FROM t_aftersale_return)"
        )
        return self._insert_before_tail(sql, scope_sql + time_scope), params
