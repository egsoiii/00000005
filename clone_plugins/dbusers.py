
import motor.motor_asyncio
from config import DB_NAME, DB_URI

class Database:
    
    def __init__(self, uri, database_name):
        self._client = motor.motor_asyncio.AsyncIOMotorClient(uri)
        self.db = self._client[database_name]
        self.col = self.db.users

    def new_user(self, id, name):
        return dict(
            id = id,
            name = name,
            destinations = [],  # List of destination objects
            delivery_mode = "pm",  # pm, channel, or both
        )
    
    async def add_user(self, id, name):
        user = self.new_user(id, name)
        await self.col.insert_one(user)
    
    async def is_user_exist(self, id):
        user = await self.col.find_one({'id':int(id)})
        return bool(user)

    async def total_users_count(self):
        count = await self.col.count_documents({})
        return count
    
    async def get_all_users(self):
        return self.col.find({})

    async def delete_user(self, user_id):
        await self.col.delete_many({'id': int(user_id)})
    
    async def add_destination(self, user_id, channel_id, dest_type, topic_id=None, topic_name=None):
        """Add a destination (supports multiple, prevents duplicates)"""
        dest_obj = {
            'channel_id': int(channel_id),
            'type': dest_type,
            'topic_id': topic_id,
            'topic_name': topic_name,  # Store topic name to avoid fetching later
            'enabled': True  # New destinations are enabled by default
        }
        # Use $addToSet to prevent duplicate destinations
        result = await self.col.update_one(
            {'id': int(user_id)}, 
            {'$addToSet': {'destinations': dest_obj}}
        )
        return result.modified_count > 0
    
    async def toggle_destination_status(self, user_id, channel_id):
        """Toggle destination enabled/disabled status"""
        user = await self.col.find_one({'id': int(user_id)})
        if not user:
            return False
        
        destinations = user.get('destinations', [])
        for dest in destinations:
            if dest['channel_id'] == int(channel_id):
                dest['enabled'] = not dest.get('enabled', True)
                break
        
        await self.col.update_one({'id': int(user_id)}, {'$set': {'destinations': destinations}})
        return True
    
    async def update_destination_topic(self, user_id, channel_id, topic_id, topic_name=None):
        """Update topic for a specific destination"""
        user = await self.col.find_one({'id': int(user_id)})
        if not user:
            return False
        
        destinations = user.get('destinations', [])
        for dest in destinations:
            if dest['channel_id'] == int(channel_id):
                dest['topic_id'] = topic_id
                dest['topic_name'] = topic_name
                break
        
        await self.col.update_one({'id': int(user_id)}, {'$set': {'destinations': destinations}})
        return True
    
    async def get_destinations(self, user_id):
        """Get all destinations for user"""
        user = await self.col.find_one({'id': int(user_id)})
        if not user:
            return []
        
        destinations = user.get('destinations', [])
        # Ensure all destinations have the enabled field (for backward compatibility)
        for dest in destinations:
            if 'enabled' not in dest:
                dest['enabled'] = True
        
        # Update the database with any missing enabled fields
        if destinations and any('enabled' not in d for d in destinations):
            await self.col.update_one({'id': int(user_id)}, {'$set': {'destinations': destinations}})
        
        return destinations
    
    async def remove_destination(self, user_id, channel_id):
        """Remove a specific destination"""
        await self.col.update_one({'id': int(user_id)}, {'$pull': {'destinations': {'channel_id': int(channel_id)}}})
    
    async def clear_destinations(self, user_id):
        """Remove all destinations"""
        await self.col.update_one({'id': int(user_id)}, {'$set': {'destinations': []}})
    
    async def set_delivery_mode(self, user_id, mode):
        """Set delivery mode: pm, channel, or both"""
        await self.col.update_one({'id': int(user_id)}, {'$set': {'delivery_mode': mode}})
    
    async def get_delivery_mode(self, user_id):
        """Get delivery mode for user"""
        user = await self.col.find_one({'id': int(user_id)})
        return user.get('delivery_mode', 'pm') if user else 'pm'

db = Database(DB_URI, DB_NAME)
