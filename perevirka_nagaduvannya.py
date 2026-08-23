# -*- coding: utf-8 -*-
"""Живий прогон на БОЙОВОМУ config.py: чи мовчить бот о 08:00 і 20:00
і чи не поламалась при цьому звичайна доставка новин."""
import os, sys, datetime as dt
sys.path.insert(0, os.path.expanduser("~/.claude/Новинар"))
os.chdir(os.path.expanduser("~/.claude/Новинар"))
import config, novynar as n

n.DB_PATH = "/tmp/nv_zhyvyi.db"
if os.path.exists(n.DB_PATH):
    os.remove(n.DB_PATH)

SENT = []
# Мокаємо НЕ api(), а мережу під ним: тишу (disable_notification) ставить
# сама api(), і підміна api() зрізала б саме той шар, який перевіряємо.
class _Resp:
    status_code = 200
    def __init__(self, meth): self.meth = meth
    def json(self):
        if self.meth == "getUpdates":
            return {"ok": True, "result": []}
        return {"ok": True, "result": {"message_id": len(SENT)}}
def _fake_post(url, data=None, files=None, timeout=None, **kw):
    meth = url.rsplit("/", 1)[-1]
    d = data or {}
    SENT.append((meth, d.get("chat_id"), d.get("text", "") or d.get("caption", ""),
                 d.get("disable_notification")))
    return _Resp(meth)
n.requests.post = _fake_post
n.requests.get = lambda *a, **kw: _Resp("get")
n.init_db()
n.bootstrap_sources()
with n.db() as _c:
    _c.execute("INSERT OR REPLACE INTO people (user_id, username, is_owner, active) VALUES (129576564, '', 0, 1)")
    _c.execute("INSERT OR REPLACE INTO people (user_id, username, is_owner, active) VALUES (555, '', 1, 1)")

ok = fail = 0
def check(name, cond, note=""):
    global ok, fail
    if cond:
        ok += 1; print("  ✓ %-58s %s" % (name, note))
    else:
        fail += 1; print("  ✗ %-58s %s" % (name, note))

print("Бойовий конфіг: REMINDER_TIMES = %r, QUIET_HOURS = %r"
      % (config.REMINDER_TIMES, config.QUIET_HOURS))

_real_dt = dt.datetime
class _T(_real_dt):
    H, M = 8, 0
    @classmethod
    def now(cls, tz=None): return _real_dt(2026, 8, 24, cls.H, cls.M)
n.datetime = _T

# 30 непрочитаних новин у скарбничці — тобто привід нагадати є з запасом
for _ in range(30):
    n.bump_unread()
check("новини накопичились (є привід нагадати)",
      int(n.get_state("unread", 0)) == 30, "unread = %s" % n.get_state("unread", 0))

print("\n═══ Кожна хвилина вікна нагадувань — з бойовим конфігом ═══")
tyxo = True
for hh in (7, 8, 9, 19, 20, 21):
    for mm in (0, 1, 5, 12, 19, 20, 30, 45, 59):
        _T.H, _T.M = hh, mm
        SENT[:] = []
        n.maybe_remind()
        if SENT:
            tyxo = False
            print("     ✗ %02d:%02d → надіслано %s" % (hh, mm, SENT))
check("за 54 хвилини навколо 08:00 і 20:00 — жодного повідомлення", tyxo,
      "перебрано 7:00–7:59, 8:00–8:59, 9:xx, 19:xx, 20:xx, 21:xx")

# доба поспіль, кожні 10 хвилин — чи не вилізе нагадування деінде
tyxo_doba = True
for hh in range(24):
    for mm in range(0, 60, 10):
        _T.H, _T.M = hh, mm
        SENT[:] = []
        n.maybe_remind()
        if SENT:
            tyxo_doba = False
            print("     ✗ %02d:%02d → %s" % (hh, mm, SENT[0][2][:60]))
check("ціла доба по 10 хвилин — тиша", tyxo_doba, "144 моменти часу")

check("лічильник непрочитаних тримається на нулі, а не росте роками",
      int(n.get_state("unread", 0)) == 0, "unread = %s" % n.get_state("unread", 0))
check("позначки 'нагадав сьогодні' в базі не з'явилось",
      not n.get_state("remind:2026-08-24:08:00"))

print("\n═══ Чи не поламалась доставка самих новин ═══")
_T.H, _T.M = 8, 5
SENT[:] = []
n.broadcast({"text": "Перевірка доставки після правки",
             "link": "https://t.me/test/1", "channel": "test",
             "id": 1, "photo": None}, "Тест")
novyny = [s for s in SENT if s[0] == "sendMessage"]
check("новина читачам іде", len(novyny) >= 1, "надіслано %s" % len(novyny))
chyt = [s for s in novyny if s[1] == 129576564]
vlas = [s for s in novyny if s[1] == 555]
check("новина читачеві йде тихо, як і було",
      chyt and all(s[3] is True for s in chyt),
      "disable_notification = %s" % [s[3] for s in chyt])
check("власниці новина зі звуком, як і було",
      vlas and all(s[3] is False for s in vlas),
      "disable_notification = %s" % [s[3] for s in vlas])
check("у новині немає слів нагадування",
      all("почитати новини" not in s[2] and "Доброго ранку" not in s[2]
          and "Гарного вечора" not in s[2] for s in novyny))

print("\n═══ Повний прохід once() о 08:00 — що піде людям ═══")
_T.H, _T.M = 8, 0
SENT[:] = []
n.round_trip = lambda *a, **kw: None      # каналів не смикаємо, нас цікавить решта
n.once()
nagad = [s for s in SENT if s[0] == "sendMessage" and (
    "почитати" in s[2] or "Доброго ранку" in s[2] or "Гарного вечора" in s[2]
    or "Назбиралося" in s[2])]
check("once() о 08:00 не шле нагадувань", not nagad,
      "усього викликів API: %s" % len(SENT))

_T.H, _T.M = 20, 0
SENT[:] = []
n.once()
nagad = [s for s in SENT if s[0] == "sendMessage" and (
    "почитати" in s[2] or "Гарного вечора" in s[2] or "Назбиралося" in s[2])]
check("once() о 20:00 не шле нагадувань", not nagad,
      "усього викликів API: %s" % len(SENT))

print("\n═══ Вимикач оборотний: вписав години — нагадування ожило ═══")
config.REMINDER_TIMES = ["08:00", "20:00"]
for _ in range(4):
    n.bump_unread()
_T.H, _T.M = 8, 3
SENT[:] = []
n.maybe_remind()
nag = [x for x in SENT if "Назбиралося" in x[2] or "назбиралося" in x[2]]
check("з годинами в конфізі нагадування знову шлеться", len(nag) == 1,
      nag[0][2][:52].replace("\n", " ") if nag else "нічого")
check("і рахує від нуля, а не за весь час мовчання",
      nag and "4 новини" in nag[0][2], "мало бути 4 новини")
config.REMINDER_TIMES = []

print("\n%s ПІДСУМОК ЖИВОГО ПРОГОНУ: %s правильно, %s помилок\n"
      % ("✅" if not fail else "❌", ok, fail))
sys.exit(1 if fail else 0)
