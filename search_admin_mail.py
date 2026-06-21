with open('dashboard/app.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if 'def ' in line and ('mail' in line.lower() or 'send_' in line.lower()):
        print(f"{i+1}: {line.strip()}")
