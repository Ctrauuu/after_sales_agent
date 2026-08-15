# 售后知识库 RAG 的精准检索

本文是 [05f 设计文档](05-技术重难点/05f-售后知识库RAG精准检索.md) 的实现说明。目标是让 Hermes 回答保修、三包、政策、维修和安装条款时，基于带来源编号的原文片段作答。

## 1. 边界与结构

知识库使用独立的 Milvus collection：`knowledge_chunks`，与长期记忆的 `memory_vectors` 完全隔离。它不保存用户对话，也不参与 RBAC 明细查询。

```text
用户政策问题
  -> pre_llm_call
  -> 提取品类 / 品牌 / 部件
  -> DashScope Embedding
  -> Milvus 候选召回（至少 Top10）
  -> 关键词 + 元数据 + 时效重排
  -> Top3 原文及 [知识来源N] 注入 Hermes
  -> LLM 带编号引用回答
```

实现只复用了已有 DashScope、Milvus 和 Hermes Hook，不增加新的数据库或模型服务。

## 2. 入库元数据

每个片段在 Milvus 中保存正文、向量和以下元数据：

| 字段 | 用途 |
| --- | --- |
| `doc_type` | 来源优先级：品牌政策、苏宁制度、维修手册、国家法规、FAQ |
| `category` / `brand` | 匹配提问的适用范围 |
| `effective_at` / `expire_at` | 生效和失效检查 |
| `source_title` / `article_number` | 回答中的原文来源定位 |

现有 [mock-files](../backend/mock-files) 中的三包法节选、格力政策、苏宁制度已在入库脚本中登记元数据。脚本按空行切片，稳定 ID upsert，因此可安全重复执行：

```bash
cd backend
uv run python scripts/ingest_knowledge.py --input mock-files
```

脚本需要 `DASHSCOPE_API_KEY`，Milvus 地址默认读取 `MILVUS_URI`。知识库 collection 默认为 `knowledge_chunks`。

## 3. 检索与排序

`KnowledgeRAG.search()` 先用用户问题向量从 Milvus 取至少 10 个候选，然后按如下规则重排：

- 向量相似度占 70%，原文关键词覆盖率占 30%；
- 品类相同乘 `1.5`，不同时乘 `0.3`；
- 品牌相同乘 `1.3`；
- 文档类型权重依次为品牌政策 `1.0`、苏宁制度 `0.9`、维修手册 `0.8`、国家法规 `0.7`、FAQ `0.5`；
- 已失效或尚未生效的条款乘 `0.1`；
- 最终优先保留不同来源的 Top3，不足时再按分数补足同来源片段。

因此“格力空调压缩机保修多久”会优先返回匹配品牌和空调品类的格力政策；三包法规仍可作为引用依据，但不会盖过更具体、有效的品牌条款。

## 4. Hermes 注入与引用

[knowledge_hooks.py](../.hermes/plugins/suning-rbac-bridge/knowledge_hooks.py) 仅在消息包含“保修、三包、政策、条款、维修、安装、SLA”等知识关键词时运行。它会读取已有会话中的品类、品牌槽位，帮助处理“这个品牌呢”这样的追问。

注入文本的格式如下：

```text
[知识来源1] 格力电器售后服务政策（2024版） 一、保修期限
格力家用空调：整机免费保修6年，压缩机免费保修10年。
```

注入提示明确要求模型只能依据片段回答，并在结论后写 `[知识来源N]`。查询没有命中或 Milvus/Embedding 不可用时 Hook 返回空，不会阻断正常回答。

## 5. 配置与验证

新增配置如下：

```dotenv
KNOWLEDGE_MILVUS_COLLECTION=knowledge_chunks
KNOWLEDGE_EMBEDDING_MODEL=text-embedding-v3
KNOWLEDGE_TIMEOUT_SECONDS=10
KNOWLEDGE_TOP_K=3
```

验证时可在飞书发送：

```text
格力空调压缩机保修多久？请引用条款来源。
```

预期回答包含格力政策的原文结论和 `[知识来源1]`，而不是只笼统陈述“三包规定”。

## 6. 主要文件

- `packages/suning-context-runtime/.../knowledge_rag.py`：Milvus collection、混合重排和引用上下文。
- `backend/scripts/ingest_knowledge.py`：现有 mock 政策文档的切片和入库。
- `.hermes/plugins/suning-rbac-bridge/knowledge_hooks.py`：回答前检索与 Prompt 注入。
- `backend/tests/test_knowledge_rag.py`：品牌优先、时效降权、元数据读取和来源引用测试。
