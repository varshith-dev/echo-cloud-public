with open('dashboard/static/admin.html', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if 'coupon' in line.lower():
        print(f"{i+1}: {line.strip()}")
