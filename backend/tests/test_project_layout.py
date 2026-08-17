from pathlib import Path


BACKEND_ROOT = Path(__file__).resolve().parents[1]
PROJECT_ROOT = BACKEND_ROOT.parent


def test_backend_runtime_files_are_colocated() -> None:
    """输入：无显式参数；由测试构造固定场景。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：验证后端、跨系统订单追踪、共享上下文、长期记忆插件/Skill 及基础设施文件位于约定目录。
    """
    assert (BACKEND_ROOT / "pyproject.toml").is_file()
    assert (BACKEND_ROOT / ".env.example").is_file()
    assert (BACKEND_ROOT / "mcp_suning").is_dir()
    assert (
        BACKEND_ROOT
        / "src"
        / "suning_hermes_agent"
        / "conversation_context.py"
    ).is_file()
    assert (BACKEND_ROOT / "infra" / "mysql" / "compose.yaml").is_file()
    assert (
        PROJECT_ROOT / "packages" / "suning-context-runtime" / "pyproject.toml"
    ).is_file()
    assert (
        PROJECT_ROOT
        / "packages"
        / "suning-context-runtime"
        / "src"
        / "suning_context_runtime"
        / "context.py"
    ).is_file()
    runtime_root = (
        PROJECT_ROOT
        / "packages"
        / "suning-context-runtime"
        / "src"
        / "suning_context_runtime"
    )
    for filename in (
        "long_memory_models.py",
        "long_memory_store.py",
        "long_memory_extractor.py",
        "long_memory_retriever.py",
        "long_memory_pipeline.py",
        "knowledge_rag.py",
    ):
        assert (runtime_root / filename).is_file()
    assert (
        PROJECT_ROOT
        / ".hermes"
        / "plugins"
        / "suning-rbac-bridge"
        / "memory_hooks.py"
    ).is_file()
    assert (
        PROJECT_ROOT
        / ".hermes"
        / "plugins"
        / "suning-rbac-bridge"
        / "knowledge_hooks.py"
    ).is_file()
    assert (BACKEND_ROOT / "scripts" / "ingest_knowledge.py").is_file()
    assert (
        PROJECT_ROOT
        / ".hermes"
        / "skills"
        / "memory-extractor"
        / "SKILL.md"
    ).is_file()
    assert (BACKEND_ROOT / "infra" / "milvus" / "compose.yaml").is_file()
    assert (BACKEND_ROOT / "mcp_suning" / "security" / "attestation.py").is_file()
    assert (BACKEND_ROOT / "mcp_suning" / "security" / "rbac.py").is_file()
    assert (BACKEND_ROOT / "mcp_suning" / "timeline" / "tracker.py").is_file()
    assert (BACKEND_ROOT / "mcp_suning" / "timeline" / "gateway.py").is_file()
    assert (BACKEND_ROOT / "mcp_suning" / "servers" / "timeline.py").is_file()
    assert (
        BACKEND_ROOT
        / "infra"
        / "mysql"
        / "migrations"
        / "004_register_order_timeline.sql"
    ).is_file()
