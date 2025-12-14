import logging
from pyrogram import enums
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from plugins.dbusers import db
from plugins.rawapi import edit_message_text_raw
from utils import b64_encode, b64_decode
from config import LOG_CHANNEL

logger = logging.getLogger(__name__)

CAPTION_INPUT_MODE = {}
VERIFIED_FOLDER_ACCESS = {}
PASSWORD_ATTEMPTS = {}
PASSWORD_PROMPT_MESSAGES = {}
PASSWORD_RESPONSE_MESSAGES = {}


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


async def handle_set_password_callback(client, query, item_type, idx):
    """Handle set_password_ callback for both files and folders"""
    user_id = query.from_user.id
    
    if item_type == 'file':
        user = await db.col.find_one({'id': int(user_id)})
        stored_files = user.get('stored_files', []) if user else []
        if 0 <= idx < len(stored_files):
            file_name = stored_files[idx].get('file_name', 'File')
            CAPTION_INPUT_MODE[user_id] = f"set_file_password_idx_{idx}"
            await query.message.reply_text(
                f"<b>🔐 Set Password for: {file_name}</b>\n\nSend a password (2-8 characters).\nAnyone accessing this file will need to enter this password.\n\n<i>Send /cancel to cancel</i>",
                parse_mode=enums.ParseMode.HTML
            )
            await query.answer()
    elif item_type == 'folder':
        folders = await db.get_folders(user_id)
        if 0 <= idx < len(folders):
            f = folders[idx]
            folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
            display_name = await db.get_folder_display_name(folder_name)
            CAPTION_INPUT_MODE[user_id] = f"set_folder_password_idx_{idx}"
            await query.message.reply_text(
                f"<b>🔐 Set Password for: {display_name}</b>\n\nSend the password you want to set.\nAnyone accessing this folder via share link will need to enter this password.\n\n<i>Send /cancel to cancel</i>",
                parse_mode=enums.ParseMode.HTML
            )
            await query.answer()


async def handle_view_password_callback(query, item_type, idx):
    """Handle view_password_ callback for both files and folders"""
    user_id = query.from_user.id
    
    if item_type == 'file':
        password = await db.get_file_password(user_id, idx)
        if password:
            await query.answer(f"🔑 Password: {password}", show_alert=True)
        else:
            await query.answer("No password set", show_alert=True)
    elif item_type == 'folder':
        folders = await db.get_folders(user_id)
        if 0 <= idx < len(folders):
            f = folders[idx]
            folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
            password = await db.get_folder_password_plain(user_id, folder_name)
            if password:
                await query.answer(f"🔑 Password: {password}", show_alert=True)
            else:
                await query.answer("❌ Password was set before this feature. Please remove and re-set.", show_alert=True)
        else:
            await query.answer("❌ Item not found", show_alert=True)


async def handle_confirm_remove_password_callback(query, item_type, idx):
    """Handle confirm_remove_pw_ callback for both files and folders"""
    user_id = query.from_user.id
    
    if item_type == 'file':
        user = await db.col.find_one({'id': int(user_id)})
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
        folders = await db.get_folders(user_id)
        if 0 <= idx < len(folders):
            f = folders[idx]
            folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
            display_name = await db.get_folder_display_name(folder_name)
            buttons = [
                [InlineKeyboardButton('✅ Yes, Remove', callback_data=f'remove_password_folder_{idx}'), 
                 InlineKeyboardButton('❌ Cancel', callback_data=f'edit_folder_{idx}')]
            ]
            await query.message.edit_text(
                f"<b>⚠️ Remove password from folder '{display_name}'?</b>\n\nAnyone with the share link will be able to access this folder without a password.",
                reply_markup=InlineKeyboardMarkup(buttons)
            )
            await query.answer()


async def handle_remove_password_callback(client, query, item_type, idx, show_folder_edit_menu_func):
    """Handle remove_password_ callback for both files and folders
    
    show_folder_edit_menu_func: callback function to show folder edit menu after removal
    """
    user_id = query.from_user.id
    
    if item_type == 'file':
        success = await db.remove_file_password(user_id, idx)
        if success:
            await query.answer("✅ Password removed successfully!", show_alert=True)
            user = await db.col.find_one({'id': int(user_id)})
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
                
                await edit_message_text_raw(
                    user_id, query.message.id,
                    f"<b>📤 Share File</b>\n\n<b>📄 {file_name}</b>\n\nShare this file with others using the link below:",
                    reply_markup=inline_buttons
                )
        else:
            await query.answer("Error removing password", show_alert=True)
    elif item_type == 'folder':
        folders = await db.get_folders(user_id)
        if 0 <= idx < len(folders):
            f = folders[idx]
            folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
            display_name = await db.get_folder_display_name(folder_name)
            await db.remove_folder_password(user_id, folder_name)
            await show_folder_edit_menu_func(client, user_id, query.message.id, idx, folder_name, display_name, force_is_protected=False)
            await query.answer("✅ Password deleted successfully!", show_alert=True)


async def handle_set_folder_password_message(message, idx):
    """Handle folder password setting from message input"""
    try:
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


async def handle_set_file_password_message(message, idx):
    """Handle file password setting from message input"""
    try:
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


async def handle_verify_file_password(client, message, owner_id, file_idx):
    """Handle file password verification from message input"""
    try:
        password = message.text.strip()
        
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
            
            user_data = await db.col.find_one({'id': int(owner_id)})
            stored_files = user_data.get('stored_files', []) if user_data else []
            
            if 0 <= file_idx < len(stored_files):
                file_obj = stored_files[file_idx]
                file_id = file_obj.get('file_id')
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


async def handle_verify_folder_password(client, message, owner_id, encoded_folder):
    """Handle folder password verification from message input
    
    Returns: True if password is valid, False otherwise
    """
    try:
        password = message.text.strip()
        folder_name = b64_decode(encoded_folder, "utf-8")
        
        attempt_key = f"{message.from_user.id}_{owner_id}_{folder_name}"
        attempts = PASSWORD_ATTEMPTS.get(attempt_key, 0) + 1
        PASSWORD_ATTEMPTS[attempt_key] = attempts
        
        is_valid = await db.verify_folder_password(owner_id, folder_name, password)
        
        if is_valid:
            CAPTION_INPUT_MODE[message.from_user.id] = False
            PASSWORD_ATTEMPTS.pop(attempt_key, None)
            
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
            return True
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
            return False
    except Exception as e:
        logger.error(f"Error verifying folder password: {e}")
        await message.reply_text(f"<b>❌ Error: {str(e)[:50]}</b>")
        CAPTION_INPUT_MODE[message.from_user.id] = False
        return False


def get_folder_access_key(user_id, owner_id, folder_name):
    """Generate a key for tracking verified folder access"""
    return f"{user_id}_{owner_id}_{folder_name}"


def get_file_access_key(user_id, owner_id, file_idx):
    """Generate a key for tracking verified file access"""
    return f"file_{user_id}_{owner_id}_{file_idx}"


def is_folder_access_verified(user_id, owner_id, folder_name):
    """Check if user has verified access to a folder"""
    key = get_folder_access_key(user_id, owner_id, folder_name)
    return VERIFIED_FOLDER_ACCESS.get(key, False)


def is_file_access_verified(user_id, owner_id, file_idx):
    """Check if user has verified access to a file"""
    key = get_file_access_key(user_id, owner_id, file_idx)
    return VERIFIED_FOLDER_ACCESS.get(key, False)


def set_password_input_mode(user_id, mode):
    """Set the password input mode for a user"""
    CAPTION_INPUT_MODE[user_id] = mode


def get_password_input_mode(user_id):
    """Get the password input mode for a user"""
    return CAPTION_INPUT_MODE.get(user_id, False)


def clear_password_input_mode(user_id):
    """Clear the password input mode for a user"""
    CAPTION_INPUT_MODE[user_id] = False


def track_password_prompt_message(user_id, msg_id):
    """Track a password prompt message ID for cleanup"""
    if user_id not in PASSWORD_PROMPT_MESSAGES:
        PASSWORD_PROMPT_MESSAGES[user_id] = []
    PASSWORD_PROMPT_MESSAGES[user_id].append(msg_id)


def track_password_response_message(user_id, msg_id):
    """Track a password response message ID for cleanup"""
    if user_id not in PASSWORD_RESPONSE_MESSAGES:
        PASSWORD_RESPONSE_MESSAGES[user_id] = []
    PASSWORD_RESPONSE_MESSAGES[user_id].append(msg_id)
