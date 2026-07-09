"""编排：加载 -> (文字层/OCR) -> 版面分割 -> 逐票解析 -> 去重 -> 汇总。

一个输入文件可能产出多条记录：扫描页上常贴着多张小票（含刷卡回执），
先按空间聚类切成独立区域，再逐票解析；最后由 dedup 合并同单。
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Dict, Any

import re as _re

from . import loader, ocr, parse, dedup, segment

# 整行只是「时刻」（11:22 / 20:31:05）——不含可用字段却会被金额解析误读，
# 解析前丢弃。只匹配严格的 HH:MM(:SS) 形态（小时<24、分钟<60），
# 「小数点被 OCR 误读成冒号的金额行」(233:00) 由 ocr._fix_text 修复保留。
_BARE_TIME_RE = _re.compile(
    r"^\s*([01]?\d|2[0-3])[:;][0-5]\d(?:[:;][0-5]\d)?\s*$")


def _drop_bare_time_lines(lines):
    return [l for l in lines
            if not _BARE_TIME_RE.match(l.get("text", ""))]


def process_file(path: Path, cfg: Dict[str, Any]) -> List[Dict[str, Any]]:
    ocr_cfg = cfg.get("ocr", {})
    seg_cfg = cfg.get("segment", {}) or {}
    parsing = cfg.get("parsing", {}) or {}
    date_order = parsing.get("date_order", "dmy")
    kw = cfg["keywords"]

    pages = loader.load(
        path,
        prefer_text_layer=ocr_cfg.get("prefer_pdf_text_layer", True),
        pdf_render_dpi=int(ocr_cfg.get("pdf_render_dpi", 200)),
    )

    seg_enabled = seg_cfg.get("enabled", True)
    crop_dir = cfg.get("_crop_dir")   # 由 finalize 设定；为 None 则不存裁切图
    records: List[Dict[str, Any]] = []
    for pno, pg in enumerate(pages, 1):
        # 每个 region 是 (文本行列表, 裁切图或None)
        regions: List[tuple] = []
        if pg.needs_ocr:
            source = "OCR"
            dpi_scale = int(ocr_cfg.get("pdf_render_dpi", 200)) / 200.0
            page_lines = None
            if seg_enabled:
                # 首选：整页 OCR 行 → 票据聚类 → 旋转矩形摆正裁剪 → 重 OCR。
                # 能处理物理叠压/倾斜的小票（墨迹投影切不开的场景）。
                page_lines = ocr.recognize(pg.image)
                groups = segment.cluster_receipts(page_lines or [], dpi_scale)
                if groups and len(groups) >= 2:
                    deskew_reocr = seg_cfg.get("deskew_reocr", True)
                    for g in groups:
                        crop = segment.deskew_crop(pg.image, g, dpi_scale)
                        if deskew_reocr:
                            # 精细模式：摆正+放大后重 OCR（褪色/倾斜票更准，耗时约2x）
                            up, f = segment.ocr_upscale(crop, g)
                            lines = ocr.recognize(up)
                            if f > 1.0:   # 坐标还原到未放大的裁剪图尺度
                                for l in lines:
                                    l["box"] = [v / f for v in l["box"]]
                                    if l.get("quad"):
                                        l["quad"] = [[p[0] / f, p[1] / f]
                                                     for p in l["quad"]]
                            # 重 OCR 明显变差（裁剪/旋转失败）则退回整页行
                            if len(lines) < max(3, int(0.5 * len(g))):
                                lines = g
                            region_w = crop.shape[1] / dpi_scale
                        else:
                            # 快速模式：直接用整页 OCR 的行（坐标为页面系）
                            lines = g
                            xs0 = min(l["box"][0] for l in g)
                            xs1 = max(l["box"][2] for l in g)
                            region_w = (xs1 - xs0) / dpi_scale
                        for sub in segment.refine_wide_region(lines, region_w):
                            regions.append((sub, crop))
            if not regions and seg_enabled:
                # 回退：图像级分割（单票页/聚类不适用时），再分别 OCR
                bboxes = segment.segment_image(
                    pg.image,
                    dilate_px=max(8, int(seg_cfg.get("dilate_px", 25) * dpi_scale)),
                    merge_gap_px=int(seg_cfg.get("merge_gap_px", 70) * dpi_scale),
                )
                for (x, y, w, h) in bboxes:
                    crop = pg.image[y:y + h, x:x + w]
                    lines = ocr.recognize(crop)
                    if not lines:
                        continue
                    # 过宽区域 = 两张以上小票贴死在一起，按 OCR 框邻近聚类再细分
                    for sub in segment.refine_wide_region(lines, w / dpi_scale):
                        regions.append((sub, crop))
            if not regions:  # 分割关闭或没切出区域：整页 OCR
                lines = page_lines if page_lines is not None else ocr.recognize(pg.image)
                if lines:
                    regions = [(lines, pg.image)]
        else:
            source = "PDF文字层"
            lines = pg.lines or []
            if lines:
                if seg_enabled:
                    page_h = max(l["box"][3] for l in lines)
                    regions = [(r, None) for r in segment.segment_lines(lines, page_h)]
                else:
                    regions = [(lines, None)]

        # 图像分割没切净时的兜底：一个区域里若含多张正式票据，按发票头再拆
        split_regions: List[tuple] = []
        for region, crop in regions:
            region = _drop_bare_time_lines(region)
            if not region:
                continue
            subs = (parse.split_stacked_receipts(region, kw)
                    if seg_enabled else [region])
            for s in subs:
                split_regions.append((s, crop))

        for rno, (region, crop) in enumerate(split_regions, 1):
            rec = parse.parse_invoice(region, kw, kw.get("date_hint", []),
                                      date_order=date_order)
            suffix = (f" 第{pno}页-区{rno}"
                      if (len(pages) > 1 or len(split_regions) > 1) else "")
            rec["source_file"] = f"{path.name}{suffix}"
            rec["_source"] = source
            if crop_dir is not None and crop is not None:
                rec["_crop_path"] = _save_crop(crop, crop_dir,
                                               f"{path.stem}_p{pno}_r{rno}")
            records.append(rec)
    return records


def _save_crop(crop, crop_dir, name: str):
    """存下区域裁切图（缩到合适宽度）供复核表内嵌预览。返回路径或 None。"""
    try:
        import cv2
        Path(crop_dir).mkdir(parents=True, exist_ok=True)
        h, w = crop.shape[:2]
        max_w = 360
        if w > max_w:
            crop = cv2.resize(crop, (max_w, int(h * max_w / w)),
                              interpolation=cv2.INTER_AREA)
        out = Path(crop_dir) / f"{name}.jpg"
        # cv2.imwrite 在含中文的 Windows 路径下会静默失败——改用 imencode + 二进制写
        ok, buf = cv2.imencode(".jpg", cv2.cvtColor(crop, cv2.COLOR_RGB2BGR),
                               [cv2.IMWRITE_JPEG_QUALITY, 70])
        if not ok:
            return None
        out.write_bytes(buf.tobytes())
        return str(out)
    except Exception:
        return None


def process_files(files: List[Path], cfg: Dict[str, Any], log=print,
                  progress=None) -> List[Dict[str, Any]]:
    """处理给定的文件列表（供 CLI 与 Web UI 共用）。

    progress: 可选回调 progress(done, total, filename)，用于界面进度条。
    """
    records: List[Dict[str, Any]] = []
    total = len(files)
    for i, f in enumerate(files, 1):
        f = Path(f)
        log(f"[{i}/{total}] 处理 {f.name} ...")
        if progress:
            progress(i - 1, total, f.name)
        try:
            recs = process_file(f, cfg)
            for rec in recs:
                log(f"      {rec['source_file']}: 日期={rec['invoice_date']} "
                    f"总额={rec['total_incl_tax']} ({rec['currency'] or ''}) "
                    f"{'刷卡小票' if rec.get('doc_kind') == 'card_slip' else '票据'} "
                    f"置信度={rec['confidence']}")
            records.extend(recs)
        except Exception as e:  # 单个文件失败不影响整体（不遗漏：仍占一行）
            log(f"      [错误] {e}")
            records.append({"source_file": f.name, "notes": f"处理失败: {e}",
                            "confidence": "low"})
    if progress:
        progress(total, total, "")
    # 去重：合并指向同一笔交易的多张单据（如 发票+刷卡小票）
    before = len(records)
    records = dedup.deduplicate(records, cfg, log=log)
    if len(records) != before:
        log(f"[去重] {before} 张单据 -> {len(records)} 条记录")
    _flag_year_outliers(records, log)
    return records


def process_all(cfg: Dict[str, Any], log=print) -> List[Dict[str, Any]]:
    input_dir = Path(cfg["input_dir"])
    files = loader.iter_input_files(input_dir)
    if not files:
        log(f"[!] 输入目录无可识别文件: {input_dir.resolve()}")
        return []
    return process_files(files, cfg, log=log)


def _page_of(rec: Dict[str, Any]):
    import re
    m = re.search(r"第(\d+)页", rec.get("source_file", ""))
    return int(m.group(1)) if m else None


def enrich_review(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """为逐张确认界面补充：字段默认值、猜测来源、确认状态。

    - _amount_default / _date_default：预填给确认卡片的默认值
    - 空日期猜测优先级：本记录候选解释 > 同页其他票的多数日期 > 全批次多数日期
    - _date_guessed / _date_guess_src：是否为猜测及其来源（供界面标注）
    - _status：ok（较确信，默认勾选）/ review（待核对）/ missing（缺字段或猜测）
    - _confirm_default：是否默认勾选「确认」
    """
    dates = [r["invoice_date"] for r in records if r.get("invoice_date")]
    batch_modal = max(set(dates), key=dates.count) if dates else None

    for r in records:
        r["_amount_default"] = r.get("total_incl_tax")

        if r.get("invoice_date"):
            r["_date_default"] = r["invoice_date"]
            r["_date_guessed"] = False
            r["_date_guess_src"] = None
        else:
            guess, src = None, None
            cands = r.get("date_candidates") or []
            if cands:
                guess, src = cands[0], "candidate"
            if not guess:
                pg = _page_of(r)
                same = [x["invoice_date"] for x in records
                        if x.get("invoice_date") and _page_of(x) == pg and pg is not None]
                if same:
                    guess, src = max(set(same), key=same.count), "page"
            if not guess and batch_modal:
                guess, src = batch_modal, "batch"
            r["_date_default"] = guess or ""
            r["_date_guessed"] = guess is not None
            r["_date_guess_src"] = src

        conf = r.get("confidence")
        complete = bool(r.get("invoice_date")) and r.get("total_incl_tax") is not None
        if conf == "high" and complete:
            r["_status"], r["_confirm_default"] = "ok", True
        elif complete and conf == "medium":
            r["_status"], r["_confirm_default"] = "review", False
        else:
            r["_status"], r["_confirm_default"] = "missing", False
    return records


def _flag_year_outliers(records: List[Dict[str, Any]], log=print) -> None:
    """同一批票据年份通常一致；年份偏离众数的记录按众数纠正并标注复核。

    热敏纸上 6/0/1 一字之差的年份误读很常见（2026 -> 2020/2021），而
    日/月部分是独立字符、通常可信——按批次众数纠年，保留原读数于备注，
    并降为低置信度交人工确认（宁可保守标注，不静默采信错误年份）。
    """
    years = [r["invoice_date"][:4] for r in records if r.get("invoice_date")]
    if len(years) < 3:
        return
    modal = max(set(years), key=years.count)
    if years.count(modal) < len(years) * 0.6:
        return  # 年份本来就分散，不做判断
    def _ord(sv):
        from datetime import date as _pydate
        try:
            y, m, dd = map(int, sv.split("-"))
            return _pydate(y, m, dd).toordinal()
        except Exception:
            return None

    # 批次「主体日期」的中位序数（只取众数年的日期），用于区分
    # 「OCR 误读年份」（纠正后离主体更近）与「合法跨年票」（纠正后反而更远，
    # 如 12 月底的票在 1 月批次里：2025-12-30 距 1 月初仅几天，改成
    # 2026-12-30 反而差 11 个月——绝不能改）。
    modal_ords = [o for r in records
                  if (d := r.get("invoice_date")) and d[:4] == modal
                  and (o := _ord(d)) is not None]
    med = sorted(modal_ords)[len(modal_ords) // 2] if modal_ords else None

    for r in records:
        d = r.get("invoice_date")
        if d and d[:4] != modal:
            fixed = modal + d[4:]
            fo, oo = _ord(fixed), _ord(d)
            can_fix = (med is not None and fo is not None and oo is not None
                       and abs(fo - med) < abs(oo - med))
            if can_fix:
                note = (f"日期年份按批次众数纠正为 {fixed}（票面读作 {d}，"
                        f"疑似OCR误读年份），请人工核对")
                r["invoice_date"] = fixed
                log(f"      [年份纠正] {r.get('source_file')}: {d} -> {fixed}")
            else:
                note = (f"日期年份({d[:4]})与批次众数({modal})不一致，"
                        f"可能为跨年票或OCR误读，请人工核对")
                log(f"      [年份异常·未纠正] {r.get('source_file')}: {d}")
            r["notes"] = (r.get("notes") or "") + ("; " if r.get("notes") else "") + note
            r["confidence"] = "low"
