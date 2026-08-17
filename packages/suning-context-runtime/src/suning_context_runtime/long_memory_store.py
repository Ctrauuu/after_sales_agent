"""提供长期记忆的 SQLite、Embedding 与 Milvus 存储实现。"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections.abc import Callable, Sequence
from datetime import datetime, timezone
from http import HTTPStatus
from pathlib import Path
from typing import Any

from dashscope import TextEmbedding
from pymilvus import DataType, MilvusClient

from .long_memory_models import MemoryRecord, canonical_json


_QUERY_WORDS = re.compile(r"[A-Za-z0-9_-]{3,}|[\u3400-\u9fff]+")
_EMBEDDING_BATCH_SIZE = 10


class SQLiteMemoryStore:
    """保存当前版本的长期记忆，并通过 FTS5 提供关键词检索。"""

    def __init__(self, db_path: str | Path) -> None:
        """输入：支持 ``~`` 的 SQLite 文件路径 ``db_path``。

        输出：已创建数据表和全文索引的存储对象。
        功能：保存数据库位置并初始化长期记忆的本地权威存储。
        """

        self.db_path = Path(db_path).expanduser()
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        """输入：无；使用实例保存的数据库文件路径。

        输出：配置为返回 ``sqlite3.Row`` 的独立 SQLite 连接。
        功能：为每次读写创建短生命周期连接，避免 Hook 线程共享连接。
        """

        connection = sqlite3.connect(self.db_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def initialize(self) -> None:
        """输入：无；使用实例保存的数据库文件路径。

        输出：无；创建基础表、FTS5 表和同步触发器。
        功能：让 SQLite 记录始终作为长期记忆的权威数据，并自动维护全文索引。
        """

        schema = """
        CREATE TABLE IF NOT EXISTS memories (
            id TEXT PRIMARY KEY,
            memory_key TEXT NOT NULL UNIQUE,
            user_id TEXT NOT NULL,
            topic TEXT NOT NULL,
            entities_json TEXT NOT NULL,
            filters_json TEXT NOT NULL,
            conclusion TEXT NOT NULL,
            evidence_json TEXT NOT NULL,
            confidence REAL NOT NULL,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL,
            version INTEGER NOT NULL DEFAULT 1
        );
        CREATE INDEX IF NOT EXISTS idx_memories_user_updated
            ON memories(user_id, updated_at DESC);
        CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts USING fts5(
            id UNINDEXED,
            topic,
            entities_text,
            conclusion,
            tokenize='trigram'
        );
        CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
            INSERT INTO memories_fts(id, topic, entities_text, conclusion)
            VALUES (new.id, new.topic, new.entities_json, new.conclusion);
        END;
        CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
            DELETE FROM memories_fts WHERE id = old.id;
            INSERT INTO memories_fts(id, topic, entities_text, conclusion)
            VALUES (new.id, new.topic, new.entities_json, new.conclusion);
        END;
        """
        with self._connect() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(schema)
            connection.execute(
                """
                INSERT INTO memories_fts(id, topic, entities_text, conclusion)
                SELECT id, topic, entities_json, conclusion FROM memories
                WHERE id NOT IN (SELECT id FROM memories_fts)
                """
            )

    @staticmethod
    def _datetime_text(value: datetime) -> str:
        """输入：可能不带时区的记忆时间 ``value``。

        输出：UTC 时区的 ISO 8601 时间字符串。
        功能：统一 SQLite 中创建和更新时间的存储格式。
        """

        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
        """输入：``memories`` 查询得到的一行 SQLite 数据 ``row``。

        输出：反序列化后的 ``MemoryRecord``。
        功能：集中完成 JSON 字段和时间字段到长期记忆模型的转换。
        """

        return MemoryRecord(
            id=row["id"],
            memory_key=row["memory_key"],
            user_id=row["user_id"],
            topic=row["topic"],  # type: ignore[arg-type]
            entities=json.loads(row["entities_json"]),
            filters=json.loads(row["filters_json"]),
            conclusion=row["conclusion"],
            evidence=json.loads(row["evidence_json"]),
            confidence=row["confidence"],
            created_at=datetime.fromisoformat(row["created_at"]),
            updated_at=datetime.fromisoformat(row["updated_at"]),
            version=row["version"],
        )

    @staticmethod
    def _query_terms(query_text: str) -> list[str]:
        """输入：用户问题和槽位组成的检索文本 ``query_text``。

        输出：去重后的英文词和中文三字词列表。
        功能：生成可直接放入 trigram FTS5 ``MATCH`` 表达式的安全关键词。
        
        (question + topic + entities + filters) 生成关键词
                    ↓
        在 (topic + entities + conclusion) 的 FTS5 索引中匹配
        """

        terms: list[str] = []
        for word in _QUERY_WORDS.findall(query_text):
            if any("\u3400" <= char <= "\u9fff" for char in word):
                terms.extend(word[index : index + 3] for index in range(len(word) - 2))
            else:
                terms.append(word)
        return list(dict.fromkeys(terms))[:32]

    def upsert(self, record: MemoryRecord) -> MemoryRecord:
        """输入：包含稳定 ``memory_key`` 的候选长期记忆 ``record``。

        输出：写入后的当前版本 ``MemoryRecord``。
        功能：首次插入记忆；相同键再次写入时保留身份并更新结论和版本号。
        """

        values = (
            record.id,
            record.memory_key,
            record.user_id,
            record.topic,
            canonical_json(record.entities),
            canonical_json(record.filters),
            record.conclusion,
            canonical_json(record.evidence),
            record.confidence,
            self._datetime_text(record.created_at),
            self._datetime_text(record.updated_at),
        )
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO memories(
                    id, memory_key, user_id, topic, entities_json, filters_json,
                    conclusion, evidence_json, confidence, created_at, updated_at, version
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                ON CONFLICT(memory_key) DO UPDATE SET
                    topic = excluded.topic,
                    entities_json = excluded.entities_json,
                    filters_json = excluded.filters_json,
                    conclusion = excluded.conclusion,
                    evidence_json = excluded.evidence_json,
                    confidence = excluded.confidence,
                    updated_at = excluded.updated_at,
                    version = memories.version + 1
                """,
                values,
            )
            row = connection.execute(
                "SELECT * FROM memories WHERE memory_key = ?", (record.memory_key,)
            ).fetchone()
        if row is None:
            raise RuntimeError("长期记忆写入后无法读取记录")
        return self._row_to_record(row)

    def get_by_key(self, memory_key: str) -> MemoryRecord | None:
        """输入：长期记忆唯一键 ``memory_key``。

        输出：对应的当前版本记忆；不存在时返回 ``None``。
        功能：按稳定键读取单条长期记忆，供更新确认和诊断使用。
        """

        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM memories WHERE memory_key = ?", (memory_key,)
            ).fetchone()
        return self._row_to_record(row) if row else None

    def get_many(self, user_id: str, ids: Sequence[str]) -> dict[str, MemoryRecord]:
        """输入：可信用户标识 ``user_id`` 和 Milvus 返回的记忆 ID 序列 ``ids``。

        输出：只属于该用户且以 ID 为键的当前记忆。
        功能：从 SQLite 回表加载向量候选，并以权威记录再次隔离用户数据。
        """

        ids = list(dict.fromkeys(ids))
        if not user_id or not ids:
            return {}
        placeholders = ",".join("?" for _ in ids)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM memories WHERE user_id = ? AND id IN ({placeholders})",
                (user_id, *ids),
            ).fetchall()
        return {record.id: record for record in map(self._row_to_record, rows)}

    def search_fts(
        self, user_id: str, query_text: str, limit: int
    ) -> list[tuple[MemoryRecord, float]]:
        """输入：可信用户、当前检索文本和最大候选数。

        输出：按 BM25 分数排序的用户私有 ``(MemoryRecord, score)`` 列表。
        功能：优先执行中文 trigram FTS5 检索；二字中文关键词时回退到 SQLite 子串匹配。
        """

        terms = self._query_terms(query_text)
        if not user_id or limit <= 0:
            return []
        with self._connect() as connection:
            rows = []
            if terms:
                match = " OR ".join(f'"{term}"' for term in terms)
                rows = connection.execute(
                    """
                    SELECT m.*, bm25(memories_fts) AS score
                    FROM memories_fts JOIN memories AS m ON m.id = memories_fts.id
                    WHERE memories_fts MATCH ? AND m.user_id = ?
                    ORDER BY score LIMIT ?
                    """,
                    (match, user_id, limit),
                ).fetchall()
            if rows:
                return [(self._row_to_record(row), float(row["score"])) for row in rows]

            short_words = [
                word
                for word in re.findall(r"[\u3400-\u9fff]{2}", query_text)
                if word not in {"用户", "问题", "主题", "实体", "筛选", "条件"}
            ]
            if not short_words:
                return []
            rows = connection.execute(
                """
                SELECT * FROM memories
                WHERE user_id = ?
                  AND (topic || entities_json || filters_json || conclusion) LIKE ?
                ORDER BY updated_at DESC LIMIT ?
                """,
                (user_id, f"%{short_words[0]}%", limit),
            ).fetchall()
        return [(self._row_to_record(row), -1.0) for row in rows]


class DashScopeEmbeddingClient:
    """调用 DashScope 生成长期记忆的文本向量。"""

    def __init__(
        self,
        api_key: str,
        model: str = "text-embedding-v3",
        caller: Callable[..., Any] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        """输入：DashScope 密钥、模型名、可选调用函数和请求超时秒数。

        输出：惰性 Embedding 客户端；空密钥时抛出 ``RuntimeError``。
        功能：保存外部模型配置，并记录首个成功向量的维度。
        """

        self.api_key = api_key.strip()
        if not self.api_key:
            raise RuntimeError("缺少 DASHSCOPE_API_KEY，无法生成长期记忆向量")
        self.model = model.strip() or "text-embedding-v3"
        self.caller = caller or TextEmbedding.call
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.dimension: int | None = None

    def embed(self, text: str) -> list[float]:
        """输入：需要编码的非空文本 ``text``。

        输出：有限浮点数构成的 Embedding 向量；服务失败或维度变化时抛出 ``RuntimeError``。
        功能：复用批量调用的单条入口，兼容长期记忆等已有调用方。
        """

        return self.embed_many([text])[0]

    def embed_many(self, texts: Sequence[str]) -> list[list[float]]:
        """输入：需要编码的非空文本序列 ``texts``。

        输出：与输入顺序一致的有限浮点向量列表；服务失败、数量不符或维度变化时抛出 ``RuntimeError``。
        功能：按 DashScope 的十条上限批量生成触发语句向量，避免语义路由在冷启动时按样本串行等待网络响应。
        """

        inputs = [str(text).strip() for text in texts]
        if not inputs:
            return []
        if any(not text for text in inputs):
            raise ValueError("Embedding 文本不能为空")
        vectors: list[list[float]] = []
        for start in range(0, len(inputs), _EMBEDDING_BATCH_SIZE):
            batch = inputs[start : start + _EMBEDDING_BATCH_SIZE]
            response = self.caller(
                model=self.model,
                input=batch,
                api_key=self.api_key,
                request_timeout=self.timeout_seconds,
            )
            status_code = (
                response["status_code"] if isinstance(response, dict) else response.status_code
            )
            if status_code != HTTPStatus.OK:
                code = response["code"] if isinstance(response, dict) else response.code
                message = response["message"] if isinstance(response, dict) else response.message
                raise RuntimeError(
                    f"DashScope Embedding 调用失败: {code}: {message}"
                )
            output = response["output"] if isinstance(response, dict) else response.output
            raw_vectors = output["embeddings"]
            if len(raw_vectors) != len(batch):
                raise RuntimeError("DashScope Embedding 返回数量与输入不一致")
            vectors.extend(
                [float(value) for value in item["embedding"]]
                for item in raw_vectors
            )
        if any(not vector for vector in vectors):
            raise RuntimeError("DashScope Embedding 返回了空向量")
        if not all(math.isfinite(value) for vector in vectors for value in vector):
            raise RuntimeError("DashScope Embedding 向量包含非有限数值")
        dimensions = {len(vector) for vector in vectors}
        if len(dimensions) != 1:
            raise RuntimeError("DashScope Embedding 返回向量维度不一致")
        dimension = dimensions.pop()
        if self.dimension is None:
            self.dimension = dimension
        elif self.dimension != dimension:
            raise RuntimeError("Embedding 向量维度发生变化")
        return vectors


class MilvusVectorStore:
    """保存长期记忆的 Milvus 向量索引。"""

    def __init__(
        self,
        uri: str,
        collection_name: str = "memory_vectors",
        client: Any | None = None,
        timeout_seconds: float = 5.0,
    ) -> None:
        """输入：Milvus 地址、collection 名、可选客户端和请求超时秒数。

        输出：惰性 Milvus 存储对象。
        功能：保存向量服务配置，首次读写时按实际向量维度准备 collection。
        """

        self.uri = uri.strip() or "http://127.0.0.1:19530"
        self.collection_name = collection_name.strip() or "memory_vectors"
        self.client = client
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.dimension: int | None = None

    def _client(self) -> Any:
        """输入：无；使用构造时提供的客户端或 Milvus 地址。

        输出：可调用 Milvus API 的客户端。
        功能：延迟创建连接，使向量服务不可用不影响插件启动和 FTS5 召回。
        """

        if self.client is None:
            self.client = MilvusClient(uri=self.uri, timeout=self.timeout_seconds)
        return self.client

    @staticmethod
    def _vector(vector: Sequence[float]) -> list[float]:
        """输入：Embedding 返回的向量序列 ``vector``。

        输出：有限浮点数构成的向量；非法数值时抛出 ``ValueError``。
        功能：在发往 Milvus 前拒绝空向量、NaN 和 Infinity。
        """

        values = [float(value) for value in vector]
        if not values or not all(math.isfinite(value) for value in values):
            raise ValueError("Milvus 向量包含非有限数值")
        return values

    def _ensure_collection(self, dimension: int) -> None:
        """输入：本次实际 Embedding 维度 ``dimension``。

        输出：无；保证目标 collection 已加载且维度一致。
        功能：首次写入或查询时创建固定 schema 的 COSINE 向量索引。
        """

        if self.dimension is not None:
            if self.dimension != dimension:
                raise RuntimeError("Milvus collection 维度不一致")
            return
        client = self._client()
        if client.has_collection(
            collection_name=self.collection_name, timeout=self.timeout_seconds
        ):
            fields = client.describe_collection(
                collection_name=self.collection_name, timeout=self.timeout_seconds
            )["fields"]
            vector_field = next(field for field in fields if field["name"] == "vector")
            if int(vector_field["params"]["dim"]) != dimension:
                raise RuntimeError("Milvus collection 与 Embedding 维度不一致")
        else:
            schema = MilvusClient.create_schema(auto_id=False, enable_dynamic_field=False)
            schema.add_field(
                field_name="id", datatype=DataType.VARCHAR, is_primary=True, max_length=64
            )
            schema.add_field(field_name="user_id", datatype=DataType.VARCHAR, max_length=256)
            schema.add_field(field_name="topic", datatype=DataType.VARCHAR, max_length=64)
            schema.add_field(
                field_name="vector", datatype=DataType.FLOAT_VECTOR, dim=dimension
            )
            schema.add_field(field_name="updated_at", datatype=DataType.VARCHAR, max_length=64)
            index = MilvusClient.prepare_index_params()
            index.add_index(field_name="vector", index_type="AUTOINDEX", metric_type="COSINE")
            client.create_collection(
                collection_name=self.collection_name,
                schema=schema,
                index_params=index,
                timeout=self.timeout_seconds,
            )
        client.load_collection(collection_name=self.collection_name, timeout=self.timeout_seconds)
        self.dimension = dimension

    def upsert(self, record: MemoryRecord, vector: Sequence[float]) -> None:
        """输入：SQLite 当前记忆 ``record`` 及其 Embedding ``vector``。

        输出：无；向 Milvus 新增或覆盖同 ID 的向量。
        功能：让向量索引始终反映 SQLite 中该记忆的最新结论。
        """

        vector = self._vector(vector)
        self._ensure_collection(len(vector))
        self._client().upsert(
            collection_name=self.collection_name,
            data=[
                {
                    "id": record.id,
                    "user_id": record.user_id,
                    "topic": record.topic,
                    "vector": vector,
                    "updated_at": record.updated_at.isoformat(),
                }
            ],
            timeout=self.timeout_seconds,
        )

    def search(
        self, user_id: str, vector: Sequence[float], limit: int
    ) -> list[tuple[str, float]]:
        """输入：可信用户、查询向量和最大候选数。

        输出：用户私有的 ``(memory_id, cosine_score)`` 列表。
        功能：以 Milvus 用户过滤进行语义检索，并返回 SQLite 回表所需的记忆 ID。
        """

        vector = self._vector(vector)
        if not user_id or limit <= 0:
            return []
        self._ensure_collection(len(vector))
        hits = self._client().search(
            collection_name=self.collection_name,
            data=[vector],
            filter=f"user_id == {json.dumps(user_id)}",
            limit=limit,
            output_fields=["id", "user_id"],
            search_params={"metric_type": "COSINE", "params": {}},
            timeout=self.timeout_seconds,
        )[0]
        return [
            (str(hit["id"]), float(hit["distance"]))
            for hit in hits
            if hit["entity"]["user_id"] == user_id
        ]


__all__ = ["DashScopeEmbeddingClient", "MilvusVectorStore", "SQLiteMemoryStore"]
