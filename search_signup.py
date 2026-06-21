with open('dashboard/app.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if '/signup' in line or 'INSERT INTO tenants' in line or 'def signup' in line:
        print(f"{i+1}: {line.strip()}")
