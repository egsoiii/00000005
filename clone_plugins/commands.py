
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
from utils import verify_user, check_token, check_verification, get_token
from config import *
import re
import json
import base64
from urllib.parse import quote_plus
from core.utils.file_properties import get_name, get_hash, get_media_file_size
from pyrogram.errors import PeerIdInvalid, ChannelInvalid, ChatIdInvalid
logger = logging.getLogger(__name__)

BATCH_FILES = {}
BATCH_STOP_FLAGS = {}  # Track which batches should stop: {user_id: True/False}

def get_size(size):
    """Get size in readable format"""

    units = ["Bytes", "KB", "MB", "GB", "TB", "PB", "EB"]
    size = float(size)
    i = 0
    while size >= 1024.0 and i < len(units):
        i += 1
        size /= 1024.0
    return "%.2f %s" % (size, units[i])

def formate_file_name(file_name):
    chars = ["[", "]", "(", ")"]
    for c in chars:
        file_name.replace(c, "")
    file_name = ' '.join(filter(lambda x: not x.startswith('http') and not x.startswith('@') and not x.startswith('www.'), file_name.split()))
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

async def build_settings_ui(destinations, delivery_mode):
    """Build consistent Settings UI buttons and text"""
    from config import MAX_DESTINATIONS
    
    buttons = []
    
    # Clone status
    clone_status = "✅ Enabled" if CLONE_MODE else "❌ Disabled"
    
    # Add Destination button
    buttons.append([InlineKeyboardButton('➕ Add Destination', callback_data='add_destination')])
    
    # Destinations button
    if destinations:
        buttons.append([InlineKeyboardButton(f'📋 Destinations ({len(destinations)}/{MAX_DESTINATIONS})', callback_data='view_destinations')])
    else:
        buttons.append([InlineKeyboardButton(f'📋 Destinations (0/{MAX_DESTINATIONS})', callback_data='view_destinations')])
    
    # Delivery mode button
    buttons.append([InlineKeyboardButton(f'📨 Mode: {delivery_mode.upper()}', callback_data='delivery_mode')])
    
    # Clone mode button
    buttons.append([InlineKeyboardButton(f'🤖 Clone: {clone_status}', callback_data='toggle_clone_mode')])
    
    # Back button
    buttons.append([InlineKeyboardButton('🏠 Back', callback_data='start')])
    
    text = f"<b>⚙️ Settings\n\n📤 Destinations: {len(destinations)}/{MAX_DESTINATIONS}\n📨 Delivery Mode: {delivery_mode.upper()}\n🤖 Clone Mode: {clone_status}</b>"
    
    return buttons, text

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
            buttons = [[
                InlineKeyboardButton('💝 Help', callback_data='help')
            ],[
                InlineKeyboardButton('🔍 Support', url='https://t.me/premium'),
                InlineKeyboardButton('🤖 Updates', url='https://t.me/premium')
            ],[
                InlineKeyboardButton('😊 About', callback_data='about')
            ],[
                InlineKeyboardButton('⚙️ Settings', callback_data='settings')
            ]]
            
            reply_markup = InlineKeyboardMarkup(buttons)
            me = client.me
            start_text = f"<b>Hello {message.from_user.mention}, My name {me.mention}\n\nI am a File Store Bot with clone features!</b>"
            
            try:
                await message.reply_photo(
                    photo=random.choice(PICS),
                    caption=start_text,
                    reply_markup=reply_markup
                )
            except:
                await message.reply_text(
                    text=start_text,
                    reply_markup=reply_markup
                )
            return
    except Exception as e:
        logger.error(f"Start command error: {e}")
        await message.reply_text("<b>Error processing start command</b>")
        return

    
    data = message.command[1]
    try:
        pre, file_id = data.split('_', 1)
    except:
        file_id = data
        pre = ""
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

    # Check for batch command format (BATCH-xxxx)
    is_batch_command = data.startswith("BATCH-")
    
    # Handle batch command
    if is_batch_command:
        try:
            # Extract the base64 encoded post ID
            batch_id = data.split("BATCH-", 1)[1]
            # Decode from base64
            decoded = base64.urlsafe_b64decode(batch_id + "=" * (-len(batch_id) % 4)).decode("ascii")
            post_id = int(decoded)
            
            # Fetch the batch JSON document from LOG_CHANNEL
            try:
                batch_doc = await client.get_messages(LOG_CHANNEL, post_id)
                
                if batch_doc.document:
                    # Download the JSON file
                    json_file = await client.download_media(batch_doc)
                    
                    with open(json_file, 'r') as f:
                        msgs = json.load(f)
                    
                    # Clean up
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
        except Exception as e:
            logger.error(f"Error decoding batch ID: {e}")
            await message.reply_text("<b>❌ Invalid batch link!</b>")
            return
    else:
        # Regular file link - will be decoded from base64
        msgs = []
    
    try:
        # Only try to parse text data if not a batch command (batch data is already loaded)
        if not is_batch_command:
            text_parts = message.text.split(" ", 1)
            
            if len(text_parts) > 1:
                # Try to parse as JSON first (old format)
                try:
                    msgs = json.loads(text_parts[1])
                except (json.JSONDecodeError, ValueError):
                    # If JSON fails, try to parse as t.me links
                    tme_links = re.findall(r'https://t\.me/([a-zA-Z0-9_]+/\d+|c/\d+/\d+)', text_parts[1])
                    if tme_links:
                        # Convert t.me links to msg items
                        for link_part in tme_links:
                            try:
                                if 'c/' in link_part:
                                    # Group format: c/channel_id/msg_id
                                    parts = link_part.split('/')
                                    channel_id = f"-100{parts[1]}"
                                    msg_id = parts[2]
                                else:
                                    # Public channel format: username/msg_id
                                    parts = link_part.split('/')
                                    username = parts[0]
                                    msg_id = parts[1]
                                    # Get chat ID from username
                                    try:
                                        chat = await client.get_chat(username)
                                        channel_id = str(chat.id)
                                    except:
                                        logger.error(f"Could not find chat: {username}")
                                        continue
                                
                                msgs.append({"channel_id": channel_id, "msg_id": msg_id})
                            except Exception as e:
                                logger.error(f"Error parsing t.me link: {link_part} - {e}")
                                continue
        
        if msgs:
            # Initialize stop flag for this user
            BATCH_STOP_FLAGS[message.from_user.id] = False
            
            buttons = [[InlineKeyboardButton('⏹️ Stop', callback_data=f'stop_batch_{message.from_user.id}')]]
            sts = await message.reply_text("🔄 Processing...", reply_markup=InlineKeyboardMarkup(buttons))
            
            filesarr = []
            success_count = 0
            
            # Get delivery settings
            delivery_mode = await db.get_delivery_mode(message.from_user.id)
            destinations = await db.get_destinations(message.from_user.id)
            
            for msg_item in msgs:
                # Check if stop flag is set
                if BATCH_STOP_FLAGS.get(message.from_user.id, False):
                    await sts.edit(f"⏹️ Batch stopped by user! Sent {success_count} files before stopping.")
                    # Clean up
                    BATCH_STOP_FLAGS.pop(message.from_user.id, None)
                    return
                try:
                    channel_id_str = msg_item.get("channel_id")
                    msgid_str = msg_item.get("msg_id")
                    
                    # Validate data
                    if not channel_id_str or not msgid_str:
                        logger.error(f"Missing channel_id or msg_id: {msg_item}")
                        continue
                    
                    try:
                        channel_id = int(channel_id_str)
                        msgid = int(msgid_str)
                    except (ValueError, TypeError) as e:
                        logger.error(f"Invalid channel_id or msg_id format: {channel_id_str}, {msgid_str} - Error: {e}")
                        continue
                    
                    info = await client.get_messages(channel_id, msgid)
                    
                    f_caption = ""
                    reply_markup = None
                    
                    if info.media:
                        try:
                            file_type = info.media
                            file = getattr(info, file_type.value)
                            f_caption = getattr(info, 'caption', '')
                            if f_caption:
                                f_caption = f"{f_caption.html}"
                            
                            # Send to PM if mode is 'pm' or 'both'
                            if delivery_mode in ['pm', 'both']:
                                try:
                                    sent_msg = await info.copy(chat_id=message.from_user.id, caption=f_caption if f_caption else None, protect_content=False, reply_markup=reply_markup)
                                    filesarr.append(sent_msg)
                                    success_count += 1
                                except FloodWait as e:
                                    await asyncio.sleep(e.value)
                                    sent_msg = await info.copy(chat_id=message.from_user.id, caption=f_caption if f_caption else None, protect_content=False, reply_markup=reply_markup)
                                    filesarr.append(sent_msg)
                                    success_count += 1
                            
                            # Send to enabled destinations if mode is 'channel' or 'both'
                            if delivery_mode in ['channel', 'both'] and destinations:
                                for dest in destinations:
                                    if dest.get('enabled', True):
                                        try:
                                            await info.copy(chat_id=dest['channel_id'], caption=None, protect_content=False, message_thread_id=dest.get('topic_id'))
                                            if delivery_mode == 'channel':
                                                success_count += 1
                                        except Exception as e:
                                            logger.error(f"Error sending to destination: {e}")
                        except Exception as e:
                            logger.error(f"Error copying media: {e}")
                            continue
                    else:
                        try:
                            # Send to PM if mode is 'pm' or 'both'
                            if delivery_mode in ['pm', 'both']:
                                sent_msg = await info.copy(chat_id=message.from_user.id, protect_content=False)
                                filesarr.append(sent_msg)
                                success_count += 1
                            
                            # Send to enabled destinations if mode is 'channel' or 'both'
                            if delivery_mode in ['channel', 'both'] and destinations:
                                for dest in destinations:
                                    if dest.get('enabled', True):
                                        try:
                                            await info.copy(chat_id=dest['channel_id'], protect_content=False, message_thread_id=dest.get('topic_id'))
                                            if delivery_mode == 'channel':
                                                success_count += 1
                                        except Exception as e:
                                            logger.error(f"Error sending to destination: {e}")
                        except Exception as e:
                            logger.error(f"Error copying message: {e}")
                            continue
                    
                    await asyncio.sleep(0.5)
                except Exception as e:
                    logger.error(f"Error processing batch item: {e}")
                    continue
            
            # Remove stop button
            try:
                await sts.edit(f"✅ Sent {success_count} files")
            except:
                pass
            
            # Clean up stop flag
            BATCH_STOP_FLAGS.pop(message.from_user.id, None)
            
            if AUTO_DELETE_MODE == True and filesarr:
                await asyncio.sleep(AUTO_DELETE_TIME)
                for x in filesarr:
                    try:
                        await x.delete()
                    except:
                        pass
    except Exception as e:
        logger.error(f"Batch error: {e}")
        try:
            await sts.edit(f"❌ Error: {str(e)[:50]}")
        except:
            pass
        return

    pre, decode_file_id = ((base64.urlsafe_b64decode(data + "=" * (-len(data) % 4))).decode("ascii")).split("_", 1)
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
            
            # Handle different media types
            if hasattr(media, 'file_name') and media.file_name:
                title = formate_file_name(media.file_name)
                size = get_size(media.file_size)
                f_caption = f"<code>{title}</code>"
            else:
                # For photos and other media without file_name
                f_caption = ""
            
            if CUSTOM_FILE_CAPTION:
                try:
                    f_caption=CUSTOM_FILE_CAPTION.format(file_name= '' if title is None else title, file_size='' if size is None else size, file_caption='')
                except:
                    pass
            
            
            try:
                delivery_mode = await db.get_delivery_mode(message.from_user.id)
                destinations = await db.get_destinations(message.from_user.id)
                
                # Apply delivery mode settings
                if delivery_mode == 'pm':
                    # PM only mode
                    del_msg = await msg.copy(chat_id=message.from_user.id, caption=f_caption if f_caption else None, reply_markup=reply_markup, protect_content=False)
                    await message.reply_text("✅ Sent to your PM!")
                
                elif delivery_mode == 'channel':
                    # Channel only mode - no PM
                    enabled_dests = [d for d in destinations if d.get('enabled', True)]
                    if enabled_dests:
                        success = 0
                        for dest in enabled_dests:
                            try:
                                await msg.copy(chat_id=dest['channel_id'], caption=f_caption if f_caption else None, protect_content=False, message_thread_id=dest.get('topic_id'))
                                success += 1
                            except Exception as e:
                                logger.error(f"Error sending to destination: {e}")
                        await message.reply_text(f"✅ Sent to {success} enabled destination(s)!")
                    else:
                        await message.reply_text("❌ No enabled destinations!")
                    return
                
                else:  # 'both' or default
                    # Send to both PM and destinations
                    if not destinations:
                        del_msg = await msg.copy(chat_id=message.from_user.id, caption=f_caption if f_caption else None, reply_markup=reply_markup, protect_content=False)
                        await message.reply_text("✅ Sent to your PM (no destinations configured)")
                    else:
                        enabled_dests = [d for d in destinations if d.get('enabled', True)]
                        
                        # Send to PM
                        del_msg = await msg.copy(chat_id=message.from_user.id, caption=f_caption if f_caption else None, reply_markup=reply_markup, protect_content=False)
                        
                        # Send to enabled destinations
                        success = 0
                        for dest in enabled_dests:
                            try:
                                await msg.copy(chat_id=dest['channel_id'], caption=None, protect_content=False, message_thread_id=dest.get('topic_id'))
                                success += 1
                            except Exception as e:
                                logger.error(f"Error sending to destination: {e}")
                        
                        msg_text = f"✅ Sent to PM + {success} destination(s)!" if success else "✅ Sent to PM (no enabled destinations)"
                        await message.reply_text(msg_text)
                    
                    return
                
                if AUTO_DELETE_MODE == True and del_msg:
                    await asyncio.sleep(AUTO_DELETE_TIME)
                    try:
                        await del_msg.delete()
                    except:
                        pass
            except FloodWait as e:
                await asyncio.sleep(e.value)
                del_msg = await msg.copy(chat_id=message.from_user.id, caption=f_caption if f_caption else None, reply_markup=reply_markup, protect_content=False)
                if AUTO_DELETE_MODE == True and del_msg:
                    await asyncio.sleep(AUTO_DELETE_TIME)
                    try:
                        await del_msg.delete()
                    except:
                        pass
    except Exception as e:
        logger.error(f"Error: {e}")
        await message.reply_text(f"<b>Error : {str(e)[:50]}</b>")

@Client.on_message(filters.private & filters.text & ~filters.command(["start", "clone", "deletecloned", "batch", "link"]))
async def handle_tme_link(client, message):
    """Handle standalone t.me links from users - but NOT batch commands or link commands"""
    try:
        # Skip if it starts with /batch command
        if message.text.startswith('/batch'):
            return
        
        # Skip if it's a /link command
        if message.text.startswith('/link'):
            return
        
        # Check if message contains t.me link
        if 't.me' not in message.text:
            return
        
        # Single standalone t.me link (not batch)
        # Parse t.me links like https://t.me/username/msg_id or https://t.me/c/channel_id/topic_id/msg_id
        # First try channel format with optional topic: https://t.me/c/channel_id[/topic_id][/msg_id]
        channel_match = re.search(r'https://t\.me/c/(\d+)(?:/(\d+))?(?:/(\d+))?', message.text)
        username_match = re.search(r'https://t\.me/([a-zA-Z0-9_]+)/(\d+)', message.text)
        
        match = channel_match or username_match
        if match:
            sts = await message.reply_text("🔄 Fetching post...")
            
            try:
                chat_id = None
                msg_id = None
                
                if channel_match and channel_match.groups()[0]:  # Channel format
                    channel_id = channel_match.group(1)
                    topic_id = channel_match.group(2) or None
                    msg_id = channel_match.group(3) or channel_match.group(2)  # If no msg_id, use topic_id as msg_id
                    
                    if not msg_id:
                        await sts.edit("❌ Invalid link format. Use: https://t.me/c/CHANNEL_ID/MSG_ID or https://t.me/c/CHANNEL_ID/TOPIC_ID/MSG_ID")
                        return
                    
                    chat_id = int(f"-100{channel_id}")
                    msg_id = int(msg_id)
                
                elif username_match:  # Username format
                    username = username_match.group(1)
                    msg_id = int(username_match.group(2))
                    # Get chat from username
                    try:
                        chat = await client.get_chat(username)
                        chat_id = chat.id
                    except:
                        await sts.edit("❌ Could not find chat/channel")
                        return
                
                # Get the message
                post_msg = await client.get_messages(chat_id, msg_id)
                
                # Get delivery settings
                delivery_mode = await db.get_delivery_mode(message.from_user.id)
                destinations = await db.get_destinations(message.from_user.id)
                
                success = False
                
                # Send to PM if mode is 'pm' or 'both'
                if delivery_mode in ['pm', 'both']:
                    try:
                        await post_msg.copy(chat_id=message.from_user.id, protect_content=False)
                        success = True
                    except Exception as e:
                        logger.error(f"Error sending to PM: {e}")
                
                # Send to enabled destinations if mode is 'channel' or 'both'
                if delivery_mode in ['channel', 'both'] and destinations:
                    for dest in destinations:
                        if dest.get('enabled', True):
                            try:
                                await post_msg.copy(chat_id=dest['channel_id'], caption=None, protect_content=False, message_thread_id=dest.get('topic_id'))
                                success = True
                            except Exception as e:
                                logger.error(f"Error sending to destination: {e}")
                
                if success:
                    await sts.edit("✅ Post sent successfully!")
                else:
                    await sts.edit("❌ Could not send post")
                
            except Exception as e:
                logger.error(f"Error fetching/sending post: {e}")
                await sts.edit(f"❌ Error: {str(e)[:50]}")
            
            return
    
    except Exception as e:
        logger.error(f"Error in handle_tme_link: {e}")

@Client.on_callback_query()
async def callback(client, query):
    try:
        if query.data.startswith("stop_batch_"):
            user_id = int(query.data.split("_")[2])
            if query.from_user.id == user_id:
                BATCH_STOP_FLAGS[user_id] = True
                await query.answer("⏹️ Stopping batch...", show_alert=False)
            else:
                await query.answer("❌ This is not your batch!", show_alert=True)
            return
        
        if query.data == "clone":
            await query.answer()
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
            buttons = [[InlineKeyboardButton('🔙 Back', callback_data='start'), InlineKeyboardButton('❌ Close', callback_data='close_data')]]
            await client.edit_message_media(query.message.chat.id, query.message.id, InputMediaPhoto(random.choice(PICS)))
            await query.message.edit_text(text=script.HELP_TXT, reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
            
        elif query.data == "about":
            buttons = [[InlineKeyboardButton('🔙 Back', callback_data='start'), InlineKeyboardButton('❌ Close', callback_data='close_data')]]
            await client.edit_message_media(query.message.chat.id, query.message.id, InputMediaPhoto(random.choice(PICS)))
            me2 = (await client.get_me()).mention
            await query.message.edit_text(text=script.ABOUT_TXT.format(me2), reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
            
        elif query.data == "start":
            buttons = [
                [InlineKeyboardButton('💝 Help', callback_data='help')],
                [InlineKeyboardButton('🔍 Support', url='https://t.me/premium'), InlineKeyboardButton('🤖 Updates', url='https://t.me/premium')],
                [InlineKeyboardButton('😊 About', callback_data='about')],
                [InlineKeyboardButton('⚙️ Settings', callback_data='settings')]
            ]
            await client.edit_message_media(query.message.chat.id, query.message.id, InputMediaPhoto(random.choice(PICS)))
            me2 = (await client.get_me()).mention
            await query.message.edit_text(text=script.START_TXT.format(query.from_user.mention, me2), reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
        
        elif query.data == "settings":
            destinations = await db.get_destinations(query.from_user.id)
            delivery_mode = await db.get_delivery_mode(query.from_user.id)
            buttons, text = await build_settings_ui(destinations, delivery_mode)
            await client.edit_message_media(query.message.chat.id, query.message.id, InputMediaPhoto(random.choice(PICS)))
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
        
        elif query.data == "view_destinations":
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
            
            buttons.append([InlineKeyboardButton('➕ Add Destination', callback_data='add_destination')])
            buttons.append([InlineKeyboardButton('🔙 Back', callback_data='settings')])
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
        
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
            
            try:
                chat = await client.get_chat(channel_id)
                dest_name = chat.title
            except:
                dest_name = f"Chat {channel_id}"
            
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
            
            buttons.append([InlineKeyboardButton('🔙 Back', callback_data='view_destinations')])
            
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
                            was_added = await db.add_destination(query.from_user.id, chat_id, dest_type, topic_id)
                            await user_input.delete()
                            
                            if not chat_title:
                                try:
                                    chat_obj = await client.get_chat(chat_id)
                                    chat_title = chat_obj.title
                                except:
                                    chat_title = f"Chat {chat_id}"
                            
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
                
                buttons.append([InlineKeyboardButton('🔙 Back', callback_data='view_destinations')])
                
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
                    [InlineKeyboardButton('🔙 Back', callback_data='view_destinations')]
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
            buttons.append([InlineKeyboardButton('🔙 Back', callback_data='settings')])
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
                [InlineKeyboardButton('🔙 Back', callback_data='view_destinations')]
            ]
            
            await query.message.edit_text(text, reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer(f"Status changed to {status_msg}!", show_alert=False)
            return
        
        
        
        elif query.data == "delivery_mode":
            buttons = [
                [InlineKeyboardButton('📨 PM Only', callback_data='mode_pm')],
                [InlineKeyboardButton('📤 Channel Only', callback_data='mode_channel')],
                [InlineKeyboardButton('📨📤 Both', callback_data='mode_both')],
                [InlineKeyboardButton('🔙 Back', callback_data='settings')]
            ]
            await query.message.edit_text("<b>📨 Select Delivery Mode:</b>", reply_markup=InlineKeyboardMarkup(buttons))
            await query.answer()
        
        elif query.data.startswith("mode_"):
            mode = query.data.split("_")[1]
            await db.set_delivery_mode(query.from_user.id, mode)
            mode_text = {"pm": "PM Only", "channel": "Channel Only", "both": "Both"}
            await query.message.edit_text(f"<b>✅ Delivery mode set to: {mode_text.get(mode)}</b>")
            await query.answer()
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
    except Exception as e:
        logger.error(f"Callback error: {e}")
        try:
            await query.answer("Error occurred", show_alert=False)
        except:
            pass
