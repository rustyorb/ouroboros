"""
Discord gateway integration for Ouroboros.

Parallel to telegram.py - handles Discord bot connection,
message routing, and event dispatching.
"""

import logging
import pathlib
from typing import Optional, Dict, Any

log = logging.getLogger(__name__)

_DRIVE_ROOT: Optional[pathlib.Path] = None
_DISCORD_CLIENT: Optional["DiscordClient"] = None


def init(drive_root: pathlib.Path, discord_client: "DiscordClient"):
    """Initialize Discord integration."""
    global _DRIVE_ROOT, _DISCORD_CLIENT
    _DRIVE_ROOT = drive_root
    _DISCORD_CLIENT = discord_client
    log.info("Discord gateway initialized")


class DiscordClient:
    """
    Discord bot client wrapper.
    
    For now: stub. Will implement discord.py integration when token available.
    """
    
    def __init__(self, token: str):
        self.token = token
        self._ready = False
        
    def send_message(self, channel_id: int, content: str) -> Optional[Dict[str, Any]]:
        """Send message to Discord channel."""
        log.warning("Discord send_message called but not implemented yet")
        return None
    
    def is_ready(self) -> bool:
        """Check if Discord client is connected."""
        return self._ready
