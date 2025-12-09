import os

class TokenParser:
    """Parse additional bot tokens from environment variables"""
    
    def parse_from_env(self):
        """Parse tokens from environment in format: BOT_TOKEN_2=xxx BOT_TOKEN_3=xxx"""
        tokens = {}
        i = 2
        while True:
            token_key = f"BOT_TOKEN_{i}"
            token = os.getenv(token_key)
            if not token:
                break
            tokens[i] = token
            i += 1
        return tokens
