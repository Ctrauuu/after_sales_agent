这个项目是基于Hermes二次开发,部署在企业内部、面向售后/运营团队,同时接入钉钉、飞书、企微等多个即时通讯平台的对话式售后数据分析agent
常见的应用场景有,在群里去询问昨天退单情况,不需要等BI出报表几秒内就能得到实时数据。用户投诉升级,用一句话就能拉出订单的完整售后链路,不需要去翻阅多个系统。发现某型号退货异常，几轮追问就能定位到具体批次而不需要导excel手动做透视等等。

首先用户在IM平台去给机器人发送自然语言信息,该请求会先经过Hermes gateway,但是不同平台的发送格式是不一样的 我们首先利用Hermes的平台adapter把不同平台格式统一成相同的MessageEvent。
gateway完成进行平台级处理(平台级鉴权、去重、命令识别以及会话恢复等)，然后通过ContextVar保存可信发送者身份建立可信请求上下文完成第一层鉴权。
然后进入业务插件,调用pre_llm_call前置钩子去MySQL根据IM账号绑定映射去查表映射成内部逻辑用户(保存对话的类型/逻辑会话id等)，完成第二层鉴权,并通过redis维护跨平台逻辑会话(私聊会通过redis键在30分钟内复用同一逻辑会话,而群聊则是用过ns去隔离群跟用户以及私聊内容)。

pre_llm_call 按以下顺序执行：
IdentitySessionRouter：校验用户身份，统一不同 IM 平台的用户与会话 ID。
TurnLease：通过 Redis 获取本轮会话锁，避免同一用户跨平台并发处理。
Observability：创建本轮 Trace，记录请求链路和用户问题。
ConversationHooks：提取话题、实体和筛选条件，更新 Redis 短期上下文。
LongTermMemoryHooks：通过 FTS5 和 Milvus检索该用户的历史业务结论。
KnowledgeHooks：从售后知识库召回并重排相关政策、法规和业务文档。
SkillEvolutionHooks：语义匹配已有 Skill，命中后注入可复用的 MCP 工作流。
ToolGovernor：根据当前话题选择工具白名单，初始化本轮预算并注入工具限制。
Context 合并：将短期上下文、长期记忆、RAG 知识、Skill 和工具规则合并，注入当前用户消息。

工具调用方式:模型选择工具，Bridge 转发，MCP 服务最终鉴权
agent选择已经注册的工具,首先检查会话在经过pre_tool_call后的会话状态,然后进行工具治理的硬参数校验(工具是否被禁用、是否在场景下的白名单、参数是否符合schema、有无命中工具结果的缓存、是否超mcp调用预算)
然后根据gateway中的上下文去得到真实的用户上下文并签发短期HMAC凭证
接着请求经过bridge转发并建立真实的MCP streamable http请求,并把凭证信息、Otel追踪链路用到的trace id包装在请求里,发送一次mcp tool call。然后在mcp server中去做凭证的验证(签名、签发方与接收方、防重放状态的检测),然后通过用户上下文去mysql中查表得到内部用户的角色权限与用户权限,再与请求范围三者求交集得到最后的范围权限,根据safe_filters生成可用的mysql语句追加到相应的查询语句中,最后根据数据范围进行结果脱敏,工具调用完以后通过工具调用处理器处理超时、重试、熔断、降级并转换成工具消息

post_llm_call 按以下顺序执行：
RequestState 校验：复用 pre_llm_call 保存的身份与逻辑会话，确认请求未被拒绝、未处于并发等待且仍持有 TurnLease。
ConversationHooks：保存本轮用户问题和最终回答，更新 Redis 最近对话、回复摘要及历史压缩结果。
LongTermMemoryHooks：提交异步记忆沉淀任务，从本轮问答中提取可复用业务结论，写入 SQLite FTS5 和 Milvus。
SkillEvolutionHooks：整理本轮问题、MCP 调用和最终回答；复杂度达到阈值后，异步创建或更新 Skill。
Observability：用最终回答结束本轮 Trace，并清理未结束的 LLM Span。
Context 清理：清除身份路由、拒绝状态、并发状态和租约丢失状态等 ContextVar。
TurnLease 释放：停止续租并释放 Redis 会话锁，允许下一轮或其他 IM 平台继续处理。



### 1.IM身份转换成统一用户与逻辑会话
通过UnifiedIdentityHooks去将所有hook注册到一个统一的身份的hook,将统一身份路由到各个子hook中去

首先看identity_session.py,该文件统一了IM身份解析、私聊会话路由以及hook转发。
可以拆分成:
1. IM 用户 -> Hermes_user_id 的映射
2. 原始 IM 会话 -> 内部逻辑 session_id 的映射
3. 把统一后的user/session 转发给各个hook
私聊是通过统一用户会话跨平台复用逻辑会话,而群聊则是通过(平台+群逻辑会话+用户进行隔离)

IM 发送者 -> IdentitySessionRouter(把外部 IM 平台的用户身份去解析成真正的内部逻辑用户身份,并判断聊天类型是私聊还是群聊,如果是私聊,那么会用redis查库并设置30分钟的热会话期限:使用 ``im:active-session:<hermes_user_id>`` 刷新活跃对话有效期去做私聊逻辑会话的延续,如果是群聊,则生成复合键去区分) -> SessionRoute(存储逻辑用户id+逻辑会话id+聊天类型+平台)

那么如何保证并发隔离? ConversationTurnLease模块中上锁实现并发。用 Redis 令牌租约串行化同一逻辑会话的完整 Agent 回合。
首先每个请求都有自己随机的随机所有者令牌token,在redis中设置NX,先SET NX成功的能够获得该session执行权(使用TTL+renew续租)

获得可信身份以后: 使用UnifiedIdentityHooks把路由应用到Hermes周期里,通过ContextVar(请求的临时状态)隔离请求级数据
验证完成在pre_llm_call逐个调用路由中的hooks(pre/post)
post_llm_call(写回阶段),保存的时候仍使用统一用户+逻辑session



### 2.短期上下文、长期记忆与知识检索注入
短期上下文(解决的是在同一个会话中上下指代内容):
在runtime中生成钩子,在钩子内部去执行。
用pre_llm_call来实现加载redis状态、提取slot、加载上下文,首先根据逻辑会话id来加载上下文,然后用用户信息、上下文slot中的话题、上下文中的过滤条件来提取出经过结构校验、数据库白名单和置信度门槛处理的槽位增量,再去判断是否是新的轮次(如果是新话题就清空旧槽位,如果是旧话题旧连续追问并更新),然后保存上下文并生成system prompt。
用Post_llm_call来实现生成回答以后,根据信息去做提取slot增量并更新槽位、轮次、历史(历史过长则压缩、总结),并将状态写入redis进行持久化

             build_conversation_runtime()                           
                           │
          ┌────────────────┼────────────────┐
          │                │                │
          ▼                ▼                ▼
       Redis             MySQL             LLM
          │                │                │
          ▼                ▼                │
 ContextManager   DatabaseWhitelistLoader   │
          │                │                │
          │                └──────┐         │
          │                       ▼         │
          │                  SlotExtractor ◄┘
          │                       │
          └──────────┐    ┌───────┘
                     ▼    ▼
               ConversationHooks

pre_llm_call                          post_llm_call
     │                                     │
     ▼                                     ▼
读取 Redis                           SlotExtractor
     │                                     │
     ▼                              LLM + MySQL白名单
给 Agent 旧上下文                           │
                                           ▼
                                      更新 Context
                                           │
                                           ▼
                                         Redis

长期记忆(解决的是这个用户以前得出过什么业务分析结论？并在以后遇到相似问题时进行召回):
与短期记忆类似 也是build_memory_hooks()组装然后注册 通过LongTermMemoryHooks的pre/post_llm_call来实现长期记忆
pre_llm_call:加载上下文得到slot并做处理(移除摘要并将过滤内容分拣为entities+filter的格式),然后交给管道处理得到按融合分数及时间衰减排序的用户私有 TopN 长期记忆,然后最后进行prompt的格式化
post_llm_call:判断该轮对话有没有值得沉淀结论形成长期记忆的东西(开启后台任务复制本轮快照并立即提交单线程任务,不让该post hooks等待)，根据MemoryExtractor去判断should_record=true/confidence > ?/topic在白名单中/conclusion非空是否满足。 

                 用户发送消息
                      ↓
            短期 Context Hook
                      ↓
          Redis 中的槽位已经更新
                      ↓
            pre_llm_call
                      ↓
      读取 user_id + 当前槽位
                      ↓
        pipeline.retrieve(...)
                      ↓
        SQLite FTS5 + Milvus
                      ↓
          历史长期记忆 TopK
                      ↓
        format_memory_context
                      ↓
        注入 Hermes LLM context
                      ↓
                 LLM 回答
                      ↓
            post_llm_call
                      ↓
      本轮问题 + 回答 + 槽位快照
                      ↓
        pipeline.submit_turn(...)
                      ↓
              后台异步处理
                      ↓
       MemoryExtractor / LLM
                      ↓
           是否值得形成记忆
                      ↓
        SQLite + Embedding + Milvus


长期记忆如何检索?
在检索器中使用关键字检索(fts5)+语义检索(milvus)两者等权融合召回,融合分数再乘一个时间衰减,向量召回失败时降级使用本地fts5结果,最后取top3

当前用户消息
+
Redis 当前 topic
+
当前 entities
+
当前 filters
        ↓
拼成查询文本
        ↓
┌───────────────┬────────────────┐
│ SQLite FTS5   │ Milvus         │
│ 关键词检索    │ 向量语义检索   │
└───────┬───────┴───────┬────────┘
        ↓               ↓
    top_k*2          top_k*2
    默认6条           默认6条
        └───────┬───────┘
                ↓
          按 memory_id 去重
                ↓
        两路分数归一化到 0~1
                ↓
combined_score
= 0.5 * keyword_score
+ 0.5 * semantic_score
                ↓
           时间衰减
                ↓
          final_score
                ↓
            排序
                ↓
             Top3

SQLite与Milvus如何储存?
sqlite通过memories(基础表)+memories_fts(虚表索引)存储,使用memory_key(user_id + topic + normalized entities + normalized filters)表示更新还是新建
Milvus通过topic+entities+filters+conclusion拼凑成文本向量化存储

MemoryExtraction
      ↓
生成 MemoryRecord
      ↓
计算 memory_key
      ↓
┌──────────────────────┐
│ SQLite memories      │
│ 保存完整长期记忆     │
└─────────┬────────────┘
          │
          ├── Trigger → memories_fts
          │              关键词索引
          │
          ↓
topic + entities + filters + conclusion
          ↓
text-embedding-v3
          ↓
       vector
          ↓
┌──────────────────────┐
│ Milvus memory_vectors│
│ 保存语义向量索引     │
└──────────────────────┘


### 3.RAG检索
主流程:
用户问题
   ↓
should_search_knowledge(),问题命中关键词即可
判断这一轮是否需要查知识库
   ↓
extract_entities(),确定性词典提取实体部分信息
提取 品类 / 品牌 / 部件
   ↓
KnowledgeRAG.search()
   ↓
Embedding
   ↓
Milvus向量召回候选,先search()得到备选,再在备选里打分召回(关键字召回分数:对向量进行轻量二元切分滑块 + 英文词的精确匹配混合排序 分数 = 命中的 term 数 / 总 term 数)
                     总分数 = 0.7 × 语义相似度 + 0.3 × 关键词覆盖率

   ↓
关键词 + 品类 + 品牌 + 来源 + 时效性 重排序(分数乘上业务权重,根据业务规则优先级评分进行rerank)
   ↓
Top-K + 来源去重
   ↓
format_knowledge_context()
   ↓
拼成 Prompt Context
   ↓
pre_llm_call 注入给 Hermes / LLM

哪些方法切块提高召回率:
| 方法                          | 效果                                                 |
| --------------------------- | -------------------------------------------------- |
| **调整 Chunk Size**           | 避免 chunk 太大导致语义稀释，也避免太小导致上下文缺失                     |
| **设置 Chunk Overlap**        | 防止关键信息刚好被切在两个 chunk 边界，提高命中概率                      |
| **按标题 / 段落 / 句子切块**         | 保留自然语义结构，比固定字符数切分更容易召回完整知识点                        |
| **一个 Chunk 保留一个主题**         | 减少无关信息，提高 chunk 与 Query 的相关度                       |
| **标题注入 Chunk**              | 给正文补充主题信息，对 BM25 和向量检索都有帮助                         |
| **Parent-Child Chunking**   | 用小 chunk 提高检索精度和召回，命中后返回大 chunk 保证上下文完整            |
| **Semantic Chunking**       | 根据语义变化切块，使同一个 chunk 内的内容语义更集中                      |
| **Multi-Granularity 多粒度切块** | 同时保存小块和大块，适应“具体问题”和“概括性问题”等不同 Query                |
| **根据文档类型设计切块规则**            | FAQ、Markdown、表格、说明书分别采用适合自身结构的切法，减少知识被错误拆分         |
| **通过 Recall@K 做实验调参**       | 比较不同 `chunk_size / overlap / 切块策略`，找到真实数据上召回率最高的方案 |


### 4.agent tool如何携带可信身份进入MCP
流程参照excalidraw

MCP Call
│
├── tool_name
│     get_order_detail
│
├── arguments
│     {
│       "order_id": 12345
│     }
│
└── _meta
      {
        "suning/authn": "<签名后的身份凭证>",
        "suning/trace_id": "..."
      }

### 5.MCP服务端验签、授权、生成行级范围、RBAC授权边界
MCP Tool 收到请求
      ↓
verify_attestation()
   验证 Hermes bridge 签发的短期凭证(验证通过以后,那么该用户使用该工具有哪些权限呢?既然要知道有什么权限,我们就需要知道它的身份是什么?而多平台其实是同一个人,那么需要得到内部身份!!!)
      ↓
resolve_user_context()
   飞书 open_id → 公司内部用户(如何才能知道用户身份？根据平台以及用户id去查表,一个人既有角色权限i.e.职位,也有用户权限)
      ↓
load_permission_policy()
   根据内部用户 role 查角色策略(也是查表 与构建用户上下文类似只不过是基于角色的)
      ↓
PermissionInterceptor.intercept()
   角色权限 ∩ 用户权限 ∩ 请求范围      收窄权限
      ↓
得到 safe_filters(得到了权限范围 那么如果体现在查询效果中呢?  在sql查询语句中附加条件收窄范围!)
      ↓
build_scope_clause()
   safe_filters → SQL 行级过滤
      ↓
执行业务 SQL
      ↓
mask_sensitive_data()
   根据 data_scope 脱敏
      ↓
返回结果
====================相当于=====================
认证 Authentication
      ↓
身份映射 Identity Mapping
      ↓
授权 Authorization
      ↓
数据范围 Data Scope
      ↓
行级权限 Row-Level Security
      ↓
结果脱敏 Data Masking


HMAC 验签解决什么问题？
验证身份声明有没有被伪造或篡改
除了普通验签(FastMCP -> 得到外部调用主体AuthenticatedPrincipal)外,还有mint_delegated_attestation是验什么的呢？
Timeline MCP时间线串联已经把上游token给用了,那么调用子MCP时又需要新的HMAC身份委托,而不是用已经被消费的凭证来验证

### 6.MCP服务
1. aftersale MCP
先搞懂什么是完整的售后流转工单:
以一个售后工单为核心，把退货、商品、订单、物流、退款、SLA 等跨系统数据关联起来，形成这次售后从发生到结束的完整业务上下文。
一个售后工单 = 针对某个订单里的某个商品/问题，一次售后处理过程的完整记录。(一笔订单可能有多个商品,多个售后问题,所以会有多个售后工单)

query_return_stats_nl2sql(售后统计查询,只针对退单):查询特定条件下的退单数据,预先在NL2SQL条件中限定维度。很多退单放在一起做统计分析
例如  最近7天哪个品类退单最多？  华东地区各品牌退单金额是多少？   空调退单主要是什么原因？

query_aftersale_nl2sql(针对售后业务整体做灵活的自然语言统计查询。)

get_aftersale_workflow(某一个具体售后案例，现在处理到哪了、之前经历了什么):拿一个订单号或退单号查出工单中各处理节点
例如 退单 R20001 现在处理到哪一步了？

2. order MCP
search_orders：解决“我有哪些订单 / 帮我找到那一单” 根据用户提供的条件去找到订单
e.g.  
“帮我查一下我最近的订单。”
“我上个月买的空调订单是哪一单？”
“帮我找一下还没发货的订单。”

get_order_detail：解决“这一个具体订单到底是什么情况”  根据提供的订单号去找到订单(不仅查询类订单号对应的订单 还通过跨系统查询了该订单下的相关退单)
e.g. 
“订单 123456 的详情是什么？”
“这笔订单现在什么状态？”
“这单买了什么东西？”

3. product MCP
get_product_info:通过SKU定位商品得到商品详情，再围绕这个商品跨多张业务表补齐保修、部件保修、常见故障(sku关联的历史常见故障及类型)和生产批次信息

4. logistics mcp
query_logistics:根据退单 ID 或订单 ID 查询按时间排列的完整逆向物流轨迹
return_id / order_id
    ↓
先去 t_aftersale_return 找退单
    ↓
关联订单 + 商品做权限校验
    ↓
拿到有权限查看的 return_id
    ↓
去 t_logistics_trace 查这些退单的物流节点
    ↓
返回逆向物流轨迹

5. payment mcp
第一层：入口选择
_return_selector()
return_id / order_id 二选一

第二层：业务上下文 + 权限
t_aftersale_return
+ t_order_main
+ t_product_sku
→ 找出“我有权看的 return_id”

第三层：支付数据
t_payment_refund
→ 每个 return_id 查最新 refund

### 7. NL2SQL
为什么tool中要用NL2SQL?
把自然语言转换成可以执行的sql语句 -> 适用于查询需求变化很多，但数据本身比较结构化

sandbox校验规则:
LLM生成SQL
   ↓
① 只能SELECT
② 禁止危险语句
③ 表必须合法
④ JOIN必须正确
⑤ 字段必须存在
⑥ 必须是聚合查询
⑦ 禁止敏感/明细字段
⑧ 强制WHERE
⑨ 自动补时间限制
⑩ 强制LIMIT
   ↓
注入服务端RBAC
   ↓
最终SQL
其中会对生成的sql进行校验与修正输出(是否通过, 最终 SQL, 错误说明) -> 验证通过的sql与safe_filter再经过rbac注入行级权限以及参数

ReadOnlyExecutor用于执行输出 包含 ``explain`` 执行计划和 ``rows`` 查询行的字典

### 8. 跨系统订单全链路数据串联
为什么要串联? 
用一个order_id并发查询订单、售后、逆向物流、退款MCP,根据时间跟退单记录合并成可追溯时间线,在时间线中标记卡点、SLA(公司给业务流程设定的“时限 + 质量红线”)、部分失败

trace_order_timeline MCP:
验证身份后恢复上游trace,并发调用四个私有MCP,最后聚合售后全链路

订单全链路聚合器
        ↓
TimelineMCPGateway(把一个订单id映射成四个可并行执行的私有MCP获取函数,通过gateway.fetch_order之类的来调用下游工具,内部是通过caller.call来实际调用)
        ↓
PrivateMCPCaller
        ↓
通过 MCP Streamable HTTP
调用 Order / Aftersale / Logistics / Payment
        ↓
把不同 MCP 返回格式统一成 Python 数据
        ↓
交还给上层时间线聚合器

OrderTimelineTracker的trace()做了什么？
1. 为每个数据源创建任务
2. 统一等待，设置整体超时() done or pending
3. _merge_timeline() :把订单、售后、物流、支付四种数据拼成统一时间线
检查下游返回格式是否正确，错误就记进 failures
让后面售后、物流、支付节点必须按 return_id 对齐，不能只按 order_id 全搅在一起
把四个系统的数据全部转换成 TimelineNode
去重、排序
计算 SLA 和当前卡点

如何实现OTel记录
接上游链路 → 创建自己的 Span → 把链路继续传给下游 → 最后结束 Span

Hermes / 上游
   │
   │ metadata 中带 traceparent
   ▼
订单 Timeline MCP
   │
   │ start_trace()
   │   └─ 恢复上游 Trace
   │   └─ 创建自己的 root span
   │
   ├──── Order MCP
   ├──── Aftersale MCP
   ├──── Logistics MCP
   └──── Payment MCP
          ↑
      每次 start_mcp_span()
      每次 inject_trace_metadata()
   │
   ▼
finish_trace()

trace_id：业务 Trace ID，用于日志检索
traceparent：OTel 真正用于维护父子 Span 关系的上下文

uuid 生成 suning.trace_id
→ Span attribute 和 MCP metadata 显式携带
→ OTel attach 当前 Span
→ propagate.inject 生成 traceparent
→ MCP Server propagate.extract 恢复 parent
→ 创建的新 Span 自动继承父子关系
→ span.end + context.detach 完成并恢复上下文

Trace 跟随 Agent 回合，Span 跟随每次真实的 LLM/MCP 调用边界

### 9. 图表生成与多IM适配输出
```
    ctx.register_tool(
        name="send_aftersale_chart",
        toolset="suning_business",
        schema=CHART_TOOL_SCHEMA,
        handler=handle_chart,
        is_async=True,
        description=str(CHART_TOOL_SCHEMA["description"]),
        emoji="📊",
    )
注册为hemers本地异步工具
```
send_aftersale_chart():
模型传入图表参数
    ↓
判断图表类型
    ↓
整理 labels / values
    ↓
校验 title / summary / ylabel
    ↓
读取当前 Hermes 会话平台
    ↓
是否支持飞书 / 企微 / 钉钉？
    │
    ├─ 否 → 返回 text_fallback
    │
    └─ 是
         ↓
    ChartFactory 生成图表图片
         ↓
    根据平台选择 Adapter
         ├─ 飞书 → FeishuImageAdapter
         ├─ 企微 → WeComBotImageAdapter
         └─ 钉钉 → DingTalkBotImageAdapter
         ↓
       发送图片
         ↓
    如果 HTTP/图表发送失败
         ↓
    返回文字降级结果


用户要求图表
  → Agent 先通过业务 MCP 获得聚合数据
  → 调用 send_aftersale_chart(labels, values, data_kind...)
  → 校验数据、选择图表类型
  → Matplotlib/Agg 生成桌面版与移动版 PNG
  → 从可信 Hermes 会话读取目标平台和会话 ID
  ├─ 飞书：上传两张 PNG → image_key → 富文本回复原消息
  ├─ 企微：移动版 PNG → 临时文件 → AI Bot WebSocket → 删除临时文件
  └─ 钉钉：移动版 PNG → MediaID → 企业机器人 Markdown 图片消息
  → 发送失败：返回 Top5 文字摘要，由正常回复链路呈现


解决了什么？
中文渲染(服务端配置中文字体,防止图标里都是方块)

IM API差异(不同平台API不同 因此每一个平台都需要配置一个适配器)

移动端适配(手机端与电脑端图标渲染参数不同 因此需要多套分辨率)

图表类型自动选择(llm需要根据数据特征生成合适的图标--折线图、饼图、柱状图、箱线图) -> 模型语义判断 + 代码规则兜底

降级策略(如果不支持文件,那么需要降级成文字摘要) -> 
数据有问题：明确报错，避免发送误导性摘要。
图片已经有效、只是平台发送失败：降级为文字。

### 10. 复杂分析任务的多 Agent 协作编排
TaskOrchestrator执行三件事:
1.plan:规划的合法 DAG(失败时返回固定降级DAG) 2.run:根据DAG并发派发subagent,在单节点和全局超时下继续执行可用分支
3.aggregate(把所有子agent的执行结果压缩成一份证据文本,交给主agent生成最终报告,如果聚合llm失败或超时则用退回固定规则兜底)

聚合过程:
所有子任务执行完成
      ↓
提取每个任务的：
task_type
task_id
status
summary / error
      ↓
拼成 evidence
      ↓
加入严格的 system prompt
      ↓
主聚合 LLM
      ↓
生成最终中文分析报告
      ↓
成功 → 返回报告
失败/超时 → _fallback_report(dag)


复杂任务是怎么拆分成多个可执行子任务的?
plan()中llm识别分析维度,输出子任务DAG并校验任务类型、依赖、环,执行该DAG(如果失败则使用固定fallback DAG)

DAG如何实现?
节点列表 + 依赖ID (TaskDAG1(...SubTask), TaskDAG2(...SubTask), ...)

子Agent职责如何划分?
DAG Task任务类型 + 节点目标 + 专用prompt + 工具权限 + 前置摘要 + 生命周期限制
主Agent负责理解意图并编排,并发调用最后聚合结果
子agent的硬边界:1.权限边界 不能继续创建子agent,只能调用限制工具,没有发送信息、记忆写入等能力
2.上下文边界 只能看到前置节点的压缩摘要 3.时间边界 受全局deadline限制 4.副作用边界 禁止发送图标与信息 使用图标类工具只会生成文本建议

子 Agent 失败怎么办 如何降级？
记录失败、继续其他分支、允许下游带缺失信息执行,最终输出部分报告(允许局部成功信息)
单节点失败
  → 标记 failed/timeout
  → 保存安全错误原因
  → 其他独立节点继续
  → 下游收到“前置结果暂不可用”
  → 聚合器生成部分报告

多个 Agent 得出不同结论怎么办?
不同子agent分析不同维度的问题,统一使用相同用户权限、时间窗口、业务工具、前置节点的摘要，因此大多数结论是互补关系
冲突识别主要依赖聚合agent的语义判断

### 11. Agent Skill 自进化闭环
hermes agent使用一种"经验沉淀 → 技能复用的自进化,本质上是“执行—评估—沉淀—复用”的经验闭环
   用户发起任务
      ↓
   语义匹配已有 Skill
      ├─ 高置信度命中 → 检查 MCP 依赖 → 注入工作流 → 执行
      └─ 未命中/依赖缺失 → 交给 LLM 正常规划执行
      ↓
   记录用户问题、历史轮次、MCP 名称和参数、最终回答
      ↓
   计算任务复杂度
      ├─ < 0.4 → 跳过，不沉淀
      └─ ≥ 0.4 → LLM 抽取通用工作流
                     ↓
               Embedding 相似度去重
                     ├─ > 0.8 → 更新已有 Skill
                     └─ ≤ 0.8 → 创建新 Skill
                                       ↓
                           写入 Hermes SKILL.md
                                       ↓
                              刷新语义路由索引
                                       ↓
                           后续相似问题直接复用
==========================================
自进化模块
   ↓
产生新的 Skill
   ↓
Skill Hub
   ↓
SemanticRouter 读取这些 Skill
   ↓
以后用户问题可以匹配到它
==========================================
hooks过程:
pre_llm_call
    ↓
创建 _TrackedTurn
    ↓
记录：
user_query
started_at
Agent 开始运行
    ↓
每次 MCP Tool 调用
    ↓
Hook 追加：
tool_name
arguments
    ↓
post_llm_call
    ↓
拿到 assistant_response
    ↓
计算总耗时
    ↓
组装 ExecutionTrace

根据复杂度去判断skill是否沉淀(复杂度 =
    轮次数 / 5 × 30%
  + MCP 调用数 / 5 × 40%
  + 执行时间 / 60秒 × 30%）
即通过复杂度门禁的ExecutionTrace

沉淀由调用llm完成

如何将抽取出来的skill与已有skill去比较重复度？
根据skill.description做语义相似度(> 0.8则重复 -> 更新 else 创建新的)


### 12. 意图语义路由

skill的索引如何构建？
它把每个 Skill 的 trigger_patterns 转成 Embedding 向量，再计算一个平均语义重心 centroid，最后把这些向量连同 Skill 信息一起保存在内存 _routes 中，形成可供 Query 快速匹配的向量索引。

      前置钩子：Skill 消费
用户问题
   ↓
Query Embedding
   ↓
与各 Skill 的 trigger_patterns 向量匹配
   ↓
取 Top-1
   ↓
confidence >= 0.85？
   ├─ 是 → 命中 Skill，进入 Skill 工作流
   └─ 否 → fallback，走正常 LLM 规划

      后置钩子：Skill 进化
任务结束
→ 收集执行轨迹
→ 复杂度判断
→ LLM 抽取工作流
→ 与已有 Skill 比较重复度
→ 创建新 Skill 或更新版本
→ 刷新语义路由索引

=============================
加载已有 Skill
    ↓
将每个 Skill 的触发问法生成 Embedding
    ↓
计算每个 Skill 的触发语句重心向量
    ↓
构建 N×D 向量矩阵
    ↓
用户问题生成 Embedding
    ↓
一次矩阵乘法计算与所有 Skill 的相似度
    ↓
取相似度最高的 Skill
    ├─ ≥ 0.85 → 命中并注入工作流
    └─ < 0.85 → 回退普通 LLM 规划


### 13. MCP 调用失败的重试与降级策略
解决的问题:MCP服务偶发失败、持续故障或超时的时候，不能让Agent loop任务崩溃，要尽可能恢复，哪怕恢复不了也要以可控方式返回部分结果。

invoke_business_tool()
        ↓
定义 attempt()
   （还没调用 MCP）
        ↓
call_manager.call(attempt=attempt)
        ↓
Manager 先检查熔断状态
        ↓
允许调用？
   ┌────┴────┐
   否         是
   ↓          ↓
直接返回     Manager 执行
熔断结果     await attempt()
                  ↓
            真正 session.call_tool()
                  ↓
               得到结果
================================
Manager 执行 attempt()
        ↓
       超时
        ↓
是否允许 retry？
        ↓
      再执行 attempt()
        ↓
最终还是失败
        ↓
记录 FailureType
        ↓
更新熔断失败次数
        ↓
按 DegradeLevel 降级
        ↓
MCPCallResult(success=False, degraded=True)

=====================================
本质上是在agent与mcp server之间加一层容错层,负责熔断、重试、降级
其中核心只有 4 件事：
1. 重试 Retry
   解决偶发超时。比如第一次超时，等一会再调一次，最多重试几次。
2. 熔断 Circuit Breaker
   解决 MCP 持续故障。比如某个 MCP 连续失败 5 次，就先标记为 OPEN，后面的请求暂时不再真正访问它，避免一直浪费时间。
3. 降级 Degrade
   MCP 最终还是不可用时，不一定让整个 Agent 失败。
   例如物流信息查不到，但订单、退款信息还能查，就返回“物流暂不可用，其他结果正常”。
4. 统一结果 MCPCallResult
   不管最终是成功、重试成功、熔断还是降级，都包装成统一结构：

熔断状态如何保存？
熔断状态保存在Redis中,按server_id隔离
state 不存在              → CLOSED
state、cooldown 都存在     → OPEN
state 存在、cooldown 已过期 → HALF_OPEN

MCPCallManager.call()实现逻辑:
call(...)
   │
   ├─ ① 先检查熔断状态(CLOSED正常 OPEN熔断 HALF_OPEN试探恢复)
   │
   ├─ ② 如果允许调用 → 执行 attempt()
   │
   ├─ ③ 判断调用成功还是哪种失败
   │
   ├─ ④ 如果是可重试超时 → 再 attempt()   (等待时间用指数退避方式指数增长)
   │
   ├─ ⑤ 得到最终成功 / 最终失败
   │
   ├─ ⑥ 更新 Redis 中的熔断状态
   │
   └─ ⑦ _result() → MCPCallResult

HALF_OPEN(不允许 Retry)
    ↓
尝试获得 probe lease(尝试抢探针租约)
    ↓
┌──────────────┐
│              │
拿到           没拿到
│              │
▼              ▼
执行一次       说明其他进程
attempt         已经在测试
禁止 Retry      ↓
              不调用 MCP

状态如何转换？
                 连续故障达到阈值
        CLOSED ─────────────────→ OPEN
          ▲                        │
          │                        │ 冷却时间到
          │                        ▼
          └────────────────── HALF_OPEN
              探针成功          │
                               │ 探针失败
                               ▼
                              OPEN


降级如何体现:
MCP 最终失败
   ↓
success = False
   ↓
degraded = True
   ↓
看 degrade_level
   │
   ├─ L1 → 少一个辅助数据，继续
   ├─ L2 → 少一个核心维度，带“不完整”提示继续
   └─ L3 → 无法继续，tool_error
   ↓
_hermes_result(call_result)把 L1/L2/L3 真正转换成 tool_result 或 tool_error



### 14. Prompt 版本管理与 A/B 评估框架
解决的问题:Prompt 改了以后，能不能追踪、比较，并用数据判断新版本到底有没有变好
本质上是把 Prompt 当成代码一样做版本管理，再把新旧 Prompt 当成两个模型版本，在固定 Golden Dataset 上跑相同测试，通过 SQL 正确率、执行结果、延迟等指标决定新版是否值得上线。


通过 Git 跟踪的 Prompt 版本管理,是通过promptregistry负责yaml文件 git负责版本提交/diff.....一系列操作
Golden Dataset 是 A/B 测试的固定基准集，保证新旧 Prompt 在同一批题目上比较。

在nl2sql的基础上,
yaml文件是管理prompt信息的,而其中有
```
  可用Schema：
  {{schema_json}}

  合法关联：
  {{joins_json}}

  中文品类与编码映射：
  {{categories_json}}
```
的占位符,在schema_registry中向版本化完整模板注入

其余就是nl2sql中的部分

PromptVersion = 考生
GoldenCase    = 试题
EvalReport    = 成绩单

核心测评部分:每个用例分别调用新旧 Prompt 三次，规范化 SQL 后多数投票，再评分汇总。
                    Golden Case
                        │
              “最近7天退单数量”
                        │
           ┌────────────┴────────────┐
           ↓                         ↓
       Prompt V1                 Prompt V2
           ↓                         ↓
     生成 SQL × 3              生成 SQL × 3
           ↓                         ↓
       多数投票                   多数投票   
投票是指同一个 Prompt + 同一个问题，让 LLM 连续生成多次 SQL，然后看哪一种 SQL 出现次数最多，就选哪一种。
           ↓                         ↓
     SQL Validator              SQL Validator
           ↓                         ↓
      SQL Executor               SQL Executor
           ↓                         ↓
         打分                       打分
           └────────────┬────────────┘
                        ↓
                       比较
                        ↓
                  old / new / tie

total =
    syntax_score * 0.4
    + result_score * 0.5
    + latency_score * 0.1

新 Prompt 至少领先旧 Prompt 约 3% 才赢

如何评价新Prompt能不能通过 CI？
① new 必须整体胜出
② SQL 语法通过率不能下降
③ 结果匹配率不能下降
④ 语法通过率 / 结果匹配率
   至少有一个真正提升


### 15. Agent 行为控制与 Tools 调用优化
解决的问题:agent工具滥用，限制agent使用正确的工具并且节省成本

用户信息 -> 识别场景意图 -> 场景工具白名单 -> prompt约束 -> ToolGovernor硬校验 -> MCP Bridge -> 重试 -> 结果缓存

ToolGovernor:工具调用治理,负责控制 Agent 发起的工具调用是否合理、是否需要真的执行，以及执行成功后是否可以缓存。(在插件中注册 在hooks与bridge之间起作用)

相关schema:ToolWhiteList(场景可以调用工具集) SCENE_TOOL_MAP(场景到工具集的映射) _TurnState(异步agent回合场景、已发起的调用)   ToolDecision(工具调用预检查后的结果) ToolGovernor(白名单、预算、缓存)

ToolGovernor.preflight()
preflight(tool_name, params)
① 工具被管理员禁用了吗？
    ↓

② 当前场景允许这个 Tool 吗？
    ↓

③ 参数合法吗？
    ↓

④ Redis 有成功缓存吗？
    ↓

⑤ 本轮是不是已经执行过完全相同的查询？
    ↓

⑥ 本轮 MCP 调用次数超限了吗？
    ↓

⑦ 全通过
    ↓
允许真正调用 MCP

### 16. 多 Agent 的 Harness

1. 调用级 Harness:
这个harness只负责某一次子agent调用怎么被启动、等待、取消、取结果
主链:
创建子 Agent 记录
   ↓
launch()
   ↓
wait()
   ↓
超时？→ cancel()
   ↓
result()
   ↓
成功返回 summary
失败抛异常

allowed_toolsets=("suning_business",)
→ 限制这个 Leaf Agent 能用哪些工具。
timeout_seconds + wait()
→ 限制这个子 Agent 最多跑多久。
cancel()
→ 超时后不是放任它继续跑，而是 Harness 主动终止。
AgentStatus + 日志 + _instances
→ Harness 统一登记、跟踪这次子 Agent 的生命周期状态。

2. 系统级harness
金丝雀（Canary Probe）可以理解成Harness 定期主动给 Agent 出一道固定的“业务测试题”，检查这个 Agent 不只是进程活着，而是真的还能正常干活。

① 启动 Agent
start_agent()
   ↓
启动进程
   ↓
健康检查
   ↓
注册实例
   ↓
发布流量配置


② 升级 Agent
rolling_restart()安全地完成 Agent 版本替换
   ↓
启动新版本
   ↓
10% → 30% → 60% → 100%
   ↓
逐步切流
   ↓
停止旧版本


③ 请求路由
route_request(),route_request() 会先筛选出当前可接流量的 Agent 实例，再结合各实例的 traffic_weight 做分流；具体选择时用 user_id 做稳定 Hash，把当前用户映射到某个权重区间，最终返回对应的 Agent 实例。
健康筛选 + 权重分流 + user_id 稳定路由。
   ↓
找健康实例
   ↓
根据 user_id + 权重
   ↓
选择具体 Agent 实例


④ 持续健康检查
canary_loop()  持续定时扫描所有配置了金丝雀规则的 Agent 实例，并逐个执行一次业务健康检查。
   ↓
定期给 Agent 发固定业务问题
   ↓
判断工具调用和结果是否正常
   ↓
失败 → DEGRADED
   ↓
连续失败 → UNHEALTHY + weight=0 + 告警 (在阈值后切走实例流量和发送告警)


⑤ 配置热更新
watch_config() 持续监控某个 Agent 的配置文件有没有变化，一旦变化，就通知对应 Agent 做热加载。
   ↓
发现 YAML / Prompt 修改
   ↓
Redis publish
   ↓
通知 Agent 重新加载配置

### 17. 售后领域知识图谱
解决的问题:RAG只能搜索到相关切片,但没办法给出图查询的精确关系
知识图谱把售后领域的实体与关系结构化储存,让agent能够走图查询定位答案

这套知识图谱不是独立图数据库，而是复用现有 MySQL 业务表，把品牌、SKU、品类、部件、保修、故障、批次、退单、政策来源这些实体通过外键关系组织成图，再通过JOIN和递归CTE做确定性关系查询。QueryRouter负责“图谱优先、查不到再RAG”。

先来看表结构
                         t_product_brand
                              品牌
                         PK brand_id
                              │
                              │ 1:N
                              ↓
                         t_product_sku
                              SKU
                         PK sku_code
                    ┌─────────┼──────────────┐
                    │         │              │
          category_l3_code  sku_code       sku_code
                    │         │              │
                    ↓         ↓              ↓
          t_product_category  t_batch_number  t_product_warranty
                 品类              批次             整机保修
          PK category_code     PK batch_id       PK id
          FK parent_code       FK sku_code       FK sku_code
             │       │              │              │
             │       │              │ batch_id     │ source_id
             │       │              ↓              │
             │       │       t_aftersale_return    │
             │       │              退单            │
             │       │         PK return_id         │
             │       │         FK batch_id          │
             │       │                              │
             │       └───────────────────┐          │
             │                           │          │
             │ category_code             │          │
             ↓                           │          │
     t_component_warranty                │          │
            部件保修                      │          │
       PK warranty_id                    │          │
       FK category_code                  │          │
       FK source_id                      │          │
             │                           │          │
             │ source_id                 │          │
             └──────────────┐            │          │
                            ↓            │          │
                    t_warranty_source  ←──┴──────────┘
                         条款来源
                      PK source_id

① 商品主关系
品牌 t_product_brand → SKU t_product_sku → 品类 t_product_category → 父品类 t_product_category
② 保修关系
SKU t_product_sku → 整机保修 t_product_warranty → 条款来源 t_warranty_source
品类 t_product_category → 部件保修 t_component_warranty → 条款来源 t_warranty_source
③ 故障与售后关系
SKU t_product_sku → SKU故障映射 t_sku_fault_map → 故障类型 t_fault_type → 品类 t_product_category
SKU t_product_sku → 批次 t_batch_number → 退单 t_aftersale_return

图查询场景:
1. query_warranty()：精确查保修
有 component：
品牌 → SKU → 品类 → 部件保修 → 条款来源
没有 component：
品牌 → SKU → 整机保修 → 条款来源
2. query_fault_association
故障类型(递归展开所有子品类)->SKU故障映射->SKU->品牌/品类/批次/退单
fault_type
“哪些型号容易出现压缩机故障？”
                 ↓
      t_fault_type 找到
          “压缩机故障”
                 ↓
      category_code = 空调
                 ↓
       Recursive CTE
         展开子品类
                 ↓
     空调 / 壁挂空调 / 柜机
                 ↓
        t_sku_fault_map
                 ↓
        找故障关联 SKU
                 ↓
            t_product_sku
          /       |        \
         ↓        ↓         ↓
       品牌      品类       批次
                           ↓
                          退单
                           ↓
              COUNT(DISTINCT return_id)
                           ↓
                 按 SKU + 批次分组
                           ↓
                 退单数量降序 Top10

3. query_product_chain
给一个 SKU，向上找到它完整的品类层级，同时把这个 SKU 的品牌、可保修部件、常见故障一起聚合出来。
SKU
├── 品牌
├── 品类路径
│   └── 三级品类 → 父品类 → 更上层品类
├── 可保修部件
└── 常见故障

4. ingest_from_policy_doc
把一份售后政策原文交给 LLM，抽取成“品类—部件—保修期限—条款来源”的结构化关系，再写入MySQL知识图谱。

如何进行路由?
先用正则判断“这个问题像不像可以精确查图的问题”；能查图就优先查图，图里没结果就退化到 RAG；如果一开始就不属于图谱支持的问题，直接走 RAG。

### 18. Agent 推理效率优化
解决的问题:如何在agent loop中减少token损耗、执行速度、llm调用轮次
实现思路基于"能提前返回就不继续推理"
四层优化:热点结果缓存、prompt缓存、模型分层、上下文压缩
=====================
用户请求
  ↓
热点预计算缓存命中？()
  ├─ 是 → 直接返回，Token=0
  └─ 否
      ↓
复用 System Prompt
      ↓
根据场景和置信度选择 Lite / Full
      ↓
上下文是否超过 15K Token？
  ├─ 是 → 压缩早期轮次
  └─ 否 → 保持现状
      ↓
调用 LLM
      ↓
记录模型、延迟、Token、Prompt 缓存命中
====================================


layer1:热点查询预计算缓存
把高频固定查询提前执行并缓存(通过定时cron预热阶段写入redis)，在线请求只要能映射到同一 scene + params，就直接走缓存快速路径

用户问题 ->  scene + params -> 预计算结果

layer2:System Prompt 缓存
同一个业务场景的 System Prompt 通常长期不变，所以没必要每次请求都重新 format()、重新从配置生成；可以把渲染后的 Prompt 缓存在本地内存和 Redis 中复用。
两层缓存(本地缓存追求速度，Redis 负责多实例共享。)
L1：进程内 dict
        ↓ miss

L2：Redis
        ↓ miss

重新渲染 Prompt


layer3:模型分层
先判断任务是否足够简单、明确，简单任务走 Lite 降低延迟和成本，其余请求保守地交给 Full 模型

路由规则:
complexity < 0.85
并且 scene 属于
return_analysis / order_trace
或者scene 是simple_query / greeting
        ↓
      Lite
其他则选用full模型

layer4:上下文压缩
多轮对话越来越长时，不再把完整历史都塞给模型，而是把较早的对话压缩成摘要，只保留少量近期信息，从而降低输入 Token、延迟和成本。
上下文超过 15K Token 时，把早期多轮对话压缩成摘要，保留最近三轮，控制发送给模型的上下文大小。

