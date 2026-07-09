"""InvoiceScanner —— Streamlit 可视化界面。

流程：上传发票 → 一键识别 → 表格里对着小票原图在线修正 → 下载填好的 Excel。

本地运行：
    .venv\\Scripts\\python.exe -m streamlit run app.py
"""
from __future__ import annotations

import base64
import io
import tempfile
from pathlib import Path

import pandas as pd
import streamlit as st
import yaml

from invoicescanner import pipeline, excel_writer

ROOT = Path(__file__).parent
IMG_EXT = {".jpg", ".jpeg", ".png", ".bmp", ".tif", ".tiff", ".webp"}

st.set_page_config(page_title="发票识别 · 带税金额提取", page_icon="🧾",
                   layout="wide")


# ----------------------------------------------------------------- 资源与配置
@st.cache_data
def load_cfg() -> dict:
    return yaml.safe_load(open(ROOT / "config.yaml", encoding="utf-8"))


@st.cache_resource(show_spinner="首次加载 OCR 模型（约 10~20 秒）……")
def warm_engine():
    """预热 RapidOCR 单例，避免第一张图卡顿。"""
    import numpy as np
    from invoicescanner import ocr
    ocr.recognize(np.full((80, 200, 3), 255, dtype=np.uint8))
    return True


def crop_to_datauri(path: str | None) -> str:
    if not path or not Path(path).exists():
        return ""
    b = Path(path).read_bytes()
    return "data:image/jpeg;base64," + base64.b64encode(b).decode()


def records_to_df(records: list[dict]) -> pd.DataFrame:
    rows = []
    for r in records:
        need = (r.get("confidence") == "low" or not r.get("invoice_date")
                or r.get("total_incl_tax") is None)
        rows.append({
            "核对": "⚠️" if need else "✓",
            "原图": crop_to_datauri(r.get("_crop_path")),
            "日期": r.get("invoice_date") or "",
            "金额": r.get("total_incl_tax"),
            "币种": r.get("currency") or "",
            "种类": {"invoice": "发票", "card_slip": "刷卡小票"}.get(r.get("doc_kind"), ""),
            "置信度": r.get("confidence") or "",
            "来源": r.get("source_file", ""),
            "备注": r.get("notes") or "",
        })
    return pd.DataFrame(rows)


# ----------------------------------------------------------------- 侧边栏
cfg = load_cfg()
st.sidebar.title("⚙️ 设置")
dpi = st.sidebar.slider("扫描件渲染 DPI（越高越准越慢）", 150, 400,
                        int(cfg.get("ocr", {}).get("pdf_render_dpi", 200)), 50)
date_order = st.sidebar.radio(
    "歧义日期读法", ["dmy（日/月/年·南非欧洲）", "mdy（月/日/年·美式）"],
    index=0 if cfg.get("parsing", {}).get("date_order", "dmy") == "dmy" else 1)
dedup_on = st.sidebar.checkbox("自动合并 发票+刷卡回执（去重）", value=True)
st.sidebar.markdown("---")
tpl_file = st.sidebar.file_uploader(
    "上传你的 Excel 模板（写入『费用信息』表 B/C=日期、K=金额）", type=["xlsx"])
_default_tpl = (ROOT / cfg["template"]["path"]).exists()
if tpl_file is None and not _default_tpl:
    st.sidebar.warning("未检测到默认模板，未上传时将使用通用表头。"
                       "如需按你公司的报销格式导出，请在上方上传模板。")
else:
    st.sidebar.caption("留空则用默认/通用模板；列映射见 config.yaml。")


# ----------------------------------------------------------------- 主区
st.title("🧾 扫描发票 · 日期与带税金额提取")
st.caption("本地 OCR（RapidOCR），离线运行。上传发票 → 识别 → 对着原图在线修正 → 下载导入表。")

uploads = st.file_uploader(
    "拖入发票文件（图片 / 扫描 PDF / 电子 PDF，可多选）",
    type=["pdf", "jpg", "jpeg", "png", "bmp", "tif", "tiff", "webp"],
    accept_multiple_files=True)

col_run, col_info = st.columns([1, 3])
run = col_run.button("🚀 开始识别", type="primary", disabled=not uploads,
                     width="stretch")
if uploads:
    col_info.info(f"已选择 {len(uploads)} 个文件")

if run:
    warm_engine()
    run_cfg = dict(cfg)
    run_cfg.setdefault("ocr", {})
    run_cfg["ocr"] = dict(run_cfg["ocr"], pdf_render_dpi=dpi)
    run_cfg["parsing"] = dict(run_cfg.get("parsing", {}),
                              date_order="dmy" if date_order.startswith("dmy") else "mdy")
    run_cfg["dedup"] = dict(run_cfg.get("dedup", {}), enabled=dedup_on)

    workdir = Path(tempfile.mkdtemp(prefix="invsc_"))
    run_cfg["_crop_dir"] = str(workdir / "crops")
    paths = []
    for uf in uploads:
        p = workdir / uf.name
        p.write_bytes(uf.getbuffer())
        paths.append(p)

    bar = st.progress(0.0, text="准备中……")

    def on_progress(done, total, name):
        bar.progress(done / max(total, 1),
                     text=f"识别中 {done}/{total} … {name}")

    logs: list[str] = []
    records = pipeline.process_files(paths, run_cfg, log=logs.append,
                                     progress=on_progress)
    bar.progress(1.0, text="完成")
    st.session_state["records"] = records
    st.session_state["logs"] = logs
    st.session_state["tpl_bytes"] = tpl_file.getvalue() if tpl_file else None


# ----------------------------------------------------------------- 结果与编辑
records = st.session_state.get("records")
if records:
    need = sum(1 for r in records
               if r.get("confidence") == "low" or not r.get("invoice_date")
               or r.get("total_incl_tax") is None)
    c1, c2, c3 = st.columns(3)
    c1.metric("识别出记录", len(records))
    c2.metric("需人工核对", need, help="⚠️ 行：低置信度 / 缺日期 / 缺金额")
    c3.metric("可直接用", len(records) - need)

    st.subheader("识别结果（对着原图直接改 日期 / 金额）")
    st.caption("⚠️ 行建议核对。改完下方按钮导出。日期格式 YYYY-MM-DD；删掉整行的金额即可剔除该行。")

    df = records_to_df(records)
    edited = st.data_editor(
        df, width="stretch", hide_index=True, num_rows="dynamic",
        column_config={
            "核对": st.column_config.TextColumn("核对", width="small", disabled=True),
            "原图": st.column_config.ImageColumn("原图（小票裁切）", width="medium"),
            "日期": st.column_config.TextColumn("日期", width="small",
                                              help="YYYY-MM-DD"),
            "金额": st.column_config.NumberColumn("金额", width="small", format="%.2f"),
            "币种": st.column_config.TextColumn("币种", width="small"),
            "种类": st.column_config.TextColumn("种类", width="small", disabled=True),
            "置信度": st.column_config.TextColumn("置信度", width="small", disabled=True),
            "来源": st.column_config.TextColumn("来源", disabled=True),
            "备注": st.column_config.TextColumn("备注", width="large", disabled=True),
        },
        key="editor")

    # 导出：把编辑后的 日期/金额 写进模板
    export_records = []
    for _, row in edited.iterrows():
        amt = row.get("金额")
        date = (row.get("日期") or "").strip()
        if (amt is None or pd.isna(amt)) and not date:
            continue  # 整行空 → 跳过
        export_records.append({
            "invoice_date": date or None,
            "total_incl_tax": None if (amt is None or pd.isna(amt)) else float(amt),
        })

    try:
        wb = excel_writer.build_workbook(
            export_records, cfg["template"],
            template_source=io.BytesIO(st.session_state["tpl_bytes"])
            if st.session_state.get("tpl_bytes") else None)
        buf = io.BytesIO()
        wb.save(buf)
        st.download_button(
            f"⬇️ 下载报销导入表（{len(export_records)} 行已填）",
            data=buf.getvalue(), type="primary",
            file_name="报销导入_已填.xlsx",
            mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    except Exception as e:
        st.error(f"生成 Excel 失败：{e}")

    with st.expander("查看处理日志"):
        st.code("\n".join(st.session_state.get("logs", [])), language="text")
else:
    st.info("👆 上传发票后点「开始识别」。首次识别会先加载 OCR 模型，请稍候。")
