import tokenize

with open('app.py', 'r', encoding='utf-8') as f:
    for token in tokenize.generate_tokens(f.readline):
        if token.start[0] <= 6966 and token.end[0] >= 6966:
            print(f"Token spanning line 6966: type={token.type}, string={repr(token.string)[:100]}")
        if token.type == tokenize.FSTRING_START:
            print(f"F-string starts at {token.start}")
        elif token.type == tokenize.FSTRING_END:
            print(f"F-string ends at {token.end}")
