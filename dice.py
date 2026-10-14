"""掷骰核心逻辑：选项校验、掷骰、结果匹配。

纯逻辑模块，无 GUI 依赖，便于独立测试。
选项由 UI 逐条添加（区间下拉选择），本模块负责结构化校验。
"""

import random
from dataclasses import dataclass

MIN_FACES = 1
MAX_FACES = 100


class OptionParseError(ValueError):
    """选项校验失败，消息可直接展示给用户。"""


@dataclass
class Option:
    start: int
    end: int
    text: str

    def covers(self, value: int) -> bool:
        return self.start <= value <= self.end

    def range_label(self) -> str:
        return str(self.start) if self.start == self.end else f"{self.start}-{self.end}"

    def display(self) -> str:
        return f"{self.range_label()}. {self.text}"


def check_options(options: list[Option], faces: int) -> None:
    """校验选项列表：区间合法、互不重叠、完整覆盖 [1, faces]。

    失败时抛出 OptionParseError。
    """
    if not options:
        raise OptionParseError("选项不能为空")
    covered = [False] * (faces + 1)
    for i, opt in enumerate(options, 1):
        if opt.start > opt.end:
            raise OptionParseError(
                f"第 {i} 条区间起点大于终点：{opt.start}-{opt.end}"
            )
        if opt.start < 1 or opt.end > faces:
            raise OptionParseError(
                f"第 {i} 条区间 {opt.range_label()} 超出骰子范围 1-{faces}"
            )
        for v in range(opt.start, opt.end + 1):
            if covered[v]:
                raise OptionParseError(f"骰面 {v} 被多个选项覆盖（区间重叠）")
            covered[v] = True
    missing = [str(v) for v in range(1, faces + 1) if not covered[v]]
    if missing:
        raise OptionParseError(f"选项未覆盖全部骰面，缺少：{', '.join(missing)}")


def validate_faces(value: str) -> int:
    """把用户输入的面数字符串转为合法整数，非法时抛 ValueError。"""
    try:
        faces = int(value.strip())
    except (ValueError, AttributeError):
        raise ValueError(f"面数必须是整数（当前输入：{value!r}）") from None
    if not MIN_FACES <= faces <= MAX_FACES:
        raise ValueError(f"面数须在 {MIN_FACES}-{MAX_FACES} 之间（当前：{faces}）")
    return faces


def roll(faces: int) -> int:
    if not MIN_FACES <= faces <= MAX_FACES:
        raise ValueError(f"骰子面数须在 {MIN_FACES}-{MAX_FACES} 之间")
    return random.randint(1, faces)


def match_option(options: list[Option], value: int) -> int:
    """返回命中选项的索引；选项经过 check_options 校验后必有且仅有一个命中。"""
    for i, opt in enumerate(options):
        if opt.covers(value):
            return i
    raise ValueError(f"结果 {value} 未命中任何选项")
