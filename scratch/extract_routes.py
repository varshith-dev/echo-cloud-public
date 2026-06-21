app_path = "dashboard/app.py"
functions = []

with open(app_path, "r", encoding="utf-8") as f:
    lines = f.readlines()

for i, line in enumerate(lines[:1052]):
    line_num = i + 1
    stripped = line.strip()
    if stripped.startswith("def "):
        func_name = stripped.split("(")[0].replace("def ", "")
        functions.append((line_num, stripped))

print(f"Found {len(functions)} functions before line 1053:")
for num, decl in functions:
    print(f"L{num}: {decl}")
