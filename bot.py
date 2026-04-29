import asyncio
import logging
import time
import os
import random
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
GIFT_DAYS = 3

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
call_lock = asyncio.Lock()
active_tasks = {}

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

async def send_and_delete(chat_id: int, text: str, parse_mode="HTML", delay=10.0):
    try:
        msg = await bot.send_message(chat_id, text, parse_mode=parse_mode)
        asyncio.create_task(asyncio.sleep(delay))
        asyncio.create_task(bot.delete_message(chat_id, msg.message_id))
        return msg
    except Exception:
        pass

# ================= 💰 ЭКОНОМИКА: КОМАНДЫ =================

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
        hours = wait_time // 3600
        mins = (wait_time % 3600) // 60
        return await message.answer(f" Бонус недоступен. Жди: {hours}ч {mins}мин.")
    
    await add_rankoins(uid, 100)
    await db_update_last_bonus(uid)
    await message.answer("💰 +100 Ранкоинов! Баланс обновлен.")
    asyncio.create_task(asyncio.sleep(10))
    asyncio.create_task(bot.delete_message(message.chat.id, message.message_id + 1))

@dp.message(Command("long"))
async def cmd_long(message: Message):
    uid = message.from_user.id
    data = await get_balance(uid)
    if not data:
        return await message.answer("Сначала напиши что-нибудь в чат для регистрации.")
    
    _, _, last_rob = data
    now = time.time()
    cooldown = 2 * 60 * 60  # 2 часа
    
    if now - last_rob < cooldown:
        wait_time = int((last_rob + cooldown) - now)
        mins = wait_time // 60
        return await message.answer(f"🕵️‍♂️ Отдыхай. Следующая попытка через {mins} мин.")
    
    # Кулдаун начинается сразу при попытке
    await db_update_last_rob(uid)
    
    # 🔪 ШАНС 10% НА УСПЕХ
    if random.random() > 0.10:
        return await message.answer("🕵️‍♂️ Попытка провалилась! Жертва заметила тебя. (Шанс успеха: 10%)")
    
    victim_id = await get_random_victim(uid)
    if not victim_id:
        return await message.answer("🎭 Некого ограбить (у всех 0 монет).")
    
    # 💸 СУММА КРАЖИ: от 1 до 100
    desired_amount = random.randint(1, 100)
    
    # Проверяем реальный баланс жертвы
    victim_data = await get_balance(victim_id)
    victim_balance = victim_data[0] if victim_data else 0
    actual_stolen = min(desired_amount, victim_balance)
    
    if actual_stolen > 0:
        await remove_rankoins(victim_id, actual_stolen)
        await add_rankoins(uid, actual_stolen)
        await message.answer(f"💸 Успех! Ты украл {actual_stolen} Ранкоинов! (Хотел: {desired_amount})")
    else:
        await message.answer("🕵️‍♂️ Удача на твоей стороне, но у жертвы оказались пустые карманы (0 монет).")
        
    asyncio.create_task(asyncio.sleep(10))
    asyncio.create_task(bot.delete_message(message.chat.id, message.message_id + 1))

@dp.message(Command("баланс"))
async def cmd_balance(message: Message):
    data = await get_balance(message.from_user.id)
    if not data:
        return await message.answer("Тебя нет в базе.")
    rankoins, _, _ = data
    await message.answer(f"💳 Твой баланс: {rankoins} Ранкоинов.")
    asyncio.create_task(asyncio.sleep(10))
    asyncio.create_task(bot.delete_message(message.chat.id, message.message_id + 1))

# ================= 📢 ЗАДАЧИ (АДМИН) =================
@dp.message(Command("задача"))
async def cmd_task(message: Message):
    if not await is_chat_admin(message.chat.id, message.from_user.id):
        return await message.answer("🚫 Только для админов.")
    
    parts = message.text.split()
    if len(parts) < 3:
        return await message.answer("❌ Формат: /задача [сумма] [текст задания]")
    
    try:
        amount = int(parts[1])
        text = " ".join(parts[2:])
    except ValueError:
        return await message.answer("❌ Сумма должна быть числом.")
    
    task_id = str(int(time.time()))
    active_tasks[task_id] = amount
    
    builder = InlineKeyboardBuilder()
    builder.button(text=f"Забрать {amount} 💰", callback_data=f"task_{task_id}")
    
    await message.answer(
        f"📢 <b>Групповая задача!</b>\n\n{text}\n\nНаграда: {amount} Ранкоинов",
        reply_markup=builder.as_markup(),
        parse_mode="HTML"
    )
    asyncio.create_task(asyncio.sleep(10))
    asyncio.create_task(bot.delete_message(message.chat.id, message.message_id))

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
                f"{callback.message.text}\n\n✅ Награду забрал: {callback.from_user.first_name}",
                reply_markup=None
            )
        else:
            await callback.answer("Задача уже выполнена.", show_alert=True)
    except Exception:
        await callback.answer("Ошибка при получении награды.", show_alert=True)

# ================= 📢 /призыв =================
@dp.message(Command("призыв"))
async def cmd_call(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP): return
    if not await is_chat_admin(message.chat.id, message.from_user.id): return

    if call_lock.locked():
        return await message.answer("⏳ Сбор уже запущен!")

    args = message.text.split(maxsplit=1)
    text = args[1] if len(args) > 1 else "давай в игру"
    users = await get_active_users()
    if not users: return await message.answer("Нет игроков для призыва.")

    asyncio.create_task(run_60s_rally(message.chat.id, text, users))
    await message.answer(f"🔔 Призыв запущен!\nТекст: «{text}»")
    asyncio.create_task(asyncio.sleep(10))
    asyncio.create_task(bot.delete_message(message.chat.id, message.message_id + 1))

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
                    asyncio.create_task(asyncio.sleep(10))
                    asyncio.create_task(bot.delete_message(chat_id, sent.message_id))
                    await asyncio.sleep(0.5)
                if ping < 3: await asyncio.sleep(20)
            await send_and_delete(chat_id, "✅ Сбор завершён!")
        except Exception as e:
            logging.error(f"Ошибка в /призыв: {e}")
            await send_and_delete(chat_id, f"⚠️ Сбор прерван: {e}")

# ================= 👁️ Авто-согласие + Сброс премиума =================
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
            logging.info(f"🎁 Премиум у {uid} сброшен.")

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
    logging.info("🚀 БОТ ОБНОВЛЁН: Кража 10% шанс, сумма 1-100 Ранкоинов")
    await asyncio.gather(run_bot(), run_web())

if __name__ == "__main__":
    asyncio.run(main())
