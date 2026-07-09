"""OCR 封装：RapidOCR（PP-OCR 中英文模型 / ONNX Runtime）。

对外只暴露 recognize(image) -> List[{"text","box","quad","angle"}]，
与 loader 的文字行结构一致（box 向后兼容），这样下游解析不用关心
文字来自「电子 PDF 文字层」还是「OCR」。

quad  : 检测到的旋转四边形原始 4 点 [[x,y]*4]（通常 tl,tr,br,bl 顺序）。
angle : 该行文字的倾斜角（度），取四边形较长边方向，归一化到 (-45, 45]。
        同一张小票的行角度高度一致——是分割叠压小票的重要信号。
"""
from __future__ import annotations

import math
import re
from typing import List, Dict, Any

import numpy as np

_engine = None

# OCR 常见粘连/形近修复（只做非常安全的替换；数字级修复由 parse 层负责）
_MONTHS = ("jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec")
_TXT_FIXES = [
    # 日期里数字与月份名粘连：15March2026 -> 15 March 2026
    (re.compile(rf"(\d)((?:{_MONTHS})[a-z]*)", re.IGNORECASE), r"\1 \2"),
    (re.compile(rf"((?:{_MONTHS})[a-z]*)(\d)", re.IGNORECASE), r"\1 \2"),
    # 金额小数点后被拆出空格：R208. 00 -> R208.00
    # 排除后接字母词的场景（"1. 25 Main Road" 是地址编号，不是金额）
    (re.compile(r"(\d)\.\s+(\d{2})(?!\s*[A-Za-z])"), r"\1.\2"),
    # 刷卡回执日期行的空格分隔：D:11 03 26 -> D:11-03-26（进入日期解析）
    (re.compile(r"\bD[:;](\d{2})\s+(\d{2})\s+(\d{2})(?!\d)"), r"D:\1-\2-\3"),
    # 日期的日位首数字被读成冒号：2026/03/:1 -> 2026/03/11
    (re.compile(r"(\d{4}[/-]\d{2}[/-]):(\d)"), r"\g<1>1\2"),
    # 热敏字体 ll 读成 1：标签词里的假数字会被金额解析误当候选（Bi1 Total -> 1.0）
    (re.compile(r"\bBi1{1,2}\b"), "Bill"),
    (re.compile(r"\bTota1\b"), "Total"),
]


# Q 夹在数字间读作 0（2Q8 -> 208）。两种启用条件（满足其一）：
#   a) Q 后跟「小数金额形态」（2Q8.00）——钱的长相即语境，标签与数值分列
#      两个 OCR 框时行内没有关键词，也必须能修；
#   b) 行内含金额关键词。
# 纯整数货号（"PLU 3Q5"）两条都不满足，不动。
_Q_MONEY_RE = re.compile(r"(?<=\d)Q(?=\d{0,2}[.,]\d{2}\b)")
_Q_DIGIT_RE = re.compile(r"(?<=\d)Q(?=\d)")
_AMOUNT_CTX = ("total", "amount", "tender", "balance", "due", "card",
               "合计", "总计", "金额", "应付", "实付", "价税")

# 整行「数字[:;]两位数」且首段 > 23（不可能是时刻）：小数点被 OCR 读成
# 冒号的金额（233:00 -> 233.00）。真正的时刻行由 pipeline 层丢弃。
_MONEY_COLON_RE = re.compile(r"^\s*(\d{1,3}(?:,\d{3})*|\d{1,6})[:;](\d{2})\s*$")


def _fix_text(t: str) -> str:
    for rx, rep in _TXT_FIXES:
        t = rx.sub(rep, t)
    t = _Q_MONEY_RE.sub("0", t)
    low = t.lower()
    if any(k in low for k in _AMOUNT_CTX):
        t = _Q_DIGIT_RE.sub("0", t)
    m = _MONEY_COLON_RE.match(t)
    if m:
        try:
            if int(m.group(1).replace(",", "")) > 23:
                t = f"{m.group(1)}.{m.group(2)}"
        except ValueError:
            pass
    return t


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


def quad_angle(quad) -> float:
    """旋转四边形的文字方向角（度）：较长边方向，归一化到 (-45, 45]。"""
    p0, p1, p2 = quad[0], quad[1], quad[2]
    e1 = (p1[0] - p0[0], p1[1] - p0[1])
    e2 = (p2[0] - p1[0], p2[1] - p1[1])
    v = e1 if (e1[0] ** 2 + e1[1] ** 2) >= (e2[0] ** 2 + e2[1] ** 2) else e2
    ang = math.degrees(math.atan2(v[1], v[0]))
    while ang <= -45:
        ang += 90
    while ang > 45:
        ang -= 90
    return ang


def recognize(image: np.ndarray) -> List[Dict[str, Any]]:
    """对单张位图做 OCR，返回带外接框的文本行。"""
    engine = _get_engine()
    result, _elapse = engine(image)
    lines: List[Dict[str, Any]] = []
    for item in (result or []):
        quad, text = item[0], item[1]
        score = float(item[2]) if len(item) > 2 else 1.0
        if score < 0.45:
            continue  # 过滤低置信度块（多为手写批注/污渍）
        xs = [p[0] for p in quad]
        ys = [p[1] for p in quad]
        lines.append({
            "text": _fix_text(text),
            "box": [float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys))],
            "quad": [[float(p[0]), float(p[1])] for p in quad],
            "angle": quad_angle(quad),
            "score": score,
        })
    return lines
