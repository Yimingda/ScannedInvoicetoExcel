"""InvoiceScanner 命令行入口。

用法：
    py -m invoicescanner            # 用 config.yaml，处理 input/ 下全部发票
    或  .venv\\Scripts\\python.exe main.py
    可选参数：
        --config PATH   指定配置文件（默认 config.yaml）
        --input  PATH   覆盖输入目录
        --output PATH   覆盖输出 Excel 路径
        --single PATH   只处理单个文件，结果打印到屏幕（调试用）
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

import yaml

from invoicescanner import pipeline, excel_writer


def load_config(path: Path) -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main(argv=None):
    ap = argparse.ArgumentParser(description="本地识别扫描发票的日期与带税总金额并写入 Excel 模板")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--input")
    ap.add_argument("--output")
    ap.add_argument("--single")
    args = ap.parse_args(argv)

    cfg = load_config(Path(args.config))
    if args.input:
        cfg["input_dir"] = args.input

    if args.single:
        recs = pipeline.process_file(Path(args.single), cfg)
        for i, rec in enumerate(recs, 1):
            print(f"\n=== 解析结果 {i}/{len(recs)} ===")
            for k, v in rec.items():
                print(f"  {k}: {v}")
        return 0

    records = pipeline.process_all(cfg)
    if not records:
        return 1

    out_dir = Path(cfg.get("output_dir", "output"))
    out_path = Path(args.output) if args.output else out_dir / "invoices_result.xlsx"
    excel_writer.write_records(records, cfg["template"], out_path)
    print(f"\n[OK] 已写入 {len(records)} 条记录 -> {out_path.resolve()}")
    print(f"     模板: {Path(cfg['template']['path']).resolve()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
