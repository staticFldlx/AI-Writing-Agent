"""蜜桃汽水配色与 CustomTkinter 控件工厂——骰子与写作助手共享。

仅浅色模式：白/淡桃背景、珊瑚红单主色，次要控件用同族浅色变体。
"""

import customtkinter as ctk

WINDOW_BG = "#FFF9F6"
PANEL_BG = "#FFEFE8"
CARD_BG = "#FFFFFF"
PRIMARY = "#FF6B6B"
PRIMARY_HOVER = "#FA5252"
SOFT_BG = "#FFE3E3"
SOFT_HOVER = "#FFC9C9"
SOFT_TEXT = "#C92A2A"
SEG_TEXT = "#7C2222"
BODY_TEXT = "#4A2E28"
DIM_TEXT = "#9A6B5E"
BORDER = "#EDDCD3"
TRACK_OFF = "#E8D5CC"
ERROR_TEXT = "#C0392B"
OK_TEXT = "#1E8449"


def soft_button(parent, **kwargs):
    return ctk.CTkButton(
        parent, fg_color=SOFT_BG, hover_color=SOFT_HOVER, text_color=SOFT_TEXT,
        **kwargs,
    )


def themed_entry(parent, **kwargs):
    return ctk.CTkEntry(
        parent, fg_color=CARD_BG, border_color=BORDER, text_color=BODY_TEXT,
        placeholder_text_color=DIM_TEXT, **kwargs,
    )


def themed_textbox(parent, **kwargs):
    return ctk.CTkTextbox(
        parent, fg_color=CARD_BG, text_color=BODY_TEXT,
        border_width=1, border_color=BORDER, **kwargs,
    )


def themed_optionmenu(parent, **kwargs):
    return ctk.CTkOptionMenu(
        parent, fg_color=SOFT_BG, button_color="#FFB8B8", button_hover_color="#FFA3A3",
        text_color=SOFT_TEXT, dropdown_fg_color=CARD_BG,
        dropdown_text_color=BODY_TEXT, dropdown_hover_color=SOFT_BG, **kwargs,
    )
