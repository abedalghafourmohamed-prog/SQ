"""
compare_qatarsale_columns.py

Purpose:
  You have a QatarSale archive structured like this (Hive-style partitioning):

      <root>/year=YYYY/month=MM/day=DD/<category>/excel/*.xlsx

  Inside each "excel/" folder, there may be:
    - One file with one sheet
    - One file with multiple sheets
    - Multiple files (each one may contain multiple sheets)

  This script goes through all available scraping days, and for each category,
  collects the columns that appeared on each day (union across all files and
  sheets for that day), then calculates:

    - core_columns    : columns that appeared on *every* day (consistent/stable)
    - variable_columns: columns that appeared only on some days (variable)

  It generates an Excel report containing:
    1) Summary            -> For each category: number of days, core count,
                             variable count
    2) Per-Category       -> One sheet per category with column details
                             (core / variable, and for variable columns,
                             how many days out of the total they appeared)
    3) Similar Categories -> Groups categories that have exactly the same
                             core columns (or are similar based on a
                             configurable similarity ratio)

Usage:
    python compare_qatarsale_columns.py /path/to/qatarsale_root -o report.xlsx

    If the data is still on Cloudflare R2 and you want to download it locally
    first, sync it using a tool such as rclone or aws-cli (with the R2 endpoint)
    to a local folder, then run the script on that folder.

Notes:
    - The script reads only the header row from each sheet for speed,
      not the entire dataset.
    - If an "excel" folder does not exist inside a category folder for a
      specific day, that category is considered not scraped on that day
      (it is not counted for core/variable calculations).
    - Any corrupted or locked file is recorded in the "Errors" sheet
      instead of stopping the script.
"""

import argparse
import sys
from pathlib import Path
from collections import defaultdict

import pandas as pd
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

FONT = "Arial"
HEADER_FONT = Font(name=FONT, bold=True, color="FFFFFF", size=11)
HEADER_FILL = PatternFill("solid", fgColor="2F5597")
CORE_FILL = PatternFill("solid", fgColor="C6EFCE")     # green
VARIABLE_FILL = PatternFill("solid", fgColor="FFEB9C") # yellow
ERROR_FILL = PatternFill("solid", fgColor="FFC7CE")    # red
NORMAL_FONT = Font(name=FONT, size=10)
BOLD_FONT = Font(name=FONT, size=10, bold=True)
TITLE_FONT = Font(name=FONT, size=14, bold=True)
THIN = Side(style="thin", color="B7B7B7")
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)


def find_excel_files(category_dir: Path):
    excel_dir = category_dir / "excel"
    if not excel_dir.exists():
        return []
    files = list(excel_dir.glob("*.xlsx")) + list(excel_dir.glob("*.xls"))
    return sorted(files)


def get_columns_from_file(path: Path, errors: list):
    """Return the set of column names across every sheet in one excel file (header row only)."""
    cols = set()
    try:
        xls = pd.ExcelFile(path)
        for sheet in xls.sheet_names:
            try:
                df = xls.parse(sheet, nrows=0)  # header only, fast
                cols.update(str(c).strip() for c in df.columns)
            except Exception as e:
                errors.append((str(path), sheet, str(e)))
    except Exception as e:
        errors.append((str(path), "-", str(e)))
    return cols


def scan_archive(root: Path):
    """
    Returns:
      data:   {category: {day_key: set(columns)}}
      errors: [(file_path, sheet, error_message)]
    """
    data = defaultdict(dict)
    errors = []

    day_dirs = sorted(root.glob("year=*/month=*/day=*"))
    if not day_dirs:
        print(f"!! No year=*/month=*/day=* directories found under {root}. Please check the path.")
        return data, errors

    for day_dir in day_dirs:
        parts = day_dir.parts
        try:
            y = next(p for p in parts if p.startswith("year=")).split("=", 1)[1]
            m = next(p for p in parts if p.startswith("month=")).split("=", 1)[1]
            d = next(p for p in parts if p.startswith("day=")).split("=", 1)[1]
        except StopIteration:
            continue
        day_key = f"{y}-{m}-{d}"

        for cat_dir in sorted(p for p in day_dir.iterdir() if p.is_dir()):
            category = cat_dir.name
            files = find_excel_files(cat_dir)
            if not files:
                continue
            day_cols = set()
            for f in files:
                day_cols |= get_columns_from_file(f, errors)
            if day_cols:
                data[category][day_key] = day_cols

    return data, errors


def compute_core_variable(data):
    """
    Returns:
      results: {category: {
          "days": [day_key, ...] sorted,
          "core": set,
          "variable": {col: [days it appeared in]},
      }}
    """
    results = {}
    for category, per_day in data.items():
        days_sorted = sorted(per_day.keys())
        day_sets = [per_day[d] for d in days_sorted]
        core = set.intersection(*day_sets) if day_sets else set()
        union = set.union(*day_sets) if day_sets else set()
        variable_cols = union - core
        variable_detail = {
            col: [d for d in days_sorted if col in per_day[d]]
            for col in variable_cols
        }
        results[category] = {
            "days": days_sorted,
            "core": core,
            "variable": variable_detail,
        }
    return results


def group_similar_categories(results, min_overlap_ratio=1.0):
    """
    Groups categories whose CORE column sets are identical (min_overlap_ratio=1.0)
    or similar (Jaccard >= min_overlap_ratio, if you lower it below 1.0).
    Returns a list of groups: [{"categories": [...], "core": set, "jaccard_note": str}]
    """
    cats = list(results.keys())
    used = set()
    groups = []

    for i, c1 in enumerate(cats):
        if c1 in used:
            continue
        core1 = results[c1]["core"]
        group = [c1]
        for c2 in cats[i + 1:]:
            if c2 in used:
                continue
            core2 = results[c2]["core"]
            if not core1 and not core2:
                continue
            union = core1 | core2
            inter = core1 & core2
            jaccard = len(inter) / len(union) if union else 0
            if jaccard >= min_overlap_ratio:
                group.append(c2)
        if len(group) > 1:
            for c in group:
                used.add(c)
            groups.append({"categories": group, "core": core1})
    return groups


def write_report(results, errors, out_path, min_overlap_ratio):
    wb = Workbook()

    # ---------- Summary ----------
    ws = wb.active
    ws.title = "Summary"
    ws["A1"] = "Column Summary for Each Category Across All Scraping Days"
    ws["A1"].font = TITLE_FONT
    ws.merge_cells("A1:E1")
    headers = ["Category", "Available Days", "Core Columns Count",
               "Variable Columns Count", "Sample Variable Columns"]
    for i, h in enumerate(headers, start=1):
        c = ws.cell(row=2, column=i, value=h)
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
        c.alignment = Alignment(horizontal="center", wrap_text=True, vertical="center")

    row = 3
    for category in sorted(results.keys()):
        r = results[category]
        ws.cell(row=row, column=1, value=category).font = BOLD_FONT
        ws.cell(row=row, column=2, value=len(r["days"])).font = NORMAL_FONT
        ws.cell(row=row, column=3, value=len(r["core"])).font = NORMAL_FONT
        ws.cell(row=row, column=4, value=len(r["variable"])).font = NORMAL_FONT
        sample = ", ".join(sorted(r["variable"].keys())[:5])
        ws.cell(row=row, column=5, value=sample).font = NORMAL_FONT
        for col in range(1, 6):
            ws.cell(row=row, column=col).border = BORDER
        row += 1
    ws.column_dimensions["A"].width = 35
    ws.column_dimensions["B"].width = 18
    ws.column_dimensions["C"].width = 22
    ws.column_dimensions["D"].width = 24
    ws.column_dimensions["E"].width = 50
    ws.freeze_panes = "A3"

    # ---------- Per-Category detail (one sheet each, name truncated to 31 chars) ----------
    used_names = set()
    for category in sorted(results.keys()):
        r = results[category]
        sheet_name = category[:31]
        base = sheet_name
        n = 1
        while sheet_name in used_names:
            n += 1
            suffix = f"_{n}"
            sheet_name = base[: 31 - len(suffix)] + suffix
        used_names.add(sheet_name)

        wsx = wb.create_sheet(sheet_name)
        wsx["A1"] = f"Category: {category}  |  Available scraping days: {len(r['days'])}"
        wsx["A1"].font = TITLE_FONT
        wsx.merge_cells("A1:C1")

        wsx["A3"] = "Core columns (present on all days)"
        wsx["A3"].font = BOLD_FONT
        wsx["A3"].fill = CORE_FILL
        row = 4
        for col_name in sorted(r["core"]):
            c = wsx.cell(row=row, column=1, value=col_name)
            c.font = NORMAL_FONT
            c.fill = CORE_FILL
            c.border = BORDER
            row += 1

        start_variable = row + 1
        wsx.cell(row=start_variable, column=1, value="Variable columns (appeared on some days only)").font = BOLD_FONT
        wsx.cell(row=start_variable, column=1).fill = VARIABLE_FILL
        wsx.cell(row=start_variable, column=2, value="Number of Days Present").font = BOLD_FONT
        wsx.cell(row=start_variable, column=2).fill = VARIABLE_FILL
        wsx.cell(row=start_variable, column=3, value="Days (Dates)").font = BOLD_FONT
        wsx.cell(row=start_variable, column=3).fill = VARIABLE_FILL
        row = start_variable + 1
        for col_name in sorted(r["variable"].keys()):
            days_present = r["variable"][col_name]
            c1 = wsx.cell(row=row, column=1, value=col_name)
            c2 = wsx.cell(row=row, column=2, value=f"{len(days_present)}/{len(r['days'])}")
            c3 = wsx.cell(row=row, column=3, value=", ".join(days_present))
            for c in (c1, c2, c3):
                c.font = NORMAL_FONT
                c.fill = VARIABLE_FILL
                c.border = BORDER
            row += 1

        wsx.column_dimensions["A"].width = 32
        wsx.column_dimensions["B"].width = 22
        wsx.column_dimensions["C"].width = 60

    # ---------- Similar Categories ----------
    wsg = wb.create_sheet("Similar Categories")
    note = ("100% identical" if min_overlap_ratio >= 1.0
            else f">= {int(min_overlap_ratio*100)}% similarity")
    wsg["A1"] = f"Groups of Categories with {note} Core Columns"
    wsg["A1"].font = TITLE_FONT
    wsg.merge_cells("A1:C1")
    headers = ["Group #", "Categories", "Shared Core Columns"]
    for i, h in enumerate(headers, start=1):
        c = wsg.cell(row=2, column=i, value=h)
        c.font = HEADER_FONT
        c.fill = HEADER_FILL
        c.alignment = Alignment(horizontal="center", wrap_text=True)

    groups = group_similar_categories(results, min_overlap_ratio)
    row = 3
    if not groups:
        wsg.cell(row=row, column=1, value="No identical/similar groups found with the current criteria").font = NORMAL_FONT
    for idx, g in enumerate(groups, start=1):
        wsg.cell(row=row, column=1, value=idx).font = BOLD_FONT
        wsg.cell(row=row, column=2, value=", ".join(g["categories"])).font = NORMAL_FONT
        wsg.cell(row=row, column=2).alignment = Alignment(wrap_text=True)
        wsg.cell(row=row, column=3, value=", ".join(sorted(g["core"]))).font = NORMAL_FONT
        wsg.cell(row=row, column=3).alignment = Alignment(wrap_text=True)
        for col in range(1, 4):
            wsg.cell(row=row, column=col).border = BORDER
        row += 1
    wsg.column_dimensions["A"].width = 10
    wsg.column_dimensions["B"].width = 45
    wsg.column_dimensions["C"].width = 70

    # ---------- Errors ----------
    if errors:
        wse = wb.create_sheet("Errors")
        wse["A1"] = "Files/Sheets with Reading Errors"
        wse["A1"].font = TITLE_FONT
        wse.merge_cells("A1:C1")
        headers = ["File", "Sheet", "Error"]
        for i, h in enumerate(headers, start=1):
            c = wse.cell(row=2, column=i, value=h)
            c.font = HEADER_FONT
            c.fill = HEADER_FILL
        row = 3
        for f, sheet, msg in errors:
            wse.cell(row=row, column=1, value=f).font = NORMAL_FONT
            wse.cell(row=row, column=2, value=sheet).font = NORMAL_FONT
            wse.cell(row=row, column=3, value=msg).font = NORMAL_FONT
            for col in range(1, 4):
                wse.cell(row=row, column=col).fill = ERROR_FILL
                wse.cell(row=row, column=col).border = BORDER
            row += 1
        wse.column_dimensions["A"].width = 60
        wse.column_dimensions["B"].width = 20
        wse.column_dimensions["C"].width = 60

    wb.save(out_path)
    print(f"✔ Report saved to: {out_path}")


def main():
    parser = argparse.ArgumentParser(description="Compare QatarSale scraped columns across days per category.")
    parser.add_argument("root", help="Path to the main archive folder (containing year=YYYY/...)")
    parser.add_argument("-o", "--output", default="qatarsale_columns_report.xlsx",
                         help="Path to the output Excel file (default: qatarsale_columns_report.xlsx)")
    parser.add_argument("--similarity", type=float, default=1.0,
                         help="Required Jaccard similarity ratio for grouping categories in the "
                              "Similar Categories sheet. 1.0 = exactly identical (default). "
                              "For example, 0.8 = 80%% similarity or higher.")
    args = parser.parse_args()

    root = Path(args.root)
    if not root.exists():
        print(f"!! Path does not exist: {root}")
        sys.exit(1)

    print(f"Scanning: {root} ...")
    data, errors = scan_archive(root)

    if not data:
        print("!! No data found (Excel files). Please check the folder structure.")
        sys.exit(1)

    print(f"Found {len(data)} categories. Computing core/variable columns...")
    results = compute_core_variable(data)

    write_report(results, errors, args.output, args.similarity)

    if errors:
        print(f"⚠ {len(errors)} issues occurred while reading some files. Check the Errors sheet in the report.")


if __name__ == "__main__":
    main()
