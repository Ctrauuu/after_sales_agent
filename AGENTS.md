# 项目实现约束

## 函数注释

所有新增或修改的函数、异步函数、方法、闭包和测试函数都必须在函数体第一行提供
docstring（前端函数使用等价的 JSDoc）。注释必须真实描述当前实现，并明确包含：

- `输入`：列出参数、隐式上下文或外部数据来源；无参数也要写明。
- `输出`：写明返回值、原地副作用或可能抛出的关键异常。
- `功能`：说明函数承担的职责和关键处理，不重复函数名。

Python 统一使用以下格式：

```python
def example(value: str) -> int:
    """输入：待处理字符串 ``value``。

    输出：字符串解析后的整数；格式无效时抛出 ``ValueError``。
    功能：校验并转换业务输入，供后续计算使用。
    """
```

修改函数行为、参数或返回值时，必须同步修改 docstring。不得用“执行对应逻辑”、
“处理数据”等无法说明实际职责的占位描述。运行时代码和测试代码都适用此规则。

提交前运行：

```bash
cd backend
uv run pytest
```

`tests/test_function_documentation.py` 会阻止缺少 `输入`、`输出` 或 `功能` 的
Python 函数进入项目。

## Code simplicity

Prefer simple, explicit implementations.

- Do not introduce abstraction unless it removes real duplication.
- Prefer functions over classes when no persistent state is required.
- Avoid unnecessary Factory / Manager / Handler / Registry patterns.
- Avoid wrappers that only forward parameters.
- Keep call chains short.
- Prefer existing project structure over new generic frameworks.
- Do not create "future-proof" abstractions without a current requirement.
- Keep business logic easy to trace from entry point to data access.