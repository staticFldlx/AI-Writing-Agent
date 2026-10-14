"""AI 个性化回复客户端：OpenAI 兼容 chat/completions 接口（DeepSeek/Kimi/通义等通用）。

纯逻辑模块，无 GUI 依赖。配置与密钥存放在 %APPDATA%\\DiceBot\\，绝不写进代码或 exe。
仅用标准库 urllib，不增加打包体积。

人格是用户可编辑的数据（config.json 中的 personas 列表），DEFAULT_PERSONAS
只用于首次播种与"恢复预设"；上下文浓缩稿缓存对 UI 开放读写，用户可查看/修改/导出。
"""

import hashlib
import json
import os
import time
import urllib.error
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from datetime import datetime

CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "DiceBot")
CONFIG_PATH = os.path.join(CONFIG_DIR, "config.json")
CACHE_PATH = os.path.join(CONFIG_DIR, "context_cache.json")
ERROR_LOG_PATH = os.path.join(CONFIG_DIR, "ai_error.log")

MAX_FILE_BYTES = 2 * 1024 * 1024   # 上下文文件大小硬上限
MAX_CONTEXT_CHARS = 500_000        # 提取文本的硬上限
MAX_DIRECT_CHARS = 3000            # 超过此长度的背景文本需要先浓缩
CONDENSE_CHUNK = 28_000            # 长文档分块浓缩的块大小
HISTORY_IN_PROMPT = 5              # 每次请求携带的最近掷骰条数
REQUEST_TIMEOUT = 60               # 秒；推理类模型响应较慢
MAX_ATTEMPTS = 3                   # 5xx/网络错误的总尝试次数
BACKOFF = (1.5, 4.0)               # 每次重试前的等待秒数（指数退避）

DEFAULT_CONFIG = {
    "ai_enabled": False,
    "base_url": "https://api.deepseek.com/v1",
    "api_key": "",
    "model": "deepseek-chat",
    "reply_limit": 80,
    "context_file": "",
    "context_note": "",
}

# 仅作首次播种与"恢复预设"；运行时人格读 cfg["personas"]。
# 字数限制不写在人设里——由 reply_limit 设置统一注入。
DEFAULT_PERSONAS = [
    {
        "name": "元气骰子精灵",
        "prompt": "你是一只元气满满的骰子精灵，说话活泼可爱，喜欢用感叹词。"
                  "每次掷骰后，用一两句话对结果做出兴奋的反应或俏皮的吐槽；"
                  "如果有背景设定或补充说明，让回应贴合剧情。",
    },
    {
        "name": "毒舌损友",
        "prompt": "你是用户的毒舌损友，嘴上不饶人但其实关心对方。"
                  "每次掷骰后，用一两句话毒舌点评结果，运气差就嘲两句，运气好就酸两句；"
                  "如果有背景设定或补充说明，让吐槽贴合剧情。",
    },
    {
        "name": "高冷贵族",
        "prompt": "你是一位高冷优雅的贵族，言简意赅、惜字如金，"
                  "偶尔流露一丝不易察觉的关心。每次掷骰后用一两句矜持的话点评结果。",
    },
    {
        "name": "神秘占卜师",
        "prompt": "你是神秘的占卜师，把每一次掷骰都视为命运的启示，说话带着玄妙的隐喻。"
                  "每次掷骰后用一两句话解读结果背后的'天意'；"
                  "如果有背景设定或补充说明，把启示与剧情勾连起来。",
    },
]

# 旧版（第二期）配置的 persona_key → 人格名，用于迁移
_LEGACY_KEYS = {
    "sprite": "元气骰子精灵",
    "snark": "毒舌损友",
    "noble": "高冷贵族",
    "oracle": "神秘占卜师",
}

_CONDENSE_SYSTEM = "你是一个擅长提炼小说与设定文档的助手，输出精炼的条目式中文。"
_CONDENSE_PROMPT = (
    "请把下面的内容浓缩成一段背景提示词，供后续掷骰互动时让 AI 快速了解设定。"
    "保留：剧情主线、主要人物及关系、世界观或规则要点、当前进展。500 字以内。\n\n"
)
TRUNCATION_MARK = "……（回复被截断，可在 AI 设置中调大字数上限）"


class AIError(Exception):
    """AI 请求或文件处理失败，消息可直接展示给用户。status 记录 HTTP 状态码（若有）。"""

    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def _log_error(context: str, detail: str) -> None:
    """把错误细节追加到 ai_error.log，便于排查偶发问题；日志失败不影响主流程。"""
    try:
        os.makedirs(CONFIG_DIR, exist_ok=True)
        with open(ERROR_LOG_PATH, "a", encoding="utf-8") as f:
            f.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {context}\n{detail}\n\n")
    except OSError:
        pass


# ------------------------------------------------------------------ 配置

def _migrate(cfg: dict) -> None:
    """补齐 personas/active_persona，并迁移第二期的 persona_key/custom_persona。"""
    personas = cfg.get("personas")
    if not isinstance(personas, list) or not personas:
        personas = [dict(p) for p in DEFAULT_PERSONAS]
        legacy_custom = (cfg.get("custom_persona") or "").strip()
        if legacy_custom:
            personas.append({"name": "自定义", "prompt": legacy_custom})
        cfg["personas"] = personas
    cfg["personas"] = [
        {"name": str(p.get("name", "")).strip() or "未命名", "prompt": str(p.get("prompt", ""))}
        for p in cfg["personas"]
    ]
    active = cfg.get("active_persona")
    if not active:
        legacy = cfg.get("persona_key")
        if legacy == "custom" and any(p["name"] == "自定义" for p in cfg["personas"]):
            active = "自定义"
        else:
            active = _LEGACY_KEYS.get(legacy, cfg["personas"][0]["name"])
    if not any(p["name"] == active for p in cfg["personas"]):
        active = cfg["personas"][0]["name"]
    cfg["active_persona"] = active
    cfg.pop("persona_key", None)
    cfg.pop("custom_persona", None)


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, json.JSONDecodeError):
        pass
    _migrate(cfg)
    return cfg


def save_config(cfg: dict) -> None:
    os.makedirs(CONFIG_DIR, exist_ok=True)
    with open(CONFIG_PATH, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def _active_persona(cfg: dict) -> dict:
    personas = cfg.get("personas") or []
    name = cfg.get("active_persona", "")
    for p in personas:
        if p.get("name") == name:
            return p
    return personas[0] if personas else DEFAULT_PERSONAS[0]


def persona_name(cfg: dict) -> str:
    return _active_persona(cfg)["name"]


def system_prompt(cfg: dict) -> str:
    return (_active_persona(cfg).get("prompt") or "").strip() or DEFAULT_PERSONAS[0]["prompt"]


def reply_limit(cfg: dict) -> int:
    """回复字数上限（仅骰子端个性化回复用），夹紧到 [20, 2000]。"""
    try:
        n = int(cfg.get("reply_limit", 80))
    except (TypeError, ValueError):
        n = 80
    return min(2000, max(20, n))


# ------------------------------------------------------------ 上下文文件

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _extract_docx(path: str) -> str:
    try:
        with zipfile.ZipFile(path) as z:
            xml_data = z.read("word/document.xml")
        root = ET.fromstring(xml_data)
    except (zipfile.BadZipFile, KeyError, ET.ParseError) as e:
        raise AIError(f"无法解析 Word 文件：{e}") from e
    paragraphs = []
    for p in root.iter(f"{_W}p"):
        runs = [t.text or "" for t in p.iter(f"{_W}t")]
        if runs:
            paragraphs.append("".join(runs))
    return "\n".join(paragraphs)


def extract_text(path: str) -> str:
    """读取上下文文件文本；支持 .txt / .md / .docx。失败抛 AIError。"""
    try:
        if os.path.getsize(path) > MAX_FILE_BYTES:
            raise AIError(f"文件超过 {MAX_FILE_BYTES // (1024 * 1024)}MB 上限")
        if os.path.splitext(path)[1].lower() == ".docx":
            text = _extract_docx(path)
        else:
            with open(path, encoding="utf-8", errors="replace") as f:
                text = f.read()
    except AIError:
        raise
    except OSError as e:
        raise AIError(f"读取文件失败：{e}") from e
    text = text.strip()
    if not text:
        raise AIError("文件内容为空")
    return text[:MAX_CONTEXT_CHARS]


def _content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _load_cache() -> dict:
    try:
        with open(CACHE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict) -> None:
    os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
    with open(CACHE_PATH, "w", encoding="utf-8") as f:
        json.dump(cache, f, ensure_ascii=False, indent=2)


def digest_key(path: str) -> str:
    """浓缩稿缓存 key：文件路径 + 内容哈希（文件改动后 key 变化，自动失效）。"""
    return f"{os.path.abspath(path)}::{_content_hash(extract_text(path))}"


def get_cached_digest(path: str):
    """返回该文件当前内容对应的浓缩稿；无缓存返回 None。"""
    return _load_cache().get(digest_key(path))


def set_cached_digest(path: str, text: str) -> None:
    """写入/覆盖浓缩稿缓存——用户在 UI 中编辑后保存走这里。"""
    cache = _load_cache()
    cache[digest_key(path)] = text
    _save_cache(cache)


def _condense(cfg: dict, text: str, status_cb=None) -> str:
    chunks = [text[i:i + CONDENSE_CHUNK] for i in range(0, len(text), CONDENSE_CHUNK)]
    if len(chunks) == 1:
        return _chat(cfg, _CONDENSE_SYSTEM, _CONDENSE_PROMPT + chunks[0], max_tokens=900)[0]
    parts = []
    for i, chunk in enumerate(chunks, 1):
        if status_cb:
            status_cb(f"正在阅读背景文件 {i}/{len(chunks)}…")
        parts.append(_chat(
            cfg, _CONDENSE_SYSTEM,
            f"这是同一份长文档的第 {i}/{len(chunks)} 部分。{_CONDENSE_PROMPT}{chunk}",
            max_tokens=600,
        )[0])
    return _chat(
        cfg, _CONDENSE_SYSTEM,
        "下面是同一份文档各部分的摘要，请合并成一段统一的背景提示词，"
        "条目式中文，500 字以内。\n\n" + "\n\n".join(parts),
        max_tokens=900,
    )[0]


_RECAP_SYSTEM = (
    "你是小说编辑助手，擅长把长篇前文压缩成前情提要，"
    "供后续写作时快速回忆剧情。输出条理清晰的中文，不要额外解释。"
)
_RECAP_PROMPT = (
    "请把下面的前文内容写成前情提要，保留：主线进展、出场人物及其处境与关系、"
    "关键伏笔与未解决的悬念、当前所处的时间地点。800 字以内。\n\n"
)


def condense_recap(cfg: dict, text: str, prev_summary: str = "", status_cb=None) -> str:
    """把前文浓缩成前情提要；prev_summary 非空时在其基础上增量更新。

    网络调用，应放后台线程。长文自动分块后合并。
    """
    text = (text or "").strip()
    if not text:
        raise AIError("没有可浓缩的前文")
    prev = (prev_summary or "").strip()
    prefix = (f"【已有前情提要】\n{prev}\n\n"
              "请在上述提要基础上，结合下面的新内容更新出一份完整的前情提要"
              "（合并重复、补充新进展、修正已被推翻的内容）。\n\n") if prev else ""

    chunks = [text[i:i + CONDENSE_CHUNK] for i in range(0, len(text), CONDENSE_CHUNK)]
    if len(chunks) == 1:
        return _chat(cfg, _RECAP_SYSTEM, prefix + _RECAP_PROMPT + chunks[0],
                     max_tokens=2000)[0]

    parts = []
    for i, chunk in enumerate(chunks, 1):
        if status_cb:
            status_cb(f"正在阅读前文 {i}/{len(chunks)}…")
        parts.append(_chat(
            cfg, _RECAP_SYSTEM,
            f"这是同一份前文的第 {i}/{len(chunks)} 部分。{_RECAP_PROMPT}{chunk}",
            max_tokens=1200,
        )[0])
    if status_cb:
        status_cb("正在合并前情提要…")
    return _chat(
        cfg, _RECAP_SYSTEM,
        prefix + "下面是同一份前文各部分的提要，请合并成一份统一的前情提要，"
        "按时间顺序组织，800 字以内。\n\n" + "\n\n".join(parts),
        max_tokens=2000,
    )[0]


_OVERVIEW_SYSTEM = (
    "你是世界观编辑，擅长把零散的设定词条归纳成一份整体总览，"
    "供后续笼统讨论时快速把握全局。输出条理清晰的中文，不要额外解释。"
)
_OVERVIEW_PROMPT = (
    "请把下面的设定词条归纳成一份世界观总览，涵盖：整体基调与题材、"
    "世界规则与核心设定、主要势力及其关系、关键人物及其立场、"
    "重要地点与时间线要点。突出彼此之间的关联，不要逐条罗列。1000 字以内。\n\n"
)


def condense_overview(cfg: dict, entries_text: str, prev_summary: str = "",
                      status_cb=None) -> str:
    """把全部设定词条归纳成世界观总览；prev_summary 非空时在其基础上更新。

    网络调用，应放后台线程。词条很多时自动分块后合并。
    """
    entries_text = (entries_text or "").strip()
    if not entries_text:
        raise AIError("设定库里还没有词条，无法生成总览")
    prev = (prev_summary or "").strip()
    prefix = (f"【已有总览】\n{prev}\n\n"
              "请在上述总览基础上，结合下面的词条更新出一份完整的世界观总览"
              "（补充新增设定、修正已变化的内容）。\n\n") if prev else ""

    chunks = [entries_text[i:i + CONDENSE_CHUNK]
              for i in range(0, len(entries_text), CONDENSE_CHUNK)]
    if len(chunks) == 1:
        return _chat(cfg, _OVERVIEW_SYSTEM, prefix + _OVERVIEW_PROMPT + chunks[0],
                     max_tokens=2500)[0]

    parts = []
    for i, chunk in enumerate(chunks, 1):
        if status_cb:
            status_cb(f"正在归纳设定 {i}/{len(chunks)}…")
        parts.append(_chat(
            cfg, _OVERVIEW_SYSTEM,
            f"这是设定库的第 {i}/{len(chunks)} 部分。{_OVERVIEW_PROMPT}{chunk}",
            max_tokens=1500,
        )[0])
    if status_cb:
        status_cb("正在合并总览…")
    return _chat(
        cfg, _OVERVIEW_SYSTEM,
        prefix + "下面是设定库各部分的归纳，请合并成一份统一的世界观总览，"
        "1000 字以内。\n\n" + "\n\n".join(parts),
        max_tokens=2500,
    )[0]


def condense_file(cfg: dict, path: str, status_cb=None) -> str:
    """强制（重新）浓缩文件并写入缓存，返回浓缩稿。网络调用，应放后台线程。"""
    text = extract_text(path)
    digest = _condense(cfg, text, status_cb)
    cache = _load_cache()
    cache[f"{os.path.abspath(path)}::{_content_hash(text)}"] = digest
    _save_cache(cache)
    return digest


def get_context(cfg: dict, path: str, status_cb=None) -> str:
    """返回可直接进 prompt 的背景文本：短文件用原文，长文件用浓缩稿（带缓存）。

    长文件首次加载会发起 AI 请求，调用方应放在后台线程。
    """
    text = extract_text(path)
    if len(text) <= MAX_DIRECT_CHARS:
        return text
    key = f"{os.path.abspath(path)}::{_content_hash(text)}"
    cached = _load_cache().get(key)
    if cached:
        return cached
    if status_cb:
        status_cb("背景文件较长，正在请 AI 浓缩设定…")
    digest = _condense(cfg, text, status_cb)
    cache = _load_cache()
    cache[key] = digest
    _save_cache(cache)
    return digest


# ------------------------------------------------------------------ 请求

def normalize_base_url(url: str) -> str:
    """把用户填的 API 地址规整成基址（不含 /chat/completions）。

    容忍直接从官方文档整段粘贴的完整请求地址，避免拼成
    .../chat/completions/chat/completions 而 404。
    """
    base = (url or "").strip().rstrip("/")
    for suffix in ("/chat/completions", "/chat/completion"):
        if base.lower().endswith(suffix):
            base = base[:-len(suffix)]
            break
    return base.rstrip("/")


def _post(cfg: dict, messages: list, max_tokens: int, response_format=None,
          tools=None, tool_choice=None, want_message=False) -> tuple:
    """向 chat/completions 发一次请求，返回 (内容, finish_reason)。

    messages 为完整消息列表（支持多轮）。response_format 可传
    {"type":"json_object"} 强制 JSON 输出。网络类错误自动重试一次；
    空内容与所有失败细节写入 ai_error.log。
    """
    base = normalize_base_url(cfg.get("base_url"))
    if not base:
        raise AIError("未配置 API 地址（见 AI 设置）")
    if not cfg.get("api_key"):
        raise AIError("未配置 API 密钥（见 AI 设置）")
    url = base + "/chat/completions"
    model = cfg.get("model") or DEFAULT_CONFIG["model"]
    body = {"model": model, "messages": messages, "max_tokens": max_tokens}
    if response_format:
        body["response_format"] = response_format
    if tools:
        body["tools"] = tools
        body["tool_choice"] = tool_choice or "auto"
    payload = json.dumps(body).encode("utf-8")
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {cfg['api_key']}",
    }

    data = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        req = urllib.request.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            break
        except urllib.error.HTTPError as e:
            try:
                body = e.read().decode("utf-8", "replace")
            except OSError:
                body = ""
            _log_error(f"HTTP {e.code} {url} model={model}", body[:2000])
            if e.code >= 500 and attempt < MAX_ATTEMPTS:
                # 服务端过载（如 Gemini 的 503 high demand）：退避后重试
                time.sleep(BACKOFF[attempt - 1])
                continue
            hint = ""
            if e.code in (429, 503):
                hint = (f"\n该模型当前繁忙或已限流（已自动重试 {MAX_ATTEMPTS} 次）。"
                        "可稍后再试，或在 AI 设置里换一个模型/服务商。")
            if e.code == 404:
                hint = (f"\n请求的是 {url}。"
                        "请检查 AI 设置里的「API 地址」是否填到基址为止"
                        "（如 https://open.bigmodel.cn/api/paas/v4），以及模型名是否正确。")
            raise AIError(
                f"API 返回错误 {e.code}：{body[:160] or e.reason}（详情见 ai_error.log）{hint}",
                status=e.code,
            ) from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < MAX_ATTEMPTS:
                time.sleep(BACKOFF[attempt - 1])
                continue
            _log_error(f"网络错误 {url} model={model}", repr(e))
            reason = getattr(e, "reason", e)
            raise AIError(f"网络请求失败：{reason}"
                          f"（已重试 {MAX_ATTEMPTS} 次，详情见 ai_error.log）") from e
        except json.JSONDecodeError as e:
            _log_error(f"响应非 JSON {url} model={model}", repr(e))
            raise AIError("API 响应不是有效的 JSON（详情见 ai_error.log）") from e
    try:
        choice = data["choices"][0]
        message = choice.get("message") or {}
        content = (message.get("content") or "").strip()
        finish = choice.get("finish_reason") or ""
    except (KeyError, IndexError, TypeError) as e:
        _log_error(f"响应结构异常 {url} model={model}", str(data)[:2000])
        raise AIError("无法解析 API 响应（详情见 ai_error.log）") from e
    if want_message:
        # 工具调用轮次里 content 可以为空（模型只返回 tool_calls），不视为异常
        if not content and not message.get("tool_calls"):
            _log_error(f"空内容响应 {url} model={model}", str(data)[:2000])
            raise AIError("模型返回了空内容（完整响应见 ai_error.log）")
        return message, finish
    if not content:
        # 空内容常见于：模型名不对、推理类模型额度耗在思考、端点不支持该请求
        _log_error(f"空内容响应 {url} model={model}", str(data)[:2000])
        raise AIError(
            "模型返回了空内容——请检查 AI 设置里的模型名是否正确，"
            "或换一个模型/调大回复字数上限（完整响应见 ai_error.log）"
        )
    return content, finish


def _chat(cfg: dict, system: str, user: str, max_tokens: int = 400) -> tuple:
    """单轮 system+user 请求，返回 (内容, finish_reason)。"""
    return _post(cfg, [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], max_tokens)


def chat_once(cfg: dict, system: str, user: str, max_tokens: int = 800,
              json_mode: bool = False) -> str:
    """单轮请求，只取文本（录入/提取等用）。json_mode 强制 JSON 输出。失败抛 AIError。"""
    rf = {"type": "json_object"} if json_mode else None
    return _post(cfg, [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ], max_tokens, response_format=rf)[0]


def chat_with_tools(cfg: dict, messages: list, tools: list,
                    max_tokens: int = 3000) -> tuple:
    """带 function calling 的对话，返回 (message, finish_reason)。

    message 是完整的助手消息（可能含 tool_calls）。端点不支持 tools 时
    会抛出带 status=400 的 AIError，调用方可回退到普通对话。
    """
    return _post(cfg, messages, max_tokens, tools=tools, want_message=True)


def chat_messages(cfg: dict, messages: list, max_tokens: int = 3000) -> tuple:
    """多轮对话请求（写作/讨论用，不套回复字数限制），返回 (内容, finish_reason)。"""
    return _post(cfg, messages, max_tokens)


def finalize_reply(text: str, finish_reason: str) -> str:
    """finish_reason 为 length 说明被 token 上限截断，补上明确标注。"""
    if finish_reason == "length":
        return text + TRUNCATION_MARK
    return text


def build_user_message(record: dict, history: list,
                       context_text: str = "", note: str = "") -> str:
    """组装单次掷骰的 user 消息：背景设定 → 补充说明 → 最近历史 → 本次掷骰。"""
    parts = []
    if context_text:
        parts.append("【背景设定】\n" + context_text)
    if note:
        parts.append("【补充说明】\n" + note)
    prev = [r for r in history if r is not record][-HISTORY_IN_PROMPT:]
    if prev:
        lines = []
        for r in prev:
            line = f"- {r['description']}（1d{r['faces']}）→ {r['result']}"
            opts = r.get("options")
            if opts and r.get("hit_index") is not None:
                line += f"（{opts[r['hit_index']].display()}）"
            lines.append(line)
        parts.append("【之前的掷骰】\n" + "\n".join(lines))
    cur = f"{record['description']}（1d{record['faces']}）"
    opts = record.get("options")
    if opts:
        cur += "\n选项：" + " / ".join(o.display() for o in opts)
    cur += f"\n掷出：{record['result']}"
    if opts and record.get("hit_index") is not None:
        cur += f" → 命中：{opts[record['hit_index']].display()}"
    parts.append("【本次掷骰】\n" + cur)
    parts.append("请按你的人设，对本次掷骰结果做出回应。")
    return "\n\n".join(parts)


def generate_reply(cfg: dict, record: dict, history: list,
                   context_text: str = "", note: str = "") -> str:
    """生成本次掷骰的 AI 回复；网络调用，应放后台线程。失败抛 AIError。"""
    limit = reply_limit(cfg)
    system = (
        system_prompt(cfg)
        + f"\n你的回复必须控制在 {limit} 字以内，并保证语义完整、自然收尾。"
    )
    text, finish = _chat(
        cfg, system, build_user_message(record, history, context_text, note),
        max_tokens=max(200, limit * 4),
    )
    return finalize_reply(text, finish)
