"""网关：把自己插在"客户端"和"模型"之间，做三件事：

1. **三档接管**（一格 env 切换，随时能退回原样）
   - passthrough：原样转发，只记录
   - enrich：保留对方的人格提示，追加状态包 + 联想
   - replace：系统提示完全由自己合成（人格文件 + 状态）

2. **请求卫生**
   - 历史裁剪：只发最近 N 条（对方经常把上千条历史整包发过来）
   - 媒体剥离：把图片 base64 换成占位符（一次请求能到十几 MB）
   - 只记真人对话：插件/工具调用、纯 JSON 回复不写进记忆

3. **透明转发**：不持有密钥，原样转发调用方的 Authorization。
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request

MODE = os.environ.get("GATEWAY_MODE") or "passthrough"
MAX_MSGS = int(os.environ.get("GATEWAY_MAX_MSGS") or 60)
UPSTREAM = os.environ.get("GATEWAY_UPSTREAM") or "https://api.openai.com/v1"

RE_INJECT = re.compile(r"</?(?:system_reminder|cronjob)>.*?</(?:system_reminder|cronjob)>",
                       re.S | re.I)
RE_TAG = re.compile(r"\[(?:msg_id|cq):[^\]]*\]", re.I)
RE_IMG_PATH = re.compile(r"\[image \d+:[^\]]*\]", re.I)


def clean_text(t: str) -> str:
    t = RE_INJECT.sub("", t or "")
    t = RE_TAG.sub("", t)
    return RE_IMG_PATH.sub("[图片]", t).strip()


def strip_media(messages: list[dict]) -> list[dict]:
    """图片 base64 → [图片]。一次请求里图片能占 99% 的体积。"""
    out = []
    for m in messages:
        c = m.get("content")
        if isinstance(c, list):
            parts = []
            for p in c:
                if isinstance(p, dict) and p.get("type") in ("image_url", "image", "input_image"):
                    parts.append({"type": "text", "text": "[图片]"})
                else:
                    parts.append(p)
            m = dict(m, content=parts)
        elif isinstance(c, str) and c.startswith("data:image"):
            m = dict(m, content="[图片]")
        out.append(m)
    return out


def trim(messages: list[dict], limit: int = MAX_MSGS) -> list[dict]:
    """只留最近的 N 条，但不要把 tool 调用切成孤儿。"""
    if limit <= 0 or len(messages) <= limit:
        return messages
    systems = [m for m in messages if m.get("role") == "system"][:1]
    rest = [m for m in messages if m.get("role") != "system"]
    tail = rest[-(limit - len(systems)):] if limit > len(systems) else []
    while tail and (tail[0].get("role") == "tool"
                    or (tail[0].get("role") == "tool_calls")):
        tail.pop(0)
    return systems + tail


def apply_mode(messages: list[dict], block: str, mode: str | None = None) -> list[dict]:
    mode = mode or MODE
    msgs = [dict(m) for m in messages]
    if mode == "passthrough" or not block:
        return msgs
    idx = next((i for i, m in enumerate(msgs) if m.get("role") == "system"), None)
    if mode == "replace":
        if idx is None:
            msgs.insert(0, {"role": "system", "content": block})
        else:
            msgs[idx] = dict(msgs[idx], content=block)
        return msgs
    # enrich：追加，不覆盖
    if idx is None:
        msgs.insert(0, {"role": "system", "content": block})
    else:
        msgs[idx] = dict(msgs[idx], content=(msgs[idx].get("content") or "") + "\n\n" + block)
    return msgs


def forward(body: dict, auth: str | None, timeout: int = 180):
    """把请求原样转给上游；返回原始响应对象（流式时逐块读）。"""
    req = urllib.request.Request(
        UPSTREAM.rstrip("/") + "/chat/completions",
        data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json",
                 **({"Authorization": auth} if auth else {})},
        method="POST")
    # 显式禁用系统代理：否则会被本机的科学上网工具拖慢甚至打断
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return opener.open(req, timeout=timeout)


def log(path: str, line: str) -> None:
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{time.strftime('%H:%M:%S')} {line}\n")
    except Exception:
        pass
