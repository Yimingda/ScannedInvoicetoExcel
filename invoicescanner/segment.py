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


def strip_edge_bleed(lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """剥离「邻票渗入」：裁切边缘混进来的另一张（不完整）票的残边。

    残边的两个强特征（同时保守使用，宁可漏删不误删）：
      a) 版面外：主体包络由「较宽的结构行」定出；整体落在包络之外、
         且自身很窄的行，是贴着裁切边的外来内容；
      b) 角度异类：摆正后的本票行接近同一角度；与主体角度差 >=12° 且
         偏离版面中心的行，是斜着渗入的邻票。
    删除量超过 40% 时放弃剥离（版面假设不成立，保持原样）。
    """
    boxes = [l for l in lines if l.get("text", "").strip()]
    if len(boxes) < 8:
        return lines
    widths = sorted(b["box"][2] - b["box"][0] for b in boxes)
    w_main = widths[int(len(widths) * 0.8)]
    # 结构行（宽度 >= 0.55 主宽）定包络
    struct = [b for b in boxes if (b["box"][2] - b["box"][0]) >= 0.55 * w_main]
    if len(struct) < 3:
        return lines
    env_l = min(b["box"][0] for b in struct) - 6
    env_r = max(b["box"][2] for b in struct) + 6
    env_c = (env_l + env_r) / 2
    env_w = env_r - env_l
    angles = [b.get("angle") for b in boxes
              if isinstance(b.get("angle"), (int, float))]
    a_med = median(angles) if angles else 0.0

    kept, dropped = [], 0
    for b in boxes:
        x0, x1 = b["box"][0], b["box"][2]
        w = x1 - x0
        outside = (x1 < env_l or x0 > env_r) and w < 0.5 * w_main
        ang = b.get("angle")
        tilted = (isinstance(ang, (int, float)) and abs(ang - a_med) >= 12
                  and abs((x0 + x1) / 2 - env_c) > 0.3 * env_w)
        if outside or tilted:
            dropped += 1
            continue
        kept.append(b)
    if dropped == 0 or dropped > 0.4 * len(boxes):
        return lines
    return kept


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


# ================================================================
# 路线A：整页 OCR 行 → 票据聚类 → 旋转矩形（处理物理叠压/倾斜的小票）
#
# 核心先验：每张热敏小票是一个固定宽度的长方形（57/80mm 纸 @200dpi
# 文字横向跨度约 340~680px）。即使局部被另一张票遮挡，可见文字行的
# x 跨度 + 行距连续性也足以把它们分开。
# 三阶段：
#   1) 纵向成链：x 重叠且行距小的 OCR 行连成「条带」（票内段落）；
#   2) 横向拼列：条带按「票宽先验」拼成完整票（标签列+数值列）。
#      已达完整票宽的条带拒绝吞并旁边的碎条带——防止把相邻票的
#      数值列桥接过来（叠压票最常见的错误）；
#   3) 堆叠校验：纵向相邻的组若不满足「上组含总额 & 下组头部含
#      日期/刷卡回执头」则并回同一张票；琐碎残片就近吸收。
# ================================================================

RECEIPT_MAX_W = 680       # 票面文字横向跨度上限 @200dpi（80mm 纸 ~630px + 容差）
RECEIPT_FULL_W = 340      # 文字跨度达到该值即可能自成一张「完整票」@200dpi
V_GAP_BREAK = 100.0       # 纵向断带阈值 @200dpi：票内段距 < 它 < 叠压票接缝的空隙
V_LINK_XOV = 0.5          # 纵向成链所需 x 重叠（相对较窄行宽度的比例）
H_GAP_MAX = 340.0         # 同票「标签列↔数值列」最大横向净距 @200dpi
H_YOV_MIN = 0.45          # 横向拼列所需 y 重叠（相对较矮条带 y 跨度的比例）
FRAG_PAIR_GAP = 150.0     # 两个「都不完整」的碎条带可直接合并的最大横向净距 @200dpi
# 纵向堆叠组净距小于它时视为「同票被切碎」候选并做接缝校验。
# 必须略大于 V_GAP_BREAK（碎片间隙 ≤ ~75px），且小于真实票缝（实测 ≥ ~115px）
STACK_CHECK_GAP = 108.0
ABSORB_X_EXPAND = 40.0    # 碎块吸收：候选目标组 x 区间的外扩量 @200dpi
WEAK_SCORE = 0.60         # 分数低且极短的行多为手写圈码——禁止其参与成链（防桥接）
TYPICAL_LINE_H = 25.0     # 200dpi 下小票正文行高的典型值，用于按行高推算尺度因子


def _trace(msg: str) -> None:
    import os
    if os.environ.get("INVSCAN_TRACE"):
        print("[seg]", msg)


def _gdesc(g: List[Dict[str, Any]]) -> str:
    gb = _gbox(g)
    t = " | ".join(b.get("text", "")[:12] for b in g[:2])
    return f"[{gb[0]:.0f},{gb[1]:.0f},{gb[2]:.0f},{gb[3]:.0f}]n{len(g)}({t})"


def _gbox(g: List[Dict[str, Any]]) -> List[float]:
    return [min(b["box"][0] for b in g), min(b["box"][1] for b in g),
            max(b["box"][2] for b in g), max(b["box"][3] for b in g)]


def _joined_text(g: List[Dict[str, Any]]) -> str:
    return " ".join(b.get("text", "") for b in g)


def _has_date(g: List[Dict[str, Any]]) -> bool:
    from . import parse as _p
    return bool(_p.extract_dates(_joined_text(g)))


def _row_pairs(a: List[Dict[str, Any]], b: List[Dict[str, Any]],
               h_med: float) -> int:
    """两组行里「处于同一水平线」的行对数（票的标签列/数值列逐行对齐）。"""
    n = 0
    for la in a:
        ca = (la["box"][1] + la["box"][3]) / 2
        for lb in b:
            cb = (lb["box"][1] + lb["box"][3]) / 2
            if abs(ca - cb) <= 0.7 * h_med:
                n += 1
                break
    return n


def _has_money(g: List[Dict[str, Any]]) -> bool:
    """组内是否有「带小数的金额」行（碎片是否携带关键信息）。"""
    from . import parse as _p
    for b in g:
        for _v, _f, _c, no_sep in _p.amounts_in_row(b.get("text", "")):
            if not no_sep:
                return True
    return False


def _lower_head_ok(g: List[Dict[str, Any]]) -> bool:
    """作为「下方另一张票的开头」是否合法：头部有日期/发票头，或是刷卡回执头。

    日期窗口取上部 60%——热敏票常以大幅 LOGO 开头，日期行偏下。
    """
    if _texts_have_date(g, top_ratio=0.6):
        return True
    import re
    from . import parse as _p
    head = " ".join(b.get("text", "") for b in
                    sorted(g, key=lambda b: b["box"][1])[:10])
    if re.search(r"\bD[:;]\s?\d\d", head):    # 刷卡回执的日期行 D:25-03-26
        return True
    canon = _p._canon_kw(head)
    return any(k in canon for k in ("customer copy", "approved", "tax invoice",
                                    "vat invoice", "fnb", "nedbank"))


def cluster_receipts(lines: List[Dict[str, Any]],
                     dpi_scale: float = 1.0
                     ) -> List[List[Dict[str, Any]]] | None:
    """整页 OCR 行 → 每张小票一个行组（列优先顺序）。

    不依赖墨迹投影（叠压票之间没有空白带可投影），只用 OCR 行的
    几何关系 + 票宽先验。返回 None 表示行数太少，不适用本算法。
    """
    boxes = [l for l in lines if l.get("text", "").strip()]
    if len(boxes) < 8:
        return None
    heights = [b["box"][3] - b["box"][1] for b in boxes]
    h_med = median(heights)
    # 尺度因子：横向票宽先验按 200dpi 像素标定。dpi_scale 只反映 PDF 渲染
    # 配置，对「直接上传的图片」（手机照片/高分辨率扫描）恒为 1——必须按
    # 页面自身行高推算（200dpi 小票行高 ≈ 25px），否则高分辨率单票会因
    # 超出票宽上限被拆成「标签列+数值列」两条残缺记录。
    # 死区：行高比 < 1.35 视为正常字号波动、不放大（否则 200dpi 页面上
    # 字号略大的票会把 STACK_CHECK_GAP 等阈值推过真实票缝，引发误合并——
    # 实测第2页 ⑤⑥ 两票 116px 的接缝就毁于 s≈1.1）。
    s_line = h_med / TYPICAL_LINE_H
    if s_line < 1.35:
        s_line = 1.0
    s = max(dpi_scale, s_line, 0.2)
    gv = max(V_GAP_BREAK * s, 3.5 * h_med)   # 纵向断带阈值（自适应行高）

    # 适用性守卫 a：存在横跨超过票宽上限的单行 → A4 宽幅版式（如增值税
    # 发票整页表格），本算法的窄票先验不适用，交回旧的墨迹投影路径。
    for b in boxes:
        if (b["box"][2] - b["box"][0]) > RECEIPT_MAX_W * s * 1.15:
            return None

    # ---- 1) 纵向成链（并查集） ----
    n = len(boxes)
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def weak(l):   # 低分极短行（手写圈码/污渍）：不许当「桥」，只能被吸收
        return (l.get("score", 1.0) < WEAK_SCORE
                and len(l.get("text", "").strip()) <= 4)

    for i in range(n):
        if weak(boxes[i]):
            continue
        bi = boxes[i]["box"]
        for j in range(i + 1, n):
            if weak(boxes[j]):
                continue
            bj = boxes[j]["box"]
            xov = min(bi[2], bj[2]) - max(bi[0], bj[0])
            if xov < V_LINK_XOV * min(bi[2] - bi[0], bj[2] - bj[0]):
                continue
            vgap = max(bi[1], bj[1]) - min(bi[3], bj[3])
            # 同一行被 OCR 切成两块（vgap<0）或相邻行/段（vgap<=gv）都成链
            if vgap <= gv and vgap >= -1.2 * h_med:
                parent[find(j)] = find(i)

    strips: Dict[int, List[int]] = {}
    for i in range(n):
        strips.setdefault(find(i), []).append(i)
    groups: List[List[Dict[str, Any]]] = [[boxes[k] for k in g]
                                          for g in strips.values()]

    def fit_g(g: List[Dict[str, Any]]) -> bool:
        """条带是否已像「完整票」：宽度达标 且 含金额行。
        纯标签列可以和票一样宽（如 Amount Tendered:/TOTAL 列），
        但没有任何金额——仍需并入右侧数值列。"""
        w = (max(b["box"][2] for b in g) - min(b["box"][0] for b in g))
        return (RECEIPT_FULL_W * s <= w <= RECEIPT_MAX_W * s
                and _has_money(g))

    def fit(w: float) -> bool:
        return RECEIPT_FULL_W * s <= w <= RECEIPT_MAX_W * s

    # ---- 2) 横向拼列（票宽先验驱动的贪心合并） ----
    def third_party_between(a: int, b: int, A, B) -> bool:
        """A、B 的横向间隙带（y 取两者交叠区）里是否有第三方条带的行——
        有则说明 A、B 分属间隙两侧的不同小票，不许跨过去合并。"""
        gx0, gx1 = min(A[2], B[2]), max(A[0], B[0])
        gy0, gy1 = max(A[1], B[1]), min(A[3], B[3])
        if gx1 <= gx0 or gy1 <= gy0:
            return False
        for c in range(len(groups)):
            if c in (a, b):
                continue
            for l in groups[c]:
                cx = (l["box"][0] + l["box"][2]) / 2
                cy = (l["box"][1] + l["box"][3]) / 2
                if gx0 < cx < gx1 and gy0 < cy < gy1:
                    return True
        return False

    while True:
        best = None   # (gain, -xgap, a, b)
        for a in range(len(groups)):
            A = _gbox(groups[a])
            for b in range(a + 1, len(groups)):
                B = _gbox(groups[b])
                yov = min(A[3], B[3]) - max(A[1], B[1])
                if yov < H_YOV_MIN * min(A[3] - A[1], B[3] - B[1]):
                    continue
                xgap = max(A[0], B[0]) - min(A[2], B[2])   # 净距（重叠为负）
                if xgap > H_GAP_MAX * s:
                    continue
                mw = max(A[2], B[2]) - min(A[0], B[0])
                if mw > RECEIPT_MAX_W * s:
                    continue
                fa, fb = fit_g(groups[a]), fit_g(groups[b])
                fm = fit(mw) and (_has_money(groups[a]) or _has_money(groups[b]))
                gain = int(fm) - int(fa) - int(fb)
                # 碎带凑碎带只允许拼出「窄」结果（数值列+数量列等），
                # 拼出接近整票宽的组合应由 gain>0（真正凑成整票）负责
                frag_ok = (not fa and not fb and xgap <= FRAG_PAIR_GAP * s
                           and mw <= 420 * s)
                ok = (xgap <= 0 and gain >= 0) or gain > 0 or frag_ok
                if not ok:
                    continue
                if xgap > 0:
                    # 单行条带跨空隙极易误挂到邻票（它跟谁都 y 重叠）——
                    # 交给残片吸收阶段按矩形距离归属
                    if len(groups[a]) < 2 or len(groups[b]) < 2:
                        continue
                    # 两侧各自带日期的（多为两张独立票并排）不做跨空隙合并
                    if _has_date(groups[a]) and _has_date(groups[b]):
                        continue
                    # 间隙带里有第三方条带 = 跨票桥接，禁止
                    if third_party_between(a, b, A, B):
                        continue
                    # 远距合并（>150px）须有「标签列↔数值列」的行配对证据：
                    # 至少 2 对行在同一水平线上（碎片巧合对齐通常只有 1 对）
                    if (xgap > 150 * s
                            and _row_pairs(groups[a], groups[b], h_med) < 2):
                        continue
                key = (gain, -xgap, a, b)
                if best is None or key > best:
                    best = key
        if best is None:
            break
        _g, _x, a, b = best
        _trace(f"P2 merge gain={_g} xgap={-_x:.0f}: {_gdesc(groups[a])} + {_gdesc(groups[b])}")
        groups[a].extend(groups[b])
        del groups[b]

    # ---- 3) 堆叠校验 + 残片吸收 ----
    groups = _merge_invalid_stacks(groups, s)
    groups = _absorb_tiny_groups(groups, s)
    groups = _merge_invalid_stacks(groups, s)
    # 剩下的纯标签/手写残余组（无金额无日期且行数少）：丢弃——
    # 它们挂到哪张票都可能污染解析，丢掉不损失关键字段
    if len(groups) > 1:
        kept = [g for g in groups
                if len(g) >= 5 or _has_money(g) or _has_date(g)]
        if kept:
            groups = kept

    # 列优先排序
    def colkey(gs):
        cols: List[List[float]] = []   # 每列 [x0, x1]
        order = []
        for gi in sorted(range(len(gs)), key=lambda k: _gbox(gs[k])[0]):
            gb = _gbox(gs[gi])
            placed = None
            for ci, c in enumerate(cols):
                ov = min(gb[2], c[1]) - max(gb[0], c[0])
                if ov > 0.5 * min(gb[2] - gb[0], c[1] - c[0]):
                    placed = ci
                    c[0] = min(c[0], gb[0])
                    c[1] = max(c[1], gb[2])
                    break
            if placed is None:
                cols.append([gb[0], gb[2]])
                placed = len(cols) - 1
            order.append((placed, gb[1], gi))
        # 列按 x0 排序后重编号
        rank = {ci: r for r, (ci, _) in enumerate(
            sorted(enumerate(c[0] for c in cols), key=lambda t: t[1]))}
        return [gi for _, _, gi in sorted(order,
                                          key=lambda t: (rank[t[0]], t[1]))]

    groups = [sorted(groups[i], key=lambda b: (b["box"][1], b["box"][0]))
              for i in colkey(groups)]

    # 适用性守卫 b：若有 >=2 个「大组」都既无金额行也无日期/回执特征，
    # 说明是系统性拆列（表格版式被拆成多条数值列）——整页回退旧路径。
    # 单个无特征组必须保留：严重褪色的票可能什么关键词都读不出，
    # 但仍要产出（低置信度）记录交人工——不遗漏优先于整洁。
    if len(groups) >= 2:
        featureless_big = sum(
            1 for g in groups
            if len(g) >= 5
            and not (_texts_have_total(g) or _texts_have_date(g, top_ratio=1.0)))
        if featureless_big >= 2:
            return None
    return groups


def _merge_invalid_stacks(groups: List[List[Dict[str, Any]]],
                          s: float) -> List[List[Dict[str, Any]]]:
    """纵向堆叠的相邻组：若不像「两张完整票的接缝」则并回一张。

    候选按纵向净距从小到大处理——碎片总是先并回贴得最近的一侧
    （票头碎片距正文比距上一张票近得多）。
    """
    while len(groups) > 1:
        cands = []   # (ygap, a, b)
        for a in range(len(groups)):
            A = _gbox(groups[a])
            for b in range(a + 1, len(groups)):
                B = _gbox(groups[b])
                xov = min(A[2], B[2]) - max(A[0], B[0])
                if xov < 0.4 * min(A[2] - A[0], B[2] - B[0]):
                    continue
                if max(A[2], B[2]) - min(A[0], B[0]) > RECEIPT_MAX_W * s:
                    continue
                up, lo = (a, b) if A[1] <= B[1] else (b, a)
                U, L = (A, B) if up == a else (B, A)
                ygap = L[1] - U[3]           # 负值 = y 交叠
                if ygap > STACK_CHECK_GAP * s:
                    continue
                yov = -ygap
                deep = yov > 0.25 * min(A[3] - A[1], B[3] - B[1])
                # 深度 y 交叠 = 同一张票被切碎，直接并；浅间隙做内容校验
                if not deep:
                    if _texts_have_total(groups[up]) and _lower_head_ok(groups[lo]):
                        continue   # 合法接缝：上有总额、下有票头
                cands.append((max(ygap, -1.0), a, b))
        if not cands:
            break
        _yg, a, b = min(cands)
        _trace(f"P3 stack ygap={_yg:.0f}: {_gdesc(groups[a])} + {_gdesc(groups[b])}")
        groups[a].extend(groups[b])
        del groups[b]
    return groups


def _absorb_tiny_groups(groups: List[List[Dict[str, Any]]],
                        s: float) -> List[List[Dict[str, Any]]]:
    """琐碎残片（行数/字数太少，不可能是完整票）就近并入正常组。"""
    def tiny(g):
        return len(g) <= 2 or sum(len(b["text"].strip()) for b in g) <= 14

    changed = True
    while changed and len(groups) > 1:
        changed = False
        for i in range(len(groups)):
            g = groups[i]
            if not tiny(g):
                continue
            gb = _gbox(g)
            cx = (gb[0] + gb[2]) / 2
            hosts = [j for j in range(len(groups)) if j != i and not tiny(groups[j])]
            # 吸收不许把票撑破宽度先验，也不许明显撑宽宿主（>70px 的
            # 横向外扩说明残片其实骑在两票之间，归属存疑）
            def x_expand(j):
                H = _gbox(groups[j])
                return ((max(gb[2], H[2]) - min(gb[0], H[0]))
                        - (H[2] - H[0]))
            def ok_width(j):
                H = _gbox(groups[j])
                return (max(gb[2], H[2]) - min(gb[0], H[0])) <= RECEIPT_MAX_W * s
            safe = [j for j in hosts if ok_width(j) and x_expand(j) <= 70 * s]
            if not safe:
                # 无金额也无日期的悬空标签残片：丢弃（挂错票比丢掉更伤）
                if not _has_date(g) and not _has_money(g):
                    _trace(f"P3 drop {_gdesc(g)}")
                    del groups[i]
                    changed = True
                    break
                hosts = [j for j in hosts if ok_width(j)]
                if not hosts:
                    continue
                safe = hosts
            # 优先落在「x 区间能罩住残片中心」的组里（小票是竖条，残片
            # 多是本票的行），其中取矩形净距最近者
            def rect_gap(j):
                H = _gbox(groups[j])
                dx = max(0.0, max(gb[0], H[0]) - min(gb[2], H[2]))
                dy = max(0.0, max(gb[1], H[1]) - min(gb[3], H[3]))
                return dx + dy
            within = [j for j in safe
                      if _gbox(groups[j])[0] - ABSORB_X_EXPAND * s <= cx
                      <= _gbox(groups[j])[2] + ABSORB_X_EXPAND * s]
            pool = within or safe
            j = min(pool, key=rect_gap)
            _trace(f"P3 absorb {_gdesc(g)} -> {_gdesc(groups[j])}")
            groups[j].extend(g)
            del groups[i]
            changed = True
            break
    return groups


def deskew_crop(img: "np.ndarray", group: List[Dict[str, Any]],
                dpi_scale: float = 1.0) -> "np.ndarray":
    """按行组的角度中位数把小票摆正后裁剪（摆正后重 OCR 质量显著提升）。

    角度取「较长行」的行宽加权中位数（长行的方向估计更可靠；短行/
    数字块常被检测器回退成 0°）。裁剪范围 = 旋转后所有 quad 点的
    外接框 + 边距。
    """
    import cv2
    s = max(dpi_scale, 0.2)
    pts: List[List[float]] = []
    angs: List[Tuple[float, float]] = []   # (angle, weight=行宽)
    for l in group:
        q = l.get("quad")
        if q:
            pts.extend(q)
        else:
            x0, y0, x1, y1 = l["box"]
            pts.extend([[x0, y0], [x1, y0], [x1, y1], [x0, y1]])
        w = l["box"][2] - l["box"][0]
        if w >= 120 * s and l.get("angle") is not None:
            angs.append((float(l["angle"]), w))
    ang = 0.0
    if angs:
        angs.sort()
        total = sum(w for _, w in angs)
        acc = 0.0
        for a, w in angs:
            acc += w
            if acc >= total / 2:
                ang = a
                break
    P = np.array(pts, dtype=np.float32)
    H, W = img.shape[:2]
    if abs(ang) < 0.3:   # 近乎水平：直接裁剪，省一次整页旋转
        rot, RP = img, P
    else:
        cx, cy = float(P[:, 0].mean()), float(P[:, 1].mean())
        M = cv2.getRotationMatrix2D((cx, cy), ang, 1.0)  # 正角=逆时针回正
        rot = cv2.warpAffine(img, M, (W, H), flags=cv2.INTER_LINEAR,
                             borderMode=cv2.BORDER_CONSTANT,
                             borderValue=(255, 255, 255))
        RP = (P @ M[:, :2].T) + M[:, 2]
    x0, y0 = RP[:, 0].min(), RP[:, 1].min()
    x1, y1 = RP[:, 0].max(), RP[:, 1].max()
    # 边距：横向 3.5% + 10px，纵向 2% + 10px（补半个字符/淡印边缘）
    mx = 0.035 * (x1 - x0) + 10 * s
    my = 0.02 * (y1 - y0) + 10 * s
    xa = int(max(0, x0 - mx)); ya = int(max(0, y0 - my))
    xb = int(min(W, x1 + mx)); yb = int(min(H, y1 + my))
    if xb - xa < 20 or yb - ya < 20:
        return img[int(max(0, y0)):int(min(H, y1)) or H,
                   int(max(0, x0)):int(min(W, x1)) or W]
    return np.ascontiguousarray(rot[ya:yb, xa:xb])


def ocr_upscale(crop: "np.ndarray",
                group: List[Dict[str, Any]]) -> Tuple["np.ndarray", float]:
    """小字放大供重 OCR：识别模型对 ~40px 行高最稳；200dpi 小票行高
    仅 ~25px，放大后对褪色热敏字/被圈划的数字明显更准。
    返回 (放大图, 放大倍数)——调用方须把 OCR 框坐标除以倍数还原，
    保证下游（refine/parse）看到的几何尺度不变。
    """
    import cv2
    hs = [l["box"][3] - l["box"][1] for l in group]
    h_line = sorted(hs)[len(hs) // 2] if hs else 40.0
    if h_line >= 34:
        return crop, 1.0
    f = min(2.2, 40.0 / max(h_line, 12.0))
    up = cv2.resize(crop, (int(crop.shape[1] * f), int(crop.shape[0] * f)),
                    interpolation=cv2.INTER_CUBIC)
    return up, f


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
