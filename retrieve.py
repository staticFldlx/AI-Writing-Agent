"""设定库自动召回：让 AI 从词条清单里挑出与本轮输入真正相关的条目。

纯逻辑，无 GUI 依赖。目标是替代"手动勾选"——词条一多就无法手动管理，
且模糊描述靠字面检索会漏。清单只带标题+摘要，token 开销可控。
"""

import json
import re

from ai_client import AIError, _log_error, chat_once

SUMMARY_CHARS = 60        # 清单里每条摘要的截断长度
MAX_LIST_ENTRIES = 150    # 送给 AI 的清单最多带多少条（超出先粗筛）
MAX_PICK = 12             # 最多召回多少条

_PICK_SYSTEM = (
    "你是写作助手的检索模块。根据用户这一轮的写作请求，"
    "从设定清单里挑出确实相关、能帮上忙的条目。"
    "只输出 JSON 对象，形如 {\"ids\": [1, 5]}，不要解释、不要代码围栏。"
    "宁缺毋滥：只挑真正相关的；没有相关条目就输出 {\"ids\": []}。"
    f"最多挑 {MAX_PICK} 条。"
)


def summarize(entry: dict, chars: int = SUMMARY_CHARS) -> str:
    """把词条正文压成一行摘要，供清单使用。"""
    text = " ".join((entry.get("content") or "").split())
    return text[:chars] + ("…" if len(text) > chars else "")


def build_catalog(entries: list, chars: int = SUMMARY_CHARS) -> str:
    """把词条列表压成紧凑清单：#id [类别] 标题（别名） — 摘要"""
    lines = []
    for e in entries:
        alias = (e.get("aliases") or "").strip()
        alias_part = f"（别名：{alias}）" if alias else ""
        lines.append(
            f"#{e['id']} [{e.get('category') or '设定'}] {e['title']}{alias_part}"
            f" — {summarize(e, chars)}"
        )
    return "\n".join(lines)


def parse_ids(text: str, valid_ids=None) -> list:
    """从 AI 回复中稳健解析出 id 列表；解析不出结构就退化为扫描数字。"""
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
        for key in ("ids", "id", "entries", "result"):
            if isinstance(data.get(key), list):
                raw = data[key]
                break
    elif isinstance(data, list):
        raw = data
    if not raw:
        raw = re.findall(r"\d+", s)   # 兜底：直接扫数字

    out = []
    for item in raw:
        if isinstance(item, dict):
            item = item.get("id")
        try:
            n = int(item)
        except (TypeError, ValueError):
            continue
        if valid_ids is not None and n not in valid_ids:
            continue
        if n not in out:
            out.append(n)
    return out


def mention_ids(entries: list, user_text: str) -> set:
    """标题或别名在输入里直接出现的词条（便宜且高置信，作粗筛与兜底）。"""
    hits = set()
    for e in entries:
        names = [e["title"]] + [
            a.strip() for a in (e.get("aliases") or "").replace("，", ",").split(",")
        ]
        if any(n and n in user_text for n in names):
            hits.add(e["id"])
    return hits


def prefilter(entries: list, user_text: str, limit: int = MAX_LIST_ENTRIES) -> list:
    """词条过多时先粗筛：直接被提及的优先，其余按最近更新补足。"""
    if len(entries) <= limit:
        return entries
    hits = mention_ids(entries, user_text)
    picked = [e for e in entries if e["id"] in hits]
    rest = [e for e in entries if e["id"] not in hits]
    rest.sort(key=lambda e: e.get("updated_at") or "", reverse=True)
    return (picked + rest)[:limit]


def pick_relevant(cfg: dict, user_text: str, entries: list,
                  max_pick: int = MAX_PICK, status_cb=None) -> list:
    """返回 AI 认为与本轮输入相关的词条 id 列表。网络调用，应放后台线程。

    失败抛 AIError，调用方可退回 mention_ids 兜底。
    """
    entries = [e for e in entries if e.get("title")]
    if not entries or not (user_text or "").strip():
        return []
    candidates = prefilter(entries, user_text)
    if status_cb:
        status_cb(f"正在从 {len(candidates)} 条设定中挑选相关内容…")
    catalog = build_catalog(candidates)
    user = (
        "【设定清单】\n" + catalog
        + "\n\n【用户这一轮的写作请求】\n" + user_text.strip()
        + "\n\n请挑出相关条目的 id。"
    )
    try:
        reply = chat_once(cfg, _PICK_SYSTEM, user, max_tokens=500, json_mode=True)
    except AIError as e:
        if getattr(e, "status", None) != 400:
            raise
        _log_error("召回：JSON 输出模式疑似不受支持(400)，回退普通请求", str(e))
        reply = chat_once(cfg, _PICK_SYSTEM, user, max_tokens=500)
    valid = {e["id"] for e in candidates}
    ids = parse_ids(reply, valid)
    if not ids and reply.strip():
        _log_error("自动召回未解析出 id，AI 原始返回：", reply[:1000])
    return ids[:max_pick]
