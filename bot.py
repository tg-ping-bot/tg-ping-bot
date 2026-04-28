import asyncio
import logging
import time
import os
import aiosqlite
from aiogram import Bot, Dispatcher, F
from aiogram.filters import Command
from aiogram.types import Message
from aiogram.enums import ChatType, ChatMemberStatus
from aiogram.exceptions import TelegramForbiddenError, TelegramBadRequest
from aiohttp import web

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")

# 🔑 Настройки
BOT_TOKEN = os.getenv("BOT_TOKEN", "ТВОЙ_ТОКЕН_ЗДЕСЬ")
GIFT_DAYS = 3  # Сколько дней действует подарок
DB_PATH = "chat_users.db"

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
call_active = False
call_lock = asyncio.Lock()

# ================= DATABASE =================
async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS active_users (
                user_id INTEGER PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                last_seen REAL,
                immune_until REAL DEFAULT 0
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

async def get_all_user_ids():
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id FROM active_users") as cur:
            return await cur.fetchall()

# ================= HELPERS =================
async def is_chat_admin(chat_id: int, user_id: int) -> bool:
    """Проверяет, является ли пользователь админом или создателем чата"""
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)
    except Exception:
        return False

def make_mention(user_id: int, username: str, first_name: str) -> str:
    display_name = first_name or username or "Участник"
    return f'<a href="tg://user?id={user_id}">{display_name}</a>'

async def send_and_delete(chat_id: int, text: str, parse_mode="HTML", delay=10.0):
    """Отправляет сообщение и планирует его удаление"""
    try:
        msg = await bot.send_message(chat_id, text, parse_mode=parse_mode)
        asyncio.create_task(asyncio.sleep(delay))
        asyncio.create_task(bot.delete_message(chat_id, msg.message_id))
        return msg
    except Exception as e:
        logging.error(f"Ошибка отправки/удаления: {e}")

# ================= 🎁 /подарок =================
@dp.message(Command("подарок"))
async def cmd_gift(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return await message.answer("Команда работает только в группах.")
    
    uid = message.from_user.id
    until = time.time() + (GIFT_DAYS * 86400)
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO active_users (user_id, username, first_name, immune_until)
            VALUES (?, ?, ?, ?)
        """, (uid, message.from_user.username, message.from_user.first_name, until))
        await db.commit()

    # Отправляем в ЛС, чтобы открыть диалог и дать разрешение на рассылку
    try:
        await bot.send_message(uid, 
            f"🎁 <b>Подарок активирован!</b>\n"
            f"У тебя {GIFT_DAYS} дня защиты от тегов.\n"
            f"⚠️ Защита автоматически спадёт, как только ты напишешь любое сообщение в чат.",
            parse_mode="HTML")
    except TelegramForbiddenError:
        pass  # Пользователь заблокировал бота или закрыл ЛС
        
    await message.answer("✅ Подарок получен! Проверь личные сообщения от бота.")
    asyncio.create_task(asyncio.sleep(10))
    asyncio.create_task(bot.delete_message(message.chat.id, message.message_id + 1)) # Удаляем ответ бота

# ================= 🛡️ /нетегать =================
@dp.message(Command("нетегать"))
async def cmd_no_tag(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE active_users SET immune_until = 0 WHERE user_id = ?", (message.from_user.id,))
        await db.commit()
    await message.answer("🛡️ Защита снята. Теперь тебя могут тегать.")
    asyncio.create_task(asyncio.sleep(10))
    asyncio.create_task(bot.delete_message(message.chat.id, message.message_id + 1))

# ================= 📢 /призыв =================
@dp.message(Command("призыв"))
async def cmd_call(message: Message):
    if not await is_chat_admin(message.chat.id, message.from_user.id):
        return await message.answer("🚫 Команда только для администраторов чата.")

    async with call_lock:
        if call_active:
            return await message.answer("⏳ Общий сбор уже запущен! Подожди окончания.")
        call_active = True

    args = message.text.split(maxsplit=1)
    text = args[1] if len(args) > 1 else "давай в игру"
    
    users = await get_active_users()
    if not users:
        call_active = False
        return await message.answer("Нет активных игроков (все в защите или молчат).")

    asyncio.create_task(run_60s_rally(message.chat.id, text, users))
    await message.answer(f"🔔 Запускаю общий сбор на 60 сек!\nТекст: «{text}»")
    asyncio.create_task(asyncio.sleep(10))
    asyncio.create_task(bot.delete_message(message.chat.id, message.message_id + 1))

async def run_60s_rally(chat_id: int, text: str, users: list):
    global call_active
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
            
            if ping < 3:
                await asyncio.sleep(20)
                
        await send_and_delete(chat_id, "✅ Сбор завершён!")
    except Exception as e:
        logging.error(f"Ошибка в /призыв: {e}")
        await send_and_delete(chat_id, f"⚠️ Сбор прерван: {e}")
    finally:
        call_active = False

# ================= 📩 /призывлс =================
@dp.message(Command("призывлс"))
async def cmd_call_dm(message: Message):
    if not await is_chat_admin(message.chat.id, message.from_user.id):
        return await message.answer("🚫 Команда только для администраторов чата.")

    args = message.text.split(maxsplit=1)
    text = args[1] if len(args) > 1 else "Давай в игру!"
    
    raw_users = await get_all_user_ids()
    if not raw_users:
        return await message.answer("Нет пользователей в базе.")
    
    user_ids = [u[0] for u in raw_users]
    await message.answer(f"📤 Начинаю рассылку в ЛС {len(user_ids)} участникам...")
    asyncio.create_task(asyncio.sleep(10))
    asyncio.create_task(bot.delete_message(message.chat.id, message.message_id + 1))
    
    success = fail = 0
    for uid in user_ids:
        try:
            await bot.send_message(uid, text)
            success += 1
        except (TelegramForbiddenError, TelegramBadRequest):
            fail += 1
        except Exception:
            fail += 1
        await asyncio.sleep(0.4)  # Защита от FloodWait
        
    await send_and_delete(message.chat.id, f"✅ Рассылка завершена!\n🟢 Доставлено: {success}\n🔴 Пропущено: {fail}")

# ================= 👁️ Авто-согласие + Сброс премиума =================
@dp.message()
async def auto_consent(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP): return
    if message.from_user is None or message.from_user.is_bot: return
    
    uid = message.from_user.id
    # Если у пользователя активна защита от подарка, сбрасываем её при первом сообщении
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT immune_until FROM active_users WHERE user_id = ?", (uid,)) as cur:
            res = await cur.fetchone()
        if res and res[0] > time.time():
            await db.execute("UPDATE active_users SET immune_until = 0 WHERE user_id = ?", (uid,))
            await db.commit()
            logging.info(f"🎁 Премиум у {uid} сброшен из-за сообщения в чате.")

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
    logging.info("🚀 БОТ ОБНОВЛЁН. Админы: /призыв, /призывлс | Игроки: /подарок, /нетегать")
    await asyncio.gather(run_bot(), run_web())

if __name__ == "__main__":
    asyncio.run(main())
