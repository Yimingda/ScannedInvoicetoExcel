"""OCR 封装：RapidOCR（PP-OCR 中英文模型 / ONNX Runtime）。

对外只暴露 recognize(image) -> List[{"text","box"}]，与 loader 的文字行结构一致，
这样下游解析不用关心文字来自「电子 PDF 文字层」还是「OCR」。
"""
from __future__ import annotations

from typing import List, Dict, Any

import numpy as np

_engine = None


def _get_engine():
    global _engine
    if _engine is None:
        from rapidocr_onnxruntime import RapidOCR
        _engine = RapidOCR()
    return _engine


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
            "text": text,
            "box": [float(min(xs)), float(min(ys)), float(max(xs)), float(max(ys))],
            "score": score,
        })
    return lines
