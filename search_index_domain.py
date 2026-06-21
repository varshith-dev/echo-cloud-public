with open('dashboard/static/index.html', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if 'custom_domain' in line or 'custom-domain' in line or 'Custom Domain Mapping' in line:
        print(f"{i+1}: {line.strip()}")
