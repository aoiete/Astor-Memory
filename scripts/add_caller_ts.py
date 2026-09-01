src = r'<source_dir>astor_memory\server.py'
with open(src, encoding='utf-8') as f:
    text = f.read()

# Find the doc_timestamp line and add caller_event_ts above it
old = "            doc_timestamp=str(event.ts) if hasattr(event, 'ts') else None,"
new = "            caller_event_ts = body.get('event_time') or body.get('event_ts')\n            doc_timestamp=caller_event_ts or (str(event.ts) if hasattr(event, 'ts') else None),"

count = text.count(old)
print(f'Found old: {count} times')
assert count == 1, f'expected exactly 1, got {count}'
text = text.replace(old, new, 1)

import ast
ast.parse(text)
with open(src, 'w', encoding='utf-8') as f:
    f.write(text)
print('caller_event_ts added to /v1/write')