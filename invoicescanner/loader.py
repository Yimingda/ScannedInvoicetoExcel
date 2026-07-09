"""输入加载：把 jpg/png/pdf 统一转成「文本行列表」。

每条文本行是 dict: {"text": str, "box": [x0, y0, x1, y1]}
box 是文本块的外接框（用于按行聚合，从而把分开的标签与金额拼回同一行）。

两条路径：
  1) 电子 PDF（含文字层）—— 用 pdfplumber 直接取词及其坐标，速度快、最准。
  2) 图片 / 扫描 PDF —— 用 PyMuPDF 渲染成位图，再交给 OCR 引擎。
"""
from __future__ import annotations

import io
from pathlib import Path
from typing import List, Dict, Any

import numpy as np

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}
PDF_EXTS = {".pdf"}
SUPPORTED_EXTS = IMAGE_EXTS | PDF_EXTS


class LoadedPage:
    """一页的内容：要么是已提取的文字行，要么是待 OCR 的位图。"""

    def __init__(self, lines: List[Dict[str, Any]] | None = None,
                 image: np.ndarray | None = None):
        self.lines = lines          # 已含文字（电子 PDF）时非空
        self.image = image          # 需要 OCR 时非空（H×W×3 的 ndarray）

    @property
    def needs_ocr(self) -> bool:
        return self.image is not None and not self.lines


def _pdf_text_lines(page) -> List[Dict[str, Any]]:
    """从 pdfplumber page 提取词，返回带坐标的文本块。"""
    words = page.extract_words(use_text_flow=False, keep_blank_chars=False)
    lines = []
    for w in words:
        lines.append({
            "text": w["text"],
            "box": [float(w["x0"]), float(w["top"]),
                    float(w["x1"]), float(w["bottom"])],
        })
    return lines


def _render_pdf_page_to_image(fitz_page, dpi: int) -> np.ndarray:
    import fitz  # PyMuPDF
    zoom = dpi / 72.0
    mat = fitz.Matrix(zoom, zoom)
    pix = fitz_page.get_pixmap(matrix=mat, alpha=False)
    img = np.frombuffer(pix.samples, dtype=np.uint8).reshape(pix.height, pix.width, pix.n)
    if pix.n == 4:  # RGBA -> RGB
        img = img[:, :, :3]
    return np.ascontiguousarray(img)


def load(path: Path, prefer_text_layer: bool = True,
         pdf_render_dpi: int = 200) -> List[LoadedPage]:
    """把一个文件加载为若干 LoadedPage。"""
    ext = path.suffix.lower()
    if ext in IMAGE_EXTS:
        from PIL import Image
        img = Image.open(path).convert("RGB")
        return [LoadedPage(image=np.array(img))]

    if ext in PDF_EXTS:
        return _load_pdf(path, prefer_text_layer, pdf_render_dpi)

    raise ValueError(f"不支持的文件类型: {ext}")


def _load_pdf(path: Path, prefer_text_layer: bool, dpi: int) -> List[LoadedPage]:
    import fitz  # PyMuPDF
    import pdfplumber

    pages: List[LoadedPage] = []
    # 先探测文字层
    text_by_page: List[List[Dict[str, Any]]] = []
    if prefer_text_layer:
        try:
            with pdfplumber.open(str(path)) as pdf:
                for p in pdf.pages:
                    text_by_page.append(_pdf_text_lines(p))
        except Exception:
            text_by_page = []

    doc = fitz.open(str(path))
    try:
        for i, fpage in enumerate(doc):
            has_text = (i < len(text_by_page)
                        and sum(len(l["text"].strip()) for l in text_by_page[i]) >= 10)
            if has_text:
                pages.append(LoadedPage(lines=text_by_page[i]))
            else:
                img = _render_pdf_page_to_image(fpage, dpi)
                pages.append(LoadedPage(image=img))
    finally:
        doc.close()
    return pages


def iter_input_files(input_dir: Path) -> List[Path]:
    """列出输入目录下所有受支持的发票文件（含子目录）。"""
    files = [p for p in sorted(input_dir.rglob("*"))
             if p.is_file() and p.suffix.lower() in SUPPORTED_EXTS]
    return files
