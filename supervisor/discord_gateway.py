"""
Discord gateway integration for Ouroboros.

Parallel to telegram.py - handles Discord bot connection,
message routing, and event dispatching.
"""

import asyncio
import datetime
import logging
import pathlib
import threading
from typing import Any, Dict, List, Optional

import discord
from discord.ext import commands

from supervisor.state import load_state, save_state, append_jsonl

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Module-level state
# ---------------------------------------------------------------------------
_DRIVE_ROOT: Optional[pathlib.Path] = None
_DISCORD_CLIENT: Optional["DiscordClient"] = None


def init(drive_root: pathlib.Path, discord_client: "DiscordClient"):
    """Initialize Discord integration."""
    global _DRIVE_ROOT, _DISCORD_CLIENT
    _DRIVE_ROOT = drive_root
    _DISCORD_CLIENT = discord_client
    log.info("Discord gateway initialized")


def get_discord() -> "DiscordClient":
    """Get the initialized Discord client."""
    assert _DISCORD_CLIENT is not None, "discord_gateway.init() not called"
    return _DISCORD_CLIENT


def log_chat(direction: str, channel_id: int, user_id: int, text: str):
    """Log Discord message to chat.jsonl (parallel to telegram log_chat)."""
    if not _DRIVE_ROOT:
        return
    try:
        chat_log = _DRIVE_ROOT / "logs" / "chat.jsonl"
        append_jsonl(chat_log, {
            "ts": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "direction": direction,
            "channel": "discord",
            "channel_id": int(channel_id),
            "user_id": int(user_id),
            "text": str(text)[:2000],
        })
    except Exception:
        log.debug("Failed to log Discord chat", exc_info=True)


# ---------------------------------------------------------------------------
# DiscordClient - async bot wrapper
# ---------------------------------------------------------------------------

class DiscordClient:
    """
    Discord bot client wrapper.
    
    Runs discord.py bot in background thread, routes messages to supervisor.
    """
    
    def __init__(self, token: str):
        self.token = token
        self._ready = False
        self._bot: Optional[commands.Bot] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._thread: Optional[threading.Thread] = None
        self._owner_id: Optional[int] = None
        self._message_queue: asyncio.Queue = None
        
        # Start bot in background thread
        self._thread = threading.Thread(target=self._run_bot_thread, daemon=True)
        self._thread.start()
        
    def _run_bot_thread(self):
        """Run Discord bot event loop in background thread."""
        try:
            # Create new event loop for this thread
            self._loop = asyncio.new_event_loop()
            asyncio.set_event_loop(self._loop)
            
            # Create bot with minimal intents
            intents = discord.Intents.default()
            intents.message_content = True  # Required to read message content
            intents.members = True  # For member info
            
            self._bot = commands.Bot(command_prefix="!", intents=intents)
            self._message_queue = asyncio.Queue()
            
            # Register event handlers
            @self._bot.event
            async def on_ready():
                self._ready = True
                log.info(f"Discord bot ready: {self._bot.user.name} (ID: {self._bot.user.id})")

                # Load Discord-specific owner from state
                try:
                    st = load_state()
                    saved_owner = st.get("discord_owner_id")
                    if saved_owner:
                        self._owner_id = int(saved_owner)
                        log.info(f"Discord owner loaded from state: {self._owner_id}")
                except Exception:
                    log.debug("Failed to load owner from state", exc_info=True)
            
            @self._bot.event
            async def on_message(message: discord.Message):
                """Handle incoming Discord messages."""
                # Ignore own messages
                if message.author == self._bot.user:
                    return
                
                # Process bot commands first
                await self._bot.process_commands(message)
                
                # Owner recognition (first message = owner, if not already set)
                if not self._owner_id:
                    self._owner_id = message.author.id
                    try:
                        st = load_state()
                        st["discord_owner_id"] = self._owner_id
                        save_state(st)
                        log.info(f"Discord owner recognized: {message.author.name} (ID: {self._owner_id})")
                    except Exception:
                        log.error("Failed to save Discord owner", exc_info=True)
                
                # Log the message
                log_chat(
                    direction="incoming",
                    channel_id=message.channel.id,
                    user_id=message.author.id,
                    text=message.content
                )
                
                # Queue message for processing by supervisor
                await self._message_queue.put({
                    "type": "message",
                    "channel_id": message.channel.id,
                    "user_id": message.author.id,
                    "username": str(message.author),
                    "content": message.content,
                    "is_dm": isinstance(message.channel, discord.DMChannel),
                    "guild_id": message.guild.id if message.guild else None,
                })
            
            @self._bot.event
            async def on_error(event: str, *args, **kwargs):
                log.error(f"Discord error in {event}", exc_info=True)
            
            # Run bot
            self._loop.run_until_complete(self._bot.start(self.token))
            
        except Exception:
            log.error("Discord bot thread crashed", exc_info=True)
            self._ready = False
    
    def is_ready(self) -> bool:
        """Check if Discord client is connected and ready."""
        return self._ready and self._bot is not None
    
    def send_message(self, channel_id: int, content: str) -> Optional[Dict[str, Any]]:
        """
        Send message to Discord channel.
        
        Returns message dict on success, None on failure.
        Thread-safe - can be called from supervisor thread.
        """
        if not self.is_ready():
            log.warning("Discord bot not ready, cannot send message")
            return None
        
        try:
            # Schedule coroutine in bot's event loop
            future = asyncio.run_coroutine_threadsafe(
                self._send_message_async(channel_id, content),
                self._loop
            )
            result = future.result(timeout=10.0)
            
            # Log outgoing message
            if result:
                log_chat(
                    direction="outgoing",
                    channel_id=channel_id,
                    user_id=self._bot.user.id if self._bot.user else 0,
                    text=content
                )
            
            return result
            
        except Exception:
            log.error(f"Failed to send Discord message to channel {channel_id}", exc_info=True)
            return None
    
    async def _send_message_async(self, channel_id: int, content: str) -> Optional[Dict[str, Any]]:
        """Async helper for sending messages."""
        try:
            channel = self._bot.get_channel(channel_id)
            if not channel:
                # Try fetching if not in cache
                channel = await self._bot.fetch_channel(channel_id)
            
            if not channel:
                log.warning(f"Discord channel {channel_id} not found")
                return None
            
            # Split long messages (Discord limit is 2000 chars)
            chunks = self._split_message(content, 1900)
            sent_messages = []
            
            for chunk in chunks:
                msg = await channel.send(chunk)
                sent_messages.append({
                    "id": msg.id,
                    "channel_id": msg.channel.id,
                    "content": msg.content,
                })
            
            return sent_messages[0] if sent_messages else None
            
        except discord.Forbidden:
            log.error(f"No permission to send to Discord channel {channel_id}")
            return None
        except discord.HTTPException as e:
            log.error(f"Discord HTTP error sending message: {e}")
            return None
        except Exception:
            log.error("Unexpected error sending Discord message", exc_info=True)
            return None
    
    @staticmethod
    def _split_message(text: str, limit: int = 1900) -> List[str]:
        """Split long message into chunks, respecting Discord's 2000 char limit."""
        if len(text) <= limit:
            return [text]
        
        chunks = []
        while len(text) > limit:
            # Try to break at newline
            split_pos = text.rfind("\n", 0, limit)
            if split_pos < 100:  # If break point too early, break at limit
                split_pos = limit
            
            chunks.append(text[:split_pos])
            text = text[split_pos:].lstrip()
        
        if text:
            chunks.append(text)
        
        return chunks
    
    def get_pending_messages(self, timeout: float = 0.1) -> List[Dict[str, Any]]:
        """
        Get pending Discord messages (non-blocking).
        
        Called by supervisor main loop to process Discord events.
        Returns list of message dicts.
        """
        if not self.is_ready() or not self._message_queue:
            return []
        
        messages = []
        try:
            # Get messages from queue (non-blocking)
            while True:
                try:
                    # Use run_coroutine_threadsafe to access async queue from sync context
                    future = asyncio.run_coroutine_threadsafe(
                        asyncio.wait_for(self._message_queue.get(), timeout=timeout),
                        self._loop
                    )
                    msg = future.result(timeout=timeout + 0.1)
                    messages.append(msg)
                except asyncio.TimeoutError:
                    break
                except Exception:
                    break
        except Exception:
            log.debug("Error getting Discord messages", exc_info=True)
        
        return messages
    
    def shutdown(self):
        """Gracefully shutdown Discord bot."""
        if self._bot and self._loop:
            try:
                future = asyncio.run_coroutine_threadsafe(
                    self._bot.close(),
                    self._loop
                )
                future.result(timeout=5.0)
            except Exception:
                log.error("Error shutting down Discord bot", exc_info=True)
        
        self._ready = False


# ---------------------------------------------------------------------------
# Helper functions for supervisor integration
# ---------------------------------------------------------------------------

def send_discord_message(channel_id: int, content: str) -> bool:
    """Send message via initialized Discord client."""
    client = get_discord()
    result = client.send_message(channel_id, content)
    return result is not None
