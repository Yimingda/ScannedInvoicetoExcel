"""去重：识别指向同一笔交易的多张单据（如 发票 + 刷卡小票）。

同单判定（保守策略，宁可漏合并、不可错合并）：
  - 带税总金额精确相等（±0.01）；金额是最强信号，不相等一律不合并
  - 且 双方日期都存在时，相差 <= date_tolerance_days 天（开票常晚于刷卡几天）
  - 任一方缺日期 → 不自动合并，只在备注标「疑似重复」，交人工复核（确保不遗漏）

主记录优先级（谁留下代表这笔交易）：
  正式发票 > 刷卡小票；同级时 vat > western > unknown；再同级取解析字段多、置信度高者。

两种模式（config: dedup.mode）：
  - skip：重复票不单独占行，其文件名并入主记录备注（默认；金额列可直接求和不重复计数）
  - mark：重复票照常写入，但备注标记「重复:与 xxx 同单」，由使用者自行筛选
"""
from __future__ import annotations

from datetime import date
from typing import List, Dict, Any, Optional

_KIND_RANK = {"invoice": 0, "card_slip": 1}
_TYPE_RANK = {"vat": 0, "western": 1, "unknown": 2}
_CONF_RANK = {"high": 0, "medium": 1, "low": 2}


def _parse_iso(s: Optional[str]) -> Optional[date]:
    if not s:
        return None
    try:
        y, m, d = s.split("-")
        return date(int(y), int(m), int(d))
    except (ValueError, AttributeError):
        return None


def _priority(rec: Dict[str, Any]) -> tuple:
    fields = sum(1 for k in ("subtotal", "tax", "tip", "invoice_date")
                 if rec.get(k) is not None)
    return (
        _KIND_RANK.get(rec.get("doc_kind"), 1),
        _TYPE_RANK.get(rec.get("invoice_type"), 2),
        _CONF_RANK.get(rec.get("confidence"), 2),
        -fields,
    )


TIP_RATIO_MAX = 1.35   # 与 parse.py 一致：刷卡金额最多超票面 35%


def _date_set(rec: Dict[str, Any]) -> List[date]:
    """记录的全部合法日期解释（含 日/月 歧义的两种读法）。"""
    cands = rec.get("date_candidates") or []
    if not cands and rec.get("invoice_date"):
        cands = [rec["invoice_date"]]
    out = []
    for c in cands:
        d = _parse_iso(c)
        if d:
            out.append(d)
    return out


def _dates_match(a: Dict[str, Any], b: Dict[str, Any], tol_days: int) -> Optional[bool]:
    """None=任一方缺日期；True/False=候选日期集合是否有一对在容差内。"""
    da, db = _date_set(a), _date_set(b)
    if not da or not db:
        return None
    return any(abs((x - y).days) <= tol_days for x in da for y in db)


SUSPECT_GAP_DAYS = 3   # 金额匹配但日期差超容差时，差距 ≤ 它则标「疑似同单」交人工


def _same_transaction(a: Dict[str, Any], b: Dict[str, Any],
                      date_tol_days: int) -> Optional[str]:
    """返回 'dup'（确认同单）/ 'suspect'（缺日期疑似）/
    'suspect_gap'（金额匹配但日期差 1~3 天，发票↔回执对，交人工）/ None。"""
    ta, tb = a.get("total_incl_tax"), b.get("total_incl_tax")
    if ta is None or tb is None:
        return None
    exact = abs(ta - tb) <= 0.011
    ka, kb = a.get("doc_kind"), b.get("doc_kind")
    cross = {ka, kb} == {"invoice", "card_slip"}
    if not exact:
        if not cross:
            return None
        inv, slip = (a, b) if ka == "invoice" else (b, a)
        inv_t, slip_t = inv["total_incl_tax"], slip["total_incl_tax"]
        # 两种「金额不等但确为同单」的版式：
        #  1) 小费刷进卡里：刷卡金额 = 票面 + 小费（slip > inv，比例受限）
        #  2) 小费付了现金：发票终额=手写(印刷+小费)，刷卡只付印刷部分
        #     → 回执金额 ≈ 发票的印刷总额（强互验信号，精确到分）
        tip_on_card = inv_t < slip_t <= inv_t * TIP_RATIO_MAX
        pt = inv.get("printed_total")
        tip_in_cash = (pt is not None and abs(slip_t - pt) <= 0.011
                       and slip_t < inv_t <= slip_t * TIP_RATIO_MAX)
        if not (tip_on_card or tip_in_cash):
            return None
        # 金额不等的合并要求日期必须双方都有且匹配（更严格，防误合并）
        if _dates_match(a, b, date_tol_days):
            return "dup"
        if _dates_match(a, b, SUSPECT_GAP_DAYS):
            return "suspect_gap"   # 开票常晚于刷卡 1~2 天：不自动合并但要提醒
        return None
    dm = _dates_match(a, b, date_tol_days)
    if dm is True:
        return "dup"
    if dm is None:
        # 金额精确一致的 发票↔刷卡回执 对，一方日期不可读：合并（回执的
        # 机打日期可靠，合并后按回执取日期）。同类单据仍只标疑似交人工。
        if cross:
            return "dup"
        return "suspect"
    if cross and _dates_match(a, b, SUSPECT_GAP_DAYS):
        return "suspect_gap"
    return None


def _gap_days(a: Dict[str, Any], b: Dict[str, Any]) -> int:
    """两记录候选日期集合的最小天数差（用于疑似标注文案）。"""
    da, db = _date_set(a), _date_set(b)
    if not da or not db:
        return -1
    return min(abs((x - y).days) for x in da for y in db)


def _append_note(rec: Dict[str, Any], note: str) -> None:
    old = rec.get("notes") or ""
    rec["notes"] = f"{old}; {note}" if old else note


def _kind_label(rec: Dict[str, Any]) -> str:
    return "刷卡小票" if rec.get("doc_kind") == "card_slip" else "发票"


def deduplicate(records: List[Dict[str, Any]], cfg: Dict[str, Any],
                log=print) -> List[Dict[str, Any]]:
    dcfg = cfg.get("dedup", {}) or {}
    if not dcfg.get("enabled", True):
        return records
    mode = dcfg.get("mode", "skip")
    tol = int(dcfg.get("date_tolerance_days", 3))

    # 并查集式分组（按确认同单关系传递合并）
    n = len(records)
    group = list(range(n))

    def find(i):
        while group[i] != i:
            group[i] = group[group[i]]
            i = group[i]
        return i

    suspects: List[tuple] = []
    for i in range(n):
        for j in range(i + 1, n):
            verdict = _same_transaction(records[i], records[j], tol)
            if verdict == "dup":
                group[find(j)] = find(i)
            elif verdict in ("suspect", "suspect_gap"):
                suspects.append((i, j, verdict))

    # 疑似重复：双方都标注，不合并（防止金额被重复计入却无人察觉）
    for i, j, kind in suspects:
        if find(i) == find(j):
            continue  # 已被确认关系合并
        if kind == "suspect":
            msg = "疑似重复(金额相同,日期缺失)"
        else:
            gap = _gap_days(records[i], records[j])
            msg = (f"疑似同单(金额匹配,日期相差{gap}天,未自动合并)——"
                   f"若确为同一笔请删除其一或调大 date_tolerance_days")
        _append_note(records[i], f"{msg}: {records[j]['source_file']}")
        _append_note(records[j], f"{msg}: {records[i]['source_file']}")
        for k in (i, j):
            if records[k].get("confidence") != "low":
                records[k]["confidence"] = "medium"

    # 按组选主记录
    groups: Dict[int, List[int]] = {}
    for i in range(n):
        groups.setdefault(find(i), []).append(i)

    out: List[Dict[str, Any]] = []
    for _, idxs in sorted(groups.items(), key=lambda kv: min(kv[1])):
        if len(idxs) == 1:
            solo = records[idxs[0]]
            # 未匹配到发票的刷卡回执：明确标注——它可能本身就是唯一凭证
            # （超市/加油站常只有回执），也可能对应的发票识别失败，请留意
            if (solo.get("doc_kind") == "card_slip"
                    and "疑似" not in (solo.get("notes") or "")):
                _append_note(solo, "独立刷卡回执：本批未找到金额相符的发票，"
                                   "可能本身即消费凭证")
            out.append(solo)
            continue
        idxs_sorted = sorted(idxs, key=lambda i: _priority(records[i]))
        primary, dups = records[idxs_sorted[0]], [records[i] for i in idxs_sorted[1:]]
        merged = ", ".join(f"{d['source_file']}({_kind_label(d)})" for d in dups)
        _append_note(primary, f"同单合并: {merged}")
        # 金额：取组内最大（含小费终额 >= 印刷/刷卡部分，报销按最终应付）
        group = [primary] + dups
        group_totals = [r.get("total_incl_tax") for r in group
                        if r.get("total_incl_tax") is not None]
        best_total = max(group_totals) if group_totals else None
        if best_total is not None and primary.get("total_incl_tax") != best_total:
            _append_note(primary,
                         f"金额按含小费终额 {best_total} 取值，本单据读数 "
                         f"{primary['total_incl_tax']}")
            primary["total_incl_tax"] = best_total
        # 互验：组内同时有发票与刷卡回执 → 金额得到独立单据印证。
        # 全组金额精确一致 → 升为高置信；小费差额场景保持原置信但标注互验。
        kinds = {r.get("doc_kind") for r in group}
        if {"invoice", "card_slip"} <= kinds:
            primary["slip_verified"] = True
            exact_all = (best_total is not None
                         and all(abs(t - best_total) <= 0.011 for t in group_totals))
            if exact_all:
                primary["confidence"] = "high"
                _append_note(primary, "已与刷卡回执互验（金额一致）")
            else:
                _append_note(primary, "已与刷卡回执互验（回执印证印刷部分，终额含小费）")
        # 日期：优先用刷卡小票的机打日期（格式统一，几乎无歧义）
        slip_dates = [d.get("invoice_date") for d in dups
                      if d.get("doc_kind") == "card_slip" and d.get("invoice_date")]
        if slip_dates and primary.get("doc_kind") != "card_slip":
            if primary.get("invoice_date") != slip_dates[0]:
                _append_note(primary,
                             f"日期按刷卡小票 {slip_dates[0]} 取值，票面读作 {primary.get('invoice_date')}")
            primary["invoice_date"] = slip_dates[0]
        log(f"      [去重] {primary['source_file']} <- 合并 {merged}")
        out.append(primary)
        if mode == "mark":
            for d in dups:
                _append_note(d, f"重复: 与 {primary['source_file']} 同单，勿重复计入")
                d["confidence"] = "low"
                out.append(d)
    return out
