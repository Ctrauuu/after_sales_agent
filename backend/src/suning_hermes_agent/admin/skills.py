"""Agent Skill Hub 的查询、编辑、启停和受限删除路由。"""

from __future__ import annotations

import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from . import runtime
from .common import ApiError, _query_int, _request_payload
from .runtime import LOCAL_TIMEZONE


router = APIRouter(prefix="/api/admin/skills")
_SKILL_JSON_RE = re.compile(r"```json\s*(\{.*?\})\s*```", re.DOTALL)


def _read_skill(path: Path) -> tuple[str, dict[str, Any]] | None:
    """输入：一个 ``SKILL.md`` 路径 ``path``。

    输出：原文和内嵌 JSON；格式无效时返回 ``None``。
    功能：读取 Agent 自进化 Skill 的真实持久化格式。
    """

    try:
        content = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = _SKILL_JSON_RE.search(content)
    if match is None:
        return None
    try:
        payload = json.loads(match.group(1))
    except json.JSONDecodeError:
        return None
    return (content, payload) if isinstance(payload, dict) else None


def _skill_records() -> list[tuple[Path, str, dict[str, Any]]]:
    """输入：无；读取 Skills Hub 目录。

    输出：按 ID 排序的 ``(路径, 原文, payload)`` 数组。
    功能：忽略损坏文件，让单个 Skill 不阻断管理列表。
    """

    records: list[tuple[Path, str, dict[str, Any]]] = []
    if not runtime.SKILLS_DIR.is_dir():
        return records
    for path in sorted(runtime.SKILLS_DIR.glob("*/SKILL.md")):
        parsed = _read_skill(path)
        if parsed is not None:
            records.append((path, parsed[0], parsed[1]))
    return records


def _skill_score(payload: Mapping[str, Any]) -> int:
    """输入：Skill JSON ``payload``。

    输出：0 至 100 的整数质量评分。
    功能：兼容 Agent 内部 0-1 分数和管理数据的百分制。
    """

    try:
        value = float(payload.get("avg_quality_score", 0))
    except (TypeError, ValueError):
        value = 0.0
    return round(max(0.0, min(value * 100 if value <= 1 else value, 100.0)))


def _skill_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    """输入：Skill JSON ``payload``。

    输出：Skill 管理卡片字段字典。
    功能：从 Agent 原生字段转换类型、状态、触发模式、使用次数和质量分。
    """

    return {
        "id": str(payload.get("skill_id") or ""),
        "name": str(payload.get("name") or ""),
        "description": str(payload.get("description") or ""),
        "type": str(payload.get("type") or "auto"),
        "version": str(payload.get("version") or 1),
        "enabled": bool(payload.get("enabled", True)),
        "usage_count": int(payload.get("usage_count") or 0),
        "quality_score": _skill_score(payload),
        "trigger_patterns": [str(item) for item in payload.get("trigger_patterns", [])],
    }


def _skill_detail(payload: Mapping[str, Any]) -> dict[str, Any]:
    """输入：Skill JSON ``payload``。

    输出：卡片字段加工作流、模板、趋势和版本历史的详情字典。
    功能：将真实工作流参数转换为抽屉可读步骤，无额外统计库时展示当前质量快照。
    """

    result = _skill_summary(payload)
    steps = []
    for index, step in enumerate(payload.get("workflow", []), start=1):
        if not isinstance(step, dict):
            continue
        params = step.get("params") if isinstance(step.get("params"), dict) else {}
        description = f"参数：{json.dumps(params, ensure_ascii=False)}" if params else f"输出：{step.get('output_key') or '结果'}"
        steps.append({"order": int(step.get("step") or index), "tool": str(step.get("tool") or ""), "description": description})
    score = result["quality_score"]
    updated = str(payload.get("updated_at") or payload.get("created_at") or "")
    date = updated[5:10] if len(updated) >= 10 else "当前"
    result.update(
        {
            "workflow_steps": steps,
            "output_template": str(payload.get("output_template") or ""),
            "score_trend": [{"date": date, "score": score}],
            "version_history": [
                {"version": str(payload.get("version") or 1), "created_at": updated[:10], "note": "当前生效版本"}
            ],
        }
    )
    return result


def _find_skill(skill_id: str) -> tuple[Path, str, dict[str, Any]]:
    """输入：Skill ID ``skill_id``。

    输出：匹配的路径、原文和 JSON；不存在时抛出 404。
    功能：通过已解析 payload 匹配标识，避免把请求路径直接拼接到文件系统。
    """

    for path, content, payload in _skill_records():
        if str(payload.get("skill_id")) == skill_id:
            return path, content, payload
    raise ApiError(404, "Skill 不存在")


def _write_skill(path: Path, content: str, payload: Mapping[str, Any]) -> None:
    """输入：Skill 路径、原文和更新后 JSON ``payload``。

    输出：无；以原子替换写回 ``SKILL.md``，失败时保留旧文件。
    功能：仅替换内嵌 JSON，保留 Agent Skill 的 Markdown 描述和 frontmatter。
    """

    replacement = "```json\n" + json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n```"
    updated = _SKILL_JSON_RE.sub(replacement, content, count=1)
    temporary_path: str | None = None
    try:
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as handle:
            handle.write(updated)
            temporary_path = handle.name
        os.replace(temporary_path, path)
    finally:
        if temporary_path and os.path.exists(temporary_path):
            os.unlink(temporary_path)


@router.get("")
def list_skills(request: Request) -> JSONResponse:
    """输入：类型、状态、关键词和分页请求。

    输出：真实 Skill Hub 的分页卡片 JSON。
    功能：读取 Agent 已沉淀工作流并支持管理页筛选。
    """

    params = request.query_params
    items = []
    for _, _, payload in _skill_records():
        item = _skill_summary(payload)
        if params.get("type") and item["type"] != params["type"]:
            continue
        if params.get("status") == "enabled" and not item["enabled"]:
            continue
        if params.get("status") == "disabled" and item["enabled"]:
            continue
        if params.get("keyword") and params["keyword"].lower() not in item["name"].lower():
            continue
        items.append(item)
    page = _query_int(request, "page", 1, 1, 100000)
    size = _query_int(request, "size", 12, 1, 100)
    start = (page - 1) * size
    return JSONResponse({"items": items[start : start + size], "total": len(items)})


@router.get("/{skill_id}")
def skill_detail(request: Request) -> JSONResponse:
    """输入：路径 Skill ID。

    输出：真实 Skill 工作流详情 JSON。
    功能：展示触发模式、步骤、输出模板、版本和质量快照。
    """

    _, _, payload = _find_skill(request.path_params["skill_id"])
    return JSONResponse(_skill_detail(payload))


@router.put("/{skill_id}")
async def update_skill(request: Request) -> JSONResponse:
    """输入：路径 Skill ID 与触发模式 JSON。

    输出：更新后的触发模式 JSON。
    功能：原子写回 Agent 实际读取的 Skill 文件，使新模式下一轮立即生效。
    """

    skill_id = request.path_params["skill_id"]
    body = await _request_payload(request)
    patterns = body.get("trigger_patterns") if isinstance(body, dict) else None
    if not isinstance(patterns, list):
        raise ApiError(400, "trigger_patterns 必须是数组")
    normalized = list(dict.fromkeys(str(item).strip() for item in patterns if str(item).strip()))
    if not normalized:
        raise ApiError(400, "至少保留一条触发模式")
    path, content, payload = _find_skill(skill_id)
    payload["trigger_patterns"] = normalized
    payload["updated_at"] = datetime.now(LOCAL_TIMEZONE).isoformat()
    _write_skill(path, content, payload)
    return JSONResponse({"id": skill_id, "trigger_patterns": normalized})


@router.put("/{skill_id}/status")
async def update_skill_status(request: Request) -> JSONResponse:
    """输入：路径 Skill ID 与 ``enabled`` 布尔 JSON。

    输出：更新后的 Skill 状态 JSON。
    功能：写回 Agent Skill 文件，供语义路由和模式匹配跳过禁用工作流。
    """

    skill_id = request.path_params["skill_id"]
    body = await _request_payload(request)
    if not isinstance(body, dict) or not isinstance(body.get("enabled"), bool):
        raise ApiError(400, "enabled 必须是布尔值")
    path, content, payload = _find_skill(skill_id)
    payload["enabled"] = body["enabled"]
    payload["updated_at"] = datetime.now(LOCAL_TIMEZONE).isoformat()
    _write_skill(path, content, payload)
    return JSONResponse({"id": skill_id, "enabled": body["enabled"]})


@router.delete("/{skill_id}")
def delete_skill(request: Request) -> JSONResponse:
    """输入：路径 Skill ID。

    输出：手动 Skill 删除确认 JSON；自动 Skill 返回 403。
    功能：遵守页面限制，仅删除明确标记为 ``manual`` 的单个 Skill 文件和空目录。
    """

    path, _, payload = _find_skill(request.path_params["skill_id"])
    if str(payload.get("type") or "auto") != "manual":
        raise ApiError(403, "自动创建的 Skill 不可删除，可选择禁用")
    path.unlink()
    try:
        path.parent.rmdir()
    except OSError:
        pass
    return JSONResponse({"deleted": True})

