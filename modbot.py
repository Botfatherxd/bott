"""
Бот-модератор для чата @RuxNFT  (aiogram 3.7+)

Установка:
    pip install -U aiogram
    export BOT_TOKEN="123:ABC"      # токен от @BotFather
    python modbot.py

Настройка:
  * Добавь бота админом в @RuxNFT (права: удалять сообщения, банить, ограничивать).
  * Добавь бота админом в каналы/чаты из /add, иначе проверка подписки не работает.
  * Админам чата отключи «анонимность», иначе команды от их имени не видны боту.
"""
import asyncio
import html
import logging
import os
import random
import re
import sqlite3
import time
from datetime import datetime, timedelta, timezone

from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatMemberStatus as S
from aiogram.enums import ChatType, ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import (CallbackQuery, ChatPermissions, InlineKeyboardButton,
                           InlineKeyboardMarkup, Message)

TOKEN = os.getenv("BOT_TOKEN", "")
CHAT_USERNAME = "RuxNFT"
OWNER_USERNAME = "RuxDev"
TZ = timezone(timedelta(hours=3))  # МСК
OWNER_RANK = 100
RANKS = {0: "участник", 1: "модератор", 2: "старший модератор",
         3: "администратор", OWNER_RANK: "владелец"}

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("modbot")
router = Router()
bot: Bot
CHAT_ID: int = 0

# ───────────────────────── БАЗА ─────────────────────────
db = sqlite3.connect("modbot.db", check_same_thread=False)
db.row_factory = sqlite3.Row
db.executescript("""
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT, name TEXT,
    msgs INTEGER DEFAULT 0, first_seen INTEGER);
CREATE TABLE IF NOT EXISTS admins(user_id INTEGER PRIMARY KEY, rank INTEGER);
CREATE TABLE IF NOT EXISTS daily(day TEXT PRIMARY KEY, cnt INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS subs(id INTEGER PRIMARY KEY AUTOINCREMENT, ref TEXT UNIQUE,
    tg_id INTEGER, title TEXT, link TEXT, kind TEXT);
CREATE TABLE IF NOT EXISTS raffles(id INTEGER PRIMARY KEY AUTOINCREMENT, msg_id INTEGER,
    title TEXT, descr TEXT, cond TEXT, winners INTEGER, min_msgs INTEGER,
    end_ts INTEGER, status TEXT DEFAULT 'active');
CREATE TABLE IF NOT EXISTS raffle_members(raffle_id INTEGER, user_id INTEGER,
    PRIMARY KEY(raffle_id, user_id));
CREATE TABLE IF NOT EXISTS spam_cases(id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER,
    name TEXT, text TEXT, ts INTEGER, status TEXT DEFAULT 'pending');
""")


def q(sql, *a):
    return db.execute(sql, a).fetchall()


def q1(sql, *a):
    return db.execute(sql, a).fetchone()


def ex(sql, *a):
    cur = db.execute(sql, a)
    db.commit()
    return cur


# ───────────────────────── ХЕЛПЕРЫ ─────────────────────────
def esc(s):
    return html.escape(str(s or ""))


def mention(uid, name):
    return f'<a href="tg://user?id={uid}">{esc(name)}</a>'


def today():
    return datetime.now(TZ).strftime("%Y-%m-%d")


def upsert(u):
    ex("""INSERT INTO users(id,username,name,first_seen) VALUES(?,?,?,?)
          ON CONFLICT(id) DO UPDATE SET username=excluded.username, name=excluded.name""",
       u.id, u.username, u.full_name, int(time.time()))


def rank_id(uid):
    u = q1("SELECT username FROM users WHERE id=?", uid)
    if u and u["username"] and u["username"].lower() == OWNER_USERNAME.lower():
        return OWNER_RANK
    r = q1("SELECT rank FROM admins WHERE user_id=?", uid)
    return r["rank"] if r else 0


def rank_of(user):
    return rank_id(user.id)


UNITS = {"m": 60, "м": 60, "h": 3600, "ч": 3600, "d": 86400, "д": 86400,
         "w": 604800, "н": 604800}


def parse_dur(s):
    """30m / 2h / 1d / 1w -> секунды; 0 / perm / навсегда -> 0; ошибка -> None"""
    s = s.lower()
    if s in ("0", "perm", "forever", "навсегда"):
        return 0
    m = re.fullmatch(r"(\d+)([mмhчdдwн])", s)
    if not m:
        return None
    return max(60, int(m[1]) * UNITS[m[2]])


def fmt_dur(s):
    if not s:
        return "навсегда"
    parts = []
    for n, u in ((86400, "д"), (3600, "ч"), (60, "мин")):
        v, s = divmod(s, n)
        if v:
            parts.append(f"{v} {u}")
    return " ".join(parts) or "1 мин"


def lookup(token):
    token = token.lstrip("@")
    if token.isdigit():
        row = q1("SELECT id,name FROM users WHERE id=?", int(token))
        return int(token), (row["name"] if row else token)
    row = q1("SELECT id,name FROM users WHERE lower(username)=?", token.lower())
    return (row["id"], row["name"]) if row else None


def find_target(m: Message, args):
    """Возвращает (uid, name, остаток_аргументов, ошибка)."""
    r = m.reply_to_message
    if r and r.from_user and not r.forum_topic_created:
        if not r.from_user.is_bot:
            upsert(r.from_user)
        return r.from_user.id, r.from_user.full_name, args, None
    if args and (args[0].startswith("@") or args[0].isdigit()):
        t = lookup(args[0])
        if t:
            return t[0], t[1], args[1:], None
        return None, None, args, ("Пользователь не найден. Он должен хотя бы раз "
                                  "написать в чате или боту.")
    return None, None, args, "Укажи пользователя: ответом на сообщение или @username."


async def say(m: Message, text, **kw):
    """Ответ там, где написали команду; из ЛС — ещё и в чат."""
    await m.answer(text, **kw)


# ───────────────────────── ПОДПИСКИ ─────────────────────────
ok_cache: dict[int, float] = {}
warn_cache: dict[int, float] = {}


async def missing_subs(uid):
    if ok_cache.get(uid, 0) > time.time():
        return []
    out = []
    for s in q("SELECT * FROM subs WHERE kind != 'bot'"):
        try:
            mem = await bot.get_chat_member(s["tg_id"], uid)
            if mem.status in (S.LEFT, S.KICKED) or (
                    mem.status == S.RESTRICTED and not mem.is_member):
                out.append(s)
        except Exception as e:
            log.warning("Не могу проверить %s: %s", s["title"], e)
    if not out:
        ok_cache[uid] = time.time() + 60
    return out


def sub_kb():
    rows = [[InlineKeyboardButton(text=f"{'🤖' if s['kind'] == 'bot' else '📢'} {s['title']}",
                                  url=s["link"])]
            for s in q("SELECT * FROM subs") if s["link"]]
    rows.append([InlineKeyboardButton(text="✅ Проверить", callback_data="chk")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


async def del_later(msg, sec):
    await asyncio.sleep(sec)
    try:
        await msg.delete()
    except Exception:
        pass


@router.callback_query(F.data == "chk")
async def cb_check(c: CallbackQuery):
    ok_cache.pop(c.from_user.id, None)
    miss = await missing_subs(c.from_user.id)
    if miss:
        await c.answer("Ты ещё не подписан: " + ", ".join(s["title"] for s in miss)[:150],
                       show_alert=True)
    else:
        await c.answer("✅ Всё в порядке, можешь писать!", show_alert=True)


# ───────────────────────── АВТОМОДЕРАЦИЯ ─────────────────────────
# Паттерны можно свободно править/дополнять. Проверка идёт по тексту в нижнем регистре,
# знаки препинания игнорируются, латинские «двойники» букв (a, c, o...) учитываются.
SPAM_PATTERNS = [
    # «пишите в лс», «пиши мне в личку», «обращайтесь в лс»
    r"\b(пиш\w*|напиш\w*|обраща\w*|стуч\w*|кидай\w*|отпиш\w*)\s+(мне\s+|нам\s+|сюда\s+)?(в\s+)?(лс|личк\w*|личные|пм|директ|dm|pm)\b",
    # «первому в лс», «каждому в лс», «первому кто напишет в лс»
    r"\b(первому|первым|первых|каждому|желающим|нужным)\s+(\w+\s+){0,3}?(в\s+)?(лс|личк\w*|личные|директ)\b",
    r"\bвсем\s+в\s+(лс|личк\w*)\b",
    r"\bв\s+(лс|личк\w*)\s+(пиш\w*|напиш\w*)",
    # iCloud / Apple ID / «у кого айфон»
    r"\b(icloud|айклауд|айклоуд|apple\s?id|эпл\s?айди|апл\s?айди)\b",
    r"\bу\s+кого\s+(есть\s+|же\s+)?(айфон\w*|iphone|айпад\w*|ipad|макбук\w*|macbook)",
    r"\b(разблокир\w*|отвяз\w*|взлом\w*)\s+(\w+\s+)?(айфон\w*|iphone|айпад\w*|ipad|icloud|айклауд)",
    # «заработок», «без вложений»
    r"\b(заработ\w+|доход\w*|прибыл\w*)\b.{0,40}(в\s+день|в\s+неделю|в\s+час|\d+\s?(\$|usdt|руб|₽|тыс))",
    r"\b(без\s+вложений|пассивн\w+\s+доход|удаленн\w+\s+(работ|заработ)\w*|подработк\w+)",
]
SPAM_RE = [re.compile(p) for p in SPAM_PATTERNS]
LATIN2CYR = str.maketrans("acekmhoprtxyb", "асекмнорртхув")


def is_spam(text: str) -> bool:
    t = (text or "").lower().replace("ё", "е")
    t = re.sub(r"[\u200b-\u200f\u2060\ufeff]", "", t)       # невидимые символы
    t = re.sub(r"[^\w\s$₽]", " ", t)                          # пунктуация -> пробел
    t = re.sub(r"\s+", " ", t)
    variants = {t, t.translate(LATIN2CYR)}
    return any(r.search(v) for r in SPAM_RE for v in variants)


async def handle_spam(m: Message):
    u = m.from_user
    text = m.text or m.caption or ""
    cid = ex("INSERT INTO spam_cases(user_id,name,text,ts) VALUES(?,?,?,?)",
             u.id, u.full_name, text, int(time.time())).lastrowid
    try:
        await m.delete()
    except Exception:
        pass
    try:  # мут до решения модератора
        await bot.restrict_chat_member(
            CHAT_ID, u.id, permissions=ChatPermissions(can_send_messages=False))
    except TelegramBadRequest as e:
        log.warning("Автомут не удался: %s", e)
    w = await bot.send_message(
        CHAT_ID, f"🛡 {mention(u.id, u.full_name)} — подозрение на спам. "
                 f"Мут до проверки модератором.")
    asyncio.create_task(del_later(w, 30))

    uname = f"@{u.username}" if u.username else "без username"
    report = (f"🛡 <b>Автомодерация</b>\n"
              f"Пользователь: {mention(u.id, u.full_name)} ({esc(uname)}, id <code>{u.id}</code>)\n\n"
              f"💬 {esc(text[:1500])}\n\n<b>Это спам?</b>")
    kb = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🚫 Спам (бан)", callback_data=f"sp:{cid}:1"),
        InlineKeyboardButton(text="✅ Не спам (размут)", callback_data=f"sp:{cid}:0")]])
    owners = [r["id"] for r in q("SELECT id FROM users WHERE lower(username)=?",
                                 OWNER_USERNAME.lower())]
    others = [x for x in staff_ids() if x not in owners]
    for uid in owners + others:          # сначала владелец, если не вышло — остальные админы
        try:
            await bot.send_message(uid, report, reply_markup=kb)
            return
        except Exception:
            continue
    log.warning("Некому отправить проверку спама — владелец не запускал бота (/start в ЛС)")


@router.callback_query(F.data.startswith("sp:"))
async def cb_spam(c: CallbackQuery):
    if rank_of(c.from_user) < 2:
        return await c.answer()
    _, cid, flag = c.data.split(":")
    case = q1("SELECT * FROM spam_cases WHERE id=?", int(cid))
    if not case or case["status"] != "pending":
        return await c.answer("Уже обработано", show_alert=True)
    try:
        if flag == "1":
            await bot.ban_chat_member(CHAT_ID, case["user_id"])
            status, verdict = "spam", "🚫 Спам — пользователь забанен"
        else:
            await bot.restrict_chat_member(CHAT_ID, case["user_id"], permissions=FULL)
            status, verdict = "ok", "✅ Не спам — мут снят"
    except TelegramBadRequest as e:
        return await c.answer(f"Ошибка: {e.message}"[:190], show_alert=True)
    ex("UPDATE spam_cases SET status=? WHERE id=?", status, case["id"])
    try:
        await c.message.edit_text(f"{c.message.html_text}\n\n{verdict} ({esc(c.from_user.full_name)})",
                                  reply_markup=None)
    except Exception:
        pass
    await c.answer("Готово")


# ───────────────────────── ГЛАВНЫЙ ФИЛЬТР ─────────────────────────
class Guard(BaseMiddleware):
    async def __call__(self, handler, event: Message, data):
        u = event.from_user
        if not u or u.is_bot:
            return
        upsert(u)

        if event.chat.type == ChatType.PRIVATE:
            if rank_of(u) < 1:          # в ЛС обычным людям не отвечаем
                return
            return await handler(event, data)

        if event.chat.id != CHAT_ID:    # работаем только в @RuxNFT
            try:
                await event.chat.leave()
            except Exception:
                pass
            return

        if (event.text or event.caption or event.sticker or event.photo or event.video
                or event.voice or event.animation or event.document
                or event.video_note or event.audio):
            ex("UPDATE users SET msgs=msgs+1 WHERE id=?", u.id)
            ex("""INSERT INTO daily(day,cnt) VALUES(?,1)
                  ON CONFLICT(day) DO UPDATE SET cnt=cnt+1""", today())

        if rank_of(u) < 1 and q1("SELECT 1 FROM subs"):
            miss = await missing_subs(u.id)
            if miss:
                try:
                    await event.delete()
                except Exception:
                    pass
                if time.time() - warn_cache.get(u.id, 0) > 10:
                    warn_cache[u.id] = time.time()
                    w = await bot.send_message(
                        CHAT_ID,
                        f"{mention(u.id, u.full_name)}, чтобы писать в чате, "
                        f"подпишись на всё из списка 👇",
                        reply_markup=sub_kb())
                    asyncio.create_task(del_later(w, 25))
                return

        if rank_of(u) < 1 and is_spam(event.text or event.caption or ""):
            await handle_spam(event)
            return
        return await handler(event, data)


# ───────────────────────── МОДЕРАЦИЯ ─────────────────────────
FULL = ChatPermissions(
    can_send_messages=True, can_send_audios=True, can_send_documents=True,
    can_send_photos=True, can_send_videos=True, can_send_video_notes=True,
    can_send_voice_notes=True, can_send_polls=True, can_send_other_messages=True,
    can_add_web_page_previews=True, can_invite_users=True)


async def moderate(m: Message, cmd: CommandObject, kind: str):
    me = rank_of(m.from_user)
    if me < (2 if kind == "ban" else 1):
        return
    uid, name, rest, err = find_target(m, (cmd.args or "").split())
    usage = (f"Формат: /{kind} [@user] время причина\n"
             f"Время: 30m, 2h, 1d, 1w или 0 (навсегда).\n"
             f"В чате — ответом на сообщение, в ЛС — с @username.")
    if err:
        return await m.answer(f"{err}\n\n{usage}")
    secs = parse_dur(rest[0]) if rest else None
    if secs is None:
        return await m.answer(usage)
    if uid == bot.id or rank_id(uid) >= me:
        return await m.answer("⛔ Нельзя применить к этому пользователю.")
    reason = " ".join(rest[1:]) or "не указана"
    until = datetime.now(timezone.utc) + timedelta(seconds=secs) if secs else None
    try:
        if kind == "ban":
            await bot.ban_chat_member(CHAT_ID, uid, until_date=until)
        else:
            await bot.restrict_chat_member(
                CHAT_ID, uid, permissions=ChatPermissions(can_send_messages=False),
                until_date=until)
    except TelegramBadRequest as e:
        return await m.answer(f"Ошибка Telegram: {esc(e.message)}")
    icon, word = ("🔨", "забанен") if kind == "ban" else ("🔇", "в муте")
    await bot.send_message(
        CHAT_ID,
        f"{icon} {mention(uid, name)} {word}\n⏱ Срок: {fmt_dur(secs)}\n"
        f"📝 Причина: {esc(reason)}\n👮 Модератор: {mention(m.from_user.id, m.from_user.full_name)}")
    if m.chat.type == ChatType.PRIVATE:
        await m.answer("✅ Готово")


@router.message(Command("ban"))
async def cmd_ban(m: Message, command: CommandObject):
    await moderate(m, command, "ban")


@router.message(Command("mute"))
async def cmd_mute(m: Message, command: CommandObject):
    await moderate(m, command, "mute")


@router.message(Command("unban", "unmute"))
async def cmd_unpunish(m: Message, command: CommandObject):
    is_ban = m.text.lstrip("/").lower().startswith("unban")
    if rank_of(m.from_user) < (2 if is_ban else 1):
        return
    uid, name, _, err = find_target(m, (command.args or "").split())
    if err:
        return await m.answer(err)
    try:
        if is_ban:
            await bot.unban_chat_member(CHAT_ID, uid, only_if_banned=True)
        else:
            await bot.restrict_chat_member(CHAT_ID, uid, permissions=FULL)
    except TelegramBadRequest as e:
        return await m.answer(f"Ошибка Telegram: {esc(e.message)}")
    await bot.send_message(CHAT_ID, f"✅ С {mention(uid, name)} сняты ограничения")


@router.message(Command("admin"))
async def cmd_admin(m: Message, command: CommandObject):
    if rank_of(m.from_user) != OWNER_RANK:
        return
    uid, name, rest, err = find_target(m, (command.args or "").split())
    usage = ("Формат: /admin @username ранг\n"
             "0 — снять, 1 — мут, 2 — + бан и розыгрыши, 3 — + подписки (/add)")
    if err:
        return await m.answer(f"{err}\n\n{usage}")
    if not rest or rest[0] not in ("0", "1", "2", "3"):
        return await m.answer(usage)
    rk = int(rest[0])
    if rank_id(uid) == OWNER_RANK:
        return await m.answer("Владельца менять нельзя.")
    if rk == 0:
        ex("DELETE FROM admins WHERE user_id=?", uid)
        return await m.answer(f"➖ {mention(uid, name)} больше не админ")
    ex("INSERT INTO admins(user_id,rank) VALUES(?,?) ON CONFLICT(user_id) DO UPDATE SET rank=?",
       uid, rk, rk)
    await m.answer(f"➕ {mention(uid, name)} теперь {RANKS[rk]} (ранг {rk})")


# ───────────────────────── СТАТИСТИКА ─────────────────────────
@router.message(Command("stat"))
async def cmd_stat(m: Message):
    total = q1("SELECT COALESCE(SUM(msgs),0) s, COUNT(*) c FROM users WHERE msgs>0")
    day = q1("SELECT cnt FROM daily WHERE day=?", today())
    top = q("SELECT id,name,msgs FROM users WHERE msgs>0 ORDER BY msgs DESC LIMIT 5")
    lines = [f"{i}. {mention(r['id'], r['name'])} — {r['msgs']}" for i, r in enumerate(top, 1)]
    await m.answer(
        f"📊 <b>Статистика @{CHAT_USERNAME}</b>\n\n"
        f"💬 Всего сообщений: <b>{total['s']}</b>\n"
        f"📅 Сегодня: <b>{day['cnt'] if day else 0}</b>\n"
        f"👥 Писали в чате: <b>{total['c']}</b>\n\n"
        f"🏆 <b>Топ-5</b>\n" + ("\n".join(lines) or "пока пусто"))


@router.message(Command("me"))
async def cmd_me(m: Message):
    u = q1("SELECT * FROM users WHERE id=?", m.from_user.id)
    place = q1("SELECT COUNT(*)+1 p FROM users WHERE msgs>?", u["msgs"])["p"]
    since = datetime.fromtimestamp(u["first_seen"], TZ).strftime("%d.%m.%Y")
    await m.answer(
        f"👤 {mention(u['id'], u['name'])}\n"
        f"🎖 Роль: {RANKS.get(rank_id(u['id']), 'админ')}\n"
        f"💬 Сообщений: <b>{u['msgs']}</b>\n"
        f"🏅 Место в топе: <b>{place}</b>\n"
        f"📆 В базе с: {since}")


# ───────────────────────── РЕПОРТ / АДМИНЫ ─────────────────────────
last_report: dict[int, float] = {}
last_call = 0.0


def staff_ids():
    ids = {r["user_id"] for r in q("SELECT user_id FROM admins WHERE rank>0")}
    ids |= {r["id"] for r in q("SELECT id FROM users WHERE lower(username)=?",
                               OWNER_USERNAME.lower())}
    return ids


@router.message(Command("report"))
async def cmd_report(m: Message, command: CommandObject):
    args = (command.args or "").split()
    r = m.reply_to_message
    usage = "Формат: /report причина @username\nили ответом на сообщение: /report причина"
    link_id = None
    if r and r.from_user and not r.forum_topic_created:
        tid, tname, reason, link_id = (r.from_user.id, r.from_user.full_name,
                                       " ".join(args), r.message_id)
    elif len(args) >= 2 and lookup(args[-1]):
        tid, tname = lookup(args[-1])
        reason = " ".join(args[:-1])
    else:
        return await m.answer(usage)
    if not reason:
        return await m.answer(usage)
    if time.time() - last_report.get(m.from_user.id, 0) < 30:
        return await m.answer("⏳ Подожди немного перед следующей жалобой.")
    last_report[m.from_user.id] = time.time()
    text = (f"🚨 <b>Жалоба</b>\nОт: {mention(m.from_user.id, m.from_user.full_name)}\n"
            f"На: {mention(tid, tname)}\nПричина: {esc(reason)}")
    if link_id:
        text += f'\n<a href="https://t.me/{CHAT_USERNAME}/{link_id}">Перейти к сообщению</a>'
    for uid in staff_ids():
        try:
            await bot.send_message(uid, text)
        except Exception:
            pass  # админ ещё не запускал бота
    await m.reply("✅ Жалоба отправлена администрации.")


@router.message(Command("admins"))
async def cmd_admins(m: Message, command: CommandObject):
    global last_call
    if time.time() - last_call < 60:
        return
    last_call = time.time()
    ids = staff_ids()
    tags = []
    for uid in ids:
        u = q1("SELECT name FROM users WHERE id=?", uid)
        tags.append(mention(uid, u["name"] if u else "админ"))
    note = f"\n📝 {esc(command.args)}" if command.args else ""
    await bot.send_message(
        CHAT_ID, f"🚨 {mention(m.from_user.id, m.from_user.full_name)} зовёт админов!{note}\n\n"
                 + " ".join(tags))


# ───────────────────────── ОБЯЗАТЕЛЬНАЯ ПОДПИСКА ─────────────────────────
def norm_ref(a):
    a = a.strip()
    mm = re.match(r"(?:https?://)?t\.me/([A-Za-z0-9_]{4,})/?$", a)
    if mm:
        return "@" + mm[1]
    if re.fullmatch(r"-?\d+", a):
        return int(a)
    return a if a.startswith("@") else "@" + a


@router.message(Command("add"))
async def cmd_add(m: Message, command: CommandObject):
    if rank_of(m.from_user) < 3:
        return
    arg = (command.args or "").strip()
    if not arg:
        rows = q("SELECT * FROM subs")
        txt = "\n".join(f"{r['id']}. {r['kind']}: {esc(r['title'])}" for r in rows) or "пусто"
        return await m.answer(f"📌 <b>Обязательные подписки:</b>\n{txt}\n\n"
                              f"Добавить: /add @канал_чат_или_бот\nУдалить: /del номер")
    ref = norm_ref(arg)
    try:
        chat = await bot.get_chat(ref)
        kind = {"channel": "канал", "private": "бот"}.get(chat.type, "чат")
        title = chat.title or chat.full_name or str(ref)
        tg_id, uname = chat.id, chat.username
        link = f"https://t.me/{uname}" if uname else chat.invite_link
    except TelegramBadRequest:
        if isinstance(ref, str) and ref.lower().endswith("bot"):
            kind, title, tg_id, uname = "бот", ref, None, ref[1:]
            link = f"https://t.me/{uname}"
        else:
            return await m.answer("❌ Не нашёл такой канал/чат. Нужен @username или id, "
                                  "и бот должен быть в нём.")
    warn = ""
    if kind != "бот":
        try:
            me = await bot.get_chat_member(tg_id, bot.id)
            if me.status not in (S.ADMINISTRATOR, S.CREATOR):
                raise ValueError
        except Exception:
            warn = "\n⚠️ Бот не админ там — проверка подписки работать не будет."
        if not link:
            try:
                link = await bot.export_chat_invite_link(tg_id)
            except Exception:
                warn += "\n⚠️ Нет ссылки для кнопки (дай боту право приглашать)."
    elif tg_id is None or True:
        warn = "\nℹ️ Подписку на бота Telegram проверить не позволяет — он покажется кнопкой, без принуждения."
    ex("""INSERT INTO subs(ref,tg_id,title,link,kind) VALUES(?,?,?,?,?)
          ON CONFLICT(ref) DO UPDATE SET title=excluded.title, link=excluded.link""",
       str(tg_id or ref), tg_id, title, link, kind if kind != "бот" else "bot")
    ok_cache.clear()
    await m.answer(f"✅ Добавлено ({kind}): {esc(title)}{warn}")


@router.message(Command("del"))
async def cmd_del(m: Message, command: CommandObject):
    if rank_of(m.from_user) < 3:
        return
    if not (command.args or "").strip().isdigit():
        return await m.answer("Формат: /del номер (список — /add)")
    cur = ex("DELETE FROM subs WHERE id=?", int(command.args))
    ok_cache.clear()
    await m.answer("🗑 Удалено" if cur.rowcount else "Нет такого номера")


# ───────────────────────── РОЗЫГРЫШИ ─────────────────────────
def raffle_text(r, extra=""):
    end = datetime.fromtimestamp(r["end_ts"], TZ).strftime("%d.%m.%Y %H:%M МСК")
    t = (f"🎁 <b>{esc(r['title'])}</b>\n\n{esc(r['descr'])}\n\n"
         f"📌 Условие: {esc(r['cond'])}\n🏆 Победителей: {r['winners']}\n")
    if r["min_msgs"]:
        t += f"💬 Минимум сообщений в чате: {r['min_msgs']}\n"
    return t + f"⏰ Итоги: {end}" + extra


def raffle_kb(rid):
    n = q1("SELECT COUNT(*) c FROM raffle_members WHERE raffle_id=?", rid)["c"]
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(
        text=f"🎉 Участвовать ({n})", callback_data=f"r:{rid}")]])


@router.message(Command("create"))
async def cmd_create(m: Message, command: CommandObject):
    if rank_of(m.from_user) < 2:
        return
    p = [x.strip() for x in (command.args or "").split("|")]
    usage = ("Формат:\n<code>/create время | название | описание | условие | "
             "победителей | мин. сообщений</code>\n"
             "Последние два — необязательные.\n"
             "Пример:\n<code>/create 1d | NFT-дроп | Разыгрываем NFT | "
             "Быть подписанным на канал | 3 | 20</code>")
    secs = parse_dur(p[0]) if p and p[0] else None
    if len(p) < 4 or not secs:
        return await m.answer(usage)
    winners = int(p[4]) if len(p) > 4 and p[4].isdigit() and int(p[4]) > 0 else 1
    min_msgs = int(p[5]) if len(p) > 5 and p[5].isdigit() else 0
    cur = ex("INSERT INTO raffles(title,descr,cond,winners,min_msgs,end_ts) VALUES(?,?,?,?,?,?)",
             p[1], p[2], p[3], winners, min_msgs, int(time.time()) + secs)
    rid = cur.lastrowid
    msg = await bot.send_message(CHAT_ID, raffle_text(q1("SELECT * FROM raffles WHERE id=?", rid)),
                                 reply_markup=raffle_kb(rid))
    ex("UPDATE raffles SET msg_id=? WHERE id=?", msg.message_id, rid)
    if m.chat.type == ChatType.PRIVATE:
        await m.answer("✅ Розыгрыш опубликован в чате")


@router.callback_query(F.data.startswith("r:"))
async def cb_join(c: CallbackQuery):
    if not c.message or c.message.chat.id != CHAT_ID:
        return await c.answer()
    r = q1("SELECT * FROM raffles WHERE id=?", int(c.data[2:]))
    if not r or r["status"] != "active" or r["end_ts"] <= time.time():
        return await c.answer("Розыгрыш уже завершён", show_alert=True)
    upsert(c.from_user)
    miss = await missing_subs(c.from_user.id)
    if miss:
        return await c.answer("Сначала подпишись: " + ", ".join(s["title"] for s in miss)[:150],
                              show_alert=True)
    msgs = q1("SELECT msgs FROM users WHERE id=?", c.from_user.id)["msgs"]
    if msgs < r["min_msgs"]:
        return await c.answer(f"Нужно минимум {r['min_msgs']} сообщений в чате, у тебя {msgs}.",
                              show_alert=True)
    cur = ex("INSERT OR IGNORE INTO raffle_members VALUES(?,?)", r["id"], c.from_user.id)
    if not cur.rowcount:
        return await c.answer("Ты уже участвуешь 😉", show_alert=True)
    try:
        await c.message.edit_reply_markup(reply_markup=raffle_kb(r["id"]))
    except TelegramBadRequest:
        pass
    await c.answer("✅ Ты участвуешь!")


async def raffle_loop():
    while True:
        await asyncio.sleep(15)
        try:
            for r in q("SELECT * FROM raffles WHERE status='active' AND end_ts<=?",
                       int(time.time())):
                ids = [x["user_id"] for x in
                       q("SELECT user_id FROM raffle_members WHERE raffle_id=?", r["id"])]
                win = random.sample(ids, min(len(ids), r["winners"]))
                ex("UPDATE raffles SET status='done' WHERE id=?", r["id"])
                names = []
                for uid in win:
                    u = q1("SELECT name FROM users WHERE id=?", uid)
                    names.append(mention(uid, u["name"] if u else uid))
                result = ("🏆 Победители: " + ", ".join(names)) if names else \
                         "😔 Никто не участвовал"
                try:
                    await bot.edit_message_text(
                        raffle_text(r, f"\n\n✅ <b>Завершён</b>\n{result}"),
                        chat_id=CHAT_ID, message_id=r["msg_id"])
                except Exception:
                    pass
                await bot.send_message(
                    CHAT_ID, f"🎉 Розыгрыш «{esc(r['title'])}» завершён!\n{result}",
                    reply_to_message_id=r["msg_id"])
        except Exception:
            log.exception("raffle_loop")


# ───────────────────────── ПОМОЩЬ ─────────────────────────
@router.message(Command("start", "help"))
async def cmd_help(m: Message):
    rk = rank_of(m.from_user)
    t = ("<b>Команды для всех:</b>\n/stat — статистика чата\n/me — моя статистика\n"
         "/report причина @user — жалоба (или ответом)\n/admins — позвать админов\n")
    if rk >= 1:
        t += "\n<b>Модераторы:</b>\n/mute [@user] время причина\n/unmute [@user]\n"
    if rk >= 2:
        t += "/ban [@user] время причина\n/unban [@user]\n/create время | название | описание | условие | победителей | мин.сообщ.\n"
    if rk >= 3:
        t += "\n<b>Админы:</b>\n/add — список подписок / добавить\n/del номер — убрать\n"
    if rk == OWNER_RANK:
        t += "\n<b>Владелец:</b>\n/admin @user ранг (0–3)\n"
    await m.answer(t)


# ───────────────────────── ЗАПУСК ─────────────────────────
async def main():
    global bot, CHAT_ID
    bot = Bot(TOKEN, default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    CHAT_ID = (await bot.get_chat(f"@{CHAT_USERNAME}")).id
    log.info("Работаю в чате @%s (%s)", CHAT_USERNAME, CHAT_ID)
    dp = Dispatcher()
    dp.message.outer_middleware(Guard())
    dp.include_router(router)
    asyncio.create_task(raffle_loop())
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
