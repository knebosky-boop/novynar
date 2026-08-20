# -*- coding: utf-8 -*-
"""Службовий звіт по живій базі: шукаємо, звідки в читача повтори.

Нічого не надсилає й не змінює — тільки читає novynar.db і друкує.
Запускається окремою дією «Звіт про повтори» на GitHub.
"""
import sqlite3, time, sys, collections
import novynar as n
import config

DB = sys.argv[1] if len(sys.argv) > 1 else "novynar.db"
c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row
teper = int(time.time())


def zaholovok(s):
    print("\n" + "═" * 70)
    print("  " + s)
    print("═" * 70)


zaholovok("ЩО ВЗАГАЛІ Є В БАЗІ")
for t in ("sources", "sent", "sent_msg", "recent", "seen", "people", "queue"):
    try:
        k = c.execute("SELECT COUNT(*) k FROM %s" % t).fetchone()["k"]
        print("  %-10s %s" % (t, k))
    except Exception as e:
        print("  %-10s — немає (%s)" % (t, e))

zaholovok("ПОЗИЦІЇ КАНАЛІВ (чи не відкочуються назад)")
for r in c.execute("SELECT channel, last_id, active FROM sources ORDER BY channel"):
    print("  %-24s last_id=%-10s active=%s" % (r["channel"], r["last_id"], r["active"]))

zaholovok("ОДНЕ Й ТЕ САМЕ ПОВІДОМЛЕННЯ НАДІСЛАНО ДВІЧІ ОДНІЙ ЛЮДИНІ")
dubl = c.execute("""SELECT channel, post_id, user_id, part, kind, COUNT(*) k
                    FROM sent_msg GROUP BY channel, post_id, user_id, part
                    HAVING k > 1 ORDER BY k DESC""").fetchall()
if dubl:
    for r in dubl[:20]:
        print("  ✗ %s/%s → %s (частина %s, %s) надіслано %s разів"
              % (r["channel"], r["post_id"], r["user_id"], r["part"], r["kind"], r["k"]))
    print("  РАЗОМ таких випадків: %s" % len(dubl))
else:
    print("  ✓ технічних повторів немає — жоден пост не пішов двічі")

zaholovok("СКІЛЬКИ ЧАСТИН НА ОДНУ НОВИНУ (довгий пост ріжеться на кілька)")
ch = collections.Counter()
for r in c.execute("""SELECT channel, post_id, user_id, COUNT(*) k FROM sent_msg
                      GROUP BY channel, post_id, user_id"""):
    ch[r["k"]] += 1
for k in sorted(ch):
    print("  %s повідомлення(нь) — %s новин" % (k, ch[k]))

zaholovok("СХОЖІ НОВИНИ, ЩО ВСЕ ОДНО ПРОЙШЛИ (за останні 72 год)")
rows = [dict(r) for r in c.execute(
    "SELECT channel, post_id, body, ts FROM sent WHERE ts > ? ORDER BY ts",
    (teper - 72 * 3600,))]
print("  надіслано за 72 год: %s новин" % len(rows))
pary = []
for i in range(len(rows)):
    for j in range(i + 1, len(rows)):
        a, b = rows[i], rows[j]
        if b["ts"] - a["ts"] > 24 * 3600:
            continue
        ta, tb = n.tokens(a["body"] or ""), n.tokens(b["body"] or "")
        if len(ta) < 5 or len(tb) < 5:
            continue
        zbig = n.looks_similar(ta, tb)
        same, _ = n.is_same_story(ta, n.anchors(a["body"] or ""),
                                  tb, n.anchors(b["body"] or ""))
        if zbig >= 0.30:
            pary.append((zbig, a, b, same))
pary.sort(key=lambda p: -p[0])
for zbig, a, b, same in pary[:15]:
    print("\n  ── збіг %.0f%% %s ── %s/%s   +%d хв   %s/%s"
          % (zbig * 100, "(дедуп мав би зловити!)" if same else "(для дедупу — різні)",
             a["channel"], a["post_id"],
             (b["ts"] - a["ts"]) // 60, b["channel"], b["post_id"]))
    print("     A: %s" % " ".join((a["body"] or "").split())[:130])
    print("     B: %s" % " ".join((b["body"] or "").split())[:130])
if not pary:
    print("  ✓ схожих пар не знайдено")
else:
    print("\n  РАЗОМ пар зі збігом ≥30%%: %s" % len(pary))

zaholovok("ЩО САМЕ ПРИХОДИЛО ОСТАННІМ (20 новин)")
for r in c.execute("SELECT channel, post_id, body, ts FROM sent ORDER BY ts DESC LIMIT 20"):
    print("  %s  %-22s %-8s %s" % (time.strftime("%d.%m %H:%M", time.localtime(r["ts"])),
                                   r["channel"], r["post_id"],
                                   " ".join((r["body"] or "").split())[:70]))

zaholovok("ЯК СПРАЦЮЄ ПРИДЕРЖКА (симуляція на живих новинах)")
# Придержка склеює лише те, що лежить у вікні одночасно. Тому головне питання
# не «наскільки схожі», а «скільки хвилин між публікаціями».
print("  розбіжність у часі всередині пар зі збігом >=30%:")
kosh = collections.Counter()
for zbig, a, b, _ in pary:
    hv = (b["ts"] - a["ts"]) // 60
    kosh["до 5 хв" if hv < 5 else "5–10 хв" if hv < 10 else "10–15 хв" if hv < 15
         else "15–30 хв" if hv < 30 else "30–60 хв" if hv < 60 else "понад годину"] += 1
for k in ("до 5 хв", "5–10 хв", "10–15 хв", "15–30 хв", "30–60 хв", "понад годину"):
    if kosh[k]:
        print("     %-14s %s пар" % (k, kosh[k]))

print()
print("  скільки пар склеїла б придержка (пороги %.2f + %s опор):"
      % (config.HOLD_SIMILARITY, config.HOLD_ANCHORS))
for vikno in (5, 10, 15, 30, 60):
    skleyit, rozvela = [], []
    for zbig, a, b, _ in pary:
        if (b["ts"] - a["ts"]) > vikno * 60:
            continue
        same, _sc = n.one_story_now(n.tokens(a["body"] or ""), n.anchors(a["body"] or ""),
                                    n.tokens(b["body"] or ""), n.anchors(b["body"] or ""))
        if not same:
            continue
        if n.refutes(a["body"]) != n.refutes(b["body"]):
            rozvela.append((zbig, a, b))
        else:
            skleyit.append((zbig, a, b))
    print("     вікно %2d хв → склеїть %2d пар; спростувань уберегло: %s"
          % (vikno, len(skleyit), len(rozvela)))

print("\n  ── ЩО САМЕ СКЛЕЇТЬСЯ ПРИ ВІКНІ 10 ХВИЛИН ──")
for zbig, a, b, _ in pary:
    if (b["ts"] - a["ts"]) > 600:
        continue
    same, _sc = n.one_story_now(n.tokens(a["body"] or ""), n.anchors(a["body"] or ""),
                                n.tokens(b["body"] or ""), n.anchors(b["body"] or ""))
    if same and n.refutes(a["body"]) == n.refutes(b["body"]):
        print("\n  збіг %.0f%%, +%d хв, опори: %s" % (
            zbig * 100, (b["ts"] - a["ts"]) // 60,
            ", ".join(sorted(n.anchors(a["body"] or "") & n.anchors(b["body"] or ""))[:8])))
        print("     A (%s): %s" % (a["channel"], " ".join((a["body"] or "").split())[:110]))
        print("     B (%s): %s" % (b["channel"], " ".join((b["body"] or "").split())[:110]))

zaholovok("ЩО БУДЕ, ЯКЩО ЗНИЗИТИ ПОРОГИ (симуляція на живих новинах)")
# Для кожної пари друкуємо спільні опори — видно, чим саме її можна зловити
# і чи не зловиться разом із нею спростування («затримано» / «не затримували»).
NEBEZPECHNI = ("не ", "спрост", "запереч", "не підтверд", "фейк", "опроверг", "не задерж")


def nebezpechna_para(a, b):
    la, lb = (a or "").lower(), (b or "").lower()
    return any(s in la for s in NEBEZPECHNI) != any(s in lb for s in NEBEZPECHNI)


varianty = [(0.35, 5), (0.35, 4), (0.35, 3), (0.40, 3), (0.30, 4), (0.30, 3)]
print("  зараз ловиться: %s пар із %s (поріг 0.35 + 5 опор)"
      % (sum(1 for p in pary if p[3]), len(pary)))
print()
for por, opor in varianty:
    zlovyt, ryzyk = [], []
    for zbig, a, b, _ in pary:
        anc = n.anchors(a["body"] or "") & n.anchors(b["body"] or "")
        if zbig >= por and len(anc) >= opor:
            (ryzyk if nebezpechna_para(a["body"], b["body"]) else zlovyt).append(
                (zbig, a, b, anc))
    print("  поріг %.2f + %s опор → зловить %s пар, з них підозрілих (одна новина "
          "спростовує іншу): %s" % (por, opor, len(zlovyt) + len(ryzyk), len(ryzyk)))

print("\n  ── ПОДРОБИЦІ ДЛЯ 0.35 + 3 ОПОРИ ──")
for zbig, a, b, _ in pary:
    anc = n.anchors(a["body"] or "") & n.anchors(b["body"] or "")
    if zbig >= 0.35 and len(anc) >= 3:
        print("\n  збіг %.0f%%, опори: %s%s" % (
            zbig * 100, ", ".join(sorted(anc)[:8]),
            "   ⚠ ОДНА СПРОСТОВУЄ ІНШУ?" if nebezpechna_para(a["body"], b["body"]) else ""))
        print("     A (%s): %s" % (a["channel"], " ".join((a["body"] or "").split())[:110]))
        print("     B (%s): %s" % (b["channel"], " ".join((b["body"] or "").split())[:110]))
