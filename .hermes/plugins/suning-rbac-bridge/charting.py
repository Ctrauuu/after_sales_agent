"""售后图表渲染及多 IM 内存上传工具。"""

from __future__ import annotations

import asyncio
import io
import json
import math
import os
import secrets
from collections.abc import Sequence
from enum import Enum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

import httpx
import matplotlib

matplotlib.use("Agg")
import matplotlib.font_manager as font_manager
import matplotlib.pyplot as plt

from gateway.session_context import get_session_env  # type: ignore
from tools.registry import tool_error, tool_result  # type: ignore


class ChartError(ValueError):
    """图表输入、渲染或发送失败时使用的业务异常。"""


class ChartType(str, Enum):
    """文档约定的图表类型。"""

    LINE = "line"
    BAR = "bar"
    HORIZONTAL_BAR = "hbar"
    PIE = "pie"
    BOX = "box"
    TABLE = "table"


class Platform(str, Enum):
    """图表输出平台枚举。"""

    FEISHU = "feishu"
    WECOM = "wecom"
    DINGTALK = "dingtalk"


def _get_env_value(name: str) -> str:
    """输入：Hermes 配置项名称 ``name``；隐式读取 Hermes 配置或进程环境。

    输出：配置值；未配置时返回空字符串。
    功能：Gateway 内优先读取 Hermes ``.env``，并兼容不含 ``hermes_cli`` 的独立加载环境。
    """

    try:
        from hermes_cli.config import get_env_value  # type: ignore
    except ImportError:
        return os.getenv(name, "")
    return get_env_value(name) or ""


CHART_TOOL_SCHEMA: dict[str, Any] = {
    "name": "send_aftersale_chart",
    "description": "将已获授权的售后聚合数据渲染为图表，并向当前飞书、企微或钉钉会话发送图片；发送失败时返回文字摘要。",
    "parameters": {
        "type": "object",
        "properties": {
            "labels": {
                "type": "array",
                "minItems": 1,
                "maxItems": 20,
                "items": {"type": "string", "minLength": 1, "maxLength": 40},
                "description": "横轴、分类或明细标签。",
            },
            "values": {
                "type": "array",
                "minItems": 1,
                "maxItems": 100,
                "items": {},
                "description": "普通图表使用数值数组；箱线图使用与 labels 对应的数值数组列表。",
            },
            "data_kind": {
                "type": "string",
                "enum": ["trend", "proportion", "comparison", "distribution", "detail"],
                "description": "数据语义；未传 chart_type 时用于自动选择图表。",
            },
            "chart_type": {
                "type": "string",
                "enum": ["line", "bar", "hbar", "pie", "box", "table"],
                "description": "指定图表类型；优先级高于 data_kind。",
            },
            "title": {"type": "string", "maxLength": 80, "description": "图表标题。"},
            "summary": {"type": "string", "maxLength": 2000, "description": "随图表发送的简短结论。"},
            "ylabel": {"type": "string", "maxLength": 40, "description": "纵轴名称。"},
        },
        "required": ["labels", "values", "title"],
        "additionalProperties": False,
    },
}


class ChartFactory:
    """按数据类型生成中文售后图表。"""

    COLORS = ("#FF6600", "#2879FF", "#FFB300", "#00C48C", "#FF5C8D", "#8B5CF6")
    FONT_PATHS = (
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "C:/Windows/Fonts/simhei.ttf",
    )

    def __init__(self) -> None:
        """输入：无；隐式读取文档规定的中文字体候选路径。

        输出：初始化可用于 Matplotlib 文本渲染的字体属性；无可用中文字体时抛出 ``ChartError``。
        功能：保证售后标签和标题不会以乱码或方块形式输出。
        """

        self._font = self._load_chinese_font()

    @classmethod
    def _load_chinese_font(cls) -> font_manager.FontProperties:
        """输入：类级 ``FONT_PATHS`` 中文字体路径。

        输出：已注册的 Matplotlib ``FontProperties``；找不到字体时抛出 ``ChartError``。
        功能：依次加载部署环境和 Windows 开发环境支持的中文字体。
        """

        for raw_path in cls.FONT_PATHS:
            path = Path(raw_path)
            if path.is_file():
                font_manager.fontManager.addfont(str(path))
                return font_manager.FontProperties(fname=str(path))
        raise ChartError("未找到中文字体，无法生成可读图表")

    def render_pair(
        self,
        chart_type: ChartType,
        labels: list[str],
        values: list[float] | list[list[float]],
        title: str,
        ylabel: str,
    ) -> dict[str, bytes]:
        """输入：图表类型、已校验标签和值、标题及纵轴名称。

        输出：包含 desktop 和 mobile 两个 PNG 字节串的字典。
        功能：按飞书桌面端和移动端阅读空间分别渲染同一业务数据，不创建临时文件。
        """

        return {
            "desktop": self._render(chart_type, labels, values, title, ylabel, (10, 6)),
            "mobile": self._render(chart_type, labels, values, title, ylabel, (6, 8)),
        }

    def _render(
        self,
        chart_type: ChartType,
        labels: list[str],
        values: list[float] | list[list[float]],
        title: str,
        ylabel: str,
        size: tuple[int, int],
    ) -> bytes:
        """输入：图表参数以及目标 ``size`` 英寸尺寸。

        输出：编码完成的 PNG 字节串；渲染失败时抛出 ``ChartError``。
        功能：创建无界面画布、绘制单张图表并在释放 Figure 后返回内存图像。
        """

        figure, axis = plt.subplots(figsize=size)
        try:
            self._draw(axis, chart_type, labels, values, ylabel)
            axis.set_title(title, fontproperties=self._font, fontsize=14, fontweight="bold", pad=15)
            figure.tight_layout()
            buffer = io.BytesIO()
            figure.savefig(buffer, format="png", dpi=120, bbox_inches="tight")
            return buffer.getvalue()
        finally:
            plt.close(figure)

    def _draw(
        self,
        axis: Any,
        chart_type: ChartType,
        labels: list[str],
        values: list[float] | list[list[float]],
        ylabel: str,
    ) -> None:
        """输入：Matplotlib 坐标轴、图表类型、规范化数据和纵轴名称。

        输出：原地更新 ``axis`` 的图元与标签。
        功能：根据图表类型绘制趋势、对比、占比、分布或明细数据。
        """

        if chart_type is ChartType.LINE:
            self._draw_line(axis, labels, self._flat_values(values), ylabel)
        elif chart_type is ChartType.BAR:
            self._draw_bar(axis, labels, self._flat_values(values), ylabel)
        elif chart_type is ChartType.HORIZONTAL_BAR:
            self._draw_hbar(axis, labels, self._flat_values(values), ylabel)
        elif chart_type is ChartType.PIE:
            self._draw_pie(axis, labels, self._flat_values(values))
        elif chart_type is ChartType.BOX:
            self._draw_box(axis, labels, self._group_values(values), ylabel)
        else:
            self._draw_table(axis, labels, self._flat_values(values))

    @staticmethod
    def _flat_values(values: list[float] | list[list[float]]) -> list[float]:
        """输入：普通图或箱线图的规范化 ``values``。

        输出：普通图表所需的一维数值列表；收到分组数据时抛出 ``ChartError``。
        功能：防止箱线图数据被错误地传给趋势、占比和对比绘制函数。
        """

        if values and isinstance(values[0], list):
            raise ChartError("当前图表需要一维 values 数组")
        return values  # type: ignore[return-value]

    @staticmethod
    def _group_values(values: list[float] | list[list[float]]) -> list[list[float]]:
        """输入：普通图或箱线图的规范化 ``values``。

        输出：箱线图所需的二维数值列表；收到一维数据时抛出 ``ChartError``。
        功能：确保每个箱线图标签都对应一组原始分布数值。
        """

        if not values or not isinstance(values[0], list):
            raise ChartError("箱线图需要二维 values 数组")
        return values  # type: ignore[return-value]

    def _apply_labels(self, axis: Any, xlabel: str = "") -> None:
        """输入：坐标轴 ``axis`` 与可选横轴名称 ``xlabel``。

        输出：原地更新坐标轴的中文字体、横轴名称和负号显示设置。
        功能：统一修正 Matplotlib 默认字体不支持中文和负号乱码的问题。
        """

        if xlabel:
            axis.set_xlabel(xlabel, fontproperties=self._font)
        for label in [*axis.get_xticklabels(), *axis.get_yticklabels()]:
            label.set_fontproperties(self._font)
        plt.rcParams["axes.unicode_minus"] = False

    def _draw_line(self, axis: Any, labels: list[str], values: list[float], ylabel: str) -> None:
        """输入：坐标轴、时间标签、数值和纵轴名称。

        输出：原地写入折线、网格和坐标标签。
        功能：呈现退单量或金额等随时间变化的趋势数据。
        """

        axis.plot(labels, values, marker="o", linewidth=2, color=self.COLORS[0], markersize=6)
        axis.set_ylabel(ylabel, fontproperties=self._font)
        axis.grid(True, alpha=0.3)
        axis.tick_params(axis="x", rotation=30)
        self._apply_labels(axis)

    def _draw_bar(self, axis: Any, labels: list[str], values: list[float], ylabel: str) -> None:
        """输入：坐标轴、分类标签、数值和纵轴名称。

        输出：原地写入柱状图、数值标注和坐标标签。
        功能：呈现品类、区域或原因之间的数值对比。
        """

        bars = axis.bar(labels, values, color=self._colors(len(labels)), edgecolor="white")
        axis.set_ylabel(ylabel, fontproperties=self._font)
        axis.bar_label(bars, fmt="%.2f", padding=3)
        axis.tick_params(axis="x", rotation=30)
        self._apply_labels(axis)

    def _draw_hbar(self, axis: Any, labels: list[str], values: list[float], ylabel: str) -> None:
        """输入：坐标轴、排名标签、数值和纵轴名称。

        输出：原地写入横向柱状图、数值标注和坐标标签。
        功能：呈现 TopN 品类、SKU 或原因排名，且让最大值位于顶部。
        """

        bars = axis.barh(labels, values, color=self._colors(len(labels)), edgecolor="white")
        axis.invert_yaxis()
        axis.set_xlabel(ylabel, fontproperties=self._font)
        axis.bar_label(bars, fmt="%.2f", padding=3)
        self._apply_labels(axis)

    def _draw_pie(self, axis: Any, labels: list[str], values: list[float]) -> None:
        """输入：坐标轴、占比标签和正数数值。

        输出：原地写入带百分比标注的饼图。
        功能：呈现退单原因或品类的构成占比。
        """

        if any(value < 0 for value in values) or not any(values):
            raise ChartError("饼图 values 必须至少包含一个正数")
        _, texts, autotexts = axis.pie(
            values,
            labels=labels,
            autopct="%1.1f%%",
            colors=self._colors(len(labels)),
            startangle=90,
        )
        for text in [*texts, *autotexts]:
            text.set_fontproperties(self._font)

    def _draw_box(self, axis: Any, labels: list[str], values: list[list[float]], ylabel: str) -> None:
        """输入：坐标轴、分组标签、每组原始数值和纵轴名称。

        输出：原地写入箱线图和坐标标签。
        功能：展示不同品类或区域的退单金额、时效等数据分布。
        """

        axis.boxplot(values, tick_labels=labels)
        axis.set_ylabel(ylabel, fontproperties=self._font)
        axis.tick_params(axis="x", rotation=30)
        self._apply_labels(axis)

    def _draw_table(self, axis: Any, labels: list[str], values: list[float]) -> None:
        """输入：坐标轴、明细标签和对应数值。

        输出：原地写入两列表格并隐藏坐标轴。
        功能：在分类过多或图形表达不清晰时提供移动端可读的明细降级展示。
        """

        axis.axis("off")
        table = axis.table(
            cellText=[[label, f"{value:.2f}"] for label, value in zip(labels, values)],
            colLabels=["项目", "数值"],
            cellLoc="center",
            loc="center",
        )
        table.auto_set_font_size(False)
        table.set_fontsize(10)
        table.scale(1, 1.5)
        for cell in table.get_celld().values():
            cell.get_text().set_fontproperties(self._font)

    def _colors(self, count: int) -> list[str]:
        """输入：所需颜色数量 ``count``。

        输出：长度等于 ``count`` 的苏宁色系列表。
        功能：循环复用有限调色板，避免超过六项数据时图表渲染失败。
        """

        return [self.COLORS[index % len(self.COLORS)] for index in range(count)]


class WeComBotImageAdapter:
    """通过 Hermes 当前 WeCom AI Bot WebSocket 连接发送图表。"""

    MAX_IMAGE_BYTES = 2 * 1024 * 1024

    def __init__(
        self,
        chat_id: str,
        message_id: str,
        profile: str,
    ) -> None:
        """输入：Hermes 可信当前会话的 ``chat_id``、``message_id`` 和配置 ``profile``。

        输出：保存单次图片回复所需路由；缺少当前会话 ID 时抛出 ``ChartError``。
        功能：固定图片目标为触发工具的企微会话，并保留消息 ID 供 AI Bot 原消息回复使用。
        """

        if not chat_id:
            raise ChartError("企微图表发送缺少当前会话收件人")
        self._chat_id = chat_id
        self._message_id = message_id
        self._profile = profile

    async def send(self, image: bytes, summary: str) -> dict[str, Any]:
        """输入：不超过 2MB 的 PNG ``image`` 与可选结论 ``summary``。

        输出：包含企微消息 ID 的发送状态；实时适配器不可用或发送失败时抛出 ``ChartError``。
        功能：将内存图表短暂写入 Hermes 图片接口要求的临时文件，通过现有 AI Bot WebSocket 回复并立即删除文件。
        """

        if not image or len(image) > self.MAX_IMAGE_BYTES:
            raise ChartError("图表图片为空或超过企微 2MB 限制")
        try:
            from gateway.config import Platform as GatewayPlatform  # type: ignore
            from gateway.run import _gateway_runner_ref  # type: ignore

            runner = _gateway_runner_ref()
            resolver = getattr(runner, "_authorization_adapter", None)
            adapter = resolver(GatewayPlatform.WECOM, self._profile) if callable(resolver) else None
            if adapter is None:
                raise ChartError("Hermes 当前企微 AI Bot WebSocket 未连接")
            with NamedTemporaryFile(suffix=".png", delete=False) as temporary:
                temporary.write(image)
                image_path = Path(temporary.name)
            try:
                result = await adapter.send_image_file(
                    chat_id=self._chat_id,
                    image_path=str(image_path),
                    caption=summary or None,
                    reply_to=self._message_id or None,
                )
            finally:
                image_path.unlink(missing_ok=True)
        except ChartError:
            raise
        except Exception as exc:
            raise ChartError(f"企微 AI Bot 图表发送失败: {exc}") from exc
        if not result.success:
            raise ChartError(f"企微 AI Bot 图表发送失败: {result.error or '未知错误'}")
        return {"status": "sent", "platform": "wecom", "message_id": result.message_id}


class DingTalkBotImageAdapter:
    """通过 Hermes 当前钉钉应用机器人连接上传并发送图表。"""

    MAX_IMAGE_BYTES = 2 * 1024 * 1024

    def __init__(self, chat_id: str, chat_type: str, profile: str) -> None:
        """输入：可信当前会话的 ``chat_id``、``chat_type`` 和配置 ``profile``。

        输出：保存向当前钉钉会话发送图片所需的路由；会话 ID 缺失时抛出 ``ChartError``。
        功能：限制图片仅能发送给触发工具的群聊或单聊，不接受模型指定的任意收件人。
        """

        if not chat_id:
            raise ChartError("钉钉图表发送缺少当前会话收件人")
        self._chat_id = chat_id
        self._chat_type = chat_type
        self._profile = profile

    async def send(self, image: bytes, title: str, summary: str) -> dict[str, Any]:
        """输入：不超过 2MB 的 PNG ``image``、图表 ``title`` 与可选 ``summary``。

        输出：钉钉图片消息的发送状态；实时机器人、媒体上传或消息发送失败时抛出 ``ChartError``。
        功能：复用 Stream 客户端上传 PNG 为 MediaID，再通过已授权的企业机器人 API 向当前会话发送 Markdown 图片。
        """

        if not image or len(image) > self.MAX_IMAGE_BYTES:
            raise ChartError("图表图片为空或超过钉钉 2MB 限制")
        try:
            from gateway.config import Platform as GatewayPlatform  # type: ignore
            from gateway.run import _gateway_runner_ref  # type: ignore

            runner = _gateway_runner_ref()
            resolver = getattr(runner, "_authorization_adapter", None)
            adapter = resolver(GatewayPlatform.DINGTALK, self._profile) if callable(resolver) else None
            stream_client = getattr(adapter, "_stream_client", None)
            upload = getattr(stream_client, "upload_to_dingtalk", None)
            token_getter = getattr(adapter, "_get_access_token", None)
            http_client = getattr(adapter, "_http_client", None)
            robot_code = str(getattr(adapter, "_robot_code", "") or "")
            if not callable(upload) or not callable(token_getter) or http_client is None or not robot_code:
                raise ChartError("Hermes 当前钉钉应用机器人未连接或未配置媒体发送能力")
            media_id = await asyncio.to_thread(
                upload,
                image,
                filename="aftersale-chart.png",
                mimetype="image/png",
            )
            if not isinstance(media_id, str) or not media_id.strip():
                raise ChartError("钉钉图表媒体上传未返回 MediaID")
            access_token = await token_getter()
            if not isinstance(access_token, str) or not access_token.strip():
                raise ChartError("钉钉应用访问令牌获取失败")
            endpoint = "privateChatMessages" if self._chat_type == "dm" else "groupMessages"
            caption = summary or title
            response = await http_client.post(
                f"https://api.dingtalk.com/v1.0/robot/{endpoint}/send",
                headers={"x-acs-dingtalk-access-token": access_token},
                json={
                    "robotCode": robot_code,
                    "openConversationId": self._chat_id,
                    "msgKey": "sampleMarkdown",
                    "msgParam": json.dumps(
                        {"title": title, "text": f"{caption}\n\n![{title}]({media_id.strip()})"},
                        ensure_ascii=False,
                    ),
                },
            )
            response.raise_for_status()
            payload = response.json()
        except ChartError:
            raise
        except Exception as exc:
            raise ChartError(f"钉钉图表发送失败: {exc}") from exc
        if not isinstance(payload, dict) or payload.get("errcode") not in (None, 0):
            raise ChartError(f"钉钉图表发送失败: {payload!r}")
        return {
            "status": "sent",
            "platform": "dingtalk",
            "message_id": str(payload.get("processQueryKey", "")),
        }


class FeishuImageAdapter:
    """只负责飞书图表图片的内存上传与当前消息回复。"""

    BASE_URL = "https://open.feishu.cn/open-apis"

    def __init__(self, app_id: str, app_secret: str, message_id: str) -> None:
        """输入：飞书应用凭据及可信会话提供的 ``message_id``。

        输出：保存发送图表所需的最小配置；参数为空时抛出 ``ChartError``。
        功能：把图表上传目标固定为当前会话消息，避免模型控制收件人。
        """

        if not app_id or not app_secret or not message_id:
            raise ChartError("飞书图表发送缺少应用凭据或当前消息标识")
        self._app_id = app_id
        self._app_secret = app_secret
        self._message_id = message_id

    async def send(self, images: dict[str, bytes], title: str, summary: str) -> dict[str, Any]:
        """输入：desktop/mobile PNG 字节、标题和摘要。

        输出：包含飞书 ``image_keys`` 的发送结果；接口异常时抛出 ``ChartError``。
        功能：一次获取令牌，上传两套分辨率，并以富文本回复触发图表的当前消息。
        """

        async with httpx.AsyncClient(timeout=10) as client:
            token = await self._tenant_token(client)
            image_keys = {
                name: await self._upload_image(client, token, image)
                for name, image in images.items()
            }
            await self._reply(client, token, title, summary, image_keys)
        return {"status": "sent", "platform": "feishu", "image_keys": image_keys}

    async def _tenant_token(self, client: httpx.AsyncClient) -> str:
        """输入：已打开的 HTTP ``client``。

        输出：飞书 tenant access token；接口错误或缺少 token 时抛出 ``ChartError``。
        功能：使用应用凭据换取本次图片上传和消息回复的授权令牌。
        """

        response = await client.post(
            f"{self.BASE_URL}/auth/v3/tenant_access_token/internal",
            json={"app_id": self._app_id, "app_secret": self._app_secret},
        )
        payload = self._json_response(response, "获取飞书访问令牌")
        token = str(payload.get("tenant_access_token", "")).strip()
        if not token:
            raise ChartError("飞书未返回 tenant_access_token")
        return token

    async def _upload_image(self, client: httpx.AsyncClient, token: str, image: bytes) -> str:
        """输入：HTTP ``client``、访问令牌和非空 PNG 字节。

        输出：飞书 ``image_key``；图片不合规或接口失败时抛出 ``ChartError``。
        功能：以 ``image_type=message`` 将内存图片上传为可发送的飞书资源。
        """

        if not image or len(image) > 10 * 1024 * 1024:
            raise ChartError("图表图片为空或超过飞书 10MB 限制")
        response = await client.post(
            f"{self.BASE_URL}/im/v1/images",
            headers={"Authorization": f"Bearer {token}"},
            data={"image_type": "message"},
            files={"image": ("aftersale-chart.png", image, "image/png")},
        )
        payload = self._json_response(response, "上传飞书图表")
        image_key = str((payload.get("data") or {}).get("image_key", "")).strip()
        if not image_key:
            raise ChartError("飞书未返回 image_key")
        return image_key

    async def _reply(
        self,
        client: httpx.AsyncClient,
        token: str,
        title: str,
        summary: str,
        image_keys: dict[str, str],
    ) -> None:
        """输入：HTTP ``client``、令牌、文本内容和 desktop/mobile 图片键。

        输出：无；飞书拒绝富文本回复时抛出 ``ChartError``。
        功能：把摘要及两种分辨率图表组合为 post 消息，回复可信的当前用户消息。
        """

        content = {
            "zh_cn": {
                "title": title,
                "content": [
                    [{"tag": "text", "text": summary or "售后分析图表"}],
                    [{"tag": "text", "text": "桌面版"}, {"tag": "img", "image_key": image_keys["desktop"]}],
                    [{"tag": "text", "text": "移动版"}, {"tag": "img", "image_key": image_keys["mobile"]}],
                ],
            }
        }
        response = await client.post(
            f"{self.BASE_URL}/im/v1/messages/{self._message_id}/reply",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "msg_type": "post",
                "content": json.dumps(content, ensure_ascii=False),
                "uuid": secrets.token_hex(16),
            },
        )
        self._json_response(response, "回复飞书图表")

    @staticmethod
    def _json_response(response: httpx.Response, action: str) -> dict[str, Any]:
        """输入：HTTP 响应 ``response`` 和当前操作名称 ``action``。

        输出：飞书成功响应的 JSON 字典；HTTP、JSON 或业务错误时抛出 ``ChartError``。
        功能：统一校验飞书 HTTP 状态和顶层 ``code``，避免把失败响应误当作成功。
        """

        try:
            response.raise_for_status()
            payload = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise ChartError(f"{action}失败") from exc
        if not isinstance(payload, dict) or payload.get("code") != 0:
            message = payload.get("msg", "未知错误") if isinstance(payload, dict) else "响应格式错误"
            raise ChartError(f"{action}失败: {message}")
        return payload


def _required_text(value: Any, field: str, max_length: int) -> str:
    """输入：原始字段值 ``value``、字段名和最大长度。

    输出：去除首尾空白后的非空文本；格式不正确时抛出 ``ChartError``。
    功能：在图表渲染和飞书请求前统一限制标题、摘要及标签文本。
    """

    if not isinstance(value, str) or not (text := value.strip()) or len(text) > max_length:
        raise ChartError(f"{field} 必须是 1 到 {max_length} 个字符的文本")
    return text


def _choose_chart_type(arguments: dict[str, Any]) -> ChartType:
    """输入：模型传入的图表参数 ``arguments``。

    输出：确定的 ``ChartType``；类型值无效时抛出 ``ChartError``。
    功能：优先采用模型按数据特征选择的类型，缺失时按文档数据语义进行保守兜底。
    """

    raw_type = arguments.get("chart_type")
    if raw_type:
        try:
            return ChartType(str(raw_type))
        except ValueError as exc:
            raise ChartError("不支持的 chart_type") from exc
    data_kind = arguments.get("data_kind")
    mapping = {
        "trend": ChartType.LINE,
        "proportion": ChartType.PIE,
        "comparison": ChartType.BAR,
        "distribution": ChartType.BOX,
        "detail": ChartType.TABLE,
    }
    if data_kind in mapping:
        return mapping[data_kind]
    labels = arguments.get("labels")
    return ChartType.PIE if isinstance(labels, list) and len(labels) <= 5 else ChartType.HORIZONTAL_BAR


def _normalise_data(
    chart_type: ChartType,
    arguments: dict[str, Any],
) -> tuple[list[str], list[float] | list[list[float]]]:
    """输入：图表类型和模型参数 ``arguments``。

    输出：已校验的标签与一维或二维浮点数值。
    功能：限制图表规模、校验数值有限性，并确保普通图和箱线图的数据形状正确。
    """

    raw_labels = arguments.get("labels")
    raw_values = arguments.get("values")
    if not isinstance(raw_labels, list) or not 1 <= len(raw_labels) <= 20:
        raise ChartError("labels 必须是 1 到 20 项的数组")
    labels = [_required_text(label, "labels 项", 40) for label in raw_labels]
    if not isinstance(raw_values, list) or not raw_values:
        raise ChartError("values 必须是非空数组")
    if chart_type is ChartType.BOX:
        if len(raw_values) != len(labels) or not all(
            isinstance(group, list) and 1 <= len(group) <= 100 for group in raw_values
        ):
            raise ChartError("箱线图 values 必须是与 labels 等长的非空二维数组")
        return labels, [_normalise_numbers(group) for group in raw_values]
    if len(raw_values) != len(labels) or any(isinstance(value, list) for value in raw_values):
        raise ChartError("当前图表 values 必须是与 labels 等长的一维数组")
    return labels, _normalise_numbers(raw_values)


def _normalise_numbers(values: Sequence[Any]) -> list[float]:
    """输入：待绘制的任意数值序列 ``values``。

    输出：有限的浮点数列表；遇到布尔值、非数值或无穷值时抛出 ``ChartError``。
    功能：在进入 Matplotlib 之前阻断异常统计数据，避免生成误导性图表。
    """

    numbers: list[float] = []
    for value in values:
        if isinstance(value, bool):
            raise ChartError("values 不能包含布尔值")
        try:
            number = float(value)
        except (TypeError, ValueError) as exc:
            raise ChartError("values 必须全部为数值") from exc
        if not math.isfinite(number):
            raise ChartError("values 必须全部为有限数值")
        numbers.append(number)
    return numbers


def _text_fallback(labels: list[str], values: list[float] | list[list[float]], reason: str) -> str:
    """输入：已校验标签、数值和不能发送图片的原因。

    输出：Top5 纯文字摘要。
    功能：在不支持平台或飞书上传失败时保留用户可读的核心分析结果。
    """

    if values and isinstance(values[0], list):
        pairs = [(label, f"{len(group)} 个样本") for label, group in zip(labels, values)] # type: ignore
    else:
        pairs = [(label, f"{value:.2f}") for label, value in zip(labels, values)]  # type: ignore[arg-type]
    lines = "\n".join(f"{index}. {label}: {value}" for index, (label, value) in enumerate(pairs[:5], 1))
    return f"{reason}\n{lines}"


async def send_aftersale_chart(arguments: dict[str, Any]) -> dict[str, Any]:
    """输入：模型提供的图表数据；隐式读取可信 Hermes 会话字段、飞书应用凭据和实时 IM 适配器。

    输出：飞书、企微或钉钉发送状态，或含 ``text_fallback`` 的降级结果；输入无效时抛出 ``ChartError``。
    功能：校验数据、在内存渲染图表并仅向可信当前会话发送；钉钉通过企业机器人上传媒体后发送图片。
    """

    chart_type = _choose_chart_type(arguments)
    labels, values = _normalise_data(chart_type, arguments)
    title = _required_text(arguments.get("title"), "title", 80)
    raw_summary = arguments.get("summary", "")
    raw_ylabel = arguments.get("ylabel", "数值")
    if not isinstance(raw_summary, str) or len(raw_summary) > 2000:
        raise ChartError("summary 必须是不超过 2000 个字符的文本")
    if not isinstance(raw_ylabel, str) or len(raw_ylabel) > 40:
        raise ChartError("ylabel 必须是不超过 40 个字符的文本")
    summary = raw_summary.strip()
    ylabel = raw_ylabel.strip() or "数值"
    platform_name = get_session_env("HERMES_SESSION_PLATFORM", "").strip().lower()
    try:
        platform = Platform(platform_name)
    except ValueError:
        platform = None
    if platform not in {Platform.FEISHU, Platform.WECOM, Platform.DINGTALK}:
        return {
            "status": "unsupported",
            "platform": platform_name or "unknown",
            "text_fallback": _text_fallback(labels, values, "当前平台暂不支持图表展示，已切换为文字模式。"),
        }

    images = ChartFactory().render_pair(chart_type, labels, values, title, ylabel)
    try:
        if platform is Platform.FEISHU:
            return await FeishuImageAdapter(
                _get_env_value("FEISHU_APP_ID").strip(),
                _get_env_value("FEISHU_APP_SECRET").strip(),
                get_session_env("HERMES_SESSION_MESSAGE_ID", "").strip(),
            ).send(images, title, summary)
        if platform is Platform.WECOM:
            return await WeComBotImageAdapter(
                get_session_env("HERMES_SESSION_CHAT_ID", "").strip(),
                get_session_env("HERMES_SESSION_MESSAGE_ID", "").strip(),
                get_session_env("HERMES_SESSION_PROFILE", "").strip(),
            ).send(images["mobile"], summary)
        return await DingTalkBotImageAdapter(
            get_session_env("HERMES_SESSION_CHAT_ID", "").strip(),
            get_session_env("HERMES_SESSION_CHAT_TYPE", "").strip(),
            get_session_env("HERMES_SESSION_PROFILE", "").strip(),
        ).send(images["mobile"], title, summary)
    except (httpx.HTTPError, ChartError) as exc:
        return {
            "status": "fallback",
            "platform": platform.value,
            "text_fallback": _text_fallback(labels, values, f"图表发送失败，已切换为文字模式：{exc}"),
        }


async def handle_chart(arguments: dict[str, Any], **_kwargs: Any) -> str:
    """输入：模型图表参数 ``arguments``；其余 Hermes 调用参数由 ``_kwargs`` 接收。

    输出：Hermes 工具成功结果或参数错误结果字符串。
    功能：将图表发送结果转换为 Hermes 工具协议，保证数据错误不会伪装成成功发送。
    """

    try:
        return tool_result(await send_aftersale_chart(dict(arguments or {})))
    except ChartError as exc:
        return tool_error(str(exc))


__all__ = [
    "CHART_TOOL_SCHEMA",
    "ChartError",
    "ChartFactory",
    "ChartType",
    "DingTalkBotImageAdapter",
    "FeishuImageAdapter",
    "Platform",
    "WeComBotImageAdapter",
    "handle_chart",
    "send_aftersale_chart",
]
