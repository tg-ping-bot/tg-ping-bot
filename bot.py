import asyncio
import logging
import time
import os
import random
import re
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message, CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from aiogram.enums import ChatType, ChatMemberStatus
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

BOT_TOKEN = os.getenv("BOT_TOKEN", "ТВОЙ_ТОКЕН_ЗДЕСЬ")
DB_PATH = "chat_users.db"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
call_lock = asyncio.Lock()
active_tasks = {}

# 🤡 Смешные фразы при провале кражи
FAIL_MESSAGES = [
    "Жертва оказалась ниндзя и растворилась в тумане 🌫️",
    "Ты споткнулся о собственный шнурок и уронил кошелёк 👟💸",
    "Страж чата посмотрел на тебя так, что ты забыл, зачем пришёл 👮‍♂️",
    "Попытка засчитана, но добыча ушла через чёрный ход 🚪💨",
    "Ты крался так тихо, что даже сам себя не услышал 🤫",
    "Ворона отвлекла тебя блестящей монеткой 🐦‍⬛✨",
    "Жертва включила режим 'Невидимка' на 2 часа ",
    "Ты попытался украсть воздух, но инфляция съела и его 📉"
]

# ================= DATABASE =================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS active_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_seen REAL,
                immune_until REAL DEFAULT 0,
                rankoins INTEGER DEFAULT 0,
                last_bonus REAL DEFAULT 0,
                last_rob REAL DEFAULT 0
            )
        """)
        await db.commit()

async def upsert_user(user_id: int, username: str, first_name: str):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR IGNORE INTO active_users (user_id, username, first_name, last_seen)
            VALUES (?, ?, ?, ?)
        """, (user_id, username, first_name, time.time()))
        await db.commit()

async def get_active_users():
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT user_id, username, first_name FROM active_users 
            WHERE immune_until < ?
        """, (now,)) as cur:
            return await cur.fetchall()

# ================= ЭКОНОМИКА =================
async def get_balance(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT rankoins, last_bonus, last_rob FROM active_users WHERE user_id = ?", (user_id,)) as cur:
            return await cur.fetchone()

async def add_rankoins(user_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE active_users SET rankoins = rankoins + ? WHERE user_id = ?", (amount, user_id))
        await db.commit()

async def remove_rankoins(user_id: int, amount: int) -> bool:
    async with aiosqlite.connect(DB_PATH) as db:
        res = await db.execute("UPDATE active_users SET rankoins = rankoins - ? WHERE user_id = ? AND rankoins >= ?", 
                               (amount, user_id, amount))
        await db.commit()
        return res.rowcount > 0

async def get_random_victim(thief_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM active_users WHERE rankoins > 0 AND user_id != ? ORDER BY RANDOM() LIMIT 1", (thief_id,)) as cur:
            res = await cur.fetchone()
            return res[0] if res else None

async def db_update_last_bonus(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE active_users SET last_bonus = ? WHERE user_id = ?", (time.time(), uid))
        await db.commit()

async def db_update_last_rob(uid):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE active_users SET last_rob = ? WHERE user_id = ?", (time.time(), uid))
        await db.commit()

# ================= HELPERS =================
async def is_chat_admin(chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)
    except Exception:
        return False

def make_mention(user_id: int, username: str, first_name: str) -> str:
    display_name = first_name or username or "Участник"
    return f'<a href="tg://user?id={user_id}">{display_name}</a>'

async def schedule_delete(chat_id: int, message_id: int, delay: float = 10.0):
    """Удаляет сообщение через указанное время"""
    async def _delete():
        try:
            await asyncio.sleep(delay)
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
    asyncio.create_task(_delete())

# ================= 💰 ЭКОНОМИКА =================
@dp.message(Command("бонус"))
async def cmd_bonus(message: Message):
    uid = message.from_user.id
    data = await get_balance(uid)
    if not data:
        return await message.answer("Сначала напиши что-нибудь в чат для регистрации.")
    
    rankoins, last_bonus, _ = data
    now = time.time()
    cooldown = 24 * 60 * 60
    
    if now - last_bonus < cooldown:
        wait_time = int((last_bonus + cooldown) - now)
        h, m = divmod(wait_time, 3600)
        m //= 60
        msg = await message.answer(f" Бонус недоступен. Жди: {h}ч {m}мин.")
        return schedule_delete(message.chat.id, msg.message_id)
    
    await add_rankoins(uid, 100)
    await db_update_last_bonus(uid)
    msg = await message.answer("💰 +100 Ранкоинов! Баланс обновлен.")
    schedule_delete(message.chat.id, msg.message_id)

@dp.message(Command("long"))
async def cmd_long(message: Message):
    uid = message.from_user.id
    data = await get_balance(uid)
    if not data:
        return await message.answer("Сначала напиши что-нибудь в чат для регистрации.")
    
    _, _, last_rob = data
    now = time.time()
    cooldown = 2 * 60 * 60
    
    if now - last_rob < cooldown:
        wait_time = int((last_rob + cooldown) - now)
        m = wait_time // 60
        msg = await message.answer(f"🕵️‍♂️ Кулдаун. Следующая попытка через {m} мин.")
        return schedule_delete(message.chat.id, msg.message_id)
    
    await db_update_last_rob(uid)
    
    # 🎲 Шанс успеха 10%
    if random.random() > 0.10:
        msg = await message.answer(f"🕵️‍♂️ {random.choice(FAIL_MESSAGES)}")
        return schedule_delete(message.chat.id, msg.message_id)
    
    victim_id = await get_random_victim(uid)
    if not victim_id:
        msg = await message.answer("🎭 Некого ограбить (у всех 0 монет).")
        return schedule_delete(message.chat.id, msg.message_id)
    
    desired = random.randint(1, 100)
    victim_data = await get_balance(victim_id)
    victim_balance = victim_data[0] if victim_data else 0
    actual = min(desired, victim_balance)
    
    if actual > 0:
        await remove_rankoins(victim_id, actual)
        await add_rankoins(uid, actual)
        msg = await message.answer(f"💸 Успех! Украдено {actual} Ранкоинов. (Планировал: {desired})")
    else:
        msg = await message.answer("🕵️‍♂️ Удача есть, но карманы жертвы пусты.")
        
    schedule_delete(message.chat.id, msg.message_id)

@dp.message(Command("баланс"))
async def cmd_balance(message: Message):
    data = await get_balance(message.from_user.id)
    if not data:
        msg = await message.answer("Тебя нет в базе. Напиши что-нибудь в чат.")
        return schedule_delete(message.chat.id, msg.message_id)
    rankoins, _, _ = data
    msg = await message.answer(f"💳 Твой баланс: {rankoins} Ранкоинов.")
    schedule_delete(message.chat.id, msg.message_id)

@dp.message(Command("топ"))
async def cmd_top(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, username, first_name, rankoins FROM active_users ORDER BY rankoins DESC LIMIT 10") as cur:
            users = await cur.fetchall()
    
    if not users:
        msg = await message.answer("🏆 Топ пуст. Играйте активнее!")
        return schedule_delete(message.chat.id, msg.message_id)
        
    text = "🏆 <b>Топ богачей чата:</b>\n\n"
    for i, (uid, uname, fname, coins) in enumerate(users, 1):
        display = uname or fname or f"User{uid}"
        text += f"{i}. {display} — {coins} \n"
        
    msg = await message.answer(text, parse_mode="HTML")
    schedule_delete(message.chat.id, msg.message_id)

# ================= 📢 АДМИН: /награда (ответом на сообщение) =================
@dp.message(Command("награда"))
async def cmd_award_reply(message: Message):
    if not await is_chat_admin(message.chat.id, message.from_user.id):
        msg = await message.answer(" Только для администраторов.")
        return schedule_delete(message.chat.id, msg.message_id)
        
    if not message.reply_to_message:
        msg = await message.answer("❌ Ответьте на сообщение с упоминаниями.")
        return schedule_delete(message.chat.id, msg.message_id)
        
    parts = message.text.split()
    amount = 10
    if len(parts) >= 2:
        try: amount = int(parts[1])
        except: pass
        
    targets = set()
    reply_text = message.reply_to_message.text or ""
    
    # 1. Ищем ссылки tg://user?id=
    links = re.findall(r'tg://user\?id=(\d+)', reply_text)
    targets.update(int(x) for x in links)
    
    # 2. Ищем @username через сущности
    if message.reply_to_message.entities:
        for ent in message.reply_to_message.entities:
            if ent.type == "mention":
                username = reply_text[ent.offset:ent.offset+ent.length].replace("@", "")
                async with aiosqlite.connect(DB_PATH) as db:
                    async with db.execute("SELECT user_id FROM active_users WHERE username = ?", (username,)) as cur:
                        res = await cur.fetchone()
                        if res: targets.add(res[0])
            elif ent.type == "text_link" and ent.url.startswith("tg://user?id="):
                targets.add(int(ent.url.split("=")[1]))
                
    if not targets:
        msg = await message.answer("❌ Не найдено зарегистрированных пользователей в сообщении.")
        return schedule_delete(message.chat.id, msg.message_id)
        
    awarded_count = 0
    for uid in targets:
        await add_rankoins(uid, amount)
        awarded_count += 1
        
    msg = await message.answer(f"✅ Выдано {amount} Ранкоинов {awarded_count} участникам!")
    schedule_delete(message.chat.id, msg.message_id)

# ================= 📢 ЗАДАЧИ (АДМИН) =================
@dp.message(Command("задача"))
async def cmd_task(message: Message):
    if not await is_chat_admin(message.chat.id, message.from_user.id):
        msg = await message.answer("🚫 Только для админов.")
        return schedule_delete(message.chat.id, msg.message_id)
    
    parts = message.text.split()
    if len(parts) < 3:
        msg = await message.answer("❌ Формат: /задача [сумма] [текст задания]")
        return schedule_delete(message.chat.id, msg.message_id)
    
    try:
        amount = int(parts[1])
        text = " ".join(parts[2:])
    except ValueError:
        msg = await message.answer("❌ Сумма должна быть числом.")
        return schedule_delete(message.chat.id, msg.message_id)
    
    task_id = str(int(time.time()))
    active_tasks[task_id] = amount
    
    builder = InlineKeyboardBuilder()
    builder.button(text=f"Забрать {amount} 💰", callback_data=f"task_{task_id}")
    
    msg = await message.answer(
        f" <b>Групповая задача!</b>\n\n{text}\n\nНаграда: {amount} Ранкоинов",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    schedule_delete(message.chat.id, msg.message_id)

@dp.callback_query(F.data.startswith("task_"))
async def process_task(callback: CallbackQuery):
    try:
        _, task_id = callback.data.split("_")
        amount = active_tasks.get(task_id, 0)
        
        if amount > 0:
            await add_rankoins(callback.from_user.id, amount)
            del active_tasks[task_id]
            await callback.answer(f"Получено {amount} Ранкоинов! 💰")
            await callback.message.edit_text(
                f"{callback.message.text}\n\n✅ Забрал: {callback.from_user.first_name}",
                reply_markup=None
            )
        else:
            await callback.answer("Задача уже выполнена.", show_alert=True)
    except Exception:
        await callback.answer("Ошибка.", show_alert=True)

# ================= 📢 /призыв (10 сек удаление) =================
@dp.message(Command("призыв"))
async def cmd_call(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP): return
    if not await is_chat_admin(message.chat.id, message.from_user.id): return

    if call_lock.locked():
        msg = await message.answer(" Сбор уже запущен!")
        return schedule_delete(message.chat.id, msg.message_id)

    args = message.text.split(maxsplit=1)
    text = args[1] if len(args) > 1 else "давай в игру"
    users = await get_active_users()
    if not users: 
        msg = await message.answer("Нет игроков для призыва.")
        return schedule_delete(message.chat.id, msg.message_id)

    asyncio.create_task(run_60s_rally(message.chat.id, text, users))
    msg = await message.answer(f"🔔 Призыв запущен!\nТекст: «{text}»")
    schedule_delete(message.chat.id, msg.message_id)

async def run_60s_rally(chat_id: int, text: str, users: list):
    async with call_lock:
        chunk_size = 8
        try:
            for ping in range(1, 4):
                for i in range(0, len(users), chunk_size):
                    chunk = users[i:i + chunk_size]
                    mentions = [make_mention(u[0], u[1], u[2]) for u in chunk]
                    msg = f"📢 {ping}/3 | {text}\n\n" + "\n".join(mentions)
                    sent = await bot.send_message(chat_id, msg, parse_mode="HTML")
                    schedule_delete(chat_id, sent.message_id, delay=10.0) # РОВНО 10 СЕКУНД
                    await asyncio.sleep(0.5)
                if ping < 3: await asyncio.sleep(20)
                
            final = await bot.send_message(chat_id, "✅ Сбор завершён!")
            schedule_delete(chat_id, final.message_id, delay=10.0)
        except Exception as e:
            logging.error(f"Ошибка в /призыв: {e}")
            err = await bot.send_message(chat_id, f"⚠️ Сбор прерван: {e}")
            schedule_delete(chat_id, err.message_id, delay=10.0)

# ================= ️ Авто-согласие =================
@dp.message()
async def auto_consent(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP): return
    if message.from_user is None or message.from_user.is_bot: return
    
    uid = message.from_user.id
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT immune_until FROM active_users WHERE user_id = ?", (uid,)) as cur:
            res = await cur.fetchone()
        if res and res[0] > time.time():
            await db.execute("UPDATE active_users SET immune_until = 0 WHERE user_id = ?", (uid,))
            await db.commit()

    await upsert_user(uid, message.from_user.username, message.from_user.first_name)

# ================= MAIN =================
async def run_bot(): await dp.start_polling(bot)
async def run_web():
    app = web.Application()
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    await web.TCPSite(runner, "0.0.0.0", port).start()
    while True: await asyncio.sleep(3600)

async def main():
    await init_db()
    logging.info("🚀 БОТ ГОТОВ: /топ, /награда, смешные провалы, удаление 10с")
    await asyncio.gather(run_bot(), run_web())

if __name__ == "__main__":
    asyncio.run(main())
