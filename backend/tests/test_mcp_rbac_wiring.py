import ast
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


MCP_ROOT = Path(__file__).resolve().parents[1] / "mcp_suning" / "servers"
PROJECT_ROOT = Path(__file__).resolve().parents[2]
BRIDGE_ROOT = PROJECT_ROOT / ".hermes" / "plugins" / "suning-rbac-bridge"
MCP_FILES = (
    "order.py",
    "aftersale.py",
    "product.py",
    "logistics.py",
    "payment.py",
)
TIMELINE_MCP_FILE = "timeline.py"

# 该映射直接对应 MySQL mcp_tool_registry 当前 is_enabled=1 的记录。测试会在
# 代码层阻止未登记工具被意外暴露，也能在数据库名称调整后明确提示同步修改。
DATABASE_TOOL_NAMES = {
    "order.py": {"search_orders", "get_order_detail"},
    "aftersale.py": {
        "get_aftersale_workflow",
        "query_aftersale_nl2sql",
        "query_return_stats_nl2sql",
    },
    "product.py": {"get_product_info"},
    "logistics.py": {"query_logistics"},
    "payment.py": {"get_refund_status"},
    "timeline.py": {"trace_order_timeline"},
}


FunctionNode = ast.FunctionDef | ast.AsyncFunctionDef


def _call_name(call: ast.Call) -> str:
    """输入：参数 ``call``。

    输出：返回类型为 ``str`` 的测试数据或测试替身结果。
    功能：把 AST 调用节点转换为便于比较的完整函数名。
    """
    def name(node: ast.expr) -> str:
        """输入：参数 ``node``。

        输出：返回类型为 ``str`` 的测试数据或测试替身结果。
        功能：递归拼接 AST Name 与 Attribute 节点的点分名称。
        """
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Attribute):
            parent = name(node.value)
            return f"{parent}.{node.attr}" if parent else node.attr
        return ""

    return name(call.func)


def _calls(function: FunctionNode) -> list[ast.Call]:
    """输入：参数 ``function``。

    输出：返回类型为 ``list[ast.Call]`` 的测试数据或测试替身结果。
    功能：收集一个函数及其子节点中的全部调用表达式。
    """
    return [node for node in ast.walk(function) if isinstance(node, ast.Call)]


def _tool_functions(tree: ast.Module) -> list[FunctionNode]:
    """输入：参数 ``tree``。

    输出：返回类型为 ``list[FunctionNode]`` 的测试数据或测试替身结果。
    功能：从模块 AST 中找出使用 ``mcp.tool`` 装饰的公开工具函数。
    """
    return [
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and any(
            isinstance(decorator, ast.Call)
            and isinstance(decorator.func, ast.Attribute)
            and decorator.func.attr == "tool"
            for decorator in node.decorator_list
        )
    ]


def _reachable_functions(
    root: FunctionNode,
    functions: dict[str, FunctionNode],
) -> list[FunctionNode]:
    """输入：参数 ``root``、``functions``。

    输出：返回类型为 ``list[FunctionNode]`` 的测试数据或测试替身结果。
    功能：沿模块内函数调用关系收集从根函数可到达的实现函数。
    """
    reachable: list[FunctionNode] = []
    pending = [root]
    seen: set[str] = set()
    while pending:
        function = pending.pop()
        if function.name in seen:
            continue
        seen.add(function.name)
        reachable.append(function)
        for call in _calls(function):
            called = functions.get(_call_name(call))
            if called is not None:
                pending.append(called)
    return reachable


def _uses_safe_data_scope(call: ast.Call) -> bool:
    """输入：参数 ``call``。

    输出：返回类型为 ``bool`` 的测试数据或测试替身结果。
    功能：确认脱敏调用使用服务端 ``safe_filters`` 决定数据粒度。
    """
    if len(call.args) < 2:
        return False
    scope = call.args[1]
    return (
        isinstance(scope, ast.Subscript)
        and isinstance(scope.value, ast.Name)
        and scope.value.id == "safe_filters"
        and isinstance(scope.slice, ast.Constant)
        and scope.slice.value == "data_scope"
    )


def _manifest_tool_names() -> set[str]:
    """输入：项目内 Hermes 桥接插件的 ``plugin.yaml``。

    输出：清单中 ``provides_tools`` 声明的工具名集合。
    功能：读取简单 YAML 列表，供测试核对插件声明与实际注册表是否一致。
    """

    names: set[str] = set()
    in_tools = False
    for line in (BRIDGE_ROOT / "plugin.yaml").read_text(encoding="utf-8").splitlines():
        if line == "provides_tools:":
            in_tools = True
            continue
        if in_tools and line.startswith("  - "):
            names.add(line.removeprefix("  - ").strip())
            continue
        if in_tools and line and not line.startswith(" "):
            break
    return names


def _mcp_server_tool_names() -> set[str]:
    """输入：当前六个 MCP Server 源文件。

    输出：所有 ``@mcp.tool`` 公开函数的名称集合。
    功能：复用现有 AST 工具发现逻辑，为桥接策略一致性测试提供真实名称。
    """

    return {
        function.name
        for filename in (*MCP_FILES, TIMELINE_MCP_FILE)
        for function in _tool_functions(
            ast.parse((MCP_ROOT / filename).read_text(encoding="utf-8"))
        )
    }


def _load_bridge_schemas(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """输入：pytest ``monkeypatch`` 和桥接插件目录。

    输出：保留相对导入语义加载的真实 ``schemas`` 模块。
    功能：构造临时插件 package，避免 ``run_path`` 无法解析 resilience 模块。
    """

    package_name = "_suning_rbac_bridge_wiring_test"
    package = ModuleType(package_name)
    package.__path__ = [str(BRIDGE_ROOT)]  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, package_name, package)

    module_name = f"{package_name}.schemas"
    spec = importlib.util.spec_from_file_location(
        module_name,
        BRIDGE_ROOT / "schemas.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("filename", MCP_FILES)
def test_every_public_mcp_tool_calls_authorization_before_database(filename: str) -> None:
    """输入：参数 ``filename``。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：鉴权必须先于任何直接或辅助函数中的数据库访问。
    """

    tree = ast.parse((MCP_ROOT / filename).read_text(encoding="utf-8"))
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    tool_functions = _tool_functions(tree)

    assert tool_functions, f"{filename} 中没有发现 MCP tool"
    for function in tool_functions:
        direct_calls = _calls(function)
        auth_calls = [
            call
            for call in direct_calls
            if _call_name(call) == "authorize_mcp_request"
        ]
        assert auth_calls, f"{filename}:{function.name} 未调用 authorize_mcp_request"

        database_boundary_lines: list[int] = []
        for call in direct_calls:
            called_name = _call_name(call)
            if called_name in {
                "engine.connect",
                "nl2sql_lite_pipeline.run",
                "nl2sql_pipeline.run",
            }:
                database_boundary_lines.append(call.lineno)
                continue
            called = functions.get(called_name)
            if called is None:
                continue
            reachable = _reachable_functions(called, functions)
            if any(
                _call_name(reachable_call) == "engine.connect"
                for reachable_function in reachable
                for reachable_call in _calls(reachable_function)
            ):
                database_boundary_lines.append(call.lineno)

        assert database_boundary_lines, (
            f"{filename}:{function.name} 未发现数据库访问边界"
        )
        assert min(call.lineno for call in auth_calls) < min(database_boundary_lines), (
            f"{filename}:{function.name} 在鉴权前访问数据库"
        )

        reachable = _reachable_functions(function, functions)
        reachable_calls = [
            call
            for reachable_function in reachable
            for call in _calls(reachable_function)
        ]
        scope_calls = [
            call
            for call in reachable_calls
            if _call_name(call) == "build_scope_clause"
        ]
        delegated_scope_calls = [
            call
            for call in reachable_calls
            if _call_name(call) in {
                "nl2sql_lite_pipeline.run",
                "nl2sql_pipeline.run",
            }
        ]
        assert scope_calls or delegated_scope_calls, (
            f"{filename}:{function.name} 未注入 SQL 行级权限"
        )
        if scope_calls:
            assert all(
                call.args
                and isinstance(call.args[0], ast.Name)
                and call.args[0].id == "safe_filters"
                for call in scope_calls
            ), f"{filename}:{function.name} SQL 范围未使用 safe_filters"
        if delegated_scope_calls:
            assert all(
                len(call.args) >= 2
                and isinstance(call.args[1], ast.Name)
                and call.args[1].id == "safe_filters"
                for call in delegated_scope_calls
            ), f"{filename}:{function.name} NL2SQL 未使用 safe_filters"

        mask_calls = [
            call
            for call in direct_calls
            if _call_name(call) == "interceptor.mask_sensitive_data"
        ]
        assert mask_calls, f"{filename}:{function.name} 返回值未脱敏"
        assert all(_uses_safe_data_scope(call) for call in mask_calls), (
            f"{filename}:{function.name} 脱敏粒度未使用 safe_filters"
        )


@pytest.mark.parametrize("filename", MCP_FILES)
def test_exposed_tool_names_match_database_registry(filename: str) -> None:
    """输入：参数 ``filename``。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：MCP 只暴露数据库登记且启用的工具名称。
    """

    tree = ast.parse((MCP_ROOT / filename).read_text(encoding="utf-8"))
    exposed_names = {node.name for node in _tool_functions(tree)}

    assert exposed_names == DATABASE_TOOL_NAMES[filename]


def test_hermes_bridge_tool_names_match_mcp_servers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """输入：当前 MCP Server、插件清单、schema 和 ``monkeypatch``。

    输出：无；名称或 resilience 策略不一致时由 pytest 报告失败。
    功能：实现 T22，确保 MCP Tool、桥接清单与逐项调用策略完整一致。
    """

    server_tool_names = _mcp_server_tool_names()
    schemas = _load_bridge_schemas(monkeypatch)
    tool_specs = schemas.TOOL_SPECS
    expected_policies = {
        "search_orders": ("mcp-order", "L2_CORE", True, True),
        "get_order_detail": ("mcp-order", "L2_CORE", True, False),
        "query_return_stats_nl2sql": ("mcp-aftersale", "L2_CORE", True, True),
        "query_aftersale_nl2sql": ("mcp-aftersale", "L2_CORE", True, True),
        "get_aftersale_workflow": (
            "mcp-aftersale",
            "L1_NON_CRITICAL",
            True,
            True,
        ),
        "get_product_info": ("mcp-product", "L1_NON_CRITICAL", True, False),
        "query_logistics": ("mcp-logistics", "L1_NON_CRITICAL", True, True),
        "get_refund_status": ("mcp-payment", "L2_CORE", True, False),
        "trace_order_timeline": (
            "mcp-order-timeline",
            "L2_CORE",
            True,
            False,
        ),
    }
    actual_policies = {
        name: (
            tool_spec.server_id,
            tool_spec.degrade_level.value,
            tool_spec.retry_on_timeout,
            tool_spec.empty_result_is_success,
        )
        for name, tool_spec in tool_specs.items()
    }

    assert server_tool_names == set().union(*DATABASE_TOOL_NAMES.values())
    assert set(tool_specs) == server_tool_names
    assert _manifest_tool_names() == server_tool_names | {
        "send_aftersale_chart",
        "orchestrate_aftersale_analysis",
    }
    assert actual_policies == expected_policies
    assert "query_return_stats" not in tool_specs


def test_order_timeline_aggregator_authenticates_and_masks_result() -> None:
    """输入：订单全链路聚合 MCP 的模块 AST。

    输出：无；断言失败时由 pytest 报告顶层鉴权或脱敏调用缺失。
    功能：验证不直接访问数据库的聚合工具仍先验证主体，并将结果交给现有数据粒度脱敏器。
    """

    tree = ast.parse((MCP_ROOT / TIMELINE_MCP_FILE).read_text(encoding="utf-8"))
    tools = _tool_functions(tree)
    assert {tool.name for tool in tools} == DATABASE_TOOL_NAMES[TIMELINE_MCP_FILE]
    tool = tools[0]
    calls = _calls(tool)
    assert any(
        _call_name(call) == "authorize_mcp_request_with_principal" for call in calls
    )
    mask_calls = [
        call
        for call in calls
        if _call_name(call) == "interceptor.mask_sensitive_data"
    ]
    assert len(mask_calls) == 1
    assert _uses_safe_data_scope(mask_calls[0])


@pytest.mark.parametrize("filename", MCP_FILES)
def test_mcp_tools_do_not_expose_model_controlled_user_context(filename: str) -> None:
    """输入：参数 ``filename``。

    输出：无；断言失败时由 pytest 报告测试失败。
    功能：身份只能来自 FastMCP Context，不能重新暴露为 LLM 可填写的参数。
    """

    tree = ast.parse((MCP_ROOT / filename).read_text(encoding="utf-8"))
    for function in (
        node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ):
        argument_names = {
            argument.arg
            for argument in (*function.args.posonlyargs, *function.args.args)
        }
        assert "user_context" not in argument_names, (
            f"{filename}:{function.name} 暴露了可伪造的 user_context"
        )


def test_order_timeline_does_not_expose_model_controlled_user_context() -> None:
    """输入：订单全链路聚合 MCP 的模块 AST。

    输出：无；断言失败时由 pytest 报告可由模型伪造的身份参数。
    功能：锁定聚合工具只接收订单业务标识，用户身份继续完全来自 MCP 请求上下文。
    """

    tree = ast.parse((MCP_ROOT / TIMELINE_MCP_FILE).read_text(encoding="utf-8"))
    tool = _tool_functions(tree)[0]
    argument_names = {
        argument.arg
        for argument in (*tool.args.posonlyargs, *tool.args.args)
    }
    assert "user_context" not in argument_names
