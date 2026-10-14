"""写作助手的 Agent 工具：让 AI 通过 function calling 读写设定库。

纯逻辑，无 GUI 依赖、可脱网单测。安全约定：
- 读工具（search/get/list）由程序自动执行；
- 写工具（create/update/delete）一律先经用户审批，调用方负责把关。
execute() 对任何非法输入都返回可读的错误文本而非抛异常，让模型能自我纠正。
"""

from knowledge import SPECIAL_CATEGORIES

WRITE_TOOLS = {"create_entry", "update_entry", "delete_entry"}

_CATEGORY_HINT = "人物/地点/势力/物品/事件/规则/其他"

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "search_entries",
            "description": "在设定库中按关键字检索词条，返回匹配的词条摘要（含 id）。"
                           "想了解某个设定的细节、或修改前确认目标词条时使用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "检索关键字，可为空表示列出全部"},
                    "category": {"type": "string", "description": f"可选，限定类别：{_CATEGORY_HINT}"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_entry",
            "description": "按 id 读取单个词条的完整内容。",
            "parameters": {
                "type": "object",
                "properties": {"entry_id": {"type": "integer", "description": "词条 id"}},
                "required": ["entry_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_categories",
            "description": "列出设定库中已有的类别及各类别的词条数量。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "create_entry",
            "description": "新建一个设定词条。需要用户批准后才会真正写入。",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string", "description": "词条名称"},
                    "category": {"type": "string", "description": f"类别：{_CATEGORY_HINT}"},
                    "content": {"type": "string", "description": "词条正文"},
                    "tags": {"type": "string", "description": "可选，逗号分隔的标签"},
                    "aliases": {"type": "string", "description": "可选，逗号分隔的别名"},
                },
                "required": ["title", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "update_entry",
            "description": "修改已有词条。只传需要改的字段。需要用户批准后才会真正写入。",
            "parameters": {
                "type": "object",
                "properties": {
                    "entry_id": {"type": "integer", "description": "要修改的词条 id"},
                    "title": {"type": "string"},
                    "category": {"type": "string", "description": f"类别：{_CATEGORY_HINT}"},
                    "content": {"type": "string"},
                    "tags": {"type": "string"},
                    "aliases": {"type": "string"},
                },
                "required": ["entry_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "delete_entry",
            "description": "删除一个词条。需要用户批准后才会真正删除，请谨慎使用。",
            "parameters": {
                "type": "object",
                "properties": {"entry_id": {"type": "integer", "description": "词条 id"}},
                "required": ["entry_id"],
            },
        },
    },
]

READ_SCHEMAS = [t for t in TOOL_SCHEMAS if t["function"]["name"] not in WRITE_TOOLS]


def is_write(name: str) -> bool:
    return name in WRITE_TOOLS


def schemas(allow_write: bool = True) -> list:
    """按是否允许写库返回工具列表。"""
    return TOOL_SCHEMAS if allow_write else READ_SCHEMAS


def _brief(e: dict, chars: int = 80) -> str:
    body = " ".join((e.get("content") or "").split())
    if len(body) > chars:
        body = body[:chars] + "…"
    return f"#{e['id']} [{e.get('category') or '未分类'}] {e['title']}：{body}"


def describe(name: str, args: dict, kb=None) -> str:
    """把一次写操作渲染成给用户看的一句话（审批面板用）。"""
    if name == "create_entry":
        return (f"新建词条「{args.get('title', '?')}」"
                f"（类别：{args.get('category') or '未分类'}）")
    if name == "update_entry":
        eid = args.get("entry_id")
        title = ""
        if kb is not None:
            e = kb.get(eid) if isinstance(eid, int) else None
            title = f"「{e['title']}」" if e else ""
        fields = [k for k in ("title", "category", "content", "tags", "aliases")
                  if k in args]
        return f"修改词条 #{eid}{title}，涉及字段：{', '.join(fields) or '无'}"
    if name == "delete_entry":
        eid = args.get("entry_id")
        title = ""
        if kb is not None:
            e = kb.get(eid) if isinstance(eid, int) else None
            title = f"「{e['title']}」" if e else ""
        return f"删除词条 #{eid}{title}"
    return f"{name}({args})"


def _as_int(value):
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def execute(kb, name: str, args: dict) -> str:
    """执行一次工具调用，返回给模型看的结果文本。不抛异常。"""
    args = args or {}
    try:
        if name == "search_entries":
            query = str(args.get("query") or "").strip()
            category = str(args.get("category") or "").strip() or None
            results = [e for e in kb.search(query, category=category)
                       if e["category"] not in SPECIAL_CATEGORIES]
            if not results:
                return "没有匹配的词条。"
            return "找到 %d 条：\n%s" % (
                len(results), "\n".join(_brief(e) for e in results[:20]))

        if name == "get_entry":
            eid = _as_int(args.get("entry_id"))
            if eid is None:
                return "错误：entry_id 必须是整数。"
            e = kb.get(eid)
            if not e:
                return f"错误：不存在 id 为 {eid} 的词条。"
            return (f"#{e['id']} {e['title']}\n类别：{e['category'] or '未分类'}\n"
                    f"标签：{e['tags'] or '无'}\n别名：{e['aliases'] or '无'}\n"
                    f"正文：{e['content']}")

        if name == "list_categories":
            counts = {}
            for e in kb.normal_entries():
                counts[e["category"] or "未分类"] = counts.get(e["category"] or "未分类", 0) + 1
            if not counts:
                return "设定库为空。"
            return "、".join(f"{k}（{v} 条）" for k, v in sorted(counts.items()))

        if name == "create_entry":
            title = str(args.get("title") or "").strip()
            if not title:
                return "错误：title 不能为空。"
            new_id = kb.add(
                title, category=str(args.get("category") or "其他"),
                content=str(args.get("content") or ""),
                tags=str(args.get("tags") or ""), aliases=str(args.get("aliases") or ""),
                source="AI 助手")
            return f"已新建词条 #{new_id}「{title}」。"

        if name == "update_entry":
            eid = _as_int(args.get("entry_id"))
            if eid is None:
                return "错误：entry_id 必须是整数。"
            if not kb.get(eid):
                return f"错误：不存在 id 为 {eid} 的词条。"
            fields = {k: str(args[k]) for k in
                      ("title", "category", "content", "tags", "aliases") if k in args}
            if not fields:
                return "错误：没有提供任何要修改的字段。"
            kb.update(eid, **fields)
            return f"已更新词条 #{eid}，修改字段：{', '.join(fields)}。"

        if name == "delete_entry":
            eid = _as_int(args.get("entry_id"))
            if eid is None:
                return "错误：entry_id 必须是整数。"
            e = kb.get(eid)
            if not e:
                return f"错误：不存在 id 为 {eid} 的词条。"
            kb.delete(eid)
            return f"已删除词条 #{eid}「{e['title']}」。"

        return f"错误：未知工具 {name}。"
    except Exception as e:                    # 任何意外都返回文本，避免中断 agent 循环
        return f"错误：执行 {name} 时出错——{e}"
