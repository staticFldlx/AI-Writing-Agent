"""写作助手界面（CustomTkinter，蜜桃配色）。

三个面板：设定库（检索/编辑/新建）、录入（AI 从文件或对话提取词条）、
写作讨论（多轮对话，勾选词条+自动关键词注入设定库参考，对话可反哺入库）。
复用 ai_client / knowledge / ingest / theme，及骰子端的 AISettingsDialog。
AI 调用一律走后台线程经 _ui_queue 回主线程。
"""

import json
import os
import queue
import threading
import tkinter.filedialog as filedialog
import traceback

import customtkinter as ctk

import agent_tools
import ai_client
import ingest
import memory
import retrieve
from chatstore import ChatStore
from knowledge import (OVERVIEW_CATEGORY, RECAP_CATEGORY, SPECIAL_CATEGORIES,
                       KnowledgeBase, split_list)
from theme import (
    BODY_TEXT, BORDER, CARD_BG, DIM_TEXT, ERROR_TEXT, OK_TEXT, PANEL_BG,
    PRIMARY, PRIMARY_HOVER, SEG_TEXT, SOFT_BG, SOFT_HOVER, TRACK_OFF,
    WINDOW_BG, soft_button, themed_entry, themed_optionmenu, themed_textbox,
)
from ui import AISettingsDialog, DicePanel

PANEL_KB = "📚 设定库"
PANEL_INGEST = "📥 录入"
PANEL_CHAT = "✍️ 写作讨论"
PANEL_DICE = "🎲 骰子"

CATEGORIES = ["人物", "地点", "势力", "物品", "事件", "规则", "其他"]
CONTEXT_CHAR_LIMIT = 6000
MAX_TOOL_ROUNDS = 5        # agent 工具调用最多几轮，防死循环
APPROVAL_TIMEOUT = 300     # 秒；写操作审批超时按拒绝处理
WRITER_SYSTEM = (
    "你是一位写作助手，帮助用户构思剧情、讨论设定、续写文字。"
    "回答时充分利用【长期记忆】【设定总览】【前情提要】【设定库参考】中的信息，"
    "保持剧情与设定一致；参考为空时正常创作。"
)


def section(parent, text):
    return ctk.CTkLabel(
        parent, text=text, anchor="w",
        font=ctk.CTkFont(size=14, weight="bold"), text_color=BODY_TEXT,
    )


class ProposalRow(ctk.CTkFrame):
    """录入提案的一行：勾选 + 状态 + 可编辑字段（+ 疑似重复时的处理方式）。"""

    def __init__(self, master, app, proposal):
        super().__init__(master, fg_color=CARD_BG, border_width=1, border_color=BORDER)
        self.app = app
        self.matches = proposal.get("matches", [])
        self.checked = ctk.BooleanVar(value=True)

        head = ctk.CTkFrame(self, fg_color="transparent")
        head.pack(fill="x", padx=8, pady=(6, 0))
        ctk.CTkCheckBox(head, text="", variable=self.checked, width=24,
                        fg_color=PRIMARY, hover_color=PRIMARY_HOVER).pack(side="left")
        self.title_entry = themed_entry(head, width=160)
        self.title_entry.insert(0, proposal["title"])
        self.title_entry.pack(side="left", padx=(2, 6))
        self.cat_menu = themed_optionmenu(head, width=90, values=CATEGORIES)
        self.cat_menu.set(proposal.get("category") or "其他")
        self.cat_menu.pack(side="left")
        status = "🆕 新词条" if proposal["status"] == "new" else "⚠ 疑似已有"
        ctk.CTkLabel(head, text=status, font=ctk.CTkFont(size=12),
                     text_color=OK_TEXT if proposal["status"] == "new" else ERROR_TEXT
                     ).pack(side="left", padx=(8, 0))

        self.content_box = themed_textbox(self, height=54)
        self.content_box.insert("1.0", proposal.get("content", ""))
        self.content_box.pack(fill="x", padx=8, pady=4)

        meta = ctk.CTkFrame(self, fg_color="transparent")
        meta.pack(fill="x", padx=8, pady=(0, 6))
        ctk.CTkLabel(meta, text="标签", font=ctk.CTkFont(size=11),
                     text_color=DIM_TEXT).pack(side="left")
        self.tags_entry = themed_entry(meta, width=120)
        self.tags_entry.insert(0, proposal.get("tags", ""))
        self.tags_entry.pack(side="left", padx=(4, 8))
        ctk.CTkLabel(meta, text="别名", font=ctk.CTkFont(size=11),
                     text_color=DIM_TEXT).pack(side="left")
        self.alias_entry = themed_entry(meta, width=120)
        self.alias_entry.insert(0, proposal.get("aliases", ""))
        self.alias_entry.pack(side="left", padx=(4, 8))

        self.action = None
        if self.matches:
            m = self.matches[0]
            self.action = ctk.CTkOptionMenu(
                meta, width=150,
                values=[f"增补到 #{m['id']} {m['title']}", "新增为独立词条"],
                fg_color=SOFT_BG, button_color="#FFB8B8", button_hover_color="#FFA3A3",
                text_color=SEG_TEXT, dropdown_fg_color=CARD_BG,
                dropdown_text_color=BODY_TEXT, dropdown_hover_color=SOFT_BG,
            )
            self.action.set(f"增补到 #{m['id']} {m['title']}")
            self.action.pack(side="left", padx=(0, 6))
            soft_button(meta, text="AI 合并", width=72, command=self._ai_merge).pack(side="left")

    def _ai_merge(self):
        m = self.matches[0]
        proposed = {"content": self.content_box.get("1.0", "end-1c")}
        self.app._run_async(
            lambda: ingest.merge_entry(self.app.ai_cfg, m, proposed),
            lambda text: self._fill_merged(text),
            lambda err: self.app._status(f"合并失败：{err}", error=True),
            busy="正在 AI 合并…",
        )

    def _fill_merged(self, text):
        self.content_box.delete("1.0", "end")
        self.content_box.insert("1.0", text)
        self.app._status("已生成合并正文，可再编辑后提交")

    def data(self):
        return {
            "checked": self.checked.get(),
            "title": self.title_entry.get().strip(),
            "category": self.cat_menu.get(),
            "content": self.content_box.get("1.0", "end-1c").strip(),
            "tags": self.tags_entry.get().strip(),
            "aliases": self.alias_entry.get().strip(),
            "augment_id": (self.matches[0]["id"]
                           if self.matches and self.action
                           and self.action.get().startswith("增补") else None),
        }


class ChatMessage(ctk.CTkFrame):
    """对话中的一条消息：勾选（供转录入）+ 角色 + 文本。"""

    def __init__(self, master, role, text):
        super().__init__(master, fg_color=CARD_BG, border_width=1, border_color=BORDER)
        self.role = role
        self.text = text
        self.checked = ctk.BooleanVar(value=False)
        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=8, pady=(4, 0))
        ctk.CTkCheckBox(top, text="", variable=self.checked, width=22,
                        fg_color=PRIMARY, hover_color=PRIMARY_HOVER).pack(side="left")
        ctk.CTkLabel(top, text="你" if role == "user" else "助手",
                     font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=PRIMARY if role == "user" else OK_TEXT).pack(side="left")
        ctk.CTkLabel(self, text=text, anchor="w", justify="left", wraplength=560,
                     font=ctk.CTkFont(size=13), text_color=BODY_TEXT
                     ).pack(fill="x", padx=10, pady=(0, 6))


class RecapDialog(ctk.CTkToplevel):
    """前情提要：导入前文 → AI 浓缩 → 查看/编辑/保存/导出。

    已有提要时可勾选增量更新，把上次提要作为基础再浓缩新内容。
    """

    def __init__(self, app):
        super().__init__(app, fg_color=WINDOW_BG)
        self.app = app
        self.title("前情提要")
        self.geometry("640x720")
        self.transient(app)

        def label(text, pady=(12, 2)):
            ctk.CTkLabel(
                self, text=text, anchor="w",
                font=ctk.CTkFont(size=13, weight="bold"), text_color=BODY_TEXT,
            ).pack(fill="x", padx=16, pady=pady)

        label("① 前文原文（选文件或直接粘贴，用完可清空）", pady=(14, 2))
        row = ctk.CTkFrame(self, fg_color="transparent")
        row.pack(fill="x", padx=16)
        soft_button(row, text="选择文件", width=84, command=self._pick_file).pack(side="left")
        soft_button(row, text="清空", width=56, command=self._clear_src).pack(side="left", padx=(6, 8))
        self.src_label = ctk.CTkLabel(row, text="未选择文件", anchor="w",
                                      font=ctk.CTkFont(size=12), text_color=DIM_TEXT)
        self.src_label.pack(side="left", fill="x", expand=True)
        self.src_box = themed_textbox(self, height=150)
        self.src_box.pack(fill="x", padx=16, pady=(6, 0))

        act = ctk.CTkFrame(self, fg_color="transparent")
        act.pack(fill="x", padx=16, pady=8)
        self.incremental = ctk.CTkSwitch(act, text="在现有提要基础上增量更新",
                                         progress_color=PRIMARY, fg_color=TRACK_OFF,
                                         text_color=BODY_TEXT)
        self.incremental.pack(side="left")
        self.condense_btn = ctk.CTkButton(act, text="AI 浓缩为前情提要", width=150,
                                          fg_color=PRIMARY, hover_color=PRIMARY_HOVER,
                                          command=self._condense)
        self.condense_btn.pack(side="right")

        label("② 前情提要（对话时始终自动注入，可直接编辑）")
        self.hint = ctk.CTkLabel(self, text="", anchor="w", justify="left", wraplength=590,
                                 font=ctk.CTkFont(size=12), text_color=DIM_TEXT)
        self.hint.pack(fill="x", padx=16)
        self.recap_box = themed_textbox(self, height=240)
        self.recap_box.pack(fill="both", expand=True, padx=16, pady=(4, 0))

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=16, pady=(8, 0))
        soft_button(btns, text="导出…", command=self._export).pack(
            side="left", expand=True, fill="x", padx=(0, 4))
        soft_button(btns, text="清除提要", command=self._delete).pack(
            side="left", expand=True, fill="x", padx=4)
        ctk.CTkButton(btns, text="保存", fg_color=PRIMARY, hover_color=PRIMARY_HOVER,
                      command=self._save).pack(side="left", expand=True, fill="x", padx=(4, 0))

        ctk.CTkButton(self, text="保存并关闭", height=36, fg_color=PRIMARY,
                      hover_color=PRIMARY_HOVER, font=ctk.CTkFont(size=14, weight="bold"),
                      command=self._save_and_close).pack(fill="x", padx=16, pady=(8, 16))

        existing = self.app.kb.get_recap()
        if existing:
            self.recap_box.insert("1.0", existing["content"])
            self.incremental.select()
            self._set_hint(f"已有前情提要（{len(existing['content'])} 字），可直接编辑或增量更新")
        else:
            self._set_hint("还没有前情提要：导入前文后点「AI 浓缩」，或直接手写")
        self.after(150, self.grab_set)

    def _set_hint(self, msg, error=False):
        self.hint.configure(text=msg, text_color=ERROR_TEXT if error else DIM_TEXT)

    def _pick_file(self):
        path = filedialog.askopenfilename(
            parent=self, title="选择前文",
            filetypes=[("文本 / Word", "*.txt *.md *.docx"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            text = ai_client.extract_text(path)
        except ai_client.AIError as e:
            self._set_hint(str(e), error=True)
            return
        self.src_box.delete("1.0", "end")
        self.src_box.insert("1.0", text)
        self.src_label.configure(text=os.path.basename(path), text_color=BODY_TEXT)
        self._set_hint(f"已载入前文 {len(text)} 字，点「AI 浓缩为前情提要」")

    def _clear_src(self):
        self.src_box.delete("1.0", "end")
        self.src_label.configure(text="未选择文件", text_color=DIM_TEXT)

    def _condense(self):
        src = self.src_box.get("1.0", "end-1c").strip()
        if not src:
            self._set_hint("请先导入或粘贴前文", error=True)
            return
        prev = self.recap_box.get("1.0", "end-1c").strip() if self.incremental.get() else ""
        self.condense_btn.configure(state="disabled")
        self._set_hint("正在浓缩…（长文会分段处理）")
        post = self.app._ui_queue.put

        def hint(msg):
            if self.winfo_exists():
                self._set_hint(msg)

        def work():
            return ai_client.condense_recap(
                self.app.ai_cfg, src, prev_summary=prev,
                status_cb=lambda m: post(lambda m=m: hint(m)))

        def ok(text):
            if not self.winfo_exists():
                return
            self.recap_box.delete("1.0", "end")
            self.recap_box.insert("1.0", text)
            self.condense_btn.configure(state="normal")
            self._set_hint("浓缩完成，可编辑后保存")

        def err(msg):
            if self.winfo_exists():
                self.condense_btn.configure(state="normal")
                self._set_hint(msg, error=True)

        self.app._run_async(work, ok, err)

    def _save(self):
        text = self.recap_box.get("1.0", "end-1c").strip()
        if not text:
            self._set_hint("前情提要为空，未保存", error=True)
            return
        self.app.kb.set_recap(text, source=self.src_label.cget("text"))
        self.app._refresh_entry_picker()
        self._set_hint("已保存，之后每轮对话都会自动带上这段前情提要")

    def _delete(self):
        existing = self.app.kb.get_recap()
        if not existing:
            self._set_hint("当前没有前情提要", error=True)
            return
        self.app.kb.delete(existing["id"])
        self.recap_box.delete("1.0", "end")
        self.app._refresh_entry_picker()
        self._set_hint("已清除前情提要")

    def _export(self):
        text = self.recap_box.get("1.0", "end-1c").strip()
        if not text:
            self._set_hint("没有可导出的内容", error=True)
            return
        target = filedialog.asksaveasfilename(
            parent=self, title="导出前情提要", defaultextension=".txt",
            filetypes=[("文本文件", "*.txt")])
        if not target:
            return
        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            self._set_hint(f"导出失败：{e}", error=True)
            return
        self._set_hint(f"已导出：{os.path.basename(target)}")

    def _save_and_close(self):
        if self.recap_box.get("1.0", "end-1c").strip():
            self._save()
        self.app._update_recap_status()
        self.destroy()


class ApprovalDialog(ctk.CTkToplevel):
    """写操作审批门：AI 要改设定库时，逐条列出待执行操作，由用户勾选批准。

    后台的 agent 线程会阻塞等待这里的结果（见 WriterApp._request_approval）；
    关窗或超时一律按「全部拒绝」处理，保证线程不会永久挂起。
    """

    def __init__(self, app, ops, decisions, event):
        super().__init__(app, fg_color=WINDOW_BG)
        self.app = app
        self.ops = ops
        self.decisions = decisions
        self.event = event
        self.vars = {}
        self.title("AI 请求修改设定库")
        self.geometry("560x420")
        self.transient(app)
        self.protocol("WM_DELETE_WINDOW", self._reject_all)

        ctk.CTkLabel(
            self, text="AI 想对设定库做以下修改。勾选你同意的操作，未勾选的不会执行。",
            anchor="w", justify="left", wraplength=510,
            font=ctk.CTkFont(size=13), text_color=BODY_TEXT,
        ).pack(fill="x", padx=16, pady=(14, 8))

        area = ctk.CTkScrollableFrame(self, fg_color=PANEL_BG, corner_radius=12)
        area.pack(fill="both", expand=True, padx=16)
        for call_id, name, _args, desc in ops:
            var = ctk.BooleanVar(value=not name == "delete_entry")   # 删除默认不勾
            self.vars[call_id] = var
            row = ctk.CTkFrame(area, fg_color=CARD_BG, corner_radius=8)
            row.pack(fill="x", padx=6, pady=3)
            ctk.CTkCheckBox(row, text="", variable=var, width=24, fg_color=PRIMARY,
                            hover_color=PRIMARY_HOVER).pack(side="left", padx=(6, 0))
            ctk.CTkLabel(row, text=desc, anchor="w", justify="left", wraplength=430,
                         font=ctk.CTkFont(size=12), text_color=BODY_TEXT
                         ).pack(side="left", fill="x", expand=True, padx=6, pady=6)

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=16, pady=12)
        soft_button(btns, text="全部拒绝", command=self._reject_all).pack(
            side="left", expand=True, fill="x", padx=(0, 4))
        soft_button(btns, text="全选", command=self._select_all).pack(
            side="left", expand=True, fill="x", padx=4)
        ctk.CTkButton(btns, text="执行勾选项", fg_color=PRIMARY, hover_color=PRIMARY_HOVER,
                      command=self._confirm).pack(side="left", expand=True, fill="x", padx=(4, 0))

        self.after(120, self.grab_set)

    def _select_all(self):
        for v in self.vars.values():
            v.set(True)

    def _finish(self, decisions):
        self.decisions.update(decisions)
        self.event.set()
        self.destroy()

    def _confirm(self):
        self._finish({cid: v.get() for cid, v in self.vars.items()})

    def _reject_all(self):
        self._finish({cid: False for cid in self.vars})


class OverviewDialog(ctk.CTkToplevel):
    """设定总览：把全库词条 AI 归纳成世界观总览，供「整体讨论」时注入。

    显示与设定库的指纹比对结果，词条变动后提示刷新。
    """

    def __init__(self, app):
        super().__init__(app, fg_color=WINDOW_BG)
        self.app = app
        self.title("设定总览")
        self.geometry("640x680")
        self.transient(app)

        ctk.CTkLabel(
            self, text="用于笼统讨论整体世界观。开启写作面板的「整体讨论」开关后，"
                       "这份总览会代替逐条召回被注入对话。",
            anchor="w", justify="left", wraplength=590,
            font=ctk.CTkFont(size=12), text_color=DIM_TEXT,
        ).pack(fill="x", padx=16, pady=(14, 6))

        act = ctk.CTkFrame(self, fg_color="transparent")
        act.pack(fill="x", padx=16)
        self.incremental = ctk.CTkSwitch(act, text="在现有总览基础上增量更新",
                                         progress_color=PRIMARY, fg_color=TRACK_OFF,
                                         text_color=BODY_TEXT)
        self.incremental.pack(side="left")
        self.gen_btn = ctk.CTkButton(act, text="AI 生成总览", width=130,
                                     fg_color=PRIMARY, hover_color=PRIMARY_HOVER,
                                     command=self._generate)
        self.gen_btn.pack(side="right")

        self.hint = ctk.CTkLabel(self, text="", anchor="w", justify="left", wraplength=590,
                                 font=ctk.CTkFont(size=12), text_color=DIM_TEXT)
        self.hint.pack(fill="x", padx=16, pady=(8, 2))

        self.box = themed_textbox(self, height=330)
        self.box.pack(fill="both", expand=True, padx=16)

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=16, pady=(8, 0))
        soft_button(btns, text="导出…", command=self._export).pack(
            side="left", expand=True, fill="x", padx=(0, 4))
        soft_button(btns, text="清除总览", command=self._delete).pack(
            side="left", expand=True, fill="x", padx=4)
        ctk.CTkButton(btns, text="保存", fg_color=PRIMARY, hover_color=PRIMARY_HOVER,
                      command=self._save).pack(side="left", expand=True, fill="x", padx=(4, 0))

        ctk.CTkButton(self, text="保存并关闭", height=36, fg_color=PRIMARY,
                      hover_color=PRIMARY_HOVER, font=ctk.CTkFont(size=14, weight="bold"),
                      command=self._save_and_close).pack(fill="x", padx=16, pady=(8, 16))

        existing = self.app.kb.get_overview()
        if existing:
            self.box.insert("1.0", existing["content"])
            self.incremental.select()
        self._refresh_hint()
        self.after(150, self.grab_set)

    def _refresh_hint(self, msg=None, error=False):
        if msg:
            self.hint.configure(text=msg, text_color=ERROR_TEXT if error else OK_TEXT)
            return
        n = len(self.app.kb.normal_entries())
        if not self.app.kb.get_overview():
            self.hint.configure(text=f"设定库共 {n} 条词条，尚未生成总览",
                                text_color=DIM_TEXT)
        elif self.app.kb.overview_stale():
            self.hint.configure(
                text=f"⚠ 设定库已变动（现有 {n} 条词条），当前总览已过期，建议重新生成",
                text_color=ERROR_TEXT)
        else:
            self.hint.configure(text=f"✓ 总览与设定库一致（{n} 条词条）", text_color=OK_TEXT)

    def _generate(self):
        entries = self.app.kb.normal_entries()
        if not entries:
            self._refresh_hint("设定库里还没有词条", error=True)
            return
        text = "\n\n".join(
            f"[{e['category'] or '未分类'}] {e['title']}"
            + (f"（别名：{e['aliases']}）" if e["aliases"] else "")
            + f"\n{e['content']}" for e in entries)
        prev = self.box.get("1.0", "end-1c").strip() if self.incremental.get() else ""
        self.gen_btn.configure(state="disabled")
        self._refresh_hint("正在归纳…（词条多时会分段处理）")
        post = self.app._ui_queue.put

        def work():
            return ai_client.condense_overview(
                self.app.ai_cfg, text, prev_summary=prev,
                status_cb=lambda m: post(lambda m=m: self._refresh_hint(m)))

        def ok(result):
            if not self.winfo_exists():
                return
            self.box.delete("1.0", "end")
            self.box.insert("1.0", result)
            self.gen_btn.configure(state="normal")
            self._refresh_hint("归纳完成，可编辑后保存")

        def err(msg):
            if self.winfo_exists():
                self.gen_btn.configure(state="normal")
                self._refresh_hint(msg, error=True)

        self.app._run_async(work, ok, err)

    def _save(self):
        text = self.box.get("1.0", "end-1c").strip()
        if not text:
            self._refresh_hint("总览为空，未保存", error=True)
            return
        self.app.kb.set_overview(text, fingerprint=self.app.kb.overview_fingerprint())
        self.app._refresh_entry_picker()
        self._refresh_hint("已保存，开启「整体讨论」即可使用")

    def _delete(self):
        ov = self.app.kb.get_overview()
        if not ov:
            self._refresh_hint("当前没有总览", error=True)
            return
        self.app.kb.delete(ov["id"])
        self.box.delete("1.0", "end")
        self.app._refresh_entry_picker()
        self._refresh_hint("已清除总览")

    def _export(self):
        text = self.box.get("1.0", "end-1c").strip()
        if not text:
            self._refresh_hint("没有可导出的内容", error=True)
            return
        target = filedialog.asksaveasfilename(
            parent=self, title="导出设定总览", defaultextension=".txt",
            filetypes=[("文本文件", "*.txt")])
        if not target:
            return
        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(text)
        except OSError as e:
            self._refresh_hint(f"导出失败：{e}", error=True)
            return
        self._refresh_hint(f"已导出：{os.path.basename(target)}")

    def _save_and_close(self):
        if self.box.get("1.0", "end-1c").strip():
            self._save()
        self.app._update_overview_status()
        self.destroy()


class MemoryDialog(ctk.CTkToplevel):
    """长期记忆管理：查看、编辑、删除、手动新增（与设定库分开存放）。"""

    def __init__(self, app):
        super().__init__(app, fg_color=WINDOW_BG)
        self.app = app
        self.title("长期记忆")
        self.geometry("600x620")
        self.transient(app)
        self.rows = []

        ctk.CTkLabel(
            self, text="AI 在对话中自动记下的长期信息，每轮对话都会注入。",
            anchor="w", wraplength=550, font=ctk.CTkFont(size=12), text_color=DIM_TEXT,
        ).pack(fill="x", padx=16, pady=(14, 6))

        self.area = ctk.CTkScrollableFrame(self, fg_color=PANEL_BG, corner_radius=12,
                                           label_text="记忆条目", label_fg_color=PANEL_BG,
                                           label_text_color=BODY_TEXT)
        self.area.pack(fill="both", expand=True, padx=16)

        add_row = ctk.CTkFrame(self, fg_color="transparent")
        add_row.pack(fill="x", padx=16, pady=(8, 0))
        self.new_entry = themed_entry(add_row, placeholder_text="手动添加一条记忆…")
        self.new_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        soft_button(add_row, text="添加", width=64, command=self._add).pack(side="left")

        self.hint = ctk.CTkLabel(self, text="", anchor="w", font=ctk.CTkFont(size=12),
                                 text_color=DIM_TEXT)
        self.hint.pack(fill="x", padx=16, pady=(6, 0))

        btns = ctk.CTkFrame(self, fg_color="transparent")
        btns.pack(fill="x", padx=16, pady=(6, 16))
        soft_button(btns, text="全部清空", command=self._clear_all).pack(
            side="left", expand=True, fill="x", padx=(0, 4))
        ctk.CTkButton(btns, text="保存修改", fg_color=PRIMARY, hover_color=PRIMARY_HOVER,
                      command=self._save).pack(side="left", expand=True, fill="x", padx=(4, 0))

        self._reload()
        self.after(150, self.grab_set)

    def _set_hint(self, msg, error=False):
        self.hint.configure(text=msg, text_color=ERROR_TEXT if error else OK_TEXT)

    def _reload(self):
        for w in self.area.winfo_children():
            w.destroy()
        self.rows = []
        items = self.app.store.list_memories()
        if not items:
            ctk.CTkLabel(self.area, text="还没有记忆条目", text_color=DIM_TEXT).pack(pady=8)
        for m in items:
            row = ctk.CTkFrame(self.area, fg_color="transparent")
            row.pack(fill="x", padx=4, pady=2)
            entry = themed_entry(row)
            entry.insert(0, m["content"])
            entry.pack(side="left", fill="x", expand=True)
            ctk.CTkButton(row, text="✕", width=28, fg_color="transparent",
                          text_color=ERROR_TEXT, hover_color=SOFT_BG,
                          command=lambda i=m["id"]: self._delete(i)).pack(side="left", padx=(6, 0))
            self.rows.append((m["id"], entry))

    def _add(self):
        text = self.new_entry.get().strip()
        if not text:
            return
        if self.app.store.add_memory(text, self.app.session_id) is None:
            self._set_hint("这条记忆已存在", error=True)
            return
        self.new_entry.delete(0, "end")
        self._reload()
        self._set_hint("已添加")

    def _delete(self, memory_id):
        self.app.store.delete_memory(memory_id)
        self._reload()
        self._set_hint("已删除")

    def _clear_all(self):
        for m in self.app.store.list_memories():
            self.app.store.delete_memory(m["id"])
        self._reload()
        self._set_hint("已清空全部记忆")

    def _save(self):
        changed = 0
        for mid, entry in self.rows:
            text = entry.get().strip()
            if not text:
                continue
            self.app.store.update_memory(mid, text)
            changed += 1
        self._set_hint(f"已保存 {changed} 条")


class WriterApp(ctk.CTk):
    def __init__(self, db_path=None, chat_db_path=None):
        super().__init__(fg_color=WINDOW_BG)
        ctk.set_appearance_mode("light")
        self.title("✍️ DiceBot 写作助手")
        self.geometry("1040x760")
        self.minsize(960, 660)

        self.ai_cfg = ai_client.load_config()
        self.kb = KnowledgeBase(db_path) if db_path else KnowledgeBase()
        self.store = ChatStore(chat_db_path) if chat_db_path else ChatStore()
        self.selected_id = None          # 设定库当前选中词条
        self._kb_expanded = {}           # 设定库侧栏：类别 -> 是否展开
        self._kb_last_results = []       # 最近一次检索结果（折叠切换时复用）
        self.proposal_rows = []          # 录入提案行
        self._collected_proposals = []   # 本次提取已得的提案（供续传做种）
        self._ingest_partial = None      # (next_index, total, msg)：中断续传状态
        self.chat_history = []           # [{role, content}]
        self.chat_messages = []          # ChatMessage 控件
        self.entry_checks = {}           # 当前可见的 entry_id -> BooleanVar
        self.entry_selected = set()      # 强制包含的词条 id（跨筛选保留）
        sessions = self.store.list_sessions()
        self.session_id = sessions[0]["id"] if sessions else self.store.create_session()

        self._ui_queue = queue.Queue()
        self.after(100, self._poll_ui_queue)

        top = ctk.CTkFrame(self, fg_color="transparent")
        top.pack(fill="x", padx=12, pady=(10, 0))
        self.nav = ctk.CTkSegmentedButton(
            top, values=[PANEL_KB, PANEL_INGEST, PANEL_CHAT, PANEL_DICE],
            command=self._show_panel,
            fg_color=SOFT_BG, selected_color=PRIMARY, selected_hover_color=PRIMARY_HOVER,
            unselected_color=SOFT_BG, unselected_hover_color=SOFT_HOVER, text_color=SEG_TEXT,
        )
        self.nav.set(PANEL_KB)
        self.nav.pack(side="left")
        soft_button(top, text="AI 设置…", width=90,
                    command=lambda: AISettingsDialog(self)).pack(side="right")

        self.status_label = ctk.CTkLabel(
            self, text=f"设定库：{self.kb.count()} 条词条", anchor="w",
            font=ctk.CTkFont(size=12), text_color=DIM_TEXT,
        )
        self.status_label.pack(fill="x", padx=14, pady=(4, 0))

        self.container = ctk.CTkFrame(self, fg_color="transparent")
        self.container.pack(fill="both", expand=True, padx=10, pady=8)

        self.panels = {
            PANEL_KB: self._build_kb_panel(),
            PANEL_INGEST: self._build_ingest_panel(),
            PANEL_CHAT: self._build_chat_panel(),
            PANEL_DICE: DicePanel(self.container, self),   # 骰子作为一个面板并入
        }
        self._show_panel(PANEL_KB)
        self._refresh_session_menu()
        self._load_session(self.session_id)

    # -------------------------------------------------- 线程/状态基建

    def _poll_ui_queue(self):
        try:
            while True:
                cb = self._ui_queue.get_nowait()
                try:
                    cb()
                except Exception:
                    # 单个回调出错不能拖垮整个轮询循环，否则界面会永久卡住
                    ai_client._log_error("UI 回调异常", traceback.format_exc())
                    self._status("界面更新出错（详情见 ai_error.log）", error=True)
        except queue.Empty:
            pass
        self.after(100, self._poll_ui_queue)

    def _run_async(self, work, on_ok, on_err=None, busy=None):
        """在后台线程跑 work()，成功回 on_ok(result)，失败回 on_err(msg)。

        捕获所有异常（不只 AIError），避免后台线程静默死掉导致界面卡在忙碌态。
        """
        if not self.ai_cfg.get("api_key"):
            self._status("请先在「AI 设置」中配置 API 密钥", error=True)
            return
        if busy:
            self._status(busy)
        post = self._ui_queue.put

        def run():
            try:
                result = work()
            except ai_client.AIError as e:
                msg = str(e)
                post(lambda: (on_err or self._default_err)(msg))
                return
            except Exception as e:
                ai_client._log_error("写作助手后台任务异常", traceback.format_exc())
                msg = f"出错了：{e}（详情见 ai_error.log）"
                post(lambda: (on_err or self._default_err)(msg))
                return
            post(lambda: on_ok(result))

        threading.Thread(target=run, daemon=True).start()

    def _default_err(self, msg):
        self._status(msg, error=True)

    def _status(self, msg, error=False):
        self.status_label.configure(text=msg, text_color=ERROR_TEXT if error else OK_TEXT)

    def _show_status(self, msg, error=False):   # 供 AISettingsDialog 复用
        self._status(msg, error)

    def _show_panel(self, name):
        for key, frame in self.panels.items():
            frame.pack_forget()
        self.panels[name].pack(fill="both", expand=True)
        if name == PANEL_CHAT:
            self._refresh_entry_picker()
            self._update_recap_status()
            self._update_overview_status()

    # -------------------------------------------------- 面板一：设定库

    def _build_kb_panel(self):
        f = ctk.CTkFrame(self.container, fg_color="transparent")
        f.grid_columnconfigure(0, weight=1)
        f.grid_columnconfigure(1, weight=2)
        f.grid_rowconfigure(1, weight=1)

        bar = ctk.CTkFrame(f, fg_color=PANEL_BG, corner_radius=12)
        bar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 8))
        self.kw_entry = themed_entry(bar, width=180, placeholder_text="关键字…")
        self.kw_entry.pack(side="left", padx=(10, 6), pady=8)
        self.kw_entry.bind("<Return>", lambda e: self._kb_search())
        self.cat_filter = themed_optionmenu(bar, width=90, values=["全部"] + CATEGORIES)
        self.cat_filter.set("全部")
        self.cat_filter.pack(side="left", padx=(0, 6))
        self.from_entry = themed_entry(bar, width=104, placeholder_text="起 YYYY-MM-DD")
        self.from_entry.pack(side="left", padx=(0, 4))
        self.to_entry = themed_entry(bar, width=104, placeholder_text="止 YYYY-MM-DD")
        self.to_entry.pack(side="left", padx=(0, 6))
        self.exact_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(bar, text="精确", variable=self.exact_var, width=60,
                        text_color=BODY_TEXT, fg_color=PRIMARY,
                        hover_color=PRIMARY_HOVER).pack(side="left")
        soft_button(bar, text="搜索", width=64, command=self._kb_search).pack(side="left", padx=4)
        soft_button(bar, text="新建", width=64, command=self._kb_new).pack(side="left")

        self.kb_list = ctk.CTkScrollableFrame(f, fg_color=CARD_BG, corner_radius=12,
                                              label_text="词条", label_fg_color=PANEL_BG,
                                              label_text_color=BODY_TEXT)
        self.kb_list.grid(row=1, column=0, sticky="nsew", padx=(0, 6))

        detail = ctk.CTkFrame(f, fg_color=CARD_BG, corner_radius=12,
                              border_width=1, border_color=BORDER)
        detail.grid(row=1, column=1, sticky="nsew")
        detail.grid_columnconfigure(1, weight=1)
        r = 0
        self.d_title = self._detail_field(detail, "名称", r); r += 1
        self.d_category = self._detail_field(detail, "类别", r); r += 1
        self.d_tags = self._detail_field(detail, "标签", r); r += 1
        self.d_aliases = self._detail_field(detail, "别名", r); r += 1
        self.d_source = self._detail_field(detail, "来源", r); r += 1
        ctk.CTkLabel(detail, text="正文", text_color=BODY_TEXT,
                     font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=r, column=0, sticky="nw", padx=(10, 4), pady=(8, 2)); r += 1
        self.d_content = themed_textbox(detail, height=220)
        self.d_content.grid(row=r, column=0, columnspan=2, sticky="nsew", padx=10)
        detail.grid_rowconfigure(r, weight=1); r += 1
        btns = ctk.CTkFrame(detail, fg_color="transparent")
        btns.grid(row=r, column=0, columnspan=2, sticky="ew", padx=10, pady=10)
        ctk.CTkButton(btns, text="保存", fg_color=PRIMARY, hover_color=PRIMARY_HOVER,
                      command=self._kb_save).pack(side="left", expand=True, fill="x", padx=(0, 4))
        soft_button(btns, text="删除", command=self._kb_delete).pack(
            side="left", expand=True, fill="x", padx=(4, 0))

        self._kb_search()
        return f

    def _detail_field(self, parent, label, row):
        ctk.CTkLabel(parent, text=label, text_color=BODY_TEXT,
                     font=ctk.CTkFont(size=12, weight="bold")).grid(
            row=row, column=0, sticky="w", padx=(10, 4), pady=(8, 0))
        e = themed_entry(parent)
        e.grid(row=row, column=1, sticky="ew", padx=(0, 10), pady=(8, 0))
        return e

    def _kb_search(self):
        cat = self.cat_filter.get()
        results = self.kb.search(
            self.kw_entry.get(), category=None if cat == "全部" else cat,
            date_from=self.from_entry.get().strip() or None,
            date_to=self.to_entry.get().strip() or None,
            exact=self.exact_var.get(),
        )
        searching = bool(self.kw_entry.get().strip())
        self._kb_last_results = results
        self._render_kb_tree(results, auto_expand=searching)
        self._status(f"检索到 {len(results)} 条")

    def _render_kb_tree(self, results, auto_expand=False):
        """按类别分组渲染成可折叠的树：大标题一行，展开后是组内词条。"""
        for w in self.kb_list.winfo_children():
            w.destroy()
        if not results:
            ctk.CTkLabel(self.kb_list, text="没有匹配的词条", text_color=DIM_TEXT).pack(pady=8)
            return

        groups = {}
        for e in results:
            groups.setdefault(e["category"] or "未分类", []).append(e)

        for cat in sorted(groups):
            items = groups[cat]
            if auto_expand:                       # 搜索时自动展开命中的组
                self._kb_expanded[cat] = True
            opened = self._kb_expanded.get(cat, False)
            ctk.CTkButton(
                self.kb_list, text=f"{'▼' if opened else '▶'}  {cat}（{len(items)}）",
                anchor="w", fg_color=PANEL_BG, text_color=BODY_TEXT, hover_color=SOFT_BG,
                font=ctk.CTkFont(size=13, weight="bold"), corner_radius=6,
                command=lambda c=cat: self._toggle_kb_group(c),
            ).pack(fill="x", padx=4, pady=(4, 1))
            if not opened:
                continue
            for e in items:
                ctk.CTkButton(
                    self.kb_list, text="    " + e["title"], anchor="w",
                    fg_color="transparent", text_color=BODY_TEXT, hover_color=SOFT_BG,
                    command=lambda i=e["id"]: self._kb_load(i),
                ).pack(fill="x", padx=(12, 4), pady=1)

    def _toggle_kb_group(self, cat):
        # 只重渲染，不重新检索——否则搜索态下 auto_expand 会把刚收起的组又打开
        self._kb_expanded[cat] = not self._kb_expanded.get(cat, False)
        self._render_kb_tree(self._kb_last_results, auto_expand=False)

    def _kb_load(self, entry_id):
        e = self.kb.get(entry_id)
        if not e:
            return
        self.selected_id = entry_id
        for widget, key in ((self.d_title, "title"), (self.d_category, "category"),
                            (self.d_tags, "tags"), (self.d_aliases, "aliases"),
                            (self.d_source, "source")):
            widget.delete(0, "end")
            widget.insert(0, e[key])
        self.d_content.delete("1.0", "end")
        self.d_content.insert("1.0", e["content"])

    def _kb_new(self):
        self.selected_id = None
        for w in (self.d_title, self.d_category, self.d_tags, self.d_aliases, self.d_source):
            w.delete(0, "end")
        self.d_content.delete("1.0", "end")
        self.d_title.focus_set()
        self._status("新建词条：填写后点保存")

    def _kb_save(self):
        title = self.d_title.get().strip()
        if not title:
            self._status("名称不能为空", error=True)
            return
        fields = dict(
            title=title, category=self.d_category.get(), tags=self.d_tags.get(),
            aliases=self.d_aliases.get(), source=self.d_source.get(),
            content=self.d_content.get("1.0", "end-1c"),
        )
        if self.selected_id:
            self.kb.update(self.selected_id, **fields)
            self._status(f"已保存：{title}")
        else:
            self.selected_id = self.kb.add(**fields)
            self._status(f"已新建：{title}")
        self._refresh_categories()
        self._kb_search()

    def _kb_delete(self):
        if not self.selected_id:
            self._status("未选中词条", error=True)
            return
        self.kb.delete(self.selected_id)
        self.selected_id = None
        self._kb_new()
        self._kb_search()
        self._status("已删除")

    def _refresh_categories(self):
        cats = ["全部"] + CATEGORIES
        for c in self.kb.categories():
            if c not in cats:
                cats.append(c)
        self.cat_filter.configure(values=cats)

    # -------------------------------------------------- 面板二：录入

    def _build_ingest_panel(self):
        f = ctk.CTkFrame(self.container, fg_color="transparent")
        f.grid_columnconfigure(0, weight=1)
        f.grid_rowconfigure(3, weight=1)

        src = ctk.CTkFrame(f, fg_color="transparent")
        src.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        soft_button(src, text="选择文件（txt/md/docx）", width=180,
                    command=self._ingest_file).pack(side="left")
        self.ingest_src_label = ctk.CTkLabel(src, text="或在下方粘贴文本 / 对话记录",
                                             text_color=DIM_TEXT, font=ctk.CTkFont(size=12))
        self.ingest_src_label.pack(side="left", padx=(10, 0))

        self.ingest_input = themed_textbox(f, height=150)
        self.ingest_input.grid(row=1, column=0, sticky="ew")

        act = ctk.CTkFrame(f, fg_color="transparent")
        act.grid(row=2, column=0, sticky="ew", pady=6)
        ctk.CTkButton(act, text="🤖 AI 提取词条", fg_color=PRIMARY, hover_color=PRIMARY_HOVER,
                      command=self._ingest_extract).pack(side="left")
        self.resume_btn = soft_button(act, text="继续提取", width=88,
                                      command=self._ingest_resume)
        self.resume_btn.pack(side="left", padx=8)
        self.resume_btn.configure(state="disabled")
        soft_button(act, text="全选/全不选", width=110,
                    command=self._toggle_all_proposals).pack(side="left")
        ctk.CTkLabel(act, text="每批字数", font=ctk.CTkFont(size=12),
                     text_color=DIM_TEXT).pack(side="left", padx=(12, 4))
        self.chunk_entry = themed_entry(act, width=64, justify="center")
        self.chunk_entry.insert(0, str(self.ai_cfg.get("extract_chunk") or ingest.EXTRACT_CHUNK))
        self.chunk_entry.pack(side="left")
        ctk.CTkButton(act, text="✅ 提交勾选项", fg_color=PRIMARY, hover_color=PRIMARY_HOVER,
                      command=self._ingest_commit).pack(side="right")

        self.proposal_area = ctk.CTkScrollableFrame(
            f, fg_color=PANEL_BG, corner_radius=12, label_text="提取的词条（可编辑）",
            label_fg_color=PANEL_BG, label_text_color=BODY_TEXT)
        self.proposal_area.grid(row=3, column=0, sticky="nsew")
        return f

    def _ingest_file(self):
        path = filedialog.askopenfilename(
            title="选择文件", filetypes=[("文本/Word", "*.txt *.md *.docx"), ("所有文件", "*.*")])
        if not path:
            return
        try:
            text = ai_client.extract_text(path)
        except ai_client.AIError as e:
            self._status(str(e), error=True)
            return
        self.ingest_input.delete("1.0", "end")
        self.ingest_input.insert("1.0", text)
        self.ingest_src_label.configure(text=f"已载入：{os.path.basename(path)}")
        self._status("文件已载入，点「AI 提取词条」")

    def _chunk_size(self):
        """读取并持久化"每批字数"设置。"""
        try:
            size = max(500, int(self.chunk_entry.get().strip()))
        except (ValueError, AttributeError):
            size = ingest.EXTRACT_CHUNK
        if self.ai_cfg.get("extract_chunk") != size:
            self.ai_cfg["extract_chunk"] = size
            ai_client.save_config(self.ai_cfg)
        return size

    def _ingest_extract(self, start_index=0, seed=None):
        raw = self.ingest_input.get("1.0", "end-1c").strip()
        if not raw:
            self._status("请先粘贴文本或选择文件", error=True)
            return
        src = self.ingest_src_label.cget("text").replace("已载入：", "")
        size = self._chunk_size()
        post = self._ui_queue.put

        def status_cb(msg):
            post(lambda: self._status(msg))

        def work():
            partial = None
            try:
                proposals = ingest.extract_entries(
                    self.ai_cfg, raw, source=src, status_cb=status_cb,
                    chunk_size=size, start_index=start_index, seed=seed)
            except ingest.PartialExtraction as e:
                # 中途失败：保留已提取的部分，记下续传位置
                proposals, partial = e.entries, (e.next_index, e.total, str(e))
            status_cb("正在核对是否与已有词条重复…")
            return ingest.classify_proposals(proposals, self.kb), partial

        self._run_async(work, self._show_proposals, busy="正在提取词条…（长文可能较慢）")

    def _ingest_resume(self):
        if not self._ingest_partial:
            return
        next_index, _total, _msg = self._ingest_partial
        seed = [{k: v for k, v in p.items() if k not in ("status", "matches")}
                for p in self._collected_proposals]
        self._ingest_extract(start_index=next_index, seed=seed)

    def _show_proposals(self, payload):
        proposals, partial = payload if isinstance(payload, tuple) else (payload, None)
        for w in self.proposal_area.winfo_children():
            w.destroy()
        self.proposal_rows = []
        self._collected_proposals = proposals
        self._ingest_partial = partial
        self.resume_btn.configure(state="normal" if partial else "disabled")

        if not proposals:
            ctk.CTkLabel(self.proposal_area, text="没有提取到词条",
                         text_color=DIM_TEXT).pack(pady=8)
            self._status(f"提取中断：{partial[2]}" if partial else "没有提取到词条",
                         error=bool(partial))
            return
        for p in proposals:
            row = ProposalRow(self.proposal_area, self, p)
            row.pack(fill="x", padx=6, pady=4)
            self.proposal_rows.append(row)
        dup = sum(1 for p in proposals if p["status"] == "duplicate")
        if partial:
            done, total, msg = partial
            self._status(f"已提取 {len(proposals)} 条，但第 {done + 1}/{total} 批失败："
                         f"{msg} — 可先提交已有的，或点「继续提取」续传", error=True)
        else:
            self._status(f"提取到 {len(proposals)} 条（{dup} 条疑似已有），核对后提交")

    def _toggle_all_proposals(self):
        if not self.proposal_rows:
            return
        target = not all(r.checked.get() for r in self.proposal_rows)
        for r in self.proposal_rows:
            r.checked.set(target)

    def _ingest_commit(self):
        added = updated = 0
        for row in self.proposal_rows:
            d = row.data()
            if not d["checked"] or not d["title"]:
                continue
            if d["augment_id"]:
                existing = self.kb.get(d["augment_id"])
                tags = ", ".join(dict.fromkeys(
                    split_list(existing["tags"]) + split_list(d["tags"])))
                aliases = ", ".join(dict.fromkeys(
                    split_list(existing["aliases"]) + split_list(d["aliases"])))
                self.kb.update(d["augment_id"], content=d["content"],
                               tags=tags, aliases=aliases)
                updated += 1
            else:
                self.kb.add(d["title"], category=d["category"], content=d["content"],
                            tags=d["tags"], aliases=d["aliases"], source="录入")
                added += 1
        for w in self.proposal_area.winfo_children():
            w.destroy()
        self.proposal_rows = []
        self._collected_proposals = []
        self._ingest_partial = None
        self.resume_btn.configure(state="disabled")
        self._refresh_categories()
        self._status(f"已入库：新增 {added} 条，增补 {updated} 条")

    # -------------------------------------------------- 面板三：写作讨论

    def _build_chat_panel(self):
        f = ctk.CTkFrame(self.container, fg_color="transparent")
        f.grid_columnconfigure(1, weight=1)
        f.grid_rowconfigure(0, weight=1)

        side = ctk.CTkFrame(f, fg_color=PANEL_BG, corner_radius=12, width=240)
        side.grid(row=0, column=0, sticky="nsew", padx=(0, 8))
        side.grid_propagate(False)
        section(side, "🧠 设定注入").pack(fill="x", padx=10, pady=(10, 2))
        self.smart_recall = ctk.CTkSwitch(side, text="AI 自动召回设定", progress_color=PRIMARY,
                                          fg_color=TRACK_OFF, text_color=BODY_TEXT)
        self.smart_recall.select()
        self.smart_recall.pack(anchor="w", padx=10)
        self.auto_kw = ctk.CTkSwitch(side, text="关键词匹配（兜底）", progress_color=PRIMARY,
                                     fg_color=TRACK_OFF, text_color=BODY_TEXT)
        self.auto_kw.select()
        self.auto_kw.pack(anchor="w", padx=10, pady=(4, 0))
        ov_row = ctk.CTkFrame(side, fg_color="transparent")
        ov_row.pack(fill="x", padx=10, pady=(4, 0))
        self.whole_world = ctk.CTkSwitch(ov_row, text="整体讨论", progress_color=PRIMARY,
                                         fg_color=TRACK_OFF, text_color=BODY_TEXT)
        self.whole_world.pack(side="left")
        soft_button(ov_row, text="总览…", width=60,
                    command=self._open_overview).pack(side="right")
        self.overview_status = ctk.CTkLabel(side, text="", anchor="w",
                                            font=ctk.CTkFont(size=11), text_color=DIM_TEXT)
        self.overview_status.pack(fill="x", padx=10)
        ctk.CTkLabel(side, text="下面勾选的会强制包含（可留空）", anchor="w",
                     font=ctk.CTkFont(size=11), text_color=DIM_TEXT
                     ).pack(fill="x", padx=10, pady=(6, 0))
        self.entry_filter = themed_entry(side, placeholder_text="筛选词条…")
        self.entry_filter.pack(fill="x", padx=10, pady=(6, 2))
        self.entry_filter.bind("<KeyRelease>", lambda e: self._refresh_entry_picker())
        prow = ctk.CTkFrame(side, fg_color="transparent")
        prow.pack(fill="x", padx=10)
        soft_button(prow, text="刷新", width=56,
                    command=self._refresh_entry_picker).pack(side="left")
        soft_button(prow, text="全选/全不选", width=104,
                    command=self._toggle_all_entries).pack(side="left", padx=(6, 0))
        self.entry_picker = ctk.CTkScrollableFrame(side, fg_color=CARD_BG, corner_radius=8)
        self.entry_picker.pack(fill="both", expand=True, padx=10, pady=(6, 6))
        self.allow_write = ctk.CTkSwitch(side, text="允许 AI 改设定库", progress_color=PRIMARY,
                                         fg_color=TRACK_OFF, text_color=BODY_TEXT)
        self.allow_write.select()
        self.allow_write.pack(anchor="w", padx=10, pady=(6, 0))
        mem_row = ctk.CTkFrame(side, fg_color="transparent")
        mem_row.pack(fill="x", padx=10, pady=(0, 10))
        self.mem_switch = ctk.CTkSwitch(mem_row, text="AI 记忆", progress_color=PRIMARY,
                                        fg_color=TRACK_OFF, text_color=BODY_TEXT)
        self.mem_switch.select()
        self.mem_switch.pack(side="left")
        soft_button(mem_row, text="管理…", width=64,
                    command=self._open_memory).pack(side="right")

        right = ctk.CTkFrame(f, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew")
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=1)

        sess = ctk.CTkFrame(right, fg_color="transparent")
        sess.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        ctk.CTkLabel(sess, text="会话", font=ctk.CTkFont(size=12, weight="bold"),
                     text_color=BODY_TEXT).pack(side="left")
        self.session_menu = themed_optionmenu(sess, width=200, values=["（无）"],
                                              command=self._on_session_pick)
        self.session_menu.pack(side="left", padx=6)
        soft_button(sess, text="新建", width=56, command=self._new_session).pack(side="left")
        soft_button(sess, text="重命名", width=64,
                    command=self._rename_session).pack(side="left", padx=6)
        soft_button(sess, text="删除", width=56, command=self._delete_session).pack(side="left")

        self.chat_area = ctk.CTkScrollableFrame(right, fg_color=CARD_BG, corner_radius=12,
                                               label_text="对话", label_fg_color=PANEL_BG,
                                               label_text_color=BODY_TEXT)
        self.chat_area.grid(row=1, column=0, sticky="nsew")

        toolbar = ctk.CTkFrame(right, fg_color="transparent")
        toolbar.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        soft_button(toolbar, text="勾选对话→录入", width=130,
                    command=self._chat_to_ingest).pack(side="left")
        soft_button(toolbar, text="清空对话", width=90,
                    command=self._chat_clear).pack(side="left", padx=8)
        soft_button(toolbar, text="📖 前情提要…", width=110,
                    command=self._open_recap).pack(side="left")
        self.recap_status = ctk.CTkLabel(toolbar, text="", anchor="w",
                                         font=ctk.CTkFont(size=12), text_color=DIM_TEXT)
        self.recap_status.pack(side="left", padx=(8, 0))

        inp = ctk.CTkFrame(right, fg_color="transparent")
        inp.grid(row=3, column=0, sticky="ew", pady=(6, 0))
        inp.grid_columnconfigure(0, weight=1)
        self.chat_input = themed_textbox(inp, height=70)
        self.chat_input.grid(row=0, column=0, sticky="ew", padx=(0, 6))
        ctk.CTkButton(inp, text="发送", width=72, fg_color=PRIMARY, hover_color=PRIMARY_HOVER,
                      command=self._chat_send).grid(row=0, column=1, sticky="ns")
        return f

    def _refresh_entry_picker(self):
        if not hasattr(self, "entry_picker"):
            return
        kw = self.entry_filter.get().strip() if hasattr(self, "entry_filter") else ""
        for w in self.entry_picker.winfo_children():
            w.destroy()
        self.entry_checks = {}
        entries = [e for e in (self.kb.search(kw) if kw else self.kb.list_entries())
                   if e["category"] not in SPECIAL_CATEGORIES]
        if not entries:
            ctk.CTkLabel(self.entry_picker, text="没有词条" if kw else "设定库为空",
                         text_color=DIM_TEXT).pack(pady=6)
        for e in entries:
            var = ctk.BooleanVar(value=e["id"] in self.entry_selected)
            self.entry_checks[e["id"]] = var
            ctk.CTkCheckBox(
                self.entry_picker, text=f"{e['title']}·{e['category'] or '—'}",
                variable=var, font=ctk.CTkFont(size=12), text_color=BODY_TEXT,
                fg_color=PRIMARY, hover_color=PRIMARY_HOVER,
                command=lambda i=e["id"], v=var: self._on_entry_toggle(i, v),
            ).pack(anchor="w", padx=6, pady=1)

    def _on_entry_toggle(self, entry_id, var):
        if var.get():
            self.entry_selected.add(entry_id)
        else:
            self.entry_selected.discard(entry_id)

    def _toggle_all_entries(self):
        if not self.entry_checks:
            return
        target = not all(v.get() for v in self.entry_checks.values())
        for i, v in self.entry_checks.items():
            v.set(target)
            self._on_entry_toggle(i, v)

    def _context_blocks(self, ids):
        """把词条 id 集合渲染成参考文本，受 CONTEXT_CHAR_LIMIT 限制。"""
        blocks, total = [], 0
        for i in ids:
            e = self.kb.get(i)
            if not e:
                continue
            block = f"[{e['category'] or '设定'}] {e['title']}\n{e['content']}"
            if total + len(block) > CONTEXT_CHAR_LIMIT:
                break
            blocks.append(block)
            total += len(block)
        return "\n---\n".join(blocks)

    def _resolve_entry_ids(self, user_text, forced, use_mention, use_smart, status_cb=None):
        """决定本轮注入哪些词条：强制包含 + 关键词命中 + AI 自动召回。

        在后台线程调用（会发网络请求）；AI 召回失败时静默退回关键词兜底。
        """
        chosen = set(forced)
        entries = self.kb.normal_entries()
        if use_mention:
            chosen |= retrieve.mention_ids(entries, user_text)
        if use_smart and entries:
            try:
                chosen |= set(retrieve.pick_relevant(
                    self.ai_cfg, user_text, entries, status_cb=status_cb))
            except ai_client.AIError as e:
                ai_client._log_error("自动召回失败，已退回关键词匹配", str(e))
                if status_cb:
                    status_cb("自动召回失败，已退回关键词匹配")
                chosen |= retrieve.mention_ids(entries, user_text)
        return chosen

    # -------------------------------------------------- Agent 工具循环

    def _request_approval(self, ops):
        """从后台线程请求用户批准写操作，阻塞等待；超时/关窗按全部拒绝。"""
        event = threading.Event()
        decisions = {}
        self._ui_queue.put(lambda: ApprovalDialog(self, ops, decisions, event))
        if not event.wait(timeout=APPROVAL_TIMEOUT):
            ai_client._log_error("审批超时", f"{len(ops)} 个写操作未获批准，按拒绝处理")
            return {}
        return decisions

    def _agent_turn(self, messages, allow_write, status_cb):
        """带 function calling 的对话循环，返回 (回复文本, 是否改动了设定库)。

        读工具自动执行；写工具先经 ApprovalDialog 审批，批准的才落库，
        拒绝的把结果回填给模型，让它据此调整措辞。
        """
        tools = agent_tools.schemas(allow_write=allow_write)
        changed = False
        for _round in range(MAX_TOOL_ROUNDS):
            try:
                message, _finish = ai_client.chat_with_tools(self.ai_cfg, messages, tools)
            except ai_client.AIError as e:
                if _round == 0 and not changed:
                    raise          # 首轮失败交给上层处理（含 tools 不支持时的降级）
                # 工具已经执行过（可能已改库），别让后续请求失败把这轮成果全丢掉
                ai_client._log_error("agent 循环中断", str(e))
                return f"（工具已执行完毕，但后续对话请求失败：{e}）", changed
            calls = message.get("tool_calls") or []
            if not calls:
                return (message.get("content") or "").strip(), changed
            messages.append(message)          # 助手消息（含 tool_calls）必须原样回填

            results, pending = {}, []
            for call in calls:
                fn = call.get("function") or {}
                name = fn.get("name") or ""
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except json.JSONDecodeError:
                    args = {}
                if agent_tools.is_write(name):
                    pending.append((call.get("id"), name, args))
                else:
                    status_cb(f"AI 正在查设定库：{name}")
                    results[call.get("id")] = agent_tools.execute(self.kb, name, args)

            if pending:
                status_cb("AI 请求修改设定库，等待你确认…")
                ops = [(cid, n, a, agent_tools.describe(n, a, self.kb))
                       for cid, n, a in pending]
                decisions = self._request_approval(ops)
                for cid, name, args in pending:
                    if decisions.get(cid):
                        results[cid] = agent_tools.execute(self.kb, name, args)
                        changed = True
                    else:
                        results[cid] = ("用户拒绝了此操作，设定库未被修改。"
                                        "不要重复尝试，请改为在回复里说明你的建议。")

            for call in calls:
                messages.append({
                    "role": "tool",
                    "tool_call_id": call.get("id"),
                    "content": results.get(call.get("id"), ""),
                })
        return "（工具调用轮次过多，已停止。请换个说法再试。）", changed

    def _append_message(self, role, text):
        msg = ChatMessage(self.chat_area, role, text)
        msg.pack(fill="x", padx=6, pady=4)
        self.chat_messages.append(msg)
        self.chat_area._parent_canvas.yview_moveto(1.0)

    def _chat_send(self):
        text = self.chat_input.get("1.0", "end-1c").strip()
        if not text:
            return
        self.chat_input.delete("1.0", "end")
        self._append_message("user", text)

        # 主线程快照，后台线程只读这些值（Tk 变量不能跨线程访问）
        forced = set(self.entry_selected)
        use_mention = bool(self.auto_kw.get())
        use_smart = bool(self.smart_recall.get())
        use_memory = bool(self.mem_switch.get())
        whole = bool(self.whole_world.get())
        allow_write = bool(self.allow_write.get())
        history_snapshot = list(self.chat_history)
        post = self._ui_queue.put

        def status_cb(msg):
            post(lambda m=msg: self._status(m))

        def work():
            # 整体讨论：注入设定总览，跳过逐条召回
            if whole:
                ids, context = set(), ""
            else:
                ids = self._resolve_entry_ids(text, forced, use_mention, use_smart, status_cb)
                context = self._context_blocks(ids)
            recap = self.kb.get_recap()
            sections = []
            if use_memory:
                mem_text = self.store.memory_text()
                if mem_text:
                    sections.append("【长期记忆】\n" + mem_text)
            if whole:
                ov = self.kb.get_overview()
                if ov and ov["content"].strip():
                    sections.append("【设定总览】\n" + ov["content"].strip())
                else:
                    status_cb("尚未生成设定总览，本轮未注入整体设定")
            if recap and recap["content"].strip():
                sections.append("【前情提要】\n" + recap["content"].strip())
            if context:
                sections.append("【设定库参考】\n" + context)
            user_content = ("\n\n".join(sections) + "\n\n---\n\n" + text) if sections else text
            system = WRITER_SYSTEM + "\n\n【你的人设】\n" + ai_client.system_prompt(self.ai_cfg)
            messages = ([{"role": "system", "content": system}]
                        + history_snapshot
                        + [{"role": "user", "content": user_content}])
            status_cb(f"已注入 {len(ids)} 条设定，生成中…" if ids else "生成中…")
            try:
                reply, changed = self._agent_turn(messages, allow_write, status_cb)
            except ai_client.AIError as e:
                if getattr(e, "status", None) != 400:
                    raise
                # 端点不支持 tools：本轮降级为普通对话
                ai_client._log_error("端点不支持 tools(400)，降级为普通对话", str(e))
                status_cb("当前端点不支持工具调用，已降级为普通对话")
                reply = ai_client.finalize_reply(
                    *ai_client.chat_messages(self.ai_cfg, messages))
                changed = False
            return text, user_content, len(ids), reply, changed

        self._run_async(work, self._chat_reply, busy="正在准备参考资料…")

    def _chat_reply(self, payload):
        """回到主线程：把本轮 user/assistant 消息落进历史、存档并渲染。"""
        raw_user, user_content, n_ids, text, kb_changed = payload
        if kb_changed:          # AI 改过设定库，刷新列表与勾选区
            self._kb_search()
            self._refresh_entry_picker()
            self._update_overview_status()
        self.chat_history.append({"role": "user", "content": user_content})
        self.chat_history.append({"role": "assistant", "content": text})
        self.store.add_message(self.session_id, "user", user_content, display=raw_user)
        self.store.add_message(self.session_id, "assistant", text, display=text)
        self._append_message("assistant", text)
        self._status(f"对话已更新（本轮参考 {n_ids} 条设定）")
        if self.mem_switch.get():
            self._extract_memory(raw_user, text)

    def _open_recap(self):
        RecapDialog(self)

    # -------------------------------------------------- 会话与长期记忆

    @staticmethod
    def _session_label(s):
        return f"{s['title']}  #{s['id']}"

    def _refresh_session_menu(self):
        sessions = self.store.list_sessions()
        if not sessions:
            self.session_id = self.store.create_session()
            sessions = self.store.list_sessions()
        self._session_labels = {self._session_label(s): s["id"] for s in sessions}
        self.session_menu.configure(values=list(self._session_labels))
        cur = next((s for s in sessions if s["id"] == self.session_id), sessions[0])
        self.session_id = cur["id"]
        self.session_menu.set(self._session_label(cur))

    def _on_session_pick(self, label):
        sid = getattr(self, "_session_labels", {}).get(label)
        if sid and sid != self.session_id:
            self._load_session(sid)

    def _load_session(self, session_id):
        """切换/载入会话：从存档恢复历史与界面。"""
        self.session_id = session_id
        self.chat_history = []
        for w in self.chat_area.winfo_children():
            w.destroy()
        self.chat_messages = []
        for m in self.store.get_messages(session_id):
            self.chat_history.append({"role": m["role"], "content": m["content"]})
            self._append_message(m["role"], m["display"])
        self._refresh_session_menu()
        self._status(f"已载入会话（{len(self.chat_messages)} 条消息）")

    def _new_session(self):
        self.session_id = self.store.create_session()
        self._load_session(self.session_id)
        self._status("已新建会话")

    def _rename_session(self):
        dialog = ctk.CTkInputDialog(title="重命名会话", text="输入新的会话名：")
        name = (dialog.get_input() or "").strip()
        if not name:
            return
        self.store.rename_session(self.session_id, name)
        self._refresh_session_menu()
        self._status(f"已重命名为：{name}")

    def _delete_session(self):
        self.store.delete_session(self.session_id)
        sessions = self.store.list_sessions()
        self.session_id = sessions[0]["id"] if sessions else self.store.create_session()
        self._load_session(self.session_id)
        self._status("已删除会话")

    def _open_overview(self):
        OverviewDialog(self)

    def _update_overview_status(self):
        if not hasattr(self, "overview_status"):
            return
        ov = self.kb.get_overview()
        if not ov:
            self.overview_status.configure(text="尚未生成设定总览", text_color=DIM_TEXT)
        elif self.kb.overview_stale():
            self.overview_status.configure(text="⚠ 总览已过期，建议刷新", text_color=ERROR_TEXT)
        else:
            self.overview_status.configure(
                text=f"总览就绪（{len(ov['content'])} 字）", text_color=OK_TEXT)

    def _open_memory(self):
        MemoryDialog(self)

    def _extract_memory(self, raw_user, reply_text):
        """后台把这一轮对话里值得长记的信息提炼成记忆条目。"""
        cfg = self.ai_cfg
        sid = self.session_id
        existing = [m["content"] for m in self.store.list_memories(limit=20)]

        def work():
            return memory.extract_notes(cfg, raw_user, reply_text, existing)

        def ok(notes):
            added = sum(1 for n in notes if self.store.add_memory(n, sid))
            if added:
                self._status(f"已记住 {added} 条新信息（可在「AI 记忆 · 管理」中查看）")

        def err(msg):
            ai_client._log_error("记忆提取失败", msg)   # 静默失败，不打断写作

        self._run_async(work, ok, err)

    def _update_recap_status(self):
        if not hasattr(self, "recap_status"):
            return
        recap = self.kb.get_recap()
        if recap and recap["content"].strip():
            self.recap_status.configure(
                text=f"已启用前情提要（{len(recap['content'])} 字）", text_color=OK_TEXT)
        else:
            self.recap_status.configure(text="未设置前情提要", text_color=DIM_TEXT)

    def _chat_clear(self):
        self.store.clear_messages(self.session_id)
        self.chat_history = []
        for w in self.chat_area.winfo_children():
            w.destroy()
        self.chat_messages = []
        self._status("已清空当前会话")

    def _chat_to_ingest(self):
        picked = [m for m in self.chat_messages if m.checked.get()]
        if not picked:
            self._status("请先勾选要入库的对话", error=True)
            return
        transcript = "\n\n".join(
            f"{'你' if m.role == 'user' else '助手'}：{m.text}" for m in picked)
        self.ingest_input.delete("1.0", "end")
        self.ingest_input.insert("1.0", transcript)
        self.ingest_src_label.configure(text="已载入：对话记录")
        self.nav.set(PANEL_INGEST)
        self._show_panel(PANEL_INGEST)
        self._status("已把勾选对话转到录入，点「AI 提取词条」")
