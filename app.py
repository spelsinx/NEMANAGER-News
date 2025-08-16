# bot.py
import os
import re
import json
import sqlite3
import hashlib
import asyncio
import logging
import html
import signal
from datetime import datetime
from importlib import reload

from dotenv import load_dotenv
from telegram import Update, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    filters,
    ContextTypes,
)
from telethon import TelegramClient
from telethon.errors.rpcerrorlist import (
    ChannelPrivateError, ChannelInvalidError, ChannelBannedError, AuthKeyUnregisteredError
)

import sources

# ——— Load .env ———
load_dotenv()
API_ID      = int(os.getenv("TG_API_ID", "0"))
API_HASH    = os.getenv("TG_API_HASH", "")
BOT_TOKEN   = os.getenv("BOT_TOKEN", "")
INBOX_CHAT  = os.getenv("INBOX_CHAT", "")
ADMIN_ID    = int(os.getenv("ADMIN_ID", "0"))
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "3600"))
DB_PATH     = os.getenv("DB_PATH", "bot.db")
SESSION_FILE = "session_fetcher.session"
KW_CONFIG   = os.path.join(os.path.dirname(__file__), "config.json")

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ——— Telethon client ———
telethon_client = TelegramClient(SESSION_FILE, API_ID, API_HASH)
async def ensure_telethon_connected():
    if not telethon_client.is_connected():
        try:
            await telethon_client.connect()
        except AuthKeyUnregisteredError:
            if os.path.exists(SESSION_FILE):
                os.remove(SESSION_FILE)
            logger.error("Повреждена сессия Telethon")
            raise

# ——— Sources persistence ———
def save_sources_py():
    path = os.path.join(os.path.dirname(__file__), "sources.py")
    with open(path, "w", encoding="utf-8") as f:
        f.write("SOURCES = " + repr(sources.SOURCES) + "\n")

# ——— Keywords persistence ———
def load_keywords():
    if not os.path.exists(KW_CONFIG):
        with open(KW_CONFIG, "w", encoding="utf-8") as f:
            json.dump([], f, ensure_ascii=False, indent=2)
    with open(KW_CONFIG, "r", encoding="utf-8") as f:
        return json.load(f)

def save_keywords(keywords):
    with open(KW_CONFIG, "w", encoding="utf-8") as f:
        json.dump(keywords, f, ensure_ascii=False, indent=2)

# ——— Database init ———
def init_db():
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("""CREATE TABLE IF NOT EXISTS seen (
        id INTEGER PRIMARY KEY, source TEXT, url TEXT, hash TEXT UNIQUE
    )""")
    c.execute("""CREATE TABLE IF NOT EXISTS admins (
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT UNIQUE
    )""")
    # Добавляем главного админа из .env
    if ADMIN_ID:
        c.execute("INSERT OR IGNORE INTO admins(username) VALUES(?)", (ADMIN_ID,))
    con.commit(); con.close()

def is_admin(username):
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("SELECT 1 FROM admins WHERE username=?", (username,))
    ok = c.fetchone() is not None
    con.close()
    return ok

def add_admin(username):
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("INSERT OR IGNORE INTO admins(username) VALUES(?)", (username,))
    con.commit(); con.close()

# ——— NewsItem & fetch ———
class NewsItem:
    def __init__(self, title, body, url, date, source):
        self.title = title
        self.body  = body
        self.url   = url
        self.date  = date
        self.source= source

async def fetch_posts(channel, name, limit=500):
    await ensure_telethon_connected()
    items=[]
    try:
        ent = await telethon_client.get_entity(channel)
        msgs = await telethon_client.get_messages(ent, limit=limit)
        for m in msgs:
            if not m.message: continue
            if "\n\n" in m.message:
                title, body = m.message.split("\n\n",1)
            else:
                title = m.message; body = m.message
            items.append(NewsItem(
                title.strip(), body.strip(),
                f"https://t.me/{channel.lstrip('@')}/{m.id}",
                m.date.astimezone(), name
            ))
    except (ChannelPrivateError,ChannelInvalidError,ChannelBannedError):
        logger.warning("Канал недоступен: %s", channel)
    return items

# ——— Helpers ———
def is_new(source,url,title):
    h = hashlib.sha256(f"{source}|{title}".encode()).hexdigest()
    con = sqlite3.connect(DB_PATH); c = con.cursor()
    c.execute("SELECT 1 FROM seen WHERE hash=?", (h,))
    if not c.fetchone():
        c.execute("INSERT INTO seen(source,url,hash) VALUES(?,?,?)",(source,url,h))
        con.commit(); con.close()
        return True
    con.close(); return False

def match_keywords(text):
    kws = load_keywords()
    norm = re.sub(r"[^\wа-яё]+"," ",text.lower())
    return [k for k in kws if re.search(k, norm, flags=re.IGNORECASE)]

# ——— Core job ———
async def process_and_send(bot):
    reload(sources)
    for src in sources.SOURCES:
        if not src["enabled"]: continue
        posts = await fetch_posts(src["identifier"], src["name"])
        for it in posts:
            if not is_new(it.source,it.url,it.title): continue
            kws = match_keywords(it.title+" "+it.body)
            if not kws: continue
            full_body = it.title + "\n\n" + it.body
            text = (
                f"<b>{html.escape(it.title)}</b>\n"
                f"<i>Источник:</i> {html.escape(it.source)} • {it.date:%Y-%m-%d %H:%M} (MSK)\n\n"
                f"<i>Ключевые слова:</i> {', '.join(kws)}\n\n"
                f"<i>Ссылка:</i> <a href=\"{it.url}\">Перейти</a>\n\n"
                f"<i>Содержание поста:</i>\n<pre>{html.escape(full_body)}</pre>"
            )
            await bot.send_message(INBOX_CHAT, text,
                                   parse_mode="HTML",
                                   disable_web_page_preview=True)

# ——— Command handlers ———
async def require_admin(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user.username or update.effective_user.id
    if not is_admin(str(user)):
        await update.message.reply_text("Доступ только для администраторов")
        return False
    return True

async def cmd_start(update: Update, ctx):
    lines = [
        "/status — статистика",
        "/last — последние 5",
        "/sources — список источников",
        "/addsource — добавить источник",
        "/disable — отключить источник",
        "/enable — включить источник",
        "/delsource — удалить источник",
        "/filter — показать/добавить ключевые слова",
        "/delconfig — удалить ключевое слово",
        "/check — пробная проверка",
        "/post — опубликовать выбранную новость",
        "/giveadmin — выдать права админа",
    ]
    await update.message.reply_text("Команды:\n" + "\n".join(lines))

async def cmd_status(update, ctx):
    if not await require_admin(update, ctx): return
    con=sqlite3.connect(DB_PATH)
    total=con.execute("SELECT COUNT(*) FROM seen").fetchone()[0]
    con.close()
    await update.message.reply_text(f"Обработано новостей: {total}")

async def cmd_last(update, ctx):
    if not await require_admin(update, ctx): return
    con=sqlite3.connect(DB_PATH)
    rows=con.execute("SELECT source,url FROM seen ORDER BY id DESC LIMIT 5").fetchall()
    con.close()
    if not rows: 
        return await update.message.reply_text("Нет истории")
    text="\n".join(f"{s}: {u}" for s,u in rows)
    await update.message.reply_text(text)

async def cmd_sources(update, ctx):
    if not await require_admin(update, ctx): return
    reload(sources)
    lines = [f"{i}. {s['name']} ({s['identifier']}) — {'🟢' if s['enabled'] else '🔴'}"
             for i,s in enumerate(sources.SOURCES,1)]
    await update.message.reply_text("\n".join(lines) or "Пусто")

async def cmd_addsource(update, ctx):
    if not await require_admin(update, ctx): return
    if not ctx.args:
        return await update.message.reply_text("Использование: /addsource @user или t.me/user")
    arg=ctx.args[0]
    ident = arg if arg.startswith('@') else '@'+arg.rstrip('/').split('/')[-1]
    reload(sources)
    if any(s["identifier"].lower()==ident.lower() for s in sources.SOURCES):
        return await update.message.reply_text("Уже есть")
    await ensure_telethon_connected()
    try:
        ent=await telethon_client.get_entity(ident); name=ent.title or ident
    except: name=ident
    sources.SOURCES.append({"identifier":ident,"name":name,"enabled":True})
    save_sources_py()
    await update.message.reply_text(f"Добавлен: {name}")

async def cmd_disable(update, ctx):
    if not await require_admin(update, ctx): return
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("Исп: /disable <№>")
    idx=int(ctx.args[0])-1
    reload(sources)
    if idx<0 or idx>=len(sources.SOURCES):
        return await update.message.reply_text("Неправильный №")
    sources.SOURCES[idx]["enabled"]=False
    save_sources_py()
    await update.message.reply_text("Отключено")

async def cmd_enable(update, ctx):
    if not await require_admin(update, ctx): return
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("Исп: /enable <№>")
    idx=int(ctx.args[0])-1
    reload(sources)
    if idx<0 or idx>=len(sources.SOURCES):
        return await update.message.reply_text("Неправильный №")
    sources.SOURCES[idx]["enabled"]=True
    save_sources_py()
    await update.message.reply_text("Включено")

async def cmd_delsource(update, ctx):
    if not await require_admin(update, ctx): return
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("Исп: /delsource <№>")
    idx=int(ctx.args[0])-1
    reload(sources)
    if idx<0 or idx>=len(sources.SOURCES):
        return await update.message.reply_text("Неправильный №")
    rem=sources.SOURCES.pop(idx)
    save_sources_py()
    await update.message.reply_text(f"Удалён: {rem['name']}")

async def cmd_filter(update, ctx):
    if not await require_admin(update, ctx): return
    # показать и добавить
    keywords = load_keywords()
    if ctx.args:
        new = ctx.args[0].split("|")
        for w in new:
            w=w.strip()
            if w and w not in keywords:
                keywords.append(w)
        save_keywords(keywords)
        return await update.message.reply_text("Добавлено.")
    # показать
    lines = [f"{i+1}. {w}" for i,w in enumerate(keywords)]
    await update.message.reply_text("\n".join(lines) or "Пусто")

async def cmd_delconfig(update, ctx):
    if not await require_admin(update, ctx): return
    if not ctx.args or not ctx.args[0].isdigit():
        return await update.message.reply_text("Исп: /delconfig <№>")
    num=int(ctx.args[0])-1
    keywords=load_keywords()
    if num<0 or num>=len(keywords):
        return await update.message.reply_text("Неправильный №")
    keywords.pop(num)
    save_keywords(keywords)
    await update.message.reply_text("Удалено.")

async def cmd_check(update, ctx):
    if not await require_admin(update, ctx): return
    await update.message.reply_text("Пробная проверка…")
    reload(sources)
    for src in sources.SOURCES:
        if not src["enabled"]: continue
        posts=await fetch_posts(src["identifier"],src["name"])
        for it in posts:
            kws=match_keywords(it.title+" "+it.body)
            if kws:
                full=it.title+"\n\n"+it.body
                text=(
                    f"<b>{html.escape(it.title)}</b>\n"
                    f"<i>Источник:</i> {html.escape(it.source)} • {it.date:%Y-%m-%d %H:%M}\n\n"
                    f"<i>Ключевые слова:</i> {', '.join(kws)}\n\n"
                    f"<i>Ссылка:</i> <a href=\"{it.url}\">Перейти</a>\n\n"
                    f"<i>Содержание поста:</i>\n<pre>{html.escape(full)}</pre>"
                )
                await ctx.bot.send_message(ADMIN_ID, text,
                                           parse_mode="HTML",
                                           disable_web_page_preview=True)
                break
    await update.message.reply_text("Готово")

async def cmd_post(update, ctx):
    if not await require_admin(update, ctx): return
    rm=update.message.reply_to_message
    if not rm:
        return await update.message.reply_text("Ответьте на сообщение")
    await ctx.bot.send_message(
        INBOX_CHAT,
        text=rm.text or rm.caption or "",
        entities=rm.entities or rm.caption_entities,
        disable_web_page_preview=True
    )
    await update.message.reply_text("Опубликовано.")

async def cmd_giveadmin(update, ctx):
    if not await require_admin(update, ctx): return
    if not ctx.args:
        return await update.message.reply_text("Исп: /giveadmin @username")
    user=ctx.args[0].lstrip('@')
    add_admin(user)
    await update.message.reply_text(f"{user} стал админом.")

# ——— Command menu ———
async def setup_commands(app):
    cmds=[
        BotCommand("start","Список команд"),
        BotCommand("status","Статистика"),
        BotCommand("last","Последние 5"),
        BotCommand("sources","Список источников"),
        BotCommand("addsource","Добавить источник"),
        BotCommand("disable","Отключить источник"),
        BotCommand("enable","Включить источник"),
        BotCommand("delsource","Удалить источник"),
        BotCommand("filter","Управление ключевыми словами"),
        BotCommand("delconfig","Удалить ключевое слово"),
        BotCommand("check","Пробная проверка"),
        BotCommand("post","Опубликовать новость"),
        BotCommand("giveadmin","Добавить админа"),
    ]
    await app.bot.set_my_commands(cmds)

# ——— Graceful shutdown ———
def _shutdown(sig, frame):
    asyncio.get_event_loop().create_task(telethon_client.disconnect())
signal.signal(signal.SIGINT, _shutdown)
signal.signal(signal.SIGTERM, _shutdown)

# ——— Main ———
def main():
    init_db()
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start",   cmd_start))
    app.add_handler(CommandHandler("status",  cmd_status))
    app.add_handler(CommandHandler("last",    cmd_last))
    app.add_handler(CommandHandler("sources", cmd_sources))
    app.add_handler(CommandHandler("addsource", cmd_addsource))
    app.add_handler(CommandHandler("disable", cmd_disable))
    app.add_handler(CommandHandler("enable",  cmd_enable))
    app.add_handler(CommandHandler("delsource", cmd_delsource))
    app.add_handler(CommandHandler("filter",  cmd_filter))
    app.add_handler(CommandHandler("delconfig", cmd_delconfig))
    app.add_handler(CommandHandler("check",   cmd_check))
    app.add_handler(CommandHandler("post",    cmd_post))
    app.add_handler(CommandHandler("giveadmin", cmd_giveadmin))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, lambda u,c: None))  # no-op

    app.post_init = setup_commands

    jq = app.job_queue
    jq.run_repeating(lambda ctx: asyncio.create_task(process_and_send(ctx.bot)),
                     interval=POLL_INTERVAL_SECONDS, first=0)

    logger.info("Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
