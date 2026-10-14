"""AI 录入：把原文/对话记录提取成结构化设定词条，并对既有库去重分类。

纯逻辑，无 GUI 依赖。JSON 解析与去重分类可脱离网络单测。
"""

import json
import re
import time

from ai_client import AIError, _log_error, chat_once

EXTRACT_CHUNK = 6000       # 提取时的输入分块（比浓缩块小，减少单次输出、降低截断风险）
EXTRACT_MAX_TOKENS = 8000  # 给思考型模型（如 Gemini Flash）的思考+输出留足空间

_EXTRACT_SYSTEM = (
    "你是设定整理助手，从文本中提取世界观设定词条。"
    "直接以 JSON 对象输出，不要输出任何思考过程、解释或代码围栏。形如 "
    '{"entries":[{"title":"名称","category":"类别","content":"正文",'
    '"tags":"标签1, 标签2","aliases":"别名1, 别名2"}]}。'
    "category 从 人物/地点/势力/物品/事件/规则/其他 中选最贴切的。"
    "content 用简洁中文概括该设定的关键信息，控制在 200 字以内。"
    "没有别名或标签就留空字符串。"
    "只提取有价值的设定条目，不要把对话本身或无关内容也做成词条。"
    "如果没有可提取的设定，输出 {\"entries\":[]}。"
)


def _extract_chunk(cfg: dict, user: str) -> str:
    """请求一块文本的提取结果：先试 JSON 输出模式，端点不支持(400)才回退普通请求。

    5xx/超时等非格式问题直接抛出，避免过载时又发一次徒增压力。
    """
    try:
        return chat_once(cfg, _EXTRACT_SYSTEM, user,
                         max_tokens=EXTRACT_MAX_TOKENS, json_mode=True)
    except AIError as e:
        if getattr(e, "status", None) != 400:
            raise
        _log_error("JSON 输出模式疑似不受支持(400)，回退普通请求", str(e))
        return chat_once(cfg, _EXTRACT_SYSTEM, user, max_tokens=EXTRACT_MAX_TOKENS)


def _try_load(s: str):
    try:
        return json.loads(s)
    except json.JSONDecodeError:
        return None


def _salvage_objects(s: str) -> list:
    """从（可能被截断的）文本中扫出所有已完整的 {...} 对象并逐个解析。"""
    objs, stack = [], []
    in_str = esc = False
    for i, ch in enumerate(s):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            stack.append(i)
        elif ch == "}" and stack:
            obj = _try_load(s[stack.pop():i + 1])
            if isinstance(obj, dict):
                objs.append(obj)
    return objs


def parse_entries_json(text: str) -> list:
    """稳健解析词条：剥离围栏 → 直接解析 → 定位 JSON → 截断时抢救完整对象。"""
    s = (text or "").strip()
    if s.startswith("```"):
        s = re.sub(r"^```[a-zA-Z]*\s*", "", s)
        s = re.sub(r"\s*```$", "", s).strip()

    data = _try_load(s)
    if data is None:
        for opener in ("{", "["):
            i = s.find(opener)
            if i != -1 and (data := _try_load(s[i:])) is not None:
                break
    if data is None:
        # 被截断或夹带思考过程：抢救所有已完整的词条对象（只留含 title 的叶子对象）
        data = [o for o in _salvage_objects(s) if o.get("title") or o.get("name")]
        if not data:
            raise AIError("AI 返回的内容不是合法 JSON（无法解析，可能被截断），详情见 ai_error.log")

    if isinstance(data, dict):
        data = data["entries"] if isinstance(data.get("entries"), list) else [data]
    if not isinstance(data, list):
        raise AIError("AI 返回的内容不是合法 JSON 词条数组")

    out = []
    for item in data:
        if not isinstance(item, dict):
            continue
        title = str(item.get("title") or item.get("name") or "").strip()
        if not title:
            continue
        out.append({
            "title": title,
            "category": str(item.get("category") or "").strip(),
            "content": str(item.get("content") or "").strip(),
            "tags": _join(item.get("tags")),
            "aliases": _join(item.get("aliases")),
        })
    return out


def _join(value) -> str:
    if isinstance(value, (list, tuple)):
        return ", ".join(str(v).strip() for v in value if str(v).strip())
    return str(value or "").strip()


class PartialExtraction(AIError):
    """长文分批提取中途失败：带上已成功的结果与下一批下标，供"继续提取"续传。"""

    def __init__(self, message, entries, next_index, total):
        super().__init__(message)
        self.entries = entries
        self.next_index = next_index
        self.total = total


def split_chunks(raw_text: str, chunk_size: int = None) -> list:
    size = max(500, int(chunk_size or EXTRACT_CHUNK))
    return [raw_text[i:i + size] for i in range(0, len(raw_text), size)]


def extract_entries(cfg: dict, raw_text: str, source: str = "", status_cb=None,
                    chunk_size: int = None, start_index: int = 0,
                    seed: list = None, pause: float = 0.4) -> list:
    """调用 AI 把原文提取成词条提案列表；长文分块后合并（按名称去重）。

    status_cb(msg) 回报分批进度。中途失败抛 PartialExtraction，携带已提取的
    结果与下一批下标——调用方可用 start_index/seed 续传，不必从头重来。
    解析失败时把 AI 原始返回写入 ai_error.log。
    """
    raw_text = (raw_text or "").strip()
    if not raw_text:
        raise AIError("没有可提取的文本")
    chunks = split_chunks(raw_text, chunk_size)
    total = len(chunks)
    merged = list(seed or [])
    seen = {e["title"].lower() for e in merged}

    def fail(exc, index):
        """有已提取结果时抛可续传的 PartialExtraction，否则原样抛出。"""
        if merged or index > 0:
            raise PartialExtraction(str(exc), merged, index, total) from exc
        raise exc

    for idx in range(start_index, total):
        if status_cb:
            status_cb(f"正在提取词条 {idx + 1}/{total}…（已得 {len(merged)} 条）")
        hint = f"（这是长文档的第 {idx + 1}/{total} 部分）\n" if total > 1 else ""
        try:
            reply = _extract_chunk(cfg, hint + chunks[idx])
        except AIError as e:
            fail(e, idx)
        try:
            parsed = parse_entries_json(reply)
        except AIError as e:
            _log_error("词条提取 JSON 解析失败，AI 原始返回：", (reply or "")[:2000])
            fail(e, idx)
        for item in parsed:
            key = item["title"].lower()
            if key in seen:
                continue
            seen.add(key)
            item["source"] = source
            merged.append(item)
        if pause and idx + 1 < total:
            time.sleep(pause)      # 批间短停，降低触发限流的概率
    return merged


def classify_proposals(proposals: list, kb) -> list:
    """给每条提案标 new / duplicate，并附带命中的既有词条。"""
    result = []
    for p in proposals:
        matches = kb.find_similar(p["title"], p.get("aliases", ""))
        result.append({
            **p,
            "status": "duplicate" if matches else "new",
            "matches": matches,
        })
    return result


def merge_entry(cfg: dict, existing: dict, proposed: dict) -> str:
    """AI 辅助增补：把新信息并入既有词条正文，返回整合后的正文。"""
    system = (
        "你是设定整理助手。把新增信息并入已有词条，保留原有要点、补充新内容、"
        "去除重复矛盾，只输出整合后的完整正文，不要额外解释或标题。"
    )
    user = (
        f"已有词条【{existing['title']}】的正文：\n{existing.get('content', '')}\n\n"
        f"新增信息：\n{proposed.get('content', '')}\n\n请输出整合后的完整正文。"
    )
    return chat_once(cfg, system, user, max_tokens=1500)
