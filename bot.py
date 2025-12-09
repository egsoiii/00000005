import sys
import glob
import importlib
from pathlib import Path
import logging
import logging.config

# Patch asyncio for Python 3.12 compatibility
import patch_asyncio

import asyncio
from pyrogram import Client, __version__, idle
from pyrogram.raw.all import layer
from config import LOG_CHANNEL, ON_HEROKU, CLONE_MODE, PORT
from Script import script 
from datetime import date, datetime 
import pytz
from aiohttp import web
from plugins.clone import restart_bots
from core.bot import StreamBot
from core.utils.keepalive import ping_server
from core.bot.clients import initialize_clients

# Get logging configurations
logging.config.fileConfig('logging.conf')
logging.getLogger().setLevel(logging.INFO)
logging.getLogger("pyrogram").setLevel(logging.ERROR)

async def health_check(_):
    """Minimal health check endpoint for port binding"""
    return web.json_response({"status": "running"})

ppath = "plugins/*.py"
files = glob.glob(ppath)
StreamBot.start()
loop = asyncio.get_event_loop()

async def start():
    print('\n')
    print('Initializing Bot')
    bot_info = await StreamBot.get_me()
    StreamBot.username = bot_info.username
    await initialize_clients()
    for name in files:
        with open(name) as a:
            patt = Path(a.name)
            plugin_name = patt.stem.replace(".py", "")
            plugins_dir = Path(f"plugins/{plugin_name}.py")
            import_path = "plugins.{}".format(plugin_name)
            spec = importlib.util.spec_from_file_location(import_path, plugins_dir)
            load = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(load)
            sys.modules["plugins." + plugin_name] = load
            print("Imported => " + plugin_name)
    if ON_HEROKU:
        asyncio.create_task(ping_server())
    me = await StreamBot.get_me()
    tz = pytz.timezone('Asia/Kolkata')
    today = date.today()
    now = datetime.now(tz)
    time = now.strftime("%H:%M:%S %p")
    await StreamBot.send_message(chat_id=LOG_CHANNEL, text=script.RESTART_TXT.format(today, time))
    
    # Minimal HTTP server for port binding (Render requirement)
    app = web.Application()
    app.router.add_get("/", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"✅ Bot server listening on port {PORT}")
    
    if CLONE_MODE == True:
        await restart_bots()
    print("Bot Started")
    await idle()

loop.run_until_complete(start())
