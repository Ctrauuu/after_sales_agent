"""通过 LangChain DeepSeek 模型生成候选 SQL。"""

from langchain_core.language_models.chat_models import BaseChatModel

from suning_context_runtime.model import create_model, invoke_model_text


class DeepSeekSQLGenerator:
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str = "https://api.deepseek.com",
        model: str = "deepseek-v4-flash",
        timeout_seconds: float = 30.0,
        chat_model: BaseChatModel | None = None,
    ) -> None:
        """输入：DeepSeek 配置以及可选的已创建 LangChain 模型。

        输出：初始化后的 SQL 生成器实例状态。
        功能：保存惰性模型配置或测试替身，不在服务启动阶段访问外部网络。
        """
        self.api_key = api_key.strip()
        self.base_url = base_url.rstrip("/")
        self.model = model.strip() or "deepseek-v4-flash"
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self._chat_model = chat_model

    def _get_model(self) -> BaseChatModel:
        """输入：初始化时保存的模型实例或 DeepSeek 连接配置。

        输出：用于 SQL 生成的 LangChain 聊天模型；密钥缺失时抛出 ``RuntimeError``。
        功能：首次生成 SQL 时惰性调用公共 ``create_model`` 工厂并缓存结果。
        """

        if self._chat_model is None:
            self._chat_model = create_model(
                api_key=self.api_key,
                base_url=self.base_url,
                model=self.model,
                timeout_seconds=self.timeout_seconds,
                max_tokens=1200,
            )
        return self._chat_model

    def __call__(self, system_prompt: str, user_prompt: str) -> str:
        """输入：包含 Schema 规则的 ``system_prompt`` 和用户问题 ``user_prompt``。

        输出：模型返回并去除首尾空白的候选 SQL 字符串。
        功能：按 System/User 角色调用 LangChain 模型，并校验非空 SQL 内容。
        """
        try:
            return invoke_model_text(
                self._get_model(),
                system_prompt,
                user_prompt,
            )
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("NL2SQL LangChain 模型调用失败") from exc
