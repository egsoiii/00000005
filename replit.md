# File Storage Bot

A Telegram file storage bot built with Pyrogram. It allows users to store and organize files in Telegram channels with shareable links.

## Overview

This bot provides:
- File storage in Telegram channels
- Shareable link generation
- Batch file processing
- Multi-destination routing
- Clone functionality
- Auto-delete support

## Setup

### Required Environment Variables

The bot requires the following secrets to be configured:

| Variable | Description |
|----------|-------------|
| `API_ID` | Telegram API ID from [my.telegram.org](https://my.telegram.org) |
| `API_HASH` | Telegram API Hash from [my.telegram.org](https://my.telegram.org) |
| `BOT_TOKEN` | Bot token from [@BotFather](https://t.me/BotFather) |
| `LOG_CHANNEL` | Channel ID for logging (e.g., `-100xxxxxxxxxx`) |
| `DB_URI` | MongoDB connection string |

### Optional Environment Variables

| Variable | Default | Description |
|----------|---------|-------------|
| `DB_NAME` | `filestorebotz` | MongoDB database name |
| `ADMINS` | - | Space-separated admin user IDs |
| `BOT_USERNAME` | - | Bot username without @ |
| `AUTO_DELETE_MODE` | `True` | Enable auto-delete |
| `AUTO_DELETE_TIME` | `1800` | Auto-delete time in seconds |
| `MAX_DESTINATIONS` | `3` | Maximum destinations per user |

## Running

The bot runs on port 5000 with a health check endpoint at `/`.

```bash
python bot.py
```

## Project Structure

```
.
├── bot.py              # Main entry point
├── config.py           # Configuration
├── Script.py           # Bot messages
├── core/
│   ├── bot/            # Bot client setup
│   └── utils/          # Utility functions
└── plugins/            # Command handlers
```

## Backup & Restore Feature

The bot includes a backup/restore system for users who want to migrate their files to a new Telegram account:

- **Token Format**: `UserId:token` (e.g., `123456789:UyFjZE01PpIWFdIBZKAHLVHRYWT9eyjZ`)
- **Security Model**: Possession-based - whoever has the token can restore (like a password)
- **One-Time Use**: Tokens are invalidated after successful restore
- **Self-Restore Prevention**: Cannot restore to the same account that created the token
- **Token Management**: Users can generate, change, or delete their tokens

## Token-Based Folder Links

Folders can be shared via secure token-based links that don't expose the owner's user ID:

- **Link Format**: `https://t.me/botname?start=folder_TOKEN`
- **Token Generation**: Each folder gets a unique random token when first shared
- **Change Link**: Users can regenerate the token to invalidate all previous links
- **Password Protection**: Folders can optionally have password protection
- **Token Security**: Tokens are stored per-folder and can be changed anytime

### How It Works
1. When a user clicks "Edit" on a folder, they see "Copy Link" and "Change Link" buttons
2. "Copy Link" copies the current token-based share link
3. "Change Link" generates a new token, making the old link invalid
4. Recipients access the folder by clicking the link (password required if set)

## Tech Stack

- Python 3.11
- Pyrogram (Telegram MTProto)
- MongoDB (via Motor)
- aiohttp (HTTP server)
