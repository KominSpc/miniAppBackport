"""TTS（语音合成）接口预留。

需求明确「后端准备 tts 的接口（暂时不用）」，所以这里只把契约与失败语义定下来：
未接供应商时抛 ``UPSTREAM_UNAVAILABLE``（reason=``tts_not_configured``），
客户端据此隐藏播放入口；将来接上供应商，只需在 :func:`synthesize` 里补一段调用，
接口形状与错误码都不变。
"""

from __future__ import annotations

from typing import Any

from app.core.errors import AppError

#: 当前没有接任何语音合成服务；接入后改成读配置。
CONFIGURED = False


def configured() -> bool:
    return CONFIGURED


def synthesize(text: str, *, voice: str | None = None) -> dict[str, Any]:
    """文本 → 音频。当前版本一律失败，但失败原因是明确的「未配置」。"""
    reason = "tts_not_implemented" if configured() else "tts_not_configured"
    raise AppError("UPSTREAM_UNAVAILABLE", details={"reason": reason})
