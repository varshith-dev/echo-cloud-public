import json
import os

log_path = r"C:\Users\ADMIN\AppData\Local\Temp" # wait, the log path is C:\Users\ADMIN\.gemini\antigravity\brain\3c95338d-a708-4dd2-b26e-048a980b8e88\.system_generated\logs\transcript.jsonl
# Let's check absolute path
log_path = r"C:\Users\ADMIN\.gemini\antigravity\brain\3c95338d-a708-4dd2-b26e-048a980b8e88\.system_generated\logs\transcript.jsonl"

if not os.path.exists(log_path):
    # Try case/slash variant or search for it
    print(f"Log not found at {log_path}")
else:
    with open(log_path, 'r', encoding='utf-8') as f:
        lines = f.readlines()
    
    print(f"Total lines: {len(lines)}")
    # Print the last few lines to see where we left off
    for i in range(max(0, len(lines)-30), len(lines)):
        try:
            data = json.loads(lines[i])
            # Only print relevant info
            source = data.get("source")
            msg_type = data.get("type")
            content = data.get("content", "")
            if content:
                content_preview = content[:200].replace("\n", " ")
            else:
                content_preview = ""
            print(f"[{i}] {data.get('step_index')} | {source} | {msg_type} | {content_preview}")
        except Exception as e:
            print(f"[{i}] Error parsing line: {e}")
