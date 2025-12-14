import aiohttp
import logging
from config import BOT_TOKEN

logger = logging.getLogger(__name__)

async def send_message_raw(chat_id, text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=None):
    """Send a message using raw Telegram Bot API (supports copy_text buttons)"""
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_page_preview
    }
    if reply_markup:
        payload["reply_markup"] = {"inline_keyboard": reply_markup}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(api_url, json=payload) as resp:
            result = await resp.json()
            if not result.get("ok"):
                logger.error(f"Send message error: {result.get('description')}")
            return result

async def edit_message_text_raw(chat_id, message_id, text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=None):
    """Edit message text using raw Telegram Bot API (supports copy_text buttons)"""
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": text,
        "parse_mode": parse_mode,
        "disable_web_page_preview": disable_web_page_preview
    }
    if reply_markup:
        payload["reply_markup"] = {"inline_keyboard": reply_markup}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(api_url, json=payload) as resp:
            result = await resp.json()
            if not result.get("ok"):
                logger.error(f"Edit message text error: {result.get('description')}")
            return result

async def edit_message_caption_raw(chat_id, message_id, caption, parse_mode="HTML", reply_markup=None):
    """Edit message caption using raw Telegram Bot API (supports copy_text buttons)"""
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageCaption"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "caption": caption,
        "parse_mode": parse_mode
    }
    if reply_markup:
        payload["reply_markup"] = {"inline_keyboard": reply_markup}
    
    async with aiohttp.ClientSession() as session:
        async with session.post(api_url, json=payload) as resp:
            result = await resp.json()
            if not result.get("ok"):
                logger.error(f"Edit message caption error: {result.get('description')}")
            return result

async def edit_message_reply_markup_raw(chat_id, message_id, reply_markup):
    """Edit message reply markup using raw Telegram Bot API (supports copy_text buttons)"""
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageReplyMarkup"
    payload = {
        "chat_id": chat_id,
        "message_id": message_id,
        "reply_markup": {"inline_keyboard": reply_markup}
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(api_url, json=payload) as resp:
            result = await resp.json()
            if not result.get("ok"):
                logger.error(f"Edit message reply markup error: {result.get('description')}")
            return result

async def edit_message_with_fallback(chat_id, message_id, text, parse_mode="HTML", disable_web_page_preview=True, reply_markup=None):
    """Edit message text with fallback to caption for media messages"""
    result = await edit_message_text_raw(chat_id, message_id, text, parse_mode, disable_web_page_preview, reply_markup)
    if not result.get("ok"):
        result = await edit_message_caption_raw(chat_id, message_id, text, parse_mode, reply_markup)
    return result

def convert_pyrogram_buttons_to_raw(buttons):
    """Convert Pyrogram InlineKeyboardButton list to raw API format"""
    raw_buttons = []
    for row in buttons:
        raw_row = []
        for btn in row:
            if hasattr(btn, 'callback_data') and btn.callback_data:
                raw_row.append({"text": btn.text, "callback_data": btn.callback_data})
            elif hasattr(btn, 'url') and btn.url:
                raw_row.append({"text": btn.text, "url": btn.url})
        if raw_row:
            raw_buttons.append(raw_row)
    return raw_buttons
