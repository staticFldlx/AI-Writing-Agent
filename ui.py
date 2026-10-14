"""DiceBot 主界面（CustomTkinter）。

配色：「蜜桃汽水」——强制浅色模式、白/淡桃背景、珊瑚红单主色，
次要按钮统一用同族浅色变体（浅桃底深红字）。

布局：左列为控制区（掷骰设置 / 描述 / 选项 / 日志 / AI），右列为结果展示、
AI 回复面板与会话历史。选项逐条添加：起点自动衔接上一条的终点，终点用
下拉选择，从结构上杜绝区间重叠与缺口。

AI：掷骰完成后在后台线程调用 OpenAI 兼容接口生成个性化回复，绝不阻塞
掷骰与日志。人格是可增删改的数据（AI 设置弹窗管理）；上下文（文件 +
补充说明）集中在独立弹窗，浓缩稿可查看/编辑/导出，且跨启动持久化。
"""

import os
import queue
import random
import threading
import tkinter.filedialog as filedialog
import traceback
from datetime import datetime

import customtkinter as ctk

import ai_client
import dice
from dice import Option, OptionParseError
from log_manager import LogError, LogManager, format_record
from theme import (
    BODY_TEXT, BORDER, CARD_BG, DIM_TEXT, ERROR_TEXT, OK_TEXT, PANEL_BG,
    PRIMARY, PRIMARY_HOVER, SEG_TEXT, SOFT_BG, SOFT_HOVER, SOFT_TEXT,
    TRACK_OFF, WINDOW_BG, soft_button, themed_entry, themed_optionmenu,
    themed_textbox,
)

MODE_RANGE = "仅范围判定"
MODE_OPTIONS = "随机选项"


class OptionRow(ctk.CTkFrame):
    """一条选项：起点（自动衔接，不可编辑）+ 终点下拉 + 内容输入 + 删除按钮。"""

    def __init__(self, master, app, start: int, end: int, faces: int):
        super().__init__(master, fg_color="transparent")
        self.app = app
        self.start = start
        self.end = end
        self.invalid = False

        self.start_label = ctk.CTkLabel(
            self, text=f"{start} –", width=44, anchor="e",
            font=ctk.CTkFont(size=13, weight="bold"), text_color=BODY_TEXT,
        )
        self.start_label.pack(side="left")
        self.end_menu = themed_optionmenu(
            self, width=68, command=self._on_end_change,
            values=[str(v) for v in range(start, faces + 1)],
        )
        self.end_menu.set(str(end))
        self.end_menu.pack(side="left", padx=(4, 6))
        self.text_entry = themed_entry(self, placeholder_text="选项内容")
        self.text_entry.bind("<KeyRelease>", lambda e: app._refresh_option_status())
        self.text_entry.pack(side="left", fill="x", expand=True)
        ctk.CTkButton(
            self, text="✕", width=30, fg_color="transparent",
            text_color=ERROR_TEXT, hover_color=SOFT_BG,
            command=lambda: app._remove_option_row(self),
        ).pack(side="left", padx=(6, 0))

    def _on_end_change(self, value):
        self.end = int(value)
        self.app._recompute_rows()
        self.app._refresh_option_status()

    def refresh(self, start: int, faces: int):
        """级联更新：重设起点，同步终点下拉的可选值并夹紧当前终点。"""
        self.start = start
        if start > faces:
            self.invalid = True
            self.start_label.configure(text="超出!", text_color=ERROR_TEXT)
            self.end_menu.configure(state="disabled")
            self.text_entry.configure(state="disabled")
            return
        self.invalid = False
        self.start_label.configure(text=f"{start} –", text_color=BODY_TEXT)
        if not start <= self.end <= faces:
            self.end = faces
        self.end_menu.configure(
            state="normal", values=[str(v) for v in range(start, faces + 1)]
        )
        self.end_menu.set(str(self.end))
        self.text_entry.configure(state="normal")

    def set_enabled(self, enabled: bool):
        state = "normal" if enabled and not self.invalid else "disabled"
        self.end_menu.configure(state=state)
        self.text_entry.configure(state=state)


class AISettingsDialog(ctk.CTkToplevel):
    """AI 设置：接口参数、回复字数上限、人格管理（新建/重命名/编辑/删除/恢复预设）。"""

    def __init__(self, app):
        super().__init__(app, fg_color=WINDOW_BG)
        self.app = app
        self.title("AI 设置")
        self.geometry("470x700")
        self.resizable(False, False)
        self.transient(app)
        cfg = app.ai_cfg
        # 工作副本：点「保存」才写回配置
        self.personas = [dict(p) for p in cfg["personas"]]
        self.current = cfg["active_persona"]

        def label(text, pady=(10, 2)):
            ctk.CTkLabel(
                self, text=text, anchor="w",
                font=ctk.CTkFont(size=13, weight="bold"), text_color=BODY_TEXT,
            ).pack(fill="x", padx=16, pady=pady)

        label("API 地址（OpenAI 兼容，如 DeepSeek/Kimi/通义）", pady=(14, 2))
        self.url_entry = themed_entry(self)
        self.url_entry.insert(0, cfg.get("base_url", ""))
        self.url_entry.pack(fill="x", padx=16)

        label("API 密钥（只保存在本机配置文件中）")
        self.key_entry = themed_entry(self, show="*")
        self.key_entry.insert(0, cfg.get("api_key", ""))
        self.key_entry.pack(fill="x", padx=16)

        mrow = ctk.CTkFrame(self, fg_color="transparent")
        mrow.pack(fill="x", padx=16, pady=(10, 0))
        mleft = ctk.CTkFrame(mrow, fg_color="transparent")
        mleft.pack(side="left", fill="x", expand=True, padx=(0, 8))
        ctk.CTkLabel(mleft, text="模型名", anchor="w",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=BODY_TEXT).pack(fill="x", pady=(0, 2))
        self.model_entry = themed_entry(mleft)
        self.model_entry.insert(0, cfg.get("model", ""))
        self.model_entry.pack(fill="x")
        mright = ctk.CTkFrame(mrow, fg_color="transparent")
        mright.pack(side="left")
        ctk.CTkLabel(mright, text="回复字数上限", anchor="w",
                     font=ctk.CTkFont(size=13, weight="bold"),
                     text_color=BODY_TEXT).pack(fill="x", pady=(0, 2))
        self.limit_entry = themed_entry(mright, width=90, justify="center")
        self.limit_entry.insert(0, str(ai_client.reply_limit(cfg)))
        self.limit_entry.pack()

        label("人格（AI 会按此上限控制篇幅，避免回复被截断）", pady=(12, 2))
        self.persona_menu = themed_optionmenu(
            self, values=[p["name"] for p in self.personas],
            command=self._on_persona_selected,
        )
        self.persona_menu.set(self.current)
        self.persona_menu.pack(fill="x", padx=16)

        btn_row = ctk.CTkFrame(self, fg_color="transparent")
        btn_row.pack(fill="x", padx=16, pady=(6, 0))
        for text, cmd, padx in (
            ("新建", self._new_persona, (0, 4)),
            ("重命名", self._rename_persona, 4),
            ("删除", self._delete_persona, 4),
            ("恢复预设", self._restore_presets, (4, 0)),
        ):
            soft_button(btn_row, text=text, command=cmd).pack(
                side="left", expand=True, fill="x", padx=padx)

        label("人设提示词（写给 AI 的系统提示词，可自由修改）")
        self.persona_box = themed_textbox(self, height=140)
        self.persona_box.pack(fill="x", padx=16)
        self._load_persona(self.current)

        self.dlg_status = ctk.CTkLabel(
            self, text="", anchor="w", wraplength=430,
            font=ctk.CTkFont(size=12), text_color=DIM_TEXT,
        )
        self.dlg_status.pack(fill="x", padx=16, pady=(6, 0))

        ctk.CTkButton(
            self, text="保存", height=36, fg_color=PRIMARY,
            hover_color=PRIMARY_HOVER, font=ctk.CTkFont(size=14, weight="bold"),
            command=self._save,
        ).pack(fill="x", padx=16, pady=(8, 16))

        self.after(150, self.grab_set)

    # ---------------------------------------------------------- 人格管理

    def _set_status(self, msg, error=False):
        self.dlg_status.configure(text=msg, text_color=ERROR_TEXT if error else OK_TEXT)

    def _find(self, name):
        for p in self.personas:
            if p["name"] == name:
                return p
        return None

    def _load_persona(self, name):
        self.current = name
        self.persona_menu.set(name)
        p = self._find(name)
        self.persona_box.delete("1.0", "end")
        self.persona_box.insert("1.0", p["prompt"] if p else "")

    def _store_current(self):
        p = self._find(self.current)
        if p is not None:
            p["prompt"] = self.persona_box.get("1.0", "end-1c").strip()

    def _on_persona_selected(self, name):
        self._store_current()
        self._load_persona(name)

    def _refresh_menu(self):
        self.persona_menu.configure(values=[p["name"] for p in self.personas])
        self.persona_menu.set(self.current)

    def _ask_name(self, prompt_text):
        dialog = ctk.CTkInputDialog(title="人格名称", text=prompt_text)
        name = (dialog.get_input() or "").strip()
        if not name:
            return None
        if self._find(name):
            self._set_status(f"已存在名为「{name}」的人格", error=True)
            return None
        return name

    def _new_persona(self):
        name = self._ask_name("新人格的名字：")
        if not name:
            return
        self._store_current()
        self.personas.append({"name": name, "prompt": ""})
        self.current = name
        self._refresh_menu()
        self._load_persona(name)
        self._set_status(f"已新建「{name}」，在下方编辑它的人设提示词")

    def _rename_persona(self):
        name = self._ask_name(f"把「{self.current}」重命名为：")
        if not name:
            return
        self._find(self.current)["name"] = name
        self.current = name
        self._refresh_menu()
        self._set_status(f"已重命名为「{name}」")

    def _delete_persona(self):
        if len(self.personas) <= 1:
            self._set_status("至少保留一条人格", error=True)
            return
        deleted = self.current
        self.personas = [p for p in self.personas if p["name"] != deleted]
        self.current = self.personas[0]["name"]
        self._refresh_menu()
        self._load_persona(self.current)
        self._set_status(f"已删除「{deleted}」")

    def _restore_presets(self):
        """还原/补齐四个内置人格，不影响用户自建的。"""
        self._store_current()
        for d in ai_client.DEFAULT_PERSONAS:
            p = self._find(d["name"])
            if p:
                p["prompt"] = d["prompt"]
            else:
                self.personas.append(dict(d))
        self._refresh_menu()
        self._load_persona(self.current)
        self._set_status("已恢复四个预设人格（自建人格未受影响）")

    def _save(self):
        self._store_current()
        cfg = self.app.ai_cfg
        try:
            limit = int(self.limit_entry.get().strip())
        except ValueError:
            self._set_status("回复字数上限必须是整数", error=True)
            return
        if not 20 <= limit <= 2000:
            self._set_status("回复字数上限须在 20–2000 之间", error=True)
            return
        cfg["base_url"] = ai_client.normalize_base_url(self.url_entry.get())
        cfg["api_key"] = self.key_entry.get().strip()
        cfg["model"] = self.model_entry.get().strip()
        cfg["reply_limit"] = limit
        cfg["personas"] = [dict(p) for p in self.personas]
        cfg["active_persona"] = self.current
        ai_client.save_config(cfg)
        self.app._show_status("AI 设置已保存")
        self.destroy()


class ContextDialog(ctk.CTkToplevel):
    """上下文管理：上下文文件、补充说明、浓缩稿查看/编辑/导出。关闭时自动保存。"""

    def __init__(self, app):
        super().__init__(app, fg_color=WINDOW_BG)
        self.app = app
        self.title("上下文与补充说明")
        self.geometry("560x680")
        self.transient(app)
        self.protocol("WM_DELETE_WINDOW", self._save_and_close)

        def label(text, pady=(12, 2)):
            ctk.CTkLabel(
                self, text=text, anchor="w",
                font=ctk.CTkFont(size=13, weight="bold"), text_color=BODY_TEXT,
            ).pack(fill="x", padx=16, pady=pady)

        label("📄 上下文文件（txt / md / docx，如剧情、设定文档）", pady=(14, 2))
        file_row = ctk.CTkFrame(self, fg_color="transparent")
        file_row.pack(fill="x", padx=16)
        soft_button(file_row, text="选择文件", width=84, command=self._pick_file).pack(side="left")
        soft_button(file_row, text="清除", width=56, command=self._clear_file).pack(side="left", padx=(6, 8))
        self.file_label = ctk.CTkLabel(
            file_row, text="未选择文件", anchor="w",
            font=ctk.CTkFont(size=12), text_color=DIM_TEXT,
        )
        self.file_label.pack(side="left", fill="x", expand=True)

        label("✍️ 补充说明（无文件时作为上下文；有文件时作为补充）")
        self.note_box = themed_textbox(self, height=110)
        self.note_box.insert("1.0", self.app.ai_cfg.get("context_note", ""))
        self.note_box.pack(fill="x", padx=16)

        label("🧪 给 AI 的背景设定（长文件的浓缩稿，可编辑）")
        self.digest_status = ctk.CTkLabel(
            self, text="", anchor="w", justify="left", wraplength=510,
            font=ctk.CTkFont(size=12), text_color=DIM_TEXT,
        )
        self.digest_status.pack(fill="x", padx=16)
        self.digest_box = themed_textbox(self, height=210)
        self.digest_box.pack(fill="both", expand=True, padx=16, pady=(4, 0))

        dbtn_row = ctk.CTkFrame(self, fg_color="transparent")
        dbtn_row.pack(fill="x", padx=16, pady=(6, 0))
        self.condense_btn = soft_button(dbtn_row, text="立即浓缩", command=self._condense_now)
        self.condense_btn.pack(side="left", expand=True, fill="x", padx=(0, 4))
        self.save_digest_btn = soft_button(dbtn_row, text="保存修改", command=self._save_digest)
        self.save_digest_btn.pack(side="left", expand=True, fill="x", padx=4)
        self.export_btn = soft_button(dbtn_row, text="导出…", command=self._export_digest)
        self.export_btn.pack(side="left", expand=True, fill="x", padx=(4, 0))

        ctk.CTkButton(
            self, text="保存并关闭", height=36, fg_color=PRIMARY,
            hover_color=PRIMARY_HOVER, font=ctk.CTkFont(size=14, weight="bold"),
            command=self._save_and_close,
        ).pack(fill="x", padx=16, pady=(10, 16))

        self._refresh_file_area()
        self.after(150, self.grab_set)

    # ------------------------------------------------------------ 文件区

    def _set_digest_text(self, text, editable):
        self.digest_box.configure(state="normal")
        self.digest_box.delete("1.0", "end")
        self.digest_box.insert("1.0", text)
        if not editable:
            self.digest_box.configure(state="disabled")

    def _set_digest_buttons(self, condense, save, export):
        self.condense_btn.configure(state="normal" if condense else "disabled")
        self.save_digest_btn.configure(state="normal" if save else "disabled")
        self.export_btn.configure(state="normal" if export else "disabled")

    def _digest_hint(self, msg, error=False):
        self.digest_status.configure(text=msg, text_color=ERROR_TEXT if error else DIM_TEXT)

    def _refresh_file_area(self):
        path = self.app.context_file
        if not path:
            self.file_label.configure(text="未选择文件", text_color=DIM_TEXT)
            self._set_digest_text("", editable=False)
            self._digest_hint("选择文件后，这里可查看/编辑将提供给 AI 的背景设定")
            self._set_digest_buttons(False, False, False)
            return
        self.file_label.configure(text=os.path.basename(path), text_color=BODY_TEXT)
        try:
            text = ai_client.extract_text(path)
        except ai_client.AIError as e:
            self._set_digest_text("", editable=False)
            self._digest_hint(f"读取失败：{e}", error=True)
            self._set_digest_buttons(False, False, False)
            return
        if len(text) <= ai_client.MAX_DIRECT_CHARS:
            self._set_digest_text(text, editable=False)
            self._digest_hint("短文件：无需浓缩，将直接使用以下原文（只读）")
            self._set_digest_buttons(condense=False, save=False, export=True)
            return
        digest = ai_client.get_cached_digest(path)
        if digest:
            self._set_digest_text(digest, editable=True)
            self._digest_hint("已有浓缩稿：可直接编辑，保存后掷骰即使用编辑版")
        else:
            self._set_digest_text("", editable=True)
            self._digest_hint("长文件：尚未浓缩。点「立即浓缩」生成，也可自己手写背景后保存")
        self._set_digest_buttons(condense=True, save=True, export=True)
        self.condense_btn.configure(text="重新浓缩" if digest else "立即浓缩")

    def _pick_file(self):
        path = filedialog.askopenfilename(
            parent=self, title="选择上下文文件",
            filetypes=[("文本 / Word", "*.txt *.md *.docx"), ("所有文件", "*.*")],
        )
        if not path:
            return
        self.app.context_file = path
        self._refresh_file_area()

    def _clear_file(self):
        self.app.context_file = None
        self._refresh_file_area()

    # ------------------------------------------------------------ 浓缩稿

    def _condense_now(self):
        path = self.app.context_file
        if not path:
            return
        cfg = self.app.ai_cfg
        if not cfg.get("api_key"):
            self._digest_hint("请先在「AI 设置」中配置 API 密钥", error=True)
            return
        self.condense_btn.configure(state="disabled")
        self._digest_hint("正在请 AI 浓缩…")
        post = self.app._ui_queue.put

        def hint(msg, error=False):
            if self.winfo_exists():
                self._digest_hint(msg, error)

        def work():
            try:
                digest = ai_client.condense_file(
                    cfg, path, status_cb=lambda m: post(lambda m=m: hint(m)),
                )
            except ai_client.AIError as e:
                msg = f"浓缩失败：{e}"

                def fail():
                    if self.winfo_exists():
                        self._digest_hint(msg, error=True)
                        self.condense_btn.configure(state="normal")
                post(fail)
                return

            def done():
                if not self.winfo_exists():
                    return
                self._set_digest_text(digest, editable=True)
                self._digest_hint("浓缩完成，可编辑后保存")
                self.condense_btn.configure(state="normal", text="重新浓缩")
            post(done)

        threading.Thread(target=work, daemon=True).start()

    def _save_digest(self):
        path = self.app.context_file
        if not path:
            return
        text = self.digest_box.get("1.0", "end-1c").strip()
        if not text:
            self._digest_hint("浓缩稿为空，未保存", error=True)
            return
        try:
            ai_client.set_cached_digest(path, text)
        except ai_client.AIError as e:
            self._digest_hint(f"保存失败：{e}", error=True)
            return
        self._digest_hint("已保存，之后掷骰将使用此版本")
        self.condense_btn.configure(text="重新浓缩")

    def _export_digest(self):
        content = self.digest_box.get("1.0", "end-1c").strip()
        if not content:
            self._digest_hint("没有可导出的内容", error=True)
            return
        target = filedialog.asksaveasfilename(
            parent=self, title="导出背景设定", defaultextension=".txt",
            filetypes=[("文本文件", "*.txt")],
        )
        if not target:
            return
        try:
            with open(target, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            self._digest_hint(f"导出失败：{e}", error=True)
            return
        self._digest_hint(f"已导出：{os.path.basename(target)}")

    def _save_and_close(self):
        cfg = self.app.ai_cfg
        cfg["context_file"] = self.app.context_file or ""
        cfg["context_note"] = self.note_box.get("1.0", "end-1c").strip()
        ai_client.save_config(cfg)
        self.app._update_ctx_status()
        self.destroy()


class DicePanel(ctk.CTkFrame):
    """骰子界面主体，可独立成窗（DiceBotApp）也可嵌进写作助手当一个面板。

    传入 app 时共用宿主的 ai_cfg 与 _ui_queue，使 AI 设置在两边实时一致。
    """

    def __init__(self, master, app=None):
        super().__init__(master, fg_color="transparent")
        self.app = app

        self.log = LogManager()
        self.ai_cfg = app.ai_cfg if app is not None else ai_client.load_config()
        # 上下文文件跨启动恢复（路径失效则静默清除）
        self.context_file: str | None = None
        saved_ctx = self.ai_cfg.get("context_file") or ""
        if saved_ctx and os.path.isfile(saved_ctx):
            self.context_file = saved_ctx
        elif saved_ctx:
            self.ai_cfg["context_file"] = ""
            ai_client.save_config(self.ai_cfg)
        # 本次会话全部掷骰记录；完整传给 AI 作为上下文
        self.history: list[dict] = []
        self.option_rows: list[OptionRow] = []
        self._last_faces = 6  # 最近一次合法面数，输入框非法时下拉仍可用

        self.grid_columnconfigure(0, weight=0)
        self.grid_columnconfigure(1, weight=1)
        self.grid_rowconfigure(0, weight=1)

        self._build_left()
        self._build_right()
        self._on_mode_change(MODE_RANGE)
        self._update_ctx_status()
        if self.ai_cfg.get("ai_enabled"):
            self.ai_switch.select()

        # 后台线程 → 主线程的通信队列（tkinter 不允许跨线程直接操作控件）
        if app is not None:
            self._ui_queue = app._ui_queue          # 嵌入模式：复用宿主的队列与轮询
        else:
            self._ui_queue: queue.Queue = queue.Queue()
            self.after(100, self._poll_ui_queue)

    def _poll_ui_queue(self):
        try:
            while True:
                cb = self._ui_queue.get_nowait()
                try:
                    cb()
                except Exception:
                    ai_client._log_error("UI 回调异常", traceback.format_exc())
        except queue.Empty:
            pass
        self.after(100, self._poll_ui_queue)

    def _section_label(self, parent, text):
        ctk.CTkLabel(
            parent, text=text, font=ctk.CTkFont(size=14, weight="bold"),
            text_color=BODY_TEXT,
        ).pack(anchor="w", padx=12, pady=(12, 2))

    def _build_left(self):
        left = ctk.CTkScrollableFrame(
            self, width=400, corner_radius=16, fg_color=PANEL_BG,
        )
        left.grid(row=0, column=0, sticky="nsw", padx=(10, 5), pady=10)

        self._section_label(left, "🎲 掷骰设置")
        row = ctk.CTkFrame(left, fg_color="transparent")
        row.pack(fill="x", padx=12)
        ctk.CTkLabel(row, text="骰子范围：1d", text_color=BODY_TEXT).pack(side="left")
        self.faces_entry = themed_entry(row, width=56, justify="center")
        self.faces_entry.insert(0, "6")
        self.faces_entry.bind("<KeyRelease>", self._on_faces_typed)
        self.faces_entry.pack(side="left", padx=(2, 10))
        self.faces_slider = ctk.CTkSlider(
            row, from_=1, to=100, number_of_steps=99, command=self._on_slider,
            button_color=PRIMARY, button_hover_color=PRIMARY_HOVER,
            progress_color=PRIMARY, fg_color=SOFT_BG,
        )
        self.faces_slider.set(6)
        self.faces_slider.pack(side="left", fill="x", expand=True)

        self.mode_seg = ctk.CTkSegmentedButton(
            left, values=[MODE_RANGE, MODE_OPTIONS], command=self._on_mode_change,
            fg_color=SOFT_BG, selected_color=PRIMARY, selected_hover_color=PRIMARY_HOVER,
            unselected_color=SOFT_BG, unselected_hover_color=SOFT_HOVER,
            text_color=SEG_TEXT,
        )
        self.mode_seg.set(MODE_RANGE)
        self.mode_seg.pack(fill="x", padx=12, pady=(10, 0))

        self._section_label(left, "📝 本次掷骰决定的内容")
        self.desc_entry = themed_entry(left, placeholder_text="例如：今晚吃什么")
        self.desc_entry.pack(fill="x", padx=12)

        opt_header = ctk.CTkFrame(left, fg_color="transparent")
        opt_header.pack(fill="x", padx=12, pady=(12, 2))
        ctk.CTkLabel(
            opt_header, text="🎯 选项", font=ctk.CTkFont(size=14, weight="bold"),
            text_color=BODY_TEXT,
        ).pack(side="left")
        self.add_opt_btn = soft_button(
            opt_header, text="➕ 添加选项", width=104, corner_radius=14,
            font=ctk.CTkFont(size=13, weight="bold"), command=self._add_option_row,
        )
        self.add_opt_btn.pack(side="right")

        self.options_frame = ctk.CTkScrollableFrame(
            left, height=132, fg_color=CARD_BG, corner_radius=12,
        )
        self.options_frame.pack(fill="x", padx=12)
        self.opt_status = ctk.CTkLabel(
            left, text="", anchor="w", justify="left",
            wraplength=370, font=ctk.CTkFont(size=12),
        )
        self.opt_status.pack(fill="x", padx=12, pady=(2, 0))

        self.roll_btn = ctk.CTkButton(
            left, text="🎲 掷骰！", height=48, corner_radius=24,
            fg_color=PRIMARY, hover_color=PRIMARY_HOVER,
            font=ctk.CTkFont(size=19, weight="bold"), command=self._roll,
        )
        self.roll_btn.pack(fill="x", padx=12, pady=(14, 2))

        self._section_label(left, "📜 日志")
        self.log_switch = ctk.CTkSwitch(
            left, text="生成日志", progress_color=PRIMARY, fg_color=TRACK_OFF,
            text_color=BODY_TEXT, command=self._on_log_toggle,
        )
        self.log_switch.pack(anchor="w", padx=12)

        dir_row = ctk.CTkFrame(left, fg_color="transparent")
        dir_row.pack(fill="x", padx=12, pady=(6, 0))
        soft_button(dir_row, text="选择目录", width=84, command=self._choose_directory).pack(side="left")
        self.dir_label = ctk.CTkLabel(
            dir_row, text="未选择目录", anchor="w",
            font=ctk.CTkFont(size=12), text_color=DIM_TEXT,
        )
        self.dir_label.pack(side="left", fill="x", expand=True, padx=(8, 0))

        self.file_label = ctk.CTkLabel(
            left, text="当前文件：—（首次写入时自动新建）", anchor="w",
            wraplength=370, font=ctk.CTkFont(size=12), text_color=DIM_TEXT,
        )
        self.file_label.pack(fill="x", padx=12, pady=(4, 0))

        btn_row = ctk.CTkFrame(left, fg_color="transparent")
        btn_row.pack(fill="x", padx=12, pady=(6, 0))
        for text, cmd, padx in (
            ("新建日志文件", self._new_log_file, (0, 4)),
            ("指定已有文件", self._pick_log_file, 4),
            ("重命名当前", self._rename_log_file, (4, 0)),
        ):
            soft_button(btn_row, text=text, command=cmd).pack(
                side="left", expand=True, fill="x", padx=padx)

        self._section_label(left, "🤖 AI 互动")
        ai_row = ctk.CTkFrame(left, fg_color="transparent")
        ai_row.pack(fill="x", padx=12)
        self.ai_switch = ctk.CTkSwitch(
            ai_row, text="启用 AI 回复", progress_color=PRIMARY, fg_color=TRACK_OFF,
            text_color=BODY_TEXT, command=self._on_ai_toggle,
        )
        self.ai_switch.pack(side="left")
        soft_button(ai_row, text="AI 设置…", width=88, command=self._open_ai_settings).pack(side="right")

        ctx_row = ctk.CTkFrame(left, fg_color="transparent")
        ctx_row.pack(fill="x", padx=12, pady=(6, 0))
        soft_button(ctx_row, text="📄 上下文…", width=100, command=self._open_context).pack(side="left")
        self.ctx_status = ctk.CTkLabel(
            ctx_row, text="未设置", anchor="w",
            font=ctk.CTkFont(size=12), text_color=DIM_TEXT,
        )
        self.ctx_status.pack(side="left", fill="x", expand=True, padx=(8, 0))

        self.status_label = ctk.CTkLabel(
            left, text="", anchor="w", justify="left",
            wraplength=370, font=ctk.CTkFont(size=12),
        )
        self.status_label.pack(fill="x", padx=12, pady=(10, 12))

    def _build_right(self):
        right = ctk.CTkFrame(self, fg_color="transparent")
        right.grid(row=0, column=1, sticky="nsew", padx=(5, 10), pady=10)
        right.grid_columnconfigure(0, weight=1)
        right.grid_rowconfigure(1, weight=2)
        right.grid_rowconfigure(4, weight=3)

        result_frame = ctk.CTkFrame(
            right, corner_radius=16, fg_color=CARD_BG,
            border_width=1, border_color=BORDER,
        )
        result_frame.grid(row=0, column=0, sticky="new")
        self.result_desc = ctk.CTkLabel(
            result_frame, text="还没掷过骰子，点左边的大按钮试试！",
            wraplength=480, font=ctk.CTkFont(size=14), text_color=DIM_TEXT,
        )
        self.result_desc.pack(pady=(14, 0))
        self.result_num = ctk.CTkLabel(
            result_frame, text="🎲",
            font=ctk.CTkFont(size=56, weight="bold"), text_color=PRIMARY,
        )
        self.result_num.pack(pady=(0, 12))

        self.options_result = ctk.CTkScrollableFrame(
            right, label_text="🎯 选项", corner_radius=16, fg_color=CARD_BG,
            label_fg_color=PANEL_BG, label_text_color=BODY_TEXT,
        )
        self.options_result.grid(row=1, column=0, sticky="nsew", pady=(8, 0))

        self.ai_frame = ctk.CTkFrame(
            right, corner_radius=16, fg_color=CARD_BG,
            border_width=1, border_color=BORDER,
        )
        self.ai_frame.grid(row=2, column=0, sticky="ew", pady=(8, 0))
        self.ai_title = ctk.CTkLabel(
            self.ai_frame, text="💬 AI", anchor="w",
            font=ctk.CTkFont(size=12, weight="bold"), text_color=DIM_TEXT,
        )
        self.ai_title.pack(fill="x", padx=14, pady=(8, 0))
        self.ai_reply_label = ctk.CTkLabel(
            self.ai_frame, text="", anchor="w", justify="left",
            wraplength=520, font=ctk.CTkFont(size=13), text_color=BODY_TEXT,
        )
        self.ai_reply_label.pack(fill="x", padx=14, pady=(2, 10))
        self.ai_frame.grid_remove()

        ctk.CTkLabel(
            right, text="🕘 本次会话记录",
            font=ctk.CTkFont(size=14, weight="bold"), anchor="w",
            text_color=BODY_TEXT,
        ).grid(row=3, column=0, sticky="w", pady=(10, 2))
        self.history_box = themed_textbox(right, state="disabled", corner_radius=16)
        self.history_box.grid(row=4, column=0, sticky="nsew")

    # ------------------------------------------------------------ 掷骰设置事件

    def _current_faces(self) -> int:
        """返回最近一次合法的面数（输入框暂时非法时沿用上一个值）。"""
        try:
            self._last_faces = dice.validate_faces(self.faces_entry.get())
        except ValueError:
            pass
        return self._last_faces

    def _on_faces_typed(self, _event=None):
        try:
            faces = dice.validate_faces(self.faces_entry.get())
        except ValueError:
            self._refresh_option_status()
            return
        self.faces_slider.set(faces)
        self._recompute_rows()
        self._refresh_option_status()

    def _on_slider(self, value):
        faces = int(round(value))
        self.faces_entry.delete(0, "end")
        self.faces_entry.insert(0, str(faces))
        self._recompute_rows()
        self._refresh_option_status()

    def _on_mode_change(self, mode):
        enabled = mode == MODE_OPTIONS
        self.add_opt_btn.configure(state="normal" if enabled else "disabled")
        for row in self.option_rows:
            row.set_enabled(enabled)
        if enabled:
            self._recompute_rows()
            self._refresh_option_status()
        else:
            self.opt_status.configure(text="仅范围判定模式：无需选项", text_color=DIM_TEXT)

    # ------------------------------------------------------------ 选项行管理

    def _add_option_row(self):
        faces = self._current_faces()
        start = self.option_rows[-1].end + 1 if self.option_rows else 1
        if start > faces:
            self._show_status(
                f"骰面 1-{faces} 已全部分配，可先调小前面选项的终点", error=True
            )
            return
        row = OptionRow(self.options_frame, self, start, faces, faces)
        row.pack(fill="x", pady=2)
        self.option_rows.append(row)
        self._refresh_option_status()

    def _remove_option_row(self, row):
        self.option_rows.remove(row)
        row.destroy()
        self._recompute_rows()
        self._refresh_option_status()

    def _recompute_rows(self):
        """级联刷新所有选项行：起点自动衔接，终点下拉随面数联动。"""
        faces = self._current_faces()
        start = 1
        for row in self.option_rows:
            row.refresh(start, faces)
            start = (row.end + 1) if not row.invalid else faces + 1

    def _rows_to_options(self, faces: int) -> list[Option]:
        """把选项行转为 Option 列表，校验失败抛 OptionParseError。"""
        if not self.option_rows:
            raise OptionParseError("请先点「➕ 添加选项」添加至少一条选项")
        options = []
        for i, row in enumerate(self.option_rows, 1):
            if row.invalid:
                raise OptionParseError(f"第 {i} 条选项超出骰子范围，请删除或调整前面的区间")
            text = row.text_entry.get().strip()
            if not text:
                raise OptionParseError(f"第 {i} 条选项内容为空")
            options.append(Option(row.start, row.end, text))
        if options[-1].end != faces:
            raise OptionParseError(
                f"选项未覆盖全部骰面：还差 {options[-1].end + 1}-{faces}，"
                f"可调大最后一条的终点或继续添加"
            )
        dice.check_options(options, faces)
        return options

    def _refresh_option_status(self):
        """选项区的实时校验提示。"""
        if self.mode_seg.get() != MODE_OPTIONS:
            return
        if not self.option_rows:
            self.opt_status.configure(
                text="点「➕ 添加选项」逐条添加，区间起点会自动衔接", text_color=DIM_TEXT
            )
            return
        try:
            faces = dice.validate_faces(self.faces_entry.get())
            options = self._rows_to_options(faces)
        except (ValueError, OptionParseError) as e:
            self.opt_status.configure(text=str(e), text_color=ERROR_TEXT)
            return
        self.opt_status.configure(
            text=f"✓ 共 {len(options)} 条选项，覆盖 1-{faces}", text_color=OK_TEXT
        )

    # ------------------------------------------------------------------ 掷骰

    def _roll(self):
        self._show_status("")
        try:
            faces = dice.validate_faces(self.faces_entry.get())
        except ValueError as e:
            self._show_status(str(e), error=True)
            return
        desc = self.desc_entry.get().strip()
        if not desc:
            self._show_status("请先输入本次掷骰决定的内容", error=True)
            return
        options = None
        if self.mode_seg.get() == MODE_OPTIONS:
            try:
                options = self._rows_to_options(faces)
            except OptionParseError as e:
                self._show_status(str(e), error=True)
                return

        result = dice.roll(faces)
        record = {
            "timestamp": datetime.now(),
            "description": desc,
            "faces": faces,
            "options": options,
            "result": result,
            "hit_index": dice.match_option(options, result) if options else None,
        }
        self.roll_btn.configure(state="disabled")
        self._animate_roll(record, frames=12)

    def _animate_roll(self, record, frames):
        """掷骰小动画：数字滚动十几帧后落定。"""
        if frames > 0:
            self.result_num.configure(text=str(random.randint(1, record["faces"])))
            self.after(40, lambda: self._animate_roll(record, frames - 1))
            return
        self._render_result(record)
        self.on_roll_complete(record, self.history)
        self.roll_btn.configure(state="normal")

    def _render_result(self, record):
        self.result_desc.configure(
            text=f"{record['description']}（1d{record['faces']}）"
        )
        self.result_num.configure(text=str(record["result"]))
        for w in self.options_result.winfo_children():
            w.destroy()
        options = record["options"]
        if not options:
            return
        for i, opt in enumerate(options):
            hit = i == record["hit_index"]
            ctk.CTkLabel(
                self.options_result,
                text=("🎉 " if hit else "      ") + opt.display(),
                anchor="w",
                font=ctk.CTkFont(size=16 if hit else 13,
                                 weight="bold" if hit else "normal"),
                text_color=PRIMARY if hit else BODY_TEXT,
            ).pack(fill="x", padx=8, pady=1)

    def on_roll_complete(self, record, history):
        """掷骰完成钩子：更新历史、写日志、触发 AI 回复。"""
        history.append(record)
        self._append_history_text(format_record(record))

        if self.log_switch.get():
            try:
                path = self.log.append_record(record)
                self._set_file_label(path)
                self._show_status(f"已写入日志：{os.path.basename(path)}")
            except LogError as e:
                self._show_status(f"日志未写入：{e}", error=True)

        self._render_ai_reply(record, history)

    # -------------------------------------------------------------- AI 回复

    def _render_ai_reply(self, record, history):
        """后台线程请求 AI 回复，完成后经队列回主线程更新面板。"""
        cfg = self.ai_cfg
        if not (cfg.get("ai_enabled") and cfg.get("api_key")):
            self.ai_frame.grid_remove()
            return
        self.ai_title.configure(text=f"💬 {ai_client.persona_name(cfg)}")
        self.ai_reply_label.configure(text="思考中…", text_color=DIM_TEXT)
        self.ai_frame.grid()

        ctx_path = self.context_file
        note = (cfg.get("context_note") or "").strip()
        snapshot = list(history)
        post = self._ui_queue.put

        def set_panel(text):
            self.ai_reply_label.configure(text=text, text_color=DIM_TEXT)

        def work():
            try:
                ctx = ""
                if ctx_path:
                    ctx = ai_client.get_context(
                        cfg, ctx_path,
                        status_cb=lambda m: post(lambda m=m: set_panel(m)),
                    )
                reply = ai_client.generate_reply(cfg, record, snapshot, ctx, note)
            except ai_client.AIError as e:
                msg = f"（AI 回复失败：{e}）"
                post(lambda: self._show_ai_reply(record, msg, True))
                return
            post(lambda: self._show_ai_reply(record, reply, False))

        threading.Thread(target=work, daemon=True).start()

    def _show_ai_reply(self, record, text, error):
        self.ai_reply_label.configure(
            text=text, text_color=ERROR_TEXT if error else BODY_TEXT
        )
        if error:
            return
        record["ai_reply"] = text
        self._append_history_text(f"  💬 {text}")
        if self.log_switch.get():
            try:
                self.log.append_text(f"  AI：{text}")
            except LogError as e:
                self._show_status(f"AI 回复未写入日志：{e}", error=True)

    def _on_ai_toggle(self):
        self.ai_cfg["ai_enabled"] = bool(self.ai_switch.get())
        ai_client.save_config(self.ai_cfg)
        if self.ai_cfg["ai_enabled"] and not self.ai_cfg.get("api_key"):
            self._open_ai_settings()
        if not self.ai_cfg["ai_enabled"]:
            self.ai_frame.grid_remove()

    def _open_ai_settings(self):
        AISettingsDialog(self)

    def _open_context(self):
        ContextDialog(self)

    def _update_ctx_status(self):
        has_file = bool(self.context_file)
        has_note = bool((self.ai_cfg.get("context_note") or "").strip())
        if has_file and has_note:
            text = f"文件：{os.path.basename(self.context_file)} + 补充说明"
        elif has_file:
            text = f"文件：{os.path.basename(self.context_file)}"
        elif has_note:
            text = "已设置补充说明"
        else:
            text = "未设置"
        self.ctx_status.configure(
            text=text, text_color=BODY_TEXT if (has_file or has_note) else DIM_TEXT
        )

    # ------------------------------------------------------------------ 日志

    def _on_log_toggle(self):
        if self.log_switch.get() and not self.log.directory:
            self._choose_directory()
            if not self.log.directory:
                self.log_switch.deselect()

    def _choose_directory(self):
        path = filedialog.askdirectory(title="选择日志目录")
        if not path:
            return
        try:
            self.log.set_directory(path)
        except LogError as e:
            self._show_status(str(e), error=True)
            return
        self.dir_label.configure(text=self._shorten(path))
        self._set_file_label(None)
        self._show_status("日志目录已设置")

    def _new_log_file(self):
        try:
            path = self.log.start_new_file()
        except LogError as e:
            self._show_status(str(e), error=True)
            return
        self._set_file_label(path)
        self._show_status(f"已新建日志文件：{os.path.basename(path)}")

    def _pick_log_file(self):
        path = filedialog.askopenfilename(
            title="指定日志文件",
            filetypes=[("文本文件", "*.txt"), ("所有文件", "*.*")],
        )
        if not path:
            return
        try:
            path = self.log.use_file(path)
        except LogError as e:
            self._show_status(str(e), error=True)
            return
        self.dir_label.configure(text=self._shorten(self.log.directory))
        self._set_file_label(path)
        self._show_status("已指定日志文件，写入时只追加不覆盖")

    def _rename_log_file(self):
        if not self.log.current_file:
            self._show_status("当前没有日志文件可重命名（还未写入过日志）", error=True)
            return
        dialog = ctk.CTkInputDialog(
            title="重命名日志文件",
            text=f"当前：{os.path.basename(self.log.current_file)}\n输入新文件名（自动补 .txt）：",
        )
        new_name = dialog.get_input()
        if not new_name:
            return
        try:
            path = self.log.rename_current(new_name)
        except LogError as e:
            self._show_status(str(e), error=True)
            return
        self._set_file_label(path)
        self._show_status(f"已重命名为：{os.path.basename(path)}")

    # ------------------------------------------------------------------ 辅助

    def _append_history_text(self, text):
        self.history_box.configure(state="normal")
        self.history_box.insert("end", text + "\n\n")
        self.history_box.see("end")
        self.history_box.configure(state="disabled")

    def _set_file_label(self, path):
        if path:
            self.file_label.configure(text=f"当前文件：{os.path.basename(path)}")
        else:
            self.file_label.configure(text="当前文件：—（首次写入时自动新建）")

    @staticmethod
    def _shorten(path, limit=42):
        return path if len(path) <= limit else "…" + path[-(limit - 1):]

    def _show_status(self, msg, error=False):
        self.status_label.configure(
            text=msg, text_color=ERROR_TEXT if error else OK_TEXT
        )


class DiceBotApp(ctk.CTk):
    """骰子独立窗口（main.py 用）。界面主体在 DicePanel，属性访问委托给它，
    因此既有调用方式（含 smoke_ui.py）无需改动。"""

    def __init__(self):
        super().__init__(fg_color=WINDOW_BG)
        ctk.set_appearance_mode("light")
        self.title("🎲 DiceBot 骰子机器人")
        self.geometry("1000x740")
        self.minsize(920, 640)
        self.panel = DicePanel(self)
        self.panel.pack(fill="both", expand=True)

    def __getattr__(self, name):
        if name == "panel":                  # 防止 panel 尚未创建时无限递归
            raise AttributeError(name)
        return getattr(self.panel, name)
