"""验证飞书图表的内存渲染、发送和降级行为。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest


CHARTING_PATH = Path(__file__).resolve().parents[2] / ".hermes" / "plugins" / "suning-rbac-bridge" / "charting.py"


class _FakeResponse:
    """供图表 HTTP 测试使用的最小响应替身。"""

    def __init__(self, payload: dict[str, Any]) -> None:
        """输入：图表发送接口的 JSON ``payload``。

        输出：保存测试响应内容。
        功能：模拟 ``httpx.Response`` 的成功响应读取接口。
        """

        self._payload = payload

    def raise_for_status(self) -> None:
        """输入：无。

        输出：无；成功响应不抛出异常。
        功能：模拟 ``httpx.Response`` 的状态码校验。
        """

    def json(self) -> dict[str, Any]:
        """输入：无。

        输出：构造时传入的飞书 JSON 响应。
        功能：模拟 ``httpx.Response.json``，供各平台发送逻辑读取业务状态。
        """

        return self._payload


class _FakeClient:
    """记录异步图表 HTTP 请求的最小 AsyncClient 替身。"""

    def __init__(self, responses: list[_FakeResponse]) -> None:
        """输入：按调用顺序返回的 ``responses``。

        输出：初始化空请求记录。
        功能：在不访问飞书网络的情况下验证上传与回复请求内容。
        """

        self._responses = responses
        self.calls: list[dict[str, Any]] = []

    async def __aenter__(self) -> _FakeClient:
        """输入：无。

        输出：当前异步客户端替身。
        功能：支持生产代码使用的 ``async with httpx.AsyncClient`` 语法。
        """

        return self

    async def __aexit__(self, *_args: Any) -> None:
        """输入：异步上下文退出的异常信息 ``_args``。

        输出：无。
        功能：模拟 HTTP 客户端关闭，不需要额外清理资源。
        """

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        """输入：请求 URL ``url`` 和 HTTP 参数 ``kwargs``。

        输出：下一个预设 HTTP 响应；响应耗尽时抛出 ``AssertionError``。
        功能：记录图表模块发出的令牌、上传和消息发送请求。
        """

        self.calls.append({"url": url, **kwargs})
        if not self._responses:
            raise AssertionError("出现了未预期的飞书请求")
        return self._responses.pop(0)

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        """输入：请求 URL ``url`` 和 HTTP 参数 ``kwargs``。

        输出：下一个预设 HTTP 响应；响应耗尽时抛出 ``AssertionError``。
        功能：记录 GET 请求，使图表适配测试无需访问外部网络。
        """

        self.calls.append({"url": url, **kwargs})
        if not self._responses:
            raise AssertionError("出现了未预期的企微请求")
        return self._responses.pop(0)


class _FakeGatewayPlatform:
    """提供图表插件定位实时 IM 适配器所需的平台常量。"""

    WECOM = "wecom"
    DINGTALK = "dingtalk"


class _FakeGatewayRunner:
    """提供按当前 Hermes profile 解析实时 IM 适配器的测试替身。"""

    def __init__(self, adapter: Any) -> None:
        """输入：待返回的实时 ``adapter``。

        输出：保存适配器并初始化空解析记录。
        功能：模拟 Gateway Runner 的 profile 感知实时适配器选择接口。
        """

        self._adapter = adapter
        self.calls: list[tuple[Any, str]] = []

    def _authorization_adapter(self, platform: Any, profile: str) -> Any:
        """输入：Gateway 平台 ``platform`` 与当前配置 ``profile``。

        输出：构造时提供的实时适配器。
        功能：记录生产代码是否按当前平台和 profile 获取连接。
        """

        self.calls.append((platform, profile))
        return self._adapter


class _FakeWeComAdapter:
    """记录 AI Bot WebSocket 图片发送参数的实时适配器替身。"""

    def __init__(self) -> None:
        """输入：无。

        输出：初始化空图片发送记录。
        功能：保存发送期间读取到的临时 PNG 与可信会话路由，供断言使用。
        """

        self.calls: list[dict[str, Any]] = []

    async def send_image_file(
        self,
        chat_id: str,
        image_path: str,
        caption: str | None = None,
        reply_to: str | None = None,
    ) -> SimpleNamespace:
        """输入：当前会话、临时图片路径、结论和原消息 ID。

        输出：成功的 Hermes ``SendResult`` 等价对象。
        功能：在文件仍存在时读取 PNG 并记录全部发送参数，模拟 WebSocket 上传完成。
        """

        path = Path(image_path)
        self.calls.append(
            {
                "chat_id": chat_id,
                "image_path": image_path,
                "image": path.read_bytes(),
                "caption": caption,
                "reply_to": reply_to,
            }
        )
        return SimpleNamespace(success=True, message_id="wecom_reply", error=None)


class _FakeDingTalkStreamClient:
    """模拟钉钉 Stream 客户端的本地图片上传接口。"""

    def __init__(self) -> None:
        """输入：无。

        输出：初始化空上传记录。
        功能：记录图表 PNG 与元数据，模拟钉钉上传后返回媒体 ID。
        """

        self.calls: list[dict[str, Any]] = []

    def upload_to_dingtalk(
        self,
        image: bytes,
        filetype: str = "image",
        filename: str = "image.png",
        mimetype: str = "image/png",
    ) -> str:
        """输入：PNG 字节 ``image``、钉钉文件类型、文件名和 MIME 类型。

        输出：固定的钉钉媒体 ID。
        功能：记录上传参数，验证生产代码以本地图表字节请求钉钉媒体上传。
        """

        self.calls.append(
            {
                "image": image,
                "filetype": filetype,
                "filename": filename,
                "mimetype": mimetype,
            }
        )
        return "media_test_id"


class _FakeDingTalkAdapter:
    """提供钉钉企业机器人媒体上传和主动消息发送依赖。"""

    def __init__(self) -> None:
        """输入：无。

        输出：初始化 Stream 客户端、HTTP 客户端和机器人标识。
        功能：模拟已连接且获授权的钉钉应用机器人，供图表发送端到端测试使用。
        """

        self._stream_client = _FakeDingTalkStreamClient()
        self._http_client = _FakeClient([_FakeResponse({"processQueryKey": "ding_reply"})])
        self._robot_code = "ding_robot"

    async def _get_access_token(self) -> str:
        """输入：无。

        输出：固定的应用访问令牌。
        功能：模拟 Stream 客户端缓存的钉钉机器人访问令牌读取。
        """

        return "ding_access_token"


def _load_charting(
    monkeypatch: pytest.MonkeyPatch,
    platform: str = "feishu",
    env_values: dict[str, str] | None = None,
    session_values: dict[str, str] | None = None,
    live_adapter: Any = None,
) -> ModuleType:
    """输入：pytest 补丁器、平台、配置、会话字段及可选实时 IM 适配器。

    输出：已加载的真实 ``charting`` 模块。
    功能：为独立插件模块提供 Hermes 会话、实时 Gateway、配置读取器和工具协议替身。
    """

    gateway = ModuleType("gateway")
    gateway.__path__ = []  # type: ignore[attr-defined]
    session_context = ModuleType("gateway.session_context")
    values = {
        "HERMES_SESSION_PLATFORM": platform,
        "HERMES_SESSION_MESSAGE_ID": "om_test_message",
        "HERMES_SESSION_CHAT_TYPE": "dm",
        "HERMES_SESSION_CHAT_ID": "oc_test_chat",
        "HERMES_SESSION_USER_ID": "wecom_test_user",
        "HERMES_SESSION_PROFILE": "customer-service",
    }
    values.update(session_values or {})
    session_context.get_session_env = lambda name, default="": values.get(name, default)
    gateway_config = ModuleType("gateway.config")
    gateway_config.Platform = _FakeGatewayPlatform
    gateway_run = ModuleType("gateway.run")
    runner = _FakeGatewayRunner(live_adapter)

    def gateway_runner_ref() -> _FakeGatewayRunner:
        """输入：无。

        输出：当前测试的 Gateway Runner 替身。
        功能：模拟 Hermes 用弱引用公开进程内实时适配器注册表。
        """

        return runner

    gateway_run._gateway_runner_ref = gateway_runner_ref
    tools = ModuleType("tools")
    tools.__path__ = []  # type: ignore[attr-defined]
    registry = ModuleType("tools.registry")
    registry.tool_error = lambda message: json.dumps({"error": message}, ensure_ascii=False)
    registry.tool_result = lambda payload: json.dumps(payload, ensure_ascii=False)
    hermes_cli = ModuleType("hermes_cli")
    hermes_cli.__path__ = []  # type: ignore[attr-defined]
    config = ModuleType("hermes_cli.config")
    config.get_env_value = lambda name: (env_values or {}).get(name)
    monkeypatch.setitem(sys.modules, "gateway", gateway)
    monkeypatch.setitem(sys.modules, "gateway.session_context", session_context)
    monkeypatch.setitem(sys.modules, "gateway.config", gateway_config)
    monkeypatch.setitem(sys.modules, "gateway.run", gateway_run)
    monkeypatch.setitem(sys.modules, "tools", tools)
    monkeypatch.setitem(sys.modules, "tools.registry", registry)
    monkeypatch.setitem(sys.modules, "hermes_cli", hermes_cli)
    monkeypatch.setitem(sys.modules, "hermes_cli.config", config)

    module_name = "_suning_charting_test"
    spec = importlib.util.spec_from_file_location(module_name, CHARTING_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载 charting 插件模块")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _chart_arguments(**overrides: Any) -> dict[str, Any]:
    """输入：需要覆盖的图表参数 ``overrides``。

    输出：一份可发送的趋势图参数字典。
    功能：减少测试中无关的图表输入重复，保证每个案例只突出目标行为。
    """

    arguments = {
        "labels": ["8月1日", "8月2日", "8月3日"],
        "values": [12, 18, 15],
        "data_kind": "trend",
        "title": "本月退单趋势",
        "summary": "退单量整体平稳。",
        "ylabel": "退单量",
    }
    arguments.update(overrides)
    return arguments


def test_chart_factory_renders_two_pngs_without_files(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """输入：pytest 环境补丁器和空临时目录 ``tmp_path``。

    输出：无；PNG 编码或落盘时通过断言报告失败。
    功能：验证桌面和移动图表均在内存生成，不依赖临时文件。
    """

    charting = _load_charting(monkeypatch)
    images = charting.ChartFactory().render_pair(
        charting.ChartType.LINE,
        ["8月1日", "8月2日"],
        [1.0, 2.0],
        "退单趋势",
        "退单量",
    )

    assert set(images) == {"desktop", "mobile"}
    assert all(image.startswith(b"\x89PNG\r\n\x1a\n") for image in images.values())
    assert not list(tmp_path.iterdir())


@pytest.mark.asyncio
async def test_feishu_chart_uploads_two_images_and_replies(monkeypatch: pytest.MonkeyPatch) -> None:
    """输入：pytest 环境补丁器。

    输出：无；飞书请求顺序或富文本结构错误时通过断言报告失败。
    功能：验证图表仅通过内存 PNG 上传，并回复可信会话中的原始飞书消息。
    """

    charting = _load_charting(
        monkeypatch,
        env_values={"FEISHU_APP_ID": "cli_test", "FEISHU_APP_SECRET": "secret_test"},
    )
    client = _FakeClient(
        [
            _FakeResponse({"code": 0, "tenant_access_token": "tenant_token"}),
            _FakeResponse({"code": 0, "data": {"image_key": "desktop_key"}}),
            _FakeResponse({"code": 0, "data": {"image_key": "mobile_key"}}),
            _FakeResponse({"code": 0, "data": {"message_id": "om_reply"}}),
        ]
    )
    monkeypatch.setattr(charting.httpx, "AsyncClient", lambda **_kwargs: client)

    result = await charting.send_aftersale_chart(_chart_arguments())

    assert result == {
        "status": "sent",
        "platform": "feishu",
        "image_keys": {"desktop": "desktop_key", "mobile": "mobile_key"},
    }
    assert [call["url"].rsplit("/", 1)[-1] for call in client.calls] == [
        "internal",
        "images",
        "images",
        "reply",
    ]
    assert all(isinstance(call["files"]["image"][1], bytes) for call in client.calls[1:3])
    post_content = json.loads(client.calls[3]["json"]["content"])
    assert post_content["zh_cn"]["content"][1][1]["image_key"] == "desktop_key"
    assert post_content["zh_cn"]["content"][2][1]["image_key"] == "mobile_key"


@pytest.mark.asyncio
async def test_dingtalk_chart_uploads_media_and_sends_markdown(monkeypatch: pytest.MonkeyPatch) -> None:
    """输入：pytest 环境补丁器。

    输出：无；MediaID 上传或向当前钉钉会话的图片消息参数错误时通过断言报告失败。
    功能：验证已授权的钉钉应用机器人上传本地图表，并以 Markdown 图片发送到当前群会话。
    """

    adapter = _FakeDingTalkAdapter()
    charting = _load_charting(
        monkeypatch,
        platform="dingtalk",
        session_values={"HERMES_SESSION_CHAT_TYPE": "group"},
        live_adapter=adapter,
    )

    result = await charting.send_aftersale_chart(_chart_arguments())

    assert result == {"status": "sent", "platform": "dingtalk", "message_id": "ding_reply"}
    assert len(adapter._stream_client.calls) == 1
    assert adapter._stream_client.calls[0]["image"].startswith(b"\x89PNG\r\n\x1a\n")
    assert adapter._stream_client.calls[0]["filename"] == "aftersale-chart.png"
    assert len(adapter._http_client.calls) == 1
    call = adapter._http_client.calls[0]
    assert call["url"].endswith("/v1.0/robot/groupMessages/send")
    assert call["headers"] == {"x-acs-dingtalk-access-token": "ding_access_token"}
    assert call["json"]["openConversationId"] == "oc_test_chat"
    assert call["json"]["robotCode"] == "ding_robot"
    assert call["json"]["msgKey"] == "sampleMarkdown"
    assert "media_test_id" in json.loads(call["json"]["msgParam"])["text"]


@pytest.mark.asyncio
async def test_wecom_chart_uses_live_bot_and_removes_temporary_png(monkeypatch: pytest.MonkeyPatch) -> None:
    """输入：pytest 补丁器 ``monkeypatch``。

    输出：无；企微适配器、会话回复或临时文件清理错误时由 pytest 报告失败。
    功能：验证图表复用现有 AI Bot WebSocket，按当前 Hermes 会话发送移动图并在完成后立即删文件。
    """

    adapter = _FakeWeComAdapter()
    charting = _load_charting(
        monkeypatch,
        platform="wecom",
        live_adapter=adapter,
    )

    result = await charting.send_aftersale_chart(_chart_arguments())

    assert result == {"status": "sent", "platform": "wecom", "message_id": "wecom_reply"}
    assert len(adapter.calls) == 1
    call = adapter.calls[0]
    assert call["chat_id"] == "oc_test_chat"
    assert call["reply_to"] == "om_test_message"
    assert call["caption"] == "退单量整体平稳。"
    assert call["image"].startswith(b"\x89PNG\r\n\x1a\n")
    assert not Path(call["image_path"]).exists()


@pytest.mark.asyncio
async def test_invalid_chart_data_returns_tool_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """输入：pytest 环境补丁器。

    输出：无；非法数据未被工具拒绝时通过断言报告失败。
    功能：验证图表工具在模型输入边界拦截标签和值长度不一致的数据。
    """

    charting = _load_charting(monkeypatch)

    result = await charting.handle_chart(_chart_arguments(values=[1]))

    assert "error" in json.loads(result)
