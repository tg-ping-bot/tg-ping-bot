import asyncio
import logging
import time
import os
import random
import re
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery, BotCommand
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ChatType, ChatMemberStatus
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

BOT_TOKEN = os.getenv("BOT_TOKEN")
if not BOT_TOKEN:
    logging.error("❌ ОШИБКА: BOT_TOKEN не найден!")
    exit(1)

DB_PATH = "chat_users.db"
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
call_lock = asyncio.Lock()
active_tasks = {}

FAIL_MESSAGES = [
    "Жертва оказалась ниндзя 🌫️", "Споткнулся о шнурок 👟",
    "Страж чата спугнул тебя 👮‍♂️", "Добыча ушла через чёрный ход 🚪",
    "Крался тише мыши, но спалил себя 🐭", "Ворона отобрала монету 🐦‍",
    "Жертва включила невидимость 👻", "Инфляция съела награду 📉"
]

# ================= DATABASE =================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""CREATE TABLE IF NOT EXISTS active_users (
            user_id INTEGER PRIMARY KEY, username TEXT, first_name TEXT,
            last_seen REAL, immune_until REAL DEFAULT 0, is_premium INTEGER DEFAULT 0,
            rankoins INTEGER DEFAULT 0, last_bonus REAL DEFAULT 0, last_rob REAL DEFAULT 0)""")
        await db.commit()

async def upsert_user(user_id, username, first_name):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR IGNORE INTO active_users (user_id, username, first_name, last_seen) VALUES (?, ?, ?, ?)",
                         (user_id, username, first_name, time.time()))
        await db.commit()

async def get_active_users():
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, username, first_name FROM active_users WHERE immune_until < ?", (now,)) as cur:
            return await cur.fetchall()

async def get_balance(user_id):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT rankoins, last_bonus, last_rob FROM active_users WHERE user_id = ?", (user_id,)) as cur:
            return await cur.fetchone()

async def add_rankoins(user_id, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE active_users SET rankoins = rankoins + ? WHERE user_id = ?", (amount, user_id))
        await db.commit()

async def remove_rankoins(user_id, amount):
    async with aiosqlite.connect(DB_PATH) as db:
        res = await db.execute("UPDATE active_users SET rankoins = rankoins - ? WHERE user_id = ? AND rankoins >= ?", (amount, user_id, amount))
        await db.commit()
        return res.rowcount > 0

async def db_update_last_bonus(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE active_users SET last_bonus = ? WHERE user_id = ?", (time.time(), uid))
        await db.commit()

async def db_update_last_rob(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE active_users SET last_rob = ? WHERE user_id = ?", (time.time(), uid))
        await db.commit()

async def get_user_premium_status(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT is_premium FROM active_users WHERE user_id = ?", (uid,)) as cur:
            res = await cur.fetchone()
            return res[0] if res else 0

async def get_all_users_map():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, username FROM active_users") as cur:
            rows = await cur.fetchall()
    user_map = {}
    for uid, uname in rows:
        if uname: user_map[uname.lower()] = uid
        user_map[f"id_{uid}"] = uid
    return user_map

# ================= HELPERS =================
async def is_chat_admin(chat_id, user_id):
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)
    except: return False

def make_mention(user_id, username, first_name):
    display_name = first_name or username or "Участник"
    return f'<a href="tg://user?id={user_id}">{display_name}</a>'

def schedule_delete(chat_id, message_id, delay=10.0):
    async def _delete():
        try:
            await asyncio.sleep(delay)
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except: pass
    asyncio.create_task(_delete())

# ================= 📜 КОМАНДЫ (ЛАТИНИЦА) =================
async def register_commands(bot):
    commands = [
        BotCommand(command="bonus", description="Получить 100 Ранкоинов"),
        BotCommand(command="long", description="Ограбить участника (ответом)"),
        BotCommand(command="balance", description="Проверить баланс"),
        BotCommand(command="top", description="Топ-10 богачей"),
        BotCommand(command="gift", description="Получить Премиум"),
        BotCommand(command="notag", description="Включить защиту (Премиум)"),
        BotCommand(command="call", description="Общий сбор (Админ)"),
        BotCommand(command="quest", description="Создать задание (Админ)"),
        BotCommand(command="award", description="Выдать монеты (Админ)")
    ]
    await bot.set_my_commands(commands)
    logging.info("✅ Команды зарегистрированы")

# ================= 💰 ЭКОНОМИКА =================
@dp.message(Command("bonus", "бонус"))
async def cmd_bonus(message: Message):
    uid = message.from_user.id
    data = await get_balance(uid)
    if not data:
        msg = await message.answer("Сначала напиши что-нибудь в чат.")
        schedule_delete(message.chat.id, msg.message_id)
        return
    rankoins, last_bonus, _ = data
    now = time.time()
    if now - last_bonus < 86400:
        wait = int((last_bonus + 86400) - now)
        msg = await message.answer(f"⏳ Жди: {wait//3600}ч {(wait%3600)//60}мин")
        schedule_delete(message.chat.id, msg.message_id)
        return
    await add_rankoins(uid, 100)
    await db_update_last_bonus(uid)
    msg = await message.answer("💰 +100 Ранкоинов!")
    schedule_delete(message.chat.id, msg.message_id)

@dp.message(Command("long"))
async def cmd_long(message: Message):
    uid = message.from_user.id
    data = await get_balance(uid)
    if not data:
        msg = await message.answer("Сначала напиши в чат для регистрации.")
        schedule_delete(message.chat.id, msg.message_id)
        return
    if not message.reply_to_message:
        msg = await message.answer("❌ Ответь на сообщение жертвы!")
        schedule_delete(message.chat.id, msg.message_id)
        return
    target_id = message.reply_to_message.from_user.id
    if target_id == uid:
        msg = await message.answer("🤡 Себя грабить нельзя!")
        schedule_delete(message.chat.id, msg.message_id)
        return
    target_data = await get_balance(target_id)
    if not target_data:
        msg = await message.answer("❌ Жертва не в базе.")
        schedule_delete(message.chat.id, msg.message_id)
        return
    _, _, last_rob = data
    now = time.time()
    if now - last_rob < 7200:
        wait = int((last_rob + 7200) - now)
        msg = await message.answer(f"🕵️‍♂️ Кулдаун: {wait//60} мин")
        schedule_delete(message.chat.id, msg.message_id)
        return
    await db_update_last_rob(uid)
    if random.random() > 0.10:
        text = f"🕵️‍♂️ Провал! {random.choice(FAIL_MESSAGES)}"
        msg = await message.answer(text)
        schedule_delete(message.chat.id, msg.message_id)
        return
    desired = random.randint(1, 100)
    actual = min(desired, target_data[0])
    target_name = message.reply_to_message.from_user.first_name
    if actual > 0:
        await remove_rankoins(target_id, actual)
        await add_rankoins(uid, actual)
        text = f"💸 Украл {actual} у {target_name}!"
    else:
        text = f"🕵️‍♂️ У {target_name} пусто."
    msg = await message.answer(text)
    schedule_delete(message.chat.id, msg.message_id)

@dp.message(Command("balance", "баланс"))
async def cmd_balance(message: Message):
    data = await get_balance(message.from_user.id)
    if not data:
        msg = await message.answer("Тебя нет в базе.")
        schedule_delete(message.chat.id, msg.message_id)
        return
    msg = await message.answer(f"💳 Баланс: {data[0]} Ранкоинов")
    schedule_delete(message.chat.id, msg.message_id)

@dp.message(Command("top", "топ"))
async def cmd_top(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, username, first_name, rankoins FROM active_users ORDER BY rankoins DESC LIMIT 10") as cur:
            users = await cur.fetchall()
    if not users:
        msg = await message.answer("🏆 Топ пуст.")
        schedule_delete(message.chat.id, msg.message_id)
        return
    text = "🏆 <b>Топ:</b>\n" + "\n".join([f"{i}. {u[1] or u[2]} — {u[3]} 💰" for i, u in enumerate(users, 1)])
    msg = await message.answer(text, parse_mode="HTML")
    schedule_delete(message.chat.id, msg.message_id)

@dp.message(Command("gift", "подарок"))
async def cmd_gift(message: Message):
    uid = message.from_user.id
    until = time.time() + 259200
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("INSERT OR REPLACE INTO active_users (user_id, username, first_name, immune_until, is_premium) VALUES (?, ?, ?, ?, 1)",
                         (uid, message.from_user.username, message.from_user.first_name, until))
        await db.commit()
    try: await bot.send_message(uid, "🎁 Премиум на 3 дня! Пиши /notag для защиты.")
    except: pass
    msg = await message.answer("✅ Премиум получен!")
    schedule_delete(message.chat.id, msg.message_id)

@dp.message(Command("notag", "нетегать"))
async def cmd_no_tag(message: Message):
    uid = message.from_user.id
    if await get_user_premium_status(uid) != 1:
        msg = await message.answer("❌ Только для Премиум (/gift).")
        schedule_delete(message.chat.id, msg.message_id)
        return
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE active_users SET immune_until = ? WHERE user_id = ?", (time.time() + 259200, uid))
        await db.commit()
    msg = await message.answer("🛡️ Защита включена!")
    schedule_delete(message.chat.id, msg.message_id)

@dp.message(Command("award", "награда"))
async def cmd_award(message: Message):
    if not await is_chat_admin(message.chat.id, message.from_user.id): return
    if not message.reply_to_message: return
    amount = 10
    parts = message.text.split()
    if len(parts) >= 2:
        try: amount = int(parts[1])
        except: pass
    reply = message.reply_to_message
    text = reply.text or reply.caption or ""
    targets = set()
    if reply.entities:
        for e in reply.entities:
            if e.type == "mention": targets.add(text[e.offset:e.offset+e.length].lstrip("@").lower())
            elif e.type == "text_link" and e.url.startswith("tg://user?id="):
                try: targets.add(f"id_{int(e.url.split('=')[1])}")
                except: pass
    for m in re.finditer(r'@([a-zA-Z0-9_]{5,32})', text): targets.add(m.group(1).lower())
    user_map = await get_all_users_map()
    awarded = [user_map[t] for t in targets if t in user_map]
    if not awarded:
        msg = await message.answer("❌ Нет зарегистрированных юзеров.")
        schedule_delete(message.chat.id, msg.message_id)
        return
    for uid in awarded: await add_rankoins(uid, amount)
    msg = await message.answer(f"✅ Выдано {amount} монет {len(awarded)} игрокам!")
    schedule_delete(message.chat.id, msg.message_id)

@dp.message(Command("quest", "задача"))
async def cmd_quest(message: Message):
    if not await is_chat_admin(message.chat.id, message.from_user.id): return
    parts = message.text.split()
    if len(parts) < 3: return
    try: amount, text = int(parts[1]), " ".join(parts[2:])
    except: return
    task_id = str(int(time.time()))
    active_tasks[task_id] = amount
    kb = InlineKeyboardBuilder()
    kb.button(text=f"Забрать {amount} 💰", callback_data=f"task_{task_id}")
    msg = await message.answer(f"📢 <b>Задача:</b> {text}\nНаграда: {amount}", reply_markup=kb.as_markup(), parse_mode="HTML")
    schedule_delete(message.chat.id, msg.message_id)

@dp.callback_query(F.data.startswith("task_"))
async def process_task(cb: CallbackQuery):
    try:
        tid = cb.data.split("_")[1]
        amt = active_tasks.get(tid, 0)
        if amt > 0:
            await add_rankoins(cb.from_user.id, amt)
            del active_tasks[tid]
            await cb.answer(f"+{amt} 💰")
            await cb.message.edit_text(f"{cb.message.text}\n✅ Забрал: {cb.from_user.first_name}", reply_markup=None)
        else: await cb.answer("Уже выполнено", show_alert=True)
    except: await cb.answer("Ошибка", show_alert=True)

@dp.message(Command("call", "призыв"))
async def cmd_call(message: Message):
    if not await is_chat_admin(message.chat.id, message.from_user.id): return
    if call_lock.locked(): return
    users = await get_active_users()
    if not users: return
    text = message.text.split(maxsplit=1)[1] if len(message.text.split()) > 1 else "давай в игру"
    asyncio.create_task(run_rally(message.chat.id, text, users))
    msg = await message.answer(f"🔔 Призыв: {text}")
    schedule_delete(message.chat.id, msg.message_id)

async def run_rally(chat_id, text, users):
    async with call_lock:
        for ping in range(1, 4):
            for i in range(0, len(users), 8):
                chunk = users[i:i+8]
                mentions = [make_mention(u[0], u[1], u[2]) for u in chunk]
                sent = await bot.send_message(chat_id, f"📢 {ping}/3 | {text}\n\n" + "\n".join(mentions), parse_mode="HTML")
                schedule_delete(chat_id, sent.message_id)
                await asyncio.sleep(0.5)
            if ping < 3: await asyncio.sleep(20)
        final = await bot.send_message(chat_id, "✅ Готово!")
        schedule_delete(chat_id, final.message_id)

@dp.message()
async def auto_consent(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP): return
    if not message.from_user or message.from_user.is_bot: return
    uid = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT immune_until FROM active_users WHERE user_id=?", (uid,)) as cur:
            res = await cur.fetchone()
        if res and res[0] > time.time():
            await db.execute("UPDATE active_users SET immune_until=0 WHERE user_id=?", (uid,))
            await db.commit()
    await upsert_user(uid, message.from_user.username, message.from_user.first_name)

# ================= 🌐 WEB SERVER (АНТИ-СОН) =================
async def handle_health(request):
    return web.Response(text="OK", status=200)

async def run_web():
    app = web.Application()
    app.router.add_get('/', handle_health)
    app.router.add_get('/health', handle_health)
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    logging.info(f"🌐 Web server on port {port}")
    while True: await asyncio.sleep(3600)

# ================= MAIN =================
async def main():
    if os.path.exists(DB_PATH): os.remove(DB_PATH)
    await init_db()
    await register_commands(bot)
    logging.info("🚀 БОТ ЗАПУЩЕН")
    await asyncio.gather(dp.start_polling(bot), run_web())

if __name__ == "__main__":
    asyncio.run(main())
