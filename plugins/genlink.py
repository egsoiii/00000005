
import re
from pyrogram import filters, Client, enums
from pyrogram.errors.exceptions.bad_request_400 import ChannelInvalid, UsernameInvalid, UsernameNotModified
from config import ADMINS, LOG_CHANNEL, PUBLIC_FILE_STORE, WEBSITE_URL, WEBSITE_URL_MODE
import os
import json
import base64


async def allowed(_, __, message):
    if PUBLIC_FILE_STORE:
        return True
    if message.from_user and message.from_user.id in ADMINS:
        return True
    return False


@Client.on_message((filters.document | filters.video | filters.audio | filters.photo) & filters.private & filters.create(allowed))
async def incoming_gen_link(bot, message):
    from plugins.dbusers import db
    username = (await bot.get_me()).username
    file_type = message.media
    post = await message.copy(LOG_CHANNEL, caption=None)
    file_id = str(post.id)
    string = 'file_'
    string += file_id
    outstr = base64.urlsafe_b64encode(string.encode("ascii")).decode().strip("=")
    if WEBSITE_URL_MODE == True:
        share_link = f"{WEBSITE_URL}?file={outstr}"
    else:
        share_link = f"https://t.me/{username}?start={outstr}"
    
    # Save file to selected folder
    try:
        if await db.is_user_exist(message.from_user.id):
            selected_folder = await db.get_selected_folder(message.from_user.id)
            file_name = getattr(message.document or message.video or message.audio or message.photo, 'file_name', f'file_{file_id}')
            await db.save_file(message.from_user.id, file_id, file_name, folder=selected_folder)
    except Exception as e:
        pass
    
    await message.reply(f"<b>⭕ ʜᴇʀᴇ ɪs ʏᴏᴜʀ ʟɪɴᴋ:\n\n🔗 ʟɪɴᴋ :- {share_link}</b>")
        

@Client.on_message(filters.command(['link']) & filters.private)
async def gen_link_s(bot, message):
    try:
        from plugins.dbusers import db
        username = (await bot.get_me()).username
        replied = message.reply_to_message
        if not replied:
            return await message.reply('Reply to a message to get a shareable link.')

        # Copy without captions
        post = await replied.copy(LOG_CHANNEL, caption=None)
        file_id = str(post.id)
        string = f"file_"
        string += file_id
        outstr = base64.urlsafe_b64encode(string.encode("ascii")).decode().strip("=")
        if WEBSITE_URL_MODE == True:
            share_link = f"{WEBSITE_URL}?file={outstr}"
        else:
            share_link = f"https://t.me/{username}?start={outstr}"
        
        # Save file to selected folder
        if await db.is_user_exist(message.from_user.id):
            selected_folder = await db.get_selected_folder(message.from_user.id)
            file_name = getattr(replied.document or replied.video or replied.audio or replied.photo, 'file_name', f'file_{file_id}')
            await db.save_file(message.from_user.id, file_id, file_name, folder=selected_folder)
        
        await message.reply(f"<b>⭕ ʜᴇʀᴇ ɪs ʏᴏᴜʀ ʟɪɴᴋ:\n\n🔗 ʟɪɴᴋ :- {share_link}</b>")
    except Exception as e:
        import logging
        logger = logging.getLogger(__name__)
        logger.error(f"Error in /link command: {e}")
        await message.reply(f"❌ Error: {str(e)[:100]}")
        


@Client.on_message(filters.command(['batch']) & filters.create(allowed))
async def gen_link_batch(bot, message):
    username = (await bot.get_me()).username
    if " " not in message.text:
        return await message.reply("Use correct format.\nExample /batch https://t.me/premium/10 https://t.me/premium/20.")
    links = message.text.strip().split(" ")
    if len(links) != 3:
        return await message.reply("Use correct format.\nExample /batch https://t.me/premium/10 https://t.me/premium/20.")
    cmd, first, last = links
    regex = re.compile("(https://)?(t\.me/|telegram\.me/|telegram\.dog/)(c/)?(\d+|[a-zA-Z_0-9]+)/(\d+)$")
    match = regex.match(first)
    if not match:
        return await message.reply('Invalid link')
    f_chat_id = match.group(4)
    f_msg_id = int(match.group(5))
    
    # Handle numeric channel IDs vs usernames
    if f_chat_id.isnumeric():
        f_chat_id = int(("-100" + f_chat_id))
    else:
        # For usernames, convert to chat object first to get the actual ID
        try:
            chat_obj = await bot.get_chat(f_chat_id)
            f_chat_id = chat_obj.id
        except:
            return await message.reply(f'Invalid channel: {f_chat_id}. Make sure bot is admin there.')

    
    match = regex.match(last)
    if not match:
        return await message.reply('Invalid link')
    l_chat_id = match.group(4)
    l_msg_id = int(match.group(5))
    
    # Handle numeric channel IDs vs usernames
    if l_chat_id.isnumeric():
        l_chat_id = int(("-100" + l_chat_id))
    else:
        try:
            chat_obj = await bot.get_chat(l_chat_id)
            l_chat_id = chat_obj.id
        except:
            return await message.reply(f'Invalid channel: {l_chat_id}. Make sure bot is admin there.')

    if f_chat_id != l_chat_id:
        return await message.reply("Chat ids not matched.")
    try:
        chat_id = (await bot.get_chat(f_chat_id)).id
    except ChannelInvalid:
        return await message.reply('This may be a private channel / group. Make me an admin over there to index the files.')
    except (UsernameInvalid, UsernameNotModified):
        return await message.reply('Invalid Link specified.')
    except Exception as e:
        return await message.reply(f'Errors - {e}')

    
    sts = await message.reply("**ɢᴇɴᴇʀᴀᴛɪɴɢ ʟɪɴᴋ ғᴏʀ ʏᴏᴜʀ ᴍᴇssᴀɢᴇ**.\n**ᴛʜɪs ᴍᴀʏ ᴛᴀᴋᴇ ᴛɪᴍᴇ ᴅᴇᴘᴇɴᴅɪɴɢ ᴜᴘᴏɴ ɴᴜᴍʙᴇʀ ᴏғ ᴍᴇssᴀɢᴇs**")

    FRMT = "**ɢᴇɴᴇʀᴀᴛɪɴɢ ʟɪɴᴋ...**\n**ᴛᴏᴛᴀʟ ᴍᴇssᴀɢᴇs:** {total}\n**ᴅᴏɴᴇ:** {current}\n**ʀᴇᴍᴀɪɴɪɴɢ:** {rem}\n**sᴛᴀᴛᴜs:** {sts}"

    outlist = []
    og_msg = 0
    tot = 0
    
    try:
        async for msg in bot.get_chat_history(f_chat_id, limit=(l_msg_id - f_msg_id + 1), offset=f_msg_id - 1):
            tot += 1
            if og_msg % 10 == 0:
                try:
                    await sts.edit(FRMT.format(total=l_msg_id-f_msg_id, current=tot, rem=((l_msg_id-f_msg_id) - tot), sts="Saving Messages"))
                except:
                    pass
            if msg.empty or msg.service:
                continue
            file = {
                "channel_id": f_chat_id,
                "msg_id": msg.id
            }
            og_msg += 1
            outlist.append(file)
    except:
        for msg_id in range(f_msg_id, l_msg_id + 1):
            tot += 1
            if og_msg % 10 == 0:
                try:
                    await sts.edit(FRMT.format(total=l_msg_id-f_msg_id, current=tot, rem=((l_msg_id-f_msg_id) - tot), sts="Saving Messages"))
                except:
                    pass
            try:
                msg = await bot.get_messages(f_chat_id, msg_id)
                if msg and not msg.empty and not msg.service:
                    file = {
                        "channel_id": f_chat_id,
                        "msg_id": msg.id
                    }
                    og_msg += 1
                    outlist.append(file)
            except:
                pass


    with open(f"batchmode_{message.from_user.id}.json", "w+") as out:
        json.dump(outlist, out)
    post = await bot.send_document(LOG_CHANNEL, f"batchmode_{message.from_user.id}.json", file_name="Batch.json", caption="⚠️ Batch Generated For Filestore.")
    os.remove(f"batchmode_{message.from_user.id}.json")
    string = f"file_{post.id}"
    encoded_id = base64.urlsafe_b64encode(string.encode("ascii")).decode().strip("=")
    file_id = f"BATCH-{encoded_id}"
    if WEBSITE_URL_MODE == True:
        share_link = f"{WEBSITE_URL}?file={file_id}"
    else:
        share_link = f"https://t.me/{username}?start={file_id}"
    await sts.edit(f"<b>⭕ ʜᴇʀᴇ ɪs ʏᴏᴜʀ ʟɪɴᴋ:\n\nContains `{og_msg}` files.\n\n🔗 ʟɪɴᴋ :- {share_link}</b>")
