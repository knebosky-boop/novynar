# Сухий прогін прод-коду Новинаря по живих каналах: нічого не шле.
import sys, os, logging, json, time, sqlite3, copy
os.chdir(sys.argv[1]); sys.path.insert(0, sys.argv[1])
WIDE = len(sys.argv) > 2 and sys.argv[2] == 'wide'
import novynar as n, config
rows = []
class H(logging.Handler):
    def emit(self, r): rows.append(r.getMessage())
n.log.addHandler(H())
would = []
n.broadcast = lambda post, title: (would.append(dict(ch=post['channel'], id=post['id'], link=post['link'], via='send', t=(post['text'] or '')[:100].replace('\n',' '))) or True)
n.enqueue   = lambda post, title: would.append(dict(ch=post['channel'], id=post['id'], link=post['link'], via='queue', t=(post['text'] or '')[:100].replace('\n',' ')))
n.check_edits = lambda *a, **k: None
n.remember = lambda *a, **k: None
n.hold = lambda *a, **k: None
if hasattr(n, 'api'): n.api = lambda *a, **k: None
if WIDE:
    _src = n.sources
    def src():
        out = []
        for s in _src():
            d = dict(s); d['last_id'] = 1; out.append(d)
        return out
    n.sources = src
    n.seen_before = lambda k: False
    n.already_told = lambda t: None
    config.MAX_PER_ROUND = 100
t0 = time.time()
n.round_trip()
c = sqlite3.connect('novynar.db')
sent_links = set((r[0],r[1]) for r in c.execute('select channel,post_id from sent'))
print('WIDE' if WIDE else 'PLAIN', 'за %.0f с' % (time.time()-t0))
print('would:', len(would), ' skips:', sum(1 for r in rows if r.startswith('пропуск')), ' дублі:', sum(1 for r in rows if r.startswith('дубль') or 'схоже на вже' in r), ' warn:', [r for r in rows if 'не відповів' in r or 'зірвалась' in r])
from collections import Counter
print('skip reasons:', Counter(r.split(': ',1)[1] for r in rows if r.startswith('пропуск')).most_common(12))
print('per channel would:', Counter(w['ch'] for w in would))
if WIDE:
    inbase = [w for w in would if (w['ch'],w['id']) in sent_links]
    notin = [w for w in would if (w['ch'],w['id']) not in sent_links]
    print('would & в sent(24h):', len(inbase), '; would & НЕ в sent:', len(notin))
    json.dump(dict(would=would, rows=rows, notin=notin), open(os.environ['S']+'/sukho-wide.json','w'), ensure_ascii=False, indent=1)
else:
    for w in would: print(' ', w['via'], w['ch'], w['id'], w['t'][:80])
    json.dump(dict(would=would, rows=rows), open(os.environ['S']+'/sukho-plain.json','w'), ensure_ascii=False, indent=1)
