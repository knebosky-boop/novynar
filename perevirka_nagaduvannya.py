# -*- coding: utf-8 -*-
"""Живий прогон на БОЙОВОМУ config.py: чи шле бот нагадування о 08:00 і 20:00
(повернуто 27.08.2026), чи мовчить решту доби і чи не поламалась доставка новин."""
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
# 12.09.2026: суддя вимкнула нагадування («вимкни нагадування Миколі»), тож
# бойовий список порожній. Механізм від цього не зник — далі женемо його на
# підставлених годинах, інакше правка, що його зламає, пройде непоміченою.
check("у бойовому конфізі нагадування вимкнені (вказівка судді 12.09.2026)",
      config.REMINDER_TIMES == [], str(config.REMINDER_TIMES))
_бойові_години = config.REMINDER_TIMES
config.REMINDER_TIMES = ["08:00", "20:00"]   # лише для цього прогону

_real_dt = dt.datetime
class _T(_real_dt):
    H, M = 8, 0
    @classmethod
    def now(cls, tz=None): return _real_dt(2026, 8, 24, cls.H, cls.M)
n.datetime = _T

def nagady():
    return [s for s in SENT if s[0] == "sendMessage" and (
        "почитати" in s[2] or "Доброго ранку" in s[2]
        or "Гарного вечора" in s[2] or "азбиралося" in s[2])]

# 30 непрочитаних новин у скарбничці — привід нагадати є з запасом
for _ in range(30):
    n.bump_unread()
check("новини накопичились (є привід нагадати)",
      int(n.get_state("unread", 0)) == 30, "unread = %s" % n.get_state("unread", 0))

print("\n═══ Ранкове нагадування о 08:00 ═══")
_T.H, _T.M = 7, 59
SENT[:] = []
n.maybe_remind()
check("о 07:59 ще мовчить", not SENT)

_T.H, _T.M = 8, 3
SENT[:] = []
n.maybe_remind()
nag = nagady()
check("о 08:03 нагадування пішло", len(nag) == 1,
      nag[0][2][:52].replace("\n", " ") if nag else "нічого")
check("текст ранковий і рахунок точний",
      nag and "Доброго ранку" in nag[0][2] and "30 новин" in nag[0][2])
check("іде читачеві, а не власниці",
      nag and nag[0][1] == 129576564 and all(s[1] != 555 for s in nag))
check("нагадування зі звуком (на відміну від новин)",
      nag and nag[0][3] is False, "disable_notification = %s" % (nag[0][3] if nag else "?"))
check("лічильник обнулився", int(n.get_state("unread", 0)) == 0,
      "unread = %s" % n.get_state("unread", 0))
check("позначка 'нагадав сьогодні' стала в базі",
      bool(n.get_state("remind:2026-08-24:08:00")))

for _ in range(5):
    n.bump_unread()
_T.H, _T.M = 8, 10
SENT[:] = []
n.maybe_remind()
check("вдруге того ж ранку не турбує", not nagady())

print("\n═══ Вечірнє нагадування о 20:00 ═══")
_T.H, _T.M = 20, 5
SENT[:] = []
n.maybe_remind()
nag = nagady()
check("о 20:05 нагадування пішло", len(nag) == 1,
      nag[0][2][:52].replace("\n", " ") if nag else "нічого")
check("текст вечірній і рахує від ранку, не за весь час",
      nag and "Гарного вечора" in nag[0][2] and "5 новин" in nag[0][2])

print("\n═══ Поза вікнами — тиша, порожня скарбничка — не турбує ═══")
# доба по 10 хвилин; дозволені лише вікна 20 хв після 08:00 і 20:00,
# але позначки за сьогодні вже стоять — отже має бути суцільна тиша
tyxo_doba = True
for hh in range(24):
    for mm in range(0, 60, 10):
        _T.H, _T.M = hh, mm
        SENT[:] = []
        n.maybe_remind()
        if nagady():
            tyxo_doba = False
            print("     ✗ %02d:%02d → %s" % (hh, mm, SENT[0][2][:60]))
check("решта доби по 10 хвилин — тиша", tyxo_doba, "144 моменти часу")

# наступний день, новин нуль — час настав, а турбувати нічим
_T2 = _T
class _T3(_real_dt):
    H, M = 8, 3
    @classmethod
    def now(cls, tz=None): return _real_dt(2026, 8, 25, cls.H, cls.M)
n.datetime = _T3
SENT[:] = []
n.maybe_remind()
check("новин нема — час настав, але не турбує", not nagady())
check("позначку при цьому не ставить (ще встигне у вікні)",
      not n.get_state("remind:2026-08-25:08:00"))
n.datetime = _T2

print("\n═══ Чи не поламалась доставка самих новин ═══")
_T.H, _T.M = 12, 5
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

print("\n═══ Повний прохід once() — нагадування вбудоване у зміну ═══")
class _T4(_real_dt):
    H, M = 20, 2
    @classmethod
    def now(cls, tz=None): return _real_dt(2026, 8, 25, cls.H, cls.M)
n.datetime = _T4
n.bump_unread()
n.round_trip = lambda *a, **kw: None      # каналів не смикаємо, нас цікавить решта
SENT[:] = []
n.once()
check("once() о 20:02 шле рівно одне нагадування", len(nagady()) == 1,
      "усього викликів API: %s" % len(SENT))
_T4.H, _T4.M = 12, 0
SENT[:] = []
n.once()
check("once() серед дня нагадувань не шле", not nagady(),
      "усього викликів API: %s" % len(SENT))

print("\n═══ Вимикач оборотний: порожній список — тиша і нуль ═══")
config.REMINDER_TIMES = []
for _ in range(4):
    n.bump_unread()
_T4.H, _T4.M = 8, 3
class _T5(_real_dt):
    @classmethod
    def now(cls, tz=None): return _real_dt(2026, 8, 26, 8, 3)
n.datetime = _T5
SENT[:] = []
n.maybe_remind()
check("без годин у конфізі мовчить навіть о 08:03", not nagady())
check("і лічильник тримає на нулі, а не копить",
      int(n.get_state("unread", 0)) == 0, "unread = %s" % n.get_state("unread", 0))
config.REMINDER_TIMES = _бойові_години    # повертаємо бойове значення

print("\n%s ПІДСУМОК ЖИВОГО ПРОГОНУ: %s правильно, %s помилок\n"
      % ("✅" if not fail else "❌", ok, fail))
sys.exit(1 if fail else 0)
