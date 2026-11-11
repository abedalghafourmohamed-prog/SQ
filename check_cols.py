import pandas as pd
from pathlib import Path
from collections import defaultdict


FOLDER = Path(r"C:\Users\Lyn\Downloads\qs")


# ============================================================
# 1. Collect columns for EACH FILE
#    All sheets inside the same file are merged together
# ============================================================

files_columns = {}

for file in FOLDER.glob("*.xlsx"):

    print(f"Reading: {file.name}")

    try:
        sheets = pd.read_excel(file, sheet_name=None)

        columns = set()

        for sheet_name, df in sheets.items():

            sheet_columns = {
                str(col).strip()
                for col in df.columns
            }

            columns.update(sheet_columns)

        files_columns[file.name] = columns

    except Exception as e:
        print(f"ERROR: {file.name} -> {e}")


# ============================================================
# 2. For every column, store WHICH FILES contain it
# ============================================================

column_files = defaultdict(set)

for file_name, columns in files_columns.items():

    for column in columns:
        column_files[column].add(file_name)


# ============================================================
# 3. Common columns
#    Column exists in EVERY FILE
# ============================================================

total_files = len(files_columns)

common_columns = sorted(
    column
    for column, file_names in column_files.items()
    if len(file_names) == total_files
)


# ============================================================
# 4. Unique columns per FILE
#    Column exists in THIS FILE only
# ============================================================

unique_columns = {}

for file_name, columns in files_columns.items():

    unique_columns[file_name] = sorted(
        column
        for column in columns
        if len(column_files[column]) == 1
        and file_name in column_files[column]
    )


# ============================================================
# 5. FINAL RESULT
# ============================================================

print("\n" + "=" * 80)
print("COMMON COLUMNS")
print("=" * 80)

for column in common_columns:
    print(f"- {column}")


print("\n" + "=" * 80)
print("UNIQUE COLUMNS PER FILE")
print("=" * 80)

for file_name, columns in unique_columns.items():

    print(f"\n{file_name}:")

    if columns:
        for column in columns:
            print(f"- {column}")
    else:
        print("- None")


# ============================================================
# 6. OPTIONAL CHECK
# ============================================================

print("\n" + "=" * 80)
print("CHECK")
print("=" * 80)
