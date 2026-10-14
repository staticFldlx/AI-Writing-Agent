"""写作助手 UI 冒烟测试：mock AI，走通录入→入库→检索→注入→对话→转录入链路。"""

import os
import tempfile
import time

import writer_ui

tmp = tempfile.mkdtemp()

# 隔离配置：测试绝不能写用户真实的 %APPDATA%\DiceBot\config.json
writer_ui.ai_client.CONFIG_DIR = tmp
writer_ui.ai_client.CONFIG_PATH = os.path.join(tmp, "config.json")
writer_ui.ai_client.CACHE_PATH = os.path.join(tmp, "context_cache.json")
writer_ui.ai_client.ERROR_LOG_PATH = os.path.join(tmp, "ai_error.log")

app = writer_ui.WriterApp(db_path=os.path.join(tmp, "kb.db"),
                          chat_db_path=os.path.join(tmp, "chat.db"))
app.ai_cfg["api_key"] = "sk-fake"
app.update_idletasks()
app.update()


def pump_until(cond, timeout=5):
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        app.update()
        time.sleep(0.02)
    assert cond(), "等待超时"


# ---- 面板二：录入。mock extract_entries 返回两条提案 ----
def fake_extract(cfg, raw, source="", status_cb=None, chunk_size=None,
                 start_index=0, seed=None, pause=0):
    if status_cb:
        status_cb("正在提取词条 1/1…")
    return [
        {"title": "艾莉娅", "category": "人物", "content": "红发剑士", "tags": "主角",
         "aliases": "小艾", "source": source},
        {"title": "黑森林", "category": "地点", "content": "北境危险森林", "tags": "",
         "aliases": "", "source": source},
    ]


writer_ui.ingest.extract_entries = fake_extract
app.nav.set(writer_ui.PANEL_INGEST)
app._show_panel(writer_ui.PANEL_INGEST)
app.ingest_input.insert("1.0", "一些设定文本")
app._ingest_extract()
pump_until(lambda: len(app.proposal_rows) == 2)
assert all(r.checked.get() for r in app.proposal_rows)
app._ingest_commit()
pump_until(lambda: app.kb.count() == 2)
assert {e["title"] for e in app.kb.list_entries()} == {"艾莉娅", "黑森林"}

# 重复录入：同名应判为 duplicate，提交时走增补而非新增
app._show_proposals(writer_ui.ingest.classify_proposals(
    [{"title": "艾莉娅", "category": "人物", "content": "补充：队长身份",
      "tags": "队长", "aliases": ""}], app.kb))
assert app.proposal_rows[0].matches, "同名未识别为疑似重复"
app._ingest_commit()
assert app.kb.count() == 2, "增补不应新增条数"
assert "队长" in app.kb.search("艾莉娅", exact=True)[0]["content"]

# ---- 面板一：设定库检索 ----
app.nav.set(writer_ui.PANEL_KB)
app._show_panel(writer_ui.PANEL_KB)
app.kw_entry.insert(0, "森林")
app._kb_search()
assert len(app.kb_list.winfo_children()) >= 1        # 检索结果已渲染成列表项
assert [e["title"] for e in app.kb.search("森林")] == ["黑森林"]
# 点开该词条载入详情
app._kb_load(app.kb.search("森林")[0]["id"])
assert app.d_title.get() == "黑森林"

# 分组折叠：搜索时命中的组自动展开，可手动收起
assert app._kb_expanded.get("地点") is True, app._kb_expanded
n_open = len(app.kb_list.winfo_children())
app._toggle_kb_group("地点")                 # 收起后只剩类别标题行
assert app._kb_expanded["地点"] is False
assert len(app.kb_list.winfo_children()) < n_open
app._toggle_kb_group("地点")
app.kw_entry.delete(0, "end")
app._kb_search()
cats = {e["category"] for e in app.kb.normal_entries()}
assert len(app.kb_list.winfo_children()) >= len(cats)   # 每个类别至少一行标题

# ---- 面板三：写作讨论。自动关键词匹配 + mock chat_messages ----
captured = {}


tool_script = {"calls": None}      # 设为一组 tool_calls 时，第一轮返回它


def fake_chat_tools(cfg, messages, tools, max_tokens=3000):
    captured["messages"] = messages
    captured["tools"] = [t["function"]["name"] for t in tools]
    calls = tool_script["calls"]
    if calls:
        tool_script["calls"] = None            # 只触发一轮，第二轮给最终回复
        return ({"role": "assistant", "content": None, "tool_calls": calls}, "tool_calls")
    return ({"role": "assistant", "content": "这是助手的回复"}, "stop")


writer_ui.ai_client.chat_with_tools = fake_chat_tools

# mock 自动召回，避免真实网络请求
picked = {"ids": [], "calls": 0, "raise": False}


def fake_pick(cfg, user_text, entries, max_pick=12, status_cb=None):
    picked["calls"] += 1
    picked["seen"] = [e["title"] for e in entries]
    if picked["raise"]:
        raise writer_ui.ai_client.AIError("模拟召回失败")
    return list(picked["ids"])


writer_ui.retrieve.pick_relevant = fake_pick

notes_out = {"notes": []}


def fake_notes(cfg, user_text, assistant_text, existing=None):
    return list(notes_out["notes"])


writer_ui.memory.extract_notes = fake_notes

app.nav.set(writer_ui.PANEL_CHAT)
app._show_panel(writer_ui.PANEL_CHAT)

# 全选/全不选 + 筛选（强制包含集跨筛选保留）
app._refresh_entry_picker()
app._toggle_all_entries()
assert app.entry_selected == {e["id"] for e in app.kb.list_entries()}
app._toggle_all_entries()
assert app.entry_selected == set()
app.entry_filter.insert(0, "森林")
app._refresh_entry_picker()
assert set(app.entry_checks) == {e["id"] for e in app.kb.search("森林")}
app.entry_filter.delete(0, "end")
app._refresh_entry_picker()

app.auto_kw.select()
app.chat_input.insert("1.0", "帮我写一段艾莉娅进入黑森林的情节")
app._chat_send()
pump_until(lambda: any(m.role == "assistant" for m in app.chat_messages))
sent = captured["messages"][-1]["content"]
assert "【设定库参考】" in sent and "艾莉娅" in sent and "黑森林" in sent, sent
assert "【你的人设】" in captured["messages"][0]["content"]   # 人格已同步进写作 system
assert app.chat_history[-1]["content"] == "这是助手的回复"

# ---- 自动召回：AI 挑的词条即使没被提及、没被勾选也应注入 ----
app.smart_recall.select()
app.auto_kw.deselect()          # 关掉关键词兜底，确保注入来自 AI 召回
app.entry_selected.clear()
elia_id = app.kb.search("艾莉娅", exact=True)[0]["id"]
picked["ids"] = [elia_id]
picked["calls"] = 0
app.chat_input.insert("1.0", "写一段关于那个红头发女孩的戏")   # 不含任何词条名
app._chat_send()
pump_until(lambda: len(app.chat_history) == 4)
assert picked["calls"] == 1, "未调用自动召回"
sent_ai = captured["messages"][-1]["content"]
assert "艾莉娅" in sent_ai and "队长" in sent_ai, sent_ai   # 正文已被增补为队长身份
assert "黑森林" not in sent_ai, "未被召回的词条不应注入"

# 手动勾选=强制包含：即使 AI 没挑中也必须在
forest_id = app.kb.search("森林")[0]["id"]
app.entry_selected.add(forest_id)
picked["ids"] = []
app.chat_input.insert("1.0", "随便聊聊")
app._chat_send()
pump_until(lambda: len(app.chat_history) == 6)
assert "黑森林" in captured["messages"][-1]["content"], "强制包含的词条丢失"
app.entry_selected.clear()

# 召回失败 → 静默退回关键词兜底，对话仍可继续
picked["raise"] = True
app.auto_kw.select()
app.chat_input.insert("1.0", "艾莉娅现在在哪")
app._chat_send()
pump_until(lambda: len(app.chat_history) == 8)
assert "艾莉娅" in captured["messages"][-1]["content"], "兜底关键词匹配失效"
picked["raise"] = False

# ---- 前情提要：保存后应始终自动注入，且不出现在勾选区 ----
app.kb.set_recap("第一章：艾莉娅离开王都，前往北境。")
app._refresh_entry_picker()
picker_ids = set(app.entry_checks)
recap_id = app.kb.get_recap()["id"]
assert recap_id not in picker_ids, "前情提要不应出现在勾选区"
app._update_recap_status()
assert "已启用前情提要" in app.recap_status.cget("text")

app.chat_input.insert("1.0", "接下来写什么好")
app._chat_send()
pump_until(lambda: captured["messages"][-1]["content"].count("前情提要") > 0)
sent2 = captured["messages"][-1]["content"]
assert "【前情提要】" in sent2 and "离开王都" in sent2, sent2

# ---- 对话存档：消息落库，切换会话可续聊 ----
assert app.store.message_count(app.session_id) == len(app.chat_history)
first_session = app.session_id
history_len = len(app.chat_history)

app._new_session()
assert app.session_id != first_session
assert app.chat_history == [] and app.chat_messages == []
app.chat_input.insert("1.0", "新会话里的第一句")
app._chat_send()
pump_until(lambda: len(app.chat_history) == 2)
assert app.store.message_count(app.session_id) == 2

app._load_session(first_session)          # 切回旧会话应恢复完整历史
assert app.session_id == first_session
assert len(app.chat_history) == history_len
assert len(app.chat_messages) == history_len
assert app.chat_messages[0].text == "帮我写一段艾莉娅进入黑森林的情节"

app.store.rename_session(first_session, "主线剧情")
app._refresh_session_menu()
assert app.session_menu.get().startswith("主线剧情")

# ---- 长期记忆：提取→入库→注入 ----
notes_out["notes"] = ["用户偏好第三人称叙述", "艾莉娅的剑名为「晨星」"]
app.mem_switch.select()
app.chat_input.insert("1.0", "记住我的写作偏好")
app._chat_send()
pump_until(lambda: len(app.store.list_memories()) >= 2)
mems = {m["content"] for m in app.store.list_memories()}
assert "用户偏好第三人称叙述" in mems and "艾莉娅的剑名为「晨星」" in mems

notes_out["notes"] = []                    # 下一轮不再新增
app.chat_input.insert("1.0", "接着写")
app._chat_send()
pump_until(lambda: "【长期记忆】" in captured["messages"][-1]["content"])
sent_mem = captured["messages"][-1]["content"]
assert "第三人称叙述" in sent_mem and "晨星" in sent_mem, sent_mem

# 关掉记忆开关后不再注入
app.mem_switch.deselect()
app.chat_input.insert("1.0", "再来一段")
app._chat_send()
pump_until(lambda: captured["messages"][-1]["content"].endswith("再来一段"))
assert "【长期记忆】" not in captured["messages"][-1]["content"]
app.mem_switch.select()

# ---- 设定总览 + 整体讨论开关 ----
app.kb.set_overview("这是一个剑与魔法的王国世界。", fingerprint=app.kb.overview_fingerprint())
assert not app.kb.overview_stale()
app._update_overview_status()
assert "总览就绪" in app.overview_status.cget("text")

app.whole_world.select()
picked["calls"] = 0
app.chat_input.insert("1.0", "整体聊聊这个世界观")
app._chat_send()
pump_until(lambda: "【设定总览】" in captured["messages"][-1]["content"])
sent_ov = captured["messages"][-1]["content"]
assert "剑与魔法的王国世界" in sent_ov
assert "【设定库参考】" not in sent_ov, "整体讨论时不应再逐条注入"
assert picked["calls"] == 0, "整体讨论时不应调用逐条召回"
app.whole_world.deselect()

# 词条变动后总览应判为过期
app.kb.add("新势力", category="势力", content="新加入的组织")
assert app.kb.overview_stale()
app._update_overview_status()
assert "过期" in app.overview_status.cget("text")

# ---- Agent 工具调用 ----
app.allow_write.select()

# 读工具自动执行，无需审批
tool_script["calls"] = [{"id": "c1", "function":
                         {"name": "search_entries", "arguments": '{"query": "艾莉娅"}'}}]
_n = len(app.chat_history)
app.chat_input.insert("1.0", "查一下艾莉娅")
app._chat_send()
pump_until(lambda: len(app.chat_history) == _n + 2)
tool_msgs = [m for m in captured["messages"] if m.get("role") == "tool"]
assert tool_msgs and "艾莉娅" in tool_msgs[-1]["content"], tool_msgs
assert "create_entry" in captured["tools"], "允许写库时应挂上写工具"

# 写工具：批准 → 真的落库
approved = {"seen": None}


def auto_approve(ops):
    approved["seen"] = ops
    return {cid: True for cid, _n, _a, _d in ops}


app._request_approval = auto_approve
before = app.kb.count()
tool_script["calls"] = [{"id": "w1", "function": {
    "name": "create_entry",
    "arguments": '{"title": "晨星之剑", "category": "物品", "content": "艾莉娅的佩剑"}'}}]
app.chat_input.insert("1.0", "把晨星之剑记进设定库")
app._chat_send()
pump_until(lambda: app.kb.count() == before + 1)
assert app.kb.search("晨星之剑", exact=True), "批准后未写入设定库"
assert approved["seen"] and "晨星之剑" in approved["seen"][0][3], approved["seen"]

# 写工具：拒绝 → 库不变，且把拒绝结果回填给模型
app._request_approval = lambda ops: {cid: False for cid, _n, _a, _d in ops}
before = app.kb.count()
tool_script["calls"] = [{"id": "w2", "function": {
    "name": "delete_entry", "arguments": '{"entry_id": %d}' % elia_id}}]
_n = len(app.chat_history)
app.chat_input.insert("1.0", "把艾莉娅删掉")
app._chat_send()
pump_until(lambda: len(app.chat_history) == _n + 2)
assert app.kb.count() == before, "被拒绝的操作不应改动设定库"
assert app.kb.get(elia_id) is not None
rejected = [m for m in captured["messages"] if m.get("role") == "tool"][-1]
assert "拒绝" in rejected["content"], rejected

# 关闭「允许 AI 改设定库」后不再挂写工具
app.allow_write.deselect()
_n = len(app.chat_history)
app.chat_input.insert("1.0", "随便说点什么")
app._chat_send()
pump_until(lambda: len(app.chat_history) == _n + 2)
assert "create_entry" not in captured["tools"] and "search_entries" in captured["tools"]
app.allow_write.select()

# 勾选对话 → 转录入
app.chat_messages[0].checked.set(True)
app._chat_to_ingest()
assert app.nav.get() == writer_ui.PANEL_INGEST
assert "艾莉娅" in app.ingest_input.get("1.0", "end-1c")

# ---- 分批录入：中途失败可续传 ----
def flaky_extract(cfg, raw, source="", status_cb=None, chunk_size=None,
                  start_index=0, seed=None, pause=0):
    got = list(seed or [])
    if start_index == 0:
        got.append({"title": "第一批词条", "category": "其他", "content": "a",
                    "tags": "", "aliases": "", "source": source})
        raise writer_ui.ingest.PartialExtraction("模拟第二批失败", got, 1, 3)
    got.append({"title": "续传词条", "category": "其他", "content": "b",
                "tags": "", "aliases": "", "source": source})
    return got


writer_ui.ingest.extract_entries = flaky_extract
app.nav.set(writer_ui.PANEL_INGEST)
app._show_panel(writer_ui.PANEL_INGEST)
app.ingest_input.delete("1.0", "end")
app.ingest_input.insert("1.0", "很长的设定原文")
app._ingest_extract()
pump_until(lambda: len(app.proposal_rows) == 1)
assert app._ingest_partial is not None, "未记录续传状态"
assert app._ingest_partial[0] == 1 and app._ingest_partial[1] == 3
assert app.resume_btn.cget("state") == "normal", "继续提取按钮未启用"
assert "第 2/3 批失败" in app.status_label.cget("text")

app._ingest_resume()                       # 续传：保留已得，补齐剩余
pump_until(lambda: len(app.proposal_rows) == 2)
titles = [r.title_entry.get() for r in app.proposal_rows]
assert titles == ["第一批词条", "续传词条"], titles
assert app._ingest_partial is None
assert app.resume_btn.cget("state") == "disabled"

# 每批字数会持久化到配置
app.chunk_entry.delete(0, "end")
app.chunk_entry.insert(0, "3000")
assert app._chunk_size() == 3000 and app.ai_cfg["extract_chunk"] == 3000

# ---- 加固回归：后台任务抛非 AIError 也应显示错误、不卡死 ----
def boom(cfg, raw, source="", status_cb=None, chunk_size=None,
         start_index=0, seed=None, pause=0):
    raise RuntimeError("模拟未预期错误")


writer_ui.ingest.extract_entries = boom
app.nav.set(writer_ui.PANEL_INGEST)
app._show_panel(writer_ui.PANEL_INGEST)
app.ingest_input.delete("1.0", "end")
app.ingest_input.insert("1.0", "触发错误")
app._ingest_extract()
pump_until(lambda: "出错了" in app.status_label.cget("text"))

app.destroy()
print("WRITER SMOKE TEST PASSED, entries:", app.kb.count())
