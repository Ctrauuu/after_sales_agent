"""保证项目中的每个 Python 函数持续遵循统一注释规范。"""

import ast
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
SOURCE_ROOTS = (
    PROJECT_ROOT / ".hermes" / "plugins",
    PROJECT_ROOT / "backend" / "mcp_suning",
    PROJECT_ROOT / "backend" / "nl2sql",
    PROJECT_ROOT / "backend" / "src",
    PROJECT_ROOT / "backend" / "tests",
    PROJECT_ROOT / "packages" / "suning-context-runtime" / "src",
)
REQUIRED_SECTIONS = ("输入", "输出", "功能")


def _python_files() -> list[Path]:
    """输入：无；使用模块级 ``SOURCE_ROOTS`` 作为扫描范围。

    输出：排序后的项目 Python 源文件路径列表。
    功能：收集插件、运行时代码和测试代码，并排除缓存与虚拟环境文件。
    """

    return sorted(
        path
        for root in SOURCE_ROOTS
        for path in root.rglob("*.py")
        if ".venv" not in path.parts and "__pycache__" not in path.parts
    )


def test_every_python_function_documents_input_output_and_purpose() -> None:
    """输入：无；读取 ``_python_files`` 返回的项目源码。

    输出：无；缺少 docstring 必需章节时通过断言报告具体文件、函数和行号。
    功能：强制所有函数持续记录输入、输出和功能，约束后续代码实现。
    """

    violations: list[str] = []
    for path in _python_files():
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            docstring = ast.get_docstring(node) or ""
            missing = [section for section in REQUIRED_SECTIONS if section not in docstring]
            if missing:
                relative_path = path.relative_to(PROJECT_ROOT)
                violations.append(
                    f"{relative_path}:{node.lineno} {node.name} 缺少 {', '.join(missing)}"
                )

    assert not violations, "函数注释不完整：\n" + "\n".join(violations)
