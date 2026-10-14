"""DiceBot 写作助手（含骰子）统一入口：python app.py

四个面板：设定库 / 录入 / 写作讨论 / 骰子，共享同一份 AI 设置与配置目录。
仅需骰子时可单独运行 main.py，仅需写作助手时可运行 writer_app.py。
"""

from writer_ui import WriterApp

if __name__ == "__main__":
    WriterApp().mainloop()
