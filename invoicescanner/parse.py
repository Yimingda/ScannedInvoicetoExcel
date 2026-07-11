"""从文本行中解析：发票日期、各金额，并判定「带税总金额」。

判定「带税总金额」的核心思路：
  - 增值税发票：直接取「价税合计」后的金额（其定义即为含税总额）。
  - 西式小票：区分 subtotal / tax / tip / total 四类标签行，
    优先选「≈ subtotal+tax+tip」的 total 候选（校验通过→高置信度）；
    否则按关键词优先级 + 金额大小选最终 total，并做「不小于小计/税额」的合理性校验，
    从而避免误取到 subtotal（税前）或不含小费的金额。
"""
from __future__ import annotations

import re
from statistics import median
from typing import List, Dict, Any, Optional, Tuple

# ---------------------------------------------------------------- 金额

_CUR_SYMBOLS = {"$": "$", "¥": "¥", "￥": "¥", "€": "€", "£": "£",
                "RMB": "¥", "CNY": "¥", "USD": "$", "EUR": "€", "GBP": "£",
                "R": "ZAR", "ZAR": "ZAR"}

# 金额：可选币种符号 + 千分位数字 + 可选两位小数；后面可跟「元」
# 注：南非兰特写作 R560.00 / R1,655.00 —— "R" 仅在紧贴带小数的数字时算币种
_MONEY_RE = re.compile(
    r"(?P<cur>[$¥￥€£]|RMB|CNY|USD|EUR|GBP|ZAR|R(?=\s?\d{1,3}(?:,\d{3})*\.\d{2}))?\s?"
    r"(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d{1,2})?|\d+(?:\.\d{1,2})?)"
    r"\s*(?P<yuan>元)?",
    re.IGNORECASE,
)


def parse_money(text: str) -> List[Tuple[float, str, Optional[str]]]:
    """返回 [(数值, 原文片段, 币种符号或None), ...]。"""
    out = []
    for m in _MONEY_RE.finditer(text):
        num = m.group("num").replace(",", "")
        try:
            val = float(num)
        except ValueError:
            continue
        # 过滤明显不是金额的裸整数（如数量 1、2）留给上层判断，这里全部返回
        cur = None
        if m.group("cur"):
            cur = _CUR_SYMBOLS.get(m.group("cur").upper(), m.group("cur"))
        elif m.group("yuan"):
            cur = "¥"
        out.append((val, m.group(0).strip(), cur))
    return out


# 含编号类标签的行：其中的数字是 ID 不是金额（商户号/卡号/授权码/发票号…）
_ID_ROW_RE = re.compile(
    r"商户号|终端号|卡号|授权码|凭证号|批次号|流水号|订单号|参考号|发票号|校验码|电话|"
    r"terminal|merchant\s*(id|no)|auth|approval|batch|ref(erence)?\s*(no|#)|"
    r"card\s*(no|#)|account|tel|phone|"
    r"inv(oice)?\s*no|tax\s*inv\s*no",   # Tax Inv No: 1362 —— 发票号不是税额
    re.IGNORECASE,
)


def _date_spans(text: str) -> List[Tuple[int, int]]:
    """会被日期解析消费的文本跨度（其中的数字不算金额）。

    含月份名的模式必须先通过月份校验——否则「54 TOTAL 98」这种
    「数字+单词+两位数」的形状会被误当日期跨度，把真金额抹掉。
    """
    spans = []
    for rx, kind in _DATE_PATTERNS:
        for m in rx.finditer(text):
            g = m.groups()
            if kind == "Mdy" and not _month_from_token(g[0]):
                continue
            if kind in ("dMy", "dMy2") and not _month_from_token(g[1]):
                continue
            spans.append(m.span())
    return spans


# 时刻碎片（12:54 / 20:3..，含分号误读）：其中的数字不是金额。
# 尾部 (?!\d) 防止吃掉千分位误读（"1;152.00" 不是时刻）。
_TIME_SPAN_RE = re.compile(
    r"(?<![\d.])(?:[01]?\d|2[0-3])[:;][0-5]?\d(?:[:;][0-5]\d)?(?!\d)")


def _time_spans(text: str) -> List[Tuple[int, int]]:
    return [m.span() for m in _TIME_SPAN_RE.finditer(text)]


_OCR_DIGIT_FIX = [
    (re.compile(r"(?<=[\d.,])[oO](?=[\d.,])"), "0"),   # 15o5.00 -> 1505.00
    (re.compile(r"(?<=[\d.,])[lI](?=[\d.,])"), "1"),   # 1I5.00  -> 115.00
    (re.compile(r"(?<=\d)\.o(?![a-z])"), ".0"),          # 1505.o  -> 1505.0
]


def _fix_ocr_digits(text: str) -> str:
    """修复数字串内部被 OCR 读成字母的位（只动夹在数字/小数点之间的字符）。"""
    prev = None
    while prev != text:
        prev = text
        for rx, rep in _OCR_DIGIT_FIX:
            text = rx.sub(rep, text)
    return text


def amounts_in_row(text: str) -> List[Tuple[float, str, Optional[str]]]:
    """提取一行里「像金额」的数字。

    过滤：编号行（商户号/卡号…）、6 位以上无小数点的数字串（编号）、
    日期片段里的数字（年份/日）、以及 >=1 万且无小数点无币种的裸整数（发票号等）。
    """
    if _ID_ROW_RE.search(text):
        return []
    text = _fix_ocr_digits(text)
    dspans = _date_spans(text) + _time_spans(text)
    out = []
    for m in _MONEY_RE.finditer(text):
        if not m.group("num"):
            continue
        if any(s <= m.start("num") < e for s, e in dspans):
            continue  # 数字在日期片段内（如年份 2026）
        if m.start() > 0 and text[m.start() - 1] == "-":
            continue  # 负数=折扣/减免行（SAVE 15% -101.94），不是总额候选
        if m.start() > 0 and text[m.start() - 1] == "*":
            continue  # 卡号掩码尾巴（479012******0821），不是金额
        frag = m.group(0).strip()
        try:
            val = float(m.group("num").replace(",", ""))
        except ValueError:
            continue
        cur = None
        if m.group("cur"):
            cur = _CUR_SYMBOLS.get(m.group("cur").upper(), m.group("cur"))
        elif m.group("yuan"):
            cur = "¥"
        digits = re.sub(r"\D", "", m.group("num"))
        no_sep = "." not in frag and "," not in frag
        if no_sep and len(digits) >= 6:
            continue  # 长数字串 → 编号
        if no_sep and val >= 10000 and cur is None:
            continue  # 大额裸整数且无币种 → 大概率发票号/单号
        if no_sep and cur is None and 2000 <= val <= 2099:
            continue  # 形似年份的裸整数（日期粘时间时年份会漏出日期匹配）
        out.append((val, frag, cur, no_sep))
    return out


def detect_currency(full_text: str) -> Optional[str]:
    """从全文的金额匹配里统计币种（多数票胜出）。"""
    counts: Dict[str, int] = {}
    for line in full_text.splitlines():
        for _v, _frag, cur in parse_money(line):
            if cur:
                counts[cur] = counts.get(cur, 0) + 1
    if counts:
        return max(counts, key=lambda k: counts[k])
    # 兜底：明确的多字符币种代码（不含裸字母 R，避免误报）
    for token in ("RMB", "CNY", "USD", "EUR", "GBP", "ZAR", "￥", "¥", "€", "£", "$"):
        if token in full_text:
            return _CUR_SYMBOLS[token]
    return None


# ---------------------------------------------------------------- 日期

_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun",
     "jul", "aug", "sep", "oct", "nov", "dec"], start=1)}

_FULL_MONTHS = ["january", "february", "march", "april", "may", "june", "july",
                "august", "september", "october", "november", "december"]


def _edit_dist(a: str, b: str, cap: int = 2) -> int:
    """带上限的编辑距离（够用即可，月份名很短）。"""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[-1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > cap:
            return cap + 1
        prev = cur
    return prev[-1]


def _month_from_token(token: str) -> Optional[int]:
    """月份名匹配，容忍 OCR 误读（Harch->March 等，编辑距离<=2 只对全称）。

    大写 I 先还原为 l（JuI->Jul）：正确大小写的月份名里大写 I 不会出现在
    词中，而真有字母 i 的月份（April 等）i 在第 4 位起，[:3] 前缀查找不受影响。
    """
    t = token.replace("I", "l").lower().strip(".")
    t = t.replace("0", "o").replace("1", "l")
    if t[:3] in _MONTHS:
        return _MONTHS[t[:3]]
    if len(t) >= 5:
        for idx, full in enumerate(_FULL_MONTHS, 1):
            if _edit_dist(t, full) <= 2:
                return idx
    return None

_DATE_PATTERNS = [
    # 2026-07-09 / 2026/07/09（结尾不强制边界，兼容 OCR 把日期与时间粘连）
    (re.compile(r"\b(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})"), "ymd"),
    # 2026年7月9日
    (re.compile(r"(\d{4})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日"), "ymd"),
    # 07/03/2026 / 07-03-2026 —— 日月顺序有歧义，返回全部合法解释
    (re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{4})"), "ab_y4"),
    # 19/03/26 / 25-03-26 —— 两位年份（yy -> 20yy）
    (re.compile(r"\b(\d{1,2})[-/.](\d{1,2})[-/.](\d{2})(?!\d)"), "ab_y2"),
    # 月份名 token：允许 OCR 把 l/I 读成 1、o 读成 0（Ju1/JuI/N0v），
    #   仍要求首字符是字母；有效性由 _month_from_token 把关。
    # 日-年分隔：允许 「, 」「,无空格」「. 」（逗号常被读成句点）或纯空格。
    # 四位年后不许紧跟数字，防「2020|15」抓错年——但允许紧跟「时间形」(202611:24)
    # Jul 9, 2026 / July 09 2026 / Ju1 5. 2026 / Jul 5,2026
    (re.compile(r"\b([A-Za-z][A-Za-z01Il]{2,8})\.?\s+(\d{1,2})(?:\s*[.,]\s*|\s+)(\d{4})"
                r"(?:(?=\d{1,2}:\d{2})|(?!\d))"), "Mdy"),
    # 9 Jul 2026 / 29 March 2026 / 15 March 202611:24
    (re.compile(r"\b(\d{1,2})\s+([A-Za-z][A-Za-z01Il]{2,8})\.?(?:\s*[.,]\s*|\s+)(\d{4})"
                r"(?:(?=\d{1,2}:\d{2})|(?!\d))"), "dMy"),
    # 30 Mar'26 —— 撇号明确标记两位年，后面可紧跟时间（OCR 常粘连 "Mar'2608:47"）
    (re.compile(r"\b(\d{1,2})\s*([A-Za-z][A-Za-z01Il]{2,8})\.?\s*'\s*(\d{2})"), "dMy2"),
    # 30 Mar 26 —— 无撇号的两位年，后面不许紧跟数字
    (re.compile(r"\b(\d{1,2})\s+([A-Za-z][A-Za-z01Il]{2,8})\.?,?\s+(\d{2})(?!\d)"), "dMy2"),
]


def _valid(y: int, mo: int, d: int) -> bool:
    return 1900 <= y <= 2100 and 1 <= mo <= 12 and 1 <= d <= 31


def _norm(y: int, mo: int, d: int) -> str:
    return f"{y:04d}-{mo:02d}-{d:02d}"


def extract_dates(text: str, order: str = "dmy") -> List[Tuple[str, List[str]]]:
    """抽取日期，返回 [(首选ISO日期, [全部合法解释]), ...]。

    order 控制 07/03/2026 这类歧义日期的首选解释：
      dmy（默认，欧洲/南非/多数地区）→ 3月7日；mdy（美式）→ 7月3日。
    全部合法解释保留给去重模块做交叉验证。
    """
    found: List[Tuple[str, List[str]]] = []
    seen_spans: List[Tuple[int, int]] = []
    for rx, kind in _DATE_PATTERNS:
        for m in rx.finditer(text):
            # 避免同一片文字被多个模式重复解析（如 07/03/2026 也匹配两位年模式）
            span = m.span()
            if any(s <= span[0] < e or s < span[1] <= e for s, e in seen_spans):
                continue
            g = m.groups()
            cands: List[str] = []
            primary: Optional[str] = None
            try:
                if kind == "ymd":
                    y, mo, d = int(g[0]), int(g[1]), int(g[2])
                    if _valid(y, mo, d):
                        primary = _norm(y, mo, d)
                        cands = [primary]
                elif kind in ("ab_y4", "ab_y2"):
                    a, b = int(g[0]), int(g[1])
                    y = int(g[2]) if kind == "ab_y4" else 2000 + int(g[2])
                    as_dmy = _norm(y, b, a) if _valid(y, b, a) else None
                    as_mdy = _norm(y, a, b) if _valid(y, a, b) else None
                    cands = [c for c in dict.fromkeys([as_dmy, as_mdy]) if c]
                    if not cands:
                        continue
                    if order == "mdy":
                        primary = as_mdy or as_dmy
                    else:
                        primary = as_dmy or as_mdy
                elif kind == "Mdy":
                    mo = _month_from_token(g[0])
                    if not mo:
                        continue
                    d, y = int(g[1]), int(g[2])
                    if _valid(y, mo, d):
                        primary = _norm(y, mo, d)
                        cands = [primary]
                elif kind in ("dMy", "dMy2"):
                    d = int(g[0])
                    mo = _month_from_token(g[1])
                    if not mo:
                        continue
                    y = int(g[2]) if kind == "dMy" else 2000 + int(g[2])
                    if _valid(y, mo, d):
                        primary = _norm(y, mo, d)
                        cands = [primary]
            except (ValueError, IndexError):
                continue
            if primary:
                seen_spans.append(span)
                found.append((primary, cands))
    return found


def parse_dates(text: str, order: str = "dmy") -> List[str]:
    """兼容旧接口：只返回首选日期列表。"""
    out = []
    for primary, _ in extract_dates(text, order):
        if primary not in out:
            out.append(primary)
    return out


# ---------------------------------------------------------------- 行聚合

def cluster_rows(lines: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """把分散的文本块按纵向位置聚成「视觉行」，行内按 x 排序拼接。"""
    boxes = [l for l in lines if l.get("text", "").strip()]
    if not boxes:
        return []
    heights = [b["box"][3] - b["box"][1] for b in boxes]
    tol = max(4.0, median(heights) * 0.6)
    boxes = sorted(boxes, key=lambda b: (b["box"][1] + b["box"][3]) / 2)
    rows: List[List[Dict[str, Any]]] = []
    for b in boxes:
        yc = (b["box"][1] + b["box"][3]) / 2
        placed = False
        for row in rows:
            ryc = sum((x["box"][1] + x["box"][3]) / 2 for x in row) / len(row)
            if abs(yc - ryc) <= tol:
                row.append(b)
                placed = True
                break
        if not placed:
            rows.append([b])
    out = []
    for row in rows:
        row_sorted = sorted(row, key=lambda b: b["box"][0])
        text = " ".join(x["text"].strip() for x in row_sorted)
        xs = [x["box"][0] for x in row_sorted] + [x["box"][2] for x in row_sorted]
        ys = [x["box"][1] for x in row_sorted] + [x["box"][3] for x in row_sorted]
        out.append({"text": text, "box": [min(xs), min(ys), max(xs), max(ys)],
                    "members": row_sorted})
    out.sort(key=lambda r: r["box"][1])
    return out


# ---------------------------------------------------------------- 标签分类

def _canon_kw(s: str) -> str:
    """OCR 容错归一化（仅用于关键词匹配）：
    热敏纸常把 l/i 读成 1、o 读成 0（如 Bi11 Tota1 / B111 Tota1）。
    把 1/i/l 归一为 l、0/o 归一为 o，文本与关键词两边都做同样变换再比对。
    """
    return (s.lower().replace("1", "l").replace("i", "l")
            .replace("0", "o"))


def _has_kw(text_low: str, kws: List[str]) -> Optional[str]:
    canon = _canon_kw(text_low)
    for kw in kws:
        k = kw.lower()
        if k in text_low or _canon_kw(k) in canon:
            return kw
    return None


def _last_amount(text: str) -> Optional[float]:
    vals = amounts_in_row(text)
    return vals[-1][0] if vals else None


def _row_best_amount(text: str) -> Optional[Tuple[float, bool]]:
    """总额/实付行的金额：优先带小数点/币种的，取最大（OCR 碎裂时更稳）。

    返回 (金额, 是否带小数/币种)。
    """
    vals = amounts_in_row(text)
    if not vals:
        return None
    good = [v for v in vals if not v[3]]   # 带小数点/千分位
    if good:
        return max(v[0] for v in good), True
    return max(v[0] for v in vals), False


# 小费建议表的行：形如「@ 15% = R52.95  New Total = R405.95」。
# 这类行会同时伪装成 tip 和 total，必须整行忽略。OCR 会把 % 读错，
# 故按「百分号或紧跟数字的等号 + 金额」的结构识别，不依赖具体文字。
_GRATUITY_SUGGEST_RE = re.compile(
    r"(@?\s*\d{1,2}\s*[%％]\s*[=＝])|(\d{1,2}\s*[%％]\s*[=＝]\s*[R$]?\s*\d)")


def classify_rows(rows: List[Dict[str, Any]], kw: Dict[str, List[str]]):
    """返回 subtotal/tax/tip 单值、total 候选列表、tendered 候选列表、
    tip 关键词行位置列表（Gratuity:/Tip: 常留空无金额，但其「位置」是
    识别手写含小费终额的关键版式信号）。"""
    subtotal = tax = tip = None
    total_candidates: List[Dict[str, Any]] = []
    tendered: List[Dict[str, Any]] = []
    tip_positions: List[int] = []
    tendered_kws = kw.get("tendered", [])
    ignore_kws = kw.get("ignore", [])
    for idx, r in enumerate(rows):
        low = r["text"].lower()
        # 忽略行：小费建议表（Gratuity Suggestions / @X% = ... New Total ...）等干扰
        if ignore_kws and _has_kw(low, ignore_kws):
            continue
        if _GRATUITY_SUGGEST_RE.search(r["text"]):
            continue
        # tip 关键词行的位置先记下——Gratuity:/Tip: 常留空（无金额），
        # 但它出现在哪一行，是判断「其后的 TOTAL 是手写含小费终额」的版式依据
        if _has_kw(low, kw["tip"]):
            tip_positions.append(idx)
        amt = _last_amount(r["text"])
        if amt is None:
            continue
        # 分类优先级：subtotal > tip > tax > tendered > total
        if _has_kw(low, kw["subtotal"]):
            if subtotal is None:
                subtotal = amt
        elif _has_kw(low, kw["tip"]):
            if tip is None:
                tip = amt
        elif _has_kw(low, kw["tax"]):
            if tax is None:
                tax = amt
        elif _has_kw(low, tendered_kws):
            # 实付行：刷卡金额常 = 票面总额 + 小费；现金行不算（找零场景）
            if "cash" not in low and "现金" not in low:
                best = _row_best_amount(r["text"])
                if best:
                    tendered.append({"value": best[0], "text": r["text"]})
        else:
            hit = _has_kw(low, kw["total"])
            if hit:
                best = _row_best_amount(r["text"])
                if best is None:
                    continue
                rank = kw["total"].index(hit)   # 越小优先级越高
                total_candidates.append({"value": best[0], "rank": rank,
                                         "decimal": best[1], "pos": idx,
                                         "kw": hit, "text": r["text"]})
    return subtotal, tax, tip, total_candidates, tendered, tip_positions


TIP_RATIO_MAX = 1.35   # 刷卡金额最多超票面总额 35%（覆盖常见 10~30% 小费）


def _apply_tendered(total, conf, notes, tendered):
    """实付/刷卡金额仲裁。

    - 实付略大于票面（≤1.35×）：小费场景，按含小费实付取值。
    - 实付远大于票面（>1.35×）：小费解释不通——票面总额几乎必是误读
      （折扣行/邻票渗入/OCR 碎裂被当成 total），刷卡行是票面上最可信的
      结构化金额，按实付取值并注明，置信度 medium 交人工过目。
    """
    if total is None or not tendered:
        return total, conf, notes
    cand = max(t["value"] for t in tendered)
    if total < cand <= total * TIP_RATIO_MAX:
        notes.append(f"实付/刷卡 {cand} > 票面 {total}，按含小费实付金额取值")
        return cand, ("high" if conf == "high" else "medium"), notes
    if cand > total * TIP_RATIO_MAX:
        notes.append(f"实付/刷卡 {cand} 与所选票面总额 {total} 严重不符(>1.35×)，"
                     f"票面读数疑似误读，按刷卡实付取值")
        return cand, "medium", notes
    return total, conf, notes


def pick_total(subtotal, tax, tip, candidates, all_amounts, invoice_type,
               tendered=None, tip_positions=None):
    """核心：从候选中挑出「带税总金额」。

    返回 (值, 置信度, 备注, 印刷总额, 来源)。来源='fallback' 表示无任何
    关键词支撑、纯兜底取的最大金额——供上游判定「残影/碎片记录」。
    """
    tendered = tendered or []
    # 仅在有税前小计时做求和校验（增值税「已含税」场景下 税+小费 无意义）
    parts = [v for v in (subtotal, tax, tip) if v is not None]
    expected = round(sum(parts), 2) if (subtotal is not None and parts) else None
    notes: List[str] = []

    val = conf = None
    src = "kw"   # 默认按关键词体系选出

    # 增值税发票：价税合计 关键词直接命中即为含税总额
    for c in candidates:
        if "价税合计" in c["kw"]:
            val = c["value"]
            conf = "high" if (tax is not None) else "medium"
            notes.append(f"取『价税合计』={val}")
            break

    if val is None and not candidates:
        if tendered:
            # 无 total 标签但有实付/刷卡行：实付即最终支付额（含小费）
            val, conf = max(t["value"] for t in tendered), "medium"
            notes.append("无 total 标签，按实付/刷卡金额取值")
            src = "tendered"
        else:
            # 兜底：优先带小数点/币种的金额（裸整数多为编号/门牌等噪声）
            decimal_pool = [a for a, _f, _c, no_sep in all_amounts if not no_sep]
            pool = decimal_pool or [a for a, _f, _c, _n in all_amounts]
            pool = [a for a in pool if subtotal is None or a >= subtotal - 0.01] or pool
            if not pool:
                return None, "low", "未找到任何金额", None, "none"
            val, conf = max(pool), "low"
            notes.append("无 total 标签，兜底取最大合理金额")
            src = "fallback"

    # 1) 优先选与 subtotal+tax+tip 最接近的候选
    if val is None and expected is not None and expected > 0:
        tol = max(0.05, expected * 0.01)
        matched = [c for c in candidates if abs(c["value"] - expected) <= tol]
        if matched:
            best = min(matched, key=lambda c: abs(c["value"] - expected))
            val, conf = best["value"], "high"
            notes.append(f"匹配 小计+税+小费={expected} 校验通过")
        else:
            # 放宽：总额应落在 [0.95, 1.6]×期望 区间（允许上浮小费/服务费），
            # 取区间内最接近期望的候选——能排掉 OCR 碎裂出的离谱数字
            window = [c for c in candidates
                      if expected * 0.95 <= c["value"] <= expected * 1.6]
            if window:
                best = min(window, key=lambda c: abs(c["value"] - expected))
                val, conf = best["value"], "medium"
                notes.append(f"期望 小计+税+小费={expected}，区间内选最近候选")
            else:
                notes.append(f"期望 小计+税+小费={expected}，无候选精确匹配")

    # 1b) 服务费/小费验算：某候选 ≈ 另一候选 + 小费（Total 931 + Service
    #     Charge 116.38 = To Pay 1047.38）——算式精确成立即为终额，高置信。
    if val is None and tip is not None and tip > 0 and len(candidates) >= 2:
        for a in candidates:
            for b in candidates:
                if b is a:
                    continue
                tol2 = max(0.02, b["value"] * 0.001)
                if abs(a["value"] + tip - b["value"]) <= tol2:
                    val, conf = b["value"], "high"
                    notes.append(
                        f"验算通过：{a['value']} + 服务费/小费 {tip} = {b['value']}，"
                        f"按含服务费终额取值")
                    break
            if val is not None:
                break

    # 2) 按关键词优先级选；同级先看是否带小数点（更像真金额），再取较大值
    if val is None:
        best = sorted(candidates,
                      key=lambda c: (c["rank"], not c.get("decimal", True),
                                     -c["value"]))[0]
        val = best["value"]
        conf = "medium"
        # 合理性校验
        if subtotal is not None and val < subtotal - 0.01:
            conf = "low"
            notes.append(f"警告：所选 total({val}) < 小计({subtotal})")
        if tax is not None and val < tax - 0.01:
            conf = "low"
            notes.append(f"警告：所选 total({val}) < 税额({tax})")
        if len(candidates) > 1:
            others = ", ".join(f"{c['kw']}={c['value']}" for c in candidates)
            notes.append(f"候选: {others}")
        notes.append(f"按关键词『{best['kw']}』选定")

    # 3) 手写含小费终额：西式小票常见「印刷 Total(税后) → Gratuity:(留空)
    #    → TOTAL:(客人手写含小费)」版式——小费关键词行之后仍出现 total 候选时，
    #    位置最靠后的那个才是最终应付额。手写数字 OCR 误读率高，故：
    #    a) 只在 [0.6, 1.6]×当前值 区间内采信（防误读出离谱数字）；
    #    b) 置信度压到 medium 强制人工过目。
    printed_total = None   # 手写终额覆盖时保留印刷总额——刷卡回执常与它互验
    if (invoice_type != "vat" and val is not None
            and tip_positions and candidates):
        first_tip = min(tip_positions)
        after_tip = [c for c in candidates
                     if c.get("pos", -1) > first_tip
                     and 0.6 * val <= c["value"] <= 1.6 * val]
        if after_tip:
            final = max(after_tip, key=lambda c: c.get("pos", -1))
            if final["value"] != val:
                notes.append(f"小费行后另有总额 {final['value']}"
                             f"（多为手写含小费终额），按其取值；印刷总额 {val}")
                printed_total = val
                val = final["value"]
                if conf == "high":
                    conf = "medium"

    # 3b) 切边票费用验算：票左缘被裁掉时 Service Fee/Amount Due 的标签残缺，
    #     关键词全部失灵——但算式仍在。若「当前总额 + 池中某费用 = 池中
    #     最大金额」精确成立（费用 ≤35%），最大值即含费终额（Amount Due）。
    #     y 限定为池内最大且带小数的金额，巧合概率极低。
    if val is not None and invoice_type != "vat":
        pool_dec = [round(a, 2) for a, _f, _c, ns in all_amounts if not ns]
        if pool_dec:
            ymax = max(pool_dec)
            fee = round(ymax - val, 2)
            if (0 < fee <= val * 0.35
                    and any(abs(a - fee) <= 0.02 for a in pool_dec)):
                notes.append(f"费用验算：{val} + {fee} = {ymax}"
                             f"（服务费/应付标签疑似被裁），按含费终额取值")
                if printed_total is None:
                    printed_total = val
                val = ymax
                if conf == "high":
                    conf = "medium"

    # 4) 实付/刷卡金额覆盖：报销口径 = 实际支付（含小费），优先级最高
    _before = val
    val, conf, notes = _apply_tendered(val, conf, notes, tendered)
    if val != _before:
        src = "tendered"
    return val, conf, "; ".join(notes), printed_total, src


# ---------------------------------------------------------------- 单据种类（发票 vs 刷卡小票）

# 刷卡小票（POS 签购单）的强特征词
_CARD_SLIP_KWS = [
    "签购单", "商户存根", "持卡人存根", "存根", "凭证号", "批次号", "授权码",
    "交易类型", "终端号", "商户号", "pos",
    "merchant copy", "cardholder copy", "customer copy",
    "auth code", "approval code", "approval", "approved", "terminal id", "batch",
    "swiped", "chip read", "entry mode",
    "rrn", "tvr:", "tsi:", "aid:", "stan", "txn",
]
# 弱特征：卡号掩码（****1234）——很多餐饮小票也带，只作辅助
_CARD_MASK_RE = re.compile(r"\*{2,}\s*\d{4}")


def detect_doc_kind(full_text: str) -> str:
    """区分正式票据(invoice)与刷卡小票(card_slip)。

    零售发票（TAX INVOICE）常在票尾印支付明细（PURCHASE/RRN/卡号…），
    会撞上刷卡特征词——有发票头时提高判定门槛，避免整张发票被当回执。
    """
    low = full_text.lower()
    hits = sum(1 for k in _CARD_SLIP_KWS if k in low)
    has_inv_header = bool(_RECEIPT_HEADER_RE.search(_canon_kw(full_text)))
    threshold = 4 if has_inv_header else 2
    if hits >= threshold:
        return "card_slip"
    if hits == 1 and not has_inv_header and _CARD_MASK_RE.search(full_text):
        return "card_slip"
    return "invoice"


# ---------------------------------------------------------------- 顶层

def detect_type(full_text: str) -> str:
    if "价税合计" in full_text or "增值税" in full_text or "发票" in full_text:
        return "vat"
    low = full_text.lower()
    if any(k in low for k in ("subtotal", "total", "tax", "amount due", "balance due")):
        return "western"
    return "unknown"


# 一张正式票据的开头标志（每张恰好一个）——用于把「同区多票」再拆开
_RECEIPT_HEADER_RE = re.compile(
    r"tax\s*lnvolce|tax\s*invoice|invoice\s*n[or]|vat\s*invoice|"
    r"增值税.*发票|普通发票|发票\s*代码|发票\s*号码",
    re.IGNORECASE,
)


def split_stacked_receipts(lines: List[Dict[str, Any]],
                           kw: Dict[str, List[str]]) -> List[List[Dict[str, Any]]]:
    """把「一个区域里含多张正式票据」按发票头再拆开（图像分割没切干净时的兜底）。

    以「TAX INVOICE / 发票」头行为锚点：出现 2 个及以上、且各自都带有总额行时，
    在第 2 个及之后的锚点前切开。只在切出的每段都像完整票据时才采纳。
    """
    rows = cluster_rows(lines)
    anchors = [i for i, r in enumerate(rows)
               if _RECEIPT_HEADER_RE.search(_canon_kw(r["text"]))]
    if len(anchors) < 2:
        return [lines]

    # 以锚点为界切段（第一个锚点前的内容并入第一段）
    bounds = anchors[1:]
    groups: List[List[Dict[str, Any]]] = []
    start = 0
    for b in bounds:
        groups.append(rows[start:b])
        start = b
    groups.append(rows[start:])

    total_kws = kw.get("total", []) + kw.get("tendered", [])
    for g in groups:
        if len(g) < 4:
            return [lines]  # 有碎段 → 放弃拆分，避免误伤
        if not any(_has_kw(r["text"].lower(), total_kws) for r in g):
            return [lines]  # 某段没有总额行 → 不像完整票据
    # 还原成 line 列表（保留原始框，交给 parse_invoice 重新聚类）
    return [[ln for r in g for ln in r["members"]] for g in groups]


def parse_invoice(lines: List[Dict[str, Any]], kw: Dict[str, List[str]],
                  date_hint: List[str], date_order: str = "dmy") -> Dict[str, Any]:
    rows = cluster_rows(lines)

    # 币种 R 被 OCR 读成 8（R206.10 -> 8206.10）：仅当本区金额普遍带 R 前缀
    # （≥3 处）时，把「总额/实付关键词行」或「纯金额行」里紧贴小数金额的
    # 孤立前导 8 还原为 R。条件收得很紧，避免误伤真正以 8 开头的金额
    # （R8,123.45 这类带 R 的不受影响——前面是字母 R 不匹配）。
    _pre_text = "\n".join(r["text"] for r in rows)
    if len(re.findall(r"R\s?\d{1,3}[.,]\d", _pre_text)) >= 3:
        _fix8 = re.compile(r"(?<![\w.,])8(?=\d{1,3}\.\d{2}(?!\d))")
        _tot_kws = kw.get("total", []) + kw.get("tendered", [])
        for r in rows:
            if (re.fullmatch(r"\s*8\d{1,3}\.\d{2}\s*", r["text"])
                    or _has_kw(r["text"].lower(), _tot_kws)):
                r["text"] = _fix8.sub("R", r["text"])

    full_text = "\n".join(r["text"] for r in rows)
    all_amounts = [t for r in rows for t in amounts_in_row(r["text"])]

    invoice_type = detect_type(full_text)
    currency = detect_currency(full_text)

    # 日期：优先取含日期提示词的行上的日期，否则取全局第一个。
    # date_candidates 收全区所有日期解释（含 日/月 歧义、贴票粘连带入的邻票日期），
    # 供去重交叉验证——主日期选错时仍能靠候选集把同单合并回来。
    invoice_date = None
    date_candidates: List[str] = []
    for r in rows:
        low = r["text"].lower()
        if any(h.lower() in low for h in date_hint):
            ds = extract_dates(r["text"], date_order)
            if ds:
                invoice_date, date_candidates = ds[0][0], list(ds[0][1])
                break
    all_ds = extract_dates(full_text, date_order)
    if invoice_date is None and all_ds:
        invoice_date, date_candidates = all_ds[0][0], list(all_ds[0][1])
    for _p, cands in all_ds[:6]:
        for c in cands:
            if c not in date_candidates:
                date_candidates.append(c)

    subtotal, tax, tip, candidates, tendered, tip_positions = classify_rows(rows, kw)
    total, conf, notes, printed_total, total_src = pick_total(
        subtotal, tax, tip, candidates, all_amounts, invoice_type,
        tendered, tip_positions)
    # 「像钱」证据：存在带小数点/币种的金额，或任一关键词字段命中。
    # 两者皆无（只有裸整数）的区域多为条码/单号纸条，不是票据。
    has_money_amt = any((not no_sep) or cur
                        for _v, _f, cur, no_sep in all_amounts)
    kw_evidence = bool(candidates or tendered) or any(
        x is not None for x in (subtotal, tax, tip))
    moneyish = has_money_amt or kw_evidence

    return {
        "invoice_type": invoice_type,
        "doc_kind": detect_doc_kind(full_text),
        "invoice_date": invoice_date,
        "date_candidates": date_candidates,
        "currency": currency,
        "subtotal": subtotal,
        "tax": tax,
        "tip": tip,
        "total_incl_tax": total,
        "printed_total": printed_total,
        "confidence": conf,
        "notes": notes,
        "_total_src": total_src,
        "_moneyish": moneyish,
    }
