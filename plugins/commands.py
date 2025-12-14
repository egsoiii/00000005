
import os
import logging
import random
import asyncio
from validators import domain
from Script import script
from plugins.dbusers import db
from pyrogram import Client, filters, enums
from pyrogram.errors import ChatAdminRequired, FloodWait
from pyrogram.types import *
from pyrogram.raw import functions, types as raw_types
from utils import verify_user, check_token, check_verification, get_token, b64_encode, b64_decode
from config import *
import re
import json
from urllib.parse import quote_plus
from core.utils.file_properties import get_name, get_hash, get_media_file_size
from pyrogram.errors import PeerIdInvalid, ChannelInvalid, ChatIdInvalid
import aiohttp
logger = logging.getLogger(__name__)

BATCH_FILES = {}
BATCH_STOP_FLAGS = {}  # Track which batches should stop: {user_id: True/False}
CAPTION_INPUT_MODE = {}  # Track users entering caption: {user_id: True/False}
FOLDER_PROMPT_MSG = {}  # Track folder creation prompt message IDs: {user_id: message_id}
REPORT_BUG_MODE = {}  # Track users in report bug mode: {user_id: message_id}
RESTORE_MODE = {}  # Track users entering restore token: {user_id: True}
VERIFIED_FOLDER_ACCESS = {}  # Track verified password access: {"user_id_owner_id_folder": True}
PASSWORD_ATTEMPTS = {}  # Track password attempt counts: {user_id_owner_id_folder: attempt_count}
PASSWORD_PROMPT_MESSAGES = {}  # Track password prompt message IDs: {user_id: [msg_ids]}
PASSWORD_RESPONSE_MESSAGES = {}  # Track password response message IDs: {user_id: [msg_ids]}

async def show_folder_edit_menu(client, user_id, message_id, idx, folder_name, display_name, force_is_protected=None):
    """Shared helper to show the folder edit menu with consistent button layout
    
    force_is_protected: If provided, use this value instead of checking database.
                       Useful after password operations to avoid cache issues.
    """
    from plugins.dbusers import db
    
    # Get or generate token-based share link
    token = await db.get_folder_token(user_id, folder_name)
    if not token:
        token = await db.generate_folder_token(user_id, folder_name)
    
    username = (await client.get_me()).username
    share_link = f"https://t.me/{username}?start=folder_{token}"
    
    folder_encoded = b64_encode(folder_name, "utf-8")
    
    # Use forced value if provided, otherwise check database
    if force_is_protected is not None:
        is_protected = force_is_protected
    else:
        is_protected = await db.is_folder_password_protected(user_id, folder_name)
    
    # Build consistent button layout
    raw_buttons = [
        [{"text": "Copy folder link", "copy_text": {"text": share_link}}, {"text": "♻️ Change Link", "callback_data": f"change_folder_link_{idx}"}],
    ]
    
    # Use unified password buttons
    raw_buttons.extend(build_password_buttons('folder', idx, is_protected))
    
    raw_buttons.append([{"text": "✏️ Rename", "callback_data": f"rename_folder_action_{idx}"}, {"text": "🗑️ Delete", "callback_data": f"delete_folder_action_{idx}"}])
    raw_buttons.append([{"text": "⋞ ʙᴀᴄᴋ", "callback_data": f"browse_folder_{folder_encoded}"}])
    
    protection_status = "🔒 Password Protected" if is_protected else ""
    edit_text = f"<b>✏️ Edit Folder: {display_name}</b>\n{protection_status}\n\nSelect an option:"
    
    # Send using raw API for copy_text support
    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
    payload = {
        "chat_id": user_id,
        "message_id": message_id,
        "text": edit_text,
        "parse_mode": "HTML",
        "reply_markup": {"inline_keyboard": raw_buttons}
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(api_url, json=payload) as resp:
            result = await resp.json()
            if not result.get("ok"):
                api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageCaption"
                payload = {
                    "chat_id": user_id,
                    "message_id": message_id,
                    "caption": edit_text,
                    "parse_mode": "HTML",
                    "reply_markup": {"inline_keyboard": raw_buttons}
                }
                async with session.post(api_url, json=payload) as resp2:
                    await resp2.json()

def build_password_buttons(item_type, identifier, is_protected):
    """Build password-related buttons for files or folders
    
    item_type: 'file' or 'folder'
    identifier: file_idx (int) for files, folder_idx (int) for folders
    is_protected: whether the item has a password set
    
    Returns: list of button rows (raw API format for copy_text support)
    """
    if is_protected:
        return [[
            {"text": "👁️ View Password", "callback_data": f"view_password_{item_type}_{identifier}"},
            {"text": "🗑️ Remove Password", "callback_data": f"confirm_remove_pw_{item_type}_{identifier}"}
        ]]
    else:
        return [[{"text": "🔐 Set Password", "callback_data": f"set_password_{item_type}_{identifier}"}]]

def get_size(size):
    """Get size in readable format"""

    units = ["Bytes", "KB", "MB", "GB", "TB", "PB", "EB"]
    size = float(size)
    i = 0
    while size >= 1024.0 and i < len(units):
        i += 1
        size /= 1024.0
    return "%.2f %s" % (size, units[i])

async def formate_file_name(file_name, user_id=None):
    """Format filename by removing brackets, URLs, and applying user filters"""
    chars = ["[", "]", "(", ")"]
    for c in chars:
        file_name = file_name.replace(c, "")
    file_name = ' '.join(filter(lambda x: not x.startswith('http') and not x.startswith('@') and not x.startswith('www.'), file_name.split()))
    
    # Apply user filters to filename
    if user_id:
        file_name = await apply_text_filters(user_id, file_name)
    
    return file_name

async def get_forum_topics(client, chat_id):
    """Fetch all forum topics from a supergroup"""
    try:
        chat = await client.get_chat(chat_id)
        if not chat.is_forum:
            return []
        
        topics = []
        async for topic in client.get_forum_topics(chat_id):
            topics.append({
                'id': topic.id,
                'title': topic.title
            })
        return topics
    except Exception as e:
        logger.error(f"Error fetching forum topics: {e}")
        return []

async def apply_text_filters(user_id, text):
    """Apply filters to any text (caption or filename) while preserving line breaks"""
    filters_list = await db.get_filename_filters(user_id)
    for item in filters_list:
        if "|" in item:
            # Replacement format: old|new
            old, new = item.split("|", 1)
            text = text.replace(old.strip(), new.strip())
        else:
            # Removal format: just remove
            text = text.replace(item, "")
    # Clean up extra spaces per line while preserving line breaks
    lines = text.split('\n')
    lines = [' '.join(line.split()) for line in lines]
    return '\n'.join(lines)

async def build_file_caption(user_id, file_name, file_size, duration=None, original_caption=None, original_filename=None):
    """Build caption with template variable support, then apply word filters"""
    user_caption = await db.get_caption(user_id)
    
    # Step 1 — Build the caption (original or template)
    if not user_caption:
        # No custom caption → use original if available, otherwise empty
        caption = original_caption or ""
    else:
        # Use custom caption template
        caption = user_caption
        caption = caption.replace("{filename}", file_name)
        caption = caption.replace("{filesize}", file_size)
        caption = caption.replace("{duration}", duration or "N/A")
    
    # Step 2 — Apply filters to the entire caption
    if caption:
        caption = await apply_text_filters(user_id, caption)
    
    return caption

async def build_settings_ui(destinations, delivery_mode, user_id=None):
    """Build consistent Settings UI buttons and text"""
    from config import MAX_DESTINATIONS
    
    buttons = []
    caption = None
    folders_count = 0
    if user_id:
        caption = await db.get_caption(user_id)
        folders = await db.get_folders(user_id)
        folders_count = len(folders)
    
    # Destinations button
    if destinations:
        buttons.append([InlineKeyboardButton(f'📋 Destinations ({len(destinations)}/{MAX_DESTINATIONS})', callback_data='view_destinations')])
    else:
        buttons.append([InlineKeyboardButton(f'📋 Destinations (0/{MAX_DESTINATIONS})', callback_data='view_destinations')])
    
    # Caption button
    caption_text = f"📝 Caption" if not caption else f"📝 Caption ({len(caption)} chars)"
    buttons.append([InlineKeyboardButton(caption_text, callback_data='caption_menu')])
    
    # Replace or Delete Word button
    buttons.append([InlineKeyboardButton('🔄 Replace or Delete Word', callback_data='customize_menu')])
    
    # Back button
    buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='start')])
    
    mode_display = {"pm": "Bot only", "channel": "Channel only", "both": "Both Bot and Channel"}.get(delivery_mode.lower(), delivery_mode.upper())
    text = f"<b>⚙️ Settings\n\n📤 Destinations: {len(destinations)}/{MAX_DESTINATIONS}\n📨 Send file in: {mode_display}</b>"
    
    return buttons, text

def build_start_buttons():
    """Build consistent Start menu buttons - used by both /start command and callback"""
    return [
        [InlineKeyboardButton('🔍 Support', url='https://t.me/premium'), InlineKeyboardButton('🤖 Updates', url='https://t.me/premium')],
        [InlineKeyboardButton('💝 Help', callback_data='help'), InlineKeyboardButton('😊 About', callback_data='about')],
        [InlineKeyboardButton('📂 My Files', callback_data='my_files_menu'), InlineKeyboardButton('⚙️ Settings', callback_data='settings')]
    ]

def build_reply_keyboard(report_mode=False):
    """Build Reply Keyboard for main menu"""
    if report_mode:
        return ReplyKeyboardMarkup([['❌ Cancel']], resize_keyboard=True, one_time_keyboard=False, is_persistent=False)
    return ReplyKeyboardMarkup([
        ['📁 My Files', '⚙️ Settings'],
        ['🧐 Report Bug', '💗 About Us']
    ], resize_keyboard=True, one_time_keyboard=False, is_persistent=False)

def build_my_files_buttons():
    """Build consistent My Files menu buttons - used by both reply keyboard and callback"""
    return [
        [InlineKeyboardButton('📄 All Files', callback_data='view_all_files')],
        [InlineKeyboardButton('🏷️ By Category', callback_data='files_by_category'), InlineKeyboardButton('📁 By Folder', callback_data='files_by_folder')],
        [InlineKeyboardButton('🔄 Backup & Restore', callback_data='backup_restore_menu')],
        [InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='start')]
    ]

def build_backup_restore_buttons():
    """Build Backup & Restore menu buttons"""
    return [
        [InlineKeyboardButton('🔑 Generate Token', callback_data='generate_backup_token'), InlineKeyboardButton('📥 Restore', callback_data='restore_files')],
        [InlineKeyboardButton('🔗 Get Restore Link', callback_data='get_restore_link')],
        [InlineKeyboardButton('🔄 Change Token', callback_data='change_backup_token'), InlineKeyboardButton('🗑️ Delete Token', callback_data='delete_backup_token')],
        [InlineKeyboardButton('❌ Close', callback_data='close_data')]
    ]

@Client.on_message(filters.command("addcaption") & filters.private)
async def add_caption_cmd(client, message):
    """Add custom caption with template variables"""
    # Extract caption from message text after /addcaption command
    caption_text = message.text
    if caption_text.startswith("/addcaption "):
        caption = caption_text[12:]  # Remove '/addcaption ' prefix
    elif caption_text.startswith("/addcaption"):
        # No caption provided
        help_text = """<b>📝 CUSTOM CAPTION

➢ /addcaption [your caption]

AVAILABLE FILLINGS:
• {filename} - file name
• {filesize} - size of the media
• {duration} - duration of the media

EXAMPLE (copy this):
<code>📹 {filename}
🎬 Size: {filesize}
⏱️ Length: {duration}
📥 Download Now</code></b>"""
        await message.reply_text(help_text)
        return
    else:
        return
    
    # Save caption with line breaks preserved
    await db.set_caption(message.from_user.id, caption)
    await message.reply_text(f"<b>✅ Your caption successfully saved</b>")

@Client.on_message(filters.command("view_caption") & filters.private)
async def view_caption_cmd(client, message):
    """View current caption"""
    caption = await db.get_caption(message.from_user.id)
    if caption:
        # Escape HTML characters to preserve formatting and line breaks
        escaped_caption = (caption
            .replace("&", "&amp;")
            .replace("<", "&lt;")
            .replace(">", "&gt;")
            .replace('"', "&quot;")
        )
        await message.reply_text(f"<b>📝 Your Caption:</b>\n\n<code>{escaped_caption}</code>")
    else:
        await message.reply_text("<b>❌ No caption set</b>")

@Client.on_message(filters.command("del_caption") & filters.private)
async def del_caption_cmd(client, message):
    """Delete current caption"""
    caption = await db.get_caption(message.from_user.id)
    if caption:
        await db.delete_caption(message.from_user.id)
        await message.reply_text("<b>✅ Caption deleted successfully</b>")
    else:
        await message.reply_text("<b>❌ No caption set to delete</b>")

@Client.on_message(filters.command("deletecapline") & filters.private)
async def delete_cap_line(client, message):
    """Delete a specific line from caption by line number"""
    caption = await db.get_caption(message.from_user.id)
    if not caption:
        await message.reply_text("<b>❌ No caption set</b>")
        return
    
    if len(message.command) < 2:
        lines = caption.split('\n')
        text = "<b>📋 Caption Lines:\n\n</b>"
        for i, line in enumerate(lines, 1):
            text += f"{i}. {line}\n"
        text += f"\n<b>Usage: /deletecapline [line_number]</b>"
        await message.reply_text(text)
        return
    
    try:
        line_num = int(message.command[1])
        lines = caption.split('\n')
        if line_num < 1 or line_num > len(lines):
            await message.reply_text(f"<b>❌ Line {line_num} not found</b>")
            return
        lines.pop(line_num - 1)
        new_caption = '\n'.join(lines)
        await db.set_caption(message.from_user.id, new_caption)
        await message.reply_text(f"<b>✅ Line {line_num} deleted</b>")
    except ValueError:
        await message.reply_text("<b>❌ Invalid line number</b>")

@Client.on_message(filters.command("deleteword") & filters.private)
async def delete_word_cmd(client, message):
    """Delete a specific word from caption"""
    caption = await db.get_caption(message.from_user.id)
    if not caption:
        await message.reply_text("<b>❌ No caption set</b>")
        return
    
    if len(message.command) < 2:
        await message.reply_text("<b>Usage: /deleteword [word_to_delete]</b>")
        return
    
    word = " ".join(message.command[1:])
    new_caption = caption.replace(word, "")
    await db.set_caption(message.from_user.id, new_caption)
    await message.reply_text(f"<b>✅ Deleted: {word}</b>")

@Client.on_message(filters.command("addfilter") & filters.private)
async def add_filter_cmd(client, message):
    """Add word to remove from filenames"""
    if len(message.command) < 2:
        await message.reply_text("<b>📝 Filename Filter\n\nUsage: /addfilter [word_to_remove]\n\nExample:\n/addfilter Extracted By:\n/addfilter 🚩🇮🇳</b>")
        return
    
    word = " ".join(message.command[1:])
    await db.add_filename_filter(message.from_user.id, word)
    await message.reply_text(f"<b>✅ Filter added:\n\n{word}</b>")

@Client.on_message(filters.command("removefilter") & filters.private)
async def remove_filter_cmd(client, message):
    """Remove word filter"""
    filters_list = await db.get_filename_filters(message.from_user.id)
    if not filters_list:
        await message.reply_text("<b>❌ No filters set</b>")
        return
    
    if len(message.command) < 2:
        text = "<b>📋 Current Filters:\n\n</b>"
        for i, f in enumerate(filters_list, 1):
            text += f"{i}. {f}\n"
        text += f"\n<b>Usage: /removefilter [filter_number]</b>"
        await message.reply_text(text)
        return
    
    try:
        idx = int(message.command[1]) - 1
        if idx < 0 or idx >= len(filters_list):
            await message.reply_text(f"<b>❌ Invalid filter number</b>")
            return
        removed = filters_list[idx]
        await db.remove_filename_filter(message.from_user.id, removed)
        await message.reply_text(f"<b>✅ Removed:\n\n{removed}</b>")
    except ValueError:
        await message.reply_text("<b>❌ Invalid filter number</b>")

@Client.on_message(filters.command("showfilters") & filters.private)
async def show_filters_cmd(client, message):
    """Show all active filters"""
    filters_list = await db.get_filename_filters(message.from_user.id)
    if not filters_list:
        await message.reply_text("<b>📭 No filename filters active</b>")
        return
    
    text = "<b>📋 Active Filters:\n\n</b>"
    for i, f in enumerate(filters_list, 1):
        text += f"{i}. {f}\n"
    await message.reply_text(text)

@Client.on_message(filters.command("createfolder") & filters.private)
async def create_folder_cmd(client, message):
    """Create a new folder"""
    if len(message.command) < 2:
        await message.reply_text("<b>📁 Create Folder\n\nUsage: /createfolder [folder_name]</b>")
        return
    
    folder_name = " ".join(message.command[1:])
    
    # Prevent creating nested folders (no "/" allowed in folder name)
    if '/' in folder_name:
        await message.reply_text("<b>❌ Folder name cannot contain '/'. Only single-level folders are allowed here. Use the subfolder button inside a folder to create subfolders.</b>")
        return
    
    folders = await db.get_folders(message.from_user.id)
    
    # Check if folder already exists (handle both dict and string formats)
    for f in folders:
        fname = f.get('name', str(f)) if isinstance(f, dict) else str(f)
        if fname == folder_name:
            await message.reply_text(f"<b>❌ Folder '{folder_name}' already exists</b>")
            return
    
    await db.create_folder(message.from_user.id, folder_name)
    await message.reply_text(f"<b>✅ Folder created: {folder_name}</b>")

@Client.on_message(filters.command("listfolders") & filters.private)
async def list_folders_cmd(client, message):
    """List all folders"""
    folders = await db.get_folders(message.from_user.id)
    selected = await db.get_selected_folder(message.from_user.id)
    
    if not folders:
        await message.reply_text("<b>📭 No folders created yet</b>")
        return
    
    text = "<b>📁 Your Folders:\n\n</b>"
    for i, f in enumerate(folders, 1):
        marker = "✓" if f['name'] == selected else " "
        text += f"{i}. [{marker}] {f['name']}\n"
    await message.reply_text(text)

@Client.on_message(filters.command("deletefolder") & filters.private)
async def delete_folder_cmd(client, message):
    """Delete a folder"""
    if len(message.command) < 2:
        await message.reply_text("<b>📁 Delete Folder\n\nUsage: /deletefolder [folder_name]</b>")
        return
    
    folder_name = " ".join(message.command[1:])
    folders = await db.get_folders(message.from_user.id)
    
    # Check if folder exists (handle both dict and string formats)
    found = False
    for f in folders:
        fname = f.get('name', str(f)) if isinstance(f, dict) else str(f)
        if fname == folder_name:
            found = True
            break
    
    if not found:
        await message.reply_text(f"<b>❌ Folder '{folder_name}' not found</b>")
        return
    
    await db.delete_folder(message.from_user.id, folder_name)
    selected = await db.get_selected_folder(message.from_user.id)
    if selected == folder_name:
        await db.set_selected_folder(message.from_user.id, None)
    
    await message.reply_text(f"<b>✅ Folder deleted: {folder_name}</b>")

@Client.on_message(filters.command("renamefolder") & filters.private)
async def rename_folder_cmd(client, message):
    """Rename a folder"""
    if len(message.command) < 3:
        await message.reply_text("<b>📁 Rename Folder\n\nUsage: /renamefolder [old_name] [new_name]</b>")
        return
    
    old_name = message.command[1]
    new_name = " ".join(message.command[2:])
    
    if await db.rename_folder(message.from_user.id, old_name, new_name):
        await message.reply_text(f"<b>✅ Renamed: {old_name} → {new_name}</b>")
    else:
        await message.reply_text(f"<b>❌ Folder '{old_name}' not found</b>")

@Client.on_message(filters.command("settings") & filters.private)
async def settings_cmd(client, message):
    """Show user settings"""
    try:
        destinations = await db.get_destinations(message.from_user.id)
        delivery_mode = await db.get_delivery_mode(message.from_user.id)
        buttons, text = await build_settings_ui(destinations, delivery_mode, message.from_user.id)
        
        try:
            await message.reply_photo(
                photo=random.choice(PICS),
                caption=text,
                reply_markup=InlineKeyboardMarkup(buttons)
            )
        except:
            await message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
    except Exception as e:
        logger.error(f"Settings command error: {e}")
        await message.reply_text("<b>❌ Error loading settings</b>")

@Client.on_message(filters.command("start") & filters.incoming)
async def start(client, message):
    try:
        username = client.me.username
        if not await db.is_user_exist(message.from_user.id):
            await db.add_user(message.from_user.id, message.from_user.first_name)
            try:
                await client.send_message(LOG_CHANNEL, script.LOG_TEXT.format(message.from_user.id, message.from_user.mention))
            except:
                pass
        
        if len(message.command) != 2:
            # Use shared function for consistent buttons
            inline_buttons = build_start_buttons()
            inline_markup = InlineKeyboardMarkup(inline_buttons)
            reply_keyboard = build_reply_keyboard()
            
            me = client.me
            start_text = script.START_TXT.format(message.from_user.mention, me.mention)
            
            try:
                await message.reply_photo(
                    photo=random.choice(PICS),
                    caption=start_text,
                    reply_markup=inline_markup
                )
            except:
                await message.reply_text(
                    text=start_text,
                    reply_markup=inline_markup
                )
            
            # Send reply keyboard - keep the message so keyboard persists
            await message.reply_text("Use the buttons below to navigate 👇", reply_markup=reply_keyboard)
            return
    except Exception as e:
        logger.error(f"Start command error: {e}")
        await message.reply_text("<b>Error processing start command</b>")
        return

    
    data = message.command[1]
    
    # Check for verify command
    if data.split("-", 1)[0] == "verify":
        userid = data.split("-", 2)[1]
        token = data.split("-", 3)[2]
        if str(message.from_user.id) != str(userid):
            return await message.reply_text(
                text="<b>This link is for different user !!</b>",
                protect_content=True,
            )
        if await check_verification(client, userid):
            return await message.reply_text(
                text="<b>Already Verified !</b>",
                protect_content=True,
            )
        is_verify = await verify_user(client, userid, token)
        if is_verify:
            return await message.reply_text(
                text="<b>Successfully verified !</b>",
                protect_content=True,
            )
        else:
            return await message.reply_text(
                text="<b>Verification failed !</b>",
                protect_content=True,
            )
    
    # Check for restore link (restore_ENCODED_TOKEN format)
    if data.startswith("restore_"):
        encoded_token = data[8:]  # Remove "restore_" prefix
        try:
            # Decode the base64 encoded token
            restore_token = b64_decode(encoded_token, "utf-8")
            
            # Parse token to extract user_id
            token_user_id, random_part = await db.parse_backup_token(restore_token)
            
            if token_user_id is None:
                return await message.reply_text("<b>❌ Invalid restore link! Token format is invalid.</b>")
            
            # Find source user by token
            source_user = await db.get_user_by_backup_token(restore_token)
            
            if not source_user:
                return await message.reply_text("<b>❌ Invalid restore link! Token not found or expired.</b>")
            
            # Verify token ownership
            if source_user['id'] != token_user_id:
                return await message.reply_text("<b>❌ Token validation failed! This token is not valid.</b>")
            
            # Prevent self-restore
            if source_user['id'] == message.from_user.id:
                return await message.reply_text("<b>❌ You cannot restore to the same account!</b>")
            
            # Ensure the new user exists in database
            if not await db.is_user_exist(message.from_user.id):
                await db.add_user(message.from_user.id, message.from_user.first_name)
            
            # Transfer files
            success, file_count = await db.transfer_files_to_user(source_user['id'], message.from_user.id)
            
            if success:
                # Invalidate the token after successful transfer
                await db.invalidate_backup_token(source_user['id'])
                return await message.reply_text(f"<b>✅ Successfully restored {file_count} files to your account!\n\nUse /start to access your files.</b>")
            else:
                return await message.reply_text("<b>❌ No files found to restore from this token.</b>")
        except Exception as e:
            logger.error(f"Restore link error: {e}")
            return await message.reply_text("<b>❌ Error during restore. Please try again later.</b>")

    # Decode file link (base64 encoded file_INDEX format)
    is_batch = False
    msg_id = None
    file_index = None
    
    try:
        # Check for new token-based folder links (format: folder_TOKEN, not base64 encoded)
        if data.startswith("folder_"):
            token = data[7:]  # Remove "folder_" prefix to get the token
            
            # Validate token and get folder info
            folder_info = await db.validate_folder_token(token)
            
            if not folder_info:
                return await message.reply_text("<b>❌ Invalid or expired folder link!</b>")
            
            owner_id = folder_info['owner_id']
            folder_name = folder_info['folder_name']
            
            # Check if folder is password protected
            is_protected = await db.is_folder_password_protected(owner_id, folder_name)
            access_key = f"{message.from_user.id}_{owner_id}_{folder_name}"
            
            if is_protected and access_key not in VERIFIED_FOLDER_ACCESS:
                # Ask for password
                display_name = await db.get_folder_display_name(folder_name)
                encoded_folder = b64_encode(folder_name, "utf-8")
                CAPTION_INPUT_MODE[message.from_user.id] = f"verify_folder_password_{owner_id}_{encoded_folder}"
                prompt_msg = await message.reply_text(f"<b>🔐 This folder is password protected</b>\n\n📁 Folder: {display_name}\n\nPlease enter the password to access this folder:", parse_mode=enums.ParseMode.HTML)
                if message.from_user.id not in PASSWORD_PROMPT_MESSAGES:
                    PASSWORD_PROMPT_MESSAGES[message.from_user.id] = []
                PASSWORD_PROMPT_MESSAGES[message.from_user.id].append(prompt_msg.id)
                return
            
            # Get all files from this folder
            folder_files = await db.get_files_in_folder_recursive(owner_id, folder_name)
            
            if not folder_files:
                return await message.reply_text("<b>❌ This folder is empty or doesn't exist!</b>")
            
            # Show browsable folder UI
            display_name = await db.get_folder_display_name(folder_name)
            files_in_folder = await db.get_files_by_folder(owner_id, folder=folder_name)
            total_files_recursive = len(folder_files)
            
            text = f"<b>📁 Shared Folder: {display_name}\n📍 Path: {folder_name}\n\n</b>"
            text += f"📄 Files here: {len(files_in_folder)}\n📂 Total (incl. subfolders): {total_files_recursive}"
            
            buttons = []
            username = (await client.get_me()).username
            share_link = f"https://t.me/{username}?start=folder_{token}"
            
            # Get All Files button
            encoded_path = b64_encode(folder_name, "utf-8")
            buttons.append([InlineKeyboardButton('📥 Get All Files', callback_data=f'getall_shared_{owner_id}_{encoded_path}')])
            
            # Get subfolders
            subfolders = await db.get_subfolders(owner_id, folder_name)
            row = []
            for f in subfolders:
                sub_folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                sub_display = await db.get_folder_display_name(sub_folder_name)
                sub_encoded = b64_encode(sub_folder_name, "utf-8")
                sub_access_key = f"{message.from_user.id}_{owner_id}_{sub_folder_name}"
                is_sub_protected = await db.is_folder_password_protected(owner_id, sub_folder_name)
                
                if is_sub_protected and sub_access_key not in VERIFIED_FOLDER_ACCESS:
                    row.append(InlineKeyboardButton(f'🔒 {sub_display}', callback_data=f'shared_folder_{owner_id}_{sub_encoded}'))
                else:
                    files_in_sub = await db.get_files_in_folder_recursive(owner_id, sub_folder_name)
                    row.append(InlineKeyboardButton(f'📁 {sub_display} ({len(files_in_sub)})', callback_data=f'shared_folder_{owner_id}_{sub_encoded}'))
                
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)
            
            # List files with pagination
            if files_in_folder:
                items_per_page = 10
                page = 0
                total_pages = max(1, (len(files_in_folder) + items_per_page - 1) // items_per_page)
                start_idx = page * items_per_page
                end_idx = start_idx + items_per_page
                display_files = files_in_folder[start_idx:end_idx]
                
                text += f"\n\n<b>Files (Page {page + 1}/{total_pages}):</b>\n"
                for file_obj in display_files:
                    file_name = file_obj.get('file_name', 'Unknown')
                    if len(file_name) > 40:
                        file_name = file_name[:37] + "..."
                    file_id = file_obj.get('file_id')
                    if file_id:
                        file_link_data = f"sharedfile_{owner_id}_{file_id}"
                        encoded_file_link = b64_encode(file_link_data)
                        link = f"https://t.me/{username}?start={encoded_file_link}"
                        text += f"• <a href='{link}'>{file_name}</a>\n"
                    else:
                        text += f"• {file_name}\n"
                
                if total_pages > 1:
                    buttons.append([InlineKeyboardButton('Next ➡️', callback_data=f'sharedp:1:{owner_id}:{encoded_path}')])
            
            # Back button
            if '/' in folder_name:
                parent_path = '/'.join(folder_name.split('/')[:-1])
                parent_encoded = b64_encode(parent_path, "utf-8")
                buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data=f'shared_folder_{owner_id}_{parent_encoded}')])
            
            # Convert to raw API format
            encoded_path = b64_encode(folder_name, "utf-8")
            raw_buttons = [[{"text": "Copy folder link", "copy_text": {"text": share_link}}, {"text": "📋 Last 5", "callback_data": f"last5_shared_{owner_id}_{encoded_path}"}]]
            
            for row in buttons:
                raw_row = []
                for btn in row:
                    if btn.callback_data:
                        raw_row.append({"text": btn.text, "callback_data": btn.callback_data})
                    elif btn.url:
                        raw_row.append({"text": btn.text, "url": btn.url})
                if raw_row:
                    raw_buttons.append(raw_row)
            
            api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
            payload = {
                "chat_id": message.from_user.id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": {"inline_keyboard": raw_buttons}
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(api_url, json=payload) as resp:
                    result = await resp.json()
                    if not result.get("ok"):
                        logger.error(f"Send message error: {result.get('description')}")
            
            return
        
        # Check if it's a BATCH link (old format)
        if data.startswith("BATCH-"):
            # Remove BATCH- prefix and decode
            batch_data = data[6:]  # Remove "BATCH-" prefix
            decoded = b64_decode(batch_data)
            is_batch = True
            # For batch: decoded is "file_XXXX" where XXXX is the message ID of the batch JSON document
            if decoded.startswith("file_"):
                prefix, batch_msg_id = decoded.split('_', 1)
                msg_id = int(batch_msg_id)
            else:
                return await message.reply_text("<b>❌ Invalid batch link format!</b>")
        else:
            decoded = b64_decode(data)
            
            if decoded.startswith("folder_"):
                # Folder share link format: folder_{owner_user_id}_{encoded_folder_name}
                parts = decoded.split('_', 2)
                if len(parts) >= 3:
                    owner_id = int(parts[1])
                    encoded_folder = parts[2]
                    folder_name = b64_decode(encoded_folder, "utf-8")
                    
                    # Check if folder is password protected
                    is_protected = await db.is_folder_password_protected(owner_id, folder_name)
                    access_key = f"{message.from_user.id}_{owner_id}_{folder_name}"
                    
                    if is_protected and access_key not in VERIFIED_FOLDER_ACCESS:
                        # Ask for password
                        display_name = await db.get_folder_display_name(folder_name)
                        CAPTION_INPUT_MODE[message.from_user.id] = f"verify_folder_password_{owner_id}_{encoded_folder}"
                        prompt_msg = await message.reply_text(f"<b>🔐 This folder is password protected</b>\n\n📁 Folder: {display_name}\n\nPlease enter the password to access this folder:", parse_mode=enums.ParseMode.HTML)
                        if message.from_user.id not in PASSWORD_PROMPT_MESSAGES:
                            PASSWORD_PROMPT_MESSAGES[message.from_user.id] = []
                        PASSWORD_PROMPT_MESSAGES[message.from_user.id].append(prompt_msg.id)
                        return
                    
                    # Get all files from this folder
                    folder_files = await db.get_files_in_folder_recursive(owner_id, folder_name)
                    
                    if not folder_files:
                        return await message.reply_text("<b>❌ This folder is empty or doesn't exist!</b>")
                    
                    # Show browsable folder UI instead of sending all files
                    display_name = await db.get_folder_display_name(folder_name)
                    
                    # Get files in current folder (not subfolders)
                    files_in_folder = await db.get_files_by_folder(owner_id, folder=folder_name)
                    total_files_recursive = len(folder_files)
                    
                    text = f"<b>📁 Shared Folder: {display_name}\n📍 Path: {folder_name}\n\n</b>"
                    text += f"📄 Files here: {len(files_in_folder)}\n📂 Total (incl. subfolders): {total_files_recursive}"
                    
                    buttons = []
                    
                    # Generate share link for Copy folder link button
                    username = (await client.get_me()).username
                    link_data = f"folder_{owner_id}_{encoded_folder}"
                    encoded_link = b64_encode(link_data)
                    share_link = f"https://t.me/{username}?start={encoded_link}"
                    
                    # 1. Get All Files button
                    action_row = []
                    encoded_path = b64_encode(folder_name, "utf-8")
                    action_row.append(InlineKeyboardButton('📥 Get All Files', callback_data=f'getall_shared_{owner_id}_{encoded_path}'))
                    buttons.append(action_row)
                    
                    # 2. Get subfolders
                    subfolders = await db.get_subfolders(owner_id, folder_name)
                    
                    # List subfolders - 2 per row
                    row = []
                    for f in subfolders:
                        sub_folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                        sub_display = await db.get_folder_display_name(sub_folder_name)
                        sub_encoded = b64_encode(sub_folder_name, "utf-8")
                        
                        # Check if subfolder has its own password
                        sub_access_key = f"{message.from_user.id}_{owner_id}_{sub_folder_name}"
                        is_sub_protected = await db.is_folder_password_protected(owner_id, sub_folder_name)
                        
                        if is_sub_protected and sub_access_key not in VERIFIED_FOLDER_ACCESS:
                            # Show lock icon, hide file count for protected subfolders
                            row.append(InlineKeyboardButton(f'🔒 {sub_display}', callback_data=f'shared_folder_{owner_id}_{sub_encoded}'))
                        else:
                            files_in_sub = await db.get_files_in_folder_recursive(owner_id, sub_folder_name)
                            row.append(InlineKeyboardButton(f'📁 {sub_display} ({len(files_in_sub)})', callback_data=f'shared_folder_{owner_id}_{sub_encoded}'))
                        
                        if len(row) == 2:
                            buttons.append(row)
                            row = []
                    if row:
                        buttons.append(row)
                    
                    # 3. List files with pagination
                    if files_in_folder:
                        owner_user = await db.col.find_one({'id': int(owner_id)})
                        all_owner_files = owner_user.get('stored_files', []) if owner_user else []
                        
                        items_per_page = 10
                        page = 0
                        total_pages = max(1, (len(files_in_folder) + items_per_page - 1) // items_per_page)
                        start_idx = page * items_per_page
                        end_idx = start_idx + items_per_page
                        display_files = files_in_folder[start_idx:end_idx]
                        
                        text += f"\n\n<b>Files (Page {page + 1}/{total_pages}):</b>\n"
                        for file_obj in display_files:
                            file_name = file_obj.get('file_name', 'Unknown')
                            if len(file_name) > 40:
                                file_name = file_name[:37] + "..."
                            # Create a direct file link using file_id from LOG_CHANNEL
                            file_id = file_obj.get('file_id')
                            if file_id:
                                # Create shared file link
                                file_link_data = f"sharedfile_{owner_id}_{file_id}"
                                encoded_file_link = b64_encode(file_link_data)
                                link = f"https://t.me/{username}?start={encoded_file_link}"
                                text += f"• <a href='{link}'>{file_name}</a>\n"
                            else:
                                text += f"• {file_name}\n"
                        
                        # Add pagination buttons if needed
                        if total_pages > 1:
                            nav_row = []
                            nav_row.append(InlineKeyboardButton('Next ➡️', callback_data=f'sharedp:1:{owner_id}:{encoded_path}'))
                            buttons.append(nav_row)
                    
                    # Back button - go to parent folder if exists
                    if '/' in folder_name:
                        parent_path = '/'.join(folder_name.split('/')[:-1])
                        parent_encoded = b64_encode(parent_path, "utf-8")
                        buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data=f'shared_folder_{owner_id}_{parent_encoded}')])
                    
                    # Convert buttons to raw API format and add copy link buttons
                    raw_buttons = []
                    
                    # Add copy link buttons at the top using raw API with copy_text
                    encoded_path = b64_encode(folder_name, "utf-8")
                    raw_buttons.append([
                        {"text": "Copy folder link", "copy_text": {"text": share_link}}, 
                        {"text": "📋 Last 5", "callback_data": f"last5_shared_{owner_id}_{encoded_path}"}
                    ])
                    
                    # Convert Pyrogram buttons to raw API format
                    for row in buttons:
                        raw_row = []
                        for btn in row:
                            if btn.callback_data:
                                raw_row.append({"text": btn.text, "callback_data": btn.callback_data})
                            elif btn.url:
                                raw_row.append({"text": btn.text, "url": btn.url})
                        if raw_row:
                            raw_buttons.append(raw_row)
                    
                    # Use raw API to send message with copy link functionality
                    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
                    payload = {
                        "chat_id": message.from_user.id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                        "reply_markup": {
                            "inline_keyboard": raw_buttons
                        }
                    }
                    
                    async with aiohttp.ClientSession() as session:
                        async with session.post(api_url, json=payload) as resp:
                            result = await resp.json()
                            if not result.get("ok"):
                                logger.error(f"Send message error: {result.get('description')}")
                    
                    return
                else:
                    return await message.reply_text("<b>❌ Invalid folder link!</b>")
            
            elif decoded.startswith("ft_"):
                # File token link format: ft_{token}
                file_token = decoded[3:]  # Remove "ft_" prefix
                
                # Find file by token
                owner_id, file_idx, file_obj = await db.get_file_by_token(file_token)
                
                if owner_id is None or file_obj is None:
                    return await message.reply_text("<b>❌ Invalid or expired file link!</b>")
                
                file_id = file_obj.get('file_id')
                file_name = file_obj.get('file_name', 'File')
                is_protected = file_obj.get('protected', False)
                file_password = file_obj.get('password')
                
                # Check folder password protection (required for everyone including owner)
                file_folder = file_obj.get('folder', '')
                if file_folder:
                    # Check if file's folder or any parent folder is password protected
                    path_parts = file_folder.split('/')
                    for i in range(len(path_parts)):
                        check_path = '/'.join(path_parts[:i+1])
                        is_folder_protected = await db.is_folder_password_protected(owner_id, check_path)
                        folder_access_key = f"{message.from_user.id}_{owner_id}_{check_path}"
                        
                        if is_folder_protected and folder_access_key not in VERIFIED_FOLDER_ACCESS:
                            # Folder requires password
                            display_name = await db.get_folder_display_name(check_path)
                            encoded_folder = b64_encode(check_path, "utf-8")
                            CAPTION_INPUT_MODE[message.from_user.id] = f"verify_folder_password_{owner_id}_{encoded_folder}"
                            await message.reply_text(
                                f"<b>🔐 This file is in a password protected folder</b>\n\n"
                                f"📁 Folder: {display_name}\n\n"
                                f"Please enter the folder password to access this file:\n\n"
                                f"<i>Send /cancel to cancel</i>",
                                parse_mode=enums.ParseMode.HTML
                            )
                            return
                
                # Check individual file password (required for everyone via shared link)
                if file_password:
                    verify_key = f"file_{message.from_user.id}_{owner_id}_{file_idx}"
                    if not VERIFIED_FOLDER_ACCESS.get(verify_key, False):
                        # Prompt for password
                        CAPTION_INPUT_MODE[message.from_user.id] = f"verify_file_password_{owner_id}_{file_idx}"
                        await message.reply_text(
                            f"<b>🔒 This file is password protected</b>\n\n"
                            f"<b>📄 {file_name}</b>\n\n"
                            f"Please enter the password to access this file:\n\n"
                            f"<i>Send /cancel to cancel</i>",
                            parse_mode=enums.ParseMode.HTML
                        )
                        return
                
                # Send the file
                try:
                    msg = await client.get_messages(LOG_CHANNEL, int(file_id))
                    if msg and msg.media:
                        await msg.copy(message.chat.id, protect_content=is_protected)
                        return
                    else:
                        return await message.reply_text("<b>❌ File not found in storage!</b>")
                except Exception as e:
                    logger.error(f"Error sending token file: {e}")
                    return await message.reply_text("<b>❌ Error sending file!</b>")
            
            elif decoded.startswith("sharedfile_"):
                # Shared file link format: sharedfile_{owner_id}_{file_id}
                parts = decoded.split('_', 2)
                if len(parts) >= 3:
                    owner_id = int(parts[1])
                    file_id = parts[2]
                    
                    # Find file index in owner's stored_files to check password
                    user_data = await db.col.find_one({'id': int(owner_id)})
                    stored_files = user_data.get('stored_files', []) if user_data else []
                    file_idx = None
                    file_obj = None
                    for idx, f in enumerate(stored_files):
                        if str(f.get('file_id')) == str(file_id):
                            file_idx = idx
                            file_obj = f
                            break
                    
                    # Check folder password protection (required for everyone including owner)
                    if file_obj:
                        file_folder = file_obj.get('folder', '')
                        if file_folder:
                            # Check if file's folder or any parent folder is password protected
                            path_parts = file_folder.split('/')
                            for i in range(len(path_parts)):
                                check_path = '/'.join(path_parts[:i+1])
                                is_folder_protected = await db.is_folder_password_protected(owner_id, check_path)
                                folder_access_key = f"{message.from_user.id}_{owner_id}_{check_path}"
                                
                                if is_folder_protected and folder_access_key not in VERIFIED_FOLDER_ACCESS:
                                    # Folder requires password
                                    display_name = await db.get_folder_display_name(check_path)
                                    encoded_folder = b64_encode(check_path, "utf-8")
                                    CAPTION_INPUT_MODE[message.from_user.id] = f"verify_folder_password_{owner_id}_{encoded_folder}"
                                    await message.reply_text(
                                        f"<b>🔐 This file is in a password protected folder</b>\n\n"
                                        f"📁 Folder: {display_name}\n\n"
                                        f"Please enter the folder password to access this file:\n\n"
                                        f"<i>Send /cancel to cancel</i>",
                                        parse_mode=enums.ParseMode.HTML
                                    )
                                    return
                        
                        # Check individual file password (required for everyone via shared link)
                        if file_obj.get('password'):
                            verify_key = f"file_{message.from_user.id}_{owner_id}_{file_idx}"
                            if not VERIFIED_FOLDER_ACCESS.get(verify_key, False):
                                file_name = file_obj.get('file_name', 'File')
                                CAPTION_INPUT_MODE[message.from_user.id] = f"verify_file_password_{owner_id}_{file_idx}"
                                await message.reply_text(
                                    f"<b>🔒 This file is password protected</b>\n\n"
                                    f"<b>📄 {file_name}</b>\n\n"
                                    f"Please enter the password to access this file:\n\n"
                                    f"<i>Send /cancel to cancel</i>",
                                    parse_mode=enums.ParseMode.HTML
                                )
                                return
                    
                    is_protected = file_obj.get('protected', False) if file_obj else False
                    try:
                        msg = await client.get_messages(LOG_CHANNEL, int(file_id))
                        if msg and msg.media:
                            await msg.copy(message.chat.id, protect_content=is_protected)
                            return
                        else:
                            return await message.reply_text("<b>❌ File not found!</b>")
                    except Exception as e:
                        logger.error(f"Error sending shared file: {e}")
                        return await message.reply_text("<b>❌ Error sending file!</b>")
                else:
                    return await message.reply_text("<b>❌ Invalid file link!</b>")
            
            elif decoded.startswith("file_"):
                # Extract file index (numeric link format)
                prefix, idx = decoded.split('_', 1)
                try:
                    file_index = int(idx)
                except ValueError:
                    # Not a numeric index, treat as direct file_id
                    msg_id = idx
                    return
                
                # Get file from user's stored_files by index
                # This is user's own file - no password required for owner
                user = await db.col.find_one({'id': int(message.from_user.id)})
                stored_files = user.get('stored_files', []) if user else []
                
                if 0 <= file_index < len(stored_files):
                    msg_id = stored_files[file_index]['file_id']
                else:
                    return await message.reply_text("<b>❌ File not found!</b>")
            else:
                # Old format or direct file_id
                prefix, file_id = decoded.split('_', 1)
                msg_id = file_id
    except Exception as e:
        logger.error(f"Error decoding link: {e}")
        await message.reply_text(f"<b>❌ Error: Invalid link format</b>")
        return
    
    # Initialize msgs for batch processing
    msgs = []
    
    # If batch, fetch JSON from LOG_CHANNEL
    if is_batch:
        try:
            batch_doc = await client.get_messages(LOG_CHANNEL, int(msg_id))
            if batch_doc.document:
                json_file = await client.download_media(batch_doc)
                with open(json_file, 'r') as f:
                    msgs = json.load(f)
                try:
                    os.remove(json_file)
                except:
                    pass
            else:
                await message.reply_text("<b>❌ Batch data not found!</b>")
                return
        except Exception as e:
            logger.error(f"Error fetching batch document: {e}")
            await message.reply_text(f"<b>❌ Error loading batch: {str(e)[:50]}</b>")
            return
    else:
        pass
    
    # Process batch files if it's a batch
    if is_batch and msgs:
        try:
            delivery_mode = await db.get_delivery_mode(message.from_user.id)
            destinations = await db.get_destinations(message.from_user.id)
            success_count = 0
            is_protected_batch = False  # Batch files don't have individual protection status
            
            # Set stop flag for this batch
            BATCH_STOP_FLAGS[message.from_user.id] = False
            
            buttons = [[InlineKeyboardButton('⏹️ Stop', callback_data=f'stop_batch_{message.from_user.id}')]]
            sts = await message.reply_text("🔄 Processing batch files...", reply_markup=InlineKeyboardMarkup(buttons))
            
            for msg_item in msgs:
                # Check if user clicked stop
                if BATCH_STOP_FLAGS.get(message.from_user.id, False):
                    await sts.edit(f"⏹️ Batch stopped! Sent {success_count} files before stopping.")
                    BATCH_STOP_FLAGS.pop(message.from_user.id, None)
                    return
                
                try:
                    channel_id = int(msg_item.get("channel_id"))
                    msgid = int(msg_item.get("msg_id"))
                    
                    info = await client.get_messages(channel_id, msgid)
                    if info.media:
                        file = getattr(info, info.media.value)
                        title = await formate_file_name(file.file_name, message.from_user.id) if hasattr(file, 'file_name') and file.file_name else "File"
                        size = get_size(file.file_size) if hasattr(file, 'file_size') else "Unknown"
                        
                        # Get original caption from message (same as single file logic)
                        original_caption_link = getattr(info, 'caption', None)
                        if original_caption_link:
                            original_caption_link = original_caption_link.html if hasattr(original_caption_link, 'html') else str(original_caption_link)
                        
                        # Build caption with same logic as single file
                        if hasattr(file, 'file_name') and file.file_name:
                            f_caption = await build_file_caption(message.from_user.id, title, size, original_caption=original_caption_link, original_filename=file.file_name)
                        else:
                            # For media without file_name - apply filters to caption
                            f_caption = await db.get_caption(message.from_user.id) or original_caption_link or ""
                            if f_caption:
                                f_caption = await apply_text_filters(message.from_user.id, f_caption)
                        
                        # Use exact same logic as single file delivery
                        if delivery_mode == 'pm':
                            # PM only mode
                            await info.copy(chat_id=message.from_user.id, caption=f_caption if f_caption else None, protect_content=False)
                            success_count += 1
                        
                        elif delivery_mode == 'channel':
                            # Channel only mode - no PM
                            enabled_dests = [d for d in destinations if d.get('enabled', True)]
                            if enabled_dests:
                                for dest in enabled_dests:
                                    try:
                                        await info.copy(chat_id=dest['channel_id'], caption=f_caption if f_caption else None, protect_content=is_protected_batch, message_thread_id=dest.get('topic_id'))
                                        success_count += 1
                                    except Exception as e:
                                        logger.error(f"Error sending to destination: {e}")
                        
                        else:  # 'both' or default
                            # Send to both PM and destinations
                            await info.copy(chat_id=message.from_user.id, caption=f_caption if f_caption else None, protect_content=is_protected_batch)
                            success_count += 1
                            
                            if destinations:
                                enabled_dests = [d for d in destinations if d.get('enabled', True)]
                                for dest in enabled_dests:
                                    try:
                                        await info.copy(chat_id=dest['channel_id'], caption=f_caption if f_caption else None, protect_content=is_protected_batch, message_thread_id=dest.get('topic_id'))
                                        # Only count as success if file was actually sent to channel
                                        success_count += 1
                                    except Exception as e:
                                        logger.error(f"Error sending to destination: {e}")
                    
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logger.error(f"Error processing batch file: {e}")
                    continue
            
            await sts.edit(f"✅ Batch complete! Sent {success_count} files")
            BATCH_STOP_FLAGS.pop(message.from_user.id, None)
            return
        except Exception as e:
            logger.error(f"Batch processing error: {e}")
            await message.reply_text(f"❌ Error processing batch: {str(e)[:50]}")
            BATCH_STOP_FLAGS.pop(message.from_user.id, None)
            return
    
    # For single file links - msg_id contains the file message ID
    decode_file_id = msg_id
    if not await check_verification(client, message.from_user.id) and VERIFY_MODE == True:
        btn = [[
            InlineKeyboardButton("Verify", url=await get_token(client, message.from_user.id, f"https://telegram.me/{username}?start="))
        ],[
            InlineKeyboardButton("How To Open Link & Verify", url=VERIFY_TUTORIAL)
        ]]
        await message.reply_text(
            text="<b>You are not verified !\nKindly verify to continue !</b>",
            protect_content=True,
            reply_markup=InlineKeyboardMarkup(btn)
        )
        return
    try:
        msg = await client.get_messages(LOG_CHANNEL, int(decode_file_id))
        if msg.media:
            media = getattr(msg, msg.media.value)
            title = None
            size = None
            f_caption = ""
            reply_markup = None
            
            # Handle different media types - build caption with file name, size, and user caption
            original_caption_link = getattr(msg, 'caption', None)
            if original_caption_link:
                original_caption_link = original_caption_link.html if hasattr(original_caption_link, 'html') else str(original_caption_link)
            
            if hasattr(media, 'file_name') and media.file_name:
                title = await formate_file_name(media.file_name, message.from_user.id)
                size = get_size(media.file_size)
                f_caption = await build_file_caption(message.from_user.id, title, size, original_caption=original_caption_link, original_filename=media.file_name)
            else:
                # For photos and other media without file_name
                f_caption = await db.get_caption(message.from_user.id) or original_caption_link
            
            
            try:
                delivery_mode = await db.get_delivery_mode(message.from_user.id)
                destinations = await db.get_destinations(message.from_user.id)
                
                # Get file index for action buttons - only if file is in user's database
                user = await db.col.find_one({'id': int(message.from_user.id)})
                stored_files = user.get('stored_files', []) if user else []
                
                # Find the file index by matching the LOG_CHANNEL message ID (stored as integer)
                file_idx = None
                for idx, file_obj in enumerate(stored_files):
                    file_id_in_db = file_obj.get('file_id')
                    # Try to match as integer
                    try:
                        if int(file_id_in_db) == int(decode_file_id):
                            file_idx = idx
                            break
                    except (ValueError, TypeError):
                        pass
                
                # Create action buttons only if file index is found AND valid
                reply_markup = None
                is_protected = False
                if file_idx is not None and isinstance(file_idx, int) and 0 <= file_idx < len(stored_files):
                    is_protected = stored_files[file_idx].get('protected', False)
                    protect_btn = '🛡️✅ Protected' if is_protected else '🛡️❌ Protect'
                    
                    # Same UI as when coming back from change folder section
                    buttons = [
                        [InlineKeyboardButton('🔗 Share', callback_data=f'file_share_{file_idx}'), InlineKeyboardButton('📁 Change Folder', callback_data=f'change_file_folder_{file_idx}')],
                        [InlineKeyboardButton(protect_btn, callback_data=f'toggle_protected_{file_idx}'), InlineKeyboardButton('❌ Delete', callback_data=f'delete_file_{file_idx}')],
                        [InlineKeyboardButton('✖️ Close', callback_data=f'close_file_message')]
                    ]
                    reply_markup = InlineKeyboardMarkup(buttons)
                
                # Apply delivery mode settings
                if delivery_mode == 'pm':
                    # PM only mode
                    del_msg = await msg.copy(chat_id=message.from_user.id, caption=f_caption if f_caption else None, reply_markup=reply_markup, protect_content=is_protected)
                
                elif delivery_mode == 'channel':
                    # Channel only mode - no PM
                    enabled_dests = [d for d in destinations if d.get('enabled', True)]
                    if enabled_dests:
                        for dest in enabled_dests:
                            try:
                                await msg.copy(chat_id=dest['channel_id'], caption=f_caption if f_caption else None, protect_content=is_protected, message_thread_id=dest.get('topic_id'))
                            except Exception as e:
                                logger.error(f"Error sending to destination: {e}")
                    return
                
                else:  # 'both' or default
                    # Send to both PM and destinations
                    if not destinations:
                        del_msg = await msg.copy(chat_id=message.from_user.id, caption=f_caption if f_caption else None, reply_markup=reply_markup, protect_content=is_protected)
                    else:
                        enabled_dests = [d for d in destinations if d.get('enabled', True)]
                        
                        # Send to PM with action buttons
                        del_msg = await msg.copy(chat_id=message.from_user.id, caption=f_caption if f_caption else None, reply_markup=reply_markup, protect_content=is_protected)
                        
                        # Send to enabled destinations with filtered caption
                        for dest in enabled_dests:
                            try:
                                await msg.copy(chat_id=dest['channel_id'], caption=f_caption if f_caption else None, protect_content=is_protected, message_thread_id=dest.get('topic_id'))
                            except Exception as e:
                                logger.error(f"Error sending to destination: {e}")
                    
                    return
                
                if AUTO_DELETE_MODE == True and del_msg:
                    await asyncio.sleep(AUTO_DELETE_TIME)
                    try:
                        await del_msg.delete()
                    except:
                        pass
            except FloodWait as e:
                await asyncio.sleep(e.value)
                del_msg = await msg.copy(chat_id=message.from_user.id, caption=f_caption if f_caption else None, reply_markup=reply_markup, protect_content=is_protected)
                if AUTO_DELETE_MODE == True and del_msg:
                    await asyncio.sleep(AUTO_DELETE_TIME)
                    try:
                        await del_msg.delete()
                    except:
                        pass
    except Exception as e:
        logger.error(f"Error: {e}")
        await message.reply_text(f"<b>Error : {str(e)[:50]}</b>")

@Client.on_message(filters.private & filters.text & ~filters.command(["start", "clone", "deletecloned", "batch", "link", "addcaption", "settings", "view_caption", "del_caption"]))
async def handle_user_input(client, message):
    """Unified handler for caption input and t.me links"""
    try:
        # Check for keyboard button taps
        if message.text == "📂 My Files" or message.text == "📁 My Files":
            buttons = build_my_files_buttons()
            await message.reply_text("<b>📂 My Files\n\nChoose view:</b>", reply_markup=InlineKeyboardMarkup(buttons))
            return
        
        elif message.text == "📁 Folders":
            folders = await db.get_folders(message.from_user.id)
            if not folders:
                await message.reply_text("<b>📭 No folders created yet\n\nCreate one with: /createfolder [name]</b>")
                return
            
            text = "<b>📁 Your Folders:\n\n</b>"
            for i, f in enumerate(folders, 1):
                folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                text += f"{i}. {folder_name}\n"
            text += "\n<b>Commands:</b>\n/createfolder [name] - Create new\n/deletefolder [name] - Delete\n/renamefolder [old] [new] - Rename"
            await message.reply_text(text)
            return
        
        elif message.text == "🧐 Report Bug":
            # Enter report bug mode
            REPORT_BUG_MODE[message.from_user.id] = True
            report_keyboard = build_reply_keyboard(report_mode=True)
            await message.reply_text("<b>🧐 Report a Bug\n\nPlease describe the issue you encountered.\nType your message and send it:</b>", reply_markup=report_keyboard)
            return
        
        elif message.text == "💗 About Us":
            me2 = client.me.mention if client.me else (await client.get_me()).mention
            buttons = [[InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='start'), InlineKeyboardButton('❌ Close', callback_data='close_data')]]
            await message.reply_text(text=script.ABOUT_TXT.format(me2), reply_markup=InlineKeyboardMarkup(buttons))
            return
        
        elif message.text == "❌ Cancel":
            # Cancel report mode
            if message.from_user.id in REPORT_BUG_MODE:
                del REPORT_BUG_MODE[message.from_user.id]
            reply_keyboard = build_reply_keyboard()
            await message.reply_text("<b>Cancelled</b>", reply_markup=reply_keyboard)
            return
        
        elif message.text == "⚙️ Settings":
            destinations = await db.get_destinations(message.from_user.id)
            delivery_mode = await db.get_delivery_mode(message.from_user.id)
            buttons, text = await build_settings_ui(destinations, delivery_mode, message.from_user.id)
            
            try:
                await message.reply_photo(
                    photo=random.choice(PICS),
                    caption=text,
                    reply_markup=InlineKeyboardMarkup(buttons)
                )
            except:
                await message.reply_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            return
        
        # Check if user is in REPORT_BUG_MODE
        if message.from_user.id in REPORT_BUG_MODE:
            try:
                # Get the report text
                report_text = message.text.strip()
                user = message.from_user
                
                # Send report to LOG_CHANNEL
                report_msg = f"<b>🧐 Bug Report</b>\n\n"
                report_msg += f"<b>From:</b> {user.mention} (<code>{user.id}</code>)\n"
                report_msg += f"<b>Username:</b> @{user.username if user.username else 'N/A'}\n\n"
                report_msg += f"<b>Report:</b>\n{report_text}"
                
                await client.send_message(LOG_CHANNEL, report_msg)
                
                # Remove from report mode
                del REPORT_BUG_MODE[message.from_user.id]
                
                # Send confirmation and restore normal reply keyboard
                reply_keyboard = build_reply_keyboard()
                await message.reply_text("<b>✅ Thank you! Your bug report has been submitted.</b>", reply_markup=reply_keyboard)
                return
            except Exception as e:
                logger.error(f"Report bug error: {e}")
                if message.from_user.id in REPORT_BUG_MODE:
                    del REPORT_BUG_MODE[message.from_user.id]
                reply_keyboard = build_reply_keyboard()
                await message.reply_text("<b>❌ Failed to submit report. Please try again later.</b>", reply_markup=reply_keyboard)
                return
        
        # Check if user is in RESTORE_MODE (entering restore token)
        if message.from_user.id in RESTORE_MODE:
            try:
                token = message.text.strip()
                
                # Parse token to extract user_id
                token_user_id, random_part = await db.parse_backup_token(token)
                
                if token_user_id is None:
                    await message.reply_text("<b>❌ Invalid token format! Token should be in format: UserId:token</b>")
                    return
                
                # Find user by token
                source_user = await db.get_user_by_backup_token(token)
                
                if not source_user:
                    await message.reply_text("<b>❌ Invalid token! Token not found or expired.</b>")
                    return
                
                # Verify token ownership - only the token creator can authorize restore
                if source_user['id'] != token_user_id:
                    await message.reply_text("<b>❌ Token validation failed! This token is not valid.</b>")
                    del RESTORE_MODE[message.from_user.id]
                    return
                
                # Prevent self-restore
                if source_user['id'] == message.from_user.id:
                    await message.reply_text("<b>❌ You cannot restore to the same account!</b>")
                    del RESTORE_MODE[message.from_user.id]
                    return
                
                # Transfer files
                success, file_count = await db.transfer_files_to_user(source_user['id'], message.from_user.id)
                
                if success:
                    # Invalidate the token after successful transfer
                    await db.invalidate_backup_token(source_user['id'])
                    del RESTORE_MODE[message.from_user.id]
                    await message.reply_text(f"<b>✅ Successfully restored {file_count} files to your account!</b>")
                else:
                    await message.reply_text("<b>❌ No files found to restore or transfer failed.</b>")
                    del RESTORE_MODE[message.from_user.id]
                return
            except Exception as e:
                logger.error(f"Restore error: {e}")
                if message.from_user.id in RESTORE_MODE:
                    del RESTORE_MODE[message.from_user.id]
                await message.reply_text("<b>❌ Error during restore. Please try again.</b>")
                return
        
        # First: Check if user is in CAPTION_INPUT_MODE
        if message.from_user.id in CAPTION_INPUT_MODE:
            mode = CAPTION_INPUT_MODE[message.from_user.id]
            
            if mode == True:
                caption = message.text
                await db.set_caption(message.from_user.id, caption)
                CAPTION_INPUT_MODE[message.from_user.id] = False
                await message.reply_text(f"<b>✅ Caption saved</b>")
                return
            
            elif mode == "replace_words":
                lines = message.text.strip().split('\n')
                for line in lines:
                    if "|" in line:
                        await db.add_filename_filter(message.from_user.id, line.strip())
                
                replacements = await db.get_filename_filters(message.from_user.id)
                replace_list = [r for r in replacements if "|" in r]
                CAPTION_INPUT_MODE[message.from_user.id] = False
                if replace_list:
                    await message.reply_text(f"<b>✅ Filename replacements set:\n\n{chr(10).join(replace_list)}</b>")
                else:
                    await message.reply_text(f"<b>❌ No valid replacements added</b>")
                return
            
            elif mode == "remove_words":
                words = message.text.split()
                for word in words:
                    await db.add_filename_filter(message.from_user.id, word)
                CAPTION_INPUT_MODE[message.from_user.id] = False
                await message.reply_text(f"<b>✅ Remove words set: {', '.join(words)}</b>")
                return
            
            elif mode == "create_folder":
                try:
                    folder_name = message.text.strip()
                    
                    # Handle cancel
                    if folder_name == "❌ Cancel":
                        CAPTION_INPUT_MODE[message.from_user.id] = False
                        # Delete the cancel button message
                        try:
                            await message.delete()
                        except:
                            pass
                        # Delete the prompt message stored earlier
                        if message.from_user.id in FOLDER_PROMPT_MSG:
                            try:
                                await client.delete_messages(message.chat.id, FOLDER_PROMPT_MSG[message.from_user.id])
                                FOLDER_PROMPT_MSG.pop(message.from_user.id, None)
                            except:
                                pass
                        return
                    
                    if not folder_name:
                        await message.reply_text("<b>❌ Folder name cannot be empty</b>")
                        CAPTION_INPUT_MODE[message.from_user.id] = False
                        return
                    
                    folders = await db.get_folders(message.from_user.id)
                    
                    # Check for duplicate - handle both dict and string formats
                    for f in folders:
                        existing_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                        if existing_name == folder_name:
                            await message.reply_text(f"<b>❌ Folder '{folder_name}' already exists</b>")
                            CAPTION_INPUT_MODE[message.from_user.id] = False
                            return
                    
                    await db.create_folder(message.from_user.id, folder_name)
                    CAPTION_INPUT_MODE[message.from_user.id] = False
                    
                    # Delete the user's folder name message
                    try:
                        await message.delete()
                    except:
                        pass
                    
                    # Delete the prompt message
                    if message.from_user.id in FOLDER_PROMPT_MSG:
                        try:
                            await client.delete_messages(message.chat.id, FOLDER_PROMPT_MSG[message.from_user.id])
                            FOLDER_PROMPT_MSG.pop(message.from_user.id, None)
                        except:
                            pass
                    
                    await message.reply_text(f"<b>✅ Folder created: {folder_name}</b>")
                    logger.info(f"Folder '{folder_name}' created for user {message.from_user.id}")
                except Exception as e:
                    logger.error(f"Error creating folder: {e}")
                    await message.reply_text(f"<b>❌ Error creating folder: {str(e)[:50]}</b>")
                    CAPTION_INPUT_MODE[message.from_user.id] = False
                return
            
            elif isinstance(mode, str) and mode.startswith("rename_folder:"):
                try:
                    parts = mode.split(":", 2)
                    old_name = parts[2] if len(parts) > 2 else None
                    new_name = message.text.strip()
                    
                    if not old_name:
                        await message.reply_text("<b>❌ Error: Folder not found</b>")
                        CAPTION_INPUT_MODE[message.from_user.id] = False
                        return
                    
                    if not new_name:
                        await message.reply_text("<b>❌ Folder name cannot be empty</b>")
                        CAPTION_INPUT_MODE[message.from_user.id] = False
                        return
                    
                    # Prevent "/" in folder names
                    if "/" in new_name:
                        await message.reply_text("<b>❌ Folder name cannot contain '/'</b>")
                        CAPTION_INPUT_MODE[message.from_user.id] = False
                        return
                    
                    # Check for duplicate
                    folders = await db.get_folders(message.from_user.id)
                    for f in folders:
                        existing_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                        if existing_name == new_name:
                            await message.reply_text(f"<b>❌ Folder '{new_name}' already exists</b>")
                            CAPTION_INPUT_MODE[message.from_user.id] = False
                            return
                    
                    if await db.rename_folder(message.from_user.id, old_name, new_name):
                        CAPTION_INPUT_MODE[message.from_user.id] = False
                        
                        # Delete the user's message
                        try:
                            await message.delete()
                        except:
                            pass
                        
                        # Delete the prompt message
                        if message.from_user.id in FOLDER_PROMPT_MSG:
                            try:
                                await client.delete_messages(message.chat.id, FOLDER_PROMPT_MSG[message.from_user.id])
                                FOLDER_PROMPT_MSG.pop(message.from_user.id, None)
                            except:
                                pass
                        
                        await message.reply_text(f"<b>✅ Folder renamed: {old_name} → {new_name}</b>")
                    else:
                        await message.reply_text(f"<b>❌ Error renaming folder</b>")
                        CAPTION_INPUT_MODE[message.from_user.id] = False
                except Exception as e:
                    logger.error(f"Error renaming folder: {e}")
                    await message.reply_text(f"<b>❌ Error renaming folder: {str(e)[:50]}</b>")
                    CAPTION_INPUT_MODE[message.from_user.id] = False
                return
            
            elif mode.startswith("create_subfolder_"):
                try:
                    parent_folder = mode[17:]  # Remove "create_subfolder_" prefix
                    subfolder_name = message.text.strip()
                    
                    # Handle cancel
                    if subfolder_name == "❌ Cancel":
                        CAPTION_INPUT_MODE[message.from_user.id] = False
                        try:
                            await message.delete()
                        except:
                            pass
                        if message.from_user.id in FOLDER_PROMPT_MSG:
                            try:
                                await client.delete_messages(message.chat.id, FOLDER_PROMPT_MSG[message.from_user.id])
                                FOLDER_PROMPT_MSG.pop(message.from_user.id, None)
                            except:
                                pass
                        return
                    
                    if not subfolder_name:
                        await message.reply_text("<b>Error: Subfolder name cannot be empty</b>")
                        CAPTION_INPUT_MODE[message.from_user.id] = False
                        return
                    
                    # Check for invalid characters
                    if '/' in subfolder_name:
                        await message.reply_text("<b>Error: Subfolder name cannot contain '/'</b>")
                        CAPTION_INPUT_MODE[message.from_user.id] = False
                        return
                    
                    # Prevent creating subfolders of subfolders (only allow one level deep)
                    if '/' in parent_folder:
                        await message.reply_text("<b>Error: Cannot create subfolders inside subfolders. Only one level of subfolders is allowed.</b>")
                        CAPTION_INPUT_MODE[message.from_user.id] = False
                        return
                    
                    # Build full path
                    full_path = f"{parent_folder}/{subfolder_name}"
                    
                    # Check for duplicate
                    folders = await db.get_folders(message.from_user.id)
                    for f in folders:
                        existing_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                        if existing_name == full_path:
                            await message.reply_text(f"<b>Error: Subfolder '{subfolder_name}' already exists in this folder</b>")
                            CAPTION_INPUT_MODE[message.from_user.id] = False
                            return
                    
                    await db.create_folder(message.from_user.id, subfolder_name, parent_folder=parent_folder)
                    CAPTION_INPUT_MODE[message.from_user.id] = False
                    
                    try:
                        await message.delete()
                    except:
                        pass
                    
                    if message.from_user.id in FOLDER_PROMPT_MSG:
                        try:
                            await client.delete_messages(message.chat.id, FOLDER_PROMPT_MSG[message.from_user.id])
                            FOLDER_PROMPT_MSG.pop(message.from_user.id, None)
                        except:
                            pass
                    
                    await message.reply_text(f"<b>Subfolder created: {full_path}</b>")
                    logger.info(f"Subfolder '{full_path}' created for user {message.from_user.id}")
                except Exception as e:
                    logger.error(f"Error creating subfolder: {e}")
                    await message.reply_text(f"<b>Error creating subfolder: {str(e)[:50]}</b>")
                    CAPTION_INPUT_MODE[message.from_user.id] = False
                return
            
            elif mode.startswith("rename_folder_idx_"):
                try:
                    idx = int(mode.split("_")[-1])
                    new_name_input = message.text.strip()
                    
                    if not new_name_input:
                        await message.reply_text("<b>❌ Folder name cannot be empty</b>")
                        CAPTION_INPUT_MODE[message.from_user.id] = False
                        return
                    
                    folders = await db.get_folders(message.from_user.id)
                    if not (0 <= idx < len(folders)):
                        await message.reply_text("<b>❌ Folder not found</b>")
                        CAPTION_INPUT_MODE[message.from_user.id] = False
                        return
                    
                    f = folders[idx]
                    old_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                    
                    # Determine the new full path
                    if '/' in new_name_input:
                        # User provided a path like "folder1/iii" - move to that location
                        new_name = new_name_input
                    elif '/' in old_name:
                        # Subfolder: keep in same parent, just rename the last part
                        parent_path = '/'.join(old_name.split('/')[:-1])
                        new_name = f"{parent_path}/{new_name_input}"
                    else:
                        # Root folder: just use the new name
                        new_name = new_name_input
                    
                    if new_name == old_name:
                        await message.reply_text(f"<b>ℹ️ Same name, no change</b>")
                        CAPTION_INPUT_MODE[message.from_user.id] = False
                        return
                    
                    # Check if new name already exists
                    for existing_f in folders:
                        existing_name = existing_f.get('name', str(existing_f)) if isinstance(existing_f, dict) else str(existing_f)
                        if existing_name == new_name:
                            await message.reply_text(f"<b>❌ Folder '{new_name}' already exists</b>")
                            CAPTION_INPUT_MODE[message.from_user.id] = False
                            return
                    
                    await db.rename_folder(message.from_user.id, old_name, new_name)
                    CAPTION_INPUT_MODE[message.from_user.id] = False
                    await message.reply_text(f"<b>✅ Folder renamed:\n'{old_name}' → '{new_name}'</b>")
                    logger.info(f"Folder '{old_name}' renamed to '{new_name}' for user {message.from_user.id}")
                except Exception as e:
                    logger.error(f"Error renaming folder: {e}")
                    await message.reply_text(f"<b>❌ Error: {str(e)[:50]}</b>")
                    CAPTION_INPUT_MODE[message.from_user.id] = False
                return
            
            elif mode.startswith("set_folder_password_idx_"):
                try:
                    idx = int(mode.split("_")[-1])
                    password = message.text.strip()
                    
                    if not password:
                        await message.reply_text("<b>❌ Password cannot be empty</b>")
                        CAPTION_INPUT_MODE[message.from_user.id] = False
                        return
                    
                    folders = await db.get_folders(message.from_user.id)
                    if not (0 <= idx < len(folders)):
                        await message.reply_text("<b>❌ Folder not found</b>")
                        CAPTION_INPUT_MODE[message.from_user.id] = False
                        return
                    
                    f = folders[idx]
                    folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                    display_name = await db.get_folder_display_name(folder_name)
                    
                    await db.set_folder_password(message.from_user.id, folder_name, password)
                    CAPTION_INPUT_MODE[message.from_user.id] = False
                    
                    try:
                        await message.delete()
                    except:
                        pass
                    
                    await message.reply_text(f"<b>🔐 Password set for folder: {display_name}</b>\n\nAnyone accessing this folder via share link will need to enter this password.")
                    logger.info(f"Password set for folder '{folder_name}' for user {message.from_user.id}")
                except Exception as e:
                    logger.error(f"Error setting folder password: {e}")
                    await message.reply_text(f"<b>❌ Error: {str(e)[:50]}</b>")
                    CAPTION_INPUT_MODE[message.from_user.id] = False
                return
            
            elif mode.startswith("set_file_password_idx_"):
                try:
                    idx = int(mode.split("_")[-1])
                    password = message.text.strip()
                    
                    if not password:
                        await message.reply_text("<b>❌ Password cannot be empty</b>")
                        CAPTION_INPUT_MODE[message.from_user.id] = False
                        return
                    
                    if len(password) < 2 or len(password) > 8:
                        await message.reply_text("<b>❌ Password must be 2-8 characters</b>")
                        CAPTION_INPUT_MODE[message.from_user.id] = False
                        return
                    
                    user = await db.col.find_one({'id': int(message.from_user.id)})
                    stored_files = user.get('stored_files', []) if user else []
                    
                    if not (0 <= idx < len(stored_files)):
                        await message.reply_text("<b>❌ File not found</b>")
                        CAPTION_INPUT_MODE[message.from_user.id] = False
                        return
                    
                    file_name = stored_files[idx].get('file_name', 'File')
                    
                    await db.set_file_password(message.from_user.id, idx, password)
                    CAPTION_INPUT_MODE[message.from_user.id] = False
                    
                    try:
                        await message.delete()
                    except:
                        pass
                    
                    await message.reply_text(f"<b>🔐 Password set for file: {file_name}</b>\n\nAnyone accessing this file via share link will need to enter this password.")
                    logger.info(f"Password set for file index {idx} for user {message.from_user.id}")
                except Exception as e:
                    logger.error(f"Error setting file password: {e}")
                    await message.reply_text(f"<b>❌ Error: {str(e)[:50]}</b>")
                    CAPTION_INPUT_MODE[message.from_user.id] = False
                return
            
            elif mode.startswith("verify_file_password_"):
                try:
                    parts = mode.replace("verify_file_password_", "").split("_")
                    owner_id = int(parts[0])
                    file_idx = int(parts[1])
                    
                    password = message.text.strip()
                    
                    # Track password attempts (max 2)
                    attempt_key = f"file_{message.from_user.id}_{owner_id}_{file_idx}"
                    attempts = PASSWORD_ATTEMPTS.get(attempt_key, 0) + 1
                    PASSWORD_ATTEMPTS[attempt_key] = attempts
                    
                    is_valid = await db.verify_file_password(owner_id, file_idx, password)
                    
                    if is_valid:
                        CAPTION_INPUT_MODE[message.from_user.id] = False
                        PASSWORD_ATTEMPTS.pop(attempt_key, None)
                        
                        try:
                            await message.delete()
                        except:
                            pass
                        
                        VERIFIED_FOLDER_ACCESS[f"file_{message.from_user.id}_{owner_id}_{file_idx}"] = True
                        
                        # Get file and send it
                        user_data = await db.col.find_one({'id': int(owner_id)})
                        stored_files = user_data.get('stored_files', []) if user_data else []
                        
                        if 0 <= file_idx < len(stored_files):
                            file_obj = stored_files[file_idx]
                            file_id = file_obj.get('file_id')
                            file_name = file_obj.get('file_name', 'File')
                            is_protected = file_obj.get('protected', False)
                            
                            try:
                                msg = await client.get_messages(LOG_CHANNEL, int(file_id))
                                if msg:
                                    await msg.copy(chat_id=message.from_user.id, protect_content=is_protected)
                                else:
                                    await message.reply_text("<b>❌ File not found in storage</b>")
                            except Exception as e:
                                logger.error(f"Error sending file after password: {e}")
                                await message.reply_text(f"<b>❌ Error retrieving file</b>")
                        else:
                            await message.reply_text("<b>❌ File not found</b>")
                    else:
                        try:
                            await message.delete()
                        except:
                            pass
                        
                        if attempts >= 2:
                            await message.reply_text("<b>❌ Too many wrong attempts. Access denied.</b>")
                            CAPTION_INPUT_MODE[message.from_user.id] = False
                            PASSWORD_ATTEMPTS.pop(attempt_key, None)
                        else:
                            await message.reply_text(f"<b>❌ Wrong password. Attempt {attempts}/2</b>\n\nPlease try again or send /cancel to cancel.")
                except Exception as e:
                    logger.error(f"Error verifying file password: {e}")
                    await message.reply_text(f"<b>❌ Error: {str(e)[:50]}</b>")
                    CAPTION_INPUT_MODE[message.from_user.id] = False
                return
            
            elif mode.startswith("verify_folder_password_"):
                try:
                    parts = mode.replace("verify_folder_password_", "").split("_")
                    owner_id = int(parts[0])
                    encoded_folder = parts[1]
                    
                    password = message.text.strip()
                    folder_name = b64_decode(encoded_folder, "utf-8")
                    
                    # Track password attempts (max 2)
                    attempt_key = f"{message.from_user.id}_{owner_id}_{folder_name}"
                    attempts = PASSWORD_ATTEMPTS.get(attempt_key, 0) + 1
                    PASSWORD_ATTEMPTS[attempt_key] = attempts
                    
                    is_valid = await db.verify_folder_password(owner_id, folder_name, password)
                    
                    if is_valid:
                        CAPTION_INPUT_MODE[message.from_user.id] = False
                        PASSWORD_ATTEMPTS.pop(attempt_key, None)
                        
                        # Delete all prompt and response messages
                        if message.from_user.id in PASSWORD_PROMPT_MESSAGES:
                            for msg_id in PASSWORD_PROMPT_MESSAGES[message.from_user.id]:
                                try:
                                    await client.delete_messages(message.from_user.id, msg_id)
                                except:
                                    pass
                            PASSWORD_PROMPT_MESSAGES.pop(message.from_user.id, None)
                        
                        if message.from_user.id in PASSWORD_RESPONSE_MESSAGES:
                            for msg_id in PASSWORD_RESPONSE_MESSAGES[message.from_user.id]:
                                try:
                                    await client.delete_messages(message.from_user.id, msg_id)
                                except:
                                    pass
                            PASSWORD_RESPONSE_MESSAGES.pop(message.from_user.id, None)
                        
                        try:
                            await message.delete()
                        except:
                            pass
                        
                        VERIFIED_FOLDER_ACCESS[f"{message.from_user.id}_{owner_id}_{folder_name}"] = True
                        
                        # Get all files from this folder (including subfolders)
                        all_folder_files = await db.get_files_in_folder_recursive(owner_id, folder_name)
                        
                        if not all_folder_files:
                            await message.reply_text("<b>❌ This folder is empty!</b>")
                            return
                        
                        # Show browsable folder UI
                        display_name = await db.get_folder_display_name(folder_name)
                        
                        # Get files in current folder (not subfolders)
                        files_in_folder = await db.get_files_by_folder(owner_id, folder=folder_name)
                        total_files_recursive = len(all_folder_files)
                        
                        text = f"<b>📁 Shared Folder: {display_name}\n📍 Path: {folder_name}\n\n</b>"
                        text += f"📄 Files here: {len(files_in_folder)}\n📂 Total (incl. subfolders): {total_files_recursive}"
                        
                        buttons = []
                        
                        # Generate share link for Copy folder link button
                        username = (await client.get_me()).username
                        link_data = f"folder_{owner_id}_{encoded_folder}"
                        encoded_link = b64_encode(link_data)
                        share_link = f"https://t.me/{username}?start={encoded_link}"
                        
                        # 1. Get All Files button
                        action_row = []
                        encoded_path = b64_encode(folder_name, "utf-8")
                        action_row.append(InlineKeyboardButton('📥 Get All Files', callback_data=f'getall_shared_{owner_id}_{encoded_path}'))
                        buttons.append(action_row)
                        
                        # 2. Get subfolders
                        subfolders = await db.get_subfolders(owner_id, folder_name)
                        
                        # List subfolders - 2 per row
                        row = []
                        for f in subfolders:
                            sub_folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                            sub_display = await db.get_folder_display_name(sub_folder_name)
                            sub_encoded = b64_encode(sub_folder_name, "utf-8")
                            
                            # Check if subfolder has its own password
                            sub_access_key = f"{message.from_user.id}_{owner_id}_{sub_folder_name}"
                            is_sub_protected = await db.is_folder_password_protected(owner_id, sub_folder_name)
                            
                            if is_sub_protected and sub_access_key not in VERIFIED_FOLDER_ACCESS:
                                # Show lock icon, hide file count for protected subfolders
                                row.append(InlineKeyboardButton(f'🔒 {sub_display}', callback_data=f'shared_folder_{owner_id}_{sub_encoded}'))
                            else:
                                files_in_sub = await db.get_files_in_folder_recursive(owner_id, sub_folder_name)
                                row.append(InlineKeyboardButton(f'📁 {sub_display} ({len(files_in_sub)})', callback_data=f'shared_folder_{owner_id}_{sub_encoded}'))
                            
                            if len(row) == 2:
                                buttons.append(row)
                                row = []
                        if row:
                            buttons.append(row)
                        
                        # 3. List files with pagination
                        if files_in_folder:
                            items_per_page = 10
                            page = 0
                            total_pages = max(1, (len(files_in_folder) + items_per_page - 1) // items_per_page)
                            start_idx = page * items_per_page
                            end_idx = start_idx + items_per_page
                            display_files = files_in_folder[start_idx:end_idx]
                            
                            text += f"\n\n<b>Files (Page {page + 1}/{total_pages}):</b>\n"
                            for file_obj in display_files:
                                file_name = file_obj.get('file_name', 'Unknown')
                                if len(file_name) > 40:
                                    file_name = file_name[:37] + "..."
                                file_id = file_obj.get('file_id')
                                if file_id:
                                    # Create shared file link
                                    file_link_data = f"sharedfile_{owner_id}_{file_id}"
                                    encoded_file_link = b64_encode(file_link_data)
                                    link = f"https://t.me/{username}?start={encoded_file_link}"
                                    text += f"• <a href='{link}'>{file_name}</a>\n"
                                else:
                                    text += f"• {file_name}\n"
                            
                            # Add pagination buttons if needed
                            if total_pages > 1:
                                nav_row = []
                                nav_row.append(InlineKeyboardButton('Next ➡️', callback_data=f'sharedp:1:{owner_id}:{encoded_path}'))
                                buttons.append(nav_row)
                        
                        # Back button - go to parent folder if exists
                        if '/' in folder_name:
                            parent_path = '/'.join(folder_name.split('/')[:-1])
                            parent_encoded = b64_encode(parent_path, "utf-8")
                            buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data=f'shared_folder_{owner_id}_{parent_encoded}')])
                        
                        # Convert buttons to raw API format and add copy link buttons
                        raw_buttons = []
                        
                        # Add copy link buttons at the top using raw API with copy_text
                        raw_buttons.append([
                            {"text": "Copy folder link", "copy_text": {"text": share_link}}, 
                            {"text": "📋 Last 5", "callback_data": f"last5_shared_{owner_id}_{encoded_path}"}
                        ])
                        
                        # Convert Pyrogram buttons to raw API format
                        for row in buttons:
                            raw_row = []
                            for btn in row:
                                if btn.callback_data:
                                    raw_row.append({"text": btn.text, "callback_data": btn.callback_data})
                                elif btn.url:
                                    raw_row.append({"text": btn.text, "url": btn.url})
                            if raw_row:
                                raw_buttons.append(raw_row)
                        
                        # Use raw API to send message with copy link functionality
                        api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage"
                        payload = {
                            "chat_id": message.from_user.id,
                            "text": text,
                            "parse_mode": "HTML",
                            "disable_web_page_preview": True,
                            "reply_markup": {
                                "inline_keyboard": raw_buttons
                            }
                        }
                        
                        async with aiohttp.ClientSession() as session:
                            async with session.post(api_url, json=payload) as resp:
                                result = await resp.json()
                                if not result.get("ok"):
                                    logger.error(f"Send message error: {result.get('description')}")
                    else:
                        # Delete the password input message
                        try:
                            await message.delete()
                        except:
                            pass
                        
                        # Check if attempts exceeded
                        if attempts >= 2:
                            CAPTION_INPUT_MODE[message.from_user.id] = False
                            PASSWORD_ATTEMPTS.pop(attempt_key, None)
                            # Delete all prompt messages
                            if message.from_user.id in PASSWORD_PROMPT_MESSAGES:
                                for msg_id in PASSWORD_PROMPT_MESSAGES[message.from_user.id]:
                                    try:
                                        await client.delete_messages(message.from_user.id, msg_id)
                                    except:
                                        pass
                                PASSWORD_PROMPT_MESSAGES.pop(message.from_user.id, None)
                            if message.from_user.id in PASSWORD_RESPONSE_MESSAGES:
                                for msg_id in PASSWORD_RESPONSE_MESSAGES[message.from_user.id]:
                                    try:
                                        await client.delete_messages(message.from_user.id, msg_id)
                                    except:
                                        pass
                                PASSWORD_RESPONSE_MESSAGES.pop(message.from_user.id, None)
                        else:
                            remaining = 2 - attempts
                            # Store response message ID to delete later
                            resp_msg = await client.send_message(message.from_user.id, f"<b>❌ Wrong password. {remaining} attempt{'s' if remaining > 1 else ''} left.\n\nTry again:</b>", parse_mode=enums.ParseMode.HTML)
                            if message.from_user.id not in PASSWORD_RESPONSE_MESSAGES:
                                PASSWORD_RESPONSE_MESSAGES[message.from_user.id] = []
                            PASSWORD_RESPONSE_MESSAGES[message.from_user.id].append(resp_msg.id)
                except Exception as e:
                    logger.error(f"Error verifying folder password: {e}")
                    await message.reply_text(f"<b>❌ Error: {str(e)[:50]}</b>")
                    CAPTION_INPUT_MODE[message.from_user.id] = False
                    PASSWORD_ATTEMPTS.pop(attempt_key, None)
                return
        
        # Second: Check if message contains t.me link
        if 't.me' not in message.text:
            return
        
        logger.info(f"Processing t.me link from {message.from_user.id}: {message.text}")
        
        # Parse t.me links like https://t.me/username/msg_id or https://t.me/c/channel_id/msg_id
        channel_match = re.search(r'https://t\.me/c/([-\d]+)(?:/(\d+))?', message.text)
        username_match = re.search(r'https://t\.me/([a-zA-Z0-9_]+)/(\d+)', message.text)
        
        logger.info(f"Channel match: {channel_match}, Username match: {username_match}")
        
        match = channel_match or username_match
        if not match:
            logger.warning(f"No valid t.me link format found in: {message.text}")
            return
        
        logger.info(f"Valid t.me link found, fetching post...")
        sts = await message.reply_text("🔄 Fetching post...")
        
        try:
            chat_id = None
            msg_id = None
            
            if channel_match:  # Channel format: https://t.me/c/channel_id/msg_id
                channel_id = channel_match.group(1)
                msg_id = channel_match.group(2)
                
                if not msg_id:
                    await sts.edit("❌ Invalid link format. Use: https://t.me/c/CHANNEL_ID/MSG_ID")
                    return
                
                # Handle both positive and negative channel IDs
                if channel_id.startswith('-'):
                    chat_id = int(channel_id)
                else:
                    chat_id = int(f"-100{channel_id}")
                msg_id = int(msg_id)
            
            elif username_match:  # Username format: https://t.me/username/msg_id
                username = username_match.group(1)
                msg_id = int(username_match.group(2))
                # Get chat from username
                try:
                    chat = await client.get_chat(username)
                    chat_id = chat.id
                except Exception as e:
                    logger.error(f"Could not find chat {username}: {e}")
                    await sts.edit("❌ Could not find chat/channel")
                    return
            
            # Get the message
            try:
                post_msg = await client.get_messages(chat_id, msg_id)
            except Exception as e:
                logger.error(f"Could not fetch message {chat_id}/{msg_id}: {e}")
                await sts.edit(f"❌ Could not fetch post: {str(e)[:50]}")
                return
            
            if not post_msg:
                await sts.edit("❌ Post not found")
                return
            
            # Extract filename and apply filters for caption building
            f_caption = ""
            original_caption = getattr(post_msg, 'caption', None)
            if original_caption:
                original_caption = original_caption.html if hasattr(original_caption, 'html') else str(original_caption)
            
            if post_msg.media:
                media = getattr(post_msg, post_msg.media.value)
                if hasattr(media, 'file_name') and media.file_name:
                    raw_filename = media.file_name
                    formatted_filename = await formate_file_name(raw_filename, message.from_user.id)
                    size = get_size(media.file_size)
                    f_caption = await build_file_caption(message.from_user.id, formatted_filename, size, original_caption=original_caption, original_filename=raw_filename)
                else:
                    f_caption = await db.get_caption(message.from_user.id) or original_caption
            
            # Get delivery settings
            delivery_mode = await db.get_delivery_mode(message.from_user.id)
            destinations = await db.get_destinations(message.from_user.id)
            
            success = False
            
            # Send to PM if mode is 'pm' or 'both'
            if delivery_mode in ['pm', 'both']:
                try:
                    await post_msg.copy(chat_id=message.from_user.id, caption=f_caption if f_caption else None, protect_content=False)
                    success = True
                except Exception as e:
                    logger.error(f"Error sending to PM: {e}")
            
            # Send to enabled destinations if mode is 'channel' or 'both'
            if delivery_mode in ['channel', 'both'] and destinations:
                for dest in destinations:
                    if dest.get('enabled', True):
                        try:
                            await post_msg.copy(chat_id=dest['channel_id'], caption=f_caption if f_caption else None, protect_content=False, message_thread_id=dest.get('topic_id'))
                            success = True
                        except Exception as e:
                            logger.error(f"Error sending to destination {dest['channel_id']}: {e}")
            
            if success:
                await sts.edit("✅ Post sent successfully!")
            else:
                await sts.edit("❌ No delivery configured or failed to send")
            
        except Exception as e:
            logger.error(f"Error fetching/sending post: {e}")
            await sts.edit(f"❌ Error: {str(e)[:80]}")
            return
    
    except Exception as e:
        logger.error(f"Error in handle_tme_link: {e}")

@Client.on_callback_query()
async def callback(client, query):
    try:
        # Answer callback immediately for faster response (unless specific handlers need custom answers)
        if not query.data.startswith(("stop_batch_", "toggle_clone", "remove_dest_", "toggle_dest_enable_", "sel_folder_", "confirm_del_", "select_topic_", "del_replace_", "del_remove_", "mode_", "file_share_", "change_file_folder_", "view_folder_password_", "confirm_remove_password_", "remove_folder_password_", "confirm_change_link_", "cancel_change_link_", "view_file_password_", "confirm_remove_file_password_", "remove_file_password_", "change_file_link_", "confirm_change_file_link_", "set_file_password_", "set_password_", "view_password_", "confirm_remove_pw_", "remove_password_")):
            await query.answer()
        
        if query.data.startswith("stop_batch_"):
            user_id = int(query.data.split("_")[2])
            if query.from_user.id == user_id:
                BATCH_STOP_FLAGS[user_id] = True
                await query.answer("⏹️ Stopping batch...", show_alert=False)
            else:
                await query.answer("❌ This is not your batch!", show_alert=True)
            return
        
        if query.data == "clone":
            pass  # Already answered above
        elif query.data == "toggle_clone":
            current = await get_clone_mode()
            new_status = not current
            await set_clone_mode(new_status)
            text = "✅ Enabled" if new_status else "❌ Disabled"
            buttons = [[InlineKeyboardButton(f"Clone: {text}", callback_data="toggle_clone")]]
            await query.message.edit_text(f"<b>Clone Mode {text}</b>", reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer(f"Clone {text}", show_alert=False)
            return
            
        if query.data == "close_data":
            await query.message.delete()
            await query.answer()
            return
            
        if query.data == "help":
            buttons = [[InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='start'), InlineKeyboardButton('❌ Close', callback_data='close_data')]]
            try:
                await query.message.edit_caption(caption=script.HELP_TXT, reply_markup=InlineKeyboardMarkup(buttons))
            except:
                await query.message.edit_text(text=script.HELP_TXT, reply_markup=InlineKeyboardMarkup(buttons))
            
        elif query.data == "about":
            buttons = [[InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='start'), InlineKeyboardButton('❌ Close', callback_data='close_data')]]
            me2 = client.me.mention if client.me else (await client.get_me()).mention
            try:
                await query.message.edit_caption(caption=script.ABOUT_TXT.format(me2), reply_markup=InlineKeyboardMarkup(buttons))
            except:
                await query.message.edit_text(text=script.ABOUT_TXT.format(me2), reply_markup=InlineKeyboardMarkup(buttons))
        
        elif query.data == "start":
            # Use shared function for consistent buttons
            buttons = build_start_buttons()
            me2 = client.me.mention if client.me else (await client.get_me()).mention
            try:
                await query.message.edit_caption(caption=script.START_TXT.format(query.from_user.mention, me2), reply_markup=InlineKeyboardMarkup(buttons))
            except:
                await query.message.edit_text(text=script.START_TXT.format(query.from_user.mention, me2), reply_markup=InlineKeyboardMarkup(buttons))
        
        elif query.data == "settings":
            destinations = await db.get_destinations(query.from_user.id)
            delivery_mode = await db.get_delivery_mode(query.from_user.id)
            buttons, text = await build_settings_ui(destinations, delivery_mode, query.from_user.id)
            try:
                await query.message.edit_caption(caption=text, reply_markup=InlineKeyboardMarkup(buttons))
            except:
                await query.message.edit_text(text=text, reply_markup=InlineKeyboardMarkup(buttons))
        
        elif query.data == "view_destinations":
            destinations = await db.get_destinations(query.from_user.id)
            buttons = []
            text = "<b>📋 Your Destinations:\n\n</b>"
            
            if not destinations:
                text += "No destinations added yet!"
            else:
                for i, dest in enumerate(destinations, 1):
                    # Use cached name from db, only fetch if not cached
                    dest_name = dest.get('cached_name') or f"Chat {dest['channel_id']}"
                    
                    # Show topic info if exists
                    topic_id = dest.get('topic_id')
                    topic_name = dest.get('topic_name')
                    topic_info = ""
                    if topic_id:
                        topic_info = f" → {topic_name}" if topic_name else f" → Topic {topic_id}"
                    
                    # Show status
                    status = "✅" if dest.get('enabled', True) else "❌"
                    
                    # Show as clickable button to view details
                    text += f"{i}. {dest_name}{topic_info} {status}\n"
                    buttons.append([InlineKeyboardButton(f"📌 {dest_name}{topic_info}", callback_data=f"dest_detail_{dest['channel_id']}")])
            
            delivery_mode = await db.get_delivery_mode(query.from_user.id)
            buttons.append([InlineKeyboardButton('➕ Add Destination', callback_data='add_destination')])
            mode_display = {"pm": "Bot only", "channel": "Channel only", "both": "Both Bot and Channel"}.get(delivery_mode.lower(), delivery_mode.upper())
            buttons.append([InlineKeyboardButton(f'📨 Send file in: {mode_display}', callback_data='delivery_mode')])
            buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='settings')])
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
        
        elif query.data.startswith("dest_detail_"):
            channel_id = int(query.data.split("_")[2])
            destinations = await db.get_destinations(query.from_user.id)
            
            # Find this destination
            dest_info = None
            for dest in destinations:
                if dest['channel_id'] == channel_id:
                    dest_info = dest
                    break
            
            if not dest_info:
                await query.answer("Destination not found!", show_alert=True)
                return
            
            # Use cached name from db
            dest_name = dest_info.get('cached_name') or f"Chat {channel_id}"
            
            # Get current status
            is_enabled = dest_info.get('enabled', True)
            status_button = "Enabled✅" if is_enabled else "Disabled❌"
            status_display = "✅ Enabled" if is_enabled else "❌ Disabled"
            
            # Get topic info if exists (only for groups)
            dest_type = dest_info.get('type', 'channel')
            topic_text = ""
            if dest_type == "group":
                topic_id = dest_info.get('topic_id')
                topic_name = dest_info.get('topic_name')
                if topic_id:
                    topic_text = f"\n📌 Topic: {topic_name}" if topic_name else f"\n📌 Topic ID: {topic_id}"
                else:
                    topic_text = "\n📌 Topic: General (All Topics)"
            
            text = f"<b>📌 Destination Details\n\n"
            text += f"Channel: {dest_name}\n"
            text += f"Status: {status_display}{topic_text}\n\n</b>"
            
            buttons = [
                [InlineKeyboardButton(f"❌ Remove", callback_data=f"remove_dest_{channel_id}"), 
                 InlineKeyboardButton(status_button, callback_data=f"toggle_dest_enable_{channel_id}")]
            ]
            
            # Only show Edit Topic for groups
            if dest_type == "group":
                buttons.append([InlineKeyboardButton('📝 Edit Topic', callback_data=f"edit_topic_{channel_id}")])
            
            buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='view_destinations')])
            
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
        
        elif query.data == "add_destination":
            from config import MAX_DESTINATIONS
            destinations = await db.get_destinations(query.from_user.id)
            
            if len(destinations) >= MAX_DESTINATIONS:
                await query.answer(f"❌ Maximum {MAX_DESTINATIONS} destinations reached!", show_alert=True)
                return
            
            await query.message.reply_text(
                "<b>➕ Add New Destination\n\n"
                "Option 1: Forward a message from channel/group\n"
                "Option 2: Send a group link like:\n"
                "<code>https://t.me/c/3354769817/7/8</code>\n"
                "Option 3: Send a channel ID like:\n"
                "<code>-1001234567890</code>"
            )
            try:
                user_input = await client.ask(query.message.chat.id, "<b>Forward message, send link, or send ID</b>", timeout=120)
                
                chat_id = None
                chat_title = None
                is_group = False
                topic_id = None
                dest_type = "channel"
                
                # Check forwarded message FIRST
                if user_input.forward_from_chat:
                    chat_id = user_input.forward_from_chat.id
                    chat_title = user_input.forward_from_chat.title
                    chat_type = user_input.forward_from_chat.type
                    is_group = chat_type in ("group", "supergroup")
                    
                    if is_group and user_input.message_thread_id:
                        topic_id = user_input.message_thread_id
                        dest_type = "group"
                    else:
                        dest_type = "group" if is_group else "channel"
                
                # Check for direct ID (like -1001234567890)
                elif user_input.text and user_input.text.strip().startswith("-100"):
                    try:
                        chat_id = int(user_input.text.strip())
                        dest_type = "channel"
                        is_group = False
                    except ValueError:
                        await query.message.reply_text("<b>❌ Invalid channel ID format!</b>")
                        await user_input.delete()
                        destinations = await db.get_destinations(query.from_user.id)
                        delivery_mode = await db.get_delivery_mode(query.from_user.id)
                        buttons, text = await build_settings_ui(destinations, delivery_mode)
                        await client.edit_message_media(query.message.chat.id, query.message.id, InputMediaPhoto(random.choice(PICS)))
                        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
                        return
                
                # Then check for link
                elif user_input.text and "t.me" in user_input.text:
                    link_parts = re.findall(r'https://t\.me/c/(\d+)(?:/(\d+))?(?:/(\d+))?', user_input.text)
                    if link_parts:
                        extracted_chat_id = link_parts[0][0]
                        extracted_topic_id = link_parts[0][1] if link_parts[0][1] else None
                        chat_id = int(f"-100{extracted_chat_id}")
                        topic_id = int(extracted_topic_id) if extracted_topic_id else None
                        dest_type = "group"
                        is_group = True
                    else:
                        await query.message.reply_text("<b>❌ Invalid link format!</b>")
                        await user_input.delete()
                        destinations = await db.get_destinations(query.from_user.id)
                        delivery_mode = await db.get_delivery_mode(query.from_user.id)
                        buttons, text = await build_settings_ui(destinations, delivery_mode)
                        await client.edit_message_media(query.message.chat.id, query.message.id, InputMediaPhoto(random.choice(PICS)))
                        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
                        return
                else:
                    await query.message.reply_text("<b>❌ Please forward a message, send a link, or send a channel ID!</b>")
                    await user_input.delete()
                    destinations = await db.get_destinations(query.from_user.id)
                    delivery_mode = await db.get_delivery_mode(query.from_user.id)
                    buttons, text = await build_settings_ui(destinations, delivery_mode)
                    await client.edit_message_media(query.message.chat.id, query.message.id, InputMediaPhoto(random.choice(PICS)))
                    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
                    return
                
                if chat_id:
                    try:
                        member = await client.get_chat_member(chat_id, "me")
                        if member.privileges and (member.privileges.can_pin_messages or member.privileges.can_delete_messages):
                            # Get chat title for caching if not already available
                            if not chat_title:
                                try:
                                    chat_obj = await client.get_chat(chat_id)
                                    chat_title = chat_obj.title
                                except:
                                    chat_title = f"Chat {chat_id}"
                            
                            # Add destination with cached name for faster future loads
                            was_added = await db.add_destination(query.from_user.id, chat_id, dest_type, topic_id, cached_name=chat_title)
                            await user_input.delete()
                            
                            topic_text = f"\n📌 Topic: {topic_id}" if topic_id else ""
                            
                            if was_added:
                                await query.message.reply_text(
                                    f"<b>✅ Added to destinations!\n\n"
                                    f"Type: {'Group' if is_group else 'Channel'}\n"
                                    f"Name: <code>{chat_title}</code>{topic_text}</b>"
                                )
                            else:
                                await query.message.reply_text(
                                    f"<b>ℹ️ This destination is already saved!\n\n"
                                    f"Type: {'Group' if is_group else 'Channel'}\n"
                                    f"Name: <code>{chat_title}</code>{topic_text}</b>"
                                )
                            destinations = await db.get_destinations(query.from_user.id)
                            delivery_mode = await db.get_delivery_mode(query.from_user.id)
                            buttons, text = await build_settings_ui(destinations, delivery_mode)
                            await client.edit_message_media(query.message.chat.id, query.message.id, InputMediaPhoto(random.choice(PICS)))
                            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
                        else:
                            await query.message.reply_text("<b>❌ I'm not admin there!</b>")
                            destinations = await db.get_destinations(query.from_user.id)
                            delivery_mode = await db.get_delivery_mode(query.from_user.id)
                            buttons, text = await build_settings_ui(destinations, delivery_mode)
                            await client.edit_message_media(query.message.chat.id, query.message.id, InputMediaPhoto(random.choice(PICS)))
                            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
                    except Exception as e:
                        logger.error(f"Admin check error: {e}")
                        await query.message.reply_text(f"<b>❌ Error: {str(e)[:50]}</b>")
                        destinations = await db.get_destinations(query.from_user.id)
                        delivery_mode = await db.get_delivery_mode(query.from_user.id)
                        buttons, text = await build_settings_ui(destinations, delivery_mode)
                        await client.edit_message_media(query.message.chat.id, query.message.id, InputMediaPhoto(random.choice(PICS)))
                        await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            except asyncio.TimeoutError:
                await query.message.reply_text("<b>❌ Timeout! Please try again.</b>")
                destinations = await db.get_destinations(query.from_user.id)
                delivery_mode = await db.get_delivery_mode(query.from_user.id)
                buttons, text = await build_settings_ui(destinations, delivery_mode)
                await client.edit_message_media(query.message.chat.id, query.message.id, InputMediaPhoto(random.choice(PICS)))
                await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            except Exception as e:
                logger.error(f"Add destination error: {e}")
                await query.message.reply_text(f"<b>❌ Error: {str(e)[:50]}</b>")
                destinations = await db.get_destinations(query.from_user.id)
                delivery_mode = await db.get_delivery_mode(query.from_user.id)
                buttons, text = await build_settings_ui(destinations, delivery_mode)
                await client.edit_message_media(query.message.chat.id, query.message.id, InputMediaPhoto(random.choice(PICS)))
                await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            return
        
        elif query.data.startswith("select_topic_"):
            # Handle topic selection: select_topic_chat_id_topic_id_topic_title
            parts = query.data.split("_", 4)  # Split only into 5 parts max
            chat_id = int(parts[2])
            topic_id = int(parts[3]) if parts[3] != "0" else None
            topic_name = parts[4].replace("_", " ") if len(parts) > 4 and parts[4] != "0" else None
            
            # Check if this is from add_destination or edit_topic
            temp_key_add = f"topic_{query.from_user.id}"
            temp_key_edit = f"edit_topic_{query.from_user.id}"
            
            if temp_key_add in BATCH_FILES:
                # Adding new destination
                temp_data = BATCH_FILES[temp_key_add]
                dest_type = temp_data['dest_type']
                chat_title = temp_data['chat_title']
                is_group = temp_data['is_group']
                
                was_added = await db.add_destination(query.from_user.id, chat_id, dest_type, topic_id, topic_name)
                
                topic_text = f"\n📌 Topic: {topic_name}" if topic_id and topic_name else (f"\n📌 Topic ID: {topic_id}" if topic_id else "\n📌 Topic: General (All Topics)")
                
                if was_added:
                    await query.message.edit_text(
                        f"<b>✅ Added to destinations!\n\n"
                        f"Type: {'Group' if is_group else 'Channel'}\n"
                        f"Name: <code>{chat_title}</code>{topic_text}</b>"
                    )
                else:
                    await query.message.edit_text(
                        f"<b>ℹ️ This destination is already saved!\n\n"
                        f"Type: {'Group' if is_group else 'Channel'}\n"
                        f"Name: <code>{chat_title}</code>{topic_text}</b>"
                    )
                
                del BATCH_FILES[temp_key_add]
                destinations = await db.get_destinations(query.from_user.id)
                delivery_mode = await db.get_delivery_mode(query.from_user.id)
                buttons, text = await build_settings_ui(destinations, delivery_mode)
                
                # Wait a moment then go back to settings
                await asyncio.sleep(1.5)
                await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            
            elif temp_key_edit in BATCH_FILES:
                # Editing existing destination
                temp_data = BATCH_FILES[temp_key_edit]
                chat_title = temp_data['chat_title']
                
                await db.update_destination_topic(query.from_user.id, chat_id, topic_id, topic_name)
                
                topic_text = f"\n📌 Topic: {topic_name}" if topic_id and topic_name else (f"\n📌 Topic ID: {topic_id}" if topic_id else "\n📌 Topic: General (All Topics)")
                
                await query.message.edit_text(
                    f"<b>✅ Topic updated!\n\n"
                    f"Name: <code>{chat_title}</code>{topic_text}</b>"
                )
                
                del BATCH_FILES[temp_key_edit]
                
                # Wait a moment then go back to destination detail
                await asyncio.sleep(1.5)
                destinations = await db.get_destinations(query.from_user.id)
                for dest in destinations:
                    if dest['channel_id'] == chat_id:
                        dest_info = dest
                        break
                
                try:
                    chat = await client.get_chat(chat_id)
                    dest_name = chat.title
                except:
                    dest_name = f"Chat {chat_id}"
                
                is_enabled = dest_info.get('enabled', True)
                status_button = "Enabled✅" if is_enabled else "Disabled❌"
                status_display = "✅ Enabled" if is_enabled else "❌ Disabled"
                
                new_topic_id = dest_info.get('topic_id')
                new_topic_name = dest_info.get('topic_name')
                new_dest_type = dest_info.get('type', 'channel')
                topic_txt = ""
                if new_dest_type == "group":
                    if new_topic_id:
                        topic_txt = f"\n📌 Topic: {new_topic_name}" if new_topic_name else f"\n📌 Topic ID: {new_topic_id}"
                    else:
                        topic_txt = "\n📌 Topic: General (All Topics)"
                
                text = f"<b>📌 Destination Details\n\n"
                text += f"Channel: {dest_name}\n"
                text += f"Status: {status_display}{topic_txt}\n\n</b>"
                
                buttons = [
                    [InlineKeyboardButton(f"❌ Remove", callback_data=f"remove_dest_{chat_id}"), 
                     InlineKeyboardButton(status_button, callback_data=f"toggle_dest_enable_{chat_id}")]
                ]
                
                # Only show Edit Topic for groups
                if new_dest_type == "group":
                    buttons.append([InlineKeyboardButton('📝 Edit Topic', callback_data=f"edit_topic_{chat_id}")])
                
                buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='view_destinations')])
                
                await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            
            await query.answer("✅ Topic updated!", show_alert=False)
            return
        
        elif query.data.startswith("edit_topic_"):
            # Edit topic for existing destination
            channel_id = int(query.data.split("_")[2])
            destinations = await db.get_destinations(query.from_user.id)
            
            dest_info = None
            for dest in destinations:
                if dest['channel_id'] == channel_id:
                    dest_info = dest
                    break
            
            if not dest_info:
                await query.answer("Destination not found!", show_alert=True)
                return
            
            try:
                chat = await client.get_chat(channel_id)
                chat_title = chat.title
            except:
                chat_title = f"Chat {channel_id}"
            
            # Ask user to enter topic ID manually or send a link
            await query.message.edit_text(
                f"<b>📝 Edit Topic for {chat_title}\n\n"
                f"Send a topic ID (number), or send a link like:\n"
                f"<code>https://t.me/c/3354769817/2/52</code>\n\n"
                f"Or send 0 for General (All Topics)</b>"
            )
            
            try:
                topic_input = await client.ask(query.message.chat.id, "<b>Topic ID, link, or 0 for General:</b>", timeout=120)
                entered_topic_id = None
                entered_topic_name = None
                
                if topic_input.text.strip() == "0":
                    entered_topic_id = None
                    entered_topic_name = None
                elif "t.me" in topic_input.text:
                    # Parse t.me link: https://t.me/c/CHANNEL_ID/TOPIC_ID/MESSAGE_ID
                    link_parts = re.findall(r'https://t\.me/c/(\d+)(?:/(\d+))?(?:/(\d+))?', topic_input.text)
                    if link_parts and link_parts[0][1]:
                        entered_topic_id = int(link_parts[0][1])
                        entered_topic_name = f"Topic {entered_topic_id}"
                    else:
                        await query.message.reply_text("<b>❌ Invalid link format! Use: https://t.me/c/CHANNEL_ID/TOPIC_ID/MESSAGE_ID</b>")
                        await topic_input.delete()
                        return
                else:
                    try:
                        entered_topic_id = int(topic_input.text.strip())
                        entered_topic_name = f"Topic {entered_topic_id}"  # Default name since we can't fetch it
                    except ValueError:
                        await query.message.reply_text("<b>❌ Invalid input! Please send a number or a valid link.</b>")
                        await topic_input.delete()
                        return
                
                await db.update_destination_topic(query.from_user.id, channel_id, entered_topic_id, entered_topic_name)
                await topic_input.delete()
                
                topic_text = f"\n📌 Topic: {entered_topic_name}" if entered_topic_id else "\n📌 Topic: General (All Topics)"
                
                await query.message.edit_text(
                    f"<b>✅ Topic updated!\n\n"
                    f"Name: <code>{chat_title}</code>{topic_text}</b>"
                )
                
                # Go back to destination detail
                await asyncio.sleep(1.5)
                destinations = await db.get_destinations(query.from_user.id)
                for dest in destinations:
                    if dest['channel_id'] == channel_id:
                        dest_info = dest
                        break
                
                try:
                    chat = await client.get_chat(channel_id)
                    dest_name = chat.title
                except:
                    dest_name = f"Chat {channel_id}"
                
                is_enabled = dest_info.get('enabled', True)
                status_button = "Enabled✅" if is_enabled else "Disabled❌"
                status_display = "✅ Enabled" if is_enabled else "❌ Disabled"
                
                new_topic_id = dest_info.get('topic_id')
                new_topic_name = dest_info.get('topic_name')
                topic_txt = ""
                if new_topic_id:
                    topic_txt = f"\n📌 Topic: {new_topic_name}" if new_topic_name else f"\n📌 Topic ID: {new_topic_id}"
                else:
                    topic_txt = "\n📌 Topic: General (All Topics)"
                
                text = f"<b>📌 Destination Details\n\n"
                text += f"Channel: {dest_name}\n"
                text += f"Status: {status_display}{topic_txt}\n\n</b>"
                
                buttons = [
                    [InlineKeyboardButton(f"❌ Remove", callback_data=f"remove_dest_{channel_id}"), 
                     InlineKeyboardButton(status_button, callback_data=f"toggle_dest_enable_{channel_id}")],
                    [InlineKeyboardButton('📝 Edit Topic', callback_data=f"edit_topic_{channel_id}")],
                    [InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='view_destinations')]
                ]
                
                await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            except asyncio.TimeoutError:
                await query.message.reply_text("<b>❌ Timeout! Please try again.</b>")
            except Exception as e:
                logger.error(f"Edit topic error: {e}")
                await query.message.reply_text(f"<b>❌ Error: {str(e)[:50]}</b>")
            
            return
        
        elif query.data.startswith("remove_dest_"):
            channel_id = int(query.data.split("_")[2])
            await db.remove_destination(query.from_user.id, channel_id)
            
            # Go back to destinations list
            destinations = await db.get_destinations(query.from_user.id)
            buttons = []
            text = "<b>📋 Your Destinations:\n\n</b>"
            
            if not destinations:
                text += "No destinations added yet!"
            else:
                for i, dest in enumerate(destinations, 1):
                    try:
                        chat = await client.get_chat(dest['channel_id'])
                        dest_name = chat.title
                    except:
                        dest_name = f"Chat {dest['channel_id']}"
                    
                    text += f"{i}. {dest_name}\n"
                    buttons.append([InlineKeyboardButton(f"📌 {dest_name}", callback_data=f"dest_detail_{dest['channel_id']}")])
            
            buttons.append([InlineKeyboardButton('➕ Add Destination', callback_data='add_destination')])
            buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='settings')])
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer("✅ Destination removed!", show_alert=False)
            return
        
        elif query.data.startswith("toggle_dest_enable_"):
            channel_id = int(query.data.split("_")[3])
            await db.toggle_destination_status(query.from_user.id, channel_id)
            
            # Refresh the detail view
            destinations = await db.get_destinations(query.from_user.id)
            
            dest_info = None
            for dest in destinations:
                if dest['channel_id'] == channel_id:
                    dest_info = dest
                    break
            
            if not dest_info:
                await query.answer("Destination not found!", show_alert=True)
                return
            
            try:
                chat = await client.get_chat(channel_id)
                dest_name = chat.title
            except:
                dest_name = f"Chat {channel_id}"
            
            is_enabled = dest_info.get('enabled', True)
            status_button = "Enabled✅" if is_enabled else "Disabled❌"
            status_msg = "Enabled ✅" if is_enabled else "Disabled ❌"
            status_display = "✅ Enabled" if is_enabled else "❌ Disabled"
            
            # Get topic info for toggle view (only for groups)
            toggle_type = dest_info.get('type', 'channel')
            toggle_topic_txt = ""
            if toggle_type == "group":
                toggle_topic_id = dest_info.get('topic_id')
                toggle_topic_name = dest_info.get('topic_name')
                if toggle_topic_id:
                    toggle_topic_txt = f"\n📌 Topic: {toggle_topic_name}" if toggle_topic_name else f"\n📌 Topic ID: {toggle_topic_id}"
                else:
                    toggle_topic_txt = "\n📌 Topic: General (All Topics)"
            
            text = f"<b>📌 Destination Details\n\n"
            text += f"Channel: {dest_name}\n"
            text += f"Status: {status_display}{toggle_topic_txt}\n\n</b>"
            
            buttons = [
                [InlineKeyboardButton(f"❌ Remove", callback_data=f"remove_dest_{channel_id}"), 
                 InlineKeyboardButton(status_button, callback_data=f"toggle_dest_enable_{channel_id}")],
                [InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='view_destinations')]
            ]
            
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer(f"Status changed to {status_msg}!", show_alert=False)
            return
        
        
        elif query.data == "caption_menu":
            caption = await db.get_caption(query.from_user.id)
            buttons = []
            buttons.append([InlineKeyboardButton('✏️ Set Caption', callback_data='set_caption')])
            if caption:
                buttons.append([InlineKeyboardButton('👁️ See Caption', callback_data='see_caption')])
                buttons.append([InlineKeyboardButton('🗑️ Delete Caption', callback_data='delete_caption')])
            buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='settings')])
            await query.message.edit_text(script.CAPTION_MENU_TXT, reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
        
        elif query.data == "customize_menu":
            buttons = [
                [InlineKeyboardButton('🔄 Replace Words', callback_data='replace_words'), InlineKeyboardButton('🗑️ Remove Words', callback_data='remove_words')],
                [InlineKeyboardButton('🔴 Reset All', callback_data='reset_all')],
                [InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='settings')]
            ]
            await query.message.edit_text("<b>⚙️ Customize Settings</b>", reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
            return
        
        elif query.data == "manage_folders":
            try:
                folders = await db.get_folders(query.from_user.id)
                selected = await db.get_selected_folder(query.from_user.id)
                
                buttons = []
                text = "<b>📁 Manage Folders</b>\n\n"
                
                if folders and len(folders) > 0:
                    text += "Your folders:\n\n"
                    for idx, f in enumerate(folders):
                        if isinstance(f, dict):
                            folder_name = f.get('name', str(f))
                        else:
                            folder_name = str(f)
                        
                        marker = "✓" if folder_name == selected else " "
                        buttons.append([
                            InlineKeyboardButton(f"[{marker}] {folder_name}", callback_data=f"sel_folder_{idx}"),
                            InlineKeyboardButton("✏️", callback_data=f"rename_folder_{idx}"),
                            InlineKeyboardButton("❌", callback_data=f"del_folder_{idx}")
                        ])
                    text += "✓ = Selected, ✏️ = Rename, ❌ = Delete"
                else:
                    text += "No folders yet"
                
                buttons.append([InlineKeyboardButton('➕ Create Folder', callback_data='create_folder_btn')])
                buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='settings')])
                
                await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            except Exception as e:
                logger.error(f"Manage folders error: {e}")
                await query.answer(f"❌ Error: {str(e)[:50]}", show_alert=True)
            return
        
        elif query.data.startswith("sel_folder_"):
            try:
                idx = int(query.data.split("_")[2])
                folders = await db.get_folders(query.from_user.id)
                
                if 0 <= idx < len(folders):
                    f = folders[idx]
                    folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                    await db.set_selected_folder(query.from_user.id, folder_name)
                    await query.answer(f"✅ Selected: {folder_name}", show_alert=False)
                
                    # Refresh UI
                    folders = await db.get_folders(query.from_user.id)
                    selected = await db.get_selected_folder(query.from_user.id)
                    
                    buttons = []
                    text = "<b>📁 Manage Folders</b>\n\n"
                    
                    if folders and len(folders) > 0:
                        text += "Your folders:\n\n"
                        for i, f in enumerate(folders):
                            folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                            marker = "✓" if folder_name == selected else " "
                            buttons.append([
                                InlineKeyboardButton(f"[{marker}] {folder_name}", callback_data=f"sel_folder_{i}"),
                                InlineKeyboardButton("✏️", callback_data=f"rename_folder_{i}"),
                                InlineKeyboardButton("❌", callback_data=f"del_folder_{i}")
                            ])
                        text += "✓ = Selected, ✏️ = Rename, ❌ = Delete"
                    
                    buttons.append([InlineKeyboardButton('➕ Create Folder', callback_data='create_folder_btn')])
                    buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='settings')])
                    
                    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            except Exception as e:
                logger.error(f"Select folder error: {e}")
            return
        
        elif query.data.startswith("del_folder_"):
            try:
                idx = int(query.data.split("_")[2])
                folders = await db.get_folders(query.from_user.id)
                
                if 0 <= idx < len(folders):
                    f = folders[idx]
                    folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                    
                    buttons = [
                        [InlineKeyboardButton("✅ Confirm Delete", callback_data=f"confirm_del_{idx}"), 
                         InlineKeyboardButton("❌ Cancel", callback_data='manage_folders')]
                    ]
                    
                    await query.message.edit_text(f"<b>Delete folder '{folder_name}'?</b>", reply_markup=InlineKeyboardMarkup(buttons))
            except Exception as e:
                logger.error(f"Delete folder error: {e}")
            return
        
        elif query.data.startswith("confirm_del_"):
            try:
                idx = int(query.data.split("_")[2])
                folders = await db.get_folders(query.from_user.id)
                
                if 0 <= idx < len(folders):
                    f = folders[idx]
                    folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                    await db.delete_folder(query.from_user.id, folder_name)
                    selected = await db.get_selected_folder(query.from_user.id)
                    if selected == folder_name:
                        await db.set_selected_folder(query.from_user.id, None)
                    
                    await query.answer(f"✅ Deleted: {folder_name}", show_alert=False)
                    
                    # Refresh UI
                    folders = await db.get_folders(query.from_user.id)
                    selected = await db.get_selected_folder(query.from_user.id)
                    
                    buttons = []
                    text = "<b>📁 Manage Folders</b>\n\n"
                    
                    if folders and len(folders) > 0:
                        text += "Your folders:\n\n"
                        for i, f in enumerate(folders):
                            folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                            marker = "✓" if folder_name == selected else " "
                            buttons.append([
                                InlineKeyboardButton(f"[{marker}] {folder_name}", callback_data=f"sel_folder_{i}"),
                                InlineKeyboardButton("✏️", callback_data=f"rename_folder_{i}"),
                                InlineKeyboardButton("❌", callback_data=f"del_folder_{i}")
                            ])
                        text += "✓ = Selected, ✏️ = Rename, ❌ = Delete"
                    else:
                        text += "No folders yet"
                    
                    buttons.append([InlineKeyboardButton('➕ Create Folder', callback_data='create_folder_btn')])
                    buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='settings')])
                    
                    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            except Exception as e:
                logger.error(f"Confirm delete error: {e}")
            return
        
        elif query.data == "create_folder_btn":
            await query.answer()
            await query.message.reply_text("<b>📁 Create New Folder\n\nSend folder name:</b>")
            CAPTION_INPUT_MODE[query.from_user.id] = "create_folder"
            return
        
        elif query.data.startswith("rename_folder_") and not query.data.startswith("rename_folder_action_"):
            try:
                idx = int(query.data.split("_")[2])
                folders = await db.get_folders(query.from_user.id)
                
                if 0 <= idx < len(folders):
                    f = folders[idx]
                    folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                    await query.answer()
                    await query.message.reply_text(f"<b>📝 Rename folder '{folder_name}'\n\nSend new name:</b>")
                    CAPTION_INPUT_MODE[query.from_user.id] = f"rename_folder:{idx}:{folder_name}"
            except Exception as e:
                logger.error(f"Rename folder error: {e}")
            return
        
        elif query.data == "replace_words":
            filters_list = await db.get_filename_filters(query.from_user.id)
            replace_list = [f for f in filters_list if "|" in f]
            
            buttons = []
            text = "<b>🔄 Replace Words</b>\n\n"
            
            if replace_list:
                text += "Current replacements:\n\n"
                for idx, item in enumerate(replace_list, 1):
                    old, new = item.split("|", 1)
                    buttons.append([InlineKeyboardButton(f"{idx}. {old.strip()} → {new.strip()}", callback_data=f"del_replace_{idx-1}")])
                    text += f"{idx}. <code>{old.strip()}</code> → <code>{new.strip()}</code>\n"
                buttons.append([InlineKeyboardButton('➕ Add More', callback_data='add_replace')])
            else:
                text += "No replacements set yet\n"
                buttons.append([InlineKeyboardButton('➕ Add', callback_data='add_replace')])
            
            buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='customize_menu')])
            
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
            return
        
        elif query.data == "remove_words":
            filters_list = await db.get_filename_filters(query.from_user.id)
            remove_list = [f for f in filters_list if "|" not in f]
            
            buttons = []
            text = "<b>🗑️ Remove Words</b>\n\n"
            
            if remove_list:
                text += "Current removals:\n\n"
                for idx, item in enumerate(remove_list, 1):
                    buttons.append([InlineKeyboardButton(f"{idx}. {item}", callback_data=f"del_remove_{idx-1}")])
                    text += f"{idx}. <code>{item}</code>\n"
                buttons.append([InlineKeyboardButton('➕ Add More', callback_data='add_remove')])
            else:
                text += "No removals set yet\n"
                buttons.append([InlineKeyboardButton('➕ Add', callback_data='add_remove')])
            
            buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='customize_menu')])
            
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
            return
        
        elif query.data.startswith("del_replace_"):
            idx = int(query.data.split("_")[2])
            filters_list = await db.get_filename_filters(query.from_user.id)
            replace_list = [f for f in filters_list if "|" in f]
            
            if 0 <= idx < len(replace_list):
                await db.remove_filename_filter(query.from_user.id, replace_list[idx])
                # Clear input mode to prevent issues
                CAPTION_INPUT_MODE.pop(query.from_user.id, None)
                await query.answer("✅ Replacement deleted!", show_alert=False)
                
                # Refresh UI
                filters_list = await db.get_filename_filters(query.from_user.id)
                replace_list = [f for f in filters_list if "|" in f]
                
                buttons = []
                text = "<b>🔄 Replace Words</b>\n\n"
                
                if replace_list:
                    text += "Current replacements:\n\n"
                    for i, item in enumerate(replace_list, 1):
                        old, new = item.split("|", 1)
                        buttons.append([InlineKeyboardButton(f"{i}. {old.strip()} → {new.strip()}", callback_data=f"del_replace_{i-1}")])
                        text += f"{i}. <code>{old.strip()}</code> → <code>{new.strip()}</code>\n"
                    buttons.append([InlineKeyboardButton('➕ Add More', callback_data='add_replace')])
                else:
                    text += "No replacements set yet\n"
                    buttons.append([InlineKeyboardButton('➕ Add', callback_data='add_replace')])
                
                buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='customize_menu')])
                
                await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            return
        
        elif query.data.startswith("del_remove_"):
            idx = int(query.data.split("_")[2])
            filters_list = await db.get_filename_filters(query.from_user.id)
            remove_list = [f for f in filters_list if "|" not in f]
            
            if 0 <= idx < len(remove_list):
                await db.remove_filename_filter(query.from_user.id, remove_list[idx])
                # Clear input mode to prevent issues
                CAPTION_INPUT_MODE.pop(query.from_user.id, None)
                await query.answer("✅ Removal deleted!", show_alert=False)
                
                # Refresh UI
                filters_list = await db.get_filename_filters(query.from_user.id)
                remove_list = [f for f in filters_list if "|" not in f]
                
                buttons = []
                text = "<b>🗑️ Remove Words</b>\n\n"
                
                if remove_list:
                    text += "Current removals:\n\n"
                    for i, item in enumerate(remove_list, 1):
                        buttons.append([InlineKeyboardButton(f"{i}. {item}", callback_data=f"del_remove_{i-1}")])
                        text += f"{i}. <code>{item}</code>\n"
                    buttons.append([InlineKeyboardButton('➕ Add More', callback_data='add_remove')])
                else:
                    text += "No removals set yet\n"
                    buttons.append([InlineKeyboardButton('➕ Add', callback_data='add_remove')])
                
                buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='customize_menu')])
                
                await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            return
        
        elif query.data == "add_replace":
            await query.answer()
            await query.message.reply_text("<b>🔄 Add More Replacements\n\nFormat replacements as:\nold1|new1\nold2|new2\n\nExample:\n<code>Bot|MyBot\nHii|💗 Krishna</code></b>")
            CAPTION_INPUT_MODE[query.from_user.id] = "replace_words"
            return
        
        elif query.data == "add_remove":
            await query.answer()
            await query.message.reply_text("<b>🗑️ Add More Words to Remove\n\nSend words separated by space:\n\nExample:\n<code>Downloaded By</code></b>")
            CAPTION_INPUT_MODE[query.from_user.id] = "remove_words"
            return
        
        elif query.data == "reset_all":
            await db.delete_caption(query.from_user.id)
            await db.col.update_one({'id': int(query.from_user.id)}, {'$set': {'filename_filters': []}})
            await query.answer("✅ All settings reset!", show_alert=True)
            return
        
        elif query.data == "set_caption":
            CAPTION_INPUT_MODE[query.from_user.id] = True
            await query.answer()
            help_text = """<b>📝 SET CAPTION

Send me your caption text

AVAILABLE TEMPLATE VARIABLES:
• {filename} - File name
• {filesize} - File size
• {duration} - Media duration

EXAMPLE:
<code>{filename}
💾 Size: {filesize}
⏰ Duration: {duration}</code></b>"""
            await query.message.reply_text(help_text)
            return
        
        elif query.data == "see_caption":
            try:
                caption = await db.get_caption(query.from_user.id)
                if caption:
                    text = f"<b>📝 YOUR CAPTION:</b>\n\n<code>{caption}</code>"
                    buttons = [[InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='caption_menu')]]
                    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
                else:
                    text = "<b>❌ No caption set yet\n\nUse 'Set Caption' to create one</b>"
                    buttons = [[InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='caption_menu')]]
                    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
                await query.answer()
            except Exception as e:
                logger.error(f"See caption error: {e}")
                await query.answer(f"Error: {str(e)[:50]}", show_alert=True)
            return
        
        elif query.data == "delete_caption":
            try:
                caption = await db.get_caption(query.from_user.id)
                if caption:
                    await db.delete_caption(query.from_user.id)
                    text = "<b>✅ CAPTION DELETED\n\nYour caption has been removed successfully</b>"
                    buttons = [[InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='caption_menu')]]
                    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
                    await query.answer("Caption deleted!", show_alert=False)
                else:
                    text = "<b>❌ No caption to delete\n\nSet a caption first</b>"
                    buttons = [[InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='caption_menu')]]
                    await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
                    await query.answer("No caption found", show_alert=False)
            except Exception as e:
                logger.error(f"Delete caption error: {e}")
                await query.answer(f"Error: {str(e)[:50]}", show_alert=True)
            return
        
        elif query.data == "delivery_mode":
            current_mode = await db.get_delivery_mode(query.from_user.id)
            pm_label = "✅ Bot only" if current_mode == "pm" else "Bot only"
            channel_label = "✅ Channel only" if current_mode == "channel" else "Channel only"
            both_label = "✅ Both Bot and Channel" if current_mode == "both" else "Both Bot and Channel"
            buttons = [
                [InlineKeyboardButton(pm_label, callback_data='mode_pm'), InlineKeyboardButton(channel_label, callback_data='mode_channel')],
                [InlineKeyboardButton(both_label, callback_data='mode_both')],
                [InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='view_destinations')]
            ]
            await query.message.edit_text("<b>Send file in :</b>", reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
        
        elif query.data.startswith("mode_"):
            mode = query.data.split("_")[1]
            
            # Check if user has destinations when selecting channel or both
            if mode in ['channel', 'both']:
                destinations = await db.get_destinations(query.from_user.id)
                if not destinations:
                    try:
                        await query.answer(text="Add at least one channel for send file in channel", show_alert=True)
                    except Exception as e:
                        logger.error(f"Alert error: {e}")
                    return
            
            await db.set_delivery_mode(query.from_user.id, mode)
            mode_text = {"pm": "Bot only", "channel": "Channel only", "both": "Both Bot and Channel"}
            await query.answer(f"✅ {mode_text.get(mode)}", show_alert=False)
            pm_label = "✅ Bot only" if mode == "pm" else "Bot only"
            channel_label = "✅ Channel only" if mode == "channel" else "Channel only"
            both_label = "✅ Both Bot and Channel" if mode == "both" else "Both Bot and Channel"
            buttons = [
                [InlineKeyboardButton(pm_label, callback_data='mode_pm'), InlineKeyboardButton(channel_label, callback_data='mode_channel')],
                [InlineKeyboardButton(both_label, callback_data='mode_both')],
                [InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='view_destinations')]
            ]
            await query.message.edit_text("<b>Send file in :</b>", reply_markup=InlineKeyboardMarkup(buttons))
            return
        
        elif query.data.startswith("select_dest_"):
            channel_id = int(query.data.split("_")[2])
            selected = BATCH_FILES.get(query.from_user.id, {}).get('selected_dests', [])
            
            if channel_id in selected:
                selected.remove(channel_id)
            else:
                selected.append(channel_id)
            
            # Update UI
            await query.answer()
            return
        
        elif query.data == "send_to_pm":
            file_data = BATCH_FILES.get(query.from_user.id)
            if file_data:
                msg = file_data['msg']
                caption = file_data['caption']
                await msg.copy(chat_id=query.from_user.id, caption=caption, protect_content=False)
                await query.message.delete()
                BATCH_FILES.pop(query.from_user.id, None)
                await query.answer("✅ Sent to PM!", show_alert=False)
            return
        
        elif query.data == "send_selected":
            file_data = BATCH_FILES.get(query.from_user.id)
            if file_data:
                msg = file_data['msg']
                selected_dests = file_data.get('selected_dests', [])
                
                if not selected_dests:
                    await query.answer("❌ No destinations selected!", show_alert=True)
                    return
                
                sts = await query.message.edit_text("🔄 Sending to destinations...")
                success = 0
                
                for dest_id in selected_dests:
                    try:
                        await msg.copy(chat_id=dest_id, caption=None, protect_content=False)
                        success += 1
                    except Exception as e:
                        logger.error(f"Error sending to destination: {e}")
                
                await sts.edit(f"✅ Sent to {success} destination(s)!")
                BATCH_FILES.pop(query.from_user.id, None)
            return
        
        elif query.data == "my_files_menu":
            buttons = build_my_files_buttons()
            await query.message.edit_text("<b>📂 My Files\n\nChoose view:</b>", reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
            return
        
        elif query.data == "backup_restore_menu":
            backup_text = """<b>🔄 Backup & Restore

Sometimes you may want to delete your Telegram account, but you don't want to lose your files and would like to access them in a new account. We've made this simple for you 😉

1️⃣ First Method:
Click 'Generate Token' to create a unique token.
Save it safely! In your new account, start the bot and click 'Restore'. Enter the token, and all your files will be transferred.

2️⃣ Second Method:
Click 'Get Restore Link' to create your unique link.
Open that link in your new account, and you're all set.

⚠️ Keep your token/link SECRET - anyone with it can restore your files to their account!</b>"""
            buttons = build_backup_restore_buttons()
            await query.message.edit_text(backup_text, reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
            return
        
        elif query.data == "generate_backup_token":
            token = await db.generate_backup_token(query.from_user.id)
            text = f"""<b>🔑 Your Backup Token

<code>{token}</code>

⚠️ IMPORTANT: Keep this token SECRET!
Anyone with this token can restore your files to their account.

To restore in a new account:
1. Delete your current Telegram account (if needed)
2. Create a new Telegram account
3. Start the bot → My Files → Backup & Restore → Restore
4. Enter this token

The token can only be used ONCE - after restore, it becomes invalid.</b>"""
            buttons = [[InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='backup_restore_menu')]]
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
            return
        
        elif query.data == "restore_files":
            RESTORE_MODE[query.from_user.id] = True
            text = """<b>📥 Restore Files

Please send the backup token you received from your old account.

The token format is: <code>UserId:token</code>
Example: <code>123456789:UyFjZE01PpIWFdIBZKAHLVHRYWT9eyjZ</code>

Send /cancel to cancel.</b>"""
            buttons = [[InlineKeyboardButton('❌ Cancel', callback_data='backup_restore_menu')]]
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
            return
        
        elif query.data == "get_restore_link":
            token = await db.generate_backup_token(query.from_user.id)
            username = (await client.get_me()).username
            encoded_token = b64_encode(token)
            restore_link = f"https://t.me/{username}?start=restore_{encoded_token}"
            text = f"""<b>🔗 Your Restore Link

{restore_link}

Your Token: <code>{token}</code>

⚠️ Keep this link safe! Only you can authorize the restore from your current account.

To restore:
1. Open this link in your new Telegram account
2. Your files will be transferred automatically</b>"""
            buttons = [[InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='backup_restore_menu')]]
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
            return
        
        elif query.data == "change_backup_token":
            existing_token = await db.get_backup_token(query.from_user.id)
            if not existing_token:
                await query.answer("❌ No token exists to change!", show_alert=True)
                return
            
            new_token = await db.change_backup_token(query.from_user.id)
            text = f"""<b>🔄 Token Changed!

New Token: <code>{new_token}</code>

⚠️ Your old token is now invalid and cannot be used.
Anyone with the old token will no longer be able to restore your files.</b>"""
            buttons = [[InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='backup_restore_menu')]]
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
            return
        
        elif query.data == "delete_backup_token":
            existing_token = await db.get_backup_token(query.from_user.id)
            if not existing_token:
                await query.answer("❌ No token exists to delete!", show_alert=True)
                return
            
            await db.delete_backup_token(query.from_user.id)
            text = """<b>🗑️ Token Deleted!

Your backup token has been permanently deleted and cannot be recovered.
No one can use it to restore your files anymore.

You can generate a new token anytime from the Backup & Restore menu.</b>"""
            buttons = [[InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='backup_restore_menu')]]
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
            return
        
        elif query.data == "view_all_files" or query.data.startswith("view_all_files_page_"):
            user = await db.col.find_one({'id': int(query.from_user.id)})
            all_files = user.get('stored_files', []) if user else []
            
            # Get page number
            page = 0
            if query.data.startswith("view_all_files_page_"):
                page = int(query.data.split("_")[-1])
            
            items_per_page = 10
            start_idx = page * items_per_page
            end_idx = start_idx + items_per_page
            paginated_files = all_files[start_idx:end_idx]
            
            username = (await client.get_me()).username
            text = f"<b>📄 All Files (Page {page + 1})\n\n</b>"
            if not all_files:
                text += "❌ No files yet"
            else:
                count = start_idx
                for idx, file_obj in enumerate(paginated_files):
                    actual_idx = start_idx + idx
                    file_name = file_obj.get('file_name', 'Unknown')
                    folder = file_obj.get('folder') or 'Unorganized'
                    string = f'file_{actual_idx}'
                    encoded = b64_encode(string)
                    link = f"https://t.me/{username}?start={encoded}"
                    text += f"{actual_idx + 1}. <a href='{link}'>{file_name}</a> <b>[{folder}]</b>\n\n"
            
            buttons = []
            # Add pagination buttons
            if page > 0:
                buttons.append(InlineKeyboardButton('⬅️ Prev', callback_data=f'view_all_files_page_{page - 1}'))
            if end_idx < len(all_files):
                buttons.append(InlineKeyboardButton('Next ➡️', callback_data=f'view_all_files_page_{page + 1}'))
            
            button_rows = [buttons] if buttons else []
            button_rows.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='my_files_menu')])
            
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(button_rows), parse_mode=enums.ParseMode.HTML)
            await query.answer()
            return
        
        elif query.data == "files_by_folder" or query.data.startswith("browse_folder_") or query.data.startswith("folderp:"):
            # Handle folder browsing with subfolder support and pagination
            current_path = None
            page = 0
            
            if query.data.startswith("folderp:"):
                # Format: folderp:{page}:{encoded_path} - using colon delimiter to avoid base64 underscore issues
                parts = query.data.split(":", 2)  # Split into max 3 parts
                if len(parts) == 3:
                    try:
                        page = int(parts[1])
                        encoded_path = parts[2]
                        current_path = b64_decode(encoded_path, "utf-8")
                    except:
                        current_path = None
            elif query.data.startswith("browse_folder_"):
                # Decode the folder path from base64
                encoded_path = query.data[14:]
                try:
                    current_path = b64_decode(encoded_path, "utf-8")
                except:
                    current_path = None
            
            buttons = []
            
            if current_path:
                # Browsing a specific folder - show subfolders and files
                display_name = await db.get_folder_display_name(current_path)
                text = f"<b>📁 {display_name}\n📍 Path: {current_path}\n\n</b>"
                
                # Get files in current folder (not recursive)
                files_in_folder = await db.get_files_by_folder(query.from_user.id, folder=current_path)
                total_files_recursive = await db.get_files_in_folder_recursive(query.from_user.id, current_path)
                
                text += f"📄 Files here: {len(files_in_folder)}\n📂 Total (incl. subfolders): {len(total_files_recursive)}"
                
                # Find folder index for Edit button
                all_folders = await db.get_folders(query.from_user.id)
                folder_idx = None
                for i, f in enumerate(all_folders):
                    fname = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                    if fname == current_path:
                        folder_idx = i
                        break
                
                # 1. Get All Files, Last 5 Files, and Edit buttons on TOP
                action_row = []
                encoded = b64_encode(current_path, "utf-8")
                if total_files_recursive:
                    action_row.append(InlineKeyboardButton('📥 Get All Files', callback_data=f'getall_folder_{encoded}'))
                if total_files_recursive:
                    action_row.append(InlineKeyboardButton('📋 Last 5', callback_data=f'last5_folder_{encoded}'))
                if folder_idx is not None:
                    action_row.append(InlineKeyboardButton('✏️ Edit', callback_data=f'edit_folder_{folder_idx}'))
                if action_row:
                    buttons.append(action_row)
                
                # 2. Separator button with alert - only for root folders
                if '/' not in current_path:
                    buttons.append([InlineKeyboardButton('•••••••••••••••••••••••••••••', callback_data='folder_separator_alert')])
                
                # Get subfolders
                subfolders = await db.get_subfolders(query.from_user.id, current_path)
                
                # 3. List subfolders - 2 per row
                row = []
                for f in subfolders:
                    folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                    sub_display = await db.get_folder_display_name(folder_name)
                    files_in_f = await db.get_files_in_folder_recursive(query.from_user.id, folder_name)
                    # Encode folder path for callback
                    encoded = b64_encode(folder_name, "utf-8")
                    row.append(InlineKeyboardButton(f'📁 {sub_display} ({len(files_in_f)})', callback_data=f'browse_folder_{encoded}'))
                    if len(row) == 2:
                        buttons.append(row)
                        row = []
                if row:
                    buttons.append(row)
                
                # List files directly on this page with pagination
                if files_in_folder:
                    username = (await client.get_me()).username
                    user = await db.col.find_one({'id': int(query.from_user.id)})
                    all_files = user.get('stored_files', []) if user else []
                    
                    items_per_page = 10
                    total_pages = max(1, (len(files_in_folder) + items_per_page - 1) // items_per_page)
                    # Clamp page to valid range
                    page = max(0, min(page, total_pages - 1))
                    start_idx = page * items_per_page
                    end_idx = start_idx + items_per_page
                    display_files = files_in_folder[start_idx:end_idx]
                    
                    text += f"\n\n<b>Files (Page {page + 1}/{total_pages}):</b>\n"
                    for file_obj in display_files:
                        file_name = file_obj.get('file_name', 'Unknown')
                        # Truncate long file names
                        if len(file_name) > 40:
                            file_name = file_name[:37] + "..."
                        # Find file index in all_files
                        file_idx = next((i for i, f in enumerate(all_files) if f.get('file_id') == file_obj.get('file_id')), None)
                        if file_idx is not None:
                            string = f'file_{file_idx}'
                            encoded_file = b64_encode(string)
                            link = f"https://t.me/{username}?start={encoded_file}"
                            text += f"• <a href='{link}'>{file_name}</a>\n"
                        else:
                            text += f"• {file_name}\n"
                    
                    # Add pagination buttons if needed
                    if total_pages > 1:
                        encoded_path = b64_encode(current_path, "utf-8")
                        nav_row = []
                        if page > 0:
                            nav_row.append(InlineKeyboardButton('⬅️ Prev', callback_data=f'folderp:{page - 1}:{encoded_path}'))
                        if page < total_pages - 1:
                            nav_row.append(InlineKeyboardButton('Next ➡️', callback_data=f'folderp:{page + 1}:{encoded_path}'))
                        if nav_row:
                            buttons.append(nav_row)
                
                # 4. Add subfolder creation button ONLY for root folders (no "/" in path) - at the end
                if '/' not in current_path:
                    encoded = b64_encode(current_path, "utf-8")
                    buttons.append([InlineKeyboardButton('➕ Add Subfolder', callback_data=f'add_subfolder_{encoded}')])
                
                # Back button - go to parent folder or root
                if '/' in current_path:
                    parent_path = '/'.join(current_path.split('/')[:-1])
                    parent_encoded = b64_encode(parent_path, "utf-8")
                    buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data=f'browse_folder_{parent_encoded}')])
                else:
                    buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='files_by_folder')])
            else:
                # Root level - show root folders
                text = "<b>📁 Files by Folder\n\n</b>"
                root_folders = await db.get_root_folders(query.from_user.id)
                
                # List root folders - 2 per row
                row = []
                for f in root_folders:
                    folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                    if not folder_name or folder_name.lower() == 'default' or folder_name == 'None':
                        continue
                    files_in_f = await db.get_files_in_folder_recursive(query.from_user.id, folder_name)
                    encoded = b64_encode(folder_name, "utf-8")
                    row.append(InlineKeyboardButton(f'📁 {folder_name} ({len(files_in_f)})', callback_data=f'browse_folder_{encoded}'))
                    if len(row) == 2:
                        buttons.append(row)
                        row = []
                if row:
                    buttons.append(row)
                
                if not buttons:
                    buttons.append([InlineKeyboardButton('No folders yet', callback_data='noop')])
                
                buttons.append([InlineKeyboardButton('➕ Add Folder', callback_data='add_folder_prompt')])
                buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='my_files_menu')])
                text += "Select folder:"
            
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
            return
        
        elif query.data == "files_by_category":
            user = await db.col.find_one({'id': int(query.from_user.id)})
            all_files = user.get('stored_files', []) if user else []
            
            # Group by file type
            categories = {'photo': [], 'video': [], 'audio': [], 'animation': [], 'sticker': [], 'document': []}
            for f in all_files:
                file_type = f.get('file_type', 'document')
                if file_type in categories:
                    categories[file_type].append(f)
            
            # Show all category buttons with counts - 2 per row
            buttons = []
            category_icons = {'photo': '📸', 'video': '🎬', 'audio': '🎵', 'animation': '🎞️', 'sticker': '🎨', 'document': '📄'}
            category_list = ['photo', 'video', 'audio', 'animation', 'sticker', 'document']
            
            row = []
            for cat_type in category_list:
                count = len(categories[cat_type])
                icon = category_icons.get(cat_type, '📌')
                row.append(InlineKeyboardButton(f'{icon} {cat_type.title()} ({count})', callback_data=f'view_category_{cat_type}'))
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)
            
            buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='my_files_menu')])
            await query.message.edit_text("<b>🏷️ Files by Category\n\nSelect category:</b>", reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
            return
        
        elif query.data.startswith("getall_folder_"):
            # Get All Files from folder with flood wait handling
            encoded_path = query.data[14:]
            try:
                folder_path = b64_decode(encoded_path, "utf-8")
            except:
                await query.answer("Error decoding folder path", show_alert=True)
                return
            
            # Get all files recursively from folder
            files = await db.get_files_in_folder_recursive(query.from_user.id, folder_path)
            if not files:
                await query.answer("No files in this folder", show_alert=True)
                return
            
            await query.answer(f"Sending {len(files)} files...", show_alert=False)
            
            # Set stop flag for this operation
            BATCH_STOP_FLAGS[query.from_user.id] = False
            
            buttons = [[InlineKeyboardButton('Stop', callback_data=f'stop_batch_{query.from_user.id}')]]
            sts = await query.message.reply_text(f"<b>Sending {len(files)} files from '{folder_path}'...\n\nPlease wait...</b>", reply_markup=InlineKeyboardMarkup(buttons))
            
            success_count = 0
            error_count = 0
            
            for file_obj in files:
                # Check stop flag
                if BATCH_STOP_FLAGS.get(query.from_user.id, False):
                    await sts.edit(f"<b>Stopped! Sent {success_count} files before stopping.</b>")
                    BATCH_STOP_FLAGS.pop(query.from_user.id, None)
                    return
                
                try:
                    file_id = file_obj.get('file_id')
                    if not file_id:
                        continue
                    
                    msg = await client.get_messages(LOG_CHANNEL, int(file_id))
                    if msg and msg.media:
                        await msg.copy(chat_id=query.from_user.id, protect_content=False)
                        success_count += 1
                        
                        # Update progress every 10 files
                        if success_count % 10 == 0:
                            try:
                                await sts.edit(f"<b>Sending files...\n\nSent: {success_count}/{len(files)}</b>", reply_markup=InlineKeyboardMarkup(buttons))
                            except:
                                pass
                        
                        # Small delay to avoid flood
                        await asyncio.sleep(0.5)
                except FloodWait as e:
                    logger.info(f"FloodWait: sleeping for {e.value} seconds")
                    try:
                        await sts.edit(f"<b>FloodWait - waiting {e.value}s...\n\nSent: {success_count}/{len(files)}</b>", reply_markup=InlineKeyboardMarkup(buttons))
                    except:
                        pass
                    await asyncio.sleep(e.value)
                    # Check stop flag after FloodWait
                    if BATCH_STOP_FLAGS.get(query.from_user.id, False):
                        await sts.edit(f"<b>Stopped! Sent {success_count} files before stopping.</b>")
                        BATCH_STOP_FLAGS.pop(query.from_user.id, None)
                        return
                    # Retry this file
                    try:
                        msg = await client.get_messages(LOG_CHANNEL, int(file_id))
                        if msg and msg.media:
                            await msg.copy(chat_id=query.from_user.id, protect_content=False)
                            success_count += 1
                    except Exception as retry_err:
                        logger.error(f"Retry error: {retry_err}")
                        error_count += 1
                except Exception as e:
                    logger.error(f"Error sending file: {e}")
                    error_count += 1
            
            BATCH_STOP_FLAGS.pop(query.from_user.id, None)
            result_text = f"<b>Completed!\n\nSent: {success_count} files"
            if error_count > 0:
                result_text += f"\nErrors: {error_count}"
            result_text += "</b>"
            await sts.edit(result_text)
            return
        
        elif query.data.startswith("last5_folder_"):
            # Get last 5 files from folder
            encoded_path = query.data[13:]
            try:
                folder_path = b64_decode(encoded_path, "utf-8")
            except:
                await query.answer("Error decoding folder path", show_alert=True)
                return
            
            # Get all files recursively from folder
            all_files = await db.get_files_in_folder_recursive(query.from_user.id, folder_path)
            if not all_files:
                await query.answer("No files in this folder", show_alert=True)
                return
            
            # Get last 5 files
            last_5_files = all_files[-5:] if len(all_files) >= 5 else all_files
            
            await query.answer(f"Sending {len(last_5_files)} files...", show_alert=False)
            
            # Set stop flag for this operation
            BATCH_STOP_FLAGS[query.from_user.id] = False
            
            buttons = [[InlineKeyboardButton('Stop', callback_data=f'stop_batch_{query.from_user.id}')]]
            sts = await query.message.reply_text(f"<b>Sending last {len(last_5_files)} files from '{folder_path}'...\n\nPlease wait...</b>", reply_markup=InlineKeyboardMarkup(buttons))
            
            success_count = 0
            error_count = 0
            
            for file_obj in last_5_files:
                # Check stop flag
                if BATCH_STOP_FLAGS.get(query.from_user.id, False):
                    await sts.edit(f"<b>Stopped! Sent {success_count} files before stopping.</b>")
                    BATCH_STOP_FLAGS.pop(query.from_user.id, None)
                    return
                
                try:
                    file_id = file_obj.get('file_id')
                    if not file_id:
                        continue
                    
                    msg = await client.get_messages(LOG_CHANNEL, int(file_id))
                    if msg and msg.media:
                        await msg.copy(chat_id=query.from_user.id, protect_content=False)
                        success_count += 1
                        await asyncio.sleep(0.5)
                except FloodWait as e:
                    logger.info(f"FloodWait: sleeping for {e.value} seconds")
                    try:
                        await sts.edit(f"<b>FloodWait - waiting {e.value}s...\n\nSent: {success_count}/{len(last_5_files)}</b>", reply_markup=InlineKeyboardMarkup(buttons))
                    except:
                        pass
                    await asyncio.sleep(e.value)
                    if BATCH_STOP_FLAGS.get(query.from_user.id, False):
                        await sts.edit(f"<b>Stopped! Sent {success_count} files before stopping.</b>")
                        BATCH_STOP_FLAGS.pop(query.from_user.id, None)
                        return
                    try:
                        msg = await client.get_messages(LOG_CHANNEL, int(file_id))
                        if msg and msg.media:
                            await msg.copy(chat_id=query.from_user.id, protect_content=False)
                            success_count += 1
                    except Exception as retry_err:
                        logger.error(f"Retry error: {retry_err}")
                        error_count += 1
                except Exception as e:
                    logger.error(f"Error sending file: {e}")
                    error_count += 1
            
            BATCH_STOP_FLAGS.pop(query.from_user.id, None)
            result_text = f"<b>Completed!\n\nSent: {success_count} files"
            if error_count > 0:
                result_text += f"\nErrors: {error_count}"
            result_text += "</b>"
            await sts.edit(result_text)
            return
        
        elif query.data.startswith("shared_folder_") or query.data.startswith("sharedp:"):
            # Handle shared folder browsing with pagination
            owner_id = None
            current_path = None
            page = 0
            
            if query.data.startswith("sharedp:"):
                # Format: sharedp:{page}:{owner_id}:{encoded_path}
                parts = query.data.split(":", 3)
                if len(parts) == 4:
                    try:
                        page = int(parts[1])
                        owner_id = int(parts[2])
                        encoded_path = parts[3]
                        current_path = b64_decode(encoded_path, "utf-8")
                    except:
                        await query.answer("Error decoding folder path", show_alert=True)
                        return
            elif query.data.startswith("shared_folder_"):
                # Format: shared_folder_{owner_id}_{encoded_path}
                remaining = query.data[14:]  # Remove "shared_folder_"
                parts = remaining.split("_", 1)
                if len(parts) >= 2:
                    try:
                        owner_id = int(parts[0])
                        encoded_path = parts[1]
                        current_path = b64_decode(encoded_path, "utf-8")
                    except:
                        await query.answer("Error decoding folder path", show_alert=True)
                        return
            
            if not owner_id or not current_path:
                await query.answer("Invalid folder data", show_alert=True)
                return
            
            # Check if current folder itself is password protected (subfolder can have its own password)
            is_current_protected = await db.is_folder_password_protected(owner_id, current_path)
            current_access_key = f"{query.from_user.id}_{owner_id}_{current_path}"
            
            if is_current_protected and current_access_key not in VERIFIED_FOLDER_ACCESS:
                # This specific folder has its own password - require verification
                encoded_current = b64_encode(current_path, "utf-8")
                display_name = await db.get_folder_display_name(current_path)
                CAPTION_INPUT_MODE[query.from_user.id] = f"verify_folder_password_{owner_id}_{encoded_current}"
                await query.message.reply_text(f"<b>🔐 This folder is password protected</b>\n\n📁 Folder: {display_name}\n\nPlease enter the password to access this folder:", parse_mode=enums.ParseMode.HTML)
                await query.answer()
                return
            
            # Also check if any parent folder in the path is password protected and not verified
            if '/' in current_path:
                path_parts = current_path.split('/')
                for i in range(len(path_parts)):
                    parent_path = '/'.join(path_parts[:i+1])
                    is_parent_protected = await db.is_folder_password_protected(owner_id, parent_path)
                    parent_access_key = f"{query.from_user.id}_{owner_id}_{parent_path}"
                    
                    if is_parent_protected and parent_access_key not in VERIFIED_FOLDER_ACCESS:
                        # Parent folder requires password
                        encoded_parent = b64_encode(parent_path, "utf-8")
                        display_name = await db.get_folder_display_name(parent_path)
                        CAPTION_INPUT_MODE[query.from_user.id] = f"verify_folder_password_{owner_id}_{encoded_parent}"
                        await query.message.reply_text(f"<b>🔐 Parent folder is password protected</b>\n\n📁 Folder: {display_name}\n\nPlease enter the password to access this folder:", parse_mode=enums.ParseMode.HTML)
                        await query.answer()
                        return
            
            # Get folder data
            display_name = await db.get_folder_display_name(current_path)
            files_in_folder = await db.get_files_by_folder(owner_id, folder=current_path)
            total_files_recursive = await db.get_files_in_folder_recursive(owner_id, current_path)
            
            text = f"<b>📁 Shared Folder: {display_name}\n📍 Path: {current_path}\n\n</b>"
            text += f"📄 Files here: {len(files_in_folder)}\n📂 Total (incl. subfolders): {len(total_files_recursive)}"
            
            buttons = []
            
            # Generate share link for Copy folder link button
            username = (await client.get_me()).username
            encoded_path = b64_encode(current_path, "utf-8")
            link_data = f"folder_{owner_id}_{encoded_path}"
            encoded_link = b64_encode(link_data)
            share_link = f"https://t.me/{username}?start={encoded_link}"
            
            # 1. Get All Files button
            action_row = []
            action_row.append(InlineKeyboardButton('📥 Get All Files', callback_data=f'getall_shared_{owner_id}_{encoded_path}'))
            buttons.append(action_row)
            
            # 2. Get subfolders
            subfolders = await db.get_subfolders(owner_id, current_path)
            
            # List subfolders - 2 per row
            row = []
            for f in subfolders:
                sub_folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                sub_display = await db.get_folder_display_name(sub_folder_name)
                sub_encoded = b64_encode(sub_folder_name, "utf-8")
                
                # Check if subfolder has its own password
                sub_access_key = f"{query.from_user.id}_{owner_id}_{sub_folder_name}"
                is_sub_protected = await db.is_folder_password_protected(owner_id, sub_folder_name)
                
                if is_sub_protected and sub_access_key not in VERIFIED_FOLDER_ACCESS:
                    # Show lock icon, hide file count for protected subfolders
                    row.append(InlineKeyboardButton(f'🔒 {sub_display}', callback_data=f'shared_folder_{owner_id}_{sub_encoded}'))
                else:
                    files_in_sub = await db.get_files_in_folder_recursive(owner_id, sub_folder_name)
                    row.append(InlineKeyboardButton(f'📁 {sub_display} ({len(files_in_sub)})', callback_data=f'shared_folder_{owner_id}_{sub_encoded}'))
                
                if len(row) == 2:
                    buttons.append(row)
                    row = []
            if row:
                buttons.append(row)
            
            # 3. List files with pagination
            if files_in_folder:
                items_per_page = 10
                total_pages = max(1, (len(files_in_folder) + items_per_page - 1) // items_per_page)
                page = max(0, min(page, total_pages - 1))
                start_idx = page * items_per_page
                end_idx = start_idx + items_per_page
                display_files = files_in_folder[start_idx:end_idx]
                
                text += f"\n\n<b>Files (Page {page + 1}/{total_pages}):</b>\n"
                for file_obj in display_files:
                    file_name = file_obj.get('file_name', 'Unknown')
                    if len(file_name) > 40:
                        file_name = file_name[:37] + "..."
                    file_id = file_obj.get('file_id')
                    if file_id:
                        file_link_data = f"sharedfile_{owner_id}_{file_id}"
                        encoded_file_link = b64_encode(file_link_data)
                        link = f"https://t.me/{username}?start={encoded_file_link}"
                        text += f"• <a href='{link}'>{file_name}</a>\n"
                    else:
                        text += f"• {file_name}\n"
                
                # Add pagination buttons if needed
                if total_pages > 1:
                    nav_row = []
                    if page > 0:
                        nav_row.append(InlineKeyboardButton('⬅️ Prev', callback_data=f'sharedp:{page - 1}:{owner_id}:{encoded_path}'))
                    if page < total_pages - 1:
                        nav_row.append(InlineKeyboardButton('Next ➡️', callback_data=f'sharedp:{page + 1}:{owner_id}:{encoded_path}'))
                    if nav_row:
                        buttons.append(nav_row)
            
            # Back button - go to parent folder if exists
            if '/' in current_path:
                parent_path = '/'.join(current_path.split('/')[:-1])
                parent_encoded = b64_encode(parent_path, "utf-8")
                buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data=f'shared_folder_{owner_id}_{parent_encoded}')])
            
            # Convert buttons to raw API format and add copy link buttons
            raw_buttons = []
            
            # Add copy link and get last 5 buttons at the top
            last5_callback = f'last5_shared_{owner_id}_{encoded_path}'
            raw_buttons.append([
                {"text": "Copy folder link", "copy_text": {"text": share_link}}, 
                {"text": "📋 Last 5", "callback_data": last5_callback}
            ])
            
            # Convert Pyrogram buttons to raw API format
            for row in buttons:
                raw_row = []
                for btn in row:
                    if btn.callback_data:
                        raw_row.append({"text": btn.text, "callback_data": btn.callback_data})
                    elif btn.url:
                        raw_row.append({"text": btn.text, "url": btn.url})
                if raw_row:
                    raw_buttons.append(raw_row)
            
            # Use raw API to update message with copy link functionality
            api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
            payload = {
                "chat_id": query.from_user.id,
                "message_id": query.message.id,
                "text": text,
                "parse_mode": "HTML",
                "disable_web_page_preview": True,
                "reply_markup": {
                    "inline_keyboard": raw_buttons
                }
            }
            
            async with aiohttp.ClientSession() as session:
                async with session.post(api_url, json=payload) as resp:
                    result = await resp.json()
                    if not result.get("ok"):
                        logger.error(f"Edit message error: {result.get('description')}")
            
            await query.answer()
            return
        
        elif query.data.startswith("getall_shared_"):
            # Get All Files from shared folder with flood wait handling
            remaining = query.data[14:]  # Remove "getall_shared_"
            parts = remaining.split("_", 1)
            if len(parts) < 2:
                await query.answer("Invalid folder data", show_alert=True)
                return
            
            try:
                owner_id = int(parts[0])
                encoded_path = parts[1]
                folder_path = b64_decode(encoded_path, "utf-8")
            except:
                await query.answer("Error decoding folder path", show_alert=True)
                return
            
            # Check if current folder is password protected
            is_protected = await db.is_folder_password_protected(owner_id, folder_path)
            access_key = f"{query.from_user.id}_{owner_id}_{folder_path}"
            
            if is_protected and access_key not in VERIFIED_FOLDER_ACCESS:
                display_name = await db.get_folder_display_name(folder_path)
                encoded_folder = b64_encode(folder_path, "utf-8")
                CAPTION_INPUT_MODE[query.from_user.id] = f"verify_folder_password_{owner_id}_{encoded_folder}"
                await query.message.reply_text(f"<b>🔐 This folder is password protected</b>\n\n📁 Folder: {display_name}\n\nPlease enter the password to access files:", parse_mode=enums.ParseMode.HTML)
                await query.answer()
                return
            
            # Also check parent folders
            if '/' in folder_path:
                path_parts = folder_path.split('/')
                for i in range(len(path_parts)):
                    parent_path = '/'.join(path_parts[:i+1])
                    is_parent_protected = await db.is_folder_password_protected(owner_id, parent_path)
                    parent_access_key = f"{query.from_user.id}_{owner_id}_{parent_path}"
                    
                    if is_parent_protected and parent_access_key not in VERIFIED_FOLDER_ACCESS:
                        display_name = await db.get_folder_display_name(parent_path)
                        encoded_parent = b64_encode(parent_path, "utf-8")
                        CAPTION_INPUT_MODE[query.from_user.id] = f"verify_folder_password_{owner_id}_{encoded_parent}"
                        await query.message.reply_text(f"<b>🔐 Parent folder is password protected</b>\n\n📁 Folder: {display_name}\n\nPlease enter the password to access files:", parse_mode=enums.ParseMode.HTML)
                        await query.answer()
                        return
            
            # Get all files recursively from folder
            all_files = await db.get_files_in_folder_recursive(owner_id, folder_path)
            
            # Filter out files from password-protected subfolders that haven't been verified
            protected_subfolders = await db.get_all_protected_subfolders(owner_id, folder_path)
            files = []
            for file_obj in all_files:
                file_folder = file_obj.get('folder')
                # Check if file is in a protected subfolder that user hasn't verified
                is_in_protected = False
                for pf in protected_subfolders:
                    sub_access_key = f"{query.from_user.id}_{owner_id}_{pf}"
                    if file_folder == pf or (file_folder and file_folder.startswith(f"{pf}/")):
                        if sub_access_key not in VERIFIED_FOLDER_ACCESS:
                            is_in_protected = True
                            break
                if not is_in_protected:
                    files.append(file_obj)
            
            if not files:
                await query.answer("No accessible files in this folder", show_alert=True)
                return
            
            await query.answer(f"Sending {len(files)} files...", show_alert=False)
            
            # Set stop flag for this operation
            BATCH_STOP_FLAGS[query.from_user.id] = False
            
            buttons = [[InlineKeyboardButton('⏹️ Stop', callback_data=f'stop_batch_{query.from_user.id}')]]
            display_name = await db.get_folder_display_name(folder_path)
            sts = await query.message.reply_text(f"<b>📁 Sending {len(files)} files from '{display_name}'...\n\nPlease wait...</b>", reply_markup=InlineKeyboardMarkup(buttons))
            
            success_count = 0
            error_count = 0
            
            for file_obj in files:
                # Check stop flag
                if BATCH_STOP_FLAGS.get(query.from_user.id, False):
                    await sts.edit(f"<b>⏹️ Stopped! Sent {success_count} files before stopping.</b>")
                    BATCH_STOP_FLAGS.pop(query.from_user.id, None)
                    return
                
                try:
                    file_id = file_obj.get('file_id')
                    if not file_id:
                        continue
                    
                    msg = await client.get_messages(LOG_CHANNEL, int(file_id))
                    if msg and msg.media:
                        await msg.copy(chat_id=query.from_user.id, protect_content=False)
                        success_count += 1
                        
                        # Update progress every 10 files
                        if success_count % 10 == 0:
                            try:
                                await sts.edit(f"<b>📁 Sending files from '{display_name}'...\n\nSent: {success_count}/{len(files)}</b>", reply_markup=InlineKeyboardMarkup(buttons))
                            except:
                                pass
                        
                        # Small delay to avoid flood
                        await asyncio.sleep(0.5)
                except FloodWait as e:
                    logger.info(f"FloodWait: sleeping for {e.value} seconds")
                    try:
                        await sts.edit(f"<b>⏳ FloodWait - waiting {e.value}s...\n\nSent: {success_count}/{len(files)}</b>", reply_markup=InlineKeyboardMarkup(buttons))
                    except:
                        pass
                    await asyncio.sleep(e.value)
                    # Check stop flag after FloodWait
                    if BATCH_STOP_FLAGS.get(query.from_user.id, False):
                        await sts.edit(f"<b>⏹️ Stopped! Sent {success_count} files before stopping.</b>")
                        BATCH_STOP_FLAGS.pop(query.from_user.id, None)
                        return
                    # Retry this file
                    try:
                        msg = await client.get_messages(LOG_CHANNEL, int(file_id))
                        if msg and msg.media:
                            await msg.copy(chat_id=query.from_user.id, protect_content=False)
                            success_count += 1
                    except Exception as retry_err:
                        logger.error(f"Retry error: {retry_err}")
                        error_count += 1
                except Exception as e:
                    logger.error(f"Error sending file: {e}")
                    error_count += 1
            
            BATCH_STOP_FLAGS.pop(query.from_user.id, None)
            result_text = f"<b>✅ Completed!\n\n📁 Folder: {display_name}\n📄 Sent: {success_count} files"
            if error_count > 0:
                result_text += f"\n❌ Errors: {error_count}"
            result_text += "</b>"
            await sts.edit(result_text)
            return
        
        elif query.data.startswith("last5_shared_"):
            # Get last 5 files from shared folder
            remaining = query.data[13:]  # Remove "last5_shared_"
            parts = remaining.split("_", 1)
            if len(parts) < 2:
                await query.answer("Invalid folder data", show_alert=True)
                return
            
            try:
                owner_id = int(parts[0])
                encoded_path = parts[1]
                folder_path = b64_decode(encoded_path, "utf-8")
            except:
                await query.answer("Error decoding folder path", show_alert=True)
                return
            
            # Get all files recursively from folder
            all_files = await db.get_files_in_folder_recursive(owner_id, folder_path)
            
            # Filter out files from password-protected subfolders that haven't been verified
            protected_subfolders = await db.get_all_protected_subfolders(owner_id, folder_path)
            files = []
            for file_obj in all_files:
                file_folder = file_obj.get('folder')
                is_in_protected = False
                for pf in protected_subfolders:
                    sub_access_key = f"{query.from_user.id}_{owner_id}_{pf}"
                    if file_folder == pf or (file_folder and file_folder.startswith(f"{pf}/")):
                        if sub_access_key not in VERIFIED_FOLDER_ACCESS:
                            is_in_protected = True
                            break
                if not is_in_protected:
                    files.append(file_obj)
            
            # Get last 5 files
            last_5_files = files[-5:] if len(files) >= 5 else files
            
            if not last_5_files:
                await query.answer("No accessible files in this folder", show_alert=True)
                return
            
            await query.answer(f"Sending {len(last_5_files)} files...", show_alert=False)
            
            # Set stop flag for this operation
            BATCH_STOP_FLAGS[query.from_user.id] = False
            
            buttons = [[InlineKeyboardButton('⏹️ Stop', callback_data=f'stop_batch_{query.from_user.id}')]]
            display_name = await db.get_folder_display_name(folder_path)
            sts = await query.message.reply_text(f"<b>📁 Sending last {len(last_5_files)} files from '{display_name}'...\n\nPlease wait...</b>", reply_markup=InlineKeyboardMarkup(buttons))
            
            success_count = 0
            error_count = 0
            
            for file_obj in last_5_files:
                if BATCH_STOP_FLAGS.get(query.from_user.id, False):
                    await sts.edit(f"<b>⏹️ Stopped! Sent {success_count} files before stopping.</b>")
                    BATCH_STOP_FLAGS.pop(query.from_user.id, None)
                    return
                
                try:
                    file_id = file_obj.get('file_id')
                    if not file_id:
                        continue
                    
                    msg = await client.get_messages(LOG_CHANNEL, int(file_id))
                    if msg and msg.media:
                        await msg.copy(chat_id=query.from_user.id, protect_content=False)
                        success_count += 1
                        await asyncio.sleep(0.5)
                except FloodWait as e:
                    logger.info(f"FloodWait: sleeping for {e.value} seconds")
                    try:
                        await sts.edit(f"<b>⏳ FloodWait - waiting {e.value}s...\n\nSent: {success_count}/{len(last_5_files)}</b>", reply_markup=InlineKeyboardMarkup(buttons))
                    except:
                        pass
                    await asyncio.sleep(e.value)
                    if BATCH_STOP_FLAGS.get(query.from_user.id, False):
                        await sts.edit(f"<b>⏹️ Stopped! Sent {success_count} files before stopping.</b>")
                        BATCH_STOP_FLAGS.pop(query.from_user.id, None)
                        return
                    try:
                        msg = await client.get_messages(LOG_CHANNEL, int(file_id))
                        if msg and msg.media:
                            await msg.copy(chat_id=query.from_user.id, protect_content=False)
                            success_count += 1
                    except Exception as retry_err:
                        logger.error(f"Retry error: {retry_err}")
                        error_count += 1
                except Exception as e:
                    logger.error(f"Error sending file: {e}")
                    error_count += 1
            
            BATCH_STOP_FLAGS.pop(query.from_user.id, None)
            result_text = f"<b>✅ Completed!\n\n📁 Folder: {display_name}\n📄 Sent: {success_count} files"
            if error_count > 0:
                result_text += f"\n❌ Errors: {error_count}"
            result_text += "</b>"
            await sts.edit(result_text)
            return
        
        elif query.data.startswith("share_back_folder_"):
            # Go back to shared folder view
            try:
                remaining = query.data[18:]  # Remove "share_back_folder_"
                parts = remaining.split("_", 1)
                if len(parts) >= 2:
                    owner_id = int(parts[0])
                    encoded_path = parts[1]
                    current_path = b64_decode(encoded_path, "utf-8")
                    
                    # Re-render the shared folder view
                    display_name = await db.get_folder_display_name(current_path)
                    files_in_folder = await db.get_files_by_folder(owner_id, folder=current_path)
                    total_files_recursive = await db.get_files_in_folder_recursive(owner_id, current_path)
                    
                    text = f"<b>📁 Shared Folder: {display_name}\n📍 Path: {current_path}\n\n</b>"
                    text += f"📄 Files here: {len(files_in_folder)}\n📂 Total (incl. subfolders): {len(total_files_recursive)}"
                    
                    buttons = []
                    
                    # Get All Files button
                    action_row = []
                    encoded_path_btn = b64_encode(current_path, "utf-8")
                    action_row.append(InlineKeyboardButton('📥 Get All Files', callback_data=f'getall_shared_{owner_id}_{encoded_path_btn}'))
                    buttons.append(action_row)
                    
                    # Subfolders - 2 per row
                    subfolders = await db.get_subfolders(owner_id, current_path)
                    row = []
                    for f in subfolders:
                        sub_folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                        sub_display = await db.get_folder_display_name(sub_folder_name)
                        sub_encoded = b64_encode(sub_folder_name, "utf-8")
                        
                        # Check if subfolder has its own password
                        sub_access_key = f"{query.from_user.id}_{owner_id}_{sub_folder_name}"
                        is_sub_protected = await db.is_folder_password_protected(owner_id, sub_folder_name)
                        
                        if is_sub_protected and sub_access_key not in VERIFIED_FOLDER_ACCESS:
                            row.append(InlineKeyboardButton(f'🔒 {sub_display}', callback_data=f'shared_folder_{owner_id}_{sub_encoded}'))
                        else:
                            files_in_sub = await db.get_files_in_folder_recursive(owner_id, sub_folder_name)
                            row.append(InlineKeyboardButton(f'📁 {sub_display} ({len(files_in_sub)})', callback_data=f'shared_folder_{owner_id}_{sub_encoded}'))
                        
                        if len(row) == 2:
                            buttons.append(row)
                            row = []
                    if row:
                        buttons.append(row)
                    
                    # Files with pagination
                    if files_in_folder:
                        items_per_page = 10
                        total_pages = max(1, (len(files_in_folder) + items_per_page - 1) // items_per_page)
                        page = 0
                        start_idx = page * items_per_page
                        end_idx = start_idx + items_per_page
                        display_files = files_in_folder[start_idx:end_idx]
                        
                        text += f"\n\n<b>Files (Page {page + 1}/{total_pages}):</b>\n"
                        username = (await client.get_me()).username
                        for file_obj in display_files:
                            file_name = file_obj.get('file_name', 'Unknown')
                            if len(file_name) > 40:
                                file_name = file_name[:37] + "..."
                            file_id = file_obj.get('file_id')
                            if file_id:
                                file_link_data = f"sharedfile_{owner_id}_{file_id}"
                                encoded_file_link = b64_encode(file_link_data)
                                link = f"https://t.me/{username}?start={encoded_file_link}"
                                text += f"• <a href='{link}'>{file_name}</a>\n"
                            else:
                                text += f"• {file_name}\n"
                        
                        # Add pagination buttons if needed
                        if total_pages > 1:
                            nav_row = []
                            if page > 0:
                                nav_row.append(InlineKeyboardButton('⬅️ Prev', callback_data=f'sharedp:{page - 1}:{owner_id}:{encoded_path_btn}'))
                            if page < total_pages - 1:
                                nav_row.append(InlineKeyboardButton('Next ➡️', callback_data=f'sharedp:{page + 1}:{owner_id}:{encoded_path_btn}'))
                            if nav_row:
                                buttons.append(nav_row)
                    
                    # Back button
                    if '/' in current_path:
                        parent_path = '/'.join(current_path.split('/')[:-1])
                        parent_encoded = b64_encode(parent_path, "utf-8")
                        buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data=f'shared_folder_{owner_id}_{parent_encoded}')])
                    
                    # Generate share link for copy
                    username = (await client.get_me()).username
                    link_data = f"folder_{owner_id}_{encoded_path_btn}"
                    encoded_link = b64_encode(link_data)
                    share_link = f"https://t.me/{username}?start={encoded_link}"
                    
                    # Convert buttons to raw API format and add copy link buttons
                    raw_buttons = []
                    
                    # Add copy link and last 5 buttons at the top
                    last5_callback = f'last5_shared_{owner_id}_{encoded_path_btn}'
                    raw_buttons.append([
                        {"text": "Copy folder link", "copy_text": {"text": share_link}}, 
                        {"text": "📋 Last 5", "callback_data": last5_callback}
                    ])
                    
                    # Convert Pyrogram buttons to raw API format
                    for row in buttons:
                        raw_row = []
                        for btn in row:
                            if btn.callback_data:
                                raw_row.append({"text": btn.text, "callback_data": btn.callback_data})
                            elif btn.url:
                                raw_row.append({"text": btn.text, "url": btn.url})
                        if raw_row:
                            raw_buttons.append(raw_row)
                    
                    # Use raw API to update message with copy link functionality
                    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
                    payload = {
                        "chat_id": query.from_user.id,
                        "message_id": query.message.id,
                        "text": text,
                        "parse_mode": "HTML",
                        "disable_web_page_preview": True,
                        "reply_markup": {
                            "inline_keyboard": raw_buttons
                        }
                    }
                    
                    async with aiohttp.ClientSession() as session:
                        async with session.post(api_url, json=payload) as resp:
                            result = await resp.json()
                            if not result.get("ok"):
                                logger.error(f"Edit message error: {result.get('description')}")
                    
                    await query.answer()
            except Exception as e:
                logger.error(f"Share back folder error: {e}")
                await query.answer("Error", show_alert=True)
            return
        
        elif query.data.startswith("getall_category_"):
            # Get All Files from category with flood wait handling
            category = query.data[16:]
            
            user = await db.col.find_one({'id': int(query.from_user.id)})
            all_files = user.get('stored_files', []) if user else []
            
            # Filter by category
            category_files = [f for f in all_files if f.get('file_type', 'document') == category]
            
            if not category_files:
                await query.answer("No files in this category", show_alert=True)
                return
            
            await query.answer(f"Sending {len(category_files)} files...", show_alert=False)
            
            # Set stop flag for this operation
            BATCH_STOP_FLAGS[query.from_user.id] = False
            
            buttons = [[InlineKeyboardButton('Stop', callback_data=f'stop_batch_{query.from_user.id}')]]
            sts = await query.message.reply_text(f"<b>Sending {len(category_files)} {category} files...\n\nPlease wait...</b>", reply_markup=InlineKeyboardMarkup(buttons))
            
            success_count = 0
            error_count = 0
            
            for file_obj in category_files:
                # Check stop flag
                if BATCH_STOP_FLAGS.get(query.from_user.id, False):
                    await sts.edit(f"<b>Stopped! Sent {success_count} files before stopping.</b>")
                    BATCH_STOP_FLAGS.pop(query.from_user.id, None)
                    return
                
                try:
                    file_id = file_obj.get('file_id')
                    if not file_id:
                        continue
                    
                    msg = await client.get_messages(LOG_CHANNEL, int(file_id))
                    if msg and msg.media:
                        await msg.copy(chat_id=query.from_user.id, protect_content=False)
                        success_count += 1
                        
                        # Update progress every 10 files
                        if success_count % 10 == 0:
                            try:
                                await sts.edit(f"<b>Sending files...\n\nSent: {success_count}/{len(category_files)}</b>", reply_markup=InlineKeyboardMarkup(buttons))
                            except:
                                pass
                        
                        # Small delay to avoid flood
                        await asyncio.sleep(0.5)
                except FloodWait as e:
                    logger.info(f"FloodWait: sleeping for {e.value} seconds")
                    try:
                        await sts.edit(f"<b>FloodWait - waiting {e.value}s...\n\nSent: {success_count}/{len(category_files)}</b>", reply_markup=InlineKeyboardMarkup(buttons))
                    except:
                        pass
                    await asyncio.sleep(e.value)
                    # Check stop flag after FloodWait
                    if BATCH_STOP_FLAGS.get(query.from_user.id, False):
                        await sts.edit(f"<b>Stopped! Sent {success_count} files before stopping.</b>")
                        BATCH_STOP_FLAGS.pop(query.from_user.id, None)
                        return
                    # Retry this file
                    try:
                        msg = await client.get_messages(LOG_CHANNEL, int(file_id))
                        if msg and msg.media:
                            await msg.copy(chat_id=query.from_user.id, protect_content=False)
                            success_count += 1
                    except Exception as retry_err:
                        logger.error(f"Retry error: {retry_err}")
                        error_count += 1
                except Exception as e:
                    logger.error(f"Error sending file: {e}")
                    error_count += 1
            
            BATCH_STOP_FLAGS.pop(query.from_user.id, None)
            result_text = f"<b>Completed!\n\nSent: {success_count} files"
            if error_count > 0:
                result_text += f"\nErrors: {error_count}"
            result_text += "</b>"
            await sts.edit(result_text)
            return
        
        elif query.data.startswith("add_subfolder_"):
            # Add subfolder to existing folder
            encoded_path = query.data[14:]
            try:
                parent_folder = b64_decode(encoded_path, "utf-8")
            except:
                await query.answer("Error decoding folder path", show_alert=True)
                return
            
            CAPTION_INPUT_MODE[query.from_user.id] = f"create_subfolder_{parent_folder}"
            keyboard = ReplyKeyboardMarkup(
                [["❌ Cancel"]],
                one_time_keyboard=True,
                resize_keyboard=True
            )
            prompt_msg = await query.message.reply_text(f"<b>📁 Create Subfolder\n\n📍 Parent: {parent_folder}\n\nSend subfolder name:</b>", reply_markup=keyboard)
            FOLDER_PROMPT_MSG[query.from_user.id] = prompt_msg.id
            await query.answer()
            return
        
        elif query.data.startswith("view_category_") or query.data.startswith("view_category_page_"):
            # Handle both initial call and pagination
            if query.data.startswith("view_category_page_"):
                parts = query.data.split("_")
                category = parts[2]
                page = int(parts[-1])
            else:
                category = query.data.split("_")[2]
                page = 0
            
            user = await db.col.find_one({'id': int(query.from_user.id)})
            all_files = user.get('stored_files', []) if user else []
            
            # Filter by category
            category_files = [f for f in all_files if f.get('file_type', 'document') == category]
            
            items_per_page = 10
            start_idx = page * items_per_page
            end_idx = start_idx + items_per_page
            paginated = category_files[start_idx:end_idx]
            
            username = (await client.get_me()).username
            text = f"<b>🏷️ {category.title()} ({len(category_files)} files) - Page {page + 1}\n\n</b>"
            
            if not category_files:
                text += "❌ No files"
            else:
                display_count = 0
                for file_obj in paginated:
                    display_count += 1
                    file_name = file_obj.get('file_name', 'Unknown')
                    # Find index in all_files
                    file_idx = all_files.index(file_obj)
                    string = f'file_{file_idx}'
                    encoded = b64_encode(string)
                    link = f"https://t.me/{username}?start={encoded}"
                    text += f"{start_idx + display_count}. <a href='{link}'>{file_name}</a>\n\n"
            
            buttons = []
            # Add pagination buttons
            if page > 0:
                buttons.append(InlineKeyboardButton('⬅️ Prev', callback_data=f'view_category_page_{category}_{page - 1}'))
            if end_idx < len(category_files):
                buttons.append(InlineKeyboardButton('Next ➡️', callback_data=f'view_category_page_{category}_{page + 1}'))
            
            button_rows = [buttons] if buttons else []
            
            # Add Get All Files button if there are files
            if category_files:
                button_rows.append([InlineKeyboardButton('📥 Get All Files', callback_data=f'getall_category_{category}')])
            
            button_rows.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='files_by_category')])
            
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(button_rows), parse_mode=enums.ParseMode.HTML)
            await query.answer()
            return
        
        elif query.data == "add_folder_prompt":
            try:
                CAPTION_INPUT_MODE[query.from_user.id] = "create_folder"
                # Send reply with keyboard containing cancel button
                keyboard = ReplyKeyboardMarkup(
                    [["❌ Cancel"]],
                    one_time_keyboard=True,
                    resize_keyboard=True
                )
                prompt_msg = await query.message.reply_text("<b>📁 Create Folder\n\nSend folder name:</b>", reply_markup=keyboard)
                # Store the prompt message ID for deletion later
                FOLDER_PROMPT_MSG[query.from_user.id] = prompt_msg.id
                logger.info(f"Folder creation prompt sent to user {query.from_user.id}")
                await query.answer()
            except Exception as e:
                logger.error(f"Error in add_folder_prompt: {e}")
                await query.answer(f"Error: {str(e)[:50]}", show_alert=True)
            return
        
        elif query.data.startswith("view_folder_files_") or query.data.startswith("view_folder_page_"):
            # Handle both initial and pagination
            if query.data.startswith("view_folder_page_"):
                parts = query.data.split("_")
                idx = int(parts[3])
                page = int(parts[-1])
            else:
                idx = int(query.data.split("_")[3])
                page = 0
            
            folders = await db.get_folders(query.from_user.id)
            if not (0 <= idx < len(folders)):
                return
            
            folder_name = folders[idx].get('name', str(folders[idx])) if isinstance(folders[idx], dict) else str(folders[idx])
            user = await db.col.find_one({'id': int(query.from_user.id)})
            all_files = user.get('stored_files', []) if user else []
            
            # Filter files by folder
            folder_files = [(f_idx, f) for f_idx, f in enumerate(all_files) if f.get('folder') == folder_name]
            
            items_per_page = 10
            start_idx = page * items_per_page
            end_idx = start_idx + items_per_page
            paginated = folder_files[start_idx:end_idx]
            
            username = (await client.get_me()).username
            text = f"<b>📁 {folder_name} (Page {page + 1})\n\n</b>"
            
            if not folder_files:
                text += "❌ No files"
            else:
                display_count = 0
                for file_idx, file_obj in paginated:
                    display_count += 1
                    file_name = file_obj.get('file_name', 'Unknown')
                    string = f'file_{file_idx}'
                    encoded = b64_encode(string)
                    link = f"https://t.me/{username}?start={encoded}"
                    text += f"{start_idx + display_count}. <a href='{link}'>{file_name}</a>\n\n"
            
            buttons = []
            # Add pagination buttons
            if page > 0:
                buttons.append(InlineKeyboardButton('⬅️ Prev', callback_data=f'view_folder_page_{idx}_{page - 1}'))
            if end_idx < len(folder_files):
                buttons.append(InlineKeyboardButton('Next ➡️', callback_data=f'view_folder_page_{idx}_{page + 1}'))
            
            button_rows = [buttons] if buttons else []
            
            # Add Get All Files and Edit buttons on same row
            action_row = []
            if folder_files:
                encoded = b64_encode(folder_name, "utf-8")
                action_row.append(InlineKeyboardButton('📥 Get All Files', callback_data=f'getall_folder_{encoded}'))
            action_row.append(InlineKeyboardButton('✏️ Edit', callback_data=f'edit_folder_{idx}'))
            button_rows.append(action_row)
            
            # Back to browse folder
            encoded = b64_encode(folder_name, "utf-8")
            if '/' in folder_name:
                parent_path = '/'.join(folder_name.split('/')[:-1])
                parent_encoded = b64_encode(parent_path, "utf-8")
                button_rows.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data=f'browse_folder_{parent_encoded}')])
            else:
                button_rows.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='files_by_folder')])
            
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(button_rows), parse_mode=enums.ParseMode.HTML)
            await query.answer()
            return
        
        elif query.data == "folder_separator_alert":
            # Show alert when separator is clicked
            await query.answer("📁 Subfolders are listed below", show_alert=True)
            return
        
        elif query.data.startswith("edit_folder_"):
            # Show Edit menu with Rename, Delete and Share options
            idx = int(query.data.split("_")[-1])
            folders = await db.get_folders(query.from_user.id)
            if 0 <= idx < len(folders):
                f = folders[idx]
                folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                display_name = await db.get_folder_display_name(folder_name)
                await show_folder_edit_menu(client, query.from_user.id, query.message.id, idx, folder_name, display_name)
                await query.answer()
            return
        
        elif query.data.startswith("change_folder_link_"):
            # Ask for confirmation before changing link
            idx = int(query.data.split("_")[-1])
            folders = await db.get_folders(query.from_user.id)
            if 0 <= idx < len(folders):
                f = folders[idx]
                folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                display_name = await db.get_folder_display_name(folder_name)
                
                buttons = [
                    [InlineKeyboardButton('✅ Yes, Change Link', callback_data=f'confirm_change_link_{idx}'), 
                     InlineKeyboardButton('❌ Cancel', callback_data=f'cancel_change_link_{idx}')]
                ]
                await query.message.edit_text(f"<b>⚠️ Change folder link?</b>\n\nThis will invalidate the old link and generate a new one.", reply_markup=InlineKeyboardMarkup(buttons))
                await query.answer()
            return
        
        elif query.data.startswith("confirm_change_link_"):
            # Actually change the folder link
            idx = int(query.data.split("_")[-1])
            folders = await db.get_folders(query.from_user.id)
            if 0 <= idx < len(folders):
                f = folders[idx]
                folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                display_name = await db.get_folder_display_name(folder_name)
                
                # Generate new token (invalidates old one)
                await db.change_folder_token(query.from_user.id, folder_name)
                await show_folder_edit_menu(client, query.from_user.id, query.message.id, idx, folder_name, display_name)
                await query.answer("✅ Link changed successfully.\n\nOld links are invalid now.", show_alert=True)
            return
        
        elif query.data.startswith("cancel_change_link_"):
            # Cancel changing link - go back to edit menu
            idx = int(query.data.split("_")[-1])
            folders = await db.get_folders(query.from_user.id)
            if 0 <= idx < len(folders):
                f = folders[idx]
                folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                display_name = await db.get_folder_display_name(folder_name)
                await show_folder_edit_menu(client, query.from_user.id, query.message.id, idx, folder_name, display_name)
                await query.answer()
            return
        
        elif query.data.startswith("share_folder_"):
            # Show folder share link with Copy folder link button (using token-based links)
            idx = int(query.data.split("_")[-1])
            folders = await db.get_folders(query.from_user.id)
            if 0 <= idx < len(folders):
                f = folders[idx]
                folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                
                # Get or generate token-based share link
                token = await db.get_folder_token(query.from_user.id, folder_name)
                if not token:
                    token = await db.generate_folder_token(query.from_user.id, folder_name)
                
                username = (await client.get_me()).username
                share_link = f"https://t.me/{username}?start=folder_{token}"
                
                reply_markup = {
                    "inline_keyboard": [
                        [{"text": "Copy folder link", "copy_text": {"text": share_link}}, {"text": "📥 Open Link", "url": share_link}],
                        [{"text": "🔄 Change Link", "callback_data": f"change_folder_link_{idx}"}],
                        [{"text": "⋞ ʙᴀᴄᴋ", "callback_data": f"edit_folder_{idx}"}]
                    ]
                }
                
                # Just update the buttons
                try:
                    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageReplyMarkup"
                    payload = {
                        "chat_id": query.from_user.id,
                        "message_id": query.message.id,
                        "reply_markup": reply_markup
                    }
                    
                    async with aiohttp.ClientSession() as session:
                        async with session.post(api_url, json=payload) as resp:
                            await resp.json()
                except Exception as e:
                    logger.error(f"Share folder error: {e}")
                
                await query.answer()
            return
        
        elif query.data.startswith("rename_folder_action_"):
            # Rename folder - ask for new name
            idx = int(query.data.split("_")[-1])
            folders = await db.get_folders(query.from_user.id)
            if 0 <= idx < len(folders):
                f = folders[idx]
                old_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                CAPTION_INPUT_MODE[query.from_user.id] = f"rename_folder_idx_{idx}"
                await query.message.reply_text(f"<b>✏️ Rename Folder\n\nCurrent name: <code>{old_name}</code>\n\nSend new name:</b>")
                await query.answer()
            return
        
        elif query.data.startswith("delete_folder_action_"):
            # Delete folder - show confirmation
            idx = int(query.data.split("_")[-1])
            folders = await db.get_folders(query.from_user.id)
            if 0 <= idx < len(folders):
                f = folders[idx]
                folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                buttons = [
                    [InlineKeyboardButton('✅ Yes, Delete', callback_data=f'confirm_delfolder_{idx}'), 
                     InlineKeyboardButton('❌ Cancel', callback_data=f'cancel_delfolder_{idx}')]
                ]
                await query.message.edit_text(f"<b>⚠️ Delete folder '{folder_name}'?</b>\n\nThis action cannot be undone.", reply_markup=InlineKeyboardMarkup(buttons))
                await query.answer()
            return
        
        # ============ UNIFIED PASSWORD HANDLERS ============
        # These handle both files and folders using the same pattern
        
        elif query.data.startswith("set_password_"):
            # Unified: set_password_file_0 or set_password_folder_0
            parts = query.data.replace("set_password_", "").split("_")
            item_type = parts[0]  # 'file' or 'folder'
            idx = int(parts[1])
            
            if item_type == 'file':
                user = await db.col.find_one({'id': int(query.from_user.id)})
                stored_files = user.get('stored_files', []) if user else []
                if 0 <= idx < len(stored_files):
                    file_name = stored_files[idx].get('file_name', 'File')
                    CAPTION_INPUT_MODE[query.from_user.id] = f"set_file_password_idx_{idx}"
                    await query.message.reply_text(f"<b>🔐 Set Password for: {file_name}</b>\n\nSend a password (2-8 characters).\nAnyone accessing this file will need to enter this password.\n\n<i>Send /cancel to cancel</i>", parse_mode=enums.ParseMode.HTML)
                    await query.answer()
            elif item_type == 'folder':
                folders = await db.get_folders(query.from_user.id)
                if 0 <= idx < len(folders):
                    f = folders[idx]
                    folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                    display_name = await db.get_folder_display_name(folder_name)
                    CAPTION_INPUT_MODE[query.from_user.id] = f"set_folder_password_idx_{idx}"
                    await query.message.reply_text(f"<b>🔐 Set Password for: {display_name}</b>\n\nSend the password you want to set.\nAnyone accessing this folder via share link will need to enter this password.\n\n<i>Send /cancel to cancel</i>", parse_mode=enums.ParseMode.HTML)
                    await query.answer()
            return
        
        elif query.data.startswith("view_password_"):
            # Unified: view_password_file_0 or view_password_folder_0
            parts = query.data.replace("view_password_", "").split("_")
            item_type = parts[0]
            idx = int(parts[1])
            
            if item_type == 'file':
                password = await db.get_file_password(query.from_user.id, idx)
                if password:
                    await query.answer(f"🔑 Password: {password}", show_alert=True)
                else:
                    await query.answer("No password set", show_alert=True)
            elif item_type == 'folder':
                folders = await db.get_folders(query.from_user.id)
                if 0 <= idx < len(folders):
                    f = folders[idx]
                    folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                    password = await db.get_folder_password_plain(query.from_user.id, folder_name)
                    if password:
                        await query.answer(f"🔑 Password: {password}", show_alert=True)
                    else:
                        await query.answer("❌ Password was set before this feature. Please remove and re-set.", show_alert=True)
                else:
                    await query.answer("❌ Item not found", show_alert=True)
            return
        
        elif query.data.startswith("confirm_remove_pw_"):
            # Unified: confirm_remove_pw_file_0 or confirm_remove_pw_folder_0
            parts = query.data.replace("confirm_remove_pw_", "").split("_")
            item_type = parts[0]
            idx = int(parts[1])
            
            if item_type == 'file':
                user = await db.col.find_one({'id': int(query.from_user.id)})
                stored_files = user.get('stored_files', []) if user else []
                if 0 <= idx < len(stored_files):
                    file_name = stored_files[idx].get('file_name', 'File')
                    buttons = [
                        [InlineKeyboardButton('✅ Yes, Remove', callback_data=f'remove_password_file_{idx}'), 
                         InlineKeyboardButton('❌ Cancel', callback_data=f'file_share_{idx}')]
                    ]
                    await query.message.edit_text(
                        f"<b>⚠️ Remove Password Protection?</b>\n\n<b>📄 {file_name}</b>\n\nAnyone with the link will be able to access this file without entering a password.",
                        reply_markup=InlineKeyboardMarkup(buttons),
                        parse_mode=enums.ParseMode.HTML
                    )
                    await query.answer()
            elif item_type == 'folder':
                folders = await db.get_folders(query.from_user.id)
                if 0 <= idx < len(folders):
                    f = folders[idx]
                    folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                    display_name = await db.get_folder_display_name(folder_name)
                    buttons = [
                        [InlineKeyboardButton('✅ Yes, Remove', callback_data=f'remove_password_folder_{idx}'), 
                         InlineKeyboardButton('❌ Cancel', callback_data=f'edit_folder_{idx}')]
                    ]
                    await query.message.edit_text(f"<b>⚠️ Remove password from folder '{display_name}'?</b>\n\nAnyone with the share link will be able to access this folder without a password.", reply_markup=InlineKeyboardMarkup(buttons))
                    await query.answer()
            return
        
        elif query.data.startswith("remove_password_"):
            # Unified: remove_password_file_0 or remove_password_folder_0
            parts = query.data.replace("remove_password_", "").split("_")
            item_type = parts[0]
            idx = int(parts[1])
            
            if item_type == 'file':
                success = await db.remove_file_password(query.from_user.id, idx)
                if success:
                    await query.answer("✅ Password removed successfully!", show_alert=True)
                    # Refresh file share menu
                    user = await db.col.find_one({'id': int(query.from_user.id)})
                    stored_files = user.get('stored_files', []) if user else []
                    if 0 <= idx < len(stored_files):
                        file_name = stored_files[idx].get('file_name', 'File')
                        username = (await client.get_me()).username
                        file_token = stored_files[idx].get('access_token')
                        if file_token:
                            string = f'ft_{file_token}'
                        else:
                            string = f'file_{idx}'
                        encoded = b64_encode(string)
                        link = f"https://t.me/{username}?start={encoded}"
                        
                        inline_buttons = [
                            [{"text": "Copy file link", "copy_text": {"text": link}}, {"text": "📥 Open Link", "url": link}],
                            [{"text": "♻️ Change Link", "callback_data": f"change_file_link_{idx}"}]
                        ]
                        inline_buttons.extend(build_password_buttons('file', idx, False))
                        inline_buttons.append([{"text": "⋞ Back", "callback_data": f"share_back_{idx}"}])
                        
                        api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
                        payload = {
                            "chat_id": query.from_user.id,
                            "message_id": query.message.id,
                            "text": f"<b>📤 Share File</b>\n\n<b>📄 {file_name}</b>\n\nShare this file with others using the link below:",
                            "parse_mode": "HTML",
                            "reply_markup": {"inline_keyboard": inline_buttons}
                        }
                        async with aiohttp.ClientSession() as session:
                            await session.post(api_url, json=payload)
                else:
                    await query.answer("Error removing password", show_alert=True)
            elif item_type == 'folder':
                folders = await db.get_folders(query.from_user.id)
                if 0 <= idx < len(folders):
                    f = folders[idx]
                    folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                    display_name = await db.get_folder_display_name(folder_name)
                    await db.remove_folder_password(query.from_user.id, folder_name)
                    await show_folder_edit_menu(client, query.from_user.id, query.message.id, idx, folder_name, display_name, force_is_protected=False)
                    await query.answer("✅ Password deleted successfully!", show_alert=True)
            return
        
        # ============ LEGACY PASSWORD HANDLERS (kept for backward compatibility) ============
        
        elif query.data.startswith("set_folder_password_"):
            idx = int(query.data.split("_")[-1])
            folders = await db.get_folders(query.from_user.id)
            if 0 <= idx < len(folders):
                f = folders[idx]
                folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                display_name = await db.get_folder_display_name(folder_name)
                CAPTION_INPUT_MODE[query.from_user.id] = f"set_folder_password_idx_{idx}"
                await query.message.reply_text(f"<b>🔐 Set Password for: {display_name}</b>\n\nSend the password you want to set for this folder.\nAnyone accessing this folder via share link will need to enter this password.\n\n<i>Send /cancel to cancel</i>", parse_mode=enums.ParseMode.HTML)
                await query.answer()
            return
        
        elif query.data.startswith("view_folder_password_"):
            idx = int(query.data.split("_")[-1])
            folders = await db.get_folders(query.from_user.id)
            if 0 <= idx < len(folders):
                f = folders[idx]
                folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                password = await db.get_folder_password_plain(query.from_user.id, folder_name)
                if password:
                    await query.answer(f"🔑 Password: {password}", show_alert=True)
                else:
                    await query.answer("❌ Password was set before this feature. Please remove and re-set the password to view it.", show_alert=True)
            else:
                await query.answer("❌ Folder not found", show_alert=True)
            return
        
        elif query.data.startswith("confirm_remove_password_"):
            idx = int(query.data.split("_")[-1])
            folders = await db.get_folders(query.from_user.id)
            if 0 <= idx < len(folders):
                f = folders[idx]
                folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                display_name = await db.get_folder_display_name(folder_name)
                buttons = [
                    [InlineKeyboardButton('✅ Yes, Remove', callback_data=f'remove_folder_password_{idx}'), 
                     InlineKeyboardButton('❌ Cancel', callback_data=f'edit_folder_{idx}')]
                ]
                await query.message.edit_text(f"<b>⚠️ Remove password from folder '{display_name}'?</b>\n\nAnyone with the share link will be able to access this folder without a password.", reply_markup=InlineKeyboardMarkup(buttons))
                await query.answer()
            return
        
        elif query.data.startswith("remove_folder_password_"):
            idx = int(query.data.split("_")[-1])
            folders = await db.get_folders(query.from_user.id)
            if 0 <= idx < len(folders):
                f = folders[idx]
                folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                display_name = await db.get_folder_display_name(folder_name)
                await db.remove_folder_password(query.from_user.id, folder_name)
                # Pass force_is_protected=False to ensure button shows "Set Password" immediately
                await show_folder_edit_menu(client, query.from_user.id, query.message.id, idx, folder_name, display_name, force_is_protected=False)
                await query.answer("✅ Password deleted successfully!", show_alert=True)
            return
        
        elif query.data.startswith("folder_"):
            # Click on folder to view files
            idx = int(query.data.split("_")[1])
            folders = await db.get_folders(query.from_user.id)
            if 0 <= idx < len(folders):
                f = folders[idx]
                folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                user = await db.col.find_one({'id': int(query.from_user.id)})
                all_files = user.get('stored_files', []) if user else []
                
                # Filter files by folder
                folder_files = [(f_idx, f) for f_idx, f in enumerate(all_files) if f.get('folder') == folder_name]
                
                items_per_page = 10
                start_idx = 0
                end_idx = start_idx + items_per_page
                paginated = folder_files[start_idx:end_idx]
                
                username = (await client.get_me()).username
                display_name = await db.get_folder_display_name(folder_name)
                text = f"<b>📁 {display_name} (Page 1)\n"
                if '/' in folder_name:
                    text += f"📍 Path: {folder_name}\n"
                text += f"\n</b>"
                
                if not folder_files:
                    text += "No files"
                else:
                    display_count = 0
                    for file_idx, file_obj in paginated:
                        display_count += 1
                        file_name = file_obj.get('file_name', 'Unknown')
                        string = f'file_{file_idx}'
                        encoded = b64_encode(string)
                        link = f"https://t.me/{username}?start={encoded}"
                        text += f"{start_idx + display_count}. <a href='{link}'>{file_name}</a>\n\n"
                
                buttons = []
                # Add pagination buttons
                if end_idx < len(folder_files):
                    buttons.append(InlineKeyboardButton('Next ➡️', callback_data=f'view_folder_page_{idx}_{1}'))
                
                button_rows = [buttons] if buttons else []
                
                # Add Get All Files and Edit buttons on same row
                action_row = []
                if folder_files:
                    folder_encoded = b64_encode(folder_name, "utf-8")
                    action_row.append(InlineKeyboardButton('📥 Get All Files', callback_data=f'getall_folder_{folder_encoded}'))
                action_row.append(InlineKeyboardButton('✏️ Edit', callback_data=f'edit_folder_{idx}'))
                button_rows.append(action_row)
                
                # Back navigation
                if '/' in folder_name:
                    parent_path = '/'.join(folder_name.split('/')[:-1])
                    parent_encoded = b64_encode(parent_path, "utf-8")
                    button_rows.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data=f'browse_folder_{parent_encoded}')])
                else:
                    button_rows.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='files_by_folder')])
                
                await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(button_rows), parse_mode=enums.ParseMode.HTML)
                await query.answer()
            return
        
        elif query.data.startswith("confirm_delfolder_"):
            # Confirm delete folder and show folder list
            try:
                idx = int(query.data.replace("confirm_delfolder_", ""))
                folders = await db.get_folders(query.from_user.id)
                if 0 <= idx < len(folders):
                    f = folders[idx]
                    folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                    await db.delete_folder(query.from_user.id, folder_name)
                    
                    # Now show updated folder list
                    folders = await db.get_folders(query.from_user.id)
                    buttons = []
                    text = "<b>📁 Files by Folder\n\n</b>"
                    
                    row = []
                    for new_idx, new_f in enumerate(folders):
                        new_folder_name = new_f.get('name', str(new_f)) if isinstance(new_f, dict) else str(new_f)
                        # Skip invalid folder names
                        if not new_folder_name or new_folder_name.lower() == 'default' or new_folder_name == 'None':
                            continue
                        files_in_f = await db.get_files_by_folder(query.from_user.id, folder=new_folder_name)
                        row.append(InlineKeyboardButton(f'📁 {new_folder_name} ({len(files_in_f)})', callback_data=f'folder_{new_idx}'))
                        if len(row) == 2:
                            buttons.append(row)
                            row = []
                    
                    if row:
                        buttons.append(row)
                    
                    if not buttons:
                        buttons.append([InlineKeyboardButton('No folders yet', callback_data='noop')])
                    
                    buttons.append([InlineKeyboardButton('➕ Add Folder', callback_data='add_folder_prompt')])
                    buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='my_files_menu')])
                    await query.message.edit_text(text + "Select folder:", reply_markup=InlineKeyboardMarkup(buttons))
                    await query.answer()
            except Exception as e:
                logger.error(f"Error deleting folder: {e}")
                await query.answer(f"Error: {str(e)[:50]}", show_alert=True)
            return
        
        elif query.data.startswith("cancel_delfolder_"):
            # Cancel delete - back to edit folder menu (use shared helper)
            idx = int(query.data.replace("cancel_delfolder_", ""))
            folders = await db.get_folders(query.from_user.id)
            if 0 <= idx < len(folders):
                f = folders[idx]
                folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                display_name = await db.get_folder_display_name(folder_name)
                await show_folder_edit_menu(client, query.from_user.id, query.message.id, idx, folder_name, display_name)
                await query.answer()
            return
        
        elif query.data.startswith("change_file_folder_"):
            # Show folder selection for specific file
            file_idx = int(query.data.split("_")[-1])
            folders = await db.get_folders(query.from_user.id)
            buttons = []
            
            # Add folder options (skip invalid folder names) - 2 per row
            valid_folders = []
            for idx, f in enumerate(folders):
                folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                # Skip invalid folder names
                if not folder_name or folder_name.lower() == 'default' or folder_name == 'None':
                    continue
                valid_folders.append((idx, folder_name))
            
            # Group folders in pairs (2 per row)
            for i in range(0, len(valid_folders), 2):
                row = []
                for idx, folder_name in valid_folders[i:i+2]:
                    row.append(InlineKeyboardButton(
                        f'📁 {folder_name}',
                        callback_data=f'select_file_folder_{file_idx}_{idx}'
                    ))
                buttons.append(row)
            
            if not buttons:
                buttons.append([InlineKeyboardButton('No folders yet', callback_data='noop')])
            
            buttons.append([InlineKeyboardButton('➕ Add Folder', callback_data='add_folder_prompt')])
            buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data=f'back_file_folder_{file_idx}')])
            await query.message.edit_text(f"<b>📁 Select Folder\n\n</b>", reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
            return
        
        elif query.data.startswith("back_file_folder_"):
            # Go back to file view
            file_idx = int(query.data.split("_")[-1])
            user = await db.col.find_one({'id': int(query.from_user.id)})
            stored_files = user.get('stored_files', []) if user else []
            
            if 0 <= file_idx < len(stored_files):
                file_name = stored_files[file_idx].get('file_name', 'File')
                username = (await client.get_me()).username
                string = f'file_{file_idx}'
                encoded = b64_encode(string)
                link = f"https://t.me/{username}?start={encoded}"
                
                protected = stored_files[file_idx].get('protected', False)
                protect_btn = '🛡️✅ Protected' if protected else '🛡️❌ Protect'
                
                buttons = [
                    [InlineKeyboardButton('🔗 Share', callback_data=f'file_share_{file_idx}'), InlineKeyboardButton('📁 Change Folder', callback_data=f'change_file_folder_{file_idx}')],
                    [InlineKeyboardButton(protect_btn, callback_data=f'toggle_protected_{file_idx}'), InlineKeyboardButton('❌ Delete', callback_data=f'delete_file_{file_idx}')],
                    [InlineKeyboardButton('✖️ Close', callback_data=f'close_file_message')]
                ]
                
                caption = f"<b>✅ File saved!</b>\n\n<b>📄 {file_name}</b>"
                await query.message.edit_text(caption, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=enums.ParseMode.HTML)
            await query.answer()
            return
        
        elif query.data.startswith("select_file_folder_"):
            # Save file to selected folder
            parts = query.data.split("_")
            file_idx = int(parts[3])
            folder_idx = int(parts[4])
            
            folders = await db.get_folders(query.from_user.id)
            if 0 <= folder_idx < len(folders):
                f = folders[folder_idx]
                folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
                
                # Update file folder
                await db.update_file_folder(query.from_user.id, file_idx, folder_name)
                await query.answer(f"✅ Moved to folder: {folder_name}", show_alert=True)
            return
        
        elif query.data.startswith("file_share_"):
            # Show share options with buttons and caption
            try:
                file_idx = int(query.data.split("_")[-1])
                user = await db.col.find_one({'id': int(query.from_user.id)})
                stored_files = user.get('stored_files', []) if user else []
                
                if 0 <= file_idx < len(stored_files):
                    file_name = stored_files[file_idx].get('file_name', 'File')
                    username = (await client.get_me()).username
                    
                    # Check if file has a custom token, otherwise use default
                    file_token = stored_files[file_idx].get('access_token')
                    if file_token:
                        string = f'ft_{file_token}'
                    else:
                        string = f'file_{file_idx}'
                    encoded = b64_encode(string)
                    link = f"https://t.me/{username}?start={encoded}"
                    
                    # Check if file is password protected
                    is_password_protected = stored_files[file_idx].get('password') is not None
                    
                    # Build buttons
                    inline_buttons = [
                        [{"text": "Copy file link", "copy_text": {"text": link}}, {"text": "📥 Open Link", "url": link}],
                        [{"text": "♻️ Change Link", "callback_data": f"change_file_link_{file_idx}"}]
                    ]
                    
                    # Use unified password buttons
                    inline_buttons.extend(build_password_buttons('file', file_idx, is_password_protected))
                    
                    inline_buttons.append([{"text": "⋞ Back", "callback_data": f"share_back_{file_idx}"}])
                    
                    protection_status = "🔒 Password Protected" if is_password_protected else ""
                    
                    # Use raw API to support copy_text feature
                    api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageText"
                    payload = {
                        "chat_id": query.from_user.id,
                        "message_id": query.message.id,
                        "text": f"<b>📤 Share File</b>\n\n<b>📄 {file_name}</b>\n{protection_status}\n\nShare this file with others using the link below:",
                        "parse_mode": "HTML",
                        "reply_markup": {"inline_keyboard": inline_buttons}
                    }
                    
                    async with aiohttp.ClientSession() as session:
                        async with session.post(api_url, json=payload) as resp:
                            result = await resp.json()
                            if not result.get("ok"):
                                # Try editMessageCaption for media messages
                                api_url = f"https://api.telegram.org/bot{BOT_TOKEN}/editMessageCaption"
                                payload = {
                                    "chat_id": query.from_user.id,
                                    "message_id": query.message.id,
                                    "caption": f"<b>📤 Share File</b>\n\n<b>📄 {file_name}</b>\n{protection_status}\n\nShare this file with others using the link below:",
                                    "parse_mode": "HTML",
                                    "reply_markup": {"inline_keyboard": inline_buttons}
                                }
                                async with session.post(api_url, json=payload) as resp2:
                                    result2 = await resp2.json()
                                    if not result2.get("ok"):
                                        logger.error(f"Share menu error: {result2.get('description')}")
                                        await query.answer("Error updating share section", show_alert=True)
                                        return
                    
                    await query.answer()
            except Exception as e:
                logger.error(f"Share menu error: {e}")
                await query.answer("Error", show_alert=True)
            return
        
        elif query.data.startswith("share_back_"):
            # Go back to file action buttons
            try:
                file_idx = int(query.data.split("_")[-1])
                user = await db.col.find_one({'id': int(query.from_user.id)})
                stored_files = user.get('stored_files', []) if user else []
                
                if 0 <= file_idx < len(stored_files):
                    file_name = stored_files[file_idx].get('file_name', 'File')
                    protected = stored_files[file_idx].get('protected', False)
                    protect_btn = '🛡️✅ Protected' if protected else '🛡️❌ Protect'
                    
                    buttons = [
                        [InlineKeyboardButton('🔗 Share', callback_data=f'file_share_{file_idx}'), InlineKeyboardButton('📁 Change Folder', callback_data=f'change_file_folder_{file_idx}')],
                        [InlineKeyboardButton(protect_btn, callback_data=f'toggle_protected_{file_idx}'), InlineKeyboardButton('❌ Delete', callback_data=f'delete_file_{file_idx}')],
                        [InlineKeyboardButton('✖️ Close', callback_data=f'close_file_message')]
                    ]
                    
                    caption = f"<b>✅ File saved!</b>\n\n<b>📄 {file_name}</b>"
                    await query.message.edit_text(caption, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=enums.ParseMode.HTML)
                await query.answer()
            except Exception as e:
                logger.error(f"Share back error: {e}")
                await query.answer("Error", show_alert=True)
            return
        
        elif query.data.startswith("delete_file_"):
            # Show confirmation before delete
            try:
                file_idx_str = query.data.split("_")[-1]
                file_idx = int(file_idx_str)
                
                user = await db.col.find_one({'id': int(query.from_user.id)})
                stored_files = user.get('stored_files', []) if user else []
                
                if 0 <= file_idx < len(stored_files):
                    file_name = stored_files[file_idx].get('file_name', 'File')
                    buttons = [
                        [InlineKeyboardButton('✅ Yes, Delete', callback_data=f'confirm_delete_{file_idx}'), 
                         InlineKeyboardButton('❌ Cancel', callback_data=f'cancel_delete_{file_idx}')]
                    ]
                    await query.message.edit_text(f"<b>⚠️ Delete '{file_name}'?</b>\n\nThis action cannot be undone.", reply_markup=InlineKeyboardMarkup(buttons))
                    await query.answer()
                else:
                    await query.answer("File not found", show_alert=True)
            except ValueError:
                await query.answer("Invalid file format", show_alert=True)
            return
        
        elif query.data.startswith("confirm_delete_"):
            # Confirm and delete file
            try:
                file_idx = int(query.data.split("_")[-1])
                user = await db.col.find_one({'id': int(query.from_user.id)})
                stored_files = user.get('stored_files', []) if user else []
                
                if 0 <= file_idx < len(stored_files):
                    file_name = stored_files[file_idx].get('file_name', 'File')
                    file_id = stored_files[file_idx].get('file_id')
                    
                    # Delete from database
                    await db.col.update_one(
                        {'id': int(query.from_user.id)},
                        {'$pull': {'stored_files': {'file_name': file_name, 'file_id': file_id}}}
                    )
                    
                    # Delete the message
                    await query.message.delete()
                    await query.answer("✅ File deleted!", show_alert=False)
                else:
                    await query.answer("File not found", show_alert=True)
            except Exception as e:
                logger.error(f"Delete error: {e}")
                await query.answer("Error deleting file", show_alert=True)
            return
        
        elif query.data.startswith("toggle_protected_"):
            # Toggle file protected status
            try:
                file_idx = int(query.data.split("_")[-1])
                is_protected = await db.toggle_file_protected(query.from_user.id, file_idx)
                
                if is_protected is not None:
                    status = "✅ Protected" if is_protected else "❌ Not Protected"
                    await query.answer(f"🛡️ {status}", show_alert=False)
                    
                    # Refresh buttons with updated protection status
                    user = await db.col.find_one({'id': int(query.from_user.id)})
                    stored_files = user.get('stored_files', []) if user else []
                    
                    if 0 <= file_idx < len(stored_files):
                        file_name = stored_files[file_idx].get('file_name', 'File')
                        username = (await client.get_me()).username
                        string = f'file_{file_idx}'
                        encoded = b64_encode(string)
                        link = f"https://t.me/{username}?start={encoded}"
                        
                        protected = stored_files[file_idx].get('protected', False)
                        protect_btn = '🛡️✅ Protected' if protected else '🛡️❌ Protect'
                        
                        buttons = [
                            [InlineKeyboardButton('🔗 Share', callback_data=f'file_share_{file_idx}'), InlineKeyboardButton('📁 Change Folder', callback_data=f'change_file_folder_{file_idx}')],
                            [InlineKeyboardButton(protect_btn, callback_data=f'toggle_protected_{file_idx}'), InlineKeyboardButton('❌ Delete', callback_data=f'delete_file_{file_idx}')],
                            [InlineKeyboardButton('✖️ Close', callback_data=f'close_file_message')]
                        ]
                        
                        caption = f"<b>✅ File saved!</b>\n\n<b>📄 {file_name}</b>"
                        await query.message.edit_text(caption, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=enums.ParseMode.HTML)
                else:
                    await query.answer("File not found", show_alert=True)
            except Exception as e:
                logger.error(f"Toggle protected error: {e}")
                await query.answer("Error", show_alert=True)
            return
        
        elif query.data.startswith("cancel_delete_"):
            # Cancel delete - show action buttons again
            try:
                file_idx = int(query.data.split("_")[-1])
                user = await db.col.find_one({'id': int(query.from_user.id)})
                stored_files = user.get('stored_files', []) if user else []
                
                if 0 <= file_idx < len(stored_files):
                    file_name = stored_files[file_idx].get('file_name', 'File')
                    username = (await client.get_me()).username
                    string = f'file_{file_idx}'
                    encoded = b64_encode(string)
                    link = f"https://t.me/{username}?start={encoded}"
                    
                    protected = stored_files[file_idx].get('protected', False)
                    protect_btn = '🛡️✅ Protect' if protected else '🛡️❌ Protect'
                    
                    buttons = [
                        [InlineKeyboardButton('🔗 Share', callback_data=f'file_share_{file_idx}'), InlineKeyboardButton('📁 Change Folder', callback_data=f'change_file_folder_{file_idx}')],
                        [InlineKeyboardButton(protect_btn, callback_data=f'toggle_protected_{file_idx}'), InlineKeyboardButton('❌ Delete', callback_data=f'delete_file_{file_idx}')],
                        [InlineKeyboardButton('✖️ Close', callback_data=f'close_file_message')]
                    ]
                    
                    caption = f"<b>✅ File saved!</b>\n\n<b>📄 {file_name}</b>"
                    await query.message.edit_text(caption, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=enums.ParseMode.HTML)
                    await query.answer()
            except Exception as e:
                logger.error(f"Cancel delete error: {e}")
                await query.answer("Error", show_alert=True)
            return
        
        elif query.data.startswith("set_file_password_"):
            # Set password for file - prompt user for password
            try:
                file_idx = int(query.data.split("_")[-1])
                user = await db.col.find_one({'id': int(query.from_user.id)})
                stored_files = user.get('stored_files', []) if user else []
                
                if 0 <= file_idx < len(stored_files):
                    file_name = stored_files[file_idx].get('file_name', 'File')
                    CAPTION_INPUT_MODE[query.from_user.id] = f"set_file_password_idx_{file_idx}"
                    await query.message.reply_text(
                        f"<b>🔐 Set Password for: {file_name}</b>\n\n"
                        f"Send the password you want to set for this file.\n"
                        f"Password must be 2-8 characters.\n"
                        f"Anyone accessing this file via share link will need to enter this password.\n\n"
                        f"<i>Send /cancel to cancel</i>",
                        parse_mode=enums.ParseMode.HTML
                    )
                    await query.answer()
            except Exception as e:
                logger.error(f"Set file password error: {e}")
                await query.answer("Error", show_alert=True)
            return
        
        elif query.data.startswith("show_file_link_"):
            # Show file link in alert (for copying)
            try:
                file_idx = int(query.data.split("_")[-1])
                user = await db.col.find_one({'id': int(query.from_user.id)})
                stored_files = user.get('stored_files', []) if user else []
                
                if 0 <= file_idx < len(stored_files):
                    username = (await client.get_me()).username
                    file_token = stored_files[file_idx].get('access_token')
                    if file_token:
                        string = f'ft_{file_token}'
                    else:
                        string = f'file_{file_idx}'
                    encoded = b64_encode(string)
                    link = f"https://t.me/{username}?start={encoded}"
                    
                    # Send link as a message so user can copy it
                    await query.message.reply_text(f"<b>🔗 Copy this link:</b>\n\n<code>{link}</code>", parse_mode=enums.ParseMode.HTML)
                    await query.answer()
                else:
                    await query.answer("File not found", show_alert=True)
            except Exception as e:
                logger.error(f"Show file link error: {e}")
                await query.answer("Error", show_alert=True)
            return
        
        elif query.data.startswith("view_file_password_"):
            # View file password (show to owner)
            try:
                file_idx = int(query.data.split("_")[-1])
                password = await db.get_file_password(query.from_user.id, file_idx)
                if password:
                    await query.answer(f"🔐 Password: {password}", show_alert=True)
                else:
                    await query.answer("No password set", show_alert=True)
            except Exception as e:
                logger.error(f"View file password error: {e}")
                await query.answer("Error", show_alert=True)
            return
        
        elif query.data.startswith("confirm_remove_file_password_"):
            # Confirm removal of file password
            try:
                file_idx = int(query.data.split("_")[-1])
                user = await db.col.find_one({'id': int(query.from_user.id)})
                stored_files = user.get('stored_files', []) if user else []
                
                if 0 <= file_idx < len(stored_files):
                    file_name = stored_files[file_idx].get('file_name', 'File')
                    
                    buttons = [
                        [InlineKeyboardButton('✅ Yes, Remove', callback_data=f'remove_file_password_{file_idx}'), 
                         InlineKeyboardButton('❌ Cancel', callback_data=f'file_share_{file_idx}')]
                    ]
                    await query.message.edit_text(
                        f"<b>⚠️ Remove Password Protection?</b>\n\n<b>📄 {file_name}</b>\n\nAnyone with the link will be able to access this file without entering a password.",
                        reply_markup=InlineKeyboardMarkup(buttons),
                        parse_mode=enums.ParseMode.HTML
                    )
                    await query.answer()
            except Exception as e:
                logger.error(f"Confirm remove file password error: {e}")
                await query.answer("Error", show_alert=True)
            return
        
        elif query.data.startswith("remove_file_password_"):
            # Remove file password
            try:
                file_idx = int(query.data.split("_")[-1])
                success = await db.remove_file_password(query.from_user.id, file_idx)
                
                if success:
                    await query.answer("✅ Password removed successfully!", show_alert=True)
                    # Go back to main file menu (like folders do)
                    user = await db.col.find_one({'id': int(query.from_user.id)})
                    stored_files = user.get('stored_files', []) if user else []
                    
                    if 0 <= file_idx < len(stored_files):
                        file_name = stored_files[file_idx].get('file_name', 'File')
                        protected = stored_files[file_idx].get('protected', False)
                        protect_btn = '🛡️✅ Protected' if protected else '🛡️❌ Protect'
                        
                        buttons = [
                            [InlineKeyboardButton('🔗 Share', callback_data=f'file_share_{file_idx}'), InlineKeyboardButton('📁 Change Folder', callback_data=f'change_file_folder_{file_idx}')],
                            [InlineKeyboardButton(protect_btn, callback_data=f'toggle_protected_{file_idx}'), InlineKeyboardButton('❌ Delete', callback_data=f'delete_file_{file_idx}')],
                            [InlineKeyboardButton('✖️ Close', callback_data=f'close_file_message')]
                        ]
                        
                        caption = f"<b>✅ File saved!</b>\n\n<b>📄 {file_name}</b>"
                        await query.message.edit_text(caption, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=enums.ParseMode.HTML)
                else:
                    await query.answer("Error removing password", show_alert=True)
            except Exception as e:
                logger.error(f"Remove file password error: {e}")
                await query.answer("Error", show_alert=True)
            return
        
        elif query.data.startswith("change_file_link_"):
            # Ask for confirmation before changing file link
            try:
                file_idx = int(query.data.split("_")[-1])
                user = await db.col.find_one({'id': int(query.from_user.id)})
                stored_files = user.get('stored_files', []) if user else []
                
                if 0 <= file_idx < len(stored_files):
                    file_name = stored_files[file_idx].get('file_name', 'File')
                    
                    buttons = [
                        [InlineKeyboardButton('✅ Yes, Change Link', callback_data=f'confirm_change_file_link_{file_idx}'), 
                         InlineKeyboardButton('❌ Cancel', callback_data=f'file_share_{file_idx}')]
                    ]
                    await query.message.edit_text(
                        f"<b>⚠️ Change File Link?</b>\n\n<b>📄 {file_name}</b>\n\nThis will generate a new link and <b>invalidate the old link</b>.\nAnyone with the old link will no longer be able to access this file.",
                        reply_markup=InlineKeyboardMarkup(buttons),
                        parse_mode=enums.ParseMode.HTML
                    )
                    await query.answer()
            except Exception as e:
                logger.error(f"Change file link error: {e}")
                await query.answer("Error", show_alert=True)
            return
        
        elif query.data.startswith("confirm_change_file_link_"):
            # Generate new token for file
            try:
                file_idx = int(query.data.split("_")[-1])
                new_token = await db.change_file_token(query.from_user.id, file_idx)
                
                if new_token:
                    await query.answer("✅ Link changed successfully!", show_alert=True)
                    
                    # Go back to main file menu (like folders do)
                    user = await db.col.find_one({'id': int(query.from_user.id)})
                    stored_files = user.get('stored_files', []) if user else []
                    
                    if 0 <= file_idx < len(stored_files):
                        file_name = stored_files[file_idx].get('file_name', 'File')
                        protected = stored_files[file_idx].get('protected', False)
                        protect_btn = '🛡️✅ Protected' if protected else '🛡️❌ Protect'
                        
                        buttons = [
                            [InlineKeyboardButton('🔗 Share', callback_data=f'file_share_{file_idx}'), InlineKeyboardButton('📁 Change Folder', callback_data=f'change_file_folder_{file_idx}')],
                            [InlineKeyboardButton(protect_btn, callback_data=f'toggle_protected_{file_idx}'), InlineKeyboardButton('❌ Delete', callback_data=f'delete_file_{file_idx}')],
                            [InlineKeyboardButton('✖️ Close', callback_data=f'close_file_message')]
                        ]
                        
                        caption = f"<b>✅ File saved!</b>\n\n<b>📄 {file_name}</b>"
                        await query.message.edit_text(caption, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=enums.ParseMode.HTML)
                else:
                    await query.answer("Error changing link", show_alert=True)
            except Exception as e:
                logger.error(f"Confirm change file link error: {e}")
                await query.answer("Error", show_alert=True)
            return
        
        elif query.data == "close_file_message":
            # Delete the file message and reply chain
            try:
                # Delete the file message
                await query.message.delete()
                
                # Also try to delete reply-to message if exists
                if query.message.reply_to_message:
                    try:
                        await query.message.reply_to_message.delete()
                    except:
                        pass
            except:
                pass
            await query.answer()
            return
    except Exception as e:
        logger.error(f"Callback error: {e}")
        try:
            await query.answer("Error occurred", show_alert=False)
        except:
            pass


@Client.on_message(filters.private & (filters.document | filters.video | filters.photo | filters.audio | filters.animation | filters.sticker))
async def handle_file_upload(client, message):
    """Handle file uploads and save to LOG_CHANNEL"""
    try:
        user_id = message.from_user.id
        
        # Get the file from message
        file_name = None
        file_type = 'document'  # default
        
        if message.document:
            file_name = message.document.file_name or "Document"
            file_type = 'document'
        elif message.video:
            file_name = message.video.file_name or "Video"
            file_type = 'video'
        elif message.photo:
            file_name = f"Photo_{message.photo.file_id[-6:]}"
            file_type = 'photo'
        elif message.audio:
            file_name = message.audio.file_name or "Audio"
            file_type = 'audio'
        elif message.animation:
            file_name = message.animation.file_name or "Animation"
            file_type = 'animation'
        elif message.sticker:
            file_name = f"Sticker_{message.sticker.file_id[-6:]}"
            file_type = 'sticker'
        else:
            await message.reply_text("❌ Could not process file")
            return
        
        # Forward file to LOG_CHANNEL
        try:
            forwarded_msg = await message.copy(chat_id=LOG_CHANNEL)
            log_message_id = forwarded_msg.id
            logger.info(f"File forwarded to LOG_CHANNEL with message ID: {log_message_id}")
        except Exception as e:
            logger.error(f"Error forwarding to LOG_CHANNEL: {e}")
            await message.reply_text(f"❌ Error saving file: {str(e)[:50]}")
            return
        
        # Save file to database with LOG_CHANNEL message ID
        await db.save_file(user_id, log_message_id, file_name, folder=None, file_type=file_type)
        
        # Generate numeric link (file_N where N is the file index)
        user = await db.col.find_one({'id': int(user_id)})
        stored_files = user.get('stored_files', []) if user else []
        file_index = len(stored_files) - 1  # 0-based index of newly added file
        
        username = (await client.get_me()).username
        string = f'file_{file_index}'
        encoded = b64_encode(string)
        link = f"https://t.me/{username}?start={encoded}"
        
        # Create action buttons with Share containing copy/open options
        buttons = [
            [InlineKeyboardButton('🔗 Share', callback_data=f'file_share_{file_index}'), InlineKeyboardButton('📁 Change Folder', callback_data=f'change_file_folder_{file_index}')],
            [InlineKeyboardButton('🛡️❌ Protect', callback_data=f'toggle_protected_{file_index}'), InlineKeyboardButton('❌ Delete', callback_data=f'delete_file_{file_index}')],
            [InlineKeyboardButton('✖️ Close', callback_data=f'close_file_message')]
        ]
        
        # Build caption with file info
        caption = f"<b>✅ File saved!</b>\n\n"
        caption += f"<b>📄 {file_name}</b>\n"
        caption += f"<b>Type:</b> {file_type}"
        
        # Copy the file as reply with buttons and caption
        try:
            reply_msg = await message.copy(chat_id=message.from_user.id, caption=caption, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=enums.ParseMode.HTML)
        except Exception as e:
            logger.error(f"Error copying file as reply: {e}")
            # Fallback to text reply if copy fails
            reply_msg = await message.reply_text(caption, reply_markup=InlineKeyboardMarkup(buttons), parse_mode=enums.ParseMode.HTML)
        
        # Delete original user file message
        try:
            await message.delete()
        except:
            pass
        
    except Exception as e:
        logger.error(f"File upload handler error: {e}")
        await message.reply_text(f"❌ Error saving file: {str(e)[:50]}")
