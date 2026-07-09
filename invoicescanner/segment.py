"""版面分割：把「一页贴多张小票」的扫描页切成独立的小票区域。

两种方式：
  A. segment_image —— 图像级（扫描页首选）：墨迹二值化 + 膨胀连块 + 连通域，
     先在图像上把每张小票切出来再分别 OCR。抗倾斜，且能避免 OCR 把相邻
     两张小票同一行的文字合并进一个文本框。
  B. segment_lines —— 文本块级（供带文字层的 PDF 使用）：x/y 投影找空隙。

输出顺序统一为：列从左到右、列内从上到下（与人工贴票/录入顺序一致）。
"""
from __future__ import annotations

from statistics import median
from typing import List, Dict, Any, Tuple

import numpy as np


def segment_image(img: np.ndarray,
                  dilate_px: int = 25,
                  merge_gap_px: int = 70,
                  min_area_ratio: float = 0.004) -> List[Tuple[int, int, int, int]]:
    """在页面图像上定位各小票区域，返回 [(x, y, w, h), ...]（列优先顺序）。

    参数按 200dpi 标定：dilate_px≈行距的 1.5 倍，merge_gap_px≈3 行高
    （小票内部段落空隙 < merge_gap_px < 上下两张小票的间距）。
    """
    import cv2

    gray = cv2.cvtColor(img, cv2.COLOR_RGB2GRAY)
    # 自适应阈值提取墨迹（对热敏纸褪色、页面阴影更稳）
    bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_MEAN_C,
                               cv2.THRESH_BINARY_INV, 31, 15)

    # 先删掉手写圈线/勾划：特征是连通域「包围盒大、填充率低」的细长笔迹。
    # 印刷字符是小而密的连通域，不受影响。若不删，划线会把相邻小票桥接成一块。
    n0, labels0, stats0, _ = cv2.connectedComponentsWithStats(bw)
    for i in range(1, n0):
        x, y, w, h, area = stats0[i]
        big = w > 80 or h > 80
        sparse = area < 0.15 * w * h
        # 细长直线：小票纸边的扫描阴影/表格线/下划线（会堵死投影空白带）
        thin_line = min(w, h) <= 12 and max(w, h) >= 100
        if (big and sparse) or thin_line:
            bw[labels0 == i] = 0

    # 2x2 开运算去掉孤立扫描噪点（1px 斑点），保留 >=2px 的文字笔画
    bw = cv2.morphologyEx(bw, cv2.MORPH_OPEN,
                          cv2.getStructuringElement(cv2.MORPH_RECT, (2, 2)))
    ink = bw  # 投影直接用去噪后的墨迹图（膨胀会把空隙里的残余噪声放大）

    H, W = gray.shape
    boxes: List[List[int]] = []
    _xy_cut(ink, 0, 0, W, H, boxes,
            min_gap_x=int(dilate_px * 0.7),   # 竖直空隙 ≥ ~18px 即切列
            min_gap_y=merge_gap_px,           # 水平空隙 ≥ ~70px 即切票
            depth=0)

    # 过滤太小/太空的区域
    boxes = [b for b in boxes
             if b[2] > 80 and b[3] > 60
             and int((ink[b[1]:b[1] + b[3], b[0]:b[0] + b[2]] > 0).sum()) > 800]
    # 小碎块（高度不足一张小票）向同列最近邻并入
    boxes = _merge_vertical(boxes, merge_gap_px)
    boxes = _absorb_small(boxes, merge_gap_px * 2)
    # 列优先排序
    return _column_major_order(boxes)


def _ink_gaps(profile: "np.ndarray", noise: float, min_gap: int) -> List[Tuple[int, int]]:
    """在投影剖面里找「近乎无墨」的空隙段（忽略两端留白）。"""
    idx = np.where(profile > noise)[0]
    if len(idx) == 0:
        return []
    gaps = []
    lo, hi = int(idx[0]), int(idx[-1])
    run_start = None
    for i in range(lo, hi + 1):
        if profile[i] <= noise:
            if run_start is None:
                run_start = i
        else:
            if run_start is not None and i - run_start >= min_gap:
                gaps.append((run_start, i))
            run_start = None
    return gaps


def _xy_cut(ink: "np.ndarray", x: int, y: int, w: int, h: int,
            out: List[List[int]], min_gap_x: int, min_gap_y: int,
            depth: int) -> None:
    """经典 XY-cut：交替沿竖直/水平空白带递归切分。"""
    region = ink[y:y + h, x:x + w]
    ys, xs = np.where(region > 0)
    if len(xs) == 0:
        return
    # 收紧到实际内容范围
    x0, x1 = int(xs.min()), int(xs.max()) + 1
    y0, y1 = int(ys.min()), int(ys.max()) + 1
    region = region[y0:y1, x0:x1]
    ax, ay, aw, ah = x + x0, y + y0, x1 - x0, y1 - y0

    if depth >= 6:
        out.append([ax, ay, aw, ah])
        return

    col_ink = (region > 0).sum(axis=0)   # 每一竖列的墨量
    row_ink = (region > 0).sum(axis=1)   # 每一横行的墨量
    # 噪声容忍：扫描斑点/纸纹会在空白带里留下少量墨点（约占轴长的 0.8%）
    x_gaps = _ink_gaps(col_ink, noise=max(3.0, ah * 0.008), min_gap=min_gap_x)
    y_gaps = _ink_gaps(row_ink, noise=max(3.0, aw * 0.008), min_gap=min_gap_y)

    # 优先沿更宽的空隙方向切
    if not x_gaps and not y_gaps:
        out.append([ax, ay, aw, ah])
        return

    def widest(gaps):
        return max((g1 - g0 for g0, g1 in gaps), default=0)

    if widest(x_gaps) >= widest(y_gaps):
        cuts, axis = x_gaps, "x"
    else:
        cuts, axis = y_gaps, "y"

    prev = 0
    bounds = []
    for g0, g1 in cuts:
        bounds.append((prev, g0))
        prev = g1
    bounds.append((prev, aw if axis == "x" else ah))

    for b0, b1 in bounds:
        if b1 - b0 < 10:
            continue
        if axis == "x":
            _xy_cut(ink, ax + b0, ay, b1 - b0, ah, out,
                    min_gap_x, min_gap_y, depth + 1)
        else:
            _xy_cut(ink, ax, ay + b0, aw, b1 - b0, out,
                    min_gap_x, min_gap_y, depth + 1)


def _rect_gap(a: List[int], b: List[int]) -> float:
    """两个 [x,y,w,h] 矩形的净间距（重叠为 0）。"""
    dx = max(0, max(a[0], b[0]) - min(a[0] + a[2], b[0] + b[2]))
    dy = max(0, max(a[1], b[1]) - min(a[1] + a[3], b[1] + b[3]))
    return dx + dy


def _absorb_small(boxes: List[List[int]], gap: int,
                  min_h: int = 250) -> List[List[int]]:
    """把高度太小、不像完整小票的碎块并入矩形间距最近的大块。

    用二维矩形间距而不是「同列纵向间距」——小票的金额列碎块在
    原票的右侧（x 不重叠、y 重叠），按列判断会被错误吸给上下邻居。
    """
    changed = True
    while changed:
        changed = False
        for i, s in enumerate(boxes):
            if s[3] >= min_h:
                continue
            best, best_gap = None, gap + 1
            for j, b in enumerate(boxes):
                if i == j or b[3] < min_h:
                    continue
                d = _rect_gap(s, b)
                if d < best_gap:
                    best, best_gap = j, d
            if best is not None:
                b = boxes[best]
                x0 = min(s[0], b[0]); y0 = min(s[1], b[1])
                x1 = max(s[0] + s[2], b[0] + b[2]); y1 = max(s[1] + s[3], b[1] + b[3])
                boxes[best] = [x0, y0, x1 - x0, y1 - y0]
                del boxes[i]
                changed = True
                break
    return boxes


def _merge_vertical(boxes: List[List[int]], gap: int) -> List[List[int]]:
    changed = True
    while changed:
        changed = False
        boxes.sort(key=lambda b: (b[0], b[1]))
        for i in range(len(boxes)):
            for j in range(i + 1, len(boxes)):
                a, b = boxes[i], boxes[j]
                ov = min(a[0] + a[2], b[0] + b[2]) - max(a[0], b[0])
                if ov < 0.6 * min(a[2], b[2]):
                    continue
                vgap = max(a[1], b[1]) - min(a[1] + a[3], b[1] + b[3])
                if vgap > gap:
                    continue
                x0 = min(a[0], b[0]); y0 = min(a[1], b[1])
                x1 = max(a[0] + a[2], b[0] + b[2]); y1 = max(a[1] + a[3], b[1] + b[3])
                boxes[i] = [x0, y0, x1 - x0, y1 - y0]
                del boxes[j]
                changed = True
                break
            if changed:
                break
    return boxes


WIDE_REGION_PX = 680   # 200dpi 下 ~8.6cm，超过它的区域大概率是两张小票贴在一起


def refine_wide_region(lines: List[Dict[str, Any]],
                       region_w: float) -> List[List[Dict[str, Any]]]:
    """对「过宽」区域（两张以上小票贴死在一起）按 OCR 框做两级细分：

    1) 零穿越切分：找一条没有任何文本框跨越的竖线/横线（贴得再近，只要
       没有内容跨界就能切开）。递归执行。
    2) 邻近聚类：仍然过宽的组（多为倾斜叠贴），按框间距做并查集聚类。
    """
    boxes = [l for l in lines if l.get("text", "").strip()]
    if len(boxes) < 12 or region_w <= WIDE_REGION_PX:
        return [lines]

    heights = [b["box"][3] - b["box"][1] for b in boxes]
    h_med = median(heights) if heights else 20.0

    groups = _cut_recursive(boxes, h_med, depth=0)

    final: List[List[Dict[str, Any]]] = []
    for g in groups:
        gw = (max(b["box"][2] for b in g) - min(b["box"][0] for b in g))
        if gw > WIDE_REGION_PX and len(g) >= 24:
            final.extend(_proximity_clusters(g, h_med))
        else:
            final.append(g)

    final = _absorb_narrow_groups(final)
    final.sort(key=lambda g: (min(b["box"][0] for b in g),
                              min(b["box"][1] for b in g)))
    return [sorted(g, key=lambda b: (b["box"][1], b["box"][0])) for g in final]


def _find_zero_cut(boxes: List[Dict[str, Any]], axis: int, h_med: float):
    """找一条「几乎没有框跨越」的切线。axis: 0=竖线切x, 1=横线切y。

    竖切时排除特别宽的框，并容忍少量跨线框（OCR 会把贴死的两张小票
    同一行的文字合并成一个框），跨线框按中心归边。
    横切要求空带宽 >= 3.2 行高（避免把票内的段落空隙当成两张票）。
    """
    lo = min(b["box"][axis] for b in boxes)
    hi = max(b["box"][axis + 2] for b in boxes)
    extent = hi - lo
    if extent < 340:      # 两张小票至少各 ~170px 宽/高
        return None
    considered = boxes
    if axis == 0:
        wide_lim = 0.6 * extent
        considered = [b for b in boxes
                      if (b["box"][2] - b["box"][0]) < wide_lim]
        tol = max(2, int(0.10 * len(considered)))   # 容忍的跨线框数
        min_run, margin = 8, 160
    else:
        tol = 0
        min_run, margin = 3.2 * h_med, 140

    # 扫描「跨越数 <= tol」的低穿越区间
    runs = []          # (run_start, run_end, 区间内最大跨越数)
    run_start = None
    run_max = 0
    step = 4
    s = lo + margin
    while s <= hi - margin:
        crossing = sum(1 for b in considered
                       if b["box"][axis] < s < b["box"][axis + 2])
        if crossing <= tol:
            if run_start is None:
                run_start, run_max = s, crossing
            run_max = max(run_max, crossing)
        else:
            if run_start is not None and s - run_start >= min_run:
                runs.append((run_start, s, run_max))
            run_start = None
        s += step
    if run_start is not None and hi - margin - run_start >= min_run:
        runs.append((run_start, hi - margin, run_max))

    # 先看跨越最少的（真边界几乎无内容跨过），同级再看空带更宽的
    for r0, r1, _rm in sorted(runs, key=lambda r: (r[2], -(r[1] - r[0]))):
        cut = (r0 + r1) / 2
        left = [b for b in boxes
                if (b["box"][axis] + b["box"][axis + 2]) / 2 <= cut]
        right = [b for b in boxes
                 if (b["box"][axis] + b["box"][axis + 2]) / 2 > cut]
        if len(left) < 6 or len(right) < 6:
            continue

        def _extent(g):
            return (max(b["box"][axis + 2] for b in g)
                    - min(b["box"][axis] for b in g))
        if _extent(left) < 160 or _extent(right) < 160:
            continue
        # 内容校验：切出来的两边必须都「像完整的小票」——
        # 竖切防止把票内的「标签列|数值列」空隙当成两票边界；
        # 横切防止把一张票从中腰斩（票的日期在头部、总额在尾部）。
        if axis == 0 and not (_side_is_receiptlike(left) and _side_is_receiptlike(right)):
            continue
        if axis == 1 and not _valid_y_cut(left, right):
            continue
        return cut
    return None


def _side_is_receiptlike(g: List[Dict[str, Any]]) -> bool:
    """一侧要像完整小票：有 >=2 行带小数的金额，且有日期或刷卡回执特征。

    小票内部的「标签列」（Purchase/TOTAL/Batch#…）没有金额；
    「数值列」全是数字但没有日期——都会被判为不完整。
    """
    from . import parse as _p
    money_rows = 0
    for b in g:
        for _v, _f, _c, no_sep in _p.amounts_in_row(b.get("text", "")):
            if not no_sep:
                money_rows += 1
                break
    if money_rows < 2:
        return False
    joined = " ".join(b.get("text", "") for b in g)
    if _p.extract_dates(joined):
        return True
    canon = _p._canon_kw(joined)
    return any(k in canon for k in ("customer copy", "approved", "merchant"))


def _texts_have_total(g: List[Dict[str, Any]]) -> bool:
    from . import parse as _p
    # 排除「小费建议表」的 New Total 行，它不是票的总额区
    texts = [b.get("text", "") for b in g]
    canon = _p._canon_kw(" ".join(
        t for t in texts
        if "suggest" not in t.lower() and "new total" not in t.lower()))
    for kw in ("total", "tendered", "amount due", "balance", "paid", "purchase",
               "合计", "总计", "应付", "实付", "价税"):
        if _p._canon_kw(kw) in canon:
            return True
    return False


def _texts_have_date(g: List[Dict[str, Any]], top_ratio: float = 0.45) -> bool:
    from . import parse as _p
    ys = [b["box"][1] for b in g]
    y0, y1 = min(ys), max(b["box"][3] for b in g)
    lim = y0 + (y1 - y0) * top_ratio
    joined = " ".join(b.get("text", "") for b in g if b["box"][1] <= lim)
    return bool(_p.extract_dates(joined))


def _valid_y_cut(upper: List[Dict[str, Any]],
                 lower: List[Dict[str, Any]]) -> bool:
    """横切合法性：上段应含总额类行（它是完整小票的尾部），
    下段应在头部含日期或本身是刷卡回执（它是另一张小票的开头）。"""
    if not _texts_have_total(upper):
        return False
    if _texts_have_date(lower):
        return True
    # 刷卡回执常以 D:xx-xx-xx / CUSTOMER COPY / APPROVED 开头
    from . import parse as _p
    head = " ".join(b.get("text", "") for b in lower[:8]).lower()
    return any(k in _p._canon_kw(head) for k in
               ("customer copy", "approved", "nedbank", "fnb", "d:"))


def _cut_recursive(boxes: List[Dict[str, Any]], h_med: float,
                   depth: int) -> List[List[Dict[str, Any]]]:
    if depth >= 5 or len(boxes) < 12:
        return [boxes]
    for axis in (0, 1):
        cut = _find_zero_cut(boxes, axis, h_med)
        if cut is not None:
            a = [b for b in boxes
                 if (b["box"][axis] + b["box"][axis + 2]) / 2 <= cut]
            c = [b for b in boxes
                 if (b["box"][axis] + b["box"][axis + 2]) / 2 > cut]
            return (_cut_recursive(a, h_med, depth + 1)
                    + _cut_recursive(c, h_med, depth + 1))
    return [boxes]


def _proximity_clusters(boxes: List[Dict[str, Any]],
                        h_med: float) -> List[List[Dict[str, Any]]]:
    """并查集邻近聚类（处理倾斜叠贴、零穿越切不开的组）。"""
    wide_lim = 0.6 * (max(b["box"][2] for b in boxes)
                      - min(b["box"][0] for b in boxes))
    eligible = [i for i, b in enumerate(boxes)
                if (b["box"][2] - b["box"][0]) < wide_lim]
    parent = list(range(len(boxes)))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for ii in range(len(eligible)):
        for jj in range(ii + 1, len(eligible)):
            a = boxes[eligible[ii]]["box"]
            b = boxes[eligible[jj]]["box"]
            xov = min(a[2], b[2]) - max(a[0], b[0])
            yov = min(a[3], b[3]) - max(a[1], b[1])
            dy = max(a[1], b[1]) - min(a[3], b[3])
            dx = max(a[0], b[0]) - min(a[2], b[2])
            v_link = xov > 0.3 * min(a[2] - a[0], b[2] - b[0]) and dy <= 1.8 * h_med
            h_link = yov > 0.5 * min(a[3] - a[1], b[3] - b[1]) and dx <= 1.3 * h_med
            if v_link or h_link:
                parent[find(eligible[jj])] = find(eligible[ii])

    groups: Dict[int, List[int]] = {}
    for i in eligible:
        groups.setdefault(find(i), []).append(i)
    cluster_list = sorted(groups.values(), key=len, reverse=True)
    if not cluster_list:
        return [boxes]

    # 特别宽的框（OCR 跨票合并行）按 x 重叠最大归属
    for i in range(len(boxes)):
        if i in {k for g in cluster_list for k in g}:
            continue
        a = boxes[i]["box"]
        best, best_ov = cluster_list[0], 0.0
        for g in cluster_list:
            gx0 = min(boxes[k]["box"][0] for k in g)
            gx1 = max(boxes[k]["box"][2] for k in g)
            ov = min(a[2], gx1) - max(a[0], gx0)
            if ov > best_ov:
                best, best_ov = g, ov
        best.append(i)

    # 碎片（<5 框）并入中心最近的大聚类
    big = [g for g in cluster_list if len(g) >= 5]
    if not big:
        return [boxes]

    def center(g):
        return (sum((boxes[k]["box"][0] + boxes[k]["box"][2]) / 2 for k in g) / len(g),
                sum((boxes[k]["box"][1] + boxes[k]["box"][3]) / 2 for k in g) / len(g))

    for g in cluster_list:
        if g in big:
            continue
        gc = center(g)
        tgt = min(big, key=lambda B: (center(B)[0] - gc[0]) ** 2
                  + (center(B)[1] - gc[1]) ** 2)
        tgt.extend(g)

    # 既没日期也没总额特征的聚类不是独立小票（如小费建议表），并入最近的
    def orphan(g):
        items = [boxes[k] for k in g]
        joined = " ".join(b.get("text", "") for b in items)
        from . import parse as _p
        return not _texts_have_total(items) and not _p.extract_dates(joined)

    keep = [g for g in big if not orphan(g)] or big
    if len(keep) < len(big):
        for g in big:
            if g in keep:
                continue
            gc = center(g)
            tgt = min(keep, key=lambda B: (center(B)[0] - gc[0]) ** 2
                      + (center(B)[1] - gc[1]) ** 2)
            tgt.extend(g)
    return [[boxes[k] for k in g] for g in keep]


def _absorb_narrow_groups(groups: List[List[Dict[str, Any]]]
                          ) -> List[List[Dict[str, Any]]]:
    """x 宽度太窄的组（多为被切散的金额列）并入水平最近的正常组。"""
    def extent(g):
        return (max(b["box"][2] for b in g) - min(b["box"][0] for b in g))

    def cx(g):
        return sum((b["box"][0] + b["box"][2]) / 2 for b in g) / len(g)

    normal = [g for g in groups if extent(g) >= 160]
    if not normal or len(normal) == len(groups):
        return groups
    for g in groups:
        if g not in normal:
            tgt = min(normal, key=lambda N: abs(cx(N) - cx(g)))
            tgt.extend(g)
    return normal


def _column_major_order(boxes: List[List[int]]) -> List[Tuple[int, int, int, int]]:
    """列从左到右、列内从上到下排序。"""
    cols: List[Dict[str, Any]] = []
    for b in sorted(boxes, key=lambda b: b[0]):
        placed = False
        for c in cols:
            ov = min(b[0] + b[2], c["x1"]) - max(b[0], c["x0"])
            if ov > 0.5 * min(b[2], c["x1"] - c["x0"]):
                c["items"].append(b)
                c["x0"] = min(c["x0"], b[0])
                c["x1"] = max(c["x1"], b[0] + b[2])
                placed = True
                break
        if not placed:
            cols.append({"x0": b[0], "x1": b[0] + b[2], "items": [b]})
    cols.sort(key=lambda c: c["x0"])
    out = []
    for c in cols:
        for b in sorted(c["items"], key=lambda b: b[1]):
            out.append((b[0], b[1], b[2], b[3]))
    return out


def _gap_centers(boxes: List[Dict[str, Any]], axis: int, span: float,
                 max_cover: float, min_gap: float) -> List[float]:
    """某一轴向投影覆盖度的「内部空隙」中心线。axis: 0=x, 1=y。"""
    n = int(span) + 2
    cover = np.zeros(n)
    lo_all, hi_all = n, 0
    for b in boxes:
        lo = max(0, int(b["box"][axis]))
        hi = min(n, int(b["box"][axis + 2]) + 1)
        cover[lo:hi] += 1
        lo_all, hi_all = min(lo_all, lo), max(hi_all, hi)

    centers = []
    in_gap = False
    start = 0
    for i in range(lo_all, hi_all):  # 只看内容范围内部，页边留白不算空隙
        if cover[i] <= max_cover:
            if not in_gap:
                in_gap, start = True, i
        else:
            if in_gap and i - start >= min_gap:
                centers.append((start + i) / 2)
            in_gap = False
    return centers


def _partition(boxes: List[Dict[str, Any]], axis: int,
               centers: List[float]) -> List[List[Dict[str, Any]]]:
    """按分割线把 boxes 分组（按各框中心点落区）。"""
    if not centers:
        return [boxes]
    groups: List[List[Dict[str, Any]]] = [[] for _ in range(len(centers) + 1)]
    for b in boxes:
        c = (b["box"][axis] + b["box"][axis + 2]) / 2
        k = sum(1 for line in centers if c > line)
        groups[k].append(b)
    return [g for g in groups if g]


def segment_lines(lines: List[Dict[str, Any]], page_h: float,
                  gap_factor: float = 3.0,
                  min_gap_abs_ratio: float = 0.025) -> List[List[Dict[str, Any]]]:
    """把一页的文本块切分成若干「小票区域」，每个区域是文本块列表。"""
    boxes = [l for l in lines if l.get("text", "").strip()]
    if not boxes:
        return []

    page_w = max(b["box"][2] for b in boxes) + 10
    heights = [b["box"][3] - b["box"][1] for b in boxes]
    h_med = median(heights) if heights else 12.0

    # 1) 列切分：x 投影。空隙容忍 1 个跨列框；最窄空隙 ~1 个行高
    col_centers = _gap_centers(boxes, axis=0, span=page_w,
                               max_cover=1, min_gap=max(h_med * 0.8, 10))
    columns = _partition(boxes, axis=0, centers=col_centers)
    columns.sort(key=lambda g: min(b["box"][0] for b in g))

    # 2) 列内分段：y 投影。空隙需 >= gap_factor 倍行高（票内段落空隙 ~1-2 行高）
    segments: List[List[Dict[str, Any]]] = []
    for col in columns:
        gap_thresh = max(gap_factor * h_med, min_gap_abs_ratio * page_h)
        row_centers = _gap_centers(col, axis=1, span=page_h,
                                   max_cover=0, min_gap=gap_thresh)
        parts = _partition(col, axis=1, centers=row_centers)
        parts.sort(key=lambda g: min(b["box"][1] for b in g))
        segments.extend(parts)

    # 3) 过滤噪声段：文本量太小（比如只有一个手写编号/条码数字）并入前一段
    out: List[List[Dict[str, Any]]] = []
    for seg in segments:
        total_chars = sum(len(b["text"].strip()) for b in seg)
        if len(seg) >= 3 and total_chars >= 20:
            out.append(sorted(seg, key=lambda b: (b["box"][1], b["box"][0])))
        elif out:
            out[-1].extend(seg)
    return out
