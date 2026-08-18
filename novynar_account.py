# -*- coding: utf-8 -*-
"""Новинар — збирає пости з телеграм-каналів і віддає їх адресатам через бота.

Читає ваш акаунт (Telethon, офіційне API), надсилає бот @Kate_news_2026_bot.
"""

import asyncio
import hashlib
import html
import logging
import os
import re
import sqlite3
import sys
import time
from datetime import datetime

from telethon import TelegramClient, events, errors
from telethon.tl.types import (
    Channel, MessageMediaPhoto, MessageMediaDocument, DocumentAttributeVideo,
)

import config

_missing = [n for n in ("API_ID", "API_HASH", "BOT_TOKEN") if not getattr(config, n, None)]
if _missing:
    sys.exit("\n❌ У config.py не заповнено: %s\n"
             "   API_ID / API_HASH — на my.telegram.org (розділ API development tools)\n"
             "   BOT_TOKEN — у @BotFather, команда /token\n" % ", ".join(_missing))

BASE = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE, "novynar.db")
TEMP = os.path.join(BASE, "temp")
USER_SESSION = os.path.join(BASE, "session_user")
BOT_SESSION = os.path.join(BASE, "session_bot")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler(os.path.join(BASE, "novynar.log"), encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logging.getLogger("telethon").setLevel(logging.WARNING)
log = logging.getLogger("novynar")

user = TelegramClient(USER_SESSION, config.API_ID, config.API_HASH)
bot = TelegramClient(BOT_SESSION, config.API_ID, config.API_HASH)
user.parse_mode = "md"
bot.parse_mode = "md"

paused = False
OWNER_ID = None
OWNER_NAME = ""


# ─────────────────────────────  база  ─────────────────────────────

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS sources (
            chat_id INTEGER PRIMARY KEY,
            username TEXT,
            title    TEXT,
            active   INTEGER DEFAULT 1
        );
        CREATE TABLE IF NOT EXISTS seen (
            key TEXT PRIMARY KEY,
            ts  INTEGER
        );
        CREATE TABLE IF NOT EXISTS queue (
            id      INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER,
            msg_id  INTEGER,
            title   TEXT,
            link    TEXT,
            text    TEXT,
            ts      INTEGER
        );
        CREATE TABLE IF NOT EXISTS allowed (
            username TEXT PRIMARY KEY,
            added_ts INTEGER
        );
        CREATE TABLE IF NOT EXISTS recipients (
            user_id  INTEGER PRIMARY KEY,
            username TEXT,
            active   INTEGER DEFAULT 1
        );
        """)


def seen_before(key):
    now = int(time.time())
    horizon = now - config.DEDUP_HOURS * 3600
    with db() as c:
        c.execute("DELETE FROM seen WHERE ts < ?", (horizon,))
        row = c.execute("SELECT 1 FROM seen WHERE key = ?", (key,)).fetchone()
        if row:
            return True
        c.execute("INSERT INTO seen (key, ts) VALUES (?, ?)", (key, now))
    return False


def active_sources():
    with db() as c:
        return {r["chat_id"]: r["title"]
                for r in c.execute("SELECT * FROM sources WHERE active = 1")}


def active_recipients():
    with db() as c:
        return [r["user_id"]
                for r in c.execute("SELECT * FROM recipients WHERE active = 1")]


# ─────────────────────────────  фільтри  ─────────────────────────────

def norm(text):
    """Нормалізуємо текст для порівняння: без посилань, емодзі та пробілів."""
    t = (text or "").lower()
    t = re.sub(r"https?://\S+", "", t)
    t = re.sub(r"[^\w\s]", "", t, flags=re.UNICODE)
    t = re.sub(r"\s+", " ", t).strip()
    return t


def dedup_key(text, message):
    n = norm(text)
    if len(n) >= 60:
        return "t:" + hashlib.sha1(n[:300].encode("utf-8")).hexdigest()
    return "m:%s:%s" % (message.chat_id, message.id)


def word_in(low, key):
    """Шукаємо за основою слова: «Дніпро» знайдеться і в «Дніпрі», і в «Дніпра».

    Українська відмінюється, тому в довгих словах відкидаємо закінчення."""
    k = (key or "").lower().strip()
    if not k:
        return False
    if len(k) > 5 and " " not in k:
        k = k[:-1]
    return k in low


def passes_filters(text, has_media):
    low = (text or "").lower()
    for w in config.STOP_WORDS:
        if word_in(low, w):
            return False, "стоп-слово «%s»" % w
    if config.KEYWORDS:
        if not any(word_in(low, k) for k in config.KEYWORDS):
            return False, "немає ключових слів"
    if not has_media and len(low.strip()) < config.MIN_LENGTH:
        return False, "закоротке"
    return True, ""


def in_quiet_hours():
    if not config.QUIET_HOURS:
        return False
    start, end = config.QUIET_HOURS
    h = datetime.now().hour
    return start <= h or h < end if start > end else start <= h < end


# ─────────────────────────────  форматування  ─────────────────────────────

def post_link(message):
    chat = message.chat
    uname = getattr(chat, "username", None)
    if uname:
        return "https://t.me/%s/%s" % (uname, message.id)
    cid = str(message.chat_id)
    if cid.startswith("-100"):
        return "https://t.me/c/%s/%s" % (cid[4:], message.id)
    return None


def build_text(message, title, body, limit):
    link = post_link(message)
    head = "📡 **%s**" % title
    tail = "\n[↗ оригінал](%s)" % link if link else ""
    room = limit - len(head) - len(tail) - 8
    body = (body or "").strip()
    if len(body) > room:
        body = body[:room].rsplit(" ", 1)[0] + " …"
    parts = [head]
    if body:
        parts.append(body)
    return "\n\n".join(parts) + tail


def media_size(message):
    doc = getattr(message, "document", None)
    if doc is not None:
        return (doc.size or 0) / 1048576.0
    if message.photo:
        return 0.0
    return 0.0


# ─────────────────────────────  надсилання  ─────────────────────────────

async def deliver(to_id, text, files=None):
    """Надсилаємо ботом. Якщо markdown кривий — шлемо як звичайний текст."""
    try:
        if files:
            await bot.send_file(to_id, files, caption=text, parse_mode="md")
        else:
            await bot.send_message(to_id, text, link_preview=False, parse_mode="md")
        return True
    except (errors.MessageEmptyError, ValueError, TypeError) as e:
        log.warning("markdown не зайшов (%s), шлю голим текстом", e)
        plain = re.sub(r"[*_`\[\]()]", "", text)
        try:
            if files:
                await bot.send_file(to_id, files, caption=plain[:1024], parse_mode=None)
            else:
                await bot.send_message(to_id, plain, link_preview=False, parse_mode=None)
            return True
        except Exception as e2:
            log.error("не вдалося надіслати: %s", e2)
            return False
    except errors.FloodWaitError as e:
        log.warning("ліміт Telegram, чекаю %s с", e.seconds)
        await asyncio.sleep(e.seconds + 1)
        return await deliver(to_id, text, files)
    except errors.ForbiddenError:
        log.error("адресат %s заблокував бота або не тиснув /start", to_id)
        return False
    except Exception as e:
        log.error("не вдалося надіслати: %s", e)
        return False


async def broadcast(message, title, body, files=None):
    targets = active_recipients()
    if not targets:
        log.warning("немає жодного активного адресата — новина осіла в черзі")
        enqueue(message, title, body)
        return
    limit = 1024 if files else 4096
    text = build_text(message, title, body, limit)
    for uid in targets:
        await deliver(uid, text, files)
        await asyncio.sleep(config.SEND_DELAY)


def enqueue(message, title, body):
    with db() as c:
        c.execute(
            "INSERT INTO queue (chat_id, msg_id, title, link, text, ts) "
            "VALUES (?,?,?,?,?,?)",
            (message.chat_id, message.id, title, post_link(message) or "",
             (body or "")[:1500], int(time.time())),
        )


# ─────────────────────────────  обробка постів  ─────────────────────────────

async def handle(messages):
    """messages — один пост або альбом."""
    if paused:
        return
    first = messages[0]
    sources = active_sources()
    if first.chat_id not in sources:
        return
    if config.SKIP_FORWARDS and first.forward is not None:
        return

    body = ""
    for m in messages:
        if m.text:
            body = m.text
            break
    has_media = any(m.media for m in messages)

    ok, why = passes_filters(body, has_media)
    if not ok:
        log.info("пропуск [%s/%s]: %s", sources[first.chat_id], first.id, why)
        return

    key = dedup_key(body, first)
    if seen_before(key):
        log.info("дубль [%s/%s] — не шлю", sources[first.chat_id], first.id)
        return

    title = sources[first.chat_id]

    if config.MODE == "digest" or in_quiet_hours():
        enqueue(first, title, body)
        log.info("у чергу [%s/%s]", title, first.id)
        return

    files = await fetch_media(messages)
    try:
        await broadcast(first, title, body, files)
        log.info("надіслано [%s/%s]", title, first.id)
    finally:
        cleanup(files)


async def fetch_media(messages):
    paths = []
    for m in messages:
        if not m.media:
            continue
        if media_size(m) > config.MEDIA_MAX_MB:
            log.info("медіа завелике (%.1f МБ) — шлю без нього", media_size(m))
            continue
        try:
            p = await user.download_media(m, file=TEMP)
            if p:
                paths.append(p)
        except Exception as e:
            log.warning("медіа не завантажилось: %s", e)
        if len(paths) >= 10:
            break
    return paths or None


def cleanup(paths):
    for p in paths or []:
        try:
            os.remove(p)
        except OSError:
            pass


@user.on(events.NewMessage())
async def on_message(event):
    if event.message.grouped_id:      # альбоми ловить окремий обробник
        return
    await handle([event.message])


@user.on(events.Album())
async def on_album(event):
    await handle(list(event.messages))


# ─────────────────────────────  черга і зведення  ─────────────────────────────

async def flush_queue(reason=""):
    with db() as c:
        rows = c.execute("SELECT * FROM queue ORDER BY id").fetchall()
        if not rows:
            return 0
        c.execute("DELETE FROM queue")

    targets = active_recipients()
    if not targets:
        return 0

    chunks, cur = [], "🗞 **Зведення новин** %s\n" % reason
    for r in rows:
        piece = "\n**%s**\n%s\n%s\n" % (
            r["title"],
            (r["text"] or "")[:400].strip(),
            "[↗ оригінал](%s)" % r["link"] if r["link"] else "",
        )
        if len(cur) + len(piece) > 3800:
            chunks.append(cur)
            cur = ""
        cur += piece
    chunks.append(cur)

    for uid in targets:
        for ch in chunks:
            await deliver(uid, ch)
            await asyncio.sleep(config.SEND_DELAY)
    log.info("зведення надіслано: %s постів, %s частин", len(rows), len(chunks))
    return len(rows)


async def scheduler():
    """Раз на хвилину дивимось, чи не час віддавати накопичене."""
    last_fired = None
    was_quiet = in_quiet_hours()
    while True:
        await asyncio.sleep(30)
        now = datetime.now().strftime("%H:%M")
        quiet = in_quiet_hours()

        if was_quiet and not quiet:
            await flush_queue("(за ніч)")
        was_quiet = quiet

        if config.MODE == "digest" and now in config.DIGEST_TIMES and now != last_fired:
            last_fired = now
            if not quiet:
                await flush_queue("(%s)" % now)


# ─────────────────────────────  команди бота  ─────────────────────────────

async def is_owner(event):
    """Власниця — той акаунт, під яким програма читає канали. Питати не треба."""
    sender = await event.get_sender()
    if OWNER_ID and sender.id == OWNER_ID:
        return True
    uname = (getattr(sender, "username", "") or "").lower()
    return bool(OWNER_NAME) and uname == OWNER_NAME


def allow(username):
    with db() as c:
        c.execute("INSERT OR REPLACE INTO allowed (username, added_ts) VALUES (?,?)",
                  (username.lstrip("@").lower(), int(time.time())))


def is_allowed(sender):
    if OWNER_ID and sender.id == OWNER_ID:
        return True
    uname = (getattr(sender, "username", "") or "").lower()
    if uname and uname == OWNER_NAME:
        return True
    with db() as c:
        for key in (uname, str(sender.id)):
            if key and c.execute("SELECT 1 FROM allowed WHERE username = ?",
                                 (key,)).fetchone():
                return True
    return False


async def tell_owner(text):
    """Шепнути власниці — якщо вона вже тиснула /start у бота."""
    if not OWNER_ID:
        return
    try:
        await bot.send_message(OWNER_ID, text, parse_mode="md")
    except Exception:
        pass


@bot.on(events.NewMessage(pattern=r"^/start"))
async def cmd_start(event):
    sender = await event.get_sender()
    uname = (getattr(sender, "username", "") or "").lower()
    if not is_allowed(sender):
        await event.respond("Цей бот приватний. Якщо це помилка — напишіть власниці.")
        log.warning("чужий стукав у бота: @%s (%s)", uname, sender.id)
        await tell_owner(
            "🔔 У бота стукав **@%s** (`%s`).\n"
            "Свій — впустіть: `/allow @%s`" % (uname or "без юзернейма", sender.id, uname)
            if uname else
            "🔔 У бота стукав хтось без юзернейма (`%s`).\n"
            "Свій — впустіть: `/allow %s`" % (sender.id, sender.id))
        return
    with db() as c:
        c.execute("INSERT OR REPLACE INTO recipients (user_id, username, active) "
                  "VALUES (?,?,1)", (sender.id, uname))
    await event.respond(
        "Готово. Новини з каналів приходитимуть сюди.\n\n"
        "/stop — пауза для себе\n/status — що зараз налаштовано\n"
        "/digest — надіслати все накопичене зараз"
    )
    log.info("підписався @%s (%s)", uname, sender.id)


@bot.on(events.NewMessage(pattern=r"^/stop"))
async def cmd_stop(event):
    sender = await event.get_sender()
    with db() as c:
        c.execute("UPDATE recipients SET active = 0 WHERE user_id = ?", (sender.id,))
    await event.respond("Зупинив. /start — щоб знову вмикнути.")


@bot.on(events.NewMessage(pattern=r"^/status"))
async def cmd_status(event):
    srcs = active_sources()
    with db() as c:
        q = c.execute("SELECT COUNT(*) n FROM queue").fetchone()["n"]
        rc = c.execute("SELECT COUNT(*) n FROM recipients WHERE active=1").fetchone()["n"]
    await event.respond(
        "**Новинар живий.**\n"
        "Режим: `%s`%s\n"
        "Каналів: %s\nАдресатів: %s\nУ черзі: %s\n\n%s" % (
            config.MODE,
            "  ⏸ на паузі" if paused else "",
            len(srcs), rc, q,
            "\n".join("• " + t for t in list(srcs.values())[:30]) or "— порожньо —",
        )
    )


@bot.on(events.NewMessage(pattern=r"^/digest"))
async def cmd_digest(event):
    n = await flush_queue("(на запит)")
    if not n:
        await event.respond("Черга порожня.")


@bot.on(events.NewMessage(pattern=r"^/pause"))
async def cmd_pause(event):
    global paused
    if not await is_owner(event):
        return
    paused = True
    await event.respond("Призупинив збір. /resume — продовжити.")


@bot.on(events.NewMessage(pattern=r"^/resume"))
async def cmd_resume(event):
    global paused
    if not await is_owner(event):
        return
    paused = False
    await event.respond("Знову збираю.")


@bot.on(events.NewMessage(pattern=r"^/add\s+(\S+)"))
async def cmd_add(event):
    if not await is_owner(event):
        return
    name = event.pattern_match.group(1)
    try:
        ent = await user.get_entity(name)
        with db() as c:
            c.execute("INSERT OR REPLACE INTO sources (chat_id, username, title, active) "
                      "VALUES (?,?,?,1)",
                      (int("-100%s" % ent.id) if isinstance(ent, Channel) else ent.id,
                       getattr(ent, "username", ""), ent.title))
        await event.respond("Додав: **%s**" % ent.title)
    except Exception as e:
        await event.respond("Не вийшло: `%s`" % e)


@bot.on(events.NewMessage(pattern=r"^/del\s+(\S+)"))
async def cmd_del(event):
    if not await is_owner(event):
        return
    name = event.pattern_match.group(1).lstrip("@").lower()
    with db() as c:
        n = c.execute("UPDATE sources SET active = 0 WHERE lower(username) = ? "
                      "OR lower(title) = ?", (name, name)).rowcount
    await event.respond("Прибрав." if n else "Такого джерела не знайшов.")


@bot.on(events.NewMessage(pattern=r"^/allow\s+(\S+)"))
async def cmd_allow(event):
    if not await is_owner(event):
        return
    who = event.pattern_match.group(1)
    allow(who)
    await event.respond("Впустив **%s**. Хай тисне /start у бота." % who)


@bot.on(events.NewMessage(pattern=r"^/deny\s+(\S+)"))
async def cmd_deny(event):
    if not await is_owner(event):
        return
    who = event.pattern_match.group(1).lstrip("@").lower()
    with db() as c:
        c.execute("DELETE FROM allowed WHERE username = ?", (who,))
        c.execute("UPDATE recipients SET active = 0 WHERE lower(username) = ?", (who,))
    await event.respond("Відрізав **@%s** від потоку." % who)


@bot.on(events.NewMessage(pattern=r"^/(help|sources)"))
async def cmd_help(event):
    await event.respond(
        "**Новинар**\n"
        "/status — стан\n/digest — віддати накопичене\n"
        "/stop, /start — вимкнути/увімкнути себе\n"
        "— тільки для власниці —\n"
        "/add @канал, /del @канал\n"
        "/allow @людина, /deny @людина\n"
        "/pause, /resume"
    )


# ─────────────────────────────  старт  ─────────────────────────────

async def sync_sources():
    known = set()
    with db() as c:
        for r in c.execute("SELECT username FROM sources"):
            if r["username"]:
                known.add(r["username"].lower())
    for name in config.SOURCES:
        if name.lstrip("@").lower() in known:
            continue
        try:
            ent = await user.get_entity(name)
            cid = int("-100%s" % ent.id) if isinstance(ent, Channel) else ent.id
            with db() as c:
                c.execute("INSERT OR REPLACE INTO sources "
                          "(chat_id, username, title, active) VALUES (?,?,?,1)",
                          (cid, name.lstrip("@"), ent.title))
            log.info("джерело підключено: %s", ent.title)
        except Exception as e:
            log.error("канал %s не підключився: %s", name, e)


async def main():
    init_db()
    os.makedirs(TEMP, exist_ok=True)

    await user.connect()
    if not await user.is_user_authorized():
        sys.exit("❌ Акаунт не авторизований. Спершу запустіть: python login.py")
    me = await user.get_me()
    global OWNER_ID, OWNER_NAME
    OWNER_ID = me.id
    OWNER_NAME = (me.username or "").lower()
    log.info("власниця: %s (@%s)", me.first_name, OWNER_NAME or "без юзернейма")
    for who in getattr(config, "RECIPIENTS", []):
        if who and who != "@":
            allow(str(who))

    await bot.start(bot_token=config.BOT_TOKEN)
    me_bot = await bot.get_me()
    log.info("бот: @%s", me_bot.username)

    await sync_sources()
    log.info("джерел активних: %s | режим: %s", len(active_sources()), config.MODE)
    log.info("Новинар на посту. Ctrl+C — зупинити.")

    asyncio.ensure_future(scheduler())
    await asyncio.gather(user.run_until_disconnected(),
                         bot.run_until_disconnected())


if __name__ == "__main__":
    try:
        asyncio.get_event_loop().run_until_complete(main())
    except KeyboardInterrupt:
        log.info("зупинено вручну")
