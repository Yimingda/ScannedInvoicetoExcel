# InvoiceScanner —— 扫描发票日期/带税总金额提取工具

本地(离线)识别扫描发票中的**日期**与**带税总金额**（含税、含小费的最终应付金额，而非税前小计），
并把结果写入你指定的 Excel 模板。

## 能力

- **输入**：图片 `jpg/png/bmp/tiff/webp`、扫描件 PDF、电子 PDF（含文字层时直接提取，更快更准）。
- **OCR**：RapidOCR（PaddleOCR 同款 PP-OCR 中英文模型，跑在 ONNX Runtime 上，纯本地、无需联网、无需 API）。
- **发票类型**：西式餐饮小票（英文 subtotal/tax/tip/total）、中国增值税发票（价税合计）、及混合。
- **带税总额判定**：
  - 增值税发票 → 直接取「价税合计」。
  - 西式小票 → 优先选 `≈ 小计+税+小费` 的 total 候选（校验通过=高置信度）；否则按关键词优先级 + 金额大小选定，并校验「不小于小计/税额」，避免误取税前或不含小费的金额。
- **一页多票 / 发票+回执叠贴**：扫描页上贴多张小票时，先在图像上按墨迹连通块把每张切开，
  再逐票 OCR；同区仍含多张正式发票的，按发票头二次拆分。
- **输出**：按 `config.yaml` 的列映射写入 Excel 模板；另出一份**复核表**（红=需重点核对）。
- **不遗漏、不重复**：
  - 每个输入文件必有着落——解析失败的也占一行（备注写明原因），不会被静默跳过。
  - 自动识别指向同一笔交易的多张单据（如 **发票 + 刷卡小票**）：金额精确相等且日期相差
    ≤3 天（可配置）→ 合并为一条，正式发票优先做主记录，小票文件名并入备注。
  - 金额相同但缺日期、无法确认的 → **不自动合并**，双方备注标「疑似重复」交人工复核。
  - 宁可漏合并、不错合并：金额差 1 分钱或日期差超容差都不会合并。

## 两种用法

| 方式 | 适合 | 入口 |
|---|---|---|
| **网页界面**（推荐） | 上传→识别→对着原图在线改→下载 | `streamlit run app.py` |
| 命令行批处理 | 放一批文件到 `input/` 一次跑完 | `python finalize.py` |

## 目录结构

```
InvoiceScanner/
├─ app.py                 # ★ Streamlit 网页界面
├─ finalize.py            # 命令行批处理（出「导入表」+「复核表」）
├─ main.py                # 命令行（只出导入表）
├─ config.yaml            # 配置：OCR/模板列映射/关键词/去重
├─ requirements.txt        # Python 依赖
├─ packages.txt           # libgl1 + libglib2.0-0t64（完整 opencv 需 libGL 与 glib）
├─ .streamlit/config.toml # 网页上传大小/主题
├─ invoicescanner/        # 引擎包（loader/ocr/parse/segment/dedup/excel_writer/pipeline）
├─ input/ output/ templates/ samples/
```

## 网页界面

```powershell
.venv\Scripts\python.exe -m streamlit run app.py
```

浏览器打开 `http://localhost:8501`：拖入发票 → 点「开始识别」→ **逐张确认卡片**（左边票面裁切图、
右边解析出的日期/金额）→ 点「下载报销导入表」。

界面特性：
- **中英双语**：左侧栏一键切换（语言 / Language）。
- **两种解析模式**：`全部信息`（含币种/税/小费/类型等）或 `仅日期 + 金额`（卡片更精简）。
- **逐张确认**：🟢 较确信的**默认已勾选「确认」**；🟡 待核对；🔴 缺字段。
  **空白字段按上下文自动猜测预填**——如某票日期缺失，用同页其它票的多数日期、
  或该票的另一种日期解释、或全批多数日期作为默认值，你核对无误勾「确认」即可。
- 左侧栏还可调 DPI、歧义日期读法、去重开关，并上传你自己的 Excel 模板。

### 部署到 Streamlit Community Cloud

1. 把本项目推到 GitHub。
2. 在 https://share.streamlit.io 新建 App，主文件选 `app.py`。
3. 依赖自动读 `requirements.txt` 与 `packages.txt`。**glib 用 trixie 的新名
   `libglib2.0-0t64`**，切勿用旧名 `libglib2.0-0`（会拉 bullseye 版、依赖 `libffi7`
   装不上，导致 apt 失败）。首次启动会下载 OCR 模型，稍慢。

> 注意：Community Cloud 免费额度内存约 1GB，OCR 大批量/高 DPI 可能吃紧；
> 自有服务器（`streamlit run app.py --server.port 80`）或内网部署更稳。

## 安装（已在本机 .venv 装好，重装时用）

```powershell
py -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 使用

1. 把发票文件放进 `input/`。
2. （可选）把你的模板放到 `templates/template.xlsx`，并在 `config.yaml` 的 `template.columns` 里把
   每个字段对应到模板里的列字母、设好 `sheet` / `start_row`。
   > 若该模板文件不存在，程序会自动生成一个带中文表头的默认模板。
3. 运行：

```powershell
.venv\Scripts\python.exe main.py
```

结果写入 `output/invoices_result.xlsx`。

### 常用参数

```powershell
# 只处理单个文件并把解析结果打印到屏幕（调试/核对用）
.venv\Scripts\python.exe main.py --single samples\receipt_western.png

# 覆盖输入目录 / 输出文件
.venv\Scripts\python.exe main.py --input D:\发票 --output D:\结果.xlsx
```

## 接入你自己的模板

打开 `config.yaml` 的 `template` 段：

```yaml
template:
  path: templates/template.xlsx   # 指向你的模板文件
  sheet: Sheet1                   # 要写入的工作表
  header_row: 1
  start_row: 2                    # 第一条数据写入的行
  columns:                        # 字段 -> 模板里的列字母
    invoice_date: B
    total_incl_tax: C
    ...
```

只需保证 `columns` 里字段名对应到模板正确的列即可，模板原有的表头、格式、公式都会保留。

可写入的字段：`source_file, invoice_date, total_incl_tax, currency, subtotal, tax, tip,
invoice_type, confidence, notes`。不需要的字段从 `columns` 删掉即可。

## 去重设置

`config.yaml` 的 `dedup` 段：

```yaml
dedup:
  enabled: true
  mode: skip               # skip=重复票并入主记录备注(金额列可直接求和)；mark=照常写入但标记
  date_tolerance_days: 3   # 开票日期常比刷卡晚几天，按需调整
```

注意：判定依据是「金额+日期」，若同一天恰有两笔**金额完全相同**的不同交易，会被合并——
这种情况备注里会列出被合并的文件名，人工瞄一眼即可发现。拿不准的一律不合并、只标「疑似」。

## 一次产出两个文件（推荐用法）

```powershell
.venv\Scripts\python.exe finalize.py
```

- `output/报销导入_已填.xlsx` —— 只填模板的 B/C/K 列，可直接上传报销系统。
- `output/复核表.xlsx` —— 每条记录的全部字段+置信度+备注，**并在 L 列内嵌该张小票的裁切原图**，
  可对着图直接改数字；**红色行**（低置信度/缺日期/缺金额）重点核对，**黄色行**扫一眼。

## 实测准确率（2026-03 南非餐饮/高尔夫票据，5 页 16 张，含叠压/倾斜/手写批注）

| 模式 | 带税金额 | 日期 | 多余记录 | 速度 |
|---|---|---|---|---|
| **精细**（默认，摆正+放大重OCR） | **16/16** | 13/16 | 0 | 基准 |
| 快速（跳过重OCR） | 15/16 | — | 2 | 约 2× |

- 核心算法：整页 OCR 行按「固定宽度旋转矩形」先验做三阶段聚类，每张小票摆正后放大重识别；
  不适用的版式（A4 宽幅表格等）自动回退旧的墨迹投影算法。
- 3 个未中的日期均为票面物理损毁（涂抹/褪色），界面会按同页/本批日期预填候选、一键填入。
- 发票+刷卡回执自动合并取实付（含小费）；日期差 1~3 天的对子标「疑似同单」交人工。

## 提升准确率的建议

- 扫描件尽量清晰、摆正、**小票之间不要叠压**；`config.yaml` 里 `ocr.pdf_render_dpi` 可调高（如 300）。
- 遇到你的票据用词特殊（例如把总额写成 "Amount Payable"），把该词加进
  `config.yaml` 的 `keywords.total` 列表即可，无需改代码。
- `置信度=low/medium` 的记录建议人工复核，`备注` 列会给出候选与校验信息。
