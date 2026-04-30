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
    logging.error("❌ ОШИБКА: Переменная BOT_TOKEN не найдена в настройках Render!")
    exit(1)

DB_PATH = "chat_users.db"
bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()
call_lock = asyncio.Lock()
active_tasks = {}

# 🎬 GIF для анимаций
SUCCESS_GIFS = [
    "https://media.giphy.com/media/l0HlHFRbmaX96hP6M/giphy.mp4",
    "https://media.giphy.com/media/3o7TKSjRrfIPjeiVyM/giphy.mp4"
]
FAIL_GIFS = [
    "https://media.giphy.com/media/3oEjI6SIIHBdRxXI40/giphy.mp4",
    "https://media.giphy.com/media/l2JhORT5IFnj6ioko/giphy.mp4"
]

FAIL_MESSAGES = [
    "Жертва оказалась ниндзя 🌫️",
    "Споткнулся о шнурок 👟",
    "Страж чата спугнул тебя 👮‍♂️",
    "Добыча ушла через чёрный ход 🚪",
    "Крался тише мыши, но спалил себя 🐭",
    "Ворона отобрала монету 🐦‍",
    "Жертва включила невидимость 👻",
    "Инфляция съела награду 📉"
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
                is_premium INTEGER DEFAULT 0,
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
async def is_chat_admin(chat_id: int, user_id: int) -> bool:
    try:
        member = await bot.get_chat_member(chat_id, user_id)
        return member.status in (ChatMemberStatus.CREATOR, ChatMemberStatus.ADMINISTRATOR)
    except Exception:
        return False

def make_mention(user_id: int, username: str, first_name: str) -> str:
    display_name = first_name or username or "Участник"
    return f'<a href="tg://user?id={user_id}">{display_name}</a>'

def schedule_delete(chat_id: int, message_id: int, delay: float = 10.0):
    async def _delete():
        try:
            await asyncio.sleep(delay)
            await bot.delete_message(chat_id=chat_id, message_id=message_id)
        except Exception:
            pass
    asyncio.create_task(_delete())

async def send_long_animation(chat_id: int, success: bool, caption: str):
    gif_url = random.choice(SUCCESS_GIFS if success else FAIL_GIFS)
    try:
        await bot.send_animation(chat_id, gif_url, caption=caption)
    except Exception:
        await bot.send_message(chat_id, caption)

# ================= 📜 РЕГИСТРАЦИЯ КОМАНД =================
async def register_commands(bot: Bot):
    commands = [
        BotCommand(command="bonus", description="Получить 100 Ранкоинов (раз в 24ч)"),
        BotCommand(command="long", description="Ограбить участника (ответом, 10% шанс)"),
        BotCommand(command="balance", description="Проверить свой баланс"),
        BotCommand(command="top", description="Топ-10 богачей чата"),
        BotCommand(command="gift", description="Получить Премиум на 3 дня"),
        BotCommand(command="notag", description="Включить защиту (только Премиум)"),
        BotCommand(command="call", description="Запустить общий сбор (только админ)"),
        BotCommand(command="quest", description="Создать задание с наградой (админ)"),
        BotCommand(command="award", description="Выдать монеты за ответ (админ)")
    ]
    await bot.set_my_commands(commands)
    logging.info("✅ Команды зарегистрированы в Telegram (Latin)")

# ================= 💰 ЭКОНОМИКА =================
@dp.message(Command("bonus", "бонус"))
async def cmd_bonus(message: Message):
    uid = message.from_user.id
    data = await get_balance(uid)
    if not 
        msg = await message.answer("Сначала напиши что-нибудь в чат для регистрации.")
        schedule_delete(message.chat.id, msg.message_id)
        return
    
    rankoins, last_bonus, _ = data
    now = time.time()
    cooldown = 24 * 60 * 60
    
    if now - last_bonus < cooldown:
        wait_time = int((last_bonus + cooldown) - now)
        h, m = divmod(wait_time, 3600)
        m //= 60
        msg = await message.answer(f"⏳ Бонус недоступен. Жди: {h}ч {m}мин.")
        schedule_delete(message.chat.id, msg.message_id)
        return
    
    await add_rankoins(uid, 100)
    await db_update_last_bonus(uid)
    msg = await message.answer("💰 +100 Ранкоинов! Баланс обновлен.")
    schedule_delete(message.chat.id, msg.message_id)

@dp.message(Command("long"))
async def cmd_long(message: Message):
    uid = message.from_user.id
    data = await get_balance(uid)
    if not 
        msg = await message.answer("Сначала напиши что-нибудь в чат для регистрации.")
        schedule_delete(message.chat.id, msg.message_id)
        return
    
    if not message.reply_to_message:
        msg = await message.answer("❌ Ответьте на сообщение того, кого хотите ограбить!")
        schedule_delete(message.chat.id, msg.message_id)
        return
    
    target_id = message.reply_to_message.from_user.id
    if target_id == uid:
        msg = await message.answer("🤡 Нельзя ограбить самого себя!")
        schedule_delete(message.chat.id, msg.message_id)
        return
    
    target_data = await get_balance(target_id)
    if not target_
        msg = await message.answer("❌ Этот участник не зарегистрирован в боте.")
        schedule_delete(message.chat.id, msg.message_id)
        return
    
    _, _, last_rob = data
    now = time.time()
    cooldown = 2 * 60 * 60
    
    if now - last_rob < cooldown:
        wait_time = int((last_rob + cooldown) - now)
        m = wait_time // 60
        msg = await message.answer(f"🕵️‍♂️ Кулдаун. Следующая попытка через {m} мин.")
        schedule_delete(message.chat.id, msg.message_id)
        return
    
    await db_update_last_rob(uid)
    target_name = message.reply_to_message.from_user.first_name
    
    if random.random() > 0.10:
        text = f"🕵️‍♂️ Провал! {random.choice(FAIL_MESSAGES)}"
        await send_long_animation(message.chat.id, success=False, caption=text)
        msg = await message.answer(text) 
        schedule_delete(message.chat.id, msg.message_id)
        return
    
    desired = random.randint(1, 100)
    victim_balance = target_data[0]
    actual = min(desired, victim_balance)
    
    if actual > 0:
        await remove_rankoins(target_id, actual)
        await add_rankoins(uid, actual)
        text = f"💸 Успех! Украл {actual} Ранкоинов у {target_name}. (Планировал: {desired})"
    else:
        text = f"🕵️‍♂️ Удача есть, но у {target_name} пустые карманы."
        
    await send_long_animation(message.chat.id, success=True, caption=text)
    msg = await message.answer(text)
    schedule_delete(message.chat.id, msg.message_id)

@dp.message(Command("balance", "баланс"))
async def cmd_balance(message: Message):
    data = await get_balance(message.from_user.id)
    if not 
        msg = await message.answer("Тебя нет в базе. Напиши что-нибудь в чат.")
        schedule_delete(message.chat.id, msg.message_id)
        return
    rankoins, _, _ = data
    msg = await message.answer(f"💳 Твой баланс: {rankoins} Ранкоинов.")
    schedule_delete(message.chat.id, msg.message_id)

@dp.message(Command("top", "топ"))
async def cmd_top(message: Message):
    async with aiosqlite.connect(DB_PATH) as db:
        async with db.execute("SELECT user_id, username, first_name, rankoins FROM active_users ORDER BY rankoins DESC LIMIT 10") as cur:
            users = await cur.fetchall()
    
    if not users:
        msg = await message.answer("🏆 Топ пуст. Играйте активнее!")
        schedule_delete(message.chat.id, msg.message_id)
        return
        
    text = "🏆 <b>Топ богачей чата:</b>\n\n"
    for i, (uid, uname, fname, coins) in enumerate(users, 1):
        display = uname or fname or f"User{uid}"
        text += f"{i}. {display} — {coins} 💰\n"
        
    msg = await message.answer(text, parse_mode="HTML")
    schedule_delete(message.chat.id, msg.message_id)

# ================= 🎁 /gift =================
@dp.message(Command("gift", "подарок"))
async def cmd_gift(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP):
        return await message.answer("Только в группах.")
    
    uid = message.from_user.id
    until = time.time() + (3 * 86400)
    
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("""
            INSERT OR REPLACE INTO active_users (user_id, username, first_name, immune_until, is_premium)
            VALUES (?, ?, ?, ?, 1)
        """, (uid, message.from_user.username, message.from_user.first_name, until))
        await db.commit()

    try:
        await bot.send_message(uid, 
            f"🎁 <b>Подарок активирован!</b>\n"
            f"Ты получил статус ПРЕМИУМ на 3 дня.\n"
            f"Используй /notag, чтобы скрыться от тегов.",
            parse_mode="HTML")
    except Exception:
        pass
        
    msg = await message.answer("✅ Подарок получен! Проверь ЛС. Ты теперь Премиум.")
    schedule_delete(message.chat.id, msg.message_id)

# ================= 🛡️ /notag =================
@dp.message(Command("notag", "нетегать"))
async def cmd_no_tag(message: Message):
    uid = message.from_user.id
    is_premium = await get_user_premium_status(uid)
    
    if is_premium != 1:
        msg = await message.answer("❌ Эта команда доступна только для Премиум пользователей.\nИспользуй /gift, чтобы получить статус.")
        schedule_delete(message.chat.id, msg.message_id)
        return
    
    until = time.time() + (3 * 86400)
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute("UPDATE active_users SET immune_until = ? WHERE user_id = ?", (until, uid))
        await db.commit()
        
    msg = await message.answer("🛡️ Защита активирована! Тебя не будут тегать, пока ты не напишешь в чат.")
    schedule_delete(message.chat.id, msg.message_id)

# ================= 🎁 АДМИН: /award =================
@dp.message(Command("award", "награда"))
async def cmd_award_reply(message: Message):
    if not await is_chat_admin(message.chat.id, message.from_user.id):
        msg = await message.answer("🚫 Только для администраторов.")
        schedule_delete(message.chat.id, msg.message_id)
        return
        
    if not message.reply_to_message:
        msg = await message.answer("❌ Ответьте на сообщение, где упомянуты участники.")
        schedule_delete(message.chat.id, msg.message_id)
        return
        
    parts = message.text.split()
    amount = 10
    if len(parts) >= 2:
        try: amount = int(parts[1])
        except: pass
        
    reply_msg = message.reply_to_message
    reply_text = reply_msg.text or reply_msg.caption or ""
    targets = set()
    
    if reply_msg.entities:
        for ent in reply_msg.entities:
            if ent.type == "mention":
                uname = reply_text[ent.offset:ent.offset+ent.length].lstrip("@").lower()
                targets.add(uname)
            elif ent.type == "text_link" and ent.url.startswith("tg://user?id="):
                try: targets.add(f"id_{int(ent.url.split('=')[1])}")
                except: pass
                
    for match in re.finditer(r'@([a-zA-Z0-9_]{5,32})', reply_text):
        targets.add(match.group(1).lower())
        
    user_map = await get_all_users_map()
    awarded_ids = set()
    for t in targets:
        if t in user_map: awarded_ids.add(user_map[t])
            
    if not awarded_ids:
        msg = await message.answer("❌ Не найдено зарегистрированных пользователей в сообщении.")
        schedule_delete(message.chat.id, msg.message_id)
        return
        
    for uid in awarded_ids: await add_rankoins(uid, amount)
        
    msg = await message.answer(f"✅ Выдано {amount} Ранкоинов {len(awarded_ids)} участникам!")
    schedule_delete(message.chat.id, msg.message_id)

# ================= 📢 ЗАДАЧИ (АДМИН) =================
@dp.message(Command("quest", "задача"))
async def cmd_task(message: Message):
    if not await is_chat_admin(message.chat.id, message.from_user.id):
        msg = await message.answer("🚫 Только для админов.")
        schedule_delete(message.chat.id, msg.message_id)
        return
    
    parts = message.text.split()
    if len(parts) < 3:
        msg = await message.answer("❌ Формат: /quest [сумма] [текст задания]")
        schedule_delete(message.chat.id, msg.message_id)
        return
    
    try:
        amount = int(parts[1])
        text = " ".join(parts[2:])
    except ValueError:
        msg = await message.answer("❌ Сумма должна быть числом.")
        schedule_delete(message.chat.id, msg.message_id)
        return
    
    task_id = str(int(time.time()))
    active_tasks[task_id] = amount
    
    builder = InlineKeyboardBuilder()
    builder.button(text=f"Забрать {amount} 💰", callback_data=f"task_{task_id}")
    
    msg = await message.answer(
        f"📢 <b>Групповая задача!</b>\n\n{text}\n\nНаграда: {amount} Ранкоинов",
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

# ================= 📢 /call =================
@dp.message(Command("call", "призыв"))
async def cmd_call(message: Message):
    if message.chat.type not in (ChatType.GROUP, ChatType.SUPERGROUP): return
    if not await is_chat_admin(message.chat.id, message.from_user.id): return

    if call_lock.locked():
        msg = await message.answer("⏳ Сбор уже запущен!")
        schedule_delete(message.chat.id, msg.message_id)
        return

    args = message.text.split(maxsplit=1)
    text = args[1] if len(args) > 1 else "давай в игру"
    users = await get_active_users()
    if not users: 
        msg = await message.answer("Нет игроков для призыва.")
        schedule_delete(message.chat.id, msg.message_id)
        return

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
                    schedule_delete(chat_id, sent.message_id, delay=10.0)
                    await asyncio.sleep(0.5)
                if ping < 3: await asyncio.sleep(20)
                
            final = await bot.send_message(chat_id, "✅ Сбор завершён!")
            schedule_delete(chat_id, final.message_id, delay=10.0)
        except Exception as e:
            logging.error(f"Ошибка в /призыв: {e}")
            err = await bot.send_message(chat_id, f"⚠️ Сбор прерван: {e}")
            schedule_delete(chat_id, err.message_id, delay=10.0)

# ================= 👁️ Авто-согласие + Сброс защиты =================
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

# ================= 🌐 WEB SERVER (АНТИ-СОН) =================
async def handle_health(request):
    """Отвечает на пинги от UptimeRobot"""
    return web.Response(text="OK", status=200)

async def run_web():
    app = web.Application()
    # Добавляем маршруты для пинга
    app.router.add_get('/', handle_health)
    app.router.add_get('/health', handle_health)
    
    runner = web.AppRunner(app)
    await runner.setup()
    port = int(os.getenv("PORT", 8080))
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logging.info(f"🌐 Web-сервер запущен на порту {port} (Health: / и /health)")
    
    # Бесконечный цикл, чтобы сервер не завершался
    while True:
        await asyncio.sleep(3600)

# ================= MAIN =================
async def run_bot(): 
    await dp.start_polling(bot)

async def main():
    # 🗑️ Автоматическая очистка старой БД при запуске
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)
        logging.info("🗑️ Старая база удалена для корректного обновления.")
        
    await init_db()
    await register_commands(bot)
    logging.info("🚀 БОТ ГОТОВ. Анти-сон активен.")
    # Запускаем бота и веб-сервер параллельно
    await asyncio.gather(run_bot(), run_web())

if __name__ == "__main__":
    asyncio.run(main())
