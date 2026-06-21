with open('dashboard/static/index.html', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if 'billing' in line.lower() or 'checkout' in line.lower():
        print(f"{i+1}: {line.strip()}")
