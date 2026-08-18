# -*- coding: utf-8 -*-
"""Новинар — збирає пости з відкритих телеграм-каналів і надсилає їх ботом.

Не потребує входу в акаунт: читає публічні веб-сторінки каналів (t.me/s/…)
і віддає новини через бота @Kate_news_2026_bot.
"""

import html as html_mod
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
            channel TEXT, words TEXT, ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS queue (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel TEXT, title TEXT, link TEXT, text TEXT, photo TEXT, ts INTEGER
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


def already_told(text):
    """Чи розповідали ми це вже — хай навіть іншими словами."""
    if not config.SIMILARITY:
        return None
    tok = tokens(text)
    if len(tok) < 5:
        return None
    now = int(time.time())
    with db() as c:
        c.execute("DELETE FROM recent WHERE ts < ?", (now - config.DEDUP_HOURS * 3600,))
        for r in c.execute("SELECT channel, words FROM recent").fetchall():
            score = looks_similar(tok, set((r["words"] or "").split()))
            if score >= config.SIMILARITY:
                return (r["channel"], score)
    return None


def remember(channel, text):
    """Запам'ятати сюжет як розказаний."""
    tok = tokens(text)
    if len(tok) < 5:
        return
    with db() as c:
        c.execute("INSERT INTO recent (channel, words, ts) VALUES (?,?,?)",
                  (channel, " ".join(sorted(tok)), int(time.time())))


def weigh(post):
    """Наскільки версія докладна: довжина тексту плюс бонус за картинку."""
    return len(post.get("text") or "") + (250 if post.get("photo") else 0)


def hold(post, title):
    """Кладемо новину в очікування — раптом хтось розповість докладніше."""
    tok = tokens(post["text"])
    with db() as c:
        grp = None
        if config.SIMILARITY and len(tok) >= 5:
            for r in c.execute("SELECT id, grp, text FROM holding").fetchall():
                if looks_similar(tok, tokens(r["text"])) >= config.SIMILARITY:
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
            log.info("у чергу: %s", best["link"])
        else:
            broadcast(post, best["title"])
            log.info("надіслано: %s", best["link"])
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

    hits = [m for m in config.ALERT_MARKERS if m in low]
    if hits and not (len(low) > 600 and len(hits) < 3):
        return hits[0]

    soft = [m for m in getattr(config, "LIVE_ATTACK_MARKERS", []) if m in low]
    if soft and len(low) <= getattr(config, "LIVE_ATTACK_MAX_LEN", 400):
        return soft[0]
    return False


def passes_filters(text, has_media):
    low = (text or "").lower()
    marker = is_alert(text)
    if marker:
        return False, "оперативка («%s»)" % marker
    for w in config.STOP_WORDS:
        if w.lower() in low:          # точно, без обрізання основи
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

        text = clean_html(box.select_one(".tgme_widget_message_text"))

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

        has_video = bool(box.select_one(".tgme_widget_message_video"))
        when = box.select_one("time")
        posts.append({
            "id": pid,
            "channel": channel,
            "text": text,
            "photo": photo,
            "video": has_video,
            "link": "https://t.me/%s/%s" % (channel, pid),
            "when": when.get("datetime") if when else "",
        })
    posts.sort(key=lambda p: p["id"])
    return title, posts


# ─────────────────────────────  надсилання  ─────────────────────────────

def api(method, **params):
    for attempt in range(3):
        try:
            r = requests.post(API + method, data=params, timeout=60)
            j = r.json()
            if j.get("ok"):
                return j["result"]
            desc = j.get("description", "")
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


def send_photo(uid, blob, caption):
    for attempt in range(2):
        try:
            r = requests.post(API + "sendPhoto",
                              data={"chat_id": uid, "caption": caption,
                                    "parse_mode": "HTML"},
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


def build_text(title, body, link, limit):
    head = "📡 <b>%s</b>" % html_mod.escape(title)
    tail = '\n\n<a href="%s">↗ оригінал</a>' % link
    room = limit - len(head) - len(tail) - 10
    body = (body or "").strip()
    if len(body) > room:
        cut = body[:room]
        cut = cut.rsplit(" ", 1)[0] if " " in cut else cut
        body = close_tags(cut) + " …"
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


def send_post(uid, post, title):
    body = post["text"]
    if post["video"] and "відео" not in (body or "").lower():
        body = (body + "\n\n🎬 у пості є відео").strip()
    if post["photo"]:
        blob = post.get("_blob") or grab_photo(post["photo"])
        if blob:
            post["_blob"] = blob          # для решти читачів качати вдруге не треба
            text = build_text(title, body, post["link"], 1024)
            if send_photo(uid, blob, text):
                return True
            log.info("фото не пішло — шлю текстом")
    text = build_text(title, body, post["link"], 4096)
    return bool(api("sendMessage", chat_id=uid, text=text,
                    parse_mode="HTML", disable_web_page_preview=True))


def broadcast(post, title):
    people = readers()
    if not people:
        enqueue(post, title)
        return False
    for p in people:
        send_post(p["user_id"], post, title)
        time.sleep(config.SEND_DELAY)
    return True


def enqueue(post, title):
    with db() as c:
        c.execute("INSERT INTO queue (channel, title, link, text, photo, ts) "
                  "VALUES (?,?,?,?,?,?)",
                  (post["channel"], title, post["link"],
                   (post["text"] or "")[:1200], post["photo"] or "", int(time.time())))


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
            html_mod.escape(r["title"]),
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
            ok, why = passes_filters(post["text"], bool(post["photo"] or post["video"]))
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


def watcher():
    was_quiet = in_quiet_hours()
    last_digest = None
    while True:
        try:
            with _lock:
                round_trip()
                if config.HOLD_MINUTES:
                    release_ready()

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
                who = ("@" + uname) if uname else str(uid)
                reply(o["user_id"], "✅ <b>%s</b> підключився за посиланням. "
                                    "Нічого робити не треба."
                      % html_mod.escape(who))
                return
            log.warning("перепустку вичерпано, стукав @%s (%s)", uname, uid)

        if not is_allowed(uid, uname):
            reply(chat, "Цей бот приватний. Якщо це помилка — напишіть власниці.")
            log.warning("чужий стукав: @%s (%s)", uname, uid)
            who = ("@" + uname) if uname else str(uid)
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
        reply(chat, "<b>Новинар живий.</b>\nРежим: <code>%s</code>%s\n"
                    "Каналів: %s | Читачів: %s | У черзі: %s\n\n%s"
              % (config.MODE, "  ⏸ пауза" if paused else "", len(srcs),
                 len(readers()), q,
                 "\n".join("• %s" % html_mod.escape(s["title"] or s["channel"])
                           for s in srcs) or "— порожньо —"))

    elif cmd == "/digest":
        if not flush_queue("(на запит)"):
            reply(chat, "Черга порожня.")

    elif cmd == "/help":
        reply(chat, "<b>Новинар</b>\n"
                    "/status — стан\n/digest — віддати накопичене\n"
                    "/stop, /start — вимкнути / увімкнути себе\n"
                    "— лише для власниці —\n"
                    "/add @канал, /del @канал\n"
                    "/allow @людина, /deny @людина\n/pause, /resume")

    elif cmd in ("/add", "/del", "/allow", "/deny", "/pause", "/resume"):
        if not is_owner(uid):
            reply(chat, "Це може лише власниця.")
            return
        handle_owner_command(chat, cmd, arg)


def handle_owner_command(chat, cmd, arg):
    global paused
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
    fresh_start = get_state("offset") is None
    upd = api("getUpdates", offset=int(get_state("offset", 0) or 0), timeout=0) or []
    if fresh_start and upd:
        set_state("offset", upd[-1]["update_id"] + 1)
        log.info("перший запуск — %s старих команд пропускаю", len(upd))
    else:
        for u in upd:
            set_state("offset", u["update_id"] + 1)
            msg = u.get("message") or u.get("edited_message")
            if msg:
                try:
                    handle_command(msg)
                except Exception as e:
                    log.error("команда впала: %s", e)
    round_trip()
    if config.HOLD_MINUTES:
        release_ready()
    if not in_quiet_hours():
        flush_queue("(за минулий час)")


def bootstrap_sources():
    with db() as c:
        known = {r["channel"].lower() for r in c.execute("SELECT channel FROM sources")}
        for ch in config.SOURCES:
            name = ch.lstrip("@")
            if name.lower() not in known:
                c.execute("INSERT INTO sources (channel, title, last_id, active) "
                          "VALUES (?,?,0,1)", (name, name))


def main():
    if not config.BOT_TOKEN:
        sys.exit("❌ У config.py не заповнений BOT_TOKEN")
    init_db()
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
