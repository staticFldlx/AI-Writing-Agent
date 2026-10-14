"""dice / log_manager / ai_client 的快速自测：python test_logic.py（无第三方依赖，不发真实网络请求）"""

import json
import os
import tempfile
import zipfile
from datetime import datetime

import ai_client
import dice
import agent_tools
import chatstore
import ingest
import memory
import retrieve
from ai_client import AIError, build_user_message
from dice import Option, OptionParseError, check_options, match_option, validate_faces
from ingest import classify_proposals, extract_entries, parse_entries_json
from knowledge import KnowledgeBase
from log_manager import LogError, LogManager, format_record


# 模块级隔离：任何测试都不得写用户真实的 %APPDATA%\DiceBot（含 ai_error.log）
_ISO = tempfile.mkdtemp()
ai_client.CONFIG_DIR = _ISO
ai_client.CONFIG_PATH = os.path.join(_ISO, "config.json")
ai_client.CACHE_PATH = os.path.join(_ISO, "context_cache.json")
ai_client.ERROR_LOG_PATH = os.path.join(_ISO, "ai_error.log")


def expect_error(fn, keyword):
    try:
        fn()
    except (OptionParseError, ValueError, LogError, AIError) as e:
        assert keyword in str(e), f"错误信息应包含 {keyword!r}，实际：{e}"
        return
    raise AssertionError(f"应当抛出包含 {keyword!r} 的错误，但没有抛出")


def test_check_options():
    check_options([Option(1, 1, "A"), Option(2, 2, "B")], 2)
    check_options([Option(1, 3, "甲"), Option(4, 6, "乙")], 6)
    assert Option(1, 3, "甲").range_label() == "1-3"
    assert Option(4, 6, "乙").display() == "4-6. 乙"
    assert Option(5, 5, "X").range_label() == "5"

    expect_error(lambda: check_options([Option(1, 3, "A"), Option(3, 6, "B")], 6), "重叠")
    expect_error(lambda: check_options([Option(1, 2, "A"), Option(4, 6, "B")], 6), "缺少")
    expect_error(lambda: check_options([Option(1, 7, "A")], 6), "超出")
    expect_error(lambda: check_options([], 6), "不能为空")
    expect_error(lambda: check_options([Option(3, 1, "A")], 6), "起点大于终点")

    # 边界：1d1 与 1d100
    check_options([Option(1, 1, "唯一")], 1)
    check_options([Option(1, 50, "前半"), Option(51, 100, "后半")], 100)


def test_faces_and_roll():
    assert validate_faces("100") == 100
    assert validate_faces(" 42 ") == 42
    expect_error(lambda: validate_faces("0"), "1-100")
    expect_error(lambda: validate_faces("101"), "1-100")
    expect_error(lambda: validate_faces("abc"), "整数")

    assert all(dice.roll(1) == 1 for _ in range(20))
    assert all(1 <= dice.roll(6) <= 6 for _ in range(500))
    assert all(1 <= dice.roll(100) <= 100 for _ in range(500))


def test_match():
    opts = [Option(1, 3, "A"), Option(4, 6, "B")]
    assert match_option(opts, 1) == 0
    assert match_option(opts, 3) == 0
    assert match_option(opts, 4) == 1
    assert match_option(opts, 6) == 1


def make_record(desc="测试描述", faces=6, options=None, result=5):
    return {
        "timestamp": datetime(2026, 6, 12, 12, 0, 0),
        "description": desc,
        "faces": faces,
        "options": options,
        "result": result,
        "hit_index": match_option(options, result) if options else None,
    }


def test_log_manager():
    opts = [Option(1, 3, "A"), Option(4, 6, "B")]
    rec = make_record(options=opts)
    text = format_record(rec)
    assert "测试描述" in text and "1d6" in text and "结果：5" in text and "4-6. B" in text
    rec["ai_reply"] = "哇，是大成功！"
    assert "AI：哇，是大成功！" in format_record(rec)
    del rec["ai_reply"]

    with tempfile.TemporaryDirectory() as tmp:
        lm = LogManager()
        expect_error(lambda: lm.append_record(rec), "尚未选择日志目录")
        lm.set_directory(tmp)
        assert lm.current_file is None  # 惰性创建：不写入不产生文件

        # 默认行为：首次写入自动新建；重复写入是追加
        path = lm.append_record(rec)
        assert os.path.isfile(path) and lm.current_file == path
        lm.append_record(make_record(desc="第二条"))
        lm.append_text("  AI：追加的 AI 回复")
        content = open(path, encoding="utf-8").read()
        assert content.count("[2026-06-12") == 2 and "第二条" in content
        assert "追加的 AI 回复" in content

        # 指定已有文件：只追加，不擦除原内容
        existing = os.path.join(tmp, "old.txt")
        with open(existing, "w", encoding="utf-8") as f:
            f.write("原有内容\n")
        lm.use_file(existing)
        lm.append_record(make_record(desc="追加进旧文件"))
        content = open(existing, encoding="utf-8").read()
        assert content.startswith("原有内容") and "追加进旧文件" in content

        # 手动新建：立即创建新的时间戳文件
        new_path = lm.start_new_file()
        assert os.path.isfile(new_path) and new_path != existing

        # 重命名：成功 / 目标已存在 / 非法名
        renamed = lm.rename_current("我的日志")
        assert os.path.basename(renamed) == "我的日志.txt" and os.path.isfile(renamed)
        assert not os.path.exists(new_path)
        expect_error(lambda: lm.rename_current("old"), "已存在")
        expect_error(lambda: lm.rename_current("a/b"), "分隔符")

        # 切换目录后回到默认行为
        lm.set_directory(tmp)
        assert lm.current_file is None

        lm2 = LogManager()
        expect_error(lambda: lm2.rename_current("x"), "没有日志文件")


def test_ai_client():
    with tempfile.TemporaryDirectory() as tmp:
        # 重定向配置/缓存/错误日志到临时目录，避免污染真实 %APPDATA%
        old = (ai_client.CONFIG_DIR, ai_client.CONFIG_PATH,
               ai_client.CACHE_PATH, ai_client.ERROR_LOG_PATH)   # 用完恢复到模块级隔离目录
        ai_client.CONFIG_DIR = tmp
        ai_client.CONFIG_PATH = os.path.join(tmp, "config.json")
        ai_client.CACHE_PATH = os.path.join(tmp, "context_cache.json")
        ai_client.ERROR_LOG_PATH = os.path.join(tmp, "ai_error.log")
        try:
            # ---- 首次运行：personas 用预设播种
            cfg = ai_client.load_config()
            assert len(cfg["personas"]) == 4
            assert cfg["active_persona"] == "元气骰子精灵"
            assert "骰子精灵" in ai_client.system_prompt(cfg)

            # ---- 第二期旧配置迁移：persona_key + custom_persona
            with open(ai_client.CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump({"persona_key": "snark", "custom_persona": "我是旧自定义",
                           "api_key": "sk-test"}, f)
            cfg = ai_client.load_config()
            names = [p["name"] for p in cfg["personas"]]
            assert names[:4] == [p["name"] for p in ai_client.DEFAULT_PERSONAS]
            assert "自定义" in names and len(cfg["personas"]) == 5
            assert cfg["active_persona"] == "毒舌损友"
            assert "persona_key" not in cfg and "custom_persona" not in cfg
            assert "毒舌" in ai_client.system_prompt(cfg)

            # 切到自定义人格 / 未知人格回退第一条
            cfg["active_persona"] = "自定义"
            assert ai_client.system_prompt(cfg) == "我是旧自定义"
            assert ai_client.persona_name(cfg) == "自定义"
            cfg["active_persona"] = "不存在的人格"
            ai_client._migrate(cfg)
            assert cfg["active_persona"] == "元气骰子精灵"

            # 人格编辑/重命名/删除后的持久化（直接操作数据层）
            cfg["personas"][0]["prompt"] = "改过的提示词"
            cfg["personas"][1]["name"] = "改名后的损友"
            ai_client.save_config(cfg)
            cfg = ai_client.load_config()
            assert cfg["personas"][0]["prompt"] == "改过的提示词"
            assert cfg["personas"][1]["name"] == "改名后的损友"
            assert ai_client.system_prompt(cfg) == "改过的提示词"

            # ---- 回复字数上限与截断标注
            assert ai_client.reply_limit({"reply_limit": 80}) == 80
            assert ai_client.reply_limit({"reply_limit": "abc"}) == 80
            assert ai_client.reply_limit({"reply_limit": 9999}) == 2000
            assert ai_client.reply_limit({"reply_limit": 2000}) == 2000
            assert ai_client.reply_limit({"reply_limit": 5}) == 20
            assert ai_client.finalize_reply("说到一半", "length").endswith(
                ai_client.TRUNCATION_MARK)
            assert ai_client.finalize_reply("完整回复", "stop") == "完整回复"

            # ---- base_url 规整：容忍粘贴完整请求地址 / 尾斜杠
            n = ai_client.normalize_base_url
            v4 = "https://open.bigmodel.cn/api/paas/v4"
            assert n(v4) == v4
            assert n(v4 + "/") == v4
            assert n(v4 + "/chat/completions") == v4          # 官方文档整段粘贴
            assert n(v4 + "/chat/completions/") == v4
            assert n("  " + v4 + "  ") == v4
            assert n("https://api.deepseek.com/v1") == "https://api.deepseek.com/v1"
            assert n("") == "" and n(None) == ""

            # ---- 错误日志
            ai_client._log_error("测试上下文", "测试细节")
            log_content = open(ai_client.ERROR_LOG_PATH, encoding="utf-8").read()
            assert "测试上下文" in log_content and "测试细节" in log_content

            # ---- prompt 构造：背景 / 补充说明 / 历史 / 本次
            opts = [Option(1, 3, "A"), Option(4, 6, "B")]
            prev = make_record(desc="昨天的事", result=2)
            rec = make_record(options=opts, result=5)
            msg = build_user_message(rec, [prev, rec],
                                     context_text="勇者正在挑战魔王",
                                     note="这一段主角处于劣势")
            assert "【背景设定】" in msg and "勇者正在挑战魔王" in msg
            assert "【补充说明】" in msg and "这一段主角处于劣势" in msg
            assert "昨天的事" in msg and "命中：4-6. B" in msg
            msg2 = build_user_message(make_record(), [])
            assert "【背景设定】" not in msg2 and "【补充说明】" not in msg2

            # ---- txt 提取
            txt = os.path.join(tmp, "ctx.txt")
            with open(txt, "w", encoding="utf-8") as f:
                f.write("剧情设定" * 10)
            assert "剧情设定" in ai_client.extract_text(txt)

            # ---- docx 提取（手工构造最小 docx，多 run 段落需拼接）
            docx_path = os.path.join(tmp, "ctx.docx")
            doc_xml = (
                '<?xml version="1.0"?>'
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                "<w:body><w:p><w:r><w:t>第一段剧情</w:t></w:r></w:p>"
                "<w:p><w:r><w:t>第二段</w:t><w:t>剧情</w:t></w:r></w:p></w:body></w:document>"
            )
            with zipfile.ZipFile(docx_path, "w") as z:
                z.writestr("word/document.xml", doc_xml)
            assert ai_client.extract_text(docx_path) == "第一段剧情\n第二段剧情"

            # ---- 短文件直接作为背景（不触网）
            assert ai_client.get_context(cfg, txt).startswith("剧情设定")

            # ---- 浓缩稿缓存：读写、随文件内容失效
            long_path = os.path.join(tmp, "long.txt")
            with open(long_path, "w", encoding="utf-8") as f:
                f.write("很长的剧情。" * 1000)
            assert ai_client.get_cached_digest(long_path) is None
            ai_client.set_cached_digest(long_path, "用户编辑过的浓缩稿")
            assert ai_client.get_cached_digest(long_path) == "用户编辑过的浓缩稿"
            # get_context 命中缓存，不触网（cfg 无 key 也不报错）
            assert ai_client.get_context({"api_key": ""}, long_path) == "用户编辑过的浓缩稿"
            # 文件内容变化 → key 失效，需要重新浓缩
            with open(long_path, "a", encoding="utf-8") as f:
                f.write("新增剧情。")
            assert ai_client.get_cached_digest(long_path) is None

            # ---- 错误场景
            empty = os.path.join(tmp, "empty.txt")
            open(empty, "w").close()
            expect_error(lambda: ai_client.extract_text(empty), "为空")
            expect_error(lambda: ai_client.extract_text(os.path.join(tmp, "no.txt")), "读取文件失败")
            expect_error(lambda: ai_client._chat({"base_url": "", "api_key": "x"}, "s", "u"), "API 地址")
            expect_error(lambda: ai_client._chat({"base_url": "https://x", "api_key": ""}, "s", "u"), "API 密钥")
        finally:
            (ai_client.CONFIG_DIR, ai_client.CONFIG_PATH,
             ai_client.CACHE_PATH, ai_client.ERROR_LOG_PATH) = old


def test_ingest():
    # 稳健 JSON 解析：纯数组
    r = parse_entries_json('[{"title":"艾莉娅","category":"人物","content":"剑士"}]')
    assert r[0]["title"] == "艾莉娅" and r[0]["category"] == "人物"

    # 代码围栏 + 前导脏文本
    fenced = '好的，这是结果：\n```json\n[{"title":"黑森林","tags":["危险","北境"]}]\n```'
    r = parse_entries_json(fenced)
    assert r[0]["title"] == "黑森林" and r[0]["tags"] == "危险, 北境"

    # 对象包 entries / 单对象 / name 兜底 / 无 title 跳过
    assert parse_entries_json('{"entries":[{"title":"王国"}]}')[0]["title"] == "王国"
    assert parse_entries_json('{"name":"孤条","content":"x"}')[0]["title"] == "孤条"
    assert parse_entries_json('[{"content":"没名字"},{"title":"有名字"}]') == \
        [{"title": "有名字", "category": "", "content": "", "tags": "", "aliases": ""}]

    # 非法 JSON
    expect_error(lambda: parse_entries_json("这根本不是 JSON"), "JSON")

    # 截断抢救：三条只写完两条，保住完整的两条
    truncated = ('{"entries":[{"title":"魔法","category":"规则","content":"完整"},'
                 '{"title":"学校","content":"另一条"},{"title":"半截的词条","content":"内容还没写完')
    r = parse_entries_json(truncated)
    assert [e["title"] for e in r] == ["魔法", "学校"], [e["title"] for e in r]

    # 对象前后夹带思考过程/闲话，仍能取出词条
    noisy = '好的，我思考一下……\n{"entries":[{"title":"甲","content":"x"}]}\n希望有用！'
    assert parse_entries_json(noisy)[0]["title"] == "甲"

    # 单条被截断且无任何完整对象 → 明确报错
    expect_error(lambda: parse_entries_json('{"entries":[{"title":"魔法","content":"魔法是通过计算'),
                 "JSON")

    # extract_entries：mock AI，验证分块合并 + 按名称去重
    calls = {"n": 0}

    def fake_chat(cfg, system, user, max_tokens=800, json_mode=False):
        calls["n"] += 1
        return '{"entries":[{"title":"艾莉娅","content":"剑士"},{"title":"王国","content":"主舞台"}]}'

    orig = ingest.chat_once
    ingest.chat_once = fake_chat
    try:
        long_text = "很长的设定。" * 6000  # 超过 CONDENSE_CHUNK，会分块
        entries = extract_entries({"api_key": "x"}, long_text, source="测试来源")
        assert calls["n"] >= 2                     # 确实分了块
        titles = [e["title"] for e in entries]
        assert titles == ["艾莉娅", "王国"]           # 跨块按名称去重
        assert all(e["source"] == "测试来源" for e in entries)
        expect_error(lambda: extract_entries({}, "   "), "没有可提取")
    finally:
        ingest.chat_once = orig

    # _extract_chunk：400 疑似不支持 json 模式 → 回退普通请求；5xx/其他 → 直接抛
    calls = []

    def chat_400(cfg, system, user, max_tokens=800, json_mode=False):
        calls.append(json_mode)
        if json_mode:
            raise AIError("bad request", status=400)
        return '{"entries":[]}'

    orig2 = ingest.chat_once
    ingest.chat_once = chat_400
    try:
        assert ingest._extract_chunk({}, "x") == '{"entries":[]}'
        assert calls == [True, False]        # 先 json，再回退普通
        calls.clear()

        def chat_503(cfg, system, user, max_tokens=800, json_mode=False):
            calls.append(json_mode)
            raise AIError("overloaded", status=503)

        ingest.chat_once = chat_503
        expect_error(lambda: ingest._extract_chunk({}, "x"), "overloaded")
        assert calls == [True]               # 5xx 不回退，只调一次
    finally:
        ingest.chat_once = orig2

    # classify_proposals：对既有库标 new/duplicate
    tmp = tempfile.mkdtemp()
    kb = KnowledgeBase(os.path.join(tmp, "kb.db"))
    kb.add("艾莉娅", category="人物", content="旧", aliases="小艾")
    tagged = classify_proposals(
        [{"title": "艾莉娅", "aliases": ""}, {"title": "全新词条", "aliases": ""},
         {"title": "某某", "aliases": "小艾"}],
        kb,
    )
    assert tagged[0]["status"] == "duplicate" and tagged[0]["matches"][0]["title"] == "艾莉娅"
    assert tagged[1]["status"] == "new" and tagged[1]["matches"] == []
    assert tagged[2]["status"] == "duplicate"   # 别名撞既有别名
    kb.close()


def test_condense_recap():
    seen = []

    def fake_chat(cfg, system, user, max_tokens=400):
        seen.append(user)
        return ("提要正文", "stop")

    orig = ai_client._chat
    ai_client._chat = fake_chat
    try:
        # 无历史提要：直接浓缩，单块只调一次
        out = ai_client.condense_recap({"api_key": "x"}, "第一章内容")
        assert out == "提要正文" and len(seen) == 1
        assert "已有前情提要" not in seen[0] and "第一章内容" in seen[0]

        # 有历史提要：作为增量基础带入
        seen.clear()
        ai_client.condense_recap({"api_key": "x"}, "第二章内容", prev_summary="旧提要内容")
        assert "已有前情提要" in seen[0] and "旧提要内容" in seen[0]
        assert "第二章内容" in seen[0]

        # 长文分块：多次分段 + 一次合并
        seen.clear()
        msgs = []
        long_text = "很长的前文。" * 12000
        ai_client.condense_recap({"api_key": "x"}, long_text,
                                 status_cb=lambda m: msgs.append(m))
        n_chunks = -(-len(long_text) // ai_client.CONDENSE_CHUNK)
        assert len(seen) == n_chunks + 1, (len(seen), n_chunks)
        assert any("正在阅读前文" in m for m in msgs)

        expect_error(lambda: ai_client.condense_recap({}, "   "), "没有可浓缩")
    finally:
        ai_client._chat = orig


def test_retrieve():
    entries = [
        {"id": 1, "title": "艾莉娅", "category": "人物", "aliases": "小艾",
         "content": "王国的年轻剑士，红发，性格倔强。", "updated_at": "2026-07-01"},
        {"id": 2, "title": "黑森林", "category": "地点", "aliases": "",
         "content": "北境的危险森林。", "updated_at": "2026-07-02"},
    ]

    # 清单构造：带 id/类别/标题/别名/摘要
    cat = retrieve.build_catalog(entries)
    assert "#1 [人物] 艾莉娅（别名：小艾）" in cat, cat
    assert "#2 [地点] 黑森林 —" in cat and "（别名" not in cat.split("#2")[1]
    assert len(retrieve.summarize({"content": "很长" * 100})) <= retrieve.SUMMARY_CHARS + 1

    # id 解析：标准 / 围栏 / 纯数组 / 夹带解释 / 过滤非法 id
    valid = {1, 2}
    assert retrieve.parse_ids('{"ids": [1, 2]}', valid) == [1, 2]
    nl = chr(10)
    fenced = "```json" + nl + '{"ids":[2]}' + nl + "```"
    assert retrieve.parse_ids(fenced, valid) == [2]
    assert retrieve.parse_ids("[1]", valid) == [1]
    assert retrieve.parse_ids('好的：{"ids":[1,99]}', valid) == [1]     # 99 不在库中被过滤
    assert retrieve.parse_ids('相关的是 #2 号', valid) == [2]           # 兜底扫数字
    assert retrieve.parse_ids('{"ids": []}', valid) == []
    assert retrieve.parse_ids('[{"id": 1}]', valid) == [1]             # 对象数组

    # 提及匹配：标题或别名出现在输入里
    assert retrieve.mention_ids(entries, "小艾去了哪") == {1}
    assert retrieve.mention_ids(entries, "黑森林很危险") == {2}
    assert retrieve.mention_ids(entries, "今天天气不错") == set()

    # 粗筛：超量时被提及的优先保留
    many = [{"id": i, "title": f"词条{i}", "aliases": "", "content": "x",
             "updated_at": f"2026-01-{i % 28 + 1:02d}"} for i in range(10, 210)]
    many.append(dict(entries[0]))
    kept = retrieve.prefilter(many, "关于艾莉娅的故事", limit=5)
    assert len(kept) == 5 and kept[0]["id"] == 1

    # pick_relevant：mock AI
    calls = {}

    def fake_chat(cfg, system, user, max_tokens=800, json_mode=False):
        calls["user"] = user
        calls["json_mode"] = json_mode
        return '{"ids": [2]}'

    orig = retrieve.chat_once
    retrieve.chat_once = fake_chat
    try:
        ids = retrieve.pick_relevant({"api_key": "x"}, "北边那片林子怎么样", entries)
        assert ids == [2]
        assert "【设定清单】" in calls["user"] and "北边那片林子" in calls["user"]
        assert calls["json_mode"] is True
        assert retrieve.pick_relevant({"api_key": "x"}, "", entries) == []
        assert retrieve.pick_relevant({"api_key": "x"}, "问题", []) == []

        # 400 回退普通请求
        seq = []

        def chat_400(cfg, system, user, max_tokens=800, json_mode=False):
            seq.append(json_mode)
            if json_mode:
                raise AIError("bad", status=400)
            return '{"ids":[1]}'

        retrieve.chat_once = chat_400
        assert retrieve.pick_relevant({"api_key": "x"}, "谁是主角", entries) == [1]
        assert seq == [True, False]

        # 5xx 直接抛，由调用方兜底
        def chat_503(cfg, system, user, max_tokens=800, json_mode=False):
            raise AIError("overloaded", status=503)

        retrieve.chat_once = chat_503
        expect_error(lambda: retrieve.pick_relevant({"api_key": "x"}, "谁是主角", entries),
                     "overloaded")
    finally:
        retrieve.chat_once = orig


def test_chatstore():
    tmp = tempfile.mkdtemp()
    st = chatstore.ChatStore(os.path.join(tmp, "chat.db"))

    # 会话：新建 / 改名 / 列表按更新时间倒序
    s1 = st.create_session("第一章")
    s2 = st.create_session()                    # 默认名带时间戳
    assert len(st.list_sessions()) == 2
    assert st.get_session(s1)["title"] == "第一章"
    st.rename_session(s1, "序章")
    assert st.get_session(s1)["title"] == "序章"
    expect_error(lambda: st.rename_session(s1, "   "), "不能为空")

    # 消息：content(送模型) 与 display(界面) 分开存
    st.add_message(s1, "user", "【设定库参考】...原始问题", display="原始问题")
    st.add_message(s1, "assistant", "回答", display="回答")
    msgs = st.get_messages(s1)
    assert [m["role"] for m in msgs] == ["user", "assistant"]
    assert msgs[0]["content"].startswith("【设定库参考】") and msgs[0]["display"] == "原始问题"
    assert st.message_count(s1) == 2 and st.message_count(s2) == 0

    # 删除会话会连带删消息
    st.add_message(s2, "user", "x")
    st.delete_session(s2)
    assert st.get_session(s2) is None and st.message_count(s2) == 0
    st.clear_messages(s1)
    assert st.message_count(s1) == 0

    # 记忆：去重、更新、删除、注入文本
    assert st.add_memory("用户喜欢第三人称") is not None
    assert st.add_memory("用户喜欢第三人称") is None      # 完全重复不再入库
    assert st.add_memory("   ") is None
    mid = st.add_memory("主角的剑叫晨星")
    assert len(st.list_memories()) == 2
    text = st.memory_text()
    assert "- 用户喜欢第三人称" in text and "- 主角的剑叫晨星" in text
    st.update_memory(mid, "主角的剑叫晨星，会发光")
    assert "会发光" in st.memory_text()
    expect_error(lambda: st.update_memory(mid, ""), "不能为空")
    st.delete_memory(mid)
    assert len(st.list_memories()) == 1

    # 注入文本受长度上限约束
    for i in range(50):
        st.add_memory(f"记忆条目{i}" * 10)
    assert len(st.memory_text(char_limit=200)) <= 200 + 40
    st.close()


def test_memory_extraction():
    # 解析：标准 / 围栏 / 纯数组 / 对象数组 / 空 / 垃圾
    assert memory.parse_notes('{"notes": ["a", "b"]}') == ["a", "b"]
    nl = chr(10)
    fenced = "```json" + nl + '{"notes":["x"]}' + nl + "```"
    assert memory.parse_notes(fenced) == ["x"]
    assert memory.parse_notes('["p", "q"]') == ["p", "q"]
    assert memory.parse_notes('{"notes":[{"content":"c1"}]}') == ["c1"]
    assert memory.parse_notes('{"notes": []}') == []
    assert memory.parse_notes("完全不是 JSON") == []
    assert memory.parse_notes('{"notes":["dup","dup"]}') == ["dup"]      # 去重
    assert len(memory.parse_notes('{"notes":%s}' % json.dumps(
        [f"n{i}" for i in range(20)]))) == memory.MAX_NOTES               # 截断到上限

    # extract_notes：mock AI，校验提示里带上已有记忆
    calls = {}

    def fake_chat(cfg, system, user, max_tokens=800, json_mode=False):
        calls["user"] = user
        calls["json_mode"] = json_mode
        return '{"notes": ["记住这件事"]}'

    orig = memory.chat_once
    memory.chat_once = fake_chat
    try:
        notes = memory.extract_notes({"api_key": "x"}, "我的问题", "助手的回答",
                                     existing=["已经记过的事"])
        assert notes == ["记住这件事"]
        assert "已经记过的事" in calls["user"] and "不要重复记录" in calls["user"]
        assert "我的问题" in calls["user"] and "助手的回答" in calls["user"]
        assert calls["json_mode"] is True
        assert memory.extract_notes({"api_key": "x"}, "", "") == []

        # 400 回退普通请求
        seq = []

        def chat_400(cfg, system, user, max_tokens=800, json_mode=False):
            seq.append(json_mode)
            if json_mode:
                raise AIError("bad", status=400)
            return '{"notes":["回退成功"]}'

        memory.chat_once = chat_400
        assert memory.extract_notes({"api_key": "x"}, "q", "a") == ["回退成功"]
        assert seq == [True, False]
    finally:
        memory.chat_once = orig


def test_agent_tools():
    tmp = tempfile.mkdtemp()
    kb = KnowledgeBase(os.path.join(tmp, "kb_tools.db"))
    eid = kb.add("艾莉娅", category="人物", content="红发剑士", aliases="小艾")
    kb.set_recap("前情提要不该被工具看到")

    # 工具清单：读写分离
    assert agent_tools.is_write("update_entry") and not agent_tools.is_write("get_entry")
    names = [t["function"]["name"] for t in agent_tools.schemas(allow_write=True)]
    ro = [t["function"]["name"] for t in agent_tools.schemas(allow_write=False)]
    assert "create_entry" in names and "create_entry" not in ro
    assert "search_entries" in ro

    ex = lambda n, a: agent_tools.execute(kb, n, a)

    # 读
    assert "艾莉娅" in ex("search_entries", {"query": "剑士"})
    assert "没有匹配" in ex("search_entries", {"query": "不存在的东西"})
    assert "前情提要" not in ex("search_entries", {"query": ""})   # 特殊词条被过滤
    assert "红发剑士" in ex("get_entry", {"entry_id": eid})
    assert "错误" in ex("get_entry", {"entry_id": 9999})
    assert "错误" in ex("get_entry", {"entry_id": "abc"})
    assert "人物" in ex("list_categories", {})

    # 写
    r = ex("create_entry", {"title": "晨星", "category": "物品", "content": "佩剑"})
    assert "已新建" in r and kb.search("晨星", exact=True)
    assert "错误" in ex("create_entry", {"content": "没名字"})
    assert "已更新" in ex("update_entry", {"entry_id": eid, "content": "改过的正文"})
    assert kb.get(eid)["content"] == "改过的正文"
    assert "错误" in ex("update_entry", {"entry_id": eid})          # 没给字段
    assert "错误" in ex("update_entry", {"entry_id": 9999, "content": "x"})
    new_id = kb.add("待删", content="x")
    assert "已删除" in ex("delete_entry", {"entry_id": new_id})
    assert kb.get(new_id) is None
    assert "错误" in ex("delete_entry", {"entry_id": 9999})
    assert "未知工具" in ex("no_such_tool", {})

    # 审批面板的描述文案
    assert "新建词条" in agent_tools.describe("create_entry", {"title": "甲"})
    d = agent_tools.describe("update_entry", {"entry_id": eid, "content": "x"}, kb)
    assert "艾莉娅" in d and "content" in d
    assert "删除词条" in agent_tools.describe("delete_entry", {"entry_id": eid}, kb)
    kb.close()


def test_condense_overview():
    seen = []

    def fake_chat(cfg, system, user, max_tokens=400):
        seen.append(user)
        return ("总览正文", "stop")

    orig = ai_client._chat
    ai_client._chat = fake_chat
    try:
        entries_text = "[人物] 艾莉娅" + chr(10) + "剑士"
        out = ai_client.condense_overview({"api_key": "x"}, entries_text)
        assert out == "总览正文" and len(seen) == 1
        assert "已有总览" not in seen[0] and "艾莉娅" in seen[0]

        seen.clear()
        ai_client.condense_overview({"api_key": "x"}, "新词条", prev_summary="旧总览")
        assert "已有总览" in seen[0] and "旧总览" in seen[0]

        expect_error(lambda: ai_client.condense_overview({}, "  "), "还没有词条")
    finally:
        ai_client._chat = orig


if __name__ == "__main__":
    test_check_options()
    test_faces_and_roll()
    test_match()
    test_log_manager()
    test_ai_client()
    test_ingest()
    test_condense_recap()
    test_retrieve()
    test_chatstore()
    test_memory_extraction()
    test_agent_tools()
    test_condense_overview()
    print("ALL TESTS PASSED")
