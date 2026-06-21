import difflib

with open("dashboard/app.py", "r", encoding="utf-8") as f:
    local_lines = f.readlines()

with open("dashboard/app_remote.py", "r", encoding="utf-8") as f:
    remote_lines = f.readlines()

diff = list(difflib.unified_diff(
    remote_lines, local_lines,
    fromfile='remote_app.py', tofile='local_app.py',
    n=3
))

print(f"Diff contains {len(diff)} lines:")
for line in diff[:100]:
    print(line, end='')
if len(diff) > 100:
    print(f"\n... and {len(diff) - 100} more lines.")
