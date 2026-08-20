# -*- coding: utf-8 -*-
"""Службовий звіт по живій базі: шукаємо, звідки в читача повтори.

Нічого не надсилає й не змінює — тільки читає novynar.db і друкує.
Запускається окремою дією «Звіт про повтори» на GitHub.
"""
import sqlite3, time, sys, collections
import novynar as n

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
