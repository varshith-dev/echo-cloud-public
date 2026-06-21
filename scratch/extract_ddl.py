import re

app_path = "dashboard/app.py"
with open(app_path, "r", encoding="utf-8") as f:
    content = f.read()

# Find all blocks of c.execute('''CREATE TABLE ... ''')
pattern = r"c\.execute\(\'\'\'(CREATE TABLE IF NOT EXISTS .*?)\'\'\'\)"
matches = re.findall(pattern, content, re.DOTALL | re.IGNORECASE)

output = []
for m in matches:
    output.append(m.strip())
    output.append("-" * 50)

with open("scratch/schema.txt", "w", encoding="utf-8") as f:
    f.write("\n".join(output))

print(f"Extracted {len(matches)} schemas to scratch/schema.txt")
