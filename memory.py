"""AI 记忆笔记：从一轮对话里提取值得长期记住的信息。

纯逻辑，无 GUI 依赖。与 ingest.py 之于设定库同理——存储在 chatstore.py，
这里只负责调 AI 把对话浓缩成简短的记忆条目。
"""

import json
import re

from ai_client import AIError, _log_error, chat_once

MAX_NOTES = 5          # 单轮最多提取几条
NOTE_CHARS = 80        # 单条记忆的建议长度

_MEMORY_SYSTEM = (
    "你是写作助手的记忆模块。从这一轮对话中提取值得长期记住的信息，"
    "例如：用户的写作偏好与习惯、双方敲定的设定或剧情决定、待办与后续计划。"
    "只输出 JSON 对象，形如 {\"notes\": [\"...\", \"...\"]}，不要解释、不要代码围栏。"
    f"每条不超过 {NOTE_CHARS} 字，最多 {MAX_NOTES} 条，用陈述句写清楚主体。"
    "只记真正需要跨会话保留的内容；本轮没有值得记的就输出 {\"notes\": []}。"
    "不要把一次性的闲聊、寒暄或纯粹的正文续写记进来。"
)


def parse_notes(text: str) -> list:
    """从 AI 回复里稳健解析记忆条目；解析不出结构就返回空列表。"""
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()

    data = None
    try:
        data = json.loads(s)
    except json.JSONDecodeError:
        for opener in ("{", "["):
            i = s.find(opener)
            if i != -1:
                try:
                    data = json.loads(s[i:])
                    break
                except json.JSONDecodeError:
                    continue

    raw = []
    if isinstance(data, dict):
        for key in ("notes", "memories", "items"):
            if isinstance(data.get(key), list):
                raw = data[key]
                break
    elif isinstance(data, list):
        raw = data

    notes = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("content") or item.get("note") or item.get("text")
        note = str(item or "").strip()
        if note and note not in notes:
            notes.append(note)
    return notes[:MAX_NOTES]


def extract_notes(cfg: dict, user_text: str, assistant_text: str,
                  existing: list = None) -> list:
    """从一问一答里提取记忆条目；网络调用，应放后台线程。失败抛 AIError。"""
    user_text = (user_text or "").strip()
    assistant_text = (assistant_text or "").strip()
    if not user_text and not assistant_text:
        return []
    prompt = ""
    if existing:
        prompt += ("【已记住的内容（不要重复记录）】\n"
                   + "\n".join("- " + e for e in existing[:20]) + "\n\n")
    prompt += (f"【用户】\n{user_text}\n\n【助手】\n{assistant_text}\n\n"
               "请提取值得长期记住的信息。")
    try:
        reply = chat_once(cfg, _MEMORY_SYSTEM, prompt, max_tokens=600, json_mode=True)
    except AIError as e:
        if getattr(e, "status", None) != 400:
            raise
        _log_error("记忆提取：JSON 输出模式疑似不受支持(400)，回退普通请求", str(e))
        reply = chat_once(cfg, _MEMORY_SYSTEM, prompt, max_tokens=600)
    notes = parse_notes(reply)
    if not notes and reply.strip() and "[]" not in reply:
        _log_error("记忆提取未解析出条目，AI 原始返回：", reply[:1000])
    return notes
