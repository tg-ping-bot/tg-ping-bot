import asyncio
import random
import logging
import time
import os
import re
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.enums import ChatType
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

BOT_TOKEN = os.getenv("BOT_TOKEN", "8686262469:AAGsGv3WAcULdJSy2GH-whPAG2s0hKIGFM4")
ADMIN_ID = 429382259  # Замени на свой ID
IRIS_USERNAME = os.getenv("IRIS_USERNAME", "im_pinger_bot")  # Твой username бота (без @)
IRIS_BOT_ID = 93372553  # ID бота @iris

DB_PATH = "chat_users.db"
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

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
                pending_payment INTEGER DEFAULT 0
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

async def set_pending_payment(user_id: int, amount: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO active_users (user_id, pending_payment)
            VALUES (?, ?)
        """, (user_id, amount))
        await db.commit()

async def clear_pending_payment(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE active_users SET pending_payment = 0 WHERE user_id = ?", (user_id,))
        await db.commit()

async def grant_immunity(user_id: int, days: int = 30):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            UPDATE active_users 
            SET immune_until = ?, pending_payment = 0
            WHERE user_id = ?
        """, (time.time() + (days * 86400), user_id))
        await db.commit()

async def get_user_status(user_id: int):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT immune_until, pending_payment 
            FROM active_users 
            WHERE user_id = ?
        """, (user_id,)) as cur:
            return await cur.fetchone()

async def get_active_users_for_tag():
    now = time.time()
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("""
            SELECT user_id, username, first_name FROM active_users 
            WHERE immune_until < ? OR immune_until IS NULL
        """, (now,)) as cur:
            return await cur.fetchall()

# ================= FORMATTER =================
def make_mention(user_id: int, username: str, first_name: str) -> str:
    display_name = first_name or username or "Участник"
    return f'<a href="tg://user?id={user_id}">{display_name}</a>'

# ================= AUTO-DELETE =================
async def schedule_deletion(chat_id: int, message_id: int, delay: float = 10.0):
    try:
        await asyncio.sleep(delay)
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

# ================= HANDLERS =================

# Обработка платежей от Iris
@dp.message(F.from_user.id == IRIS_BOT_ID)
async def handle_iris_payment(message: Message):
    """Обрабатывает сообщения от Iris bot о переводах"""
    if not message.text:
        return
    
    text = message.text.lower()
    
    # Проверяем, есть ли в сообщении информация о переводе
    # Iris обычно шлёт: "Получен перевод от @username: 10 ирисок"
    # или "You received 10 🌸 from @username"
    
    iris_match = re.search(r'(\d+)\s*(ирисок|ириска|ириски|🌸|iris)', text)
    from_match = re.search(r'от\s+@?(\w+)|from\s+@?(\w+)', text)
    
    if iris_match and from_match:
        amount = int(iris_match.group(1))
        from_username = from_match.group(1) or from_match.group(2)
        
        # Ищем пользователя по username
        async with aiosqlite.connect(DB_PATH) as db:
            async with db.execute("""
                SELECT user_id, pending_payment 
                FROM active_users 
                WHERE username = ?
            """, (from_username,)) as cur:
                user = await cur.fetchone()
        
        if user and amount >= 10:
            user_id, pending = user
            # Проверяем, ожидался ли платёж
            if pending >= 10 or True:  # Принимаем любые платежи от 10 ирисок
                await grant_immunity(user_id, 30)
                try:
                    await bot.send_message(
                        user_id,
                        f"✅ Оплата получена! Защита активирована на 30 дней.\n"
                        f"Списано: 10 ирисок 🌸"
                    )
                except:
                    pass
                logging.info(f"💰 Пользователь {user_id} (@{from_username}) купил защиту за {amount} ирисок")
        
        await clear_pending_payment(user_id if user else 0)

# Команда покупки защиты
@dp.message(Command("neteagay"))
async def cmd_buy_immunity(message: Message):
    user_id = message.from_user.id
    
    # Проверяем текущий статус
    status = await get_user_status(user_id)
    if status:
        immune_until, pending = status
        if immune_until and immune_until > time.time():
            days_left = int((immune_until - time.time()) / 86400)
            return await message.answer(f"🛡️ У тебя уже есть защита! Осталось дней: {days_left}")
    
    # Показываем инструкцию по оплате
    await set_pending_payment(user_id, 10)
    
    instruction = (
        f"🌸 <b>Защита от тегов — 10 ирисок (30 дней)</b>\n\n"
        f"📝 <b>Инструкция по оплате:</b>\n"
        f"1. Перейди в @iris\n"
        f"2. Отправь команду:\n"
        f"<code>/pay @{IRIS_USERNAME} 10</code>\n\n"
        f"Или:\n"
        f"<code>10 ирисок @{IRIS_USERNAME}</code>\n\n"
        f"⏳ После оплаты защита активируется автоматически!"
    )
    
    await message.answer(instruction, parse_mode="HTML")

# Проверка статуса
@dp.message(Command("balance"))
async def cmd_balance(message: Message):
    status = await get_user_status(message.from_user.id)
    if not status:
        return await message.answer("Сначала напиши что-нибудь в чат.")
    
    immune_until, pending = status
    if immune_until and immune_until > time.time():
        days_left = int((immune_until - time.time()) / 86400)
        await message.answer(f"🛡️ <b>Защита активна!</b>\nОсталось дней: {days_left}")
    else:
        await message.answer("⚔️ <b>Защита не активна</b>\nИспользуй /neteagay для покупки.")

# Снять защиту
@dp.message(Command("tag_on"))
async def cmd_cancel_immunity(message: Message):
    await clear_pending_payment(message.from_user.id)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE active_users SET immune_until = 0 WHERE user_id = ?", (message.from_user.id,))
        await db.commit()
    await message.answer("⚔️ Защита снята. Теперь тебя могут тегать.")

# Игровые команды
@dp.message(F.text.lower().in_(["игрок", "/игрок", "/randomcall"]))
async def cmd_randomcall(message: Message):
    users = await get_active_users_for_tag()
    if not users:
        return await message.answer("⏳ Нет доступных игроков (все купили защиту).")

    chosen = random.choice(users)
    mention = make_mention(chosen[0], chosen[1], chosen[2])
    sent = await message.answer(f"{mention}, Зайди в игру! 🎮", parse_mode="HTML")
    asyncio.create_task(schedule_deletion(message.chat.id, sent.message_id))

@dp.message(F.text.lower().in_(["все тут", "/все тут", "/tagall"]))
async def cmd_tagall(message: Message):
    users = await get_active_users_for_tag()
    if not users:
        return await message.answer("⏳ Нет доступных игроков.")

    chunk_size = 8
    for i in range(0, len(users), chunk_size):
        chunk = users[i:i + chunk_size]
        mentions = [make_mention(u[0], u[1], u[2]) for u in chunk]
        msg = "Срочный сбор! 📢\n\n" + "\n".join(mentions)
        sent = await message.answer(msg, parse_mode="HTML")
        asyncio.create_task(schedule_deletion(message.chat.id, sent.message_id))
        await asyncio.sleep(1.5)

# Авто-согласие
@dp.message()
async def auto_consent(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP): return
    if message.from_user is None or message.from_user.is_bot: return
    await upsert_user(message.from_user.id, message.from_user.username, message.from_user.first_name)

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
    logging.info("🚀 БОТ С IRIS ОПЛАТОЙ ЗАПУЩЕН")
    await asyncio.gather(run_bot(), run_web())

if __name__ == "__main__":
    asyncio.run(main())