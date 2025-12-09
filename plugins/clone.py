
import re
import logging
from pymongo import MongoClient
from Script import script
from pyrogram import Client, filters
from pyrogram.types import Message
from pyrogram.errors.exceptions.bad_request_400 import AccessTokenExpired, AccessTokenInvalid
from config import API_ID, API_HASH, DB_URI, DB_NAME, CLONE_MODE

logger = logging.getLogger(__name__)

# Lazy MongoDB connection initialization
mongo_client = None
mongo_db = None
settings_db = None

def get_mongo_db():
    """Initialize MongoDB connection lazily"""
    global mongo_client, mongo_db, settings_db
    if mongo_client is None and DB_URI:
        mongo_client = MongoClient(DB_URI)
        mongo_db = mongo_client["cloned_storebotz"]
        settings_db = mongo_client[DB_NAME]
    return mongo_db, settings_db

async def get_clone_mode():
    """Get clone mode from database, fallback to config"""
    try:
        _, settings_db_instance = get_mongo_db()
        if settings_db_instance:
            settings = settings_db_instance.settings.find_one({"_id": "clone_mode"})
            if settings:
                return settings.get("enabled", CLONE_MODE)
    except:
        pass
    return CLONE_MODE

@Client.on_message(filters.command("clone") & filters.private)
async def clone(client, message):
    try:
        from plugins.admin_settings import get_clone_mode as get_db_clone_mode
        clone_mode = await get_db_clone_mode()
    except:
        clone_mode = CLONE_MODE
    if clone_mode == False:
        return await message.reply("<b>❌ Clone mode is currently disabled by the admin.</b>") 
    Storebot = await client.ask(message.chat.id, "<b>1) sᴇɴᴅ <code>/newbot</code> ᴛᴏ @BotFather\n2) ɢɪᴠᴇ ᴀ ɴᴀᴍᴇ ꜰᴏʀ ʏᴏᴜʀ ʙᴏᴛ.\n3) ɢɪᴠᴇ ᴀ ᴜɴɪǫᴜᴇ ᴜsᴇʀɴᴀᴍᴇ.\n4) ᴛʜᴇɴ ʏᴏᴜ ᴡɪʟʟ ɢᴇᴛ ᴀ ᴍᴇssᴀɢᴇ ᴡɪᴛʜ ʏᴏᴜʀ ʙᴏᴛ ᴛᴏᴋᴇɴ.\n5) ꜰᴏʀᴡᴀʀᴅ ᴛʜᴀᴛ ᴍᴇssᴀɢᴇ ᴛᴏ ᴍᴇ.\n\n/cancel - ᴄᴀɴᴄᴇʟ ᴛʜɪs ᴘʀᴏᴄᴇss.</b>")
    if Storebot.text == '/cancel':
        await Storebot.delete()
        return await message.reply('<b>ᴄᴀɴᴄᴇʟᴇᴅ ᴛʜɪs ᴘʀᴏᴄᴇss 🚫</b>')
    if Storebot.forward_from and Storebot.forward_from.id == 93372553:
        try:
            bot_token = re.findall(r"\b(\d+:[A-Za-z0-9_-]+)\b", Storebot.text)[0]
        except:
            return await message.reply('<b>sᴏᴍᴇᴛʜɪɴɢ ᴡᴇɴᴛ ᴡʀᴏɴɢ 😕</b>')
    else:
        return await message.reply('<b>ɴᴏᴛ ꜰᴏʀᴡᴀʀᴅᴇᴅ ꜰʀᴏᴍ @BotFather 😑</b>')
    user_id = message.from_user.id
    msg = await message.reply_text("**👨‍💻 ᴡᴀɪᴛ ᴀ ᴍɪɴᴜᴛᴇ ɪ ᴀᴍ ᴄʀᴇᴀᴛɪɴɢ ʏᴏᴜʀ ʙᴏᴛ ❣️**")
    try:
        StoreClient = Client(
            f"{bot_token}", API_ID, API_HASH,
            bot_token=bot_token,
            plugins={"root": "clone_plugins"}
        )
        await StoreClient.start()
        bot = await StoreClient.get_me()
        details = {
            'bot_id': bot.id,
            'is_bot': True,
            'user_id': user_id,
            'name': bot.first_name,
            'token': bot_token,
            'username': bot.username
        }
        mongo_db_instance, _ = get_mongo_db()
        if mongo_db_instance:
            mongo_db_instance.bots.insert_one(details)
        await msg.edit_text(f"<b>sᴜᴄᴄᴇssғᴜʟʟʏ ᴄʟᴏɴᴇᴅ ʏᴏᴜʀ ʙᴏᴛ: @{bot.username}.</b>")
    except BaseException as e:
        await msg.edit_text(f"⚠️ <b>Bot Error:</b>\n\n<code>{e}</code>\n\n**Kindly forward this message to @AdminTeam to get assistance.**")

@Client.on_message(filters.command("deletecloned") & filters.private)
async def delete_cloned_bot(client, message):
    try:
        from plugins.admin_settings import get_clone_mode as get_db_clone_mode
        clone_mode = await get_db_clone_mode()
    except:
        clone_mode = CLONE_MODE
    if clone_mode == False:
        return await message.reply("<b>❌ Clone mode is currently disabled by the admin.</b>") 
    try:
        Storebot = await client.ask(message.chat.id, "**Send Me Bot Token To Delete**")
        bot_token = re.findall(r'\d[0-9]{8,10}:[0-9A-Za-z_-]{35}', Storebot.text, re.IGNORECASE)
        bot_token = bot_token[0] if bot_token else None
        bot_id = re.findall(r'\d[0-9]{8,10}', Storebot.text)
        mongo_db_instance, _ = get_mongo_db()
        if mongo_db_instance:
            cloned_bot = mongo_db_instance.bots.find_one({"token": bot_token})
            if cloned_bot:
                mongo_db_instance.bots.delete_one({"token": bot_token})
            await message.reply_text("**🤖 ᴛʜᴇ ᴄʟᴏɴᴇᴅ ʙᴏᴛ ʜᴀs ʙᴇᴇɴ ʀᴇᴍᴏᴠᴇᴅ ғʀᴏᴍ ᴛʜᴇ ʟɪsᴛ ᴀɴᴅ ɪᴛs ᴅᴇᴛᴀɪʟs ʜᴀᴠᴇ ʙᴇᴇɴ ʀᴇᴍᴏᴠᴇᴅ ғʀᴏᴍ ᴛʜᴇ ᴅᴀᴛᴀʙᴀsᴇ. ☠️**")
        else:
            await message.reply_text("**⚠️ ᴛʜᴇ ʙᴏᴛ ᴛᴏᴋᴇɴ ᴘʀᴏᴠɪᴅᴇᴅ ɪs ɴᴏᴛ ɪɴ ᴛʜᴇ ᴄʟᴏɴᴇᴅ ʟɪsᴛ.**")
    except:
        await message.reply_text("An error occurred while deleting the cloned bot.")

async def restart_bots():
    mongo_db_instance, _ = get_mongo_db()
    if not mongo_db_instance:
        logger.warning("MongoDB not configured, skipping bot restart")
        return
    
    try:
        bots = list(mongo_db_instance.bots.find())
        for bot in bots:
            bot_token = bot['token']
            try:
                StoreClient = Client(
                    f"{bot_token}", API_ID, API_HASH,
                    bot_token=bot_token,
                    plugins={"root": "clone_plugins"},
                )
                await StoreClient.start()
            except:
                pass
    except Exception as e:
        logger.warning(f"Error restarting bots: {e}")
