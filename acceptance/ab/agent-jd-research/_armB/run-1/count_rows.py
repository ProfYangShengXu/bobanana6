with open('output/jd-matrix.md', 'r') as f:
    lines = f.readlines()

# 数数据行（以| 开头的行，排除表头）
data_rows = 0
for i, line in enumerate(lines):
    if line.startswith('| ') and not '###' in line and '##' not in line:
        data_rows += 1

print(f"Total data rows (companies): {data_rows}")

print(f"\nFirst 20 lines (0-indexed):")
for i in range(min(20, len(lines))):
    print(f"{i}: {lines[i][:80]}")