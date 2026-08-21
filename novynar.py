# -*- coding: utf-8 -*-
"""Новинар — збирає пости з відкритих телеграм-каналів і надсилає їх ботом.

Не потребує входу в акаунт: читає публічні веб-сторінки каналів (t.me/s/…)
і віддає новини через бота @Kate_news_2026_bot.
"""

import html as html_mod
import difflib
import hashlib
import logging
import os
import re
import sqlite3
import sys
import threading
import time
import warnings
from datetime import datetime

warnings.filterwarnings("ignore")

import requests
from bs4 import BeautifulSoup

import config

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "novynar.db")
API = "https://api.telegram.org/bot%s/" % config.BOT_TOKEN
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.FileHandler(os.path.join(BASE, "novynar.log"), encoding="utf-8"),
              logging.StreamHandler(sys.stdout)],
)
logging.getLogger("urllib3").setLevel(logging.WARNING)
log = logging.getLogger("novynar")

paused = False
_lock = threading.Lock()


# ─────────────────────────────  база  ─────────────────────────────

def db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sources (
            channel  TEXT PRIMARY KEY,
            title    TEXT,
            last_id  INTEGER DEFAULT 0,
            active   INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS seen (
            key TEXT PRIMARY KEY, ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS holding (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            grp INTEGER, channel TEXT, title TEXT, link TEXT,
            text TEXT, photo TEXT, video INTEGER, weight INTEGER, ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS recent (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT, words TEXT, anchors TEXT, ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT, title TEXT, link TEXT, text TEXT, photo TEXT, ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS sent (
            channel TEXT, post_id INTEGER, title TEXT, hash TEXT,
            body TEXT, suffix TEXT, first_limit INTEGER, ts INTEGER,
            PRIMARY KEY (channel, post_id)
        );
        CREATE TABLE IF NOT EXISTS sent_msg (
            channel TEXT, post_id INTEGER, user_id INTEGER,
            message_id INTEGER, part INTEGER, kind TEXT
        );
        CREATE TABLE IF NOT EXISTS people (
            user_id  INTEGER PRIMARY KEY,
            username TEXT,
            is_owner INTEGER DEFAULT 0,
            active   INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS allowed (
            username TEXT PRIMARY KEY, ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS state (
            k TEXT PRIMARY KEY, v TEXT
        );
        """)


def migrate():
    """Доводимо стару базу до нового вигляду — вона живе в сховищі GitHub
    і не перестворюється при оновленні програми."""
    wanted = {
        "recent": [("anchors", "TEXT")],
        "sent": [("body", "TEXT"), ("suffix", "TEXT"), ("first_limit", "INTEGER")],
        "sources": [("title", "TEXT"), ("last_id", "INTEGER DEFAULT 0")],
        "people": [("username", "TEXT"), ("is_owner", "INTEGER DEFAULT 0")],
    }
    with db() as c:
        for table, cols in wanted.items():
            try:
                have = {r[1] for r in c.execute("PRAGMA table_info(%s)" % table)}
            except sqlite3.Error:
                continue
            if not have:
                continue
            for name, decl in cols:
                if name not in have:
                    c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, name, decl))
                    log.info("базу оновлено: %s.%s", table, name)


def get_state(k, default=None):
    with db() as c:
        r = c.execute("SELECT v FROM state WHERE k = ?", (k,)).fetchone()
    return r["v"] if r else default


def set_state(k, v):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO state (k, v) VALUES (?,?)", (k, str(v)))


def sources():
    with db() as c:
        return [dict(r) for r in
                c.execute("SELECT * FROM sources WHERE active = 1 ORDER BY channel")]


def readers():
    with db() as c:
        return [dict(r) for r in c.execute("SELECT * FROM people WHERE active = 1")]


def owner():
    with db() as c:
        r = c.execute("SELECT * FROM people WHERE is_owner = 1").fetchone()
    return dict(r) if r else None


def seen_before(key):
    now = int(time.time())
    with db() as c:
        c.execute("DELETE FROM seen WHERE ts < ?", (now - config.DEDUP_HOURS * 3600,))
        if c.execute("SELECT 1 FROM seen WHERE key = ?", (key,)).fetchone():
            return True
        c.execute("INSERT INTO seen (key, ts) VALUES (?,?)", (key, now))
    return False


# ─────────────────────────────  фільтри  ─────────────────────────────

def norm(text):
    t = (text or "").lower()
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[^\w\s]", "", t, flags=re.UNICODE)
    return re.sub(r"\s+", " ", t).strip()


def word_in(low, key):
    """Пошук за основою слова: «Дніпро» знайдеться і в «Дніпрі», і в «Дніпра»."""
    k = (key or "").lower().strip()
    if not k:
        return False
    if len(k) > 5 and " " not in k:
        k = k[:-1]
    return k in low


def starts_word(low, stem):
    """Шукаємо основу з початку слова: «гази» не сховається в «магазині»."""
    return re.search(r"(?<![\w'’-])" + re.escape(stem), low) is not None


NOISE_WORDS = {
    "який", "яка", "яке", "які", "цього", "цьому", "також", "після", "через",
    "щодо", "тому", "того", "буде", "було", "були", "може", "мають", "має",
    "його", "їхн", "вони", "вона", "цьому", "поки", "лише", "дуже", "коли",
    "этом", "этого", "также", "после", "через", "будет", "было", "были",
}


def tokens(text):
    """Значущі слова новини, обрізані до основи.

    «Нікополю» і «Нікополь», «пошкоджено» і «пошкоджені» мають збігтися —
    інакше та сама новина з двох каналів виглядає як дві різні."""
    out = set()
    for w in norm(text).split():
        if len(w) <= 3 or w in NOISE_WORDS or w.isdigit():
            continue
        out.add(w[:6] if len(w) > 6 else w)
    return out


def anchors(text):
    """Власні назви й числа — те, про що новина насправді.

    Дві розповіді про одну подію збігаються саме тут: «Фламінго», «Прогрес»,
    «Самара». Схожі за словником, але різні новини цих опор не поділяють."""
    raw = re.sub(r"<[^>]+>", " ", text or "")
    out = set()
    for w in re.findall(r"[А-ЯІЇЄҐA-Z][\w'’-]{3,}", raw):      # Нікополь, Трамп
        out.add(w.lower()[:6])
    for abbr in re.findall(r"\b[А-ЯІЇЄҐA-Z]{2,6}\b", raw):    # ВПК, СОУ, ОВА, НАТО
        out.add(abbr.lower())
    for num in re.findall(r"\b\d{3,}\b", raw):                # 265, 2026
        out.add("#" + num)
    for d, m in re.findall(r"\b(\d{1,2})\s+(січня|лютого|березня|квітня|травня|"
                           r"червня|липня|серпня|вересня|жовтня|листопада|грудня)", raw.lower()):
        out.add("@%s.%s" % (d, m[:4]))                        # 16 серпня
    return out


def looks_similar(a, b):
    """Скільки спільного у двох новин: 1.0 — одне й те саме, 0 — нічого."""
    if not a or not b:
        return 0.0
    inter = len(a & b)
    if not inter:
        return 0.0
    jaccard = inter / float(len(a | b))
    overlap = inter / float(min(len(a), len(b)))
    # коротка і докладна версії однієї новини: враховуємо перекриття
    if min(len(a), len(b)) >= 8:
        return max(jaccard, overlap * 0.9)
    return jaccard


def is_same_story(tok_a, anc_a, tok_b, anc_b):
    """Чи це та сама подія. Повертає (так/ні, збіг)."""
    score = looks_similar(tok_a, tok_b)
    if score >= config.SIMILARITY:
        return True, score
    # Слабший збіг словами рятують спільні власні назви, абревіатури й дати.
    common = len(anc_a & anc_b)
    if score >= getattr(config, "SIMILARITY_WEAK", 0.50) and \
            common >= getattr(config, "ANCHORS_NEEDED", 3):
        return True, score
    # Коли збігається багато назв і дат — сумнівів немає навіть при різних словах.
    if score >= getattr(config, "SIMILARITY_ANCHORED", 0.35) and \
            common >= getattr(config, "ANCHORS_STRONG", 5):
        return True, score
    return False, score


def already_told(text):
    """Чи розповідали ми це вже — хай навіть іншими словами."""
    if not config.SIMILARITY:
        return None
    tok, anc = tokens(text), anchors(text)
    if len(tok) < 5:
        return None
    now = int(time.time())
    with db() as c:
        c.execute("DELETE FROM recent WHERE ts < ?", (now - config.DEDUP_HOURS * 3600,))
        for r in c.execute("SELECT channel, words, anchors FROM recent").fetchall():
            same, score = is_same_story(tok, anc, set((r["words"] or "").split()),
                                        set((r["anchors"] or "").split()))
            if same:
                return (r["channel"], score)
    return None


def remember(channel, text):
    """Запам'ятати сюжет як розказаний."""
    tok = tokens(text)
    if len(tok) < 5:
        return
    with db() as c:
        c.execute("INSERT INTO recent (channel, words, anchors, ts) VALUES (?,?,?,?)",
                  (channel, " ".join(sorted(tok)), " ".join(sorted(anchors(text))),
                   int(time.time())))


def refutes(text):
    """Чи це спростування — «не затримували», «спростував», «фейк»."""
    low = (text or "").lower()
    return any(re.search(p, low) for p in getattr(config, "REFUTE_PATTERNS", []))


def one_story_now(tok_a, anc_a, tok_b, anc_b):
    """Чи це та сама подія — але у вузькому вікні придержки.

    Пороги тут м'якші, ніж у дедупі: той пам'ятає 48 годин, а це — десять
    хвилин. За дві доби три спільні назви бувають і в різних сюжетів, за
    десять хвилин — навряд."""
    score = looks_similar(tok_a, tok_b)
    if score >= config.SIMILARITY:
        return True, score
    if score >= getattr(config, "HOLD_SIMILARITY", 0.35) and \
            len(anc_a & anc_b) >= getattr(config, "HOLD_ANCHORS", 3):
        return True, score
    return False, score


def weigh(post):
    """Наскільки версія докладна: довжина тексту плюс бонус за картинку."""
    return len(post.get("text") or "") + (250 if post.get("photo") else 0)


def hold(post, title):
    """Кладемо новину в очікування — раптом хтось розповість докладніше."""
    tok, anc = tokens(post["text"]), anchors(post["text"])
    sprostuvannia = refutes(post["text"])
    with db() as c:
        grp = None
        if config.SIMILARITY and len(tok) >= 5:
            for r in c.execute("SELECT id, grp, text FROM holding").fetchall():
                if refutes(r["text"]) != sprostuvannia:
                    continue          # спростування — окрема новина, не версія
                same, _ = one_story_now(tok, anc, tokens(r["text"]),
                                        anchors(r["text"]))
                if same:
                    grp = r["grp"]
                    break
        if grp is None:
            row = c.execute("SELECT COALESCE(MAX(grp), 0) + 1 g FROM holding").fetchone()
            grp = row["g"]
        c.execute("INSERT INTO holding "
                  "(grp, channel, title, link, text, photo, video, weight, ts) "
                  "VALUES (?,?,?,?,?,?,?,?,?)",
                  (grp, post["channel"], title, post["link"], post["text"] or "",
                   post["photo"] or "", 1 if post["video"] else 0,
                   weigh(post), int(time.time())))
    return grp


def release_ready(force=False):
    """Віддаємо ті сюжети, що вже відлежались. З групи — найдокладнішу версію."""
    ripe = int(time.time()) - int(config.HOLD_MINUTES * 60)
    with db() as c:
        grps = [r["grp"] for r in c.execute(
            "SELECT grp, MIN(ts) t FROM holding GROUP BY grp "
            "HAVING (? OR MIN(ts) <= ?)", (1 if force else 0, ripe)).fetchall()]
    sent = 0
    for grp in grps:
        with db() as c:
            rows = [dict(r) for r in c.execute(
                "SELECT * FROM holding WHERE grp = ? ORDER BY weight DESC", (grp,))]
            c.execute("DELETE FROM holding WHERE grp = ?", (grp,))
        if not rows:
            continue
        best = rows[0]
        if len(rows) > 1:
            log.info("сюжет із %s версій — беру найдокладнішу (%s, %s знаків), "
                     "решту відкидаю: %s", len(rows), best["channel"],
                     len(best["text"] or ""),
                     ", ".join(r["channel"] for r in rows[1:]))
        post = {"channel": best["channel"], "text": best["text"],
                "photo": best["photo"] or None, "video": bool(best["video"]),
                "link": best["link"], "id": 0}
        if config.MODE == "digest" or in_quiet_hours():
            enqueue(post, best["title"])
        else:
            broadcast(post, best["title"])
        remember(best["channel"], best["text"])
        sent += 1
    return sent


def is_alert(text):
    """Сигналізація замість новини: тривоги, дорозвідка, атака в моменті.

    Тверді маркери («дорозвідка», «курсом на») ріжемо завжди. М'які
    («вибухи», «працює ППО») — лише в короткому повідомленні: у ранковій
    зведенці про наслідки ті самі слова означають нормальну новину."""
    if not getattr(config, "SKIP_ALERTS", False):
        return False
    low = (text or "").lower()

    hits = [m for m in config.ALERT_MARKERS if starts_word(low, m)]
    if hits and not (len(low) > 600 and len(hits) < 3):
        return hits[0]

    soft = [m for m in getattr(config, "LIVE_ATTACK_MARKERS", [])
            if starts_word(low, m)]
    if soft and len(low) <= getattr(config, "LIVE_ATTACK_MAX_LEN", 400):
        # «збили 42 шахеди минулої ночі» — це підсумок, а не сигнал тривоги
        if any(starts_word(low, w) for w in getattr(config, "NEWS_MARKERS", [])):
            return False
        return soft[0]
    return False


def is_service(text):
    """«Шрайк pinned «…»» — це позначка телеграму, а не новина."""
    raw = re.sub(r"<[^>]+>", "", text or "").strip()
    for pat in getattr(config, "SERVICE_PATTERNS", []):
        if re.search(pat, raw, re.I):
            return True
    return False


def is_greeting(text):
    """Пост-побажання замість новини: «Доброй ночи» плюс підпис каналу.

    MIN_LENGTH такого не ловить — підпис @канал добирає довжину, а картинка
    взагалі проводить пост в обхід порога. Тому дивимось на текст ПІСЛЯ зняття
    розмітки, посилань і підписів: лишилось саме побажання і нічого більше —
    ріжемо. Новина «Зеленський побажав добраніч захисникам» довша за
    GREETING_MAX_LEN і проходить."""
    pats = getattr(config, "GREETING_PATTERNS", [])
    if not pats:
        return ""
    bare = re.sub(r"<[^>]+>", " ", text or "")
    bare = re.sub(r"https?://\S+", " ", bare)
    bare = re.sub(r"@[\w_]+", " ", bare)          # підпис каналу в кінці поста
    bare = norm(bare)
    if not bare or len(bare) > getattr(config, "GREETING_MAX_LEN", 60):
        return ""
    for pat in pats:
        m = re.search(pat, bare)
        if m:
            return m.group(0)
    return ""


def is_daily_toll(text):
    """Щоденне зведення ОВА «вбито стількох, поранено стількох» — не новина.

    Ріжемо не за словом «поранено», а за зв'язкою підрахунку з періодом
    («за добу», «за 18 серпня»). Новина про конкретний обстріл — «Трьох
    цивільних поранено: наслідки бомбардування Краматорська» — періоду
    не має і проходить."""
    low = re.sub(r"<[^>]+>", " ", text or "").lower()
    low = re.sub(r"\s+", " ", low)
    for pat in getattr(config, "DAILY_TOLL_PATTERNS", []):
        m = re.search(pat, low)
        if m:
            return re.sub(r"\s+", " ", m.group(0))[:60]
    return ""


def off_topic(low, channel):
    """Тема, якої від цього каналу не треба (напр. суто ізраїльські новини)."""
    rule = getattr(config, "CHANNEL_TOPIC_SKIP", {}).get(channel or "")
    if not rule:
        return None
    hit = next((w for w in rule.get("skip", []) if starts_word(low, w)), None)
    if not hit:
        return None
    if any(starts_word(low, w) for w in rule.get("unless", [])):
        return None
    return hit


def visible(text):
    """Текст без розмітки, але з переносами рядків і пунктуацією.

    Потрібен саме такий: у сирому HTML сидять href-посилання, а в них — цифри
    (`t.me/c/1234567890123456`, id вкладень). Візерунок номера картки бачив у
    них картку і викидав звичайну новину — так 21.08.2026 зник пост
    chorleb/466 про Хартію та Єрмака, де жодних грошей не було."""
    return html_mod.unescape(re.sub(r"<[^>]+>", " ", text or ""))


def is_fundraising(text):
    """Прохання грошей: збір на канал, номер картки, банка, PayPal.

    Стоп-слова ловлять лише точну підстроку, а прохання пишуть як завгодно —
    тому тут візерунки. Повертає сам візерунок, що спрацював, або None."""
    low = visible(text).lower()
    for pat in getattr(config, "MONEY_PATTERNS", []):
        if re.search(pat, low, re.IGNORECASE):
            return pat
    return None


# Канали ховають реквізити омографами: у Звіздця «РayPal» написано кириличною
# «Р», і підстрокою "paypal" воно не знаходиться. Зводимо до латиниці.
HOMOGLYPHS = str.maketrans("аᅠвгдеєзіїкмнорстухАВГДЕЄЗІЇКМНОРСТУХ",
                           "a bcdeeзiikmhopctyxABCDEE3IIKMHOPCTYX")


def latinize(s):
    return (s or "").translate(HOMOGLYPHS)


def footer_mark(line):
    """Чи є рядок сам по собі донатним підвалом (банка, PayPal, «Ціль: 320 000»)."""
    low = (line or "").lower()
    lat = latinize(low)
    return next((w for w in getattr(config, "DONATION_FOOTER_MARKS", [])
                 if w in low or w in lat), None)


def leadin_mark(line):
    """Чи схожий рядок на підводку до реквізитів («не только лайком, но и финансово»).

    Довгий абзац підводкою НЕ вважаємо: у тексті про економіку «фінансов» і
    «гривень» — звичайні слова, і обрізка починала їсти саму новину."""
    low = (line or "").lower()
    if len(plain(line).strip()) > getattr(config, "DONATION_LEADIN_MAX_LINE", 220):
        return False
    return any(w in low for w in getattr(config, "DONATION_LEADIN", []))


def cut_donation(text):
    """Відрізає прохання грошей, приписане в кінці новини.

    Повертає сам текст, якщо різати нічого або якщо збір і Є змістом поста:
    тоді хай його ріже passes_filters цілком. Тіло після обрізки все одно
    проходить фільтри — і якщо в ньому лишились ознаки збору, пост не піде."""
    if not text:
        return text
    lines = text.split("\n")
    cut = next((i for i, ln in enumerate(lines)
                if ln.strip() and (is_fundraising(ln) or footer_mark(ln))), None)
    if not cut:                      # None або 0 — підвалу немає, або він на початку
        return text
    # Підводку до реквізитів теж прибираємо: вона частина підвалу, а не новини.
    def back(i):
        """Індекс попереднього НЕпорожнього рядка (порожні між абзацами не рахуємо)."""
        i -= 1
        while i >= 0 and not lines[i].strip():
            i -= 1
        return i

    step = 0
    while cut > 0 and step < getattr(config, "DONATION_LEADIN_MAX", 6):
        j = back(cut)
        if j < 0:
            break
        if not leadin_mark(lines[j]):
            # Короткий заголовок підвалу («Уважаемые читатели!», «Еще раз
            # подчеркиваю») лексики не має — заглядаємо за нього.
            k = back(j)
            if not (len(plain(lines[j]).strip()) <= 40 and k >= 0
                    and leadin_mark(lines[k])):
                break
        cut = j
        step += 1
    head = "\n".join(lines[:cut]).rstrip()
    body, whole = len(plain(head).strip()), len(plain(text).strip())
    if body < getattr(config, "DONATION_BODY_MIN", 400):
        return text                  # новини під підвалом немає
    if whole and body < getattr(config, "DONATION_BODY_SHARE", 0.4) * whole:
        return text                  # підвал більший за саму новину
    return close_tags(head)


def passes_filters(text, has_media, channel=None):
    if is_service(text):
        return False, "службова позначка"
    hello = is_greeting(text)
    if hello:
        return False, "побажання («%s»)" % hello
    toll = is_daily_toll(text)
    if toll:
        return False, "щоденна зведена статистика («%s»)" % toll
    low = (text or "").lower()
    theme = off_topic(low, channel)
    if theme:
        return False, "не наша тема («%s»)" % theme
    floor = getattr(config, "CHANNEL_MIN_LENGTH", {}).get(channel or "")
    if floor and len(low.strip()) < floor:
        return False, "закоротке для цього каналу"
    marker = is_alert(text)
    if marker:
        return False, "оперативка («%s»)" % marker
    money = is_fundraising(text)
    if money:
        return False, "збір грошей"
    for w in config.STOP_WORDS:
        if starts_word(low, w.lower()):     # з початку слова, без обрізання основи
            return False, "стоп-слово «%s»" % w
    if config.KEYWORDS and not any(word_in(low, k) for k in config.KEYWORDS):
        return False, "немає ключових слів"
    if not has_media and len(low.strip()) < config.MIN_LENGTH:
        return False, "закоротке"
    return True, ""


def in_quiet_hours():
    if not config.QUIET_HOURS:
        return False
    start, end = config.QUIET_HOURS
    h = datetime.now().hour
    return (start <= h or h < end) if start > end else (start <= h < end)


# ─────────────────────────────  читання каналу  ─────────────────────────────

KEEP_TAGS = {"b", "strong", "i", "em", "u", "s", "del", "code", "pre", "a"}


def clean_html(node):
    """Перетворюємо розмітку каналу на ту, яку розуміє Telegram."""
    if node is None:
        return ""
    raw = node.decode_contents()
    raw = re.sub(r"<br\s*/?>", "\n", raw)
    raw = re.sub(r"</?(div|p)[^>]*>", "\n", raw)
    raw = re.sub(r'<a[^>]*href="\?q=[^"]*"[^>]*>(.*?)</a>', r"\1", raw, flags=re.S)
    raw = re.sub(r"<tg-emoji[^>]*>(.*?)</tg-emoji>", r"\1", raw, flags=re.S)
    raw = re.sub(r"<tg-spoiler[^>]*>(.*?)</tg-spoiler>", r"\1", raw, flags=re.S)

    def strip_tag(m):
        closing, tag, attrs = m.group(1), m.group(2).lower(), m.group(3)
        if tag not in KEEP_TAGS:
            return ""
        if closing:
            return "</%s>" % tag
        if tag == "a":                       # у посилання лишаємо тільки адресу
            href = re.search(r'href="([^"]*)"', attrs)
            if not href or not href.group(1).startswith("http"):
                return ""
            return '<a href="%s">' % href.group(1)
        return "<%s>" % tag                  # решті тегів атрибути ні до чого

    raw = re.sub(r"<(/?)(\w[\w-]*)((?:[^>\"']|\"[^\"]*\"|'[^']*')*)>", strip_tag, raw)
    raw = re.sub(r"\n{3,}", "\n\n", raw)
    return raw.strip()


def message_text_node(box):
    """Текст самого поста.

    Коли пост — відповідь на інший, у розмітці ПЕРШОЮ йде урізана цитата
    того допису (`js-message_reply_text`, обривається на «…»), і лише за
    нею — власний текст (`js-message_text`). Брати перше-ліпше не можна:
    саме через це новини з «Аналітики фронту» приходили обрізаними."""
    return (box.select_one(".tgme_widget_message_text.js-message_text")
            or box.select_one(".tgme_widget_message_text:not(.js-message_reply_text)"))


def fetch_channel(channel):
    """Повертає (назва каналу, список постів). Пости — від старіших до новіших."""
    url = "https://t.me/s/%s" % channel
    r = requests.get(url, headers={"User-Agent": UA}, timeout=30)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    title_el = soup.select_one(".tgme_channel_info_header_title span") or \
        soup.select_one(".tgme_header_title span")
    title = title_el.get_text(strip=True) if title_el else channel

    posts = []
    for box in soup.select(".tgme_widget_message"):
        data_post = box.get("data-post") or ""
        m = re.search(r"/(\d+)$", data_post)
        if not m:
            continue
        pid = int(m.group(1))

        text = cut_donation(clean_html(message_text_node(box)))

        photo = None
        ph = box.select_one(".tgme_widget_message_photo_wrap")
        if ph and ph.get("style"):
            mm = re.search(r"url\('([^']+)'\)", ph["style"])
            if mm:
                photo = mm.group(1)
        if not photo:
            vt = box.select_one(".tgme_widget_message_video_thumb")
            if vt and vt.get("style"):
                mm = re.search(r"url\('([^']+)'\)", vt["style"])
                if mm:
                    photo = mm.group(1)

        vid = box.select_one("video.tgme_widget_message_video")
        video_url = vid.get("src") if vid else None
        # У списку постів частина відео віддається лише обкладинкою, без <video>.
        # Такий пост — теж відео: посилання дістанемо з окремої сторінки.
        player = box.select_one(".tgme_widget_message_video_player")
        has_video = bool(vid or player or
                         box.select_one(".tgme_widget_message_video_thumb"))
        # Telegram сам позначає частину відео як недоступні для вебу —
        # такі не віддає ні тут, ні на сторінці поста. Лишається посилання.
        web_blocked = bool(player and "not_supported" in (player.get("class") or []))
        is_gif = bool(box.select_one(".tgme_widget_message_gif"))
        is_round = bool(box.select_one(".tgme_widget_message_roundvideo"))
        dur = box.select_one(".message_video_duration")

        when = box.select_one("time")
        posts.append({
            "id": pid,
            "channel": channel,
            "text": text,
            "photo": photo,
            "video": has_video,
            "video_url": video_url,
            "video_blocked": web_blocked and not video_url,
            "gif": is_gif,
            "round": is_round,
            "duration": dur.get_text(strip=True) if dur else "",
            "link": "https://t.me/%s/%s" % (channel, pid),
            "when": when.get("datetime") if when else "",
        })
    posts.sort(key=lambda p: p["id"])
    return title, posts


# ─────────────────────────────  надсилання  ─────────────────────────────

def person_name(user_id, username=""):
    """Ім'я людини для сповіщень: спершу з налаштувань, тоді юзернейм, тоді номер."""
    name = getattr(config, "NAMES", {}).get(int(user_id or 0))
    if name:
        return name
    if username:
        return "@" + username
    return str(user_id)


def person_voc(user_id, username=""):
    """Кличний відмінок: «Миколко». Без нього — звичайне ім'я."""
    voc = getattr(config, "NAMES_VOCATIVE", {}).get(int(user_id or 0))
    return voc or person_name(user_id, username).lstrip("@")


def drop_reader(chat_id, why=""):
    """Людина заблокувала бота або видалила себе — знімаємо з потоку,
    щоб не гримати в зачинені двері. Повернеться командою /start."""
    o = owner()
    with db() as c:
        row = c.execute("SELECT username, is_owner FROM people WHERE user_id = ?",
                        (chat_id,)).fetchone()
        if not row:
            return
        c.execute("UPDATE people SET active = 0 WHERE user_id = ?", (chat_id,))
    who = person_name(chat_id, row["username"])
    log.warning("знято з потоку %s: %s", who, why[:60])
    if o and str(o["user_id"]) != str(chat_id):
        try:
            requests.post(API + "sendMessage", timeout=30, data={
                "chat_id": o["user_id"], "parse_mode": "HTML",
                "text": "ℹ️ <b>%s</b> більше не отримує новини — заблокував бота "
                        "або видалив чат. Захоче назад — хай напише боту /start."
                        % html_mod.escape(who)})
        except Exception:
            pass


def wants_silence(chat_id):
    """Власниці — зі звуком (щоб бачила, що бот живий), читачам — тихо."""
    o = owner()
    if o and str(chat_id) == str(o["user_id"]):
        return bool(getattr(config, "SILENT_OWNER", False))
    return bool(getattr(config, "SILENT_READERS", True))


def api(method, **params):
    if method in ("sendMessage", "sendPhoto") and "disable_notification" not in params:
        params["disable_notification"] = wants_silence(params.get("chat_id"))
    for attempt in range(3):
        try:
            r = requests.post(API + method, data=params, timeout=60)
            j = r.json()
            if j.get("ok"):
                return j["result"]
            desc = j.get("description", "")
            if j.get("error_code") in (400, 403) and params.get("chat_id"):
                low = desc.lower()
                if ("blocked" in low or "chat not found" in low
                        or "user is deactivated" in low or "bot was kicked" in low):
                    drop_reader(params["chat_id"], desc)
                    return None
            if j.get("error_code") == 429:
                wait = j.get("parameters", {}).get("retry_after", 5)
                log.warning("ліміт Telegram, чекаю %s с", wait)
                time.sleep(wait + 1)
                continue
            log.warning("%s не вдався: %s", method, desc)
            return None
        except Exception as e:
            log.warning("%s зірвався (%s), спроба %s", method, e, attempt + 1)
            time.sleep(3)
    return None


def grab_photo(url):
    """Telegram не ходить по адресах каналу сам — тягнемо картинку своїми руками."""
    if not url:
        return None
    try:
        r = requests.get(url, headers={"User-Agent": UA, "Referer": "https://t.me/"},
                         timeout=30)
        r.raise_for_status()
        if not r.headers.get("content-type", "").startswith("image"):
            return None
        if len(r.content) > 9 * 1024 * 1024:      # ліміт Telegram на фото
            log.info("картинка завелика (%s КБ) — шлю без неї", len(r.content) // 1024)
            return None
        return r.content
    except Exception as e:
        log.info("картинка не завантажилась: %s", e)
        return None


def resolve_video(post):
    """Якщо в списку відео без посилання — беремо його зі сторінки самого поста."""
    if post.get("video_url") or post.get("_vresolved") or post.get("video_blocked"):
        return post.get("video_url")
    post["_vresolved"] = True
    link = post.get("link") or ""
    m = re.search(r"t\.me/([^/]+)/(\d+)", link)
    if not m:
        return None
    try:
        r = requests.get("https://t.me/%s/%s?embed=1" % (m.group(1), m.group(2)),
                         headers={"User-Agent": UA}, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        vid = soup.select_one("video.tgme_widget_message_video")
        if vid and vid.get("src"):
            post["video_url"] = vid["src"]
            if soup.select_one(".tgme_widget_message_gif"):
                post["gif"] = True
            log.info("посилання на відео дотягнуто зі сторінки поста")
            return post["video_url"]
    except Exception as e:
        log.info("сторінка поста не відповіла: %s", e)
    return None


def grab_video(url):
    """Тягнемо саме відео. Завелике — відмовляємось, піде обкладинка."""
    if not url:
        return None
    limit = getattr(config, "VIDEO_MAX_MB", 45)
    try:
        head = requests.head(url, headers={"User-Agent": UA, "Referer": "https://t.me/"},
                             timeout=25, allow_redirects=True)
        size = int(head.headers.get("content-length") or 0)
        if size and size > limit * 1048576:
            log.info("відео завелике (%.1f МБ) — шлю обкладинку", size / 1048576.0)
            return None
        r = requests.get(url, headers={"User-Agent": UA, "Referer": "https://t.me/"},
                         timeout=180)
        r.raise_for_status()
        if len(r.content) > limit * 1048576:
            return None
        return r.content
    except Exception as e:
        log.info("відео не завантажилось: %s", e)
        return None


def send_media(uid, method, field, blob, filename, mime, caption, file_id=None):
    """Надсилаємо файл. Повертає file_id — щоб решті читачів не качати знову."""
    global LAST_MEDIA_MSG
    for attempt in range(2):
        try:
            if file_id:
                r = requests.post(API + method, timeout=120, data={
                    "chat_id": uid, field: file_id, "caption": caption,
                    "parse_mode": "HTML",
                    "disable_notification": wants_silence(uid)})
            else:
                r = requests.post(API + method, timeout=300,
                                  data={"chat_id": uid, "caption": caption,
                                        "parse_mode": "HTML",
                                        "disable_notification": wants_silence(uid)},
                                  files={field: (filename, blob, mime)})
            j = r.json()
            if j.get("ok"):
                res = j["result"]
                LAST_MEDIA_MSG = res.get("message_id")
                got = res.get(field) or res.get("document")
                if isinstance(got, list):
                    got = got[-1]
                return (got or {}).get("file_id", "") if isinstance(got, dict) else ""
            if j.get("error_code") == 429:
                time.sleep(j.get("parameters", {}).get("retry_after", 5) + 1)
                continue
            log.warning("%s не вдався: %s", method, j.get("description"))
            return None
        except Exception as e:
            log.warning("%s зірвався: %s", method, e)
            time.sleep(3)
    return None


def send_photo(uid, blob, caption):
    for attempt in range(2):
        try:
            r = requests.post(API + "sendPhoto",
                              data={"chat_id": uid, "caption": caption,
                                    "parse_mode": "HTML",
                                    "disable_notification": wants_silence(uid)},
                              files={"photo": ("news.jpg", blob, "image/jpeg")},
                              timeout=120)
            j = r.json()
            if j.get("ok"):
                return True
            if j.get("error_code") == 429:
                time.sleep(j.get("parameters", {}).get("retry_after", 5) + 1)
                continue
            log.warning("sendPhoto не вдався: %s", j.get("description"))
            return False
        except Exception as e:
            log.warning("sendPhoto зірвався: %s", e)
            time.sleep(3)
    return False


def cut_pos(body, room):
    """Де саме різати: межа абзацу чи речення, ніколи не всередині тега."""
    if len(body) <= room:
        return len(body)
    cut = body[:room]
    lt, gt = cut.rfind("<"), cut.rfind(">")
    if lt > gt:                             # ліміт випав усередину тега
        cut = cut[:lt]
    for sep in ("\n\n", "\n", ". ", "! ", "? ", "… ", "; ", ", ", " "):
        i = cut.rfind(sep)
        if i > len(cut) * 0.45:             # не ріжемо надто коротко
            return i + len(sep)
    if cut:
        return len(cut)
    # Сюди потрапляємо, коли сам тег довший за ліміт — приміром величезне
    # посилання на початку поста. Різати всередині тега не можна, а віддати
    # нуль означає, що розріз не зсунеться і цикл нарубає порожніх частин.
    # Тому відступаємо до кінця тега: перевищимо ліміт на довжину href, зате
    # зрушимо з місця (Telegram href у довжину повідомлення не рахує).
    end = body.find(">", lt if lt >= 0 else 0)
    if end == -1:
        return len(body)
    # Стати одразу за тегом мало: тоді в частині сам лише тег, і читач
    # отримає порожнє повідомлення. Перескочивши тег, добираємо ще тексту.
    return end + 1 + cut_pos(body[end + 1:], room)


def smart_cut(body, room):
    """Обрізаний текст і ознака, чи різали взагалі."""
    body = (body or "").strip()
    pos = cut_pos(body, room)
    if pos >= len(body):
        return body, False
    return close_tags(body[:pos].rstrip()), True


def open_stack(fragment):
    """Які теги лишились відкритими на місці розрізу (з їхніми атрибутами)."""
    stack = []
    for m in re.finditer(r"<(/?)(\w[\w-]*)([^>]*)>", fragment):
        closing, tag = m.group(1), m.group(2).lower()
        if tag not in KEEP_TAGS:
            continue
        if closing:
            for i in range(len(stack) - 1, -1, -1):
                if stack[i][0] == tag:
                    stack.pop(i)
                    break
        else:
            stack.append((tag, m.group(0)))
    return stack


def split_messages(title, body, link, first_limit, rest_limit=4096, max_parts=8):
    """Ріжемо довгий пост на кілька повідомлень, не втрачаючи ані слова.

    Розрив може випасти всередину <b> чи <a> — тому на місці розрізу теги
    закриваємо, а на початку наступної частини відкриваємо знову."""
    head = "📡 <b>%s</b>\n\n" % html_mod.escape(short_title(title))
    tail = '\n\n<a href="%s">↗ оригінал</a>' % link
    body = (body or "").strip()
    parts, rest, carry = [], body, ""

    while True:
        first = not parts
        prefix = head if first else ""
        limit = (first_limit if first else rest_limit) - len(prefix) - len(tail) - 30
        piece = carry + rest
        if len(piece) <= limit:
            parts.append(prefix + piece)
            break
        if len(parts) + 1 >= max_parts:
            # Довше вже не ріжемо, але й за межу лізти не можна:
            # обрізаємо останню частину й кажемо, де читати решту.
            pos = cut_pos(piece, limit - 40)
            raw = piece[:pos].rstrip()
            stack = open_stack(raw)
            parts.append(prefix + raw
                         + "".join("</%s>" % t for t, _ in reversed(stack))
                         + "\n\n<i>…решта за посиланням</i>")
            break
        pos = cut_pos(piece, limit)
        if pos <= 0:                        # запобіжник: розріз не зсунувся
            parts.append(prefix + piece)    # краще одне довге, ніж вісім порожніх
            break
        raw = piece[:pos].rstrip()
        stack = open_stack(raw)
        closed = raw + "".join("</%s>" % t for t, _ in reversed(stack))
        parts.append(prefix + closed)
        carry = "".join(full for _, full in stack)
        rest = piece[pos:].lstrip()
        if len(carry) > 300:
            # Величезний href (буває на початку поста) тягнути в кожну частину
            # немає сенсу. Тег не переносимо — тоді й його закривач у залишку
            # стає сиротою, а на сиротах Telegram відбиває все повідомлення.
            for tag, _ in stack:
                rest = rest.replace("</%s>" % tag, "", 1)
            carry = ""
        if not rest:
            break

    parts[-1] += tail
    if len(parts) > 1:
        parts = ["%s\n\n<i>%s з %s</i>" % (p, i + 1, len(parts)) if i < len(parts) - 1
                 else p for i, p in enumerate(parts)]
    return parts


def short_title(title):
    """Коротке ім'я каналу для заголовка.

    Канали люблять писати в назві перелік міст чи гасло — у заголовку новини
    це зайве. Беремо частину до роздільника й обрізаємо."""
    t = (title or "").strip()
    for sep in (":", "|", "—", "–", "•", " l ", " — "):
        if sep in t:
            head = t.split(sep)[0].strip()
            if len(head) >= 4:
                t = head
                break
    if len(t) > 40:
        t = t[:40].rsplit(" ", 1)[0] + "…"
    return t


def build_text(title, body, link, limit):
    head = "📡 <b>%s</b>" % html_mod.escape(short_title(title))
    tail = '\n\n<a href="%s">↗ оригінал</a>' % link
    body, was_cut = smart_cut(body, limit - len(head) - len(tail) - 40)
    if was_cut:
        body += "\n\n<i>…далі за посиланням</i>"
    return head + ("\n\n" + body if body else "") + tail


def close_tags(s):
    """Після обрізки закриваємо теги, що лишились відкритими."""
    opened = []
    for m in re.finditer(r"<(/?)(\w+)[^>]*>", s):
        tag = m.group(2).lower()
        if tag not in KEEP_TAGS:
            continue
        if m.group(1):
            if opened and opened[-1] == tag:
                opened.pop()
            elif tag in opened:
                opened.remove(tag)
        else:
            opened.append(tag)
    s = re.sub(r"<[^>]*$", "", s)          # прибрати обірваний тег
    return s + "".join("</%s>" % t for t in reversed(opened))


# ───────────────  канал виправив уже надіслану новину  ───────────────

LAST_MEDIA_MSG = None       # номер останнього повідомлення з файлом


def plain(text):
    """Сам текст без розмітки: Telegram час від часу віддає ті самі слова
    трохи іншими тегами, а це не виправлення каналу."""
    return norm(re.sub(r"<[^>]+>", " ", text or ""))


def verbatim(text):
    """Текст без розмітки, але слово в слово: із розділовими знаками, регістром
    і посиланнями. plain() усе це знімає — і правка «затримали?» → «затримали!»
    або «не о 10:00, а о 10:30» лишалась непоміченою. Зайві пробіли й переноси
    рядків не рахуємо: канал міг просто переверстати абзац."""
    t = html_mod.unescape(re.sub(r"<[^>]+>", " ", text or ""))
    return re.sub(r"\s+", " ", t).strip()


def text_hash(text):
    """Відбиток тексту поста — за ним бачимо, що канал його переписав."""
    return hashlib.sha1(verbatim(text).encode("utf-8")).hexdigest()


def post_ref(post):
    """(канал, номер поста). Відлежаний сюжет номера не несе — беремо з посилання."""
    ch = post.get("channel") or ""
    pid = int(post.get("id") or 0)
    m = re.search(r"t\.me/([^/]+)/(\d+)", post.get("link") or "")
    if m:
        ch = ch or m.group(1)
        pid = pid or int(m.group(2))
    return ch, pid


def video_mark(post):
    """Позначка «відео за посиланням», яку дописуємо в кінець тексту."""
    if not post.get("video"):
        return ""
    if post.get("duration"):
        return "\n\n🎬 <i>відео (%s) — за посиланням нижче</i>" % post["duration"]
    return "\n\n🎬 <i>відео — за посиланням нижче</i>"


def remember_sent(post, title, uid, msgs, body="", first_limit=4096, suffix=""):
    """Запам'ятовуємо, чим саме віддали новину — щоб донести правку каналу."""
    if not getattr(config, "WATCH_EDITS", True):
        return
    ch, pid = post_ref(post)
    if not pid or not msgs:
        return
    now = int(time.time())
    with db() as c:
        c.execute("INSERT OR REPLACE INTO sent "
                  "(channel, post_id, title, hash, body, suffix, first_limit, ts) "
                  "VALUES (?,?,?,?,?,?,?,?)",
                  (ch, pid, title, text_hash(post.get("text")), body, suffix,
                   int(first_limit), now))
        c.execute("DELETE FROM sent_msg WHERE channel = ? AND post_id = ? AND user_id = ?",
                  (ch, pid, uid))
        for mid, kind, part in msgs:
            c.execute("INSERT INTO sent_msg "
                      "(channel, post_id, user_id, message_id, part, kind) "
                      "VALUES (?,?,?,?,?,?)", (ch, pid, uid, mid, part, kind))


def edit_is_big(old, new):
    """Чи це справжнє виправлення, а не прибрана кома.

    Дрібницю правимо мовчки: у тексті вона з'явиться, але читача не смикаємо.
    Рахуємо по знаках, а не по словах: у короткій новині одне слово — це вже
    шоста частина тексту, а «протиправним» замість «правомірним» саме одним
    словом і робиться."""
    a, b = plain(old), plain(new)
    if a == b:
        return False
    if abs(len(b) - len(a)) >= getattr(config, "EDIT_NOTE_CHARS", 60):
        return True
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    return ratio < getattr(config, "EDIT_NOTE_RATIO", 0.97)


def push_edit(row, post, title):
    """Доводимо виправлення до тих, кому новина вже пішла."""
    with db() as c:
        msgs = [dict(r) for r in c.execute(
            "SELECT * FROM sent_msg WHERE channel = ? AND post_id = ? "
            "ORDER BY user_id, part", (row["channel"], row["post_id"]))]
    if not msgs:
        return False

    body = ((post.get("text") or "") + (row["suffix"] or "")).strip()
    parts = split_messages(title, body, post["link"],
                           first_limit=int(row["first_limit"] or 4096))
    big = edit_is_big(row["body"] or "", body) and getattr(config, "EDIT_NOTICE", True)

    by_user = {}
    for m in msgs:
        by_user.setdefault(m["user_id"], []).append(m)

    done = 0
    for uid, mine in by_user.items():
        mine.sort(key=lambda m: m["part"])
        # Правимо на місці лише тоді, коли частин рівно стільки ж:
        # інакше кінець новини лишиться від старої версії.
        fixed_in_place = len(parts) == len(mine)
        if fixed_in_place:
            for m, chunk in zip(mine, parts):
                if m["kind"] == "caption":
                    res = api("editMessageCaption", chat_id=uid,
                              message_id=m["message_id"], caption=chunk,
                              parse_mode="HTML")
                else:
                    res = api("editMessageText", chat_id=uid,
                              message_id=m["message_id"], text=chunk,
                              parse_mode="HTML", disable_web_page_preview=True)
                if res is None:
                    fixed_in_place = False
                    break
                time.sleep(0.3)

        if fixed_in_place:
            if big:
                api("sendMessage", chat_id=uid,
                    text="✏️ <b>Канал виправив цю новину</b> — текст вище оновлено.",
                    parse_mode="HTML", disable_web_page_preview=True,
                    reply_to_message_id=mine[0]["message_id"],
                    allow_sending_without_reply=True)
            done += 1
        elif big:
            # На місці не вийшло (інша кількість частин або застаре
            # повідомлення) — шлемо виправлену новину окремо.
            # Мовчки (EDIT_NOTICE = False) сюди не заходимо взагалі.
            again = split_messages("✏️ ВИПРАВЛЕНО · " + short_title(title),
                                   body, post["link"], first_limit=4096)
            for j, chunk in enumerate(again):
                if api("sendMessage", chat_id=uid, text=chunk, parse_mode="HTML",
                       disable_web_page_preview=True,
                       reply_to_message_id=mine[0]["message_id"],
                       allow_sending_without_reply=True):
                    done += 1 if j == 0 else 0
                time.sleep(0.4)
        time.sleep(config.SEND_DELAY)
    return done > 0


def check_edits(channel, posts, title=""):
    """Звіряємо надіслані пости з тим, що зараз у каналі."""
    if not getattr(config, "WATCH_EDITS", True):
        return 0
    hours = int(getattr(config, "EDIT_HOURS", 24))
    now = int(time.time())
    with db() as c:
        c.execute("DELETE FROM sent_msg WHERE EXISTS (SELECT 1 FROM sent s "
                  "WHERE s.channel = sent_msg.channel AND s.post_id = sent_msg.post_id "
                  "AND s.ts < ?)", (now - hours * 3600,))
        c.execute("DELETE FROM sent WHERE ts < ?", (now - hours * 3600,))
        watched = {r["post_id"]: dict(r) for r in
                   c.execute("SELECT * FROM sent WHERE channel = ?", (channel,))}
    if not watched:
        return 0

    fixed = 0
    for post in posts:
        row = watched.get(post.get("id"))
        if not row:
            continue
        new_hash = text_hash(post.get("text"))
        if new_hash == row["hash"]:
            continue
        body = ((post.get("text") or "") + (row["suffix"] or "")).strip()
        if verbatim(body) == verbatim(row["body"] or ""):
            # Відбиток інший, а текст той самий — так буває один раз після
            # оновлення коду, коли в базі лежать відбитки старого зразка.
            # Читача не чіпаємо, просто перезаписуємо позначку.
            with db() as c:
                c.execute("UPDATE sent SET hash = ? WHERE channel = ? AND post_id = ?",
                          (new_hash, channel, post["id"]))
            continue
        big = edit_is_big(row["body"] or "", body)
        log.info("%s/%s: канал виправив пост (%s)", channel, post["id"],
                 "суттєво" if big else "дрібниця")
        try:
            if push_edit(row, post, title or row["title"]):
                fixed += 1
        except Exception as e:
            log.warning("виправлення %s/%s не пішло: %s", channel, post["id"], e)
        with db() as c:
            c.execute("UPDATE sent SET hash = ?, body = ? WHERE channel = ? AND post_id = ?",
                      (new_hash, body, channel, post["id"]))
    return fixed


def send_post(uid, post, title):
    global LAST_MEDIA_MSG
    body = post["text"] or ""
    msgs = []                      # чим саме віддали новину: номери повідомлень

    # 1) справжнє відео, якщо дістали
    if post.get("video") and not post.get("video_url") and "_novideo" not in post:
        resolve_video(post)
    if post.get("video_url") and "_novideo" not in post:
        blob = post.get("_vblob")
        if blob is None and not post.get("_vid_id"):
            blob = grab_video(post["video_url"])
            if blob is None:
                post["_novideo"] = True
            else:
                post["_vblob"] = blob
        if post.get("_vid_id") or post.get("_vblob"):
            method, field = ("sendAnimation", "animation") if post.get("gif") \
                else ("sendVideo", "video")
            parts = split_messages(title, body, post["link"], first_limit=1024)
            LAST_MEDIA_MSG = None
            fid = send_media(uid, method, field, post.get("_vblob"),
                             "news.mp4", "video/mp4", parts[0],
                             file_id=post.get("_vid_id"))
            if fid is not None:
                if fid:
                    post["_vid_id"] = fid
                    post["_vblob"] = None      # далі шлемо за посиланням Telegram
                if LAST_MEDIA_MSG:
                    msgs.append((LAST_MEDIA_MSG, "caption", 0))
                for i, chunk in enumerate(parts[1:]):
                    res = api("sendMessage", chat_id=uid, text=chunk, parse_mode="HTML",
                              disable_web_page_preview=True)
                    if not res:
                        return False
                    if isinstance(res, dict) and res.get("message_id"):
                        msgs.append((res["message_id"], "text", i + 1))
                    time.sleep(0.4)
                remember_sent(post, title, uid, msgs, body=body, first_limit=1024)
                return True
            log.info("відео не пішло — пробую обкладинку")
            post["_novideo"] = True

    # 2) не вийшло — обкладинка чи просто текст
    suffix = video_mark(post)
    if suffix:
        body = (body + suffix).strip()

    blob = None
    if post["photo"] and not post.get("_ph_id"):
        blob = post.get("_blob") or grab_photo(post["photo"])
        if blob:
            post["_blob"] = blob

    first_limit = 1024 if (blob or post.get("_ph_id")) else 4096
    parts = split_messages(title, body, post["link"], first_limit=first_limit)

    base = 0
    if blob or post.get("_ph_id"):
        LAST_MEDIA_MSG = None
        fid = send_media(uid, "sendPhoto", "photo", blob, "news.jpg", "image/jpeg",
                         parts[0], file_id=post.get("_ph_id"))
        if fid is None:
            log.info("фото не пішло — шлю самим текстом")
            first_limit = 4096
            parts = split_messages(title, body, post["link"], first_limit=4096)
        else:
            if fid:
                post["_ph_id"] = fid
                post["_blob"] = None
            if LAST_MEDIA_MSG:
                msgs.append((LAST_MEDIA_MSG, "caption", 0))
            parts = parts[1:]
            base = 1

    for i, chunk in enumerate(parts):
        res = api("sendMessage", chat_id=uid, text=chunk, parse_mode="HTML",
                  disable_web_page_preview=True)
        if not res:
            return False
        if isinstance(res, dict) and res.get("message_id"):
            msgs.append((res["message_id"], "text", base + i))
        if i + 1 < len(parts):
            time.sleep(0.4)
    remember_sent(post, title, uid, msgs, body=body, first_limit=first_limit,
                  suffix=suffix)
    return True


def broadcast(post, title):
    people = readers()
    if not people:
        enqueue(post, title)
        return False
    for p in people:
        send_post(p["user_id"], post, title)
        time.sleep(config.SEND_DELAY)
    bump_unread()
    # Пишемо тут, а не в місцях виклику: інакше в журналі видно самі відсіви,
    # а підтверджень доставки нема жодного.
    log.info("надіслано (%s чит.): %s — %s", len(people), post["channel"], post["link"])
    return True


def bump_unread():
    """Рахуємо, скільки новин пішло від останнього нагадування."""
    try:
        set_state("unread", int(get_state("unread", 0) or 0) + 1)
    except ValueError:
        set_state("unread", 1)


def enqueue(post, title):
    with db() as c:
        c.execute("INSERT INTO queue (channel, title, link, text, photo, ts) "
                  "VALUES (?,?,?,?,?,?)",
                  (post["channel"], title, post["link"],
                   (post["text"] or "")[:1200], post["photo"] or "", int(time.time())))
    log.info("у чергу: %s — %s", post["channel"], post["link"])


def flush_queue(reason=""):
    with db() as c:
        rows = [dict(r) for r in c.execute("SELECT * FROM queue ORDER BY id")]
        if not rows:
            return 0
        c.execute("DELETE FROM queue")
    people = readers()
    if not people:
        return 0

    chunks, cur = [], "🗞 <b>Зведення новин</b> %s\n" % html_mod.escape(reason)
    for r in rows:
        piece = '\n<b>%s</b>\n%s\n<a href="%s">↗ оригінал</a>\n' % (
            html_mod.escape(short_title(r["title"])),
            close_tags((r["text"] or "")[:400].strip()),
            r["link"])
        if len(cur) + len(piece) > 3800:
            chunks.append(cur)
            cur = ""
        cur += piece
    chunks.append(cur)

    for p in people:
        for ch in chunks:
            api("sendMessage", chat_id=p["user_id"], text=ch,
                parse_mode="HTML", disable_web_page_preview=True)
            time.sleep(config.SEND_DELAY)
    for _ in rows:
        bump_unread()
    log.info("зведення: %s постів у %s повідомленнях", len(rows), len(chunks))
    return len(rows)


# ─────────────────────────────  головний обхід  ─────────────────────────────

def round_trip():
    if paused:
        return
    for src in sources():
        ch = src["channel"]
        try:
            title, posts = fetch_channel(ch)
        except Exception as e:
            log.warning("канал %s не відповів: %s", ch, e)
            continue

        if title != src["title"]:
            with db() as c:
                c.execute("UPDATE sources SET title = ? WHERE channel = ?", (title, ch))

        # Канал міг виправити вже надісланий пост — доводимо правку читачам.
        try:
            check_edits(ch, posts, title)
        except Exception as e:
            log.warning("перевірка виправлень у %s зірвалась: %s", ch, e)

        fresh = [p for p in posts if p["id"] > (src["last_id"] or 0)]
        if not fresh:
            continue

        if not src["last_id"]:          # перше знайомство — історію не шлемо
            with db() as c:
                c.execute("UPDATE sources SET last_id = ? WHERE channel = ?",
                          (posts[-1]["id"], ch))
            log.info("%s: перший огляд, запам'ятав позицію #%s", title, posts[-1]["id"])
            continue

        if len(fresh) > config.MAX_PER_ROUND:
            log.info("%s: %s нових, беру останні %s",
                     title, len(fresh), config.MAX_PER_ROUND)
            fresh = fresh[-config.MAX_PER_ROUND:]

        for post in fresh:
            ok, why = passes_filters(post["text"], bool(post["photo"] or post["video"]), ch)
            if not ok:
                log.info("пропуск %s/%s: %s", ch, post["id"], why)
                continue
            n = norm(post["text"])
            key = ("t:" + hashlib.sha1(n[:300].encode("utf-8")).hexdigest()
                   if len(n) >= 60 else "m:%s:%s" % (ch, post["id"]))
            if seen_before(key):
                log.info("дубль %s/%s — не шлю", ch, post["id"])
                continue
            same = already_told(post["text"])
            if same:
                log.info("схоже на вже надіслане з %s (збіг %.0f%%) — %s/%s пропускаю",
                         same[0], same[1] * 100, ch, post["id"])
                continue

            if config.HOLD_MINUTES:
                hold(post, title)
                log.info("притримую %s/%s — чекаю на докладнішу версію", ch, post["id"])
            elif config.MODE == "digest" or in_quiet_hours():
                enqueue(post, title)
            else:
                broadcast(post, title)
                remember(ch, post["text"])

        with db() as c:
            c.execute("UPDATE sources SET last_id = ? WHERE channel = ?",
                      (max(p["id"] for p in fresh), ch))


def maybe_remind():
    """Пару разів на день — дзенькнути читачам, що є що почитати."""
    times = getattr(config, "REMINDER_TIMES", [])
    if not times or in_quiet_hours():
        return
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")
    for t in times:
        try:
            hh, mm = [int(x) for x in t.split(":")]
        except ValueError:
            continue
        target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        waited = (now - target).total_seconds()
        if not (0 <= waited <= 1200):          # у межах 20 хвилин після часу
            continue
        key = "remind:%s:%s" % (today, t)
        if get_state(key):
            continue

        unread = int(get_state("unread", 0) or 0)
        if unread < getattr(config, "REMINDER_MIN_NEWS", 1):
            # Позначку не ставимо: новини можуть з'явитись за кілька хвилин,
            # і тоді нагадування ще встигне спрацювати у своєму вікні.
            return
        set_state(key, "1")
        set_state("unread", 0)

        word = "новина" if unread % 10 == 1 and unread % 100 != 11 else (
            "новини" if 2 <= unread % 10 <= 4 and not 12 <= unread % 100 <= 14 else "новин")
        tpl = getattr(config, "REMINDER_TEXTS", {}).get(t) or getattr(
            config, "REMINDER_TEXT",
            "📬 <b>{name}, зайди почитати новини</b>\n\nНазбиралося {count} {word}.")
        for p in readers():
            if p["is_owner"] and not getattr(config, "REMINDER_OWNER", False):
                continue
            name = person_name(p["user_id"], p["username"]).lstrip("@")
            api("sendMessage", chat_id=p["user_id"],
                text=tpl.format(name=html_mod.escape(name),
                                voc=html_mod.escape(person_voc(p["user_id"], p["username"])),
                                count=unread, word=word),
                parse_mode="HTML", disable_notification=False)
            time.sleep(config.SEND_DELAY)
        log.info("нагадування розіслано (%s новин)", unread)
        return


def watcher():
    was_quiet = in_quiet_hours()
    last_digest = None
    while True:
        try:
            with _lock:
                round_trip()
                if config.HOLD_MINUTES:
                    release_ready()
                maybe_remind()

            quiet = in_quiet_hours()
            if was_quiet and not quiet:
                flush_queue("(за ніч)")
            was_quiet = quiet

            now = datetime.now().strftime("%H:%M")
            if (config.MODE == "digest" and now in config.DIGEST_TIMES
                    and now != last_digest and not quiet):
                last_digest = now
                flush_queue("(%s)" % now)
        except Exception as e:
            log.error("збій обходу: %s", e)
        time.sleep(config.POLL_SECONDS)


# ─────────────────────────────  команди бота  ─────────────────────────────

def is_allowed(uid, uname):
    with db() as c:
        if c.execute("SELECT 1 FROM people WHERE user_id = ?", (uid,)).fetchone():
            return True
        for key in ((uname or "").lower(), str(uid)):
            if key and c.execute("SELECT 1 FROM allowed WHERE username = ?",
                                 (key,)).fetchone():
                return True
    return False


def is_owner(uid):
    o = owner()
    return bool(o) and o["user_id"] == uid


def reply(chat, text):
    api("sendMessage", chat_id=chat, text=text, parse_mode="HTML",
        disable_web_page_preview=True)


def handle_command(msg):
    text = (msg.get("text") or "").strip()
    if not text.startswith("/"):
        return
    frm = msg.get("from", {})
    uid = frm.get("id")
    uname = (frm.get("username") or "").lower()
    chat = msg["chat"]["id"]
    cmd = text.split()[0].split("@")[0].lower()
    arg = text[len(text.split()[0]):].strip()

    if cmd == "/start":
        o = owner()
        if o is None:                      # перший, хто прийшов, — господиня
            with db() as c:
                c.execute("INSERT OR REPLACE INTO people "
                          "(user_id, username, is_owner, active) VALUES (?,?,1,1)",
                          (uid, uname))
            reply(chat, "👑 Вітаю, господине. Ви тепер керуєте Новинарем.\n\n"
                        "Новини почнуть приходити сюди.\n"
                        "Друга впустите так: він тисне Start у бота, а ви — "
                        "<code>/allow @його_юзернейм</code>\n\n"
                        "/help — усі команди")
            log.info("власниця: @%s (%s)", uname, uid)
            return
        code = getattr(config, "INVITE_CODE", "")
        if code and arg.strip() == code and not is_allowed(uid, uname):
            with db() as c:
                used = c.execute("SELECT COUNT(*) n FROM people "
                                 "WHERE is_owner = 0").fetchone()["n"]
            if used < getattr(config, "INVITE_LIMIT", 2):
                with db() as c:
                    c.execute("INSERT OR REPLACE INTO people "
                              "(user_id, username, is_owner, active) VALUES (?,?,0,1)",
                              (uid, uname))
                    c.execute("INSERT OR REPLACE INTO allowed (username, ts) "
                              "VALUES (?,?)", (uname or str(uid), int(time.time())))
                reply(chat, "Готово! 🎉 Новини приходитимуть сюди самі.\n\n"
                            "Набридне — напиши /stop, повернутись — /start.")
                log.info("за перепусткою підключився @%s (%s)", uname, uid)
                who = person_name(uid, uname)
                reply(o["user_id"], "✅ <b>%s</b> підключився за посиланням. "
                                    "Нічого робити не треба."
                      % html_mod.escape(who))
                return
            log.warning("перепустку вичерпано, стукав @%s (%s)", uname, uid)

        if not is_allowed(uid, uname):
            reply(chat, "Цей бот приватний. Якщо це помилка — напишіть власниці.")
            log.warning("чужий стукав: @%s (%s)", uname, uid)
            who = person_name(uid, uname)
            reply(o["user_id"], "🔔 У бота стукав <b>%s</b> (<code>%s</code>).\n"
                                "Свій — впустіть: <code>/allow %s</code>"
                  % (html_mod.escape(who), uid, who))
            return
        with db() as c:
            c.execute("INSERT OR REPLACE INTO people "
                      "(user_id, username, is_owner, active) VALUES (?,?,0,1)",
                      (uid, uname))
        reply(chat, "Готово. Новини приходитимуть сюди.\n"
                    "/stop — вимкнути, /digest — віддати накопичене.")
        log.info("підписався @%s (%s)", uname, uid)
        return

    if not is_allowed(uid, uname):
        return

    if cmd == "/stop":
        with db() as c:
            c.execute("UPDATE people SET active = 0 WHERE user_id = ?", (uid,))
        reply(chat, "Зупинив. /start — щоб знову ввімкнути.")

    elif cmd == "/status":
        srcs = sources()
        with db() as c:
            q = c.execute("SELECT COUNT(*) n FROM queue").fetchone()["n"]
        folks = ", ".join("ви" if p["is_owner"]
                          else html_mod.escape(person_name(p["user_id"], p["username"]))
                          for p in readers()) or "нікого"
        reply(chat, "<b>Новинар живий.</b>\nРежим: <code>%s</code>%s\n"
                    "Каналів: %s | У черзі: %s\nОтримують: %s\n\n%s"
              % (config.MODE, "  ⏸ пауза" if paused else "", len(srcs), q, folks,
                 "\n".join("• %s" % html_mod.escape(short_title(s["title"] or s["channel"]))
                           for s in srcs) or "— порожньо —"))

    elif cmd == "/digest":
        if not flush_queue("(на запит)"):
            reply(chat, "Черга порожня.")

    elif cmd == "/help":
        reply(chat, "<b>Новинар</b>\n"
                    "/status — стан\n/digest — віддати накопичене\n"
                    "/stop, /start — вимкнути / увімкнути себе\n"
                    "— лише для власниці —\n"
                    "/лист <i>текст</i> — передати особисте слово читачам\n"
                    "/add @канал, /del @канал\n"
                    "/allow @людина, /deny @людина\n/pause, /resume")

    elif cmd in ("/add", "/del", "/allow", "/deny", "/pause", "/resume",
                 "/лист", "/напиши"):
        if not is_owner(uid):
            reply(chat, "Це може лише власниця.")
            return
        handle_owner_command(chat, cmd, arg)


def handle_owner_command(chat, cmd, arg):
    global paused
    if cmd in ("/лист", "/напиши"):
        return send_letter(chat, arg)
    if cmd == "/pause":
        paused = True
        reply(chat, "Призупинив збір. /resume — продовжити.")
    elif cmd == "/resume":
        paused = False
        reply(chat, "Знову збираю.")
    elif cmd == "/add":
        ch = arg.lstrip("@").strip().rstrip("/").split("/")[-1]
        if not ch:
            reply(chat, "Напишіть так: <code>/add @назва_каналу</code>")
            return
        try:
            title, posts = fetch_channel(ch)
        except Exception as e:
            reply(chat, "Не дістаю такий канал: <code>%s</code>" % html_mod.escape(str(e)))
            return
        if not posts:
            reply(chat, "За адресою <b>@%s</b> постів не видно. Буває, коли канал "
                        "закритий, порожній або в назві помилка." % html_mod.escape(ch))
            return
        with db() as c:
            c.execute("INSERT OR REPLACE INTO sources "
                      "(channel, title, last_id, active) VALUES (?,?,?,1)",
                      (ch, title, posts[-1]["id"] if posts else 0))
        reply(chat, "Додав: <b>%s</b>" % html_mod.escape(title))
    elif cmd == "/del":
        ch = arg.lstrip("@").strip().lower()
        with db() as c:
            n = c.execute("UPDATE sources SET active = 0 WHERE lower(channel) = ? "
                          "OR lower(title) = ?", (ch, ch)).rowcount
        reply(chat, "Прибрав." if n else "Такого джерела не знайшов.")
    elif cmd == "/allow":
        who = arg.lstrip("@").strip().lower()
        if not who:
            reply(chat, "Напишіть так: <code>/allow @юзернейм</code>")
            return
        with db() as c:
            c.execute("INSERT OR REPLACE INTO allowed (username, ts) VALUES (?,?)",
                      (who, int(time.time())))
        reply(chat, "Впустив <b>@%s</b>. Хай тисне Start у бота." % html_mod.escape(who))
    elif cmd == "/deny":
        who = arg.lstrip("@").strip().lower()
        with db() as c:
            c.execute("DELETE FROM allowed WHERE username = ?", (who,))
            c.execute("UPDATE people SET active = 0 WHERE lower(username) = ? "
                      "AND is_owner = 0", (who,))
        reply(chat, "Відрізав <b>@%s</b> від потоку." % html_mod.escape(who))


def send_letter(chat, text):
    """Особисте слово від власниці — бот передає його читачам."""
    text = (text or "").strip()
    if not text:
        reply(chat, "Напишіть так: <code>/лист Скучаю за тобою</code>\n"
                    "Бот передасть це тим, хто отримує новини.")
        return
    if len(text) > 3500:
        reply(chat, "Задовге — до 3500 знаків.")
        return

    body = "💛 " + html_mod.escape(text)
    got = []
    for p in readers():
        if p["is_owner"]:
            continue
        if api("sendMessage", chat_id=p["user_id"], text=body,
               parse_mode="HTML", disable_notification=False):
            got.append(person_name(p["user_id"], p["username"]))
        time.sleep(config.SEND_DELAY)

    if got:
        reply(chat, "✅ Надіслано — %s:\n\n%s" % (
            ", ".join(html_mod.escape(g) for g in got), body))
        log.info("лист передано: %s", ", ".join(got))
    else:
        reply(chat, "Нема кому передати — жоден читач не підключений.")


def listener():
    offset = int(get_state("offset", 0) or 0)
    while True:
        try:
            upd = api("getUpdates", offset=offset, timeout=30) or []
            for u in upd:
                offset = u["update_id"] + 1
                set_state("offset", offset)
                msg = u.get("message") or u.get("edited_message")
                if msg:
                    try:
                        handle_command(msg)
                    except Exception as e:
                        log.error("команда впала: %s", e)
        except Exception as e:
            log.error("слухач збоїть: %s", e)
            time.sleep(5)


# ─────────────────────────────  старт  ─────────────────────────────

def bootstrap_people():
    """Власниця й читачі, задані наперед, — щоб не залежати від пам'яті."""
    with db() as c:
        if getattr(config, "OWNER_ID", 0):
            c.execute("INSERT OR IGNORE INTO people "
                      "(user_id, username, is_owner, active) VALUES (?,'',1,1)",
                      (config.OWNER_ID,))
            c.execute("UPDATE people SET is_owner = 1, active = 1 WHERE user_id = ?",
                      (config.OWNER_ID,))
        for uid in getattr(config, "READERS", []):
            c.execute("INSERT OR IGNORE INTO people "
                      "(user_id, username, is_owner, active) VALUES (?,'',0,1)", (uid,))


def once():
    """Один прохід: розібрати команди, обійти канали, віддати відлежане."""
    upd = api("getUpdates", offset=int(get_state("offset", 0) or 0), timeout=0) or []
    now = time.time()
    stale = 0
    for u in upd:
        set_state("offset", u["update_id"] + 1)
        msg = u.get("message") or u.get("edited_message")
        if not msg:
            continue
        # Дуже старі команди (доба й більше) не виконуємо: могли накопичитись,
        # поки бот стояв. Свіжі — обробляємо завжди, навіть після втрати пам'яті.
        if now - float(msg.get("date") or now) > 86400:
            stale += 1
            continue
        try:
            handle_command(msg)
        except Exception as e:
            log.error("команда впала: %s", e)
    if stale:
        log.info("пропущено застарілих команд: %s", stale)
    round_trip()
    if config.HOLD_MINUTES:
        release_ready()
    if not in_quiet_hours():
        flush_queue("(за минулий час)")
    maybe_remind()


def bootstrap_sources():
    with db() as c:
        known = {r["channel"].lower() for r in c.execute("SELECT channel FROM sources")}
        for ch in config.SOURCES:
            name = ch.lstrip("@")
            if name.lower() not in known:
                c.execute("INSERT INTO sources (channel, title, last_id, active) "
                          "VALUES (?,?,0,1)", (name, name))
        # Вимкнені в конфізі — вимикаємо й у базі. Позиція каналу лишається:
        # повернемо його — читатиме далі, а не завалить історією.
        for ch in getattr(config, "SOURCES_OFF", []):
            name = ch.lstrip("@")
            zminen = c.execute("UPDATE sources SET active = 0 "
                               "WHERE LOWER(channel) = ? AND active = 1",
                               (name.lower(),)).rowcount
            if zminen:
                log.info("канал %s вимкнено за списком SOURCES_OFF", name)


def main():
    if not config.BOT_TOKEN:
        sys.exit("❌ У config.py не заповнений BOT_TOKEN")
    init_db()
    migrate()
    bootstrap_sources()
    bootstrap_people()

    me = api("getMe")
    if not me:
        sys.exit("❌ Бот не відповідає — перевірте BOT_TOKEN")
    log.info("бот @%s на зв'язку", me["username"])

    o = owner()
    if o:
        log.info("власниця: @%s (%s)", o["username"], o["user_id"])
    else:
        log.info("власниці ще нема — перший, хто натисне Start у бота, стане нею")

    log.info("каналів: %s | режим: %s | обхід кожні %s с",
             len(sources()), config.MODE, config.POLL_SECONDS)

    if "--once" in sys.argv:
        once()
        log.info("прохід завершено")
        return

    if "--loop-minutes" in sys.argv:
        # Розклад GitHub примхливий: буває, будить раз на пів години замість
        # десяти хвилин. Тому один запуск працює довго, обходячи канали
        # кожні POLL_SECONDS, і сам завершується перед наступним пробудженням.
        minutes = float(sys.argv[sys.argv.index("--loop-minutes") + 1])
        deadline = time.time() + minutes * 60
        n = 0
        while time.time() < deadline:
            n += 1
            try:
                once()
            except Exception as e:
                log.error("збій проходу: %s", e)
            left = deadline - time.time()
            if left <= config.POLL_SECONDS:
                break
            time.sleep(config.POLL_SECONDS)
        log.info("зміну відпрацьовано: %s обходів за %.0f хв", n, minutes)
        return

    threading.Thread(target=listener, daemon=True).start()
    watcher()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        log.info("зупинено вручну")
