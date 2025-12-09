from pyrogram import Client, types
from config import *
from typing import Union, Optional, AsyncGenerator

class StreamXBot(Client):
    def __init__(self):
        super().__init__(
            name="filebot",
            api_id=API_ID,
            api_hash=API_HASH,
            bot_token=BOT_TOKEN,
            workers=150,
            plugins={"root": "plugins"},
            sleep_threshold=5,
        )

StreamBot = StreamXBot()
multi_clients = {}
work_loads = {}
