import logging
from pyrogram import Client, filters
from config import ADMINS
from pymongo import MongoClient
from config import DB_URI, DB_NAME

logger = logging.getLogger(__name__)
mongo_client = MongoClient(DB_URI)
mongo_db = mongo_client[DB_NAME]

async def get_clone_mode():
    """Get clone mode from database"""
    try:
        settings = mongo_db.settings.find_one({"_id": "clone_mode"})
        if settings:
            return settings.get("enabled", False)
        return False
    except:
        return False

async def set_clone_mode(enabled):
    """Set clone mode in database"""
    try:
        mongo_db.settings.update_one(
            {"_id": "clone_mode"},
            {"$set": {"enabled": enabled}},
            upsert=True
        )
        return True
    except Exception as e:
        logger.error(f"Error setting clone mode: {e}")
        return False


@Client.on_message(filters.command("cloneon") & filters.private & filters.user(ADMINS))
async def clone_on(client, message):
    """Turn on clone mode"""
    await set_clone_mode(True)
    await message.reply_text("<b>✅ Clone mode is now ON</b>")


@Client.on_message(filters.command("cloneoff") & filters.private & filters.user(ADMINS))
async def clone_off(client, message):
    """Turn off clone mode"""
    await set_clone_mode(False)
    await message.reply_text("<b>✅ Clone mode is now OFF</b>")


@Client.on_message(filters.command("clonestatus") & filters.private & filters.user(ADMINS))
async def clone_status(client, message):
    """Check clone mode status"""
    status = await get_clone_mode()
    status_text = "✅ ON" if status else "❌ OFF"
    await message.reply_text(f"<b>Clone Mode Status: {status_text}</b>")

