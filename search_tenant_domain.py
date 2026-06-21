with open('dashboard/app.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if '/custom-domain' in line or '/tenant/custom-domain' in line:
        print(f"{i+1}: {line.strip()}")
