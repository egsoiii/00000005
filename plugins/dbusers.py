
import motor.motor_asyncio
import time
import bcrypt
import copy
from config import DB_NAME, DB_URI

CACHE_TTL = 300

class UserCache:
    def __init__(self, ttl=CACHE_TTL):
        self._cache = {}
        self._ttl = ttl
    
    def get(self, user_id):
        key = int(user_id)
        if key in self._cache:
            data, timestamp = self._cache[key]
            if time.time() - timestamp < self._ttl:
                return data
            del self._cache[key]
        return None
    
    def set(self, user_id, data):
        self._cache[int(user_id)] = (data, time.time())
    
    def invalidate(self, user_id):
        self._cache.pop(int(user_id), None)
    
    def clear(self):
        self._cache.clear()

class Database:
    
    def __init__(self, uri, database_name):
        self._client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self._client[database_name]
        self.col = self.db.users
        self._cache = UserCache()

    def new_user(self, id, name):
        return dict(
            id = id,
            name = name,
            destinations = [],  # List of destination objects
            delivery_mode = "pm",  # pm, channel, or both
            caption = None,  # File caption
            filename_filters = [],  # Words/phrases to remove from filenames
            folders = [],  # User folders for organizing files: [{"name": "folder_name", "created_at": timestamp}]
            selected_folder = None,  # Currently selected folder
            stored_files = []  # Files stored with folders: [{"file_id": "123", "folder": "folder_name", "created_at": timestamp, "file_name": "name"}]
        )
    
    async def _get_user_cached(self, user_id):
        cached = self._cache.get(user_id)
        if cached:
            return cached
        user = await self.col.find_one({'id': int(user_id)})
        if user:
            self._cache.set(user_id, user)
        return user
    
    async def add_user(self, id, name):
        user = self.new_user(id, name)
        await self.col.insert_one(user)
        self._cache.invalidate(id)
    
    async def is_user_exist(self, id):
        user = await self._get_user_cached(id)
        return bool(user)

    async def total_users_count(self):
        count = await self.col.count_documents({})
        return count
    
    async def get_all_users(self):
        return self.col.find({})

    async def delete_user(self, user_id):
        await self.col.delete_many({'id': int(user_id)})
        self._cache.invalidate(user_id)
    
    async def add_destination(self, user_id, channel_id, dest_type, topic_id=None, topic_name=None, cached_name=None):
        """Add a destination (supports multiple, prevents duplicates)"""
        dest_obj = {
            'channel_id': int(channel_id),
            'type': dest_type,
            'topic_id': topic_id,
            'topic_name': topic_name,  # Store topic name to avoid fetching later
            'enabled': True,  # New destinations are enabled by default
            'cached_name': cached_name  # Cache channel name to avoid API calls
        }
        # Use $addToSet to prevent duplicate destinations
        result = await self.col.update_one(
            {'id': int(user_id)}, 
            {'$addToSet': {'destinations': dest_obj}}
        )
        self._cache.invalidate(user_id)
        return result.modified_count > 0
    
    async def update_destination_cached_name(self, user_id, channel_id, cached_name):
        """Update cached channel name for a destination"""
        user = await self._get_user_cached(user_id)
        if not user:
            return False
        
        destinations = user.get('destinations', [])
        for dest in destinations:
            if dest['channel_id'] == int(channel_id):
                dest['cached_name'] = cached_name
                break
        
        await self.col.update_one({'id': int(user_id)}, {'$set': {'destinations': destinations}})
        self._cache.invalidate(user_id)
        return True
    
    async def toggle_destination_status(self, user_id, channel_id):
        """Toggle destination enabled/disabled status"""
        user = await self._get_user_cached(user_id)
        if not user:
            return False
        
        destinations = user.get('destinations', [])
        for dest in destinations:
            if dest['channel_id'] == int(channel_id):
                dest['enabled'] = not dest.get('enabled', True)
                break
        
        await self.col.update_one({'id': int(user_id)}, {'$set': {'destinations': destinations}})
        self._cache.invalidate(user_id)
        return True
    
    async def update_destination_topic(self, user_id, channel_id, topic_id, topic_name=None):
        """Update topic for a specific destination"""
        user = await self._get_user_cached(user_id)
        if not user:
            return False
        
        destinations = user.get('destinations', [])
        for dest in destinations:
            if dest['channel_id'] == int(channel_id):
                dest['topic_id'] = topic_id
                dest['topic_name'] = topic_name
                break
        
        await self.col.update_one({'id': int(user_id)}, {'$set': {'destinations': destinations}})
        self._cache.invalidate(user_id)
        return True
    
    async def get_destinations(self, user_id):
        """Get all destinations for user"""
        user = await self._get_user_cached(user_id)
        if not user:
            return []
        
        destinations = user.get('destinations', [])
        for dest in destinations:
            if 'enabled' not in dest:
                dest['enabled'] = True
        
        return destinations
    
    async def remove_destination(self, user_id, channel_id):
        """Remove a specific destination"""
        await self.col.update_one({'id': int(user_id)}, {'$pull': {'destinations': {'channel_id': int(channel_id)}}})
        self._cache.invalidate(user_id)
    
    async def clear_destinations(self, user_id):
        """Remove all destinations"""
        await self.col.update_one({'id': int(user_id)}, {'$set': {'destinations': []}})
        self._cache.invalidate(user_id)
    
    async def set_delivery_mode(self, user_id, mode):
        """Set delivery mode: pm, channel, or both"""
        await self.col.update_one({'id': int(user_id)}, {'$set': {'delivery_mode': mode}})
        self._cache.invalidate(user_id)
    
    async def get_delivery_mode(self, user_id):
        """Get delivery mode for user"""
        user = await self._get_user_cached(user_id)
        return user.get('delivery_mode', 'pm') if user else 'pm'
    
    async def set_caption(self, user_id, caption):
        """Set file caption for user"""
        await self.col.update_one({'id': int(user_id)}, {'$set': {'caption': caption}})
        self._cache.invalidate(user_id)
    
    async def get_caption(self, user_id):
        """Get file caption for user"""
        user = await self._get_user_cached(user_id)
        return user.get('caption') if user else None
    
    async def delete_caption(self, user_id):
        """Delete file caption for user"""
        await self.col.update_one({'id': int(user_id)}, {'$set': {'caption': None}})
        self._cache.invalidate(user_id)
    
    async def add_filename_filter(self, user_id, filter_text):
        """Add word/phrase to remove from filenames"""
        await self.col.update_one(
            {'id': int(user_id)},
            {'$addToSet': {'filename_filters': filter_text}}
        )
        self._cache.invalidate(user_id)
    
    async def remove_filename_filter(self, user_id, filter_text):
        """Remove word/phrase filter"""
        await self.col.update_one(
            {'id': int(user_id)},
            {'$pull': {'filename_filters': filter_text}}
        )
        self._cache.invalidate(user_id)
    
    async def get_filename_filters(self, user_id):
        """Get all filename filters"""
        user = await self._get_user_cached(user_id)
        return user.get('filename_filters', []) if user else []
    
    async def create_folder(self, user_id, folder_name, parent_folder=None):
        """Create a new folder (supports subfolders with path notation)
        
        folder_name: Name of the folder to create
        parent_folder: Parent folder path (e.g., 'Parent' or 'Parent/Child')
        
        The full path will be stored as: 'Parent/Child/NewFolder'
        """
        # Build full path
        if parent_folder:
            full_path = f"{parent_folder}/{folder_name}"
        else:
            full_path = folder_name
        
        folder = {
            'name': full_path,
            'created_at': __import__('datetime').datetime.now()
        }
        await self.col.update_one(
            {'id': int(user_id)},
            {'$addToSet': {'folders': folder}}
        )
        self._cache.invalidate(user_id)
        return True
    
    async def get_folders(self, user_id):
        """Get all folders for user"""
        user = await self._get_user_cached(user_id)
        return user.get('folders', []) if user else []
    
    async def get_root_folders(self, user_id):
        """Get only root-level folders (folders without parent)"""
        folders = await self.get_folders(user_id)
        root_folders = []
        for f in folders:
            folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
            # Root folders don't contain '/'
            if '/' not in folder_name:
                root_folders.append(f)
        return root_folders
    
    async def get_subfolders(self, user_id, parent_folder):
        """Get subfolders of a specific folder
        
        parent_folder: The parent folder path (e.g., 'Parent' or 'Parent/Child')
        Returns: List of direct subfolders
        """
        folders = await self.get_folders(user_id)
        subfolders = []
        parent_prefix = f"{parent_folder}/"
        
        for f in folders:
            folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
            # Check if this is a direct subfolder (starts with parent/ but no more /)
            if folder_name.startswith(parent_prefix):
                remainder = folder_name[len(parent_prefix):]
                # Direct subfolder has no more slashes
                if '/' not in remainder:
                    subfolders.append(f)
        return subfolders
    
    async def get_folder_display_name(self, folder_name):
        """Get just the display name of a folder (last part of path)"""
        if '/' in folder_name:
            return folder_name.split('/')[-1]
        return folder_name
    
    async def get_files_in_folder_recursive(self, user_id, folder_path):
        """Get all files in a folder and all its subfolders"""
        user = await self._get_user_cached(user_id)
        if not user:
            return []
        files = user.get('stored_files', [])
        result = []
        for f in files:
            file_folder = f.get('folder')
            # Include files in exact folder or any subfolder
            if file_folder == folder_path or (file_folder and file_folder.startswith(f"{folder_path}/")):
                result.append(f)
        return result
    
    async def delete_folder(self, user_id, folder_name):
        """Delete a folder and all its subfolders (cascade delete)"""
        user = await self._get_user_cached(user_id)
        if not user:
            return False
        
        folders = user.get('folders', [])
        updated_folders = []
        
        for folder in folders:
            fname = folder.get('name', str(folder)) if isinstance(folder, dict) else str(folder)
            # Keep folders that are NOT the target folder AND NOT subfolders of it
            if fname != folder_name and not fname.startswith(f"{folder_name}/"):
                updated_folders.append(folder)
        
        # Also update files - clear folder reference for files in deleted folder and subfolders
        files = user.get('stored_files', [])
        for f in files:
            file_folder = f.get('folder')
            if file_folder == folder_name or (file_folder and file_folder.startswith(f"{folder_name}/")):
                f['folder'] = None  # Move files to unorganized
        
        await self.col.update_one({'id': int(user_id)}, {'$set': {'folders': updated_folders, 'stored_files': files}})
        self._cache.invalidate(user_id)
        return True
    
    async def rename_folder(self, user_id, old_name, new_name):
        """Rename a folder and cascade to all subfolders and files"""
        user = await self._get_user_cached(user_id)
        if not user:
            return False
        
        # 1. Rename the folder and all subfolders
        folders = user.get('folders', [])
        old_prefix = f"{old_name}/"
        
        for folder in folders:
            folder_path = folder.get('name', str(folder)) if isinstance(folder, dict) else str(folder)
            
            if folder_path == old_name:
                # Exact match - rename
                if isinstance(folder, dict):
                    folder['name'] = new_name
                else:
                    folder_idx = folders.index(folder)
                    folders[folder_idx] = {'name': new_name, 'created_at': __import__('datetime').datetime.now()}
            elif folder_path.startswith(old_prefix):
                # Subfolder - update the prefix
                new_path = new_name + folder_path[len(old_name):]
                if isinstance(folder, dict):
                    folder['name'] = new_path
                else:
                    folder_idx = folders.index(folder)
                    folders[folder_idx] = {'name': new_path, 'created_at': __import__('datetime').datetime.now()}
        
        # 2. Update all files that are in the renamed folder or its subfolders
        files = user.get('stored_files', [])
        for f in files:
            file_folder = f.get('folder')
            if file_folder == old_name:
                f['folder'] = new_name
            elif file_folder and file_folder.startswith(old_prefix):
                f['folder'] = new_name + file_folder[len(old_name):]
        
        await self.col.update_one({'id': int(user_id)}, {'$set': {'folders': folders, 'stored_files': files}})
        
        # 3. Update selected_folder if it was the renamed folder or a subfolder
        selected = user.get('selected_folder')
        if selected == old_name:
            await self.col.update_one({'id': int(user_id)}, {'$set': {'selected_folder': new_name}})
        elif selected and selected.startswith(old_prefix):
            new_selected = new_name + selected[len(old_name):]
            await self.col.update_one({'id': int(user_id)}, {'$set': {'selected_folder': new_selected}})
        
        self._cache.invalidate(user_id)
        return True
    
    async def set_selected_folder(self, user_id, folder_name):
        """Set currently selected folder"""
        await self.col.update_one(
            {'id': int(user_id)},
            {'$set': {'selected_folder': folder_name}}
        )
        self._cache.invalidate(user_id)
    
    async def get_selected_folder(self, user_id):
        """Get currently selected folder"""
        user = await self._get_user_cached(user_id)
        return user.get('selected_folder') if user else None
    
    async def save_file(self, user_id, file_id, file_name, folder=None, file_type='document'):
        """Save file with folder information and file type"""
        import datetime
        file_obj = {
            'file_id': str(file_id),
            'folder': folder,
            'created_at': datetime.datetime.now(),
            'file_name': file_name,
            'file_type': file_type,
            'protected': False
        }
        await self.col.update_one(
            {'id': int(user_id)},
            {'$addToSet': {'stored_files': file_obj}}
        )
        self._cache.invalidate(user_id)
    
    async def toggle_file_protected(self, user_id, file_idx):
        """Toggle protected status for a file by index"""
        user = await self._get_user_cached(user_id)
        if not user:
            return False
        
        files = user.get('stored_files', [])
        if 0 <= file_idx < len(files):
            files[file_idx]['protected'] = not files[file_idx].get('protected', False)
            await self.col.update_one({'id': int(user_id)}, {'$set': {'stored_files': files}})
            self._cache.invalidate(user_id)
            return files[file_idx]['protected']
        return None
    
    async def get_files_by_folder(self, user_id, folder=None):
        """Get files in a specific folder (None = no folder)"""
        user = await self._get_user_cached(user_id)
        if not user:
            return []
        files = user.get('stored_files', [])
        return [f for f in files if f.get('folder') == folder]
    
    async def move_file_to_folder(self, user_id, file_id, new_folder):
        """Move file to different folder"""
        user = await self._get_user_cached(user_id)
        if not user:
            return False
        
        files = user.get('stored_files', [])
        for f in files:
            if f['file_id'] == str(file_id):
                f['folder'] = new_folder
                break
        
        await self.col.update_one({'id': int(user_id)}, {'$set': {'stored_files': files}})
        self._cache.invalidate(user_id)
        return True
    
    async def delete_file(self, user_id, file_id):
        """Delete file from storage"""
        await self.col.update_one(
            {'id': int(user_id)},
            {'$pull': {'stored_files': {'file_id': str(file_id)}}}
        )
        self._cache.invalidate(user_id)
    
    async def update_file_folder(self, user_id, file_idx, new_folder):
        """Update folder for a file by index in stored_files array"""
        user = await self._get_user_cached(user_id)
        if not user:
            return False
        
        files = user.get('stored_files', [])
        if 0 <= file_idx < len(files):
            files[file_idx]['folder'] = new_folder
            await self.col.update_one({'id': int(user_id)}, {'$set': {'stored_files': files}})
            self._cache.invalidate(user_id)
            return True
        return False
    
    async def generate_backup_token(self, user_id):
        """Generate a unique backup token for user in format UserId:token"""
        import secrets
        import datetime
        random_part = secrets.token_urlsafe(24)
        full_token = f"{user_id}:{random_part}"
        await self.col.update_one(
            {'id': int(user_id)},
            {'$set': {
                'backup_token': full_token,
                'backup_token_random': random_part,
                'backup_token_created': datetime.datetime.now()
            }}
        )
        self._cache.invalidate(user_id)
        return full_token
    
    async def change_backup_token(self, user_id):
        """Change/regenerate the backup token for user"""
        import secrets
        import datetime
        random_part = secrets.token_urlsafe(24)
        full_token = f"{user_id}:{random_part}"
        await self.col.update_one(
            {'id': int(user_id)},
            {'$set': {
                'backup_token': full_token,
                'backup_token_random': random_part,
                'backup_token_created': datetime.datetime.now()
            }}
        )
        self._cache.invalidate(user_id)
        return full_token
    
    async def delete_backup_token(self, user_id):
        """Permanently delete backup token - cannot be recovered"""
        await self.col.update_one(
            {'id': int(user_id)},
            {'$unset': {
                'backup_token': '',
                'backup_token_random': '',
                'backup_token_created': ''
            }}
        )
        self._cache.invalidate(user_id)
        return True
    
    async def get_backup_token(self, user_id):
        """Get existing backup token for user"""
        user = await self._get_user_cached(user_id)
        return user.get('backup_token') if user else None
    
    async def get_user_by_backup_token(self, token):
        """Find user by backup token (supports full UserId:token or just random part)"""
        if ':' in token:
            user = await self.col.find_one({'backup_token': token})
        else:
            user = await self.col.find_one({'backup_token_random': token})
        return user
    
    async def parse_backup_token(self, token):
        """Parse token to extract user_id and random part. Returns (user_id, random_part) or (None, None)"""
        if ':' in token:
            parts = token.split(':', 1)
            try:
                user_id = int(parts[0])
                random_part = parts[1]
                return user_id, random_part
            except ValueError:
                return None, None
        return None, token
    
    async def transfer_files_to_user(self, from_user_id, to_user_id):
        """Transfer all files and folders from one user to another"""
        from_user = await self.col.find_one({'id': int(from_user_id)})
        if not from_user:
            return False, 0
        
        files = from_user.get('stored_files', [])
        folders = from_user.get('folders', [])
        
        if not files:
            return False, 0
        
        await self.col.update_one(
            {'id': int(to_user_id)},
            {
                '$addToSet': {
                    'stored_files': {'$each': files},
                    'folders': {'$each': folders}
                }
            }
        )
        self._cache.invalidate(to_user_id)
        return True, len(files)
    
    async def invalidate_backup_token(self, user_id):
        """Remove backup token after successful transfer"""
        await self.col.update_one(
            {'id': int(user_id)},
            {'$unset': {
                'backup_token': '',
                'backup_token_random': '',
                'backup_token_created': ''
            }}
        )
        self._cache.invalidate(user_id)
    
    async def set_folder_password(self, user_id, folder_name, password):
        """Set password protection for a folder (password is hashed with bcrypt)"""
        user = await self._get_user_cached(user_id)
        if not user:
            return False
        
        hashed = bcrypt.hashpw(password.encode('utf-8'), bcrypt.gensalt())
        
        folders = user.get('folders', [])
        for folder in folders:
            fname = folder.get('name', str(folder)) if isinstance(folder, dict) else str(folder)
            if fname == folder_name:
                if isinstance(folder, dict):
                    folder['password'] = hashed.decode('utf-8')
                    folder['password_plain'] = password
                break
        
        await self.col.update_one({'id': int(user_id)}, {'$set': {'folders': folders}})
        self._cache.invalidate(user_id)
        return True
    
    async def remove_folder_password(self, user_id, folder_name):
        """Remove password protection from a folder"""
        # Invalidate cache first to ensure fresh read
        self._cache.invalidate(user_id)
        
        # Get fresh data directly from database
        user = await self.col.find_one({'id': int(user_id)})
        if not user:
            return False
        
        # Create a deep copy of folders to avoid cache mutation issues
        folders = copy.deepcopy(user.get('folders', []))
        
        for folder in folders:
            fname = folder.get('name', str(folder)) if isinstance(folder, dict) else str(folder)
            if fname == folder_name:
                if isinstance(folder, dict):
                    if 'password' in folder:
                        del folder['password']
                    if 'password_plain' in folder:
                        del folder['password_plain']
                break
        
        await self.col.update_one({'id': int(user_id)}, {'$set': {'folders': folders}})
        # Invalidate cache again after update to ensure fresh reads
        self._cache.invalidate(user_id)
        return True
    
    async def get_folder_password(self, user_id, folder_name):
        """Get password for a folder (returns None if not set)"""
        user = await self._get_user_cached(user_id)
        if not user:
            return None
        
        folders = user.get('folders', [])
        for folder in folders:
            fname = folder.get('name', str(folder)) if isinstance(folder, dict) else str(folder)
            if fname == folder_name:
                if isinstance(folder, dict):
                    return folder.get('password')
        return None
    
    async def get_folder_password_plain(self, user_id, folder_name):
        """Get plain text password for a folder (returns None if not set)"""
        user = await self._get_user_cached(user_id)
        if not user:
            return None
        
        folders = user.get('folders', [])
        for folder in folders:
            fname = folder.get('name', str(folder)) if isinstance(folder, dict) else str(folder)
            if fname == folder_name:
                if isinstance(folder, dict):
                    return folder.get('password_plain')
        return None
    
    async def verify_folder_password(self, user_id, folder_name, password):
        """Verify password for a folder using bcrypt"""
        stored_hash = await self.get_folder_password(user_id, folder_name)
        if stored_hash is None:
            return True
        try:
            return bcrypt.checkpw(password.encode('utf-8'), stored_hash.encode('utf-8'))
        except Exception:
            return False
    
    async def is_folder_password_protected(self, user_id, folder_name):
        """Check if a folder has password protection"""
        password = await self.get_folder_password(user_id, folder_name)
        return password is not None
    
    async def get_all_protected_subfolders(self, user_id, parent_folder):
        """Get all password-protected subfolders under a parent folder"""
        folders = await self.get_folders(user_id)
        protected = []
        parent_prefix = f"{parent_folder}/"
        
        for f in folders:
            folder_name = f.get('name', str(f)) if isinstance(f, dict) else str(f)
            if folder_name.startswith(parent_prefix):
                if isinstance(f, dict) and f.get('password'):
                    protected.append(folder_name)
        return protected
    
    async def generate_folder_token(self, user_id, folder_name):
        """Generate a unique token for folder access (no owner ID in token)"""
        import secrets
        import datetime
        
        user = await self._get_user_cached(user_id)
        if not user:
            return None
        
        folders = user.get('folders', [])
        token = secrets.token_urlsafe(16)
        
        for folder in folders:
            fname = folder.get('name', str(folder)) if isinstance(folder, dict) else str(folder)
            if fname == folder_name:
                if isinstance(folder, dict):
                    folder['access_token'] = token
                    folder['token_created'] = datetime.datetime.now()
                break
        
        await self.col.update_one({'id': int(user_id)}, {'$set': {'folders': folders}})
        self._cache.invalidate(user_id)
        return token
    
    async def get_folder_token(self, user_id, folder_name):
        """Get existing access token for a folder"""
        user = await self._get_user_cached(user_id)
        if not user:
            return None
        
        folders = user.get('folders', [])
        for folder in folders:
            fname = folder.get('name', str(folder)) if isinstance(folder, dict) else str(folder)
            if fname == folder_name:
                if isinstance(folder, dict):
                    return folder.get('access_token')
        return None
    
    async def change_folder_token(self, user_id, folder_name):
        """Change/regenerate the folder access token (invalidates old link)"""
        return await self.generate_folder_token(user_id, folder_name)
    
    async def get_folder_by_token(self, token):
        """Find folder and owner by access token. Returns (user_id, folder_name) or (None, None)"""
        user = await self.col.find_one({'folders.access_token': token})
        if not user:
            return None, None
        
        folders = user.get('folders', [])
        for folder in folders:
            if isinstance(folder, dict) and folder.get('access_token') == token:
                return user['id'], folder.get('name')
        return None, None
    
    async def validate_folder_token(self, token):
        """Validate a folder access token and return folder info"""
        owner_id, folder_name = await self.get_folder_by_token(token)
        if owner_id is None:
            return None
        return {'owner_id': owner_id, 'folder_name': folder_name}
    
    # ============ FILE PASSWORD PROTECTION ============
    
    async def set_file_password(self, user_id, file_idx, password):
        """Set password protection for a file (stored in plain text, 2-8 chars)"""
        if len(password) < 2 or len(password) > 8:
            return False
        
        user = await self._get_user_cached(user_id)
        if not user:
            return False
        
        files = user.get('stored_files', [])
        if 0 <= file_idx < len(files):
            files[file_idx]['password'] = password
            await self.col.update_one({'id': int(user_id)}, {'$set': {'stored_files': files}})
            self._cache.invalidate(user_id)
            return True
        return False
    
    async def remove_file_password(self, user_id, file_idx):
        """Remove password protection from a file"""
        user = await self._get_user_cached(user_id)
        if not user:
            return False
        
        files = user.get('stored_files', [])
        if 0 <= file_idx < len(files):
            if 'password' in files[file_idx]:
                del files[file_idx]['password']
            await self.col.update_one({'id': int(user_id)}, {'$set': {'stored_files': files}})
            self._cache.invalidate(user_id)
            return True
        return False
    
    async def get_file_password(self, user_id, file_idx):
        """Get password for a file (returns None if not set)"""
        user = await self._get_user_cached(user_id)
        if not user:
            return None
        
        files = user.get('stored_files', [])
        if 0 <= file_idx < len(files):
            return files[file_idx].get('password')
        return None
    
    async def verify_file_password(self, user_id, file_idx, password):
        """Verify password for a file (plain text comparison)"""
        stored_password = await self.get_file_password(user_id, file_idx)
        if stored_password is None:
            return True
        return password == stored_password
    
    async def is_file_password_protected(self, user_id, file_idx):
        """Check if file has password protection"""
        password = await self.get_file_password(user_id, file_idx)
        return password is not None
    
    # ============ FILE TOKEN/LINK MANAGEMENT ============
    
    async def generate_file_token(self, user_id, file_idx):
        """Generate a unique token for file access"""
        import secrets
        import datetime
        
        user = await self._get_user_cached(user_id)
        if not user:
            return None
        
        files = user.get('stored_files', [])
        if 0 <= file_idx < len(files):
            token = secrets.token_urlsafe(16)
            files[file_idx]['access_token'] = token
            files[file_idx]['token_created'] = datetime.datetime.now()
            await self.col.update_one({'id': int(user_id)}, {'$set': {'stored_files': files}})
            self._cache.invalidate(user_id)
            return token
        return None
    
    async def get_file_token(self, user_id, file_idx):
        """Get existing access token for a file"""
        user = await self._get_user_cached(user_id)
        if not user:
            return None
        
        files = user.get('stored_files', [])
        if 0 <= file_idx < len(files):
            return files[file_idx].get('access_token')
        return None
    
    async def change_file_token(self, user_id, file_idx):
        """Change/regenerate the file access token (invalidates old link)"""
        return await self.generate_file_token(user_id, file_idx)
    
    async def get_file_by_token(self, token):
        """Find file and owner by access token. Returns (user_id, file_idx, file_obj) or (None, None, None)"""
        user = await self.col.find_one({'stored_files.access_token': token})
        if not user:
            return None, None, None
        
        files = user.get('stored_files', [])
        for idx, file_obj in enumerate(files):
            if file_obj.get('access_token') == token:
                return user['id'], idx, file_obj
        return None, None, None

db = Database(DB_URI, DB_NAME)
