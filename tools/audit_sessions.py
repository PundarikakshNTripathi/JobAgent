"""Audit headless fill sessions: list every browser action that could submit (clicks, key presses, evaluate calls) and when the confirmation URL first appeared."""
import glob, json, os, re
base = glob.glob(os.path.expanduser('~/.claude/projects/*JobAgent*'))[0]
for f in sorted(glob.glob(base + '/*.jsonl'), key=os.path.getmtime):
    raw = open(f, encoding='utf-8', errors='replace').read()
    if 'fillbrowser' not in raw:
        continue
    print('=====', os.path.basename(f))
    for line in raw.splitlines():
        try: o = json.loads(line)
        except Exception: continue
        c = (o.get('message') or {}).get('content')
        if not isinstance(c, list): continue
        for b in c:
            if b.get('type') == 'tool_use':
                n = b['name'].replace('mcp__fillbrowser__browser_', '')
                s = json.dumps(b.get('input'))
                if n in ('click', 'press_key', 'evaluate', 'run_code_unsafe', 'navigate', 'tabs'):
                    print(o.get('timestamp', '')[11:19], n, s[:200])
            elif b.get('type') == 'tool_result':
                t = json.dumps(b.get('content'))
                if re.search(r'confirmation|Thank you for applying|blocked by JobAgent|\[jobagent\]', t):
                    m = re.search(r'(confirmation[^"\\]{0,80}|Thank you for applying|\[jobagent\][^"\\]{0,60}|blocked by JobAgent[^"\\]{0,40})', t)
                    print(o.get('timestamp', '')[11:19], '  RESULT>>', m.group(1) if m else '')
