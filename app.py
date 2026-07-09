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
        "review_help": "蓝影 [OK]=较确信、已默认勾选；黄影 [??]=待核对；红影 [!!]=缺字段（空白已按上下文猜测预填，虚线蓝字标注来源）。核对无误后勾「确认」。",
        "col_date": "日期", "col_amount": "金额", "col_currency": "币种",
        "col_subtotal": "税前小计", "col_tax": "税额", "col_tip": "小费/服务费",
        "col_kind": "单据种类", "col_type": "类型", "col_notes": "备注",
        "confirm": "确认",
        "guess_candidate": "猜测·另一种日期解释", "guess_page": "猜测·同页多数日期",
        "guess_batch": "猜测·本批多数日期",
        "st_ok": "较确信", "st_review": "待核对", "st_missing": "缺字段",
        "brand_tail": "本地 OCR · 离线运行 · 逐张确认",
        "sub": "票据审计工作台 · 本地 OCR · 数据不出本机",
        "step1": "上传", "step2": "识别", "step3": "确认", "step4": "导出",
        "quality": "识别精度",
        "q_fine": "精细 · 摆正重OCR", "q_fast": "快速 · 约2倍速",
        "q_help": "精细：每张小票摆正+放大后重新识别，褪色/倾斜票明显更准（实测 16/16，推荐）；快速：跳过重识别，速度约2倍（实测 15/16，多2条冗余行）。",
        "sec_recognize": "识别", "sec_parse": "解析", "sec_template": "模板",
        "quick_dates": "候选日期（点击填入）",
        "hero_title": "三步，把一叠票据变成报销导入表",
        "hero_1t": "上传", "hero_1d": "图片、扫描 PDF、电子 PDF 均可；一页贴多张小票、叠压、倾斜都能处理。",
        "hero_2t": "识别", "hero_2d": "本地 OCR 自动分割每张小票，提取日期与带税（含小费）实付金额。首次运行需加载模型。",
        "hero_3t": "确认", "hero_3d": "逐张对照原图核对，确认即跳下一张；空缺日期已按上下文预填候选。",
        "foot_l": "INVOICE/AUDIT · LOCAL OCR", "foot_r": "本地离线处理 · 数据不出本机",
        "pos": "REC {i} / {n}",
        "btn_prev": "← 上一张", "btn_skip": "跳过 →", "btn_confirm_next": "✓ 确认，下一张",
        "done_all": "全部处理完毕", "back_first": "回到第 1 张",
        "m_confirmed": "已确认", "m_left": "待处理",
        "confirmed_badge": "[OK] 已确认",
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
        "review_help": "Blue shadow [OK] = confident, pre-checked; amber [??] = review; red [!!] = missing (blanks pre-filled by best guess, source noted in blue mono). Tick “Confirm” once verified.",
        "col_date": "Date", "col_amount": "Amount", "col_currency": "Currency",
        "col_subtotal": "Subtotal", "col_tax": "Tax", "col_tip": "Tip/Service",
        "col_kind": "Doc type", "col_type": "Category", "col_notes": "Notes",
        "confirm": "Confirm",
        "guess_candidate": "guess · alternate date reading", "guess_page": "guess · majority date on page",
        "guess_batch": "guess · majority date in batch",
        "st_ok": "Confident", "st_review": "Review", "st_missing": "Missing",
        "brand_tail": "LOCAL OCR · OFFLINE · CONFIRM-EACH",
        "sub": "Receipt audit workbench · local OCR · data stays on this machine",
        "step1": "Upload", "step2": "Scan", "step3": "Confirm", "step4": "Export",
        "quality": "Recognition quality",
        "q_fine": "Fine · deskew re-OCR", "q_fast": "Fast · ~2x speed",
        "q_help": "Fine: each receipt is deskewed, upscaled and re-recognized — much better on faded/tilted paper (16/16 on benchmark, recommended). Fast: skip re-OCR, ~2x faster (15/16, a couple of noise rows).",
        "sec_recognize": "Recognition", "sec_parse": "Parsing", "sec_template": "Template",
        "quick_dates": "Candidate dates (click to fill)",
        "hero_title": "Three steps from a pile of receipts to a filled sheet",
        "hero_1t": "Upload", "hero_1d": "Images, scanned or digital PDFs; pages with several overlapping or tilted receipts are fine.",
        "hero_2t": "Scan", "hero_2d": "Local OCR splits every receipt and extracts the date and the tax-inclusive amount actually paid. First run loads the model.",
        "hero_3t": "Confirm", "hero_3d": "Verify each receipt against its image; confirming advances automatically. Missing dates come pre-filled with best guesses.",
        "foot_l": "INVOICE/AUDIT · LOCAL OCR", "foot_r": "processed locally · data never leaves this machine",
        "pos": "REC {i} / {n}",
        "btn_prev": "← Prev", "btn_skip": "Skip →", "btn_confirm_next": "✓ Confirm → next",
        "done_all": "All done", "back_first": "Back to first",
        "m_confirmed": "Confirmed", "m_left": "Remaining",
        "confirmed_badge": "[OK] CONFIRMED",
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

st.set_page_config(page_title="INVOICE/AUDIT", page_icon="🧾", layout="wide")

# ----------------------------------------------------------------- 「审计终端」主题
# 瑞士审计纸（纸白/粗黑边/硬阴影/克莱因蓝/大字重数字）× 荧光扫描台（等宽数据/扫描光带/终端标签）
SWX_CSS = """
<style>
:root{ --paper:#F7F7F4; --ink:#141414; --blue:#1436F5; --grey:#767670; --line:#D8D8D2;
       --amber:#8A6D1F; --red:#C0392B;
       --mono:ui-monospace,'Cascadia Mono',Consolas,'Courier New',monospace; }
html, body, [data-testid="stAppViewContainer"]{ background:var(--paper); }
[data-testid="stHeader"]{ background:rgba(247,247,244,.85); }
h1,h2,h3{ letter-spacing:-.01em; color:var(--ink); }

/* 版心：收拢宽屏，留白更从容 */
[data-testid="stMainBlockContainer"], .block-container{ max-width:1180px; }

/* 品牌栏 + 流程步骤 */
.swx-mast{ border-bottom:3px solid var(--ink); padding:4px 0 16px; margin-bottom:20px;
           display:flex; justify-content:space-between; align-items:flex-end;
           flex-wrap:wrap; gap:14px; }
.swx-mast .t{ font-size:31px; font-weight:800; letter-spacing:-.015em; line-height:1.05;
              color:var(--ink); }
.swx-mast .t .mono{ font-family:var(--mono); color:var(--blue); font-weight:700; }
.swx-mast .sub{ font-family:var(--mono); font-size:11px; letter-spacing:.16em;
                color:var(--grey); margin-top:7px; text-transform:uppercase; }
.swx-steps{ display:flex; align-items:stretch; }
.swx-step{ font-family:var(--mono); font-size:10.5px; letter-spacing:.12em;
           padding:7px 15px 6px; border:1.5px solid var(--line); border-left-width:0;
           color:var(--grey); display:flex; gap:9px; align-items:baseline;
           background:#fff; text-transform:uppercase; }
.swx-step:first-child{ border-left-width:1.5px; }
.swx-step b{ font-size:13px; font-weight:800; font-variant-numeric:tabular-nums; }
.swx-step.done{ border-color:var(--ink); color:var(--ink); }
.swx-step.cur{ background:var(--blue); border-color:var(--blue); color:#fff; }

/* 空状态主视觉 */
.swx-hero{ border:1.5px solid var(--ink); background:#fff; box-shadow:8px 8px 0 var(--line);
           padding:32px 36px 28px; margin:8px 0 12px; }
.swx-hero .ht{ font-size:20px; font-weight:800; letter-spacing:-.01em; margin-bottom:20px;
               color:var(--ink); }
.swx-hero .cols{ display:flex; gap:32px; flex-wrap:wrap; }
.swx-hero .c{ flex:1; min-width:190px; border-top:3px solid var(--ink); padding-top:11px; }
.swx-hero .n{ font-family:var(--mono); font-size:11px; color:var(--blue);
              letter-spacing:.18em; font-weight:700; }
.swx-hero .ct{ font-weight:800; font-size:15.5px; margin:3px 0 5px; color:var(--ink); }
.swx-hero .cd{ font-size:12.5px; color:var(--grey); line-height:1.6; }

/* 候选日期快捷键（按钮 key=chip_* 的紧凑样式） */
[class*="st-key-chip_"] button{ font-family:var(--mono) !important; font-size:12px !important;
    min-height:30px !important; padding:2px 10px !important;
    box-shadow:2px 2px 0 var(--line) !important; }

/* 页脚 */
.swx-foot{ margin-top:46px; border-top:1.5px solid var(--line); padding-top:11px;
           font-family:var(--mono); font-size:10.5px; letter-spacing:.14em;
           color:var(--grey); display:flex; justify-content:space-between;
           flex-wrap:wrap; gap:6px; text-transform:uppercase; }

/* 侧栏分组标签 */
.swx-sec{ font-family:var(--mono); font-size:10px; letter-spacing:.2em; color:var(--grey);
          text-transform:uppercase; margin:12px 0 0; }

/* 指标行 */
.swx-metrics{ display:flex; gap:26px; margin:14px 0 6px; }
.swx-m{ flex:1; border-left:3px solid var(--ink); padding:2px 0 4px 14px; }
.swx-m b{ display:block; font-size:34px; font-weight:800; letter-spacing:-.03em; line-height:1.1;
          font-variant-numeric:tabular-nums; color:var(--ink); }
.swx-m.blue b{ color:var(--blue); }
.swx-m span{ font-family:var(--mono); font-size:10.5px; color:var(--grey);
             letter-spacing:.14em; text-transform:uppercase; }

/* 确认卡片（st.container key= 会生成 st-key-card_状态_序号 类名） */
[class*="st-key-card_"]{ background:#fff; border:1.5px solid var(--ink);
    padding:16px 16px 12px; margin-bottom:16px; }
[class*="st-key-card_ok"]{ box-shadow:6px 6px 0 var(--blue); }
[class*="st-key-card_review"]{ box-shadow:6px 6px 0 #E4C465; }
[class*="st-key-card_missing"]{ box-shadow:6px 6px 0 #E2A493; }

/* 票面扫描框：裁切图上跑克莱因蓝扫描光带 */
.scanframe{ position:relative; overflow:hidden; border:1.5px solid var(--ink); background:#fff; }
.scanframe img{ width:100%; display:block; }
.scanframe::after{ content:""; position:absolute; left:0; right:0; height:34px; top:-40px;
    background:linear-gradient(180deg,transparent,rgba(20,54,245,.20),transparent);
    animation:scanbeam 3.2s linear infinite; }
@keyframes scanbeam{ to{ top:112%; } }
@media (prefers-reduced-motion: reduce){ .scanframe::after{ animation:none; } }

/* 终端状态标签 */
.ttag{ font-family:var(--mono); font-size:11px; letter-spacing:.14em; font-weight:700;
       padding:3px 10px; border:1.5px solid var(--ink); display:inline-block; }
.ttag.ok{ background:var(--blue); color:#fff; border-color:var(--blue); }
.ttag.review{ color:var(--amber); border-color:var(--amber); }
.ttag.missing{ color:var(--red); border-color:var(--red); }
.swx-src{ font-family:var(--mono); font-size:10.5px; color:var(--grey); letter-spacing:.04em; }
.swx-guess{ font-family:var(--mono); font-size:11px; color:var(--blue); }

/* 翻页进度条：一格一张票 */
.swx-pos{ font-family:var(--mono); font-size:12px; letter-spacing:.14em; color:var(--ink);
          font-weight:700; }
.swx-strip{ display:flex; flex-wrap:wrap; gap:4px; margin:6px 0 14px; }
.swx-strip i{ width:18px; height:10px; border:1.5px solid var(--ink); display:block; background:#fff; }
.swx-strip i.done{ background:var(--blue); border-color:var(--blue); }
.swx-strip i.cur{ background:var(--ink); }
.swx-done{ border:1.5px solid var(--blue); background:#fff; box-shadow:6px 6px 0 var(--blue);
           padding:26px; text-align:center; font-weight:800; font-size:22px; color:var(--blue);
           letter-spacing:.04em; margin:8px 0 16px; font-family:var(--mono); }

/* 输入控件：方角黑边、等宽数字 */
div[data-baseweb="input"], div[data-baseweb="base-input"]{
    border-radius:0 !important; border-color:var(--ink) !important; }
div[data-baseweb="input"] input{
    font-family:var(--mono) !important; font-variant-numeric:tabular-nums; font-weight:600; }

/* 按钮：克莱因蓝方块 + 硬阴影 */
.stButton button, .stDownloadButton button{
    border-radius:0 !important; border:1.5px solid var(--ink) !important;
    font-weight:700; letter-spacing:.04em; }
.stButton button[kind="primary"], .stDownloadButton button{
    background:var(--blue) !important; color:#fff !important; border-color:var(--blue) !important;
    box-shadow:4px 4px 0 var(--ink); transition:transform .06s, box-shadow .06s; }
.stButton button[kind="primary"]:hover, .stDownloadButton button:hover{
    transform:translate(-1px,-1px); box-shadow:5px 5px 0 var(--ink); }
.stButton button[kind="primary"]:active, .stDownloadButton button:active{
    transform:translate(2px,2px); box-shadow:1px 1px 0 var(--ink); }

/* 上传区：黑色虚线方框 */
[data-testid="stFileUploaderDropzone"]{
    border:1.5px dashed var(--ink) !important; border-radius:0 !important; background:#fff !important; }

/* 侧栏 */
[data-testid="stSidebar"]{ background:#fff; border-right:1.5px solid var(--ink); }

/* 提示与折叠面板方角化 */
[data-testid="stAlert"], [data-testid="stExpander"] details{ border-radius:0 !important; }
</style>
"""
st.markdown(SWX_CSS, unsafe_allow_html=True)


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
    st.markdown(f'<div class="swx-sec">{t("sec_recognize")}</div>', unsafe_allow_html=True)
    quality = st.radio(t("quality"), ["fine", "fast"],
                       format_func=lambda q: t("q_fine") if q == "fine" else t("q_fast"),
                       key="quality", help=t("q_help"))
    dpi = st.slider(t("dpi"), 150, 400,
                    int(cfg.get("ocr", {}).get("pdf_render_dpi", 200)), 50)
    st.markdown(f'<div class="swx-sec">{t("sec_parse")}</div>', unsafe_allow_html=True)
    mode = st.radio(t("parse_mode"), ["all", "min"],
                    format_func=lambda m: t("mode_all") if m == "all" else t("mode_min"),
                    key="parse_mode")
    date_order = st.radio(t("date_order"), ["dmy", "mdy"],
                          format_func=lambda o: t(o), key="date_order")
    dedup_on = st.checkbox(t("dedup"), value=True)
    st.divider()
    st.markdown(f'<div class="swx-sec">{t("sec_template")}</div>', unsafe_allow_html=True)
    tpl_file = st.file_uploader(t("upload_tpl"), type=["xlsx"])
    if tpl_file is None and not (ROOT / cfg["template"]["path"]).exists():
        st.warning(t("tpl_missing"))
    else:
        st.caption(t("tpl_hint"))


# ----------------------------------------------------------------- 顶部（品牌栏 + 流程步骤）
_recs0 = st.session_state.get("records")
if not _recs0:
    _cur_step = 1
else:
    _store0 = st.session_state.get("review") or {}
    _all_done = bool(_store0) and all(v["confirmed"] for v in _store0.values())
    _cur_step = 4 if (_all_done
                      or st.session_state.get("idx", 0) >= len(_recs0)) else 3
_steps_html = "".join(
    f'<span class="swx-step {"cur" if s == _cur_step else ("done" if s < _cur_step else "")}">'
    f'<b>0{s}</b>{t("step" + str(s))}</span>'
    for s in (1, 2, 3, 4))
st.markdown(
    f'''<div class="swx-mast">
          <div>
            <div class="t">INVOICE<span class="mono">/AUDIT_</span></div>
            <div class="sub">{t("sub")}</div>
          </div>
          <div class="swx-steps">{_steps_html}</div>
        </div>''', unsafe_allow_html=True)

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
    run_cfg["segment"] = dict(run_cfg.get("segment", {}) or {},
                              deskew_reocr=(quality == "fine"))

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
    # 清掉上一批的确认状态（进度存储 + 翻页控件）
    import re as _re
    for k in list(st.session_state.keys()):
        if _re.match(r"^pg_(dt|am)_\d+$", k) or k in ("review", "review_n", "idx", "jump_box"):
            del st.session_state[k]
    st.session_state["records"] = records
    st.session_state["logs"] = logs
    st.session_state["tpl_bytes"] = tpl_file.getvalue() if tpl_file else None


# ----------------------------------------------------------------- 逐张确认（翻页向导）
# 分页后未渲染的控件状态会被 Streamlit 回收，故用 session_state["review"]
# 作为进度真值源：{i: {date, amount, confirmed}}；控件只是当前页的编辑面。
def crop_bytes(path):
    return Path(path).read_bytes() if path and Path(path).exists() else None


_TAG_CODE = {"ok": "[OK]", "review": "[??]", "missing": "[!!]"}


def _save_current(i: int):
    """把当前页控件的编辑值写回进度存储。"""
    store = st.session_state["review"]
    if f"pg_dt_{i}" in st.session_state:
        store[i]["date"] = (st.session_state[f"pg_dt_{i}"] or "").strip()
    if f"pg_am_{i}" in st.session_state:
        a = st.session_state[f"pg_am_{i}"]
        store[i]["amount"] = None if a is None else float(a)


def _fill_date(i: int, val: str):
    """候选日期一键填入：写进度存储并清掉控件旧值，让输入框重新取值。"""
    st.session_state["review"][i]["date"] = val
    st.session_state.pop(f"pg_dt_{i}", None)


def _date_suggestions(i: int) -> list:
    """当前票的候选日期：本票的其他日期解释 > 同页多数日期 > 本批多数日期。

    专为票面日期被涂抹/褪色的场景准备——点一下即可填入，免手敲。
    """
    recs = st.session_state.get("records") or []
    r = recs[i]
    out: list = []

    def add(d):
        if d and d not in out:
            out.append(d)

    for c in (r.get("date_candidates") or []):
        add(c)
    pg = pipeline._page_of(r)
    if pg is not None:
        same = [x["invoice_date"] for x in recs
                if x is not r and x.get("invoice_date") and pipeline._page_of(x) == pg]
        for d in sorted(set(same), key=same.count, reverse=True):
            add(d)
    alldates = [x["invoice_date"] for x in recs if x.get("invoice_date")]
    if alldates:
        add(max(set(alldates), key=alldates.count))
    return out[:3]


def render_wizard_card(i: int, r: dict, mode: str, store: dict):
    status = r.get("_status", "review")
    if store[i]["confirmed"]:
        tag_html = f'<span class="ttag ok">{t("confirmed_badge")}</span>'
    else:
        badge = {"ok": t("st_ok"), "review": t("st_review"),
                 "missing": t("st_missing")}[status]
        tag_html = f'<span class="ttag {status}">{_TAG_CODE[status]} {badge}</span>'
    with st.container(border=False, key=f"wiz_{status}"):
        c_img, c_val = st.columns([2, 3])
        with c_img:
            b = crop_bytes(r.get("_crop_path"))
            if b:
                b64 = base64.b64encode(b).decode()
                st.markdown(
                    f'<div class="scanframe"><img src="data:image/jpeg;base64,{b64}"/></div>',
                    unsafe_allow_html=True)
            st.markdown(f'<span class="swx-src">{r.get("source_file", "")}</span>',
                        unsafe_allow_html=True)
        with c_val:
            st.markdown(
                tag_html + f'&nbsp;<span class="swx-src">conf={r.get("confidence", "")}</span>',
                unsafe_allow_html=True)
            f1, f2 = st.columns(2)
            f1.text_input(t("col_date"), value=store[i]["date"],
                          placeholder="YYYY-MM-DD", key=f"pg_dt_{i}")
            if r.get("_date_guessed"):
                f1.markdown(
                    f'<span class="swx-guess">↖ {t("guess_" + (r.get("_date_guess_src") or "batch"))}</span>',
                    unsafe_allow_html=True)
            # 候选日期快捷键：日期缺失/靠猜测时给一键填入（涂抹/褪色票专用）
            cur_val = (st.session_state.get(f"pg_dt_{i}", store[i]["date"]) or "").strip()
            if not store[i]["confirmed"] and (r.get("_date_guessed") or not cur_val):
                sugg = [d for d in _date_suggestions(i) if d != cur_val]
                if sugg:
                    st.markdown(f'<span class="swx-src">{t("quick_dates")}</span>',
                                unsafe_allow_html=True)
                    ccols = st.columns(max(len(sugg), 1))
                    for k, dv in enumerate(sugg):
                        ccols[k].button(dv, key=f"chip_{i}_{k}",
                                        on_click=_fill_date, args=(i, dv),
                                        width="stretch")
            amt = store[i]["amount"]
            f2.number_input(t("col_amount"), value=(None if amt is None else float(amt)),
                            step=1.0, format="%.2f", key=f"pg_am_{i}")
            if mode == "all":
                meta = " · ".join(x for x in [
                    kind_label(r.get("doc_kind")), r.get("invoice_type") or "",
                    r.get("currency") or "",
                    f'{t("col_subtotal")} {r["subtotal"]}' if r.get("subtotal") is not None else "",
                    f'{t("col_tax")} {r["tax"]}' if r.get("tax") is not None else "",
                    f'{t("col_tip")} {r["tip"]}' if r.get("tip") is not None else "",
                ] if x)
                st.markdown(f'<span class="swx-src">{meta}</span>', unsafe_allow_html=True)
                if r.get("notes"):
                    st.caption(r["notes"])


records = st.session_state.get("records")
if records:
    mode = st.session_state.get("parse_mode", "all")
    n = len(records)

    # 初始化进度存储（识别默认值 → 真值源），从第一张未确认的开始
    if st.session_state.get("review_n") != n or "review" not in st.session_state:
        st.session_state["review"] = {
            i: {"date": r.get("_date_default", "") or "",
                "amount": r.get("_amount_default"),
                "confirmed": bool(r.get("_confirm_default", False))}
            for i, r in enumerate(records)}
        st.session_state["review_n"] = n
        st.session_state["idx"] = next(
            (i for i in range(n) if not st.session_state["review"][i]["confirmed"]), n)
    store = st.session_state["review"]
    idx = min(max(int(st.session_state.get("idx", 0)), 0), n)   # idx==n 表示全部走完

    n_confirmed = sum(1 for v in store.values() if v["confirmed"])
    st.markdown(
        f'''<div class="swx-metrics">
              <div class="swx-m"><b>{n}</b><span>{t("m_total")}</span></div>
              <div class="swx-m blue"><b>{n_confirmed}</b><span>{t("m_confirmed")}</span></div>
              <div class="swx-m"><b>{n - n_confirmed}</b><span>{t("m_left")}</span></div>
            </div>''', unsafe_allow_html=True)

    # 位置行 + 进度条（蓝=已确认，黑=当前，白=待处理）
    pos_l, pos_r = st.columns([5, 1])
    pos_l.markdown(
        f'<div class="swx-pos">{t("pos", i=min(idx + 1, n), n=n)}</div>',
        unsafe_allow_html=True)

    def _jump(cur=idx):
        if cur < n:
            _save_current(cur)
        st.session_state["idx"] = int(st.session_state["jump_box"]) - 1

    pos_r.number_input("jump", min_value=1, max_value=n,
                       value=min(idx + 1, n), key="jump_box",
                       on_change=_jump, label_visibility="collapsed")

    strip = "".join(
        f'<i class="{"cur" if i == idx else ("done" if store[i]["confirmed"] else "")}"></i>'
        for i in range(n))
    st.markdown(f'<div class="swx-strip">{strip}</div>', unsafe_allow_html=True)

    if idx >= n:
        # 完成态
        st.markdown(f'<div class="swx-done">✓ {t("done_all")}_</div>',
                    unsafe_allow_html=True)
        if st.button(t("back_first")):
            st.session_state["idx"] = 0
            st.rerun()
    else:
        st.caption(t("review_help"))
        render_wizard_card(idx, records[idx], mode, store)

        b_prev, b_skip, b_ok = st.columns([1, 1, 2])
        if b_prev.button(t("btn_prev"), disabled=idx == 0, width="stretch"):
            _save_current(idx)
            st.session_state["idx"] = idx - 1
            st.rerun()
        if b_skip.button(t("btn_skip"), width="stretch"):
            _save_current(idx)
            st.session_state["idx"] = idx + 1
            st.rerun()
        if b_ok.button(t("btn_confirm_next"), type="primary", width="stretch"):
            _save_current(idx)
            store[idx]["confirmed"] = True
            # 跳到下一张未确认的（先向后找，再从头找），没有则进完成态
            nxt = next((j for j in range(idx + 1, n) if not store[j]["confirmed"]),
                       next((j for j in range(n) if not store[j]["confirmed"]), n))
            st.session_state["idx"] = nxt
            st.rerun()

    # ----------------- 导出（读进度存储，不依赖控件状态） -----------------
    only_conf = st.checkbox(t("only_confirmed"), value=False)
    export, confirmed = [], 0
    for i in range(n):
        v = store[i]
        d = (v["date"] or "").strip() if i != idx else \
            (st.session_state.get(f"pg_dt_{i}", v["date"]) or "").strip()
        a = v["amount"] if i != idx else st.session_state.get(f"pg_am_{i}", v["amount"])
        if v["confirmed"]:
            confirmed += 1
        if only_conf and not v["confirmed"]:
            continue
        if a is None and not d:
            continue
        export.append({"invoice_date": d or None,
                       "total_incl_tax": None if a is None else float(a)})

    st.caption(t("confirmed_count", c=confirmed, t=n))
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
    st.markdown(
        f'''<div class="swx-hero">
              <div class="ht">{t("hero_title")}</div>
              <div class="cols">
                <div class="c"><span class="n">01</span>
                  <div class="ct">{t("hero_1t")}</div><div class="cd">{t("hero_1d")}</div></div>
                <div class="c"><span class="n">02</span>
                  <div class="ct">{t("hero_2t")}</div><div class="cd">{t("hero_2d")}</div></div>
                <div class="c"><span class="n">03</span>
                  <div class="ct">{t("hero_3t")}</div><div class="cd">{t("hero_3d")}</div></div>
              </div>
            </div>''', unsafe_allow_html=True)

st.markdown(
    f'''<div class="swx-foot">
          <span>{t("foot_l")}</span><span>{t("foot_r")}</span>
        </div>''', unsafe_allow_html=True)
