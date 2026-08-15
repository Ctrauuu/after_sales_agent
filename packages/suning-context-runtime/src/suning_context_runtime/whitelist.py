"""从苏宁业务数据库加载槽位可用值并提供短时缓存。
从业务数据库里读取“合法的业务值”，统一成标准格式，并缓存起来，供槽位提取和模型 Prompt 使用。
业务数据库
   ↓
DatabaseWhitelistLoader
读取真实业务值
   ↓
BusinessWhitelist
形成内存中的白名单快照
   ↓
┌────────────────────┐
│                    │
↓                    ↓
normalize()      prompt_payload()
校验/规范化          给 LLM 看候选值
│                    │
↓                    ↓
稳定槽位值          提高模型提取准确率
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from typing import Any

import sqlalchemy as sa


REGION_DISPLAY_NAMES = {
    "HD": "华东",
    "HB": "华北",
    "HN": "华南",
}


@dataclass(frozen=True)
class BusinessWhitelist:
    """数据库中可用于结构化槽位的业务值及其规范化映射。"""

    categories: dict[str, str] = field(default_factory=dict)
    regions: dict[str, str] = field(default_factory=dict)
    brands: dict[str, str] = field(default_factory=dict)
    reasons: dict[str, str] = field(default_factory=dict)
    statuses: dict[str, str] = field(default_factory=dict)
    skus: dict[str, str] = field(default_factory=dict)

    def normalize(self, field_name: str, value: str) -> str | None:
        """输入：槽位字段名 ``field_name`` 和 LLM 提取的字符串 ``value``。

        输出：数据库白名单对应的规范值；字段或值未命中时返回 ``None``。
        功能：将中文名称、数据库编码和大小写变体统一为 MCP 可消费的稳定值。
        """

        lookups = {
            "category": self.categories,
            "region": self.regions,
            "brand": self.brands,
            "reason": self.reasons,
            "status": self.statuses,
            "sku_code": self.skus,
        }
        lookup = lookups.get(field_name)
        if lookup is None:
            return None
        normalized = value.strip()
        if not normalized:
            return None
        return (
            lookup.get(normalized)
            or lookup.get(normalized.upper())
            or lookup.get(normalized.lower())
        )

    def prompt_payload(self, max_values: int = 200) -> dict[str, list[str]]:
        """输入：每类最多注入模型的白名单值数量 ``max_values``。

        输出：按字段分类、排序并去重的“别名 -> 规范值”候选列表。
        功能：让模型看到数据库名称及编码的对应关系，同时限制候选数量避免 Prompt 无限膨胀。
        """

        mappings = {
            "category": self.categories,
            "region": self.regions,
            "brand": self.brands,
            "reason": self.reasons,
            "status": self.statuses,
            "sku_code": self.skus,
        }
        payload: dict[str, list[str]] = {}
        for key, mapping in mappings.items():
            candidates: dict[str, str] = {}
            for alias, canonical in mapping.items():
                if not alias or not canonical:
                    continue
                candidate = (
                    canonical
                    if alias.casefold() == canonical.casefold()
                    else f"{alias} -> {canonical}"
                )
                candidates.setdefault(candidate.casefold(), candidate)
            payload[key] = sorted(candidates.values())[:max_values]
        return payload


class DatabaseWhitelistLoader:
    """从 SQLAlchemy 数据库连接读取并缓存槽位白名单。"""

    def __init__(self, engine: Any, cache_ttl_seconds: float = 300.0) -> None:
        """输入：SQLAlchemy ``engine`` 和白名单缓存秒数。

        输出：初始化后的白名单加载器；此时不访问数据库。
        功能：保存数据库依赖并建立线程安全缓存，供并发 Hermes 会话复用。
        """

        self.engine = engine
        self.cache_ttl_seconds = max(1.0, float(cache_ttl_seconds))
        self._cached: BusinessWhitelist | None = None
        self._expires_at = 0.0
        self._lock = threading.RLock()

    @staticmethod
    def _add_alias(mapping: dict[str, str], alias: Any, canonical: Any) -> None:
        """输入：目标映射、数据库别名和对应规范值。

        输出：无；有效值会原地写入原样、大小写兼容的别名键。
        功能：统一构建支持中文名、编码及大小写变体的白名单查找表。
        """

        alias_text = str(alias or "").strip()
        canonical_text = str(canonical or "").strip()
        if not alias_text or not canonical_text:
            return
        mapping[alias_text] = canonical_text
        mapping[alias_text.upper()] = canonical_text
        mapping[alias_text.lower()] = canonical_text

    def load(self, *, force: bool = False) -> BusinessWhitelist:
        """输入：是否忽略缓存并强制查询数据库的 ``force`` 标记。

        输出：由当前数据库品类、区域、品牌、原因、状态和 SKU 组成的白名单。
        功能：在缓存过期时执行只读查询，并以原子方式替换进程内白名单快照。
        """

        now = time.monotonic()
        with self._lock:
            if not force and self._cached is not None and now < self._expires_at:
                return self._cached

            categories: dict[str, str] = {}
            regions: dict[str, str] = {}
            brands: dict[str, str] = {}
            reasons: dict[str, str] = {}
            statuses: dict[str, str] = {}
            skus: dict[str, str] = {}
            with self.engine.connect() as connection:
                category_rows = connection.execute(
                    sa.text(
                        "SELECT category_code, category_name "
                        "FROM t_product_category"
                    )
                ).mappings()
                for row in category_rows:
                    canonical = str(row["category_name"] or row["category_code"])
                    self._add_alias(categories, row["category_code"], canonical)
                    self._add_alias(categories, row["category_name"], canonical)

                region_rows = connection.execute(
                    sa.text(
                        "SELECT DISTINCT region_code FROM t_order_main "
                        "WHERE region_code IS NOT NULL"
                    )
                ).mappings()
                for row in region_rows:
                    code = str(row["region_code"])
                    self._add_alias(regions, code, code)
                    display_name = REGION_DISPLAY_NAMES.get(code.upper())
                    if display_name:
                        self._add_alias(regions, display_name, code)

                brand_rows = connection.execute(
                    sa.text("SELECT brand_id, brand_name FROM t_product_brand")
                ).mappings()
                for row in brand_rows:
                    canonical = str(row["brand_name"] or row["brand_id"])
                    self._add_alias(brands, row["brand_id"], canonical)
                    self._add_alias(brands, row["brand_name"], canonical)

                reason_rows = connection.execute(
                    sa.text(
                        "SELECT DISTINCT return_reason_code, return_reason_desc "
                        "FROM t_aftersale_return"
                    )
                ).mappings()
                for row in reason_rows:
                    canonical = str(row["return_reason_code"])
                    self._add_alias(reasons, row["return_reason_code"], canonical)
                    self._add_alias(reasons, row["return_reason_desc"], canonical)

                status_queries = (
                    "SELECT DISTINCT order_status AS status FROM t_order_main",
                    "SELECT DISTINCT return_status AS status FROM t_aftersale_return",
                    "SELECT DISTINCT refund_status AS status FROM t_payment_refund",
                )
                for query in status_queries:
                    for row in connection.execute(sa.text(query)).mappings():
                        self._add_alias(statuses, row["status"], row["status"])

                sku_rows = connection.execute(
                    sa.text("SELECT sku_code, product_name FROM t_product_sku")
                ).mappings()
                for row in sku_rows:
                    canonical = str(row["sku_code"])
                    self._add_alias(skus, row["sku_code"], canonical)
                    self._add_alias(skus, row["product_name"], canonical)

            snapshot = BusinessWhitelist(
                categories=categories,
                regions=regions,
                brands=brands,
                reasons=reasons,
                statuses=statuses,
                skus=skus,
            )
            self._cached = snapshot
            self._expires_at = time.monotonic() + self.cache_ttl_seconds
            return snapshot


__all__ = ["BusinessWhitelist", "DatabaseWhitelistLoader", "REGION_DISPLAY_NAMES"]
