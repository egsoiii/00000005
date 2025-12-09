# Patch asyncio.coroutine for Python 3.12 compatibility
import asyncio
import sys
from functools import wraps

if sys.version_info >= (3, 10) and not hasattr(asyncio, 'coroutine'):
    def coroutine(func):
        @wraps(func)
        async def wrapper(*args, **kwargs):
            return await func(*args, **kwargs)
        return wrapper
    asyncio.coroutine = coroutine
