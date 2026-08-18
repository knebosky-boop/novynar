# -*- coding: utf-8 -*-
"""Повна перевірка Новинаря. Запуск: .venv/bin/python perevirka.py

Нічого нікому не надсилає — працює на окремій базі й підмінює надсилання.
"""
import os, re, sys, time
import config, novynar as n

n.DB_PATH = "/tmp/nv_perevirka.db"
PASS = FAIL = 0
CURRENT = ""


def block(title):
    global CURRENT
    CURRENT = title
    print("\n═══ %s ═══" % title)


def check(label, cond, note=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print("  ✓ %-52s %s" % (label, note))
    else:
        FAIL += 1
        print("  ✗ %-52s %s" % (label, note))


def fresh_db():
    if os.path.exists(n.DB_PATH):
        os.remove(n.DB_PATH)
    n.init_db()


# ─────────────────────────  фільтри  ─────────────────────────
block("Фільтри: сигналізація проти новини")
for text, want, why in [
    ("Повітряна тривога в Дніпропетровській області", False, "тривога"),
    ("Відбій тривоги в усіх областях", False, "відбій"),
    ("Нові шахеди зайшли на Чернігівщину", False, "шахеди зайшли"),
    ("Нові на Чернігівщині. Рух на Броварський район", False, "рух без назв"),
    ("3 БпЛА через північ рухаються у напрямку Славутича", False, "курс"),
    ("Дорозвідка по Київщині. Якщо нічого не вилізе - буде відбій", False, "дорозвідка"),
    ("💥 Вибухи в Нікополі!", False, "вибух у моменті"),
    ("Працює ППО по цілях над містом", False, "ппо в моменті"),
    ("Сили оборони збили 42 з 48 шахедів минулої ночі, повідомляє ПК", True, "підсумок ночі"),
    ("Внаслідок нічної атаки в Нікополі пошкоджено дев'ять будинків", True, "наслідки"),
    ("Знищено 12 ворожих БпЛА над Дніпропетровщиною, повідомили військові", True, "підсумок"),
    ("РФ розробила дрон для ударів по ЛЕП, — Вадим Скібіцький", True, "звичайна новина"),
]:
    check(why, n.passes_filters(text, False, "babel")[0] == want, text[:44])

block("Фільтри: фронтові напрямки не сплутати з сигналізацією")
for text, want, why in [
    ("OSINT-ідентифікація військовослужбовців 72-ї ОМСБр ЗС РФ на Костянтинівському напрямку",
     True, "Костянтинівський напрямок"),
    ("Ситуація на Покровському напрямку залишається складною, ворог накопичує резерви",
     True, "Покровський напрямок"),
    ("Бої на Лиманському напрямку: ЗСУ відбили сім атак за добу", True, "Лиманський напрямок"),
    ("Просування ворога на Куп'янському напрямку зупинено, — Генштаб", True, "Куп'янський напрямок"),
    ("3 БпЛА через північ Чернігівщини рухаються у напрямку Славутича", False, "рух цілей"),
    ("Група шахедів курсом на Кривий Ріг", False, "курс шахедів"),
]:
    check(why, n.passes_filters(text, False, "babel")[0] == want, text[:46])

block("Фільтри: реклама і хибні спрацювання")
for text, want, why in [
    ("Відставка Федорова? Президент перезавантажує уряд", True, "«відставка» не реклама"),
    ("Британія оголосила про нові поставки ракет Україні", True, "«поставки» не реклама"),
    ("Бої на ділянці Колодязі – Ставки у смузі 20-ї ОА", True, "село Ставки"),
    ("Президент приділятиме час контактам з партнерами", True, "«партнери» не реклама"),
    ("Рада ухвалила заборону реклами азартних ігор", True, "новина про рекламу"),
    ("Партнерський матеріал: новий сервіс доставки їжі", False, "справжня реклама"),
    ("Промокод KATE дає знижку 20% на всі товари", False, "промокод"),
    ("Ставки на спорт з букмекером — вигідні коефіцієнти", False, "букмекер"),
]:
    check(why, n.passes_filters(text, False, "babel")[0] == want, text[:44])

block("Фільтри: збори грошей і службовий мотлох")
for text, want, why in [
    ("Огромное спасибо всем, кто поддерживает мою Телеграм-деятельность с помощью "
     "донатов: Банка - https://send.monobank.ua/jar/3ehE Карта - 5375411217472054",
     False, "донати на канал"),
    ("Шрайк pinned «Огромное спасибо всем, кто поддерживает»", False, "службова позначка"),
    ("‼️ УВАГА, ЗБІР на автомобіль для бійців 65-ї ОМБр, просимо підтримати",
     False, "збір на техніку"),
    ("Завдяки вам вже зібрано 265 000 грн! Залишилося зібрати 35 000", False, "добір"),
    ("Нам терміново потрібно закупити компоненти для тестерів батарей", False, "закупівля"),
    ("Рада ухвалила бюджет: на оборону піде 2,2 трильйона гривень", True, "про гроші, але новина"),
    ("Уряд оголосив збір заявок на гранти для ветеранського бізнесу", True, "збір заявок"),
    ("Зібрано врожай ранніх зернових: аграрії намолотили 28 млн тонн", True, "збір врожаю"),
]:
    check(why, n.passes_filters(text, False, "babel")[0] == want, text[:44])

block("Фільтри: прохання грошей будь-якими словами")
# Справжній пост Смолія від 18.08.2026, що пролетів крізь стоп-слова.
SMOLII_ZBIR = ("Протягом всього дня інформую вас про всі атаки та тривоги.\n"
               "Сподіваюсь вам безпечніше.\n***\n"
               "Підтримайте і мене та мій канал, за можливості. Буду вдячний за ВАШУ підтримку\n"
               "кожна гривня, 10, 50 , 100 чи більше - це важливо\n"
               "Підтримати можна тут:\nMonobank: 4441111401673367\n"
               "PayPal: smolii.andrii@gmail.com\nДякую кожному!")
for text, want, why in [
    (SMOLII_ZBIR, False, "живий пост Смолія зі зборами"),
    ("Підтримайте і мене та мій канал, за можливості, буду вдячний за вашу підтримку",
     False, "слова між «підтримайте» і «канал»"),
    ("Щиро дякую всім хто в ці хвилини підтримав канал. Працюю для вас далі",
     False, "подяка за підтримку каналу"),
    ("Monobank: 4441111401673367", False, "банк латиницею плюс номер"),
    ("Реквізити для допомоги: 5375 4112 1747 2054", False, "номер картки з пробілами"),
    ("PayPal: smolii.andrii@gmail.com", False, "пейпал з поштою"),
    ("Будь-яка сума важлива, друзі", False, "будь-яка сума"),
    # Подяки за збір — теж не новини (живі пости Левіна від 18.08.2026).
    ('Бійці підрозділу "Корсари" 29-ї бригади ЗСУ дякують підписникам каналу '
     'за допомогу у придбанні автомобіля', False, "дякують підписникам за збір"),
    ("Дякуємо за вашу підтримку, друзі, ви неймовірні", False, "подяка за вашу підтримку"),
    ("Дякую всім, хто долучився до збору на дрони для 47 бригади", False, "подяка за збір"),
    # А оце — новини, їх різати не можна.
    ("США підтримали Україну новим пакетом допомоги на 400 мільйонів доларів", True,
     "підтримали Україну — новина"),
    ("Рада підтримала законопроєкт про мобілізацію у другому читанні", True,
     "підтримала закон — новина"),
    ("Монобанк запустив нову послугу для підприємців, розповів Гороховський", True,
     "новина про сам банк"),
    ("ПриватБанк відновив роботу застосунку після нічної атаки", True,
     "новина про ПриватБанк"),
    ("PayPal остаточно вийшов з російського ринку і закрив рахунки резидентів РФ", True,
     "новина про PayPal"),
    ("Виплати 6500 гривень надійдуть на карту протягом тижня, повідомили в Мінсоцполітики",
     True, "виплати на карту — новина"),
    ("Зеленський подякував партнерам за підтримку та нові пакети допомоги", True,
     "подяка партнерам — новина"),
    ("МЗС подякувало Польщі за допомогу у розслідуванні нападу на дітей", True,
     "подяка державі — новина"),
]:
    check(why, n.passes_filters(text, False, "babel")[0] == want, text[:44])

block("Фільтри: окремий поріг для каналу")
long_text = "Розгорнута новина. " * 15
check("від Смолія коротке не беремо",
      not n.passes_filters("Нові на Київщині", False, "smolii_ukraine")[0])
check("від Смолія довге беремо",
      n.passes_filters(long_text, False, "smolii_ukraine")[0])
check("з інших каналів коротке проходить",
      n.passes_filters("Помер відомий український письменник Юрій Андрухович", False, "babel")[0])

block("Фільтри: чужа тема в окремому каналі")
for text, want, why in [
    ("44 года назад в долине Бекаа произошел бой при Султан-Якубе, где израильские "
     "танкисты попали в сирийскую западню", False, "суто ізраїльська історія"),
    ("Армия обороны Израиля ликвидировала на юге Ливана командира подразделения",
     False, "Ізраїль і Ліван"),
    ("Google создала в Израиле новую группу для разработки чипов", False, "Ізраїль і техніка"),
    ("Журналисты The Telegraph отмечают, что роль Каспийского моря для поставок "
     "между РФ и Ираном выросла после ударов по Сирии", True, "Близький Схід, але про РФ"),
    ("OSINT-ідентифікація військовослужбовців 72-ї ОМСБр ЗС РФ на Костянтинівському "
     "напрямку", True, "суто українське"),
    ("В Сан-Франциско работает экспериментальный магазин Andon Market, где почти "
     "все управленческие решения принимает нейросеть", True, "«магазин» ≠ «Газа»"),
]:
    check(why, n.passes_filters(text, False, "yigal_levin")[0] == want, text[:44])
check("в інших каналах тема не ріжеться",
      n.passes_filters("Армия обороны Израиля ликвидировала командира", False, "babel")[0])

block("Фільтри: ключові слова за основою")
config.KEYWORDS = ["Дніпро", "суд"]
for text, want in [("Ситуація у Дніпрі складна вже третій тиждень поспіль", True),
                   ("Уряд виділив кошти на відбудову шкіл у Дніпропетровській області", True),
                   ("Верховний Суд ухвалив постанову у цій категорії спорів", True),
                   ("У Львові відкрили новий сквер на місці старої забудови", False)]:
    check("«%s…»" % text[:34], n.passes_filters(text, False, "babel")[0] == want)
config.KEYWORDS = []

# ─────────────────────────  дублі  ─────────────────────────
block("Короткі назви каналів")
for full, want in [
    ("СУСПІЛЬНЕ НОВИНИ🔹: Київ Харків Дніпро Суми Львів Одеса Вінниця", "СУСПІЛЬНЕ НОВИНИ🔹"),
    ("Аналітика фронту | від офіцера ЗСУ", "Аналітика фронту"),
    ("Сергій FLASH | Про технології", "Сергій FLASH"),
    ("Бабель", "Бабель"),
    ("Шрайк Ньюс", "Шрайк Ньюс"),
]:
    check("«%s»" % full[:36], n.short_title(full) == want, "→ %s" % n.short_title(full))
check("дуже довга назва без роздільників обрізається",
      len(n.short_title("Канал " * 20)) <= 41)

block("Дублі")
fresh_db()
a = "Ворог завдав удару по Нікополю, пошкоджено п'ять житлових будинків, постраждалих немає"
b = "Росіяни вдарили по Нікополю. Пошкоджені п'ять будинків, люди не постраждали"
c = "Курс долара на міжбанку знизився на дві копійки за підсумками торгів"
d = "У Дніпрі закрили міст через Самару на ремонт до кінця року"
e = "У Дніпрі відкрили новий міст через Самару після ремонту"
check("та сама новина іншими словами — дубль",
      n.looks_similar(n.tokens(a), n.tokens(b)) >= config.SIMILARITY,
      "збіг %.0f%%" % (100 * n.looks_similar(n.tokens(a), n.tokens(b))))
check("різні теми — не дубль", n.looks_similar(n.tokens(a), n.tokens(c)) < config.SIMILARITY)
check("протилежні новини зі схожими словами — не дубль",
      n.looks_similar(n.tokens(d), n.tokens(e)) < config.SIMILARITY,
      "збіг %.0f%%" % (100 * n.looks_similar(n.tokens(d), n.tokens(e))))
class M:
    def __init__(s, cid, mid): s.chat_id, s.id = cid, mid
check("точний відбиток спрацьовує один раз",
      not n.seen_before("t:test1") and n.seen_before("t:test1"))
n.remember("babel", a)
check("після надсилання сюжет запам'ятано", n.already_told(b) is not None)
check("чужий сюжет не плутається", n.already_told(c) is None)

# ─────────────────────────  довгі пости  ─────────────────────────
block("Дублі: опора на власні назви")
for a, b, want, why in [
    ("Трамп максимально скоротив масштаби спільних військових навчань з Південною Кореєю через КНДР",
     "Дональд Трамп заявив, що США скоротять спільні навчання з Південною Кореєю через переговори з КНДР",
     True, "Трамп і Корея"),
    ("Генштаб: У ніч на 16 серпня підрозділи СОУ завдали ураження важливому підприємству "
     "ВПК противника — «Комбінат Каменський» у Ростовській області РФ",
     "💥 Сили оборони вночі атакували «Комбінат Каменський» у Ростовській області РФ, "
     "повідомляє Генштаб. Це сталося у ніч на 16 серпня",
     True, "удар по Комбінату Каменському"),
    ("🇺🇦 Прапору Донеччини – 27 років. 17 серпня 1999 року затвердили прапор Донецької області",
     "Стратегічні плани ворога. Поточний етап війни входить у фазу спроби стратегічного перелому",
     False, "різні: прапор і стратегія"),
    ("Вночі росіяни атакували портову інфраструктуру Одещини, є постраждалі, пошкоджено судно",
     "Вночі ворог атакував портову інфраструктуру Одещини, — ОВА. Пошкоджено цивільне судно",
     True, "атака на Одещину"),
    ("Пишуть, що голосування за посаду міністра оборони та закордонних справ буде завтра",
     "РБК зробили матеріал про групи впливу в оточенні Зеленського та звільнення голови Офісу",
     False, "різні: голосування і матеріал"),
    ("Макет української балістичної ракети FP-9 від Fire Point на виставці у Данії",
     "Перша українська балістика може зʼявитися восени, компанія Fire Point розробляє ракету",
     False, "різні: макет і плани"),
    ("У Дніпрі відкрили новий міст через Самару після ремонту",
     "У Дніпрі закрили міст через Самару на ремонт до кінця року",
     False, "різні: відкрили і закрили"),
]:
    same, sc = n.is_same_story(n.tokens(a), n.anchors(a), n.tokens(b), n.anchors(b))
    check(why, same == want, "збіг %.0f%%, спільних назв %s"
          % (sc * 100, len(n.anchors(a) & n.anchors(b))))

block("Оновлення старої бази")
import sqlite3 as _sq
_old_path = n.DB_PATH
n.DB_PATH = "/tmp/nv_migr_check.db"
if os.path.exists(n.DB_PATH):
    os.remove(n.DB_PATH)
_c = _sq.connect(n.DB_PATH)
_c.executescript("""
CREATE TABLE recent (id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT, words TEXT, ts INTEGER);
CREATE TABLE sources (channel TEXT PRIMARY KEY, title TEXT, last_id INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
CREATE TABLE people (user_id INTEGER PRIMARY KEY, username TEXT, is_owner INTEGER DEFAULT 0, active INTEGER DEFAULT 1);
INSERT INTO recent (channel, words, ts) VALUES ('babel','стара памʼять',1700000000);
INSERT INTO sources VALUES ('babel','Бабель',88000,1);
""")
_c.commit(); _c.close()
n.init_db(); n.migrate()
with n.db() as c:
    _cols = [r[1] for r in c.execute("PRAGMA table_info(recent)")]
    _kept = c.execute("SELECT COUNT(*) n FROM recent").fetchone()["n"]
    _pos = c.execute("SELECT last_id FROM sources WHERE channel='babel'").fetchone()["last_id"]
check("стовпець anchors додається", "anchors" in _cols)
check("стара памʼять не гине", _kept == 1)
check("позиції каналів не гинуть", _pos == 88000)
n.remember("babel", "Новина про важливі події у Дніпрі та області сьогодні вранці")
check("запис у оновлену схему працює",
      n.already_told("Новина про важливі події у Дніпрі та області сьогодні вранці") is not None)
n.DB_PATH = _old_path

block("Довгі пости: повний текст без втрат")
plain = lambda s: re.sub(r"\s+", "", re.sub(r"<[^>]+>", "", s))
body = ("<b>Заголовок новини</b>\n\n" +
        "Абзац із <i>курсивом</i> та <a href=\"https://example.com\">посиланням</a>. " * 90)
for limit in (1024, 4096):
    parts = n.split_messages("Канал", body, "https://t.me/x/1", limit)
    joined = "".join(parts).replace("↗ оригінал", "")
    joined = re.sub(r"<i>\d+ з \d+</i>", "", joined)
    got = plain(joined).replace(plain("📡Канал"), "", 1)
    check("ліміт %s: текст цілий" % limit, len(plain(body)) - len(got) <= 0,
          "%s знаків → %s повідомлень" % (len(body), len(parts)))
    check("ліміт %s: кожна частина в межах" % limit, all(len(p) <= limit or
          (i and len(p) <= 4096) for i, p in enumerate(parts)))
    stack_ok = True
    for part in parts:
        st = []
        for m in re.finditer(r"<(/?)(\w[\w-]*)([^>]*)>", part):
            cl, tag = m.group(1), m.group(2).lower()
            if tag not in n.KEEP_TAGS: stack_ok = False; break
            if cl:
                if not st or st[-1] != tag: stack_ok = False; break
                st.pop()
            else: st.append(tag)
        if st: stack_ok = False
    check("ліміт %s: розмітка ціла в кожній частині" % limit, stack_ok)

# ─────────────────────────  черга  ─────────────────────────
block("Нічна черга і зведення")
fresh_db()
sent = []
real_api = n.api
n.api = lambda m, **kw: (sent.append((m, kw.get("text", ""))) or {"ok": True})
with n.db() as c:
    c.execute("INSERT INTO people (user_id, username, is_owner, active) VALUES (1,'k',1,1)")
for i in range(5):
    n.enqueue({"channel": "babel", "text": "Новина %s про важливі події" % i,
               "photo": None, "video": False, "link": "https://t.me/babel/%s" % i}, "Бабель")
check("новини лягли в чергу", len(sent) == 0)
check("зведення віддало все", n.flush_queue("(тест)") == 5)
check("порожню чергу не шле", n.flush_queue("(тест)") == 0)
sent.clear()
for i in range(60):
    n.enqueue({"channel": "babel", "text": "Довга новина %s. " % i + "текст " * 70,
               "photo": None, "video": False, "link": "https://t.me/b/%s" % i}, "Бабель")
n.flush_queue("(багато)")
check("велике зведення порізано", len(sent) > 1, "%s повідомлень" % len(sent))
check("жодне не перевищує ліміт", all(len(t) <= 4096 for _, t in sent))
n.api = real_api

# ─────────────────────────  тихі години  ─────────────────────────
block("Тихі години")
import datetime as dt
real_dt = dt.datetime
class F(real_dt):
    H = 0
    @classmethod
    def now(cls, tz=None): return real_dt(2026, 8, 18, cls.H, 30)
n.datetime = F
config.QUIET_HOURS = (23, 8)
for h, want in [(23, True), (3, True), (7, True), (8, False), (14, False), (22, False)]:
    F.H = h
    check("%02d:30 — %s" % (h, "тиша" if want else "працюємо"), n.in_quiet_hours() == want)
config.QUIET_HOURS = None
F.H = 3
check("вимкнено — тиші немає", not n.in_quiet_hours())
n.datetime = real_dt
config.QUIET_HOURS = (23, 8)

# ─────────────────────────  доступ  ─────────────────────────
block("Нагадування «є що почитати»")
fresh_db()
with n.db() as c:
    c.execute("INSERT INTO people VALUES (111,'kate',1,1)")
    c.execute("INSERT INTO people VALUES (129576564,'',0,1)")
_sent = []
_real_api = n.api
n.api = lambda m, **kw: _sent.append((kw.get("chat_id"), kw.get("text", ""),
                                      kw.get("disable_notification"))) or {"ok": True}
_real_dt = dt.datetime
class _T(_real_dt):
    H, M = 7, 30
    @classmethod
    def now(cls, tz=None): return _real_dt(2026, 8, 18, cls.H, cls.M)
n.datetime = _T
config.QUIET_HOURS = (23, 8)
config.REMINDER_TIMES = ["08:00", "20:00"]

_T.H, _T.M = 7, 30; n.maybe_remind()
check("до часу мовчить", not _sent)
_T.H, _T.M = 8, 2; n.maybe_remind()
check("час настав, новин нема — не турбує", not _sent)
for _ in range(7):
    n.bump_unread()
_T.H, _T.M = 8, 8; _sent[:] = []; n.maybe_remind()
check("новини зʼявились — нагадало", len(_sent) == 1)
check("нагадування лише читачам, не власниці", _sent and _sent[0][0] == 129576564)
check("нагадування зі звуком", _sent and _sent[0][2] is False)
check("названо кількість новин", _sent and "7 новин" in _sent[0][1])
check("ранкове вітання", _sent and "Доброго ранку" in _sent[0][1],
      re.sub(r"<[^>]+>", "", _sent[0][1]).replace("\n", " ")[:56] if _sent else "")
_sent[:] = []; n.maybe_remind()
check("двічі не нагадує", not _sent)
n.bump_unread(); n.bump_unread()
_T.H, _T.M = 20, 4; _sent[:] = []; n.maybe_remind()
check("ввечері нагадує знову", len(_sent) == 1)
check("ввечері звертається у кличному відмінку", _sent and "Миколко" in _sent[0][1],
      re.sub(r"<[^>]+>", "", _sent[0][1]).replace("\n", " ")[:58] if _sent else "")
check("лічильник почався заново", _sent and "2 новини" in _sent[0][1])
config.REMINDER_TIMES = ["02:00"]; _T.H, _T.M = 2, 1; _sent[:] = []
for _ in range(5):
    n.bump_unread()
n.maybe_remind()
check("вночі не турбує", not _sent)
config.REMINDER_TIMES = []; _T.H, _T.M = 12, 0; _sent[:] = []; n.maybe_remind()
check("порожній список — нагадувань немає", not _sent)
n.datetime = _real_dt
n.api = _real_api
config.REMINDER_TIMES = ["08:00", "20:00"]

block("Імена і відмінювання")
check("друга звати за іменем", n.person_name(129576564, "") == "Микола")
check("без імені — юзернейм", n.person_name(999, "petro") == "@petro")
check("без нічого — номер", n.person_name(777, "") == "777")
check("кличний відмінок для Миколи", n.person_voc(129576564, "") == "Миколко")
check("без кличного — звичайне ім'я", n.person_voc(999, "petro") == "petro")
for _cnt, _want in [(1, "новина"), (2, "новини"), (5, "новин"), (11, "новин"),
                    (21, "новина"), (23, "новини"), (105, "новин")]:
    _w = "новина" if _cnt % 10 == 1 and _cnt % 100 != 11 else (
        "новини" if 2 <= _cnt % 10 <= 4 and not 12 <= _cnt % 100 <= 14 else "новин")
    check("%s → %s" % (_cnt, _want), _w == _want)

block("Хто пройде в бота")
fresh_db()
_saved_code = config.INVITE_CODE
config.INVITE_CODE = "test-perepustka"
sent = []
n.reply = lambda chat, text: sent.append((chat, text))
def start(uid, uname, payload=""):
    sent.clear()
    n.handle_command({"text": ("/start " + payload).strip(),
                      "from": {"id": uid, "username": uname}, "chat": {"id": uid}})
    return sent
start(1, "kate")
check("перший став власником", n.owner()["user_id"] == 1)
start(2, "druh", config.INVITE_CODE)
check("друг за перепусткою пройшов", n.is_allowed(2, "druh"))
start(3, "chuzhyi")
check("чужий без перепустки не пройшов", not n.is_allowed(3, "chuzhyi"))
check("власниці прийшло сповіщення про чужого", any(c == 1 for c, _ in sent))
start(4, "third", config.INVITE_CODE)
start(5, "fourth", config.INVITE_CODE)
check("перепустка вичерпується", not n.is_allowed(5, "fourth"))
config.INVITE_CODE = ""
start(6, "pizniy", "test-perepustka")
check("із закритою перепусткою не пускає нікого", not n.is_allowed(6, "pizniy"))
check("вже підключені лишаються", n.is_allowed(2, "druh"))
config.INVITE_CODE = _saved_code

block("Якщо друг вийшов і повернувся")
fresh_db()
sent = []
n.reply = lambda chat, text: sent.append(text)
_saved = config.INVITE_CODE
config.INVITE_CODE = "test-perepustka"
start(1, "kate")
start(2, "druh", "test-perepustka")
config.INVITE_CODE = ""
check("друг у потоці", 2 in [p["user_id"] for p in n.readers()])
n.handle_command({"text": "/stop", "from": {"id": 2, "username": "druh"}, "chat": {"id": 2}})
check("після /stop новини не йдуть", 2 not in [p["user_id"] for p in n.readers()])
start(2, "druh")
check("після /start повертається навіть із закритою перепусткою",
      2 in [p["user_id"] for p in n.readers()])
start(777, "chuzhyi")
check("сторонній так само не пройде", 777 not in [p["user_id"] for p in n.readers()])

# заблокував бота
_real_post = __import__("requests").post
class _R:
    def __init__(s, j): s._j = j
    def json(s): return s._j
_notes = []
def _fake(url, **kw):
    d = kw.get("data", {})
    if str(d.get("chat_id")) == "2":
        return _R({"ok": False, "error_code": 403,
                   "description": "Forbidden: bot was blocked by the user"})
    _notes.append(d.get("text", ""))
    return _R({"ok": True, "result": {"message_id": 1}})
__import__("requests").post = _fake
n.api("sendMessage", chat_id=2, text="новина")
check("заблокованого знято з потоку", 2 not in [p["user_id"] for p in n.readers()])
check("власниця лишається", 1 in [p["user_id"] for p in n.readers()])
check("власницю попереджено", any("більше не отримує" in t for t in _notes))
__import__("requests").post = _real_post
config.INVITE_CODE = _saved

block("Команди")
fresh_db()
sent = []
n.reply = lambda chat, text: sent.append((chat, text))
n.bootstrap_sources()
start(1, "kate")
def cmd(text, uid=1, uname="kate"):
    sent.clear()
    n.handle_command({"text": text, "from": {"id": uid, "username": uname},
                      "chat": {"id": uid}})
    return " ".join(t for _, t in sent)
check("/status відповідає", "Новинар" in cmd("/status"))
check("/help показує команди", "/add" in cmd("/help"))
before = len(n.sources())
check("/add додає канал", "Додав" in cmd("/add @tsn_ua") and len(n.sources()) == before + 1)
check("/add помічає дурницю", "не" in cmd("/add @qwerty_nemaye_takogo_2026").lower())
check("/del прибирає", "Прибрав" in cmd("/del @tsn_ua") and len(n.sources()) == before)
check("/allow впускає", "Впустив" in cmd("/allow @petro") and n.is_allowed(9, "petro"))
check("/deny відрізає", "Відрізав" in cmd("/deny @petro") and not n.is_allowed(9, "petro"))
check("чужому команди не даються", cmd("/add @x", uid=99, uname="hacker") == "" or
      "власниц" in cmd("/add @x", uid=99, uname="hacker"))
check("/stop вимикає читача", "Зупинив" in cmd("/stop"))

block("Особистий лист від власниці")
fresh_db()
_out, _says = [], []
_real_api2 = n.api
_real_reply = n.reply
n.api = lambda m, **kw: _out.append((kw.get("chat_id"), kw.get("text", ""),
                                     kw.get("disable_notification"))) or {"ok": True}
n.reply = lambda chat, text: _says.append(text)
with n.db() as c:
    c.execute("INSERT INTO people VALUES (1,'kate',1,1)")
    c.execute("INSERT INTO people VALUES (2,'druh',0,1)")

def letter(text, uid=1):
    _out[:] = []
    _says[:] = []
    n.handle_command({"text": text, "from": {"id": uid, "username": ""}, "chat": {"id": uid}})
    return " ".join(_says)

r = letter("/лист Скучаю за тобою")
check("лист доходить читачеві", bool(_out) and _out[0][0] == 2)
check("лист зі звуком", bool(_out) and _out[0][2] is False)
check("власниці — підтвердження", "Надіслано" in r)
letter("/напиши Гарного дня!")
check("працює і /напиши", bool(_out) and "Гарного дня" in _out[0][1])
check("порожній лист — підказка", "Напишіть так" in letter("/лист"))
letter("/лист <b>хитрий</b> & текст")
check("небезпечні символи екрануються", bool(_out) and "&lt;b&gt;" in _out[0][1])
letter("/лист Привіт", uid=2)
check("читачеві така команда недоступна", not _out)
n.api = _real_api2
n.reply = _real_reply

# ─────────────────────────  стійкість  ─────────────────────────
block("Відео замість картинки")
fresh_db()
_calls = []
_api_real = n.api
_grabv_real, _grabp_real, _media_real = n.grab_video, n.grab_photo, n.send_media
n.api = lambda m, **kw: _calls.append((m, kw.get("text", "")[:40])) or {"ok": True}
n.grab_video = lambda url: b"FAKEVIDEO" if url else None
n.grab_photo = lambda url: b"FAKEPHOTO" if url else None
n.send_media = lambda uid, method, field, blob, fn, mime, cap, file_id=None: (
    _calls.append((method, cap or "")) or "FILEID123")

_vid = {"channel": "tgp_news", "text": "Новина з відео про важливі події сьогодні",
        "photo": "https://cdn4.telesco.pe/file/thumb.jpg", "video": True,
        "video_url": "https://cdn4.telesco.pe/file/x.mp4", "gif": False,
        "round": False, "duration": "0:13", "link": "https://t.me/tgp_news/1", "id": 1}
n.send_post(1, dict(_vid), "Тарас")
check("відео йде як відео", any(m == "sendVideo" for m, _ in _calls),
      ", ".join(m for m, _ in _calls))
check("картинку замість відео не шле", not any(m == "sendPhoto" for m, _ in _calls))

_calls[:] = []
_gif = dict(_vid); _gif["gif"] = True
n.send_post(1, _gif, "Тарас")
check("гіфка йде гіфкою", any(m == "sendAnimation" for m, _ in _calls))

_calls[:] = []
_blocked = dict(_vid); _blocked["video_url"] = None; _blocked["video_blocked"] = True
n.send_post(1, _blocked, "Тарас")
check("недоступне вебу відео — обкладинка", any(m == "sendPhoto" for m, _ in _calls))
check("і чесна позначка про відео",
      any("відео" in c and "посиланням" in c for _, c in _calls),
      "позначка на місці" if _calls else "")
check("сторінку поста дарма не смикаємо", _blocked.get("_vresolved") is None)

_calls[:] = []
n.grab_video = lambda url: None          # відео завелике або недоступне
_big = dict(_vid)
n.send_post(1, _big, "Тарас")
check("завелике відео — обкладинка і позначка", any(m == "sendPhoto" for m, _ in _calls))
check("у тексті сказано про відео",
      any("🎬" in c or "відео" in c for _, c in _calls))

_calls[:] = []
n.grab_video = lambda url: b"FAKEVIDEO"
_shared = dict(_vid)
n.send_post(1, _shared, "Тарас")
n.send_post(2, _shared, "Тарас")
check("другому читачеві відео не качається вдруге", _shared.get("_vid_id") == "FILEID123")

_calls[:] = []
_photo_only = {"channel": "babel", "text": "Новина з картинкою про важливі події",
               "photo": "https://cdn4.telesco.pe/file/p.jpg", "video": False,
               "video_url": None, "gif": False, "round": False, "duration": "",
               "link": "https://t.me/babel/2", "id": 2}
n.send_post(1, _photo_only, "Бабель")
check("звичайне фото працює як раніше", any(m == "sendPhoto" for m, _ in _calls))

n.api, n.grab_video, n.grab_photo, n.send_media = _api_real, _grabv_real, _grabp_real, _media_real

block("Стійкість до збоїв")
check("порожній текст не шлемо", not n.passes_filters("", False, "babel")[0])
check("медіа без тексту проходить", n.passes_filters(None, True, "babel")[0])
check("битий URL картинки не валить", n.grab_photo("https://cdn4.telesco.pe/file/nema.jpg") is None)
check("не-картинка відсіюється", n.grab_photo("https://t.me/babel") is None)
check("обірваний тег закривається", n.close_tags("текст <b>жирний").endswith("</b>"))
check("зайвий закривальний тег не ламає", isinstance(n.close_tags("текст</b>"), str))

block("Пам'ять GitHub втрачена")
fresh_db()
n.bootstrap_sources()
calls = []
n.api = lambda m, **kw: calls.append(m) or {"ok": True}
n.round_trip()
sends = [c for c in calls if c.startswith("send")]
check("історією не завалить", not sends, "спроб надсилання: %s" % len(sends))
with n.db() as c:
    saved = c.execute("SELECT COUNT(*) n FROM sources WHERE last_id > 0").fetchone()["n"]
check("позиції всіх каналів записані", saved == len(config.SOURCES),
      "%s з %s" % (saved, len(config.SOURCES)))
calls.clear()
n.round_trip()
check("повторний обхід мовчить", not [c for c in calls if c.startswith("send")])
n.api = real_api

block("Живі канали")
alive = dead = 0
for ch in config.SOURCES:
    try:
        title, posts = n.fetch_channel(ch)
        if posts:
            alive += 1
        else:
            dead += 1
            print("     ⚠ %s — сторінка є, постів не видно" % ch)
    except Exception as e:
        dead += 1
        print("     ⚠ %s — %s" % (ch, str(e)[:50]))
check("всі канали читаються", dead == 0, "%s живих із %s" % (alive, len(config.SOURCES)))

print("\n" + "═" * 62)
print("  ПІДСУМОК: %s правильно, %s помилок" % (PASS, FAIL))
print("═" * 62)
sys.exit(1 if FAIL else 0)
