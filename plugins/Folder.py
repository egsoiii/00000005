import logging
import asyncio
from pyrogram import Client, filters, enums
from pyrogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup
from pyrogram.errors import FloodWait
from plugins.dbusers import db
from plugins.rawapi import edit_message_with_fallback, send_message_raw, edit_message_text_raw, convert_pyrogram_buttons_to_raw
from plugins.password import build_password_buttons, VERIFIED_FOLDER_ACCESS, CAPTION_INPUT_MODE, PASSWORD_ATTEMPTS, PASSWORD_PROMPT_MESSAGES, PASSWORD_RESPONSE_MESSAGES
from utils import b64_encode, b64_decode
from config import LOG_CHANNEL

logger = logging.getLogger(__name__)

BATCH_STOP_FLAGS = {}
FOLDER_PROMPT_MSG = {}


async def get_folder_name_from_idx(user_id: int, idx: int) -> tuple:
    folders = await db.get_folders(user_id)
    if 0 <= idx < len(folders):
        f = folders[idx]
        folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
        display_name = await db.get_folder_display_name(folder_name)
        return folder_name, display_name, folders
    return None, None, folders


async def get_folder_share_link(client, user_id: int, folder_name: str) -> str:
    token = await db.get_folder_token(user_id, folder_name)
    if not token:
        token = await db.generate_folder_token(user_id, folder_name)
    username = (await client.get_me()).username
    return f"https://t.me/{username}?start=folder_{token}"


async def show_folder_edit_menu(client, user_id: int, message_id: int, idx: int, folder_name: str, display_name: str, force_is_protected=None):
    share_link = await get_folder_share_link(client, user_id, folder_name)
    folder_encoded = b64_encode(folder_name, "utf-8")
    
    if force_is_protected is not None:
        is_protected = force_is_protected
    else:
        is_protected = await db.is_folder_password_protected(user_id, folder_name)
    
    raw_buttons = [
        [{"text": "Copy folder link", "copy_text": {"text": share_link}}, {"text": "♻️ Change Link", "callback_data": f"change_folder_link_{idx}"}],
    ]
    raw_buttons.extend(build_password_buttons('folder', idx, is_protected))
    raw_buttons.append([{"text": "✏️ Rename", "callback_data": f"rename_folder_action_{idx}"}, {"text": "🗑️ Delete", "callback_data": f"delete_folder_action_{idx}"}])
    raw_buttons.append([{"text": "⋞ ʙᴀᴄᴋ", "callback_data": f"browse_folder_{folder_encoded}"}])
    
    protection_status = "🔒 Password Protected" if is_protected else ""
    edit_text = f"<b>✏️ Edit Folder: {display_name}</b>\n{protection_status}\n\nSelect an option:"
    
    await edit_message_with_fallback(user_id, message_id, edit_text, reply_markup=raw_buttons)


async def build_folder_buttons(user_id: int, folders: list, selected: str = None, show_manage: bool = False) -> list:
    buttons = []
    if folders and len(folders) > 0:
        for idx, f in enumerate(folders):
            folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
            marker = "✓" if folder_name == selected else " "
            if show_manage:
                buttons.append([
                    InlineKeyboardButton(f"[{marker}] {folder_name}", callback_data=f"sel_folder_{idx}"),
                    InlineKeyboardButton("✏️", callback_data=f"rename_folder_{idx}"),
                    InlineKeyboardButton("❌", callback_data=f"del_folder_{idx}")
                ])
            else:
                buttons.append([InlineKeyboardButton(f"📁 {folder_name}", callback_data=f"browse_folder_{b64_encode(folder_name, 'utf-8')}")])
    return buttons


async def build_browse_folder_ui(client, user_id: int, current_path: str, page: int = 0) -> tuple:
    display_name = await db.get_folder_display_name(current_path)
    text = f"<b>📁 {display_name}\n📍 Path: {current_path}\n\n</b>"
    
    files_in_folder = await db.get_files_by_folder(user_id, folder=current_path)
    total_files_recursive = await db.get_files_in_folder_recursive(user_id, current_path)
    
    text += f"📄 Files here: {len(files_in_folder)}\n📂 Total (incl. subfolders): {len(total_files_recursive)}"
    
    all_folders = await db.get_folders(user_id)
    folder_idx = None
    for i, f in enumerate(all_folders):
        fname = f.get('name', str(f)) if isinstance(f, dict) else str(f)
        if fname == current_path:
            folder_idx = i
            break
    
    buttons = []
    encoded = b64_encode(current_path, "utf-8")
    
    action_row = []
    if total_files_recursive:
        action_row.append(InlineKeyboardButton('📥 Get All Files', callback_data=f'getall_folder_{encoded}'))
        action_row.append(InlineKeyboardButton('📋 Last 5', callback_data=f'last5_folder_{encoded}'))
    if folder_idx is not None:
        action_row.append(InlineKeyboardButton('✏️ Edit', callback_data=f'edit_folder_{folder_idx}'))
    if action_row:
        buttons.append(action_row)
    
    if '/' not in current_path:
        buttons.append([InlineKeyboardButton('•••••••••••••••••••••••••••••', callback_data='folder_separator_alert')])
    
    subfolders = await db.get_subfolders(user_id, current_path)
    row = []
    for f in subfolders:
        folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
        sub_display = await db.get_folder_display_name(folder_name)
        files_in_f = await db.get_files_in_folder_recursive(user_id, folder_name)
        sub_encoded = b64_encode(folder_name, "utf-8")
        row.append(InlineKeyboardButton(f'📁 {sub_display} ({len(files_in_f)})', callback_data=f'browse_folder_{sub_encoded}'))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    
    if files_in_folder:
        username = (await client.get_me()).username
        user = await db.col.find_one({'id': int(user_id)})
        all_files = user.get('stored_files', []) if user else []
        
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
            file_idx = next((i for i, f in enumerate(all_files) if f.get('file_id') == file_obj.get('file_id')), None)
            if file_idx is not None:
                string = f'file_{file_idx}'
                encoded_file = b64_encode(string)
                link = f"https://t.me/{username}?start={encoded_file}"
                text += f"• <a href='{link}'>{file_name}</a>\n"
            else:
                text += f"• {file_name}\n"
        
        if total_pages > 1:
            encoded_path = b64_encode(current_path, "utf-8")
            nav_row = []
            if page > 0:
                nav_row.append(InlineKeyboardButton('⬅️ Prev', callback_data=f'folderp:{page - 1}:{encoded_path}'))
            if page < total_pages - 1:
                nav_row.append(InlineKeyboardButton('Next ➡️', callback_data=f'folderp:{page + 1}:{encoded_path}'))
            if nav_row:
                buttons.append(nav_row)
    
    if '/' not in current_path:
        buttons.append([InlineKeyboardButton('➕ Add Subfolder', callback_data=f'add_subfolder_{encoded}')])
    
    if '/' in current_path:
        parent_path = '/'.join(current_path.split('/')[:-1])
        parent_encoded = b64_encode(parent_path, "utf-8")
        buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data=f'browse_folder_{parent_encoded}')])
    else:
        buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data='files_by_folder')])
    
    return text, buttons


async def build_shared_folder_ui(client, owner_id: int, current_path: str, viewer_id: int, page: int = 0) -> tuple:
    display_name = await db.get_folder_display_name(current_path)
    files_in_folder = await db.get_files_by_folder(owner_id, folder=current_path)
    total_files_recursive = await db.get_files_in_folder_recursive(owner_id, current_path)
    
    text = f"<b>📁 Shared Folder: {display_name}\n📍 Path: {current_path}\n\n</b>"
    text += f"📄 Files here: {len(files_in_folder)}\n📂 Total (incl. subfolders): {len(total_files_recursive)}"
    
    buttons = []
    username = (await client.get_me()).username
    encoded_path = b64_encode(current_path, "utf-8")
    
    action_row = []
    action_row.append(InlineKeyboardButton('📥 Get All Files', callback_data=f'getall_shared_{owner_id}_{encoded_path}'))
    buttons.append(action_row)
    
    subfolders = await db.get_subfolders(owner_id, current_path)
    row = []
    for f in subfolders:
        sub_folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
        sub_display = await db.get_folder_display_name(sub_folder_name)
        sub_encoded = b64_encode(sub_folder_name, "utf-8")
        
        sub_access_key = f"{viewer_id}_{owner_id}_{sub_folder_name}"
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
        
        if total_pages > 1:
            nav_row = []
            if page > 0:
                nav_row.append(InlineKeyboardButton('⬅️ Prev', callback_data=f'sharedp:{page - 1}:{owner_id}:{encoded_path}'))
            if page < total_pages - 1:
                nav_row.append(InlineKeyboardButton('Next ➡️', callback_data=f'sharedp:{page + 1}:{owner_id}:{encoded_path}'))
            if nav_row:
                buttons.append(nav_row)
    
    if '/' in current_path:
        parent_path = '/'.join(current_path.split('/')[:-1])
        parent_encoded = b64_encode(parent_path, "utf-8")
        buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data=f'shared_folder_{owner_id}_{parent_encoded}')])
    
    link_data = f"folder_{owner_id}_{encoded_path}"
    encoded_link = b64_encode(link_data)
    share_link = f"https://t.me/{username}?start={encoded_link}"
    
    raw_buttons = [[
        {"text": "Copy folder link", "copy_text": {"text": share_link}}, 
        {"text": "📋 Last 5", "callback_data": f"last5_shared_{owner_id}_{encoded_path}"}
    ]]
    raw_buttons.extend(convert_pyrogram_buttons_to_raw(buttons))
    
    return text, raw_buttons


async def send_folder_files(client, user_id: int, files: list, folder_path: str, sts_message, buttons: list):
    success_count = 0
    error_count = 0
    total = len(files)
    
    for file_obj in files:
        if BATCH_STOP_FLAGS.get(user_id, False):
            await sts_message.edit(f"<b>⏹️ Stopped! Sent {success_count} files before stopping.</b>")
            BATCH_STOP_FLAGS.pop(user_id, None)
            return success_count, error_count, True
        
        try:
            file_id = file_obj.get('file_id')
            if not file_id:
                continue
            
            msg = await client.get_messages(LOG_CHANNEL, int(file_id))
            if msg and msg.media:
                await msg.copy(chat_id=user_id, protect_content=False)
                success_count += 1
                
                if success_count % 10 == 0:
                    try:
                        await sts_message.edit(f"<b>Sending files...\n\nSent: {success_count}/{total}</b>", reply_markup=InlineKeyboardMarkup(buttons))
                    except:
                        pass
                
                await asyncio.sleep(0.5)
        except FloodWait as e:
            logger.info(f"FloodWait: sleeping for {e.value} seconds")
            try:
                await sts_message.edit(f"<b>FloodWait - waiting {e.value}s...\n\nSent: {success_count}/{total}</b>", reply_markup=InlineKeyboardMarkup(buttons))
            except:
                pass
            await asyncio.sleep(e.value)
            if BATCH_STOP_FLAGS.get(user_id, False):
                await sts_message.edit(f"<b>⏹️ Stopped! Sent {success_count} files before stopping.</b>")
                BATCH_STOP_FLAGS.pop(user_id, None)
                return success_count, error_count, True
            try:
                msg = await client.get_messages(LOG_CHANNEL, int(file_id))
                if msg and msg.media:
                    await msg.copy(chat_id=user_id, protect_content=False)
                    success_count += 1
            except Exception as retry_err:
                logger.error(f"Retry error: {retry_err}")
                error_count += 1
        except Exception as e:
            logger.error(f"Error sending file: {e}")
            error_count += 1
    
    BATCH_STOP_FLAGS.pop(user_id, None)
    return success_count, error_count, False


async def validate_folder_name(folder_name: str, user_id: int, allow_nested: bool = False) -> tuple:
    if not folder_name:
        return False, "Folder name cannot be empty"
    
    if not allow_nested and '/' in folder_name:
        return False, "Folder name cannot contain '/'. Use the subfolder button to create subfolders."
    
    folders = await db.get_folders(user_id)
    for f in folders:
        existing_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
        if existing_name == folder_name:
            return False, f"Folder '{folder_name}' already exists"
    
    return True, None


async def create_folder_for_user(user_id: int, folder_name: str) -> tuple:
    valid, error = await validate_folder_name(folder_name, user_id)
    if not valid:
        return False, error
    
    await db.create_folder(user_id, folder_name)
    return True, f"Folder created: {folder_name}"


async def create_subfolder_for_user(user_id: int, parent_folder: str, subfolder_name: str) -> tuple:
    if not subfolder_name:
        return False, "Subfolder name cannot be empty"
    
    if '/' in subfolder_name:
        return False, "Subfolder name cannot contain '/'"
    
    full_path = f"{parent_folder}/{subfolder_name}"
    folders = await db.get_folders(user_id)
    for f in folders:
        existing_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
        if existing_name == full_path:
            return False, f"Subfolder '{subfolder_name}' already exists"
    
    await db.create_folder(user_id, full_path)
    return True, full_path


async def delete_folder_for_user(user_id: int, folder_name: str) -> bool:
    await db.delete_folder(user_id, folder_name)
    selected = await db.get_selected_folder(user_id)
    if selected == folder_name:
        await db.set_selected_folder(user_id, None)
    return True


async def rename_folder_for_user(user_id: int, old_name: str, new_name_input: str) -> tuple:
    folders = await db.get_folders(user_id)
    
    if '/' in new_name_input:
        new_name = new_name_input
    elif '/' in old_name:
        parent_path = '/'.join(old_name.split('/')[:-1])
        new_name = f"{parent_path}/{new_name_input}"
    else:
        new_name = new_name_input
    
    if new_name == old_name:
        return False, "Same name, no change"
    
    for f in folders:
        existing_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
        if existing_name == new_name:
            return False, f"Folder '{new_name}' already exists"
    
    await db.rename_folder(user_id, old_name, new_name)
    return True, f"Folder renamed: '{old_name}' → '{new_name}'"


async def build_manage_folders_ui(user_id: int) -> tuple:
    folders = await db.get_folders(user_id)
    selected = await db.get_selected_folder(user_id)
    
    buttons = []
    text = "<b>📁 Manage Folders</b>\n\n"
    
    if folders and len(folders) > 0:
        text += "Your folders:\n\n"
        for idx, f in enumerate(folders):
            folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
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
    
    return text, buttons


async def build_root_folders_ui(client, user_id: int) -> tuple:
    text = "<b>📁 Files by Folder\n\n</b>"
    root_folders = await db.get_root_folders(user_id)
    
    buttons = []
    row = []
    for f in root_folders:
        folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
        if not folder_name or folder_name.lower() == 'default' or folder_name == 'None':
            continue
        files_in_f = await db.get_files_in_folder_recursive(user_id, folder_name)
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
    
    return text, buttons


async def build_change_file_folder_ui(user_id: int, file_idx: int) -> tuple:
    folders = await db.get_folders(user_id)
    user = await db.col.find_one({'id': int(user_id)})
    stored_files = user.get('stored_files', []) if user else []
    
    current_folder = None
    if 0 <= file_idx < len(stored_files):
        current_folder = stored_files[file_idx].get('folder', None)
    
    buttons = []
    valid_folders = []
    for idx, f in enumerate(folders):
        folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
        if not folder_name or folder_name.lower() == 'default' or folder_name == 'None':
            continue
        valid_folders.append((idx, folder_name))
    
    for i in range(0, len(valid_folders), 2):
        row = []
        for idx, folder_name in valid_folders[i:i+2]:
            if current_folder and folder_name == current_folder:
                display_text = f'✅ {folder_name}'
            else:
                display_text = f'📁 {folder_name}'
            row.append(InlineKeyboardButton(
                display_text,
                callback_data=f'select_file_folder_{file_idx}_{idx}'
            ))
        buttons.append(row)
    
    if not buttons:
        buttons.append([InlineKeyboardButton('No folders yet', callback_data='noop')])
    
    buttons.append([InlineKeyboardButton('➕ Add Folder', callback_data='add_folder_prompt')])
    buttons.append([InlineKeyboardButton('⋞ ʙᴀᴄᴋ', callback_data=f'back_file_folder_{file_idx}')])
    
    return "<b>📁 Select Folder\n\n</b>", buttons


@Client.on_message(filters.command("createfolder") & filters.private)
async def create_folder_cmd(client, message):
    if len(message.command) < 2:
        await message.reply_text("<b>📁 Create Folder\n\nUsage: /createfolder [folder_name]</b>")
        return
    
    folder_name = " ".join(message.command[1:])
    success, result = await create_folder_for_user(message.from_user.id, folder_name)
    
    if success:
        await message.reply_text(f"<b>✅ {result}</b>")
    else:
        await message.reply_text(f"<b>❌ {result}</b>")


@Client.on_message(filters.command("listfolders") & filters.private)
async def list_folders_cmd(client, message):
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
    if len(message.command) < 2:
        await message.reply_text("<b>📁 Delete Folder\n\nUsage: /deletefolder [folder_name]</b>")
        return
    
    folder_name = " ".join(message.command[1:])
    folders = await db.get_folders(message.from_user.id)
    
    found = False
    for f in folders:
        fname = f.get('name', str(f)) if isinstance(f, dict) else str(f)
        if fname == folder_name:
            found = True
            break
    
    if not found:
        await message.reply_text(f"<b>❌ Folder '{folder_name}' not found</b>")
        return
    
    await delete_folder_for_user(message.from_user.id, folder_name)
    await message.reply_text(f"<b>✅ Folder deleted: {folder_name}</b>")


@Client.on_message(filters.command("renamefolder") & filters.private)
async def rename_folder_cmd(client, message):
    if len(message.command) < 3:
        await message.reply_text("<b>📁 Rename Folder\n\nUsage: /renamefolder [old_name] [new_name]</b>")
        return
    
    old_name = message.command[1]
    new_name = " ".join(message.command[2:])
    
    if await db.rename_folder(message.from_user.id, old_name, new_name):
        await message.reply_text(f"<b>✅ Renamed: {old_name} → {new_name}</b>")
    else:
        await message.reply_text(f"<b>❌ Folder '{old_name}' not found</b>")
