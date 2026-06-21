import ast

try:
    with open('app.py', 'r', encoding='utf-8') as f:
        source = f.read()
    ast.parse(source)
except SyntaxError as e:
    print(f"Error on line {e.lineno}, offset {e.offset}: {e.msg}")
    print(e.text)
