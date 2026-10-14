"""日志文件管理：惰性创建、追加写入、切换、重命名。

纯逻辑模块，无 GUI 依赖。所有写入均为追加模式，绝不截断已有内容。
"""

import os
from datetime import datetime


class LogError(Exception):
    """日志操作失败，消息可直接展示给用户。"""


def format_record(record: dict) -> str:
    """把一条掷骰记录格式化为日志文本块（UI 历史区与日志文件共用）。"""
    ts = record["timestamp"].strftime("%Y-%m-%d %H:%M:%S")
    lines = [
        f"[{ts}] {record['description']}",
        f"  范围：1d{record['faces']}",
    ]
    options = record.get("options")
    if options:
        lines.append("  选项：" + " / ".join(opt.display() for opt in options))
    result_line = f"  结果：{record['result']}"
    hit = record.get("hit_index")
    if options and hit is not None:
        result_line += f" → 命中：{options[hit].display()}"
    lines.append(result_line)
    if record.get("ai_reply"):
        lines.append(f"  AI：{record['ai_reply']}")
    return "\n".join(lines)


class LogManager:
    """管理当前日志目录与日志文件。

    默认行为：程序每次运行后的第一次写入，在所选目录创建新的时间戳文件
    （惰性创建——不掷骰不产生空文件）。运行中可随时新建、指定已有文件或
    重命名当前文件，后续写入跟随当前文件。
    """

    def __init__(self):
        self.directory: str | None = None
        self.current_file: str | None = None  # 绝对路径

    def set_directory(self, directory: str) -> None:
        """设置日志目录；切换目录后回到「下次写入新建文件」的默认行为。"""
        if not os.path.isdir(directory):
            raise LogError(f"目录不存在：{directory}")
        self.directory = directory
        self.current_file = None

    def _make_new_path(self) -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(self.directory, f"dicelog_{stamp}.txt")
        n = 1
        while os.path.exists(path):
            path = os.path.join(self.directory, f"dicelog_{stamp}_{n}.txt")
            n += 1
        return path

    def start_new_file(self) -> str:
        """立即在当前目录创建一个新的时间戳日志文件并设为当前文件。"""
        if not self.directory:
            raise LogError("尚未选择日志目录")
        path = self._make_new_path()
        with open(path, "x", encoding="utf-8"):
            pass
        self.current_file = path
        return path

    def use_file(self, path: str) -> str:
        """指定一个已有文件作为当前日志文件（写入时只追加）。"""
        if not os.path.isfile(path):
            raise LogError(f"文件不存在：{path}")
        self.current_file = os.path.abspath(path)
        self.directory = os.path.dirname(self.current_file)
        return self.current_file

    def rename_current(self, new_name: str) -> str:
        """重命名当前日志文件（同目录内），返回新路径。"""
        if not self.current_file:
            raise LogError("当前没有日志文件可重命名（还未写入过日志）")
        new_name = new_name.strip()
        if not new_name:
            raise LogError("新文件名不能为空")
        if os.path.basename(new_name) != new_name:
            raise LogError("文件名不能包含路径分隔符")
        if not new_name.lower().endswith(".txt"):
            new_name += ".txt"
        new_path = os.path.join(os.path.dirname(self.current_file), new_name)
        if os.path.exists(new_path):
            raise LogError(f"目标文件已存在：{new_name}")
        try:
            os.rename(self.current_file, new_path)
        except OSError as e:
            raise LogError(f"重命名失败：{e}") from e
        self.current_file = new_path
        return new_path

    def append_text(self, text: str) -> str:
        """追加一段文本；当前无文件时按默认行为新建，返回写入的文件路径。"""
        if not self.directory:
            raise LogError("尚未选择日志目录")
        if not self.current_file:
            self.current_file = self._make_new_path()
        try:
            with open(self.current_file, "a", encoding="utf-8") as f:
                f.write(text + "\n\n")
        except OSError as e:
            raise LogError(f"写入日志失败：{e}") from e
        return self.current_file

    def append_record(self, record: dict) -> str:
        """追加一条掷骰记录；当前无文件时按默认行为新建，返回写入的文件路径。"""
        return self.append_text(format_record(record))
