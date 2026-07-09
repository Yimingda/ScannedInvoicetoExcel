"""编排：加载 -> (文字层/OCR) -> 版面分割 -> 逐票解析 -> 去重 -> 汇总。

一个输入文件可能产出多条记录：扫描页上常贴着多张小票（含刷卡回执），
先按空间聚类切成独立区域，再逐票解析；最后由 dedup 合并同单。
"""
from __future__ import annotations

from pathlib import Path
from typing import List, Dict, Any

from . import loader, ocr, parse, dedup, segment


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
            if seg_enabled:
                # 图像级分割：先把每张小票在图上切出来，再分别 OCR
                dpi_scale = int(ocr_cfg.get("pdf_render_dpi", 200)) / 200.0
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
                lines = ocr.recognize(pg.image)
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


def _flag_year_outliers(records: List[Dict[str, Any]], log=print) -> None:
    """同一批票据年份通常一致；年份偏离众数的记录标注请人工核对（防 OCR 误读年份）。"""
    years = [r["invoice_date"][:4] for r in records if r.get("invoice_date")]
    if len(years) < 3:
        return
    modal = max(set(years), key=years.count)
    if years.count(modal) < len(years) * 0.6:
        return  # 年份本来就分散，不做判断
    for r in records:
        d = r.get("invoice_date")
        if d and d[:4] != modal:
            note = f"日期年份({d[:4]})与批次众数({modal})不一致，疑似OCR误读，请人工核对"
            r["notes"] = (r.get("notes") or "") + ("; " if r.get("notes") else "") + note
            r["confidence"] = "low"
            log(f"      [年份异常] {r.get('source_file')}: {d}")
