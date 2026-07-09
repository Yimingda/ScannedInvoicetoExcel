"""InvoiceScanner —— Streamlit 可视化界面（双语 / 逐张确认）。

流程：选语言与解析模式 → 上传发票 → 一键识别 → 逐张卡片确认（左票右值，
较确信默认勾选，空字段按上下文猜测预填）→ 下载填好的 Excel。

本地运行：.venv\\Scripts\\python.exe -m streamlit run app.py
"""
from __future__ import annotations

import base64
import io
import tempfile
from pathlib import Path

import streamlit as st
import yaml

from invoicescanner import pipeline, excel_writer

# ---- Streamlit Cloud 热更新兜底 ----
# Cloud「Pulling code changes」后有时不重启 Python 进程：app.py 是新的，
# 但 invoicescanner 包还是 sys.modules 里缓存的旧模块（缺新函数 → AttributeError）。
# 检测到关键函数缺失时，清掉整个包的模块缓存重新导入，实现自愈。
if not hasattr(pipeline, "enrich_review") or not hasattr(excel_writer, "build_workbook"):
    import sys as _sys
    for _n in [n for n in list(_sys.modules) if n.startswith("invoicescanner")]:
        _sys.modules.pop(_n, None)
    from invoicescanner import pipeline, excel_writer  # noqa: F811

ROOT = Path(__file__).parent

# ----------------------------------------------------------------- 文案（中/英）
I18N = {
    "zh": {
        "title": "🧾 扫描发票 · 日期与带税金额提取",
        "caption": "本地 OCR（RapidOCR），离线运行。上传发票 → 识别 → 逐张确认 → 下载导入表。",
        "settings": "⚙️ 设置",
        "language": "语言 / Language",
        "parse_mode": "解析内容",
        "mode_all": "全部信息", "mode_min": "仅日期 + 金额",
        "dpi": "扫描件渲染 DPI（越高越准越慢）",
        "date_order": "歧义日期读法",
        "dmy": "日/月/年（南非·欧洲）", "mdy": "月/日/年（美式）",
        "dedup": "自动合并 发票 + 刷卡回执（去重）",
        "upload_tpl": "上传你的 Excel 模板（写入『费用信息』B/C=日期、K=金额）",
        "tpl_missing": "未检测到默认模板，未上传时用通用表头。",
        "tpl_hint": "留空则用默认/通用模板；列映射见 config.yaml。",
        "uploader": "拖入发票文件（图片 / 扫描 PDF / 电子 PDF，可多选）",
        "run": "🚀 开始识别", "selected_n": "已选择 {n} 个文件",
        "processing": "识别中 {done}/{total} … {name}",
        "m_total": "识别出记录", "m_review": "需确认", "m_ok": "较确信",
        "review_title": "逐张确认",
        "review_help": "🟢 较确信=已默认勾选；🟡 待核对；🔴 缺字段（空白已按上下文猜测预填）。核对无误后勾「确认」。",
        "col_date": "日期", "col_amount": "金额", "col_currency": "币种",
        "col_subtotal": "税前小计", "col_tax": "税额", "col_tip": "小费/服务费",
        "col_kind": "单据种类", "col_type": "类型", "col_notes": "备注",
        "confirm": "确认",
        "guess_candidate": "猜测·另一种日期解释", "guess_page": "猜测·同页多数日期",
        "guess_batch": "猜测·本批多数日期",
        "st_ok": "🟢 较确信", "st_review": "🟡 待核对", "st_missing": "🔴 缺字段",
        "kind_invoice": "发票", "kind_card": "刷卡小票",
        "only_confirmed": "仅导出已确认的行",
        "confirmed_count": "已确认 {c}/{t} 行",
        "download": "⬇️ 下载报销导入表（{n} 行）",
        "nothing_to_export": "没有可导出的行（勾选或填入至少一行）。",
        "gen_fail": "生成 Excel 失败：{e}",
        "logs": "查看处理日志",
        "empty_hint": "👆 上传发票后点「开始识别」。首次识别会先加载 OCR 模型，请稍候。",
        "reparse_hint": "改了设置？重新点「开始识别」即可按新设置解析。",
    },
    "en": {
        "title": "🧾 Scanned Invoice · Date & Tax-Inclusive Total",
        "caption": "Local OCR (RapidOCR), fully offline. Upload → recognize → confirm each → download.",
        "settings": "⚙️ Settings",
        "language": "语言 / Language",
        "parse_mode": "Parse fields",
        "mode_all": "All fields", "mode_min": "Date + amount only",
        "dpi": "Scan render DPI (higher = better but slower)",
        "date_order": "Ambiguous date order",
        "dmy": "D/M/Y (ZA · EU)", "mdy": "M/D/Y (US)",
        "dedup": "Auto-merge invoice + card slip (dedup)",
        "upload_tpl": "Upload your Excel template (writes sheet, B/C=date, K=amount)",
        "tpl_missing": "No default template found; a generic header is used unless you upload one.",
        "tpl_hint": "Leave empty to use the default/generic template; see config.yaml for column mapping.",
        "uploader": "Drop invoice files (image / scanned PDF / digital PDF, multiple allowed)",
        "run": "🚀 Recognize", "selected_n": "{n} file(s) selected",
        "processing": "Recognizing {done}/{total} … {name}",
        "m_total": "Records", "m_review": "Need review", "m_ok": "Confident",
        "review_title": "Confirm each receipt",
        "review_help": "🟢 confident = pre-checked; 🟡 review; 🔴 missing (blanks pre-filled by best guess). Tick “Confirm” once verified.",
        "col_date": "Date", "col_amount": "Amount", "col_currency": "Currency",
        "col_subtotal": "Subtotal", "col_tax": "Tax", "col_tip": "Tip/Service",
        "col_kind": "Doc type", "col_type": "Category", "col_notes": "Notes",
        "confirm": "Confirm",
        "guess_candidate": "guess · alternate date reading", "guess_page": "guess · majority date on page",
        "guess_batch": "guess · majority date in batch",
        "st_ok": "🟢 Confident", "st_review": "🟡 Review", "st_missing": "🔴 Missing",
        "kind_invoice": "Invoice", "kind_card": "Card slip",
        "only_confirmed": "Export confirmed rows only",
        "confirmed_count": "Confirmed {c}/{t} rows",
        "download": "⬇️ Download filled sheet ({n} rows)",
        "nothing_to_export": "Nothing to export (check or fill at least one row).",
        "gen_fail": "Failed to build Excel: {e}",
        "logs": "View processing log",
        "empty_hint": "👆 Upload invoices then click Recognize. The OCR model loads on first run.",
        "reparse_hint": "Changed a setting? Click Recognize again to re-parse.",
    },
}

st.set_page_config(page_title="Invoice → Excel", page_icon="🧾", layout="wide")


def get_lang() -> str:
    return st.session_state.get("lang", "zh")


def t(key: str, **kw) -> str:
    s = I18N[get_lang()].get(key, key)
    return s.format(**kw) if kw else s


# ----------------------------------------------------------------- 资源
@st.cache_data
def load_cfg() -> dict:
    return yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))


@st.cache_resource(show_spinner="加载 OCR 模型 / Loading OCR model…")
def warm_engine():
    import numpy as np
    from invoicescanner import ocr
    ocr.recognize(np.full((80, 200, 3), 255, dtype=np.uint8))
    return True


def kind_label(doc_kind: str) -> str:
    return {"invoice": t("kind_invoice"), "card_slip": t("kind_card")}.get(doc_kind, "")


# ----------------------------------------------------------------- 侧边栏
cfg = load_cfg()

with st.sidebar:
    st.radio(I18N["zh"]["language"], ["中文", "English"],
             index=0 if get_lang() == "zh" else 1,
             key="_lang_pick",
             on_change=lambda: st.session_state.update(
                 lang="zh" if st.session_state["_lang_pick"] == "中文" else "en"))
    if "lang" not in st.session_state:
        st.session_state["lang"] = "zh"

    st.title(t("settings"))
    mode = st.radio(t("parse_mode"), ["all", "min"],
                    format_func=lambda m: t("mode_all") if m == "all" else t("mode_min"),
                    key="parse_mode")
    dpi = st.slider(t("dpi"), 150, 400,
                    int(cfg.get("ocr", {}).get("pdf_render_dpi", 200)), 50)
    date_order = st.radio(t("date_order"), ["dmy", "mdy"],
                          format_func=lambda o: t(o), key="date_order")
    dedup_on = st.checkbox(t("dedup"), value=True)
    st.divider()
    tpl_file = st.file_uploader(t("upload_tpl"), type=["xlsx"])
    if tpl_file is None and not (ROOT / cfg["template"]["path"]).exists():
        st.warning(t("tpl_missing"))
    else:
        st.caption(t("tpl_hint"))


# ----------------------------------------------------------------- 顶部
st.title(t("title"))
st.caption(t("caption"))

uploads = st.file_uploader(
    t("uploader"),
    type=["pdf", "jpg", "jpeg", "png", "bmp", "tif", "tiff", "webp"],
    accept_multiple_files=True)

c_run, c_info = st.columns([1, 3])
run = c_run.button(t("run"), type="primary", disabled=not uploads, width="stretch")
if uploads:
    c_info.info(t("selected_n", n=len(uploads)))

if run:
    warm_engine()
    run_cfg = dict(cfg)
    run_cfg["ocr"] = dict(run_cfg.get("ocr", {}), pdf_render_dpi=dpi)
    run_cfg["parsing"] = dict(run_cfg.get("parsing", {}), date_order=date_order)
    run_cfg["dedup"] = dict(run_cfg.get("dedup", {}), enabled=dedup_on)

    workdir = Path(tempfile.mkdtemp(prefix="invsc_"))
    run_cfg["_crop_dir"] = str(workdir / "crops")
    paths = []
    for uf in uploads:
        p = workdir / uf.name
        p.write_bytes(uf.getbuffer())
        paths.append(p)

    bar = st.progress(0.0, text="…")
    logs: list[str] = []
    records = pipeline.process_files(
        paths, run_cfg, log=logs.append,
        progress=lambda d, tot, n: bar.progress(d / max(tot, 1),
                                                 text=t("processing", done=d, total=tot, name=n)))
    pipeline.enrich_review(records)
    bar.empty()
    # 清掉上一批的确认控件状态（避免行数变化时错位）
    import re as _re
    for k in list(st.session_state.keys()):
        if _re.match(r"^(dt|am|cf|cur|sub|tax|tip)_\d+$", k):
            del st.session_state[k]
    st.session_state["records"] = records
    st.session_state["logs"] = logs
    st.session_state["tpl_bytes"] = tpl_file.getvalue() if tpl_file else None


# ----------------------------------------------------------------- 逐张确认
def crop_bytes(path):
    return Path(path).read_bytes() if path and Path(path).exists() else None


def render_card(i: int, r: dict, mode: str):
    status = r.get("_status", "review")
    badge = {"ok": t("st_ok"), "review": t("st_review"), "missing": t("st_missing")}[status]
    with st.container(border=True):
        c_img, c_val = st.columns([2, 3])
        with c_img:
            b = crop_bytes(r.get("_crop_path"))
            if b:
                st.image(b, width=260)
            st.caption(r.get("source_file", ""))
        with c_val:
            head = st.columns([3, 2])
            head[0].markdown(f"**{badge}**  ·  `{r.get('confidence','')}`")
            head[1].checkbox(t("confirm"), value=r.get("_confirm_default", False),
                             key=f"cf_{i}")
            f1, f2 = st.columns(2)
            f1.text_input(t("col_date"), value=r.get("_date_default", ""),
                          placeholder="YYYY-MM-DD", key=f"dt_{i}")
            if r.get("_date_guessed"):
                f1.caption("↑ " + t("guess_" + (r.get("_date_guess_src") or "batch")))
            amt = r.get("_amount_default")
            f2.number_input(t("col_amount"), value=(None if amt is None else float(amt)),
                            step=1.0, format="%.2f", key=f"am_{i}")
            if mode == "all":
                d = st.columns(4)
                d[0].text_input(t("col_currency"), value=r.get("currency") or "",
                                key=f"cur_{i}")
                d[1].text_input(t("col_subtotal"),
                                value="" if r.get("subtotal") is None else str(r["subtotal"]),
                                key=f"sub_{i}", disabled=True)
                d[2].text_input(t("col_tax"),
                                value="" if r.get("tax") is None else str(r["tax"]),
                                key=f"tax_{i}", disabled=True)
                d[3].text_input(t("col_tip"),
                                value="" if r.get("tip") is None else str(r["tip"]),
                                key=f"tip_{i}", disabled=True)
                meta = f"{kind_label(r.get('doc_kind'))} · {r.get('invoice_type','')}"
                st.caption(meta + (f" · {r['notes']}" if r.get("notes") else ""))


records = st.session_state.get("records")
if records:
    mode = st.session_state.get("parse_mode", "all")
    n_ok = sum(1 for r in records if r.get("_status") == "ok")
    n_review = len(records) - n_ok
    m = st.columns(3)
    m[0].metric(t("m_total"), len(records))
    m[1].metric(t("m_ok"), n_ok)
    m[2].metric(t("m_review"), n_review)

    st.subheader(t("review_title"))
    st.caption(t("review_help"))
    for i, r in enumerate(records):
        render_card(i, r, mode)

    # 汇总编辑结果
    only_conf = st.checkbox(t("only_confirmed"), value=False)
    export, confirmed = [], 0
    for i in range(len(records)):
        cf = st.session_state.get(f"cf_{i}", False)
        d = (st.session_state.get(f"dt_{i}", "") or "").strip()
        a = st.session_state.get(f"am_{i}")
        if cf:
            confirmed += 1
        if only_conf and not cf:
            continue
        if a is None and not d:
            continue
        export.append({"invoice_date": d or None,
                       "total_incl_tax": None if a is None else float(a)})

    st.caption(t("confirmed_count", c=confirmed, t=len(records)))
    if export:
        try:
            wb = excel_writer.build_workbook(
                export, cfg["template"],
                template_source=io.BytesIO(st.session_state["tpl_bytes"])
                if st.session_state.get("tpl_bytes") else None)
            buf = io.BytesIO(); wb.save(buf)
            st.download_button(
                t("download", n=len(export)), data=buf.getvalue(), type="primary",
                file_name="报销导入_已填.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
        except Exception as e:
            st.error(t("gen_fail", e=e))
    else:
        st.info(t("nothing_to_export"))

    with st.expander(t("logs")):
        st.code("\n".join(st.session_state.get("logs", [])), language="text")
    st.caption(t("reparse_hint"))
else:
    st.info(t("empty_hint"))
