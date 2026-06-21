import re

html_path = "dashboard/static/photos.html"
with open(html_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

functions = []
for i, line in enumerate(lines):
    line_num = i + 1
    stripped = line.strip()
    match = re.search(r"function\s+(\w+)\s*\(", stripped)
    if match:
        functions.append((line_num, match.group(0) + " ..."))
    else:
        match2 = re.search(r"(?:const|let|var)\s+(\w+)\s*=\s*(?:async\s*)?\([^)]*\)\s*=>", stripped)
        if match2:
            functions.append((line_num, match2.group(0) + " ..."))

print(f"Found {len(functions)} Javascript declarations in photos.html:")
for num, decl in functions:
    print(f"L{num}: {decl}")
