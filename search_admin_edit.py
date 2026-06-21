with open('dashboard/static/admin.html', 'r', encoding='utf-8') as f:
    lines = f.readlines()

for i, line in enumerate(lines):
    if 'editTenant' in line or 'openEdit' in line or 'edit-modal' in line or 'showEdit' in line:
        print(f"{i+1}: {line.strip()}")
