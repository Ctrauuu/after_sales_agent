"""将现有 mock 售后政策文本切片、向量化并写入知识库 Milvus collection。
完整文件
→ TextLoader
→ 1 个完整 Document
→ 按空行进行结构切块
→ 多个 section Document
→ 超过 800 字符的 section 再进行长度切块
→ 最终 Document 列表
→ 每个 Document 转换成 KnowledgeChunk
→ 对每个 KnowledgeChunk.content 生成向量
→ KnowledgeChunk + vector 写入 Milvus
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from langchain_community.document_loaders import TextLoader
from langchain_core.documents import Document
from langchain_text_splitters import RecursiveCharacterTextSplitter
from suning_context_runtime import (
    DashScopeEmbeddingClient,
    DocType,
    KnowledgeChunk,
    KnowledgeMilvusStore,
)


DOCUMENT_METADATA = {
    "三包法-节选.txt": {
        "doc_type": DocType.NATIONAL_LAW,
        "source_title": "部分商品修理更换退货责任规定（三包规定）",
        "effective_date": datetime(1995, 8, 25, tzinfo=timezone.utc),
        "article_number": "三包有效期目录（节选）",
    },
    "格力售后政策.txt": {
        "doc_type": DocType.BRAND_POLICY,
        "source_title": "格力电器售后服务政策（2024版）",
        "category": "空调",
        "brand": "格力",
        "effective_date": datetime(2024, 1, 1, tzinfo=timezone.utc),
    },
    "苏宁售后管理制度.txt": {
        "doc_type": DocType.SUNING_POLICY,
        "source_title": "苏宁易购售后管理制度（内部文件·节选）",
        "effective_date": datetime(2024, 1, 1, tzinfo=timezone.utc),
    },
}
ARTICLE_PATTERN = re.compile(
    r"第[一二三四五六七八九十百零〇0-9]+[章节]|^[一二三四五六七八九十]+、[^\n]+",
    re.MULTILINE,
)
TEXT_SPLITTER_SEPARATORS = ("",)


def _article_number(text: str) -> str:
    """输入：一个已切分的政策文本片段 ``text``。

    输出：首个条款号或章节标题；不存在时返回空字符串。
    功能：从原文保留可展示的引用定位信息，供回答时标注具体法规条款或政策章节。
    """

    matched = ARTICLE_PATTERN.search(text)
    return matched.group(0).strip() if matched else ""


def load_document(path: Path) -> Document:
    """输入：已登记的 ``.txt`` 政策文件路径 ``path``。

    输出：带业务元数据的 LangChain ``Document``；未登记文件或 Loader 返回异常数量时抛出 ``ValueError``。
    功能：使用 LangChain ``TextLoader`` 读取原始文本，并将文件级业务元数据附加给后续 Splitter 产生的每个片段。
    """

    metadata = DOCUMENT_METADATA.get(path.name)
    if metadata is None:
        raise ValueError(f"未登记知识文档元数据: {path.name}")
    documents = TextLoader(str(path), encoding="utf-8").load()
    if len(documents) != 1:
        raise ValueError(f"知识文档必须加载为一个 Document: {path}")
    document = documents[0]
    document.metadata.update(metadata)
    document.metadata["source_filename"] = path.name
    return document


def split_document(document: Document, chunk_size: int = 800) -> list[Document]:
    """输入：带业务元数据的 LangChain ``Document`` 和单片最大字符数 ``chunk_size``。

    输出：继承原始元数据的 LangChain ``Document`` 片段列表；尺寸不足时抛出 ``ValueError``。
    功能：先保留原有的空行段落边界，再使用 LangChain Splitter 对超长段落按字符截断，保持当前入库的零重叠策略。
    """

    if chunk_size < 100:
        raise ValueError("chunk_size 必须不小于 100")
    splitter = RecursiveCharacterTextSplitter(
        separators=list(TEXT_SPLITTER_SEPARATORS),
        chunk_size=chunk_size,
        chunk_overlap=0,
    )
    sections = [
        Document(page_content=section.strip(), metadata=dict(document.metadata))
        for section in re.split(r"\n\s*\n", document.page_content)
        if section.strip()
    ]
    return splitter.split_documents(sections)


def documents_to_chunks(documents: list[Document]) -> list[KnowledgeChunk]:
    """输入：由 LangChain Loader 和 Splitter 产生、含业务元数据的 ``Document`` 片段列表。

    输出：可写入知识库的带稳定 ID ``KnowledgeChunk`` 列表；缺少必要元数据时抛出 ``ValueError``。
    功能：将通用 ``Document`` 的正文和元数据映射为领域知识片段，并提取可展示的条款定位信息。
    """

    chunks: list[KnowledgeChunk] = []
    for index, document in enumerate(documents):
        metadata = document.metadata
        content = document.page_content.strip()
        source_filename = str(metadata.get("source_filename") or "").strip()
        doc_type = metadata.get("doc_type")
        effective_date = metadata.get("effective_date")
        source_title = str(metadata.get("source_title") or "").strip()
        if (
            not content
            or not source_filename
            or not isinstance(doc_type, DocType)
            or not isinstance(effective_date, datetime)
            or not source_title
        ):
            raise ValueError("知识 Document 缺少必要元数据")
        chunk_id = hashlib.sha256(
            f"{source_filename}:{index}:{content}".encode("utf-8")
        ).hexdigest()
        chunks.append(
            KnowledgeChunk(
                chunk_id=chunk_id,
                content=content,
                doc_type=doc_type,
                category=str(metadata.get("category") or ""),
                brand=str(metadata.get("brand") or ""),
                effective_date=effective_date,
                source_title=source_title,
                article_number=_article_number(content)
                or str(metadata.get("article_number") or ""),
            )
        )
    return chunks


def chunk_document(path: Path, chunk_size: int = 800) -> list[KnowledgeChunk]:
    """输入：已登记的 ``.txt`` 政策文件路径 ``path`` 和单片最大字符数 ``chunk_size``。

    输出：带文件预设元数据和稳定 ID 的知识片段列表；输入无效时抛出 ``ValueError``。
    功能：编排 LangChain Loader、Splitter 和领域模型转换，保持现有知识入库调用接口不变。
    """

    return documents_to_chunks(split_document(load_document(path), chunk_size))


def ingest_directory(
    input_dir: Path,
    store: KnowledgeMilvusStore,
    embedder: DashScopeEmbeddingClient,
) -> int:
    """输入：包含已登记政策文本的目录 ``input_dir``、知识向量存储和 Embedding 客户端。

    输出：成功写入 Milvus 的知识片段数量。
    功能：遍历现有 mock 文档，逐片生成向量并按稳定 ID upsert，支持重复执行以更新已有知识。
    """

    count = 0
    for path in sorted(input_dir.glob("*.txt")):
        for chunk in chunk_document(path):
            store.upsert(chunk, embedder.embed(chunk.content))
            count += 1
    return count


def main() -> None:
    """输入：命令行的输入目录、collection、Milvus、Embedding 模型和超时参数。

    输出：向标准输出打印已写入的片段数量；缺少密钥、目录或入库失败时抛出异常。
    功能：提供可重复执行的知识库初始化入口，只读取项目已有的 mock 政策文件。
    """

    parser = argparse.ArgumentParser(description="入库售后知识库 mock 文档")
    parser.add_argument("--input", default="mock-files")
    parser.add_argument("--collection", default=os.getenv("KNOWLEDGE_MILVUS_COLLECTION", "knowledge_chunks"))
    parser.add_argument("--milvus-uri", default=os.getenv("MILVUS_URI", "http://127.0.0.1:19530"))
    parser.add_argument("--embedding-model", default=os.getenv("KNOWLEDGE_EMBEDDING_MODEL", "text-embedding-v3"))
    parser.add_argument("--timeout", type=float, default=float(os.getenv("KNOWLEDGE_TIMEOUT_SECONDS", "10")))
    args = parser.parse_args()

    input_dir = Path(args.input)
    if not input_dir.is_dir():
        raise ValueError(f"知识文档目录不存在: {input_dir}")
    api_key = os.getenv("DASHSCOPE_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("缺少 DASHSCOPE_API_KEY，无法入库知识文档")
    store = KnowledgeMilvusStore(args.milvus_uri, args.collection, timeout_seconds=args.timeout)
    embedder = DashScopeEmbeddingClient(api_key, args.embedding_model, timeout_seconds=args.timeout)
    print(f"已写入 {ingest_directory(input_dir, store, embedder)} 个知识片段到 {args.collection}")


if __name__ == "__main__":
    main()
