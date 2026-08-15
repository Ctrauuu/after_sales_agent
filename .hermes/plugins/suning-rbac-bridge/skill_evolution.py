"""从复杂 Agent 执行轨迹沉淀并复用 Hermes Skill。"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import logging
import math
import os
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping


logger = logging.getLogger(__name__)
EVOLUTION_COMPLEXITY_THRESHOLD = 0.4
SIMILARITY_THRESHOLD = 0.8
MAX_TRACKED_TURNS = 1024
_JSON_BLOCK_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)
_TRIGGER_TERMS = ("退单", "退货", "原因", "趋势", "品类", "售后", "统计")


@dataclass
class ExecutionTrace:
    """一次任务完成后可用于抽象工作流的最小执行轨迹。"""

    user_query: str
    turns: list[dict[str, Any]]
    mcp_tools_used: list[str]
    total_duration_seconds: float
    final_output: str


@dataclass
class SkillDefinition:
    """存储在 Hermes Skills Hub 中的可复用工作流定义。"""

    skill_id: str
    name: str
    description: str
    version: int
    trigger_patterns: list[str]
    workflow: list[dict[str, Any]]
    required_mcp_tools: list[str]
    output_template: str
    created_at: datetime
    updated_at: datetime
    usage_count: int = 0
    avg_quality_score: float = 0.0


def _text(value: Any, limit: int = 2000) -> str:
    """输入：任意文本值 ``value`` 与最大字符数 ``limit``。

    输出：空白归一化且受长度限制的字符串。
    功能：收敛不可信的模型、工具和用户文本，避免 Skill 文件或提示词无限增长。
    """

    return " ".join(str(value or "").split())[:limit]


def _parse_json_object(value: Any) -> dict[str, Any]:
    """输入：LLM 返回的文本或已解析值 ``value``。

    输出：JSON 对象；无法取得对象时抛出 ``ValueError``。
    功能：兼容宿主模型在 JSON 前后附加说明的情况，并拒绝数组等不符合工作流协议的结果。
    """

    if isinstance(value, Mapping):
        return dict(value)
    content = str(value or "").strip()
    start, end = content.find("{"), content.rfind("}")
    if start < 0 or end < start:
        raise ValueError("Skill 提取未返回 JSON 对象")
    parsed = json.loads(content[start : end + 1])
    if not isinstance(parsed, Mapping):
        raise ValueError("Skill 提取结果不是对象")
    return dict(parsed)


def _workflow(raw_workflow: Any) -> list[dict[str, Any]]:
    """输入：LLM 提取的候选工作流 ``raw_workflow``。

    输出：只含合法步骤、工具、参数模板和输出键的步骤列表。
    功能：在 Skill 落盘前过滤模型生成的畸形步骤，防止后续匹配注入不可执行指令。
    """

    if not isinstance(raw_workflow, list):
        return []
    steps: list[dict[str, Any]] = []
    for index, raw_step in enumerate(raw_workflow, start=1):
        if not isinstance(raw_step, Mapping):
            continue
        tool = _text(raw_step.get("tool"), limit=100)
        if not tool:
            continue
        params = raw_step.get("params", raw_step.get("params_template", {}))
        if not isinstance(params, Mapping):
            params = {}
        output_key = _text(raw_step.get("output_key"), limit=100) or f"result_{index}"
        step = raw_step.get("step", index)
        steps.append(
            {
                "step": step if isinstance(step, int) and step > 0 else index,
                "tool": tool,
                "params": dict(params),
                "output_key": output_key,
            }
        )
    return steps


def _extracted_skill(raw: Mapping[str, Any]) -> dict[str, Any] | None:
    """输入：LLM 返回的候选 Skill 对象 ``raw``。

    输出：可安全持久化的规范字段字典；不应创建或缺少必要字段时返回 ``None``。
    功能：集中校验自动沉淀边界，只让具备名称、描述和工具工作流的复杂任务进入 Skills Hub。
    """

    if raw.get("should_create_skill") is not True:
        return None
    name = _text(raw.get("skill_name"), limit=120)
    description = _text(raw.get("description"), limit=500)
    workflow = _workflow(raw.get("workflow"))
    if not name or not description or not workflow:
        return None
    patterns = [
        pattern
        for item in raw.get("trigger_patterns", [])
        if (pattern := _text(item, limit=300))
    ] if isinstance(raw.get("trigger_patterns", []), list) else []
    return {
        "skill_name": name,
        "description": description,
        "trigger_patterns": list(dict.fromkeys(patterns)),
        "workflow": workflow,
        "output_template": _text(raw.get("output_template"), limit=3000),
    }


def _skill_payload(skill: SkillDefinition) -> dict[str, Any]:
    """输入：内存中的 ``SkillDefinition`` 实例 ``skill``。

    输出：可写入 ``SKILL.md`` JSON 代码块的基础类型字典。
    功能：将时间对象转为 ISO 格式，保留文档规定的触发、工作流、依赖、版本和质量字段。
    """

    payload = asdict(skill)
    payload["created_at"] = skill.created_at.isoformat()
    payload["updated_at"] = skill.updated_at.isoformat()
    return payload


def _skill_from_payload(payload: Mapping[str, Any]) -> SkillDefinition | None:
    """输入：从 ``SKILL.md`` JSON 代码块读取的字段 ``payload``。

    输出：合法的 ``SkillDefinition``；字段缺失或时间格式无效时返回 ``None``。
    功能：在加载旧文件时做防御式反序列化，让单个损坏 Skill 不影响其他工作流的匹配。
    """

    try:
        workflow = _workflow(payload.get("workflow"))
        required_tools = [step["tool"] for step in workflow]
        created_at = datetime.fromisoformat(str(payload["created_at"]))
        updated_at = datetime.fromisoformat(str(payload["updated_at"]))
        skill = SkillDefinition(
            skill_id=_text(payload["skill_id"], 100),
            name=_text(payload["name"], 120),
            description=_text(payload["description"], 500),
            version=max(1, int(payload["version"])),
            trigger_patterns=[
                _text(item, 300)
                for item in payload.get("trigger_patterns", [])
                if _text(item, 300)
            ],
            workflow=workflow,
            required_mcp_tools=list(dict.fromkeys(required_tools)),
            output_template=_text(payload.get("output_template"), 3000),
            created_at=created_at,
            updated_at=updated_at,
            usage_count=max(0, int(payload.get("usage_count", 0))),
            avg_quality_score=max(0.0, min(1.0, float(payload.get("avg_quality_score", 0.0)))),
        )
    except (KeyError, TypeError, ValueError):
        return None
    return skill if skill.skill_id and skill.name and skill.description and skill.workflow else None


class SkillEvolutionEngine:
    """按复杂度提取、去重、版本化并持久化自动 Skill。"""

    def __init__(self, llm: Any, skills_dir: Path, embedder: Any) -> None:
        """输入：宿主 LLM ``llm``、Skills Hub 目录 ``skills_dir`` 与 Embedding 客户端 ``embedder``。

        输出：初始化不执行外部调用的自进化引擎。
        功能：保存可替换的模型依赖，使执行轨迹评估、语义去重和文件存储可独立测试。
        """

        self._llm = llm
        self._skills_dir = skills_dir
        self._embedder = embedder

    async def evaluate_and_evolve(self, trace: ExecutionTrace) -> SkillDefinition | None:
        """输入：一次任务完成后的 ``ExecutionTrace``。

        输出：创建或更新后的 Skill；复杂度不足、模型拒绝或提取无效时返回 ``None``。
        功能：依次执行文档定义的复杂度门禁、LLM 抽象、语义去重和版本化落盘闭环。
        """

        if self._calc_complexity(trace) < EVOLUTION_COMPLEXITY_THRESHOLD:
            return None
        extracted = await self._extract_workflow(trace)
        if extracted is None:
            return None
        similar = await self._find_similar_skill(extracted, self.load_all_skills())
        if similar is not None and similar["similarity"] > SIMILARITY_THRESHOLD:
            skill = self._update_skill(similar["skill"], extracted, trace.user_query)
            logger.info("Skill 自进化已更新: id=%s version=%s", skill.skill_id, skill.version)
            return skill
        skill = self._create_skill(extracted, trace.user_query)
        logger.info("Skill 自进化已创建: id=%s version=%s", skill.skill_id, skill.version)
        return skill

    @staticmethod
    def _calc_complexity(trace: ExecutionTrace) -> float:
        """输入：包含轮次、MCP 工具数和耗时的 ``ExecutionTrace``。

        输出：范围为 0 到 1 的复杂度评分。
        功能：按文档的 30% 轮次、40% 工具数、30% 耗时权重判断流程是否值得沉淀。
        """

        return (
            min(len(trace.turns) / 5, 1.0) * 0.3
            + min(len(trace.mcp_tools_used) / 5, 1.0) * 0.4
            + min(max(trace.total_duration_seconds, 0.0) / 60, 1.0) * 0.3
        )

    async def _extract_workflow(self, trace: ExecutionTrace) -> dict[str, Any] | None:
        """输入：已通过复杂度门禁的 ``ExecutionTrace``。

        输出：经字段校验的可复用工作流；模型失败或不应沉淀时返回 ``None``。
        功能：把具体查询参数抽象成变量模板，要求模型只输出文档约定的 JSON 协议。
        """

        prompt = (
            "从以下 Agent 执行轨迹中提取可复用分析 Skill。具体业务值必须抽象为可替换参数，"
            "仅保留可复用的 MCP 工作流。若不通用或不值得复用，should_create_skill 必须为 false。\n"
            f"用户问题：{_text(trace.user_query, 1000)}\n"
            f"调用轮次：{len(trace.turns)}\n"
            f"MCP 调用：{json.dumps(trace.turns, ensure_ascii=False, default=str)[:6000]}\n"
            f"最终输出：{_text(trace.final_output, 1500)}\n"
            "只输出 JSON："
            '{"should_create_skill":true,"skill_name":"...","description":"...",'
            '"trigger_patterns":["..."],"workflow":[{"step":1,"tool":"...",'
            '"params":{"key":"{variable}"},"output_key":"result_1"}],"output_template":"..."}'
        )
        try:
            raw = _parse_json_object(await self._complete(prompt))
            return _extracted_skill(raw)
        except Exception:
            logger.exception("Skill 工作流提取失败")
            return None

    async def _complete(self, prompt: str) -> Any:
        """输入：要求返回 JSON 的 LLM 提示词 ``prompt``。

        输出：宿主模型返回的文本或兼容响应对象中的文本。
        功能：兼容 Hermes ``acomplete`` 与文档示例的 ``chat`` 两种 LLM 门面，不绑定某一模型 SDK。
        """

        if hasattr(self._llm, "acomplete"):
            result = await self._llm.acomplete(
                [{"role": "user", "content": prompt}],
                max_tokens=1200,
                timeout=20,
                purpose="suning_skill_evolution",
            )
            return getattr(result, "text", result)
        result = self._llm.chat(prompt)
        return await result if inspect.isawaitable(result) else result

    async def _find_similar_skill(
        self, extracted: Mapping[str, Any], existing_skills: list[SkillDefinition]
    ) -> dict[str, Any] | None:
        """输入：新提取字段 ``extracted`` 与已加载 Skill 列表 ``existing_skills``。

        输出：相似度最高的 Skill 及分数；没有已有 Skill 时返回 ``None``。
        功能：通过 Embedding 余弦相似度实现文档要求的语义去重，避免同类流程反复创建文件。
        """

        if not existing_skills:
            return None
        new_embedding = await self._embed(str(extracted["description"]))
        best: dict[str, Any] | None = None
        for skill in existing_skills:
            similarity = self._cosine_similarity(new_embedding, await self._embed(skill.description))
            if best is None or similarity > best["similarity"]:
                best = {"skill": skill, "similarity": similarity}
        return best

    async def _embed(self, text: str) -> list[float]:
        """输入：需要生成语义向量的非空描述 ``text``。

        输出：有限浮点数组成的 Embedding 向量；客户端返回非法向量时抛出 ``ValueError``。
        功能：兼容同步或异步 Embedding 客户端，并在余弦计算前阻断 NaN、空向量等无效结果。
        """

        result = self._embedder.embed(text)
        vector = await result if inspect.isawaitable(result) else result
        normalized = [float(value) for value in vector]
        if not normalized or not all(math.isfinite(value) for value in normalized):
            raise ValueError("Embedding 向量无效")
        return normalized

    @staticmethod
    def _cosine_similarity(a: list[float], b: list[float]) -> float:
        """输入：两个 Embedding 向量 ``a``、``b``。

        输出：范围为 -1 到 1 的余弦相似度；空向量或维度不同时返回 0。
        功能：用标准库数值计算比较工作流描述，不引入额外向量计算依赖。
        """

        if not a or len(a) != len(b):
            return 0.0
        norm_a = math.sqrt(sum(value * value for value in a))
        norm_b = math.sqrt(sum(value * value for value in b))
        return sum(left * right for left, right in zip(a, b)) / (norm_a * norm_b) if norm_a and norm_b else 0.0

    def _create_skill(self, extracted: Mapping[str, Any], user_query: str) -> SkillDefinition:
        """输入：已校验的自动提取字段 ``extracted`` 与原始用户问题 ``user_query``。

        输出：版本为 1 且已写入 Skills Hub 的新 Skill。
        功能：基于稳定名称哈希生成标识，保留原始问法为触发模式，并从工作流推导 MCP 依赖以供后续预检查。
        """

        now = datetime.now(timezone.utc)
        workflow = list(extracted["workflow"])
        skill = SkillDefinition(
            skill_id=hashlib.sha256(str(extracted["skill_name"]).encode()).hexdigest()[:12],
            name=str(extracted["skill_name"]),
            description=str(extracted["description"]),
            version=1,
            trigger_patterns=list(
                dict.fromkeys([*extracted["trigger_patterns"], _text(user_query, 300)])
            ),
            workflow=workflow,
            required_mcp_tools=list(dict.fromkeys(step["tool"] for step in workflow)),
            output_template=str(extracted["output_template"]),
            created_at=now,
            updated_at=now,
            usage_count=1,
        )
        self._write_skill_file(skill)
        return skill

    def _update_skill(
        self, skill: SkillDefinition, extracted: Mapping[str, Any], user_query: str
    ) -> SkillDefinition:
        """输入：语义重复的已有 ``skill``、新的已校验字段 ``extracted`` 与原始问题 ``user_query``。

        输出：版本递增且已重写到磁盘的 Skill。
        功能：合并新触发问法、替换改进后的工作流和输出模板，并同步刷新依赖工具集合。
        """

        skill.version += 1
        skill.updated_at = datetime.now(timezone.utc)
        skill.usage_count += 1
        skill.trigger_patterns = list(
            dict.fromkeys(
                [*skill.trigger_patterns, *extracted["trigger_patterns"], _text(user_query, 300)]
            )
        )
        skill.workflow = list(extracted["workflow"])
        skill.required_mcp_tools = list(dict.fromkeys(step["tool"] for step in skill.workflow))
        if extracted["output_template"]:
            skill.output_template = str(extracted["output_template"])
        self._write_skill_file(skill)
        return skill

    def _write_skill_file(self, skill: SkillDefinition) -> None:
        """输入：需要持久化的 ``SkillDefinition``。

        输出：在每个 Skill 独立目录写入可读且机器可加载的 ``SKILL.md``。
        功能：沿用 Hermes Markdown Skill 约定，并以内嵌 JSON 保存完整工作流而无需新增 YAML 依赖。
        """

        directory = self._skills_dir / skill.skill_id
        directory.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(_skill_payload(skill), ensure_ascii=False, indent=2, sort_keys=True)
        content = (
            f"---\nname: {skill.name}\ndescription: {skill.description}\n---\n\n"
            f"# {skill.name}\n\n{skill.description}\n\n"
            "此文件由 Skill 自进化引擎维护。模型匹配后按下列工作流执行，执行前必须检查依赖 MCP 工具可用。\n\n"
            "```json\n"
            f"{payload}\n"
            "```\n"
        )
        (directory / "SKILL.md").write_text(content, encoding="utf-8")

    def load_all_skills(self) -> list[SkillDefinition]:
        """输入：隐式读取配置的 Skills Hub 目录。

        输出：所有格式正确的自动沉淀 Skill，按标识排序。
        功能：忽略手工 Skill 或单个损坏文件，确保 Skills Hub 中一个异常不会阻断匹配和进化。
        """

        if not self._skills_dir.is_dir():
            return []
        skills: list[SkillDefinition] = []
        for path in sorted(self._skills_dir.glob("*/SKILL.md")):
            matched = _JSON_BLOCK_RE.search(path.read_text(encoding="utf-8"))
            if matched is None:
                continue
            try:
                payload = json.loads(matched.group(1))
            except json.JSONDecodeError:
                continue
            if isinstance(payload, Mapping) and (skill := _skill_from_payload(payload)) is not None:
                skills.append(skill)
        return skills

    def match(self, query: str, available_tools: set[str]) -> SkillDefinition | None:
        """输入：本轮用户问题 ``query`` 与当前可调用 MCP 工具集合 ``available_tools``。

        输出：触发模式命中且所有依赖可用的 Skill；无匹配或预检查失败时返回 ``None``。
        功能：在回答前落实文档的 Skill 预检查，工具变更时自动降级为从头推理而非注入旧流程。
        """

        normalized_query = _text(query, 1000).lower()
        if not normalized_query:
            return None
        for skill in self.load_all_skills():
            if not set(skill.required_mcp_tools).issubset(available_tools):
                continue
            if any(_pattern_matches(pattern, normalized_query) for pattern in skill.trigger_patterns):
                return skill
        return None


def _pattern_matches(pattern: str, query: str) -> bool:
    """输入：Skill 触发模式 ``pattern`` 和已归一化的小写用户问题 ``query``。

    输出：模式直接包含或带 ``{变量}`` 占位符匹配时返回 ``True``。
    功能：以极小的正则替换支持“分析{品类}退单原因”类可参数化触发语句，不增加第二个匹配模型调用。
    """

    normalized_pattern = _text(pattern, 300).lower()
    if not normalized_pattern:
        return False
    expression = re.escape(normalized_pattern)
    expression = re.sub(r"\\\{[^}]+\\\}", ".+", expression)
    if re.search(expression, query):
        return True
    pattern_terms = {term for term in _TRIGGER_TERMS if term in normalized_pattern}
    query_terms = {term for term in _TRIGGER_TERMS if term in query}
    return len(pattern_terms) >= 2 and pattern_terms.issubset(query_terms)


@dataclass
class _TrackedTurn:
    """回答前后 Hook 间短暂保存的当前轮轨迹。"""

    user_query: str
    started_at: float
    mcp_calls: list[dict[str, Any]] = field(default_factory=list)


class SkillEvolutionHooks:
    """把 Skill 匹配和异步沉淀接入 Hermes LLM、工具生命周期。"""

    def __init__(self, engine: SkillEvolutionEngine, available_tools: set[str]) -> None:
        """输入：已装配的自进化 ``engine`` 与允许调用的 MCP 工具 ``available_tools``。

        输出：初始化可注册的 Hook 对象，不读取文件或调用模型。
        功能：保存一轮内的工具轨迹，并限制自动 Skill 只能引用插件实际暴露的工具。
        """

        self._engine = engine
        self._available_tools = set(available_tools)
        self._turns: dict[tuple[str, str], _TrackedTurn] = {}
        self._lock = threading.RLock()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._executor = ThreadPoolExecutor(
            max_workers=1,
            thread_name_prefix="suning-skill-evolution",
        )
        self._futures: set[Future[Any]] = set()

    def pre_llm_call(
        self, *, session_id: str = "", turn_id: str = "", user_message: Any = "", **_kwargs: Any
    ) -> dict[str, str] | None:
        """输入：逻辑会话、轮次、用户消息及其他 Hermes 生命周期字段。

        输出：命中且预检查通过的 Skill 执行上下文；未命中或工具不可用时返回 ``None``。
        功能：开始采集本轮轨迹，并让 Agent 直接加载验证过的工作流；失效 Skill 自动降级。
        """

        session = _text(session_id, 200)
        turn = _text(turn_id, 200) or "current"
        query = _text(user_message, 2000)
        if session and query:
            with self._lock:
                self._turns[(session, turn)] = _TrackedTurn(query, time.monotonic())
                while len(self._turns) > MAX_TRACKED_TURNS:
                    self._turns.pop(next(iter(self._turns)))
        skill = self._engine.match(query, self._available_tools)
        if skill is None:
            return None
        workflow = json.dumps(skill.workflow, ensure_ascii=False)
        return {
            "context": (
                f"已匹配并通过 MCP 依赖预检查的 Skill：{skill.name}（v{skill.version}）。\n"
                f"按以下工作流顺序执行；将 {{变量}} 替换为本轮问题的值，只使用列出的工具：{workflow}\n"
                f"输出格式：{skill.output_template or '基于各步骤结果给出结论和数据依据。'}"
            )
        }

    def pre_tool_call(
        self, *, session_id: str = "", turn_id: str = "", tool_name: str = "", arguments: Any = None,
        args: Any = None, **_kwargs: Any
    ) -> None:
        """输入：逻辑会话、轮次、工具名、参数及其他 Hermes 工具生命周期字段。

        输出：无；在当前轮轨迹中追加受限工具调用摘要。
        功能：记录实际使用的 MCP 名和参数结构，供回答完成后的 LLM 抽象可复用步骤。
        """

        session = _text(session_id, 200)
        turn = _text(turn_id, 200) or "current"
        if not session or not _text(tool_name, 100):
            return
        raw_args = arguments if isinstance(arguments, Mapping) else args
        params = dict(raw_args) if isinstance(raw_args, Mapping) else {}
        with self._lock:
            tracked = self._turns.get((session, turn))
            if tracked is not None:
                tracked.mcp_calls.append({"tool": _text(tool_name, 100), "params": params})

    def post_llm_call(
        self, *, session_id: str = "", turn_id: str = "", assistant_response: Any = "", **_kwargs: Any
    ) -> None:
        """输入：逻辑会话、轮次、最终回答及其他 Hermes 生命周期字段。

        输出：无；符合复杂度门禁时在后台提交自进化任务。
        功能：结束轨迹采集且绝不阻塞用户回复，模型或磁盘失败由后台任务隔离记录。
        """

        session = _text(session_id, 200)
        turn = _text(turn_id, 200) or "current"
        with self._lock:
            tracked = self._turns.pop((session, turn), None)
        if tracked is None:
            return
        trace = ExecutionTrace(
            user_query=tracked.user_query,
            turns=[{"user_msg": tracked.user_query, "mcp_calls": tracked.mcp_calls, "result_summary": _text(assistant_response, 1500)}],
            mcp_tools_used=[call["tool"] for call in tracked.mcp_calls],
            total_duration_seconds=time.monotonic() - tracked.started_at,
            final_output=_text(assistant_response, 3000),
        )
        if self._engine._calc_complexity(trace) < EVOLUTION_COMPLEXITY_THRESHOLD:
            return
        try:
            task = asyncio.get_running_loop().create_task(self._evolve(trace))
        except RuntimeError:
            future = self._executor.submit(self._evolve_sync, trace)
            self._futures.add(future)
            future.add_done_callback(self._futures.discard)
            return
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def _evolve(self, trace: ExecutionTrace) -> None:
        """输入：已经通过复杂度门禁的 ``ExecutionTrace``。

        输出：无；创建、更新或跳过 Skill，故障只记录日志。
        功能：作为后台任务边界隔离 LLM、Embedding 和文件系统异常，保证正常对话生命周期可靠完成。
        """

        try:
            await self._engine.evaluate_and_evolve(trace)
        except Exception:
            logger.exception("Skill 自进化失败")

    def _evolve_sync(self, trace: ExecutionTrace) -> None:
        """输入：已经通过复杂度门禁的 ``ExecutionTrace``。

        输出：无；在独立线程的事件循环中完成自进化任务。
        功能：兼容 Hermes 同步 ``post_llm_call`` 回调，避免因当前线程没有运行 asyncio loop 而丢失沉淀。
        """

        asyncio.run(self._evolve(trace))


def build_skill_evolution_hooks(llm: Any, available_tools: set[str]) -> SkillEvolutionHooks:
    """输入：宿主 LLM ``llm`` 与插件实际暴露的 MCP 工具集合 ``available_tools``。

    输出：使用 DashScope Embedding 的 ``SkillEvolutionHooks``；禁用或缺少密钥时抛出 ``RuntimeError``。
    功能：从环境变量装配自动 Skill 存储和语义去重依赖，使插件可按配置独立启用此闭环。
    """

    if os.getenv("SKILL_EVOLUTION_ENABLED", "true").strip().lower() in {"0", "false", "no"}:
        raise RuntimeError("Skill 自进化已禁用")
    from suning_context_runtime import DashScopeEmbeddingClient

    embedder = DashScopeEmbeddingClient(
        api_key=os.getenv("DASHSCOPE_API_KEY", ""),
        model=os.getenv("SKILL_EVOLUTION_EMBEDDING_MODEL", "text-embedding-v3"),
        timeout_seconds=float(os.getenv("SKILL_EVOLUTION_EMBEDDING_TIMEOUT_SECONDS", "10")),
    )
    configured_dir = os.getenv("SKILL_EVOLUTION_DIR", "").strip()
    skills_dir = (
        Path(configured_dir).expanduser()
        if configured_dir
        else Path(__file__).resolve().parents[2] / "skills" / "evolved"
    )
    return SkillEvolutionHooks(SkillEvolutionEngine(llm, skills_dir, embedder), available_tools)


__all__ = [
    "EVOLUTION_COMPLEXITY_THRESHOLD",
    "ExecutionTrace",
    "SIMILARITY_THRESHOLD",
    "SkillDefinition",
    "SkillEvolutionEngine",
    "SkillEvolutionHooks",
    "build_skill_evolution_hooks",
]
