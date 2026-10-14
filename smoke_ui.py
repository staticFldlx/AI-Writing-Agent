"""UI 冒烟测试：选项行掷骰 + 日志写入 + AI 回复链路（mock，不发真实网络请求）。"""

import os
import tempfile
import time

import ui

# 隔离配置：测试绝不能写用户真实的 %APPDATA%\DiceBot\config.json
_iso = tempfile.mkdtemp()
ui.ai_client.CONFIG_DIR = _iso
ui.ai_client.CONFIG_PATH = os.path.join(_iso, "config.json")
ui.ai_client.CACHE_PATH = os.path.join(_iso, "context_cache.json")
ui.ai_client.ERROR_LOG_PATH = os.path.join(_iso, "ai_error.log")

app = ui.DiceBotApp()
app.update_idletasks()
app.update()

app.mode_seg.set(ui.MODE_OPTIONS)
app._on_mode_change(ui.MODE_OPTIONS)

# 逐条添加：第一条默认 1-6，把终点改到 3；第二条自动衔接为 4-6
app._add_option_row()
row1 = app.option_rows[0]
assert (row1.start, row1.end) == (1, 6), (row1.start, row1.end)
row1._on_end_change("3")
row1.text_entry.insert(0, "甲")
app._add_option_row()
row2 = app.option_rows[1]
assert (row2.start, row2.end) == (4, 6), (row2.start, row2.end)
row2.text_entry.insert(0, "乙")
app._refresh_option_status()

# 面数联动：改成 1d4 时第二条终点应被夹紧到 4，改回 6 仍合法
app.faces_entry.delete(0, "end")
app.faces_entry.insert(0, "4")
app._on_faces_typed()
assert (row2.start, row2.end) == (4, 4), (row2.start, row2.end)
app.faces_entry.delete(0, "end")
app.faces_entry.insert(0, "6")
app._on_faces_typed()
row2._on_end_change("6")

app.desc_entry.insert(0, "冒烟测试")

tmp = tempfile.mkdtemp()
app.log.set_directory(tmp)
app.log_switch.select()


def pump_until(cond, timeout=5):
    deadline = time.time() + timeout
    while not cond() and time.time() < deadline:
        app.update()
        time.sleep(0.02)
    assert cond(), "等待超时"


def roll_and_wait():
    before = len(app.history)
    app._roll()
    pump_until(lambda: len(app.history) == before + 1)


roll_and_wait()
rec = app.history[0]
assert 1 <= rec["result"] <= 6 and rec["hit_index"] in (0, 1), rec

logs = os.listdir(tmp)
assert len(logs) == 1, f"日志文件数量异常：{logs}"
log_path = os.path.join(tmp, logs[0])
content = open(log_path, encoding="utf-8").read()
assert "冒烟测试" in content and "1d6" in content and "命中" in content, content

roll_and_wait()
content2 = open(log_path, encoding="utf-8").read()
assert content2.count("冒烟测试") == 2, "第二次掷骰未追加到同一文件"

# ---- AI 回复链路：mock generate_reply，验证补充说明传入 + 线程→队列→面板/历史/日志 ----
captured = {}


def fake_reply(cfg, record, history, context_text="", note=""):
    captured["note"] = note
    captured["context"] = context_text
    return "假装这是AI回复"


ui.ai_client.generate_reply = fake_reply
app.ai_cfg["ai_enabled"] = True
app.ai_cfg["api_key"] = "sk-fake"
app.ai_cfg["context_note"] = "测试补充说明"
app.context_file = None

roll_and_wait()
pump_until(lambda: app.ai_reply_label.cget("text") == "假装这是AI回复")
assert captured["note"] == "测试补充说明", captured
assert captured["context"] == "", captured
assert app.history[-1]["ai_reply"] == "假装这是AI回复"
content3 = open(log_path, encoding="utf-8").read()
assert "AI：假装这是AI回复" in content3, "AI 回复未追加进日志"

# 上下文状态文案
app._update_ctx_status()
assert "补充说明" in app.ctx_status.cget("text")

results = [r["result"] for r in app.history]
app.destroy()
print("UI SMOKE TEST PASSED, results:", results)
