import json
import sys

sys.stdout.reconfigure(encoding='utf-8')

with open(r'C:\Users\ADMIN\.gemini\antigravity\brain\3c95338d-a708-4dd2-b26e-048a980b8e88\.system_generated\logs\transcript_full.jsonl', 'r', encoding='utf-8') as f:
    for line in f:
        data = json.loads(line)
        if data.get('type') == 'USER_INPUT':
            print(f"Step {data.get('step_index')}: {data.get('content')}")
