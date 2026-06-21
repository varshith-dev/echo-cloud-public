import re

with open('app.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if 'c.execute' in line and '"' in line:
        print(f"Line {i+1}: {line.strip()}")
