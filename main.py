import discord
from discord.ext import commands
from discord import ui
import asyncio
import random
import os
import logging
from datetime import datetime
from dotenv import load_dotenv
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set
from pathlib import Path

# ==================== LOGGING SETUP ====================

# Create logs directory
LOGS_FOLDER = Path(__file__).parent / "logs"
LOGS_FOLDER.mkdir(exist_ok=True)

# Configure logging
log_filename = LOGS_FOLDER / f"bot_{datetime.now().strftime('%Y%m%d')}.log"
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s | %(levelname)s | %(message)s',
    handlers=[
        logging.FileHandler(log_filename, encoding='utf-8'),
        logging.StreamHandler()  # Also print to console
    ]
)
logger = logging.getLogger('MafiaBot')

# Text-to-speech for announcements
# try:
#     from gtts import gTTS
#     TTS_AVAILABLE = True
# except ImportError:
#     TTS_AVAILABLE = False
#     print("Warning: gTTS not installed. Audio announcements disabled. Run: pip install gTTS")

# Load environment variables
load_dotenv()
TOKEN = os.getenv('DISCORD_BOT_TOKEN')

# Audio folder setup
# AUDIO_FOLDER = Path(__file__).parent / "audio"
# AUDIO_FOLDER.mkdir(exist_ok=True)

# Bot setup with intents
intents = discord.Intents.default()
intents.message_content = True
intents.voice_states = True
intents.members = True
intents.dm_messages = True

bot = commands.Bot(command_prefix='!', intents=intents)

# ==================== MAFIA GAME SYSTEM ====================

class Role(Enum):
    CITIZEN = "Citizen"
    MAFIA = "Mafia"
    DOCTOR = "Doctor"
    POLICE = "Police"


class GamePhase(Enum):
    WAITING = "waiting"
    REGISTRATION = "registration"
    NIGHT = "night"
    DAY = "day"
    VOTING = "voting"
    ENDED = "ended"


@dataclass
class Player:
    member: discord.Member
    name: str
    role: Role = Role.CITIZEN
    is_alive: bool = True
    doctor_self_save_used: bool = False  # Track if doctor saved themselves last round


@dataclass
class GameSettings:
    num_mafia: int = 1
    num_doctor: int = 1
    num_police: int = 1
    voting_time: int = 30  # seconds
    discussion_time: int = 240  # seconds
    registration_time: int = 90  # seconds
    mafia_skip_kills: int = 1  # How many times mafia can skip killing per game
    role_reveal_mode: int = 3  # 1=no reveal, 2=mafia or not, 3=full role + suspense
    test_mode: bool = False  # Testing mode flag


@dataclass
class DummyMember:
    """Fake Discord member for testing"""
    id: int
    display_name: str
    name: str
    voice: None = None
    bot: bool = False
    
    async def send(self, *args, **kwargs):
        """Dummy send - does nothing"""
        pass
    
    async def edit(self, *args, **kwargs):
        """Dummy edit - does nothing"""
        pass


@dataclass
class GameState:
    phase: GamePhase = GamePhase.WAITING
    players: Dict[int, Player] = field(default_factory=dict)  # member.id -> Player
    settings: GameSettings = field(default_factory=GameSettings)
    voice_channel: Optional[discord.VoiceChannel] = None
    text_channel: Optional[discord.TextChannel] = None
    guild: Optional[discord.Guild] = None
    round_number: int = 0
    voice_connected: bool = False  # Track if bot is in voice
    tester_id: Optional[int] = None  # ID of the tester for test mode
    host_id: Optional[int] = None  # ID of the user who started the game
    
    # Night action tracking
    mafia_votes: Dict[int, int] = field(default_factory=dict)  # voter_id -> target_id
    mafia_target: Optional[int] = None
    doctor_save: Optional[int] = None
    police_investigation: Optional[int] = None
    night_actions_expected: int = 0  # Total night actions expected
    night_actions_received: int = 0  # Total night actions received
    night_auto_end_triggered: bool = False  # Prevent double-triggering
    night_actions_submitted: set = field(default_factory=set)  # Player IDs who already submitted
    
    # Mafia skip tracking
    mafia_skips_used: int = 0  # How many times mafia has skipped killing
    
    # Discussion tracking
    discussion_ended: bool = False  # Prevent double-triggering of voting start
    
    # Day voting
    day_votes: Dict[int, Optional[int]] = field(default_factory=dict)  # voter_id -> target_id (None = skip)
    
    # Registration message
    registration_message: Optional[discord.Message] = None
    
    # Mafia chat relay
    mafia_messages: List[tuple] = field(default_factory=list)  # (sender_name, message)
    
    # Track messages to delete at end of game
    game_messages: List[discord.Message] = field(default_factory=list)


# Active games per guild
active_games: Dict[int, GameState] = {}


async def track_message(game: GameState, message: discord.Message):
    """Add a message to the list of messages to delete at game end"""
    if message:
        game.game_messages.append(message)


async def delete_game_messages(game: GameState):
    """Delete all tracked game messages"""
    if not game.game_messages:
        return
    
    deleted_count = 0
    failed_count = 0
    
    try:
        # Try bulk delete (only works for messages < 14 days old)
        if game.text_channel:
            # Split into chunks of 100 (Discord limit)
            for i in range(0, len(game.game_messages), 100):
                chunk = game.game_messages[i:i+100]
                try:
                    await game.text_channel.delete_messages(chunk)
                    deleted_count += len(chunk)
                except discord.errors.HTTPException:
                    # If bulk delete fails, delete one by one
                    for msg in chunk:
                        try:
                            await msg.delete()
                            deleted_count += 1
                        except Exception as e:
                            failed_count += 1
                await asyncio.sleep(0.5)  # Rate limit protection
        
        logger.info(f"Message cleanup: {deleted_count} deleted, {failed_count} failed")
    except Exception as e:
        logger.error(f"Error deleting messages: {e}")
    
    game.game_messages.clear()


# ==================== AUDIO ANNOUNCEMENTS ====================

# Announcement texts (Korean drama style)
ANNOUNCEMENTS = {
    "night_has_come": "Night has come. Everyone, go to sleep.",
    "night_is_over": "The night is over. Everyone, wake up.",
    "voting_time": "It's time to vote. Choose who to eliminate.",
    "game_start": "The game begins now. Trust no one.",
    "mafia_wins": "The Mafia wins! Darkness has fallen.",
    "citizens_win": "The Citizens win! The town is saved.",
    "someone_eliminated": "Someone has been eliminated.",
    "someone_saved": "Peaceful night everyone is safe.",
}


async def send_game_message(game: GameState, content: str = None, embed: discord.Embed = None, view: discord.ui.View = None) -> Optional[discord.Message]:
    """Send a message and track it for deletion at game end"""
    if not game.text_channel:
        return None
    
    try:
        msg = await game.text_channel.send(content=content, embed=embed, view=view)
        game.game_messages.append(msg)
        return msg
    except Exception as e:
        print(f"Failed to send message: {e}")
        return None


async def generate_tts_audio(text: str, filename: str) -> Optional[Path]:
    """Generate TTS audio file"""
    if not TTS_AVAILABLE:
        return None
    
    filepath = AUDIO_FOLDER / f"{filename}.mp3"
    
    # Only generate if file doesn't exist
    if not filepath.exists():
        try:
            tts = gTTS(text=text, lang='en', slow=True)
            tts.save(str(filepath))
            print(f"Generated audio: {filepath}")
        except Exception as e:
            print(f"TTS generation failed: {e}")
            return None
    
    return filepath


async def play_announcement(game: GameState, announcement_key: str):
    """Play an announcement in the voice channel"""
    return  # Voice feedback disabled for now
    
    voice_client = game.guild.voice_client
    if not voice_client or not voice_client.is_connected():
        return
    
    text = ANNOUNCEMENTS.get(announcement_key, "")
    if not text:
        return
    
    # Generate or get audio file
    audio_path = await generate_tts_audio(text, announcement_key)
    if not audio_path or not audio_path.exists():
        return
    
    try:
        # Wait if something is already playing
        while voice_client.is_playing():
            await asyncio.sleep(0.5)
        
        # Play the audio
        audio_source = discord.FFmpegPCMAudio(str(audio_path))
        voice_client.play(audio_source)
        
        # Wait for it to finish
        while voice_client.is_playing():
            await asyncio.sleep(0.5)
        
        # Small pause after announcement
        await asyncio.sleep(0.5)
        
    except Exception as e:
        print(f"Audio playback failed: {e}")


async def pre_generate_audio():
    """Pre-generate all audio files at startup"""
    if not TTS_AVAILABLE:
        return
    
    print("Pre-generating announcement audio files...")
    for key, text in ANNOUNCEMENTS.items():
        await generate_tts_audio(text, key)
    print("Audio files ready!")


def get_game(guild_id: int) -> Optional[GameState]:
    return active_games.get(guild_id)


def create_game(guild_id: int) -> GameState:
    game = GameState()
    active_games[guild_id] = game
    return game


async def safe_voice_connect(channel: discord.VoiceChannel, guild: discord.Guild, skip_if_error: bool = True) -> tuple[bool, Optional[discord.VoiceClient]]:
    """
    Safely connect to a voice channel with robust error handling.
    Returns (success: bool, voice_client: Optional[VoiceClient])
    
    Note: Voice connection is OPTIONAL for the Mafia game.
    - Muting players works WITHOUT voice connection (uses HTTP API)
    - Voice is only needed for audio announcements (TTS)
    
    If skip_if_error is True, will return quickly on failure instead of waiting for retries.
    """
    try:
        # First, clean up any existing voice client for this guild
        existing_vc = guild.voice_client
        if existing_vc:
            try:
                await existing_vc.disconnect(force=True)
                logger.info("Disconnected existing voice client")
            except Exception as e:
                logger.warning(f"Error disconnecting existing voice client: {e}")
            # Wait for Discord to process the disconnect
            await asyncio.sleep(2.0)
        
        # Wait a moment for gateway to be ready
        await asyncio.sleep(1.0)
        
        # Attempt connection with shorter timeout since it's optional
        logger.info(f"Attempting voice connection to {channel.name}")
        
        # Use reconnect=False to prevent endless retry loops on 4006 errors
        vc = await channel.connect(timeout=15.0, reconnect=False, self_deaf=True)
        
        # Verify connection is stable
        await asyncio.sleep(1.0)
        if vc and vc.is_connected():
            logger.info(f"Successfully connected to voice channel: {channel.name}")
            return True, vc
        else:
            logger.warning("Voice client reports not connected after connect()")
            if vc:
                try:
                    await vc.disconnect(force=True)
                except:
                    pass
            return False, None
            
    except asyncio.TimeoutError:
        logger.warning("Voice connection timed out - continuing without voice")
        return False, None
    except discord.errors.ConnectionClosed as e:
        logger.warning(f"Voice connection closed ({e.code}) - continuing without voice")
        # Clean up any partial connection
        if guild.voice_client:
            try:
                await guild.voice_client.disconnect(force=True)
            except:
                pass
        return False, None
    except Exception as e:
        logger.warning(f"Voice connection failed: {e} - continuing without voice")
        return False, None


# ==================== HELPER FUNCTIONS ====================

def format_time(seconds: int) -> str:
    """Format seconds into readable time string"""
    if seconds >= 60:
        mins = seconds // 60
        secs = seconds % 60
        return f"{mins}m{secs:02d}s" if secs else f"{mins}m"
    return f"{seconds}s"


def create_progress_bar(current: int, total: int, length: int = 8) -> str:
    """Create a visual progress bar"""
    filled = int((current / total) * length) if total > 0 else 0
    return "█" * filled + "░" * (length - filled)


# ==================== SETTINGS MODALS ====================

class SettingsModal(ui.Modal, title="⚙️ Game Settings"):
    """Modal for configuring game time settings"""
    
    discussion_time = ui.TextInput(
        label="Discussion Time (30-600 seconds)",
        placeholder="300",
        default="300",
        min_length=2,
        max_length=3,
        required=True
    )
    
    voting_time = ui.TextInput(
        label="Voting Time (30-300 seconds)",
        placeholder="60",
        default="60",
        min_length=2,
        max_length=3,
        required=True
    )
    
    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        game = get_game(guild_id)
        if game:
            self.discussion_time.default = str(game.settings.discussion_time)
            self.voting_time.default = str(game.settings.voting_time)
    
    async def on_submit(self, interaction: discord.Interaction):
        game = get_game(self.guild_id)
        if not game:
            await interaction.response.send_message("❌ No active game!", ephemeral=True)
            return
        
        errors = []
        
        try:
            disc_time = int(self.discussion_time.value)
            if 30 <= disc_time <= 600:
                game.settings.discussion_time = disc_time
            else:
                errors.append("Discussion time must be 30-600 seconds")
        except ValueError:
            errors.append("Discussion time must be a number")
        
        try:
            vote = int(self.voting_time.value)
            if 30 <= vote <= 300:
                game.settings.voting_time = vote
            else:
                errors.append("Voting time must be 30-300 seconds")
        except ValueError:
            errors.append("Voting time must be a number")
        
        if errors:
            await interaction.response.send_message("❌ " + "\n".join(errors), ephemeral=True)
        else:
            await interaction.response.send_message("✅ Settings updated!", ephemeral=True)
            if game.registration_message:
                try:
                    view = RegistrationView(self.guild_id, game.host_id)
                    await view.update_registration_embed(game)
                except:
                    pass


class RevealModeSelect(ui.Select):
    """Dropdown for selecting role reveal mode"""
    def __init__(self, guild_id: int, current_mode: int):
        self.guild_id = guild_id
        options = [
            discord.SelectOption(
                label="Hidden", value="1",
                description="Just say 'voted out' — no role info",
                emoji="🚫",
                default=(current_mode == 1)
            ),
            discord.SelectOption(
                label="Mafia or Not", value="2",
                description="Reveal if they were Mafia or not",
                emoji="❓",
                default=(current_mode == 2)
            ),
            discord.SelectOption(
                label="Full Role + Suspense", value="3",
                description="Dramatic reveal with exact role",
                emoji="🎭",
                default=(current_mode == 3)
            ),
        ]
        super().__init__(placeholder="Select reveal mode...", options=options, min_values=1, max_values=1)
    
    async def callback(self, interaction: discord.Interaction):
        game = get_game(self.guild_id)
        if not game:
            await interaction.response.send_message("❌ No active game!", ephemeral=True)
            return
        
        mode = int(self.values[0])
        game.settings.role_reveal_mode = mode
        labels = {1: "Hidden", 2: "Mafia or Not", 3: "Full Role + Suspense"}
        
        self.disabled = True
        self.placeholder = f"✅ {labels[mode]}"
        await interaction.response.edit_message(view=self.view)
        
        # Update registration embed
        if game.registration_message:
            try:
                reg_view = RegistrationView(self.guild_id, game.host_id)
                await reg_view.update_registration_embed(game)
            except:
                pass


class RevealModeView(ui.View):
    def __init__(self, guild_id: int, current_mode: int):
        super().__init__(timeout=30)
        self.add_item(RevealModeSelect(guild_id, current_mode))


class RoleSettingsModal(ui.Modal, title="👥 Role Settings"):
    """Modal for configuring role counts"""
    
    num_mafia = ui.TextInput(
        label="Number of Mafia (1-5)",
        placeholder="1",
        default="1",
        min_length=1,
        max_length=1,
        required=True
    )
    
    num_doctor = ui.TextInput(
        label="Number of Doctors (0-3)",
        placeholder="1",
        default="1",
        min_length=1,
        max_length=1,
        required=True
    )
    
    num_police = ui.TextInput(
        label="Number of Police (0-3)",
        placeholder="1",
        default="1",
        min_length=1,
        max_length=1,
        required=True
    )
    
    def __init__(self, guild_id: int):
        super().__init__()
        self.guild_id = guild_id
        # Pre-fill with current settings
        game = get_game(guild_id)
        if game:
            self.num_mafia.default = str(game.settings.num_mafia)
            self.num_doctor.default = str(game.settings.num_doctor)
            self.num_police.default = str(game.settings.num_police)
    
    async def on_submit(self, interaction: discord.Interaction):
        game = get_game(self.guild_id)
        if not game:
            await interaction.response.send_message("❌ No active game!", ephemeral=True)
            return
        
        errors = []
        
        # Validate mafia count
        try:
            mafia = int(self.num_mafia.value)
            if 1 <= mafia <= 5:
                game.settings.num_mafia = mafia
            else:
                errors.append("Mafia count must be 1-5")
        except ValueError:
            errors.append("Mafia count must be a number")
        
        # Validate doctor count
        try:
            doctor = int(self.num_doctor.value)
            if 0 <= doctor <= 3:
                game.settings.num_doctor = doctor
            else:
                errors.append("Doctor count must be 0-3")
        except ValueError:
            errors.append("Doctor count must be a number")
        
        # Validate police count
        try:
            police = int(self.num_police.value)
            if 0 <= police <= 3:
                game.settings.num_police = police
            else:
                errors.append("Police count must be 0-3")
        except ValueError:
            errors.append("Police count must be a number")
        
        if errors:
            await interaction.response.send_message("❌ " + "\n".join(errors), ephemeral=True)
        else:
            await interaction.response.send_message("✅ Role settings updated!", ephemeral=True)
            # Update the registration embed
            if game.registration_message:
                try:
                    view = RegistrationView(self.guild_id, game.host_id)
                    await view.update_registration_embed(game)
                except:
                    pass


# ==================== REGISTRATION BUTTONS ====================

class RegistrationView(ui.View):
    def __init__(self, guild_id: int, host_id: int):
        super().__init__(timeout=None)
        self.guild_id = guild_id
        self.host_id = host_id
    
    async def update_registration_embed(self, game: GameState):
        """Update the registration message with current player list and settings"""
        try:
            if game.registration_message:
                if game.players:
                    player_list = "\n".join([f"• {p.name}" for p in game.players.values()])
                else:
                    player_list = "*No players yet*"
                
                min_players = game.settings.num_mafia + game.settings.num_doctor + game.settings.num_police + 1
                
                # Build settings display
                reveal_labels = {1: "Hidden", 2: "Mafia/Not", 3: "Full Role"}
                settings_text = (
                    f"🔪 **Mafia:** {game.settings.num_mafia} | "
                    f"💉 **Doctor:** {game.settings.num_doctor} | "
                    f"🔍 **Police:** {game.settings.num_police}\n"
                    f"💬 **Discussion:** {format_time(game.settings.discussion_time)} | "
                    f"🗳️ **Vote:** {format_time(game.settings.voting_time)} | "
                    f"👁️ **Reveal:** {reveal_labels.get(game.settings.role_reveal_mode, 'Full Role')}"
                )
                
                embed = discord.Embed(
                    title="🌙 Night Has Come - Registration",
                    description=f"Click **Join Game** to enter!\n\n**Players ({len(game.players)}):**\n{player_list}",
                    color=discord.Color.purple()
                )
                embed.add_field(name="📋 Requirements", value=f"Minimum **{min_players}** players", inline=False)
                embed.add_field(name="⚙️ Current Settings", value=settings_text, inline=False)
                embed.set_footer(text="Host: Use ⚙️ Settings or 👥 Roles buttons to customize")
                await game.registration_message.edit(embed=embed, view=self)
        except Exception as e:
            logger.error(f"Failed to update registration embed: {e}")
    
    @ui.button(label="Join", style=discord.ButtonStyle.green, custom_id="join_mafia_game", row=0)
    async def join_button(self, interaction: discord.Interaction, button: ui.Button):
        try:
            game = get_game(self.guild_id)
            if not game or game.phase != GamePhase.REGISTRATION:
                await interaction.response.send_message("No game is currently accepting players!", ephemeral=True)
                return
            
            if interaction.user.id in game.players:
                await interaction.response.send_message("You're already registered! Use 'Leave Game' to leave.", ephemeral=True)
                return
            
            player = Player(member=interaction.user, name=interaction.user.display_name)
            game.players[interaction.user.id] = player
            logger.info(f"Player {interaction.user.display_name} joined game in guild {self.guild_id}")
            
            await interaction.response.send_message(f"✅ You've joined the game as **{player.name}**!", ephemeral=True)
            await self.update_registration_embed(game)
        except Exception as e:
            logger.error(f"Error in join_button: {e}")
            await interaction.response.send_message("❌ An error occurred. Please try again.", ephemeral=True)
    
    @ui.button(label="Leave", style=discord.ButtonStyle.danger, custom_id="leave_mafia_game", row=0)
    async def leave_button(self, interaction: discord.Interaction, button: ui.Button):
        try:
            game = get_game(self.guild_id)
            if not game or game.phase != GamePhase.REGISTRATION:
                await interaction.response.send_message("No game is currently in registration!", ephemeral=True)
                return
            
            if interaction.user.id not in game.players:
                await interaction.response.send_message("You're not in the game!", ephemeral=True)
                return
            
            # Remove player
            player_name = game.players[interaction.user.id].name
            del game.players[interaction.user.id]
            logger.info(f"Player {player_name} left game in guild {self.guild_id}")
            
            await interaction.response.send_message(f"👋 You've left the game, **{player_name}**!", ephemeral=True)
            await self.update_registration_embed(game)
        except Exception as e:
            logger.error(f"Error in leave_button: {e}")
            await interaction.response.send_message("❌ An error occurred. Please try again.", ephemeral=True)
    
    @ui.button(label="Settings", style=discord.ButtonStyle.secondary, custom_id="game_settings", row=1)
    async def settings_button(self, interaction: discord.Interaction, button: ui.Button):
        """Open time settings modal"""
        try:
            game = get_game(self.guild_id)
            if not game or game.phase != GamePhase.REGISTRATION:
                await interaction.response.send_message("❌ No game in registration!", ephemeral=True)
                return
            
            # Only host or admin can change settings
            is_host = interaction.user.id == self.host_id
            is_admin = interaction.user.guild_permissions.administrator
            
            if not is_host and not is_admin:
                await interaction.response.send_message("❌ Only the host or admin can change settings!", ephemeral=True)
                return
            
            modal = SettingsModal(self.guild_id)
            await interaction.response.send_modal(modal)
        except Exception as e:
            logger.error(f"Error in settings_button: {e}")
            await interaction.response.send_message("❌ An error occurred.", ephemeral=True)
    
    @ui.button(label="Roles", style=discord.ButtonStyle.secondary, custom_id="role_settings", row=1)
    async def roles_button(self, interaction: discord.Interaction, button: ui.Button):
        """Open role settings modal"""
        try:
            game = get_game(self.guild_id)
            if not game or game.phase != GamePhase.REGISTRATION:
                await interaction.response.send_message("❌ No game in registration!", ephemeral=True)
                return
            
            is_host = interaction.user.id == self.host_id
            is_admin = interaction.user.guild_permissions.administrator
            
            if not is_host and not is_admin:
                await interaction.response.send_message("❌ Only the host or admin can change role settings!", ephemeral=True)
                return
            
            modal = RoleSettingsModal(self.guild_id)
            await interaction.response.send_modal(modal)
        except Exception as e:
            logger.error(f"Error in roles_button: {e}")
            await interaction.response.send_message("❌ An error occurred.", ephemeral=True)
    
    @ui.button(label="Reveal", style=discord.ButtonStyle.secondary, custom_id="reveal_settings", row=1)
    async def reveal_button(self, interaction: discord.Interaction, button: ui.Button):
        """Open reveal mode dropdown"""
        try:
            game = get_game(self.guild_id)
            if not game or game.phase != GamePhase.REGISTRATION:
                await interaction.response.send_message("❌ No game in registration!", ephemeral=True)
                return
            
            is_host = interaction.user.id == self.host_id
            is_admin = interaction.user.guild_permissions.administrator
            
            if not is_host and not is_admin:
                await interaction.response.send_message("❌ Only the host or admin can change this!", ephemeral=True)
                return
            
            view = RevealModeView(self.guild_id, game.settings.role_reveal_mode)
            await interaction.response.send_message("👁️ **Choose what to reveal when a player is voted out:**", view=view, ephemeral=True)
        except Exception as e:
            logger.error(f"Error in reveal_button: {e}")
            await interaction.response.send_message("❌ An error occurred.", ephemeral=True)
    
    @ui.button(label="Start", style=discord.ButtonStyle.primary, custom_id="start_mafia_game", row=0)
    async def start_button(self, interaction: discord.Interaction, button: ui.Button):
        try:
            game = get_game(self.guild_id)
            if not game or game.phase != GamePhase.REGISTRATION:
                await interaction.response.send_message("No game is currently in registration!", ephemeral=True)
                return
            
            # Only host or admin can start
            is_host = interaction.user.id == self.host_id
            is_admin = interaction.user.guild_permissions.administrator
            
            if not is_host and not is_admin:
                await interaction.response.send_message("❌ Only the game host or an admin can start the game!", ephemeral=True)
                return
            
            min_players = game.settings.num_mafia + game.settings.num_doctor + game.settings.num_police + 1
            
            if len(game.players) < min_players:
                await interaction.response.send_message(
                    f"❌ Need at least **{min_players}** players to start! Currently have **{len(game.players)}**.",
                    ephemeral=True
                )
                return
            
            await interaction.response.send_message("🎮 **Starting the game!**", ephemeral=False)
            logger.info(f"Game started by {interaction.user.display_name} in guild {self.guild_id} with {len(game.players)} players")
            
            # Disable buttons
            for item in self.children:
                item.disabled = True
            await game.registration_message.edit(view=self)
            
            # Assign roles and start
            await assign_roles(game)
            await asyncio.sleep(3)
            await start_night_phase(game)
        except Exception as e:
            logger.error(f"Error in start_button: {e}")
            await interaction.response.send_message("❌ An error occurred while starting the game.", ephemeral=True)
    
    @ui.button(label="Exit", style=discord.ButtonStyle.danger, custom_id="end_mafia_game", row=2)
    async def end_button(self, interaction: discord.Interaction, button: ui.Button):
        try:
            game = get_game(self.guild_id)
            if not game:
                await interaction.response.send_message("No active game!", ephemeral=True)
                return
            
            # Only host or admin can end
            is_host = interaction.user.id == self.host_id
            is_admin = interaction.user.guild_permissions.administrator
            
            if not is_host and not is_admin:
                await interaction.response.send_message("❌ Only the host or an admin can end the game!", ephemeral=True)
                return
            
            game.phase = GamePhase.ENDED
            
            # Disable all buttons
            for item in self.children:
                item.disabled = True
            if game.registration_message:
                await game.registration_message.edit(view=self)
            
            await interaction.response.send_message("🛑 **Game has been ended by the host.**")
            
            # Unmute all players
            for player in game.players.values():
                if hasattr(player.member, 'voice') and player.member.voice:
                    try:
                        await player.member.edit(mute=False)
                    except:
                        pass
            
            # Clean up
            await delete_game_messages(game)
            if self.guild_id in active_games:
                del active_games[self.guild_id]
            
            logger.info(f"Game ended by {interaction.user.display_name} in guild {self.guild_id}")
        except Exception as e:
            logger.error(f"Error in end_button: {e}")
            await interaction.response.send_message("❌ An error occurred.", ephemeral=True)


# Legacy alias for backwards compatibility
class JoinGameButton(RegistrationView):
    pass


# ==================== VOTING VIEW ====================

class VotingView(ui.View):
    def __init__(self, game: GameState, timeout: int):
        super().__init__(timeout=timeout)
        self.game = game
        
        # Add player buttons
        alive_players = [p for p in game.players.values() if p.is_alive]
        for player in alive_players:
            button = ui.Button(
                label=player.name,
                style=discord.ButtonStyle.primary,
                custom_id=f"vote_{player.member.id}"
            )
            button.callback = self.create_vote_callback(player.member.id)
            self.add_item(button)
        
        # Add skip button
        skip_button = ui.Button(label="⏭️ Skip", style=discord.ButtonStyle.secondary, custom_id="vote_skip")
        skip_button.callback = self.skip_callback
        self.add_item(skip_button)
    
    def create_vote_callback(self, target_id: int):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id not in self.game.players:
                await interaction.response.send_message("You're not in this game!", ephemeral=True)
                return
            
            player = self.game.players[interaction.user.id]
            if not player.is_alive:
                await interaction.response.send_message("Dead players cannot vote!", ephemeral=True)
                return
            
            # Check if changing vote
            previous_vote = self.game.day_votes.get(interaction.user.id)
            self.game.day_votes[interaction.user.id] = target_id
            target_name = self.game.players[target_id].name
            
            if previous_vote is not None and previous_vote != target_id:
                if previous_vote in self.game.players:
                    old_target = self.game.players[previous_vote].name
                    await interaction.response.send_message(f"🔄 Vote changed from **{old_target}** to **{target_name}**", ephemeral=True)
                else:
                    await interaction.response.send_message(f"🔄 Vote changed to **{target_name}**", ephemeral=True)
            elif previous_vote is None and interaction.user.id in self.game.day_votes:
                await interaction.response.send_message(f"🔄 Changed from skip to **{target_name}**", ephemeral=True)
            else:
                await interaction.response.send_message(f"✅ You voted for **{target_name}**", ephemeral=True)
        
        return callback
    
    async def skip_callback(self, interaction: discord.Interaction):
        if interaction.user.id not in self.game.players:
            await interaction.response.send_message("You're not in this game!", ephemeral=True)
            return
        
        player = self.game.players[interaction.user.id]
        if not player.is_alive:
            await interaction.response.send_message("Dead players cannot vote!", ephemeral=True)
            return
        
        # Check if changing vote
        previous_vote = self.game.day_votes.get(interaction.user.id)
        self.game.day_votes[interaction.user.id] = None  # None means skip
        
        if previous_vote is not None:
            old_target = self.game.players[previous_vote].name if previous_vote in self.game.players else "someone"
            await interaction.response.send_message(f"🔄 Changed vote from **{old_target}** to **skip**", ephemeral=True)
        else:
            await interaction.response.send_message("✅ You chose to **skip** this vote", ephemeral=True)


# ==================== MAFIA TARGET SELECT ====================

class MafiaTargetSelect(ui.Select):
    def __init__(self, game: GameState, mafia_player: Player):
        self.game = game
        self.mafia_player = mafia_player
        
        options = [
            discord.SelectOption(label=p.name, value=str(p.member.id))
            for p in game.players.values()
            if p.is_alive and p.role != Role.MAFIA
        ]
        
        # Add skip option if mafia has skips remaining
        skips_remaining = game.settings.mafia_skip_kills - game.mafia_skips_used
        if skips_remaining > 0:
            options.append(discord.SelectOption(
                label=f"⏭️ Skip Kill ({skips_remaining} left)",
                value="skip_kill",
                description="No one dies tonight"
            ))
        
        super().__init__(placeholder="Select target to eliminate...", options=options)
    
    async def callback(self, interaction: discord.Interaction):
        player_id = self.mafia_player.member.id
        already_submitted = player_id in self.game.night_actions_submitted
        
        if already_submitted:
            await interaction.response.send_message("❌ You have already made your choice this round!", ephemeral=True)
            return
        
        selected = self.values[0]
        
        if selected == "skip_kill":
            confirm_view = MafiaConfirmView(self.game, self.mafia_player, None, self.view)
            await interaction.response.send_message(
                "⏭️ You selected to **skip the kill** tonight.\nAre you sure?",
                view=confirm_view, ephemeral=True
            )
        else:
            target_id = int(selected)
            target_name = self.game.players[target_id].name
            confirm_view = MafiaConfirmView(self.game, self.mafia_player, target_id, self.view)
            await interaction.response.send_message(
                f"🔪 You selected **{target_name}** as your target.\nAre you sure?",
                view=confirm_view, ephemeral=True
            )


class MafiaConfirmView(ui.View):
    """Confirmation buttons for Mafia kill choice"""
    def __init__(self, game: GameState, mafia_player: Player, target_id: int | None, parent_view: ui.View):
        super().__init__(timeout=30)
        self.game = game
        self.mafia_player = mafia_player
        self.target_id = target_id  # None = skip
        self.parent_view = parent_view

    @ui.button(label="✅ Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        player_id = self.mafia_player.member.id
        if player_id in self.game.night_actions_submitted:
            await interaction.response.edit_message(content="❌ You already locked in your choice!", view=None)
            return

        if self.target_id is None:
            self.game.mafia_votes[player_id] = -1
            await interaction.response.edit_message(content="⏭️ Confirmed: **skip the kill** tonight.", view=None)
            for p in self.game.players.values():
                if p.role == Role.MAFIA and p.member.id != player_id and p.is_alive:
                    try: await p.member.send(f"⏭️ **{self.mafia_player.name}** voted to **skip the kill** tonight.")
                    except: pass
        else:
            self.game.mafia_votes[player_id] = self.target_id
            target_name = self.game.players[self.target_id].name
            await interaction.response.edit_message(content=f"🔪 Confirmed: eliminate **{target_name}**.", view=None)
            for p in self.game.players.values():
                if p.role == Role.MAFIA and p.member.id != player_id and p.is_alive:
                    try: await p.member.send(f"🔪 **{self.mafia_player.name}** voted to eliminate **{target_name}**")
                    except: pass

        self.game.night_actions_submitted.add(player_id)
        self.game.night_actions_received += 1
        # Disable the parent select
        for item in self.parent_view.children:
            item.disabled = True
            item.placeholder = "✅ Choice locked in"
        try: await interaction.message.edit(view=None)
        except: pass
        try: await self.mafia_player.member.send("✅ Your night action is locked in.")
        except: pass
        await check_all_night_actions_done(self.game)

    @ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content="↩️ Selection cancelled. Pick again from the dropdown above.", view=None)


class MafiaTargetView(ui.View):
    def __init__(self, game: GameState, mafia_player: Player):
        super().__init__(timeout=None)
        self.add_item(MafiaTargetSelect(game, mafia_player))


# ==================== DOCTOR SAVE SELECT ====================

class DoctorSaveSelect(ui.Select):
    def __init__(self, game: GameState, doctor_player: Player):
        self.game = game
        self.doctor_player = doctor_player
        
        options = []
        for p in game.players.values():
            if p.is_alive:
                # If it's the doctor themselves and they used self-save last round, skip
                if p.member.id == doctor_player.member.id and doctor_player.doctor_self_save_used:
                    continue
                options.append(discord.SelectOption(label=p.name, value=str(p.member.id)))
        
        super().__init__(placeholder="Select who to save...", options=options if options else [discord.SelectOption(label="No one", value="none")])
    
    async def callback(self, interaction: discord.Interaction):
        player_id = self.doctor_player.member.id
        
        if player_id in self.game.night_actions_submitted:
            await interaction.response.send_message("❌ You have already made your choice this round!", ephemeral=True)
            return
        
        if self.values[0] == "none":
            await interaction.response.send_message("💉 There's no one you can save this round.", ephemeral=True)
            self.game.night_actions_submitted.add(player_id)
            self.game.night_actions_received += 1
            self.disabled = True
            self.placeholder = "✅ Choice submitted"
            await interaction.edit_original_response(view=self.view)
            await check_all_night_actions_done(self.game)
            return
        
        target_id = int(self.values[0])
        target_name = self.game.players[target_id].name
        confirm_view = DoctorConfirmView(self.game, self.doctor_player, target_id, self.view)
        await interaction.response.send_message(
            f"💉 You selected to save **{target_name}**.\nAre you sure?",
            view=confirm_view, ephemeral=True
        )


class DoctorConfirmView(ui.View):
    """Confirmation buttons for Doctor save choice"""
    def __init__(self, game: GameState, doctor_player: Player, target_id: int, parent_view: ui.View):
        super().__init__(timeout=30)
        self.game = game
        self.doctor_player = doctor_player
        self.target_id = target_id
        self.parent_view = parent_view

    @ui.button(label="✅ Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        player_id = self.doctor_player.member.id
        if player_id in self.game.night_actions_submitted:
            await interaction.response.edit_message(content="❌ You already locked in your choice!", view=None)
            return

        self.game.doctor_save = self.target_id
        target_name = self.game.players[self.target_id].name

        if self.target_id == self.doctor_player.member.id:
            self.doctor_player.doctor_self_save_used = True
        else:
            self.doctor_player.doctor_self_save_used = False

        await interaction.response.edit_message(content=f"💉 Confirmed: saving **{target_name}**.", view=None)

        self.game.night_actions_submitted.add(player_id)
        self.game.night_actions_received += 1
        for item in self.parent_view.children:
            item.disabled = True
            item.placeholder = "✅ Choice locked in"
        try: await interaction.message.edit(view=None)
        except: pass
        try: await self.doctor_player.member.send("✅ Your night action is locked in.")
        except: pass
        await check_all_night_actions_done(self.game)

    @ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content="↩️ Selection cancelled. Pick again from the dropdown above.", view=None)


class DoctorSaveView(ui.View):
    def __init__(self, game: GameState, doctor_player: Player):
        super().__init__(timeout=None)
        self.add_item(DoctorSaveSelect(game, doctor_player))


# ==================== POLICE INVESTIGATE SELECT ====================

class PoliceInvestigateSelect(ui.Select):
    def __init__(self, game: GameState, police_player: Player):
        self.game = game
        self.police_player = police_player
        
        options = [
            discord.SelectOption(label=p.name, value=str(p.member.id))
            for p in game.players.values()
            if p.is_alive and p.member.id != police_player.member.id
        ]
        
        super().__init__(placeholder="Select who to investigate...", options=options)
    
    async def callback(self, interaction: discord.Interaction):
        player_id = self.police_player.member.id
        
        if player_id in self.game.night_actions_submitted:
            await interaction.response.send_message("❌ You have already investigated someone this round!", ephemeral=True)
            return
        
        target_id = int(self.values[0])
        target = self.game.players[target_id]
        confirm_view = PoliceConfirmView(self.game, self.police_player, target_id, self.view)
        await interaction.response.send_message(
            f"🔍 You selected to investigate **{target.name}**.\nAre you sure?",
            view=confirm_view, ephemeral=True
        )


class PoliceConfirmView(ui.View):
    """Confirmation buttons for Police investigate choice"""
    def __init__(self, game: GameState, police_player: Player, target_id: int, parent_view: ui.View):
        super().__init__(timeout=30)
        self.game = game
        self.police_player = police_player
        self.target_id = target_id
        self.parent_view = parent_view

    @ui.button(label="✅ Confirm", style=discord.ButtonStyle.success)
    async def confirm(self, interaction: discord.Interaction, button: ui.Button):
        player_id = self.police_player.member.id
        if player_id in self.game.night_actions_submitted:
            await interaction.response.edit_message(content="❌ You already locked in your choice!", view=None)
            return

        self.game.police_investigation = self.target_id
        target = self.game.players[self.target_id]
        is_mafia = target.role == Role.MAFIA
        result = "🔴 **IS MAFIA!**" if is_mafia else "🟢 **NOT Mafia**"

        await interaction.response.edit_message(
            content=f"🔍 Investigation result for **{target.name}**: {result}", view=None
        )

        self.game.night_actions_submitted.add(player_id)
        self.game.night_actions_received += 1
        for item in self.parent_view.children:
            item.disabled = True
            item.placeholder = "✅ Investigation complete"
        try: await interaction.message.edit(view=None)
        except: pass
        try: await self.police_player.member.send("✅ Your night action is locked in.")
        except: pass
        await check_all_night_actions_done(self.game)

    @ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: ui.Button):
        await interaction.response.edit_message(content="↩️ Selection cancelled. Pick again from the dropdown above.", view=None)


class PoliceInvestigateView(ui.View):
    def __init__(self, game: GameState, police_player: Player):
        super().__init__(timeout=None)
        self.add_item(PoliceInvestigateSelect(game, police_player))


# ==================== PHASE CONTROL BUTTONS ====================

class NightEndView(ui.View):
    """Button for host/admin to end the night phase and proceed to day"""
    def __init__(self, game: GameState, host_id: int):
        super().__init__(timeout=None)
        self.game = game
        self.host_id = host_id
    
    @ui.button(label="☀️ End Night", style=discord.ButtonStyle.primary, custom_id="end_night_phase")
    async def end_night_button(self, interaction: discord.Interaction, button: ui.Button):
        try:
            # Only host or admin can click
            is_host = interaction.user.id == self.host_id
            is_admin = interaction.user.guild_permissions.administrator
            
            if not is_host and not is_admin:
                await interaction.response.send_message("❌ Only the host or admin can end the night!", ephemeral=True)
                return
            
            if self.game.phase != GamePhase.NIGHT:
                await interaction.response.send_message("❌ It's not night time!", ephemeral=True)
                return
            
            # Disable the button
            button.disabled = True
            button.label = "☀️ Night Ended"
            await interaction.response.edit_message(view=self)
            
            logger.info(f"Night ended manually by {interaction.user.display_name}")
            
            # Process night results
            await process_night_results(self.game)
        except Exception as e:
            logger.error(f"Error in end_night_button: {e}")
            try:
                await interaction.response.send_message("❌ An error occurred.", ephemeral=True)
            except:
                pass


class DiscussionEndView(ui.View):
    """Button for host/admin to end discussion, with auto-timer fallback"""
    def __init__(self, game: GameState, host_id: int, discussion_time: int):
        super().__init__(timeout=None)
        self.game = game
        self.host_id = host_id
        self.discussion_time = discussion_time
        self.discussion_message: Optional[discord.Message] = None
    
    async def start_timer(self):
        """Background countdown that auto-starts voting when time runs out"""
        elapsed = 0
        while elapsed < self.discussion_time:
            if self.game.discussion_ended or self.game.phase != GamePhase.DAY:
                return
            await asyncio.sleep(1)
            elapsed += 1
            # Update countdown every 30 seconds
            remaining = self.discussion_time - elapsed
            if remaining > 0 and remaining % 30 == 0 and self.discussion_message:
                try:
                    await self.discussion_message.edit(
                        content=f"💬 **Discussion time!** ⏱️ **{format_time(remaining)}** remaining\nHost can also click **🗳️ Start Voting** to skip."
                    )
                except:
                    pass
            # Warning at 10 seconds
            if remaining == 10 and self.discussion_message:
                try:
                    await self.discussion_message.edit(
                        content=f"💬 **Discussion time!** ⏱️ **10s** remaining — voting starts soon!"
                    )
                except:
                    pass
        
        # Timer expired — auto-start voting
        if not self.game.discussion_ended and self.game.phase == GamePhase.DAY:
            self.game.discussion_ended = True
            for item in self.children:
                item.disabled = True
            if self.discussion_message:
                try:
                    await self.discussion_message.edit(content="⏰ **Discussion time is over!**", view=self)
                except:
                    pass
            logger.info("Discussion ended by timer")
            await start_voting_phase(self.game)
    
    @ui.button(label="🗳️ Start Voting", style=discord.ButtonStyle.danger, custom_id="start_voting_phase")
    async def start_voting_button(self, interaction: discord.Interaction, button: ui.Button):
        try:
            is_host = interaction.user.id == self.host_id
            is_admin = interaction.user.guild_permissions.administrator
            
            if not is_host and not is_admin:
                await interaction.response.send_message("❌ Only the host or admin can start voting!", ephemeral=True)
                return
            
            if self.game.phase != GamePhase.DAY or self.game.discussion_ended:
                await interaction.response.send_message("❌ Voting has already started!", ephemeral=True)
                return
            
            self.game.discussion_ended = True
            button.disabled = True
            button.label = "🗳️ Voting Started"
            await interaction.response.edit_message(view=self)
            
            logger.info(f"Voting started manually by {interaction.user.display_name}")
            await start_voting_phase(self.game)
        except Exception as e:
            logger.error(f"Error in start_voting_button: {e}")
            try:
                await interaction.response.send_message("❌ An error occurred.", ephemeral=True)
            except:
                pass


class NextRoundView(ui.View):
    """Button for host/admin to start the next night round"""
    def __init__(self, game: GameState, host_id: int):
        super().__init__(timeout=None)
        self.game = game
        self.host_id = host_id
    
    @ui.button(label="🌙 Start Night", style=discord.ButtonStyle.primary, custom_id="start_next_night")
    async def start_night_button(self, interaction: discord.Interaction, button: ui.Button):
        try:
            # Only host or admin can click
            is_host = interaction.user.id == self.host_id
            is_admin = interaction.user.guild_permissions.administrator
            
            if not is_host and not is_admin:
                await interaction.response.send_message("❌ Only the host or admin can start the next round!", ephemeral=True)
                return
            
            if self.game.phase == GamePhase.ENDED:
                await interaction.response.send_message("❌ The game has ended!", ephemeral=True)
                return
            
            # Disable the button
            button.disabled = True
            button.label = "🌙 Night Starting..."
            await interaction.response.edit_message(view=self)
            
            logger.info(f"Next night started manually by {interaction.user.display_name}")
            
            # Start next night
            await start_night_phase(self.game)
        except Exception as e:
            logger.error(f"Error in start_night_button: {e}")
            try:
                await interaction.response.send_message("❌ An error occurred.", ephemeral=True)
            except:
                pass


# ==================== MAFIA CHAT RELAY ====================

async def relay_mafia_message(game: GameState, sender: Player, message: str):
    """Relay message from one mafia to all other mafias"""
    for player in game.players.values():
        if player.role == Role.MAFIA and player.member.id != sender.member.id and player.is_alive:
            try:
                await player.member.send(f"🗣️ **{sender.name}** (Mafia): {message}")
            except:
                pass


# ==================== GAME LOGIC ====================

async def assign_roles(game: GameState):
    """Assign roles to all players"""
    player_list = list(game.players.values())
    random.shuffle(player_list)
    
    idx = 0
    
    # Assign Mafia
    for _ in range(game.settings.num_mafia):
        if idx < len(player_list):
            player_list[idx].role = Role.MAFIA
            idx += 1
    
    # Assign Doctor
    for _ in range(game.settings.num_doctor):
        if idx < len(player_list):
            player_list[idx].role = Role.DOCTOR
            idx += 1
    
    # Assign Police
    for _ in range(game.settings.num_police):
        if idx < len(player_list):
            player_list[idx].role = Role.POLICE
            idx += 1
    
    # Rest are Citizens
    for i in range(idx, len(player_list)):
        player_list[i].role = Role.CITIZEN
    
    # DM each player their role with enhanced formatting
    role_icons = {
        Role.CITIZEN: "🧑‍🤝‍🧑",
        Role.MAFIA: "🔪",
        Role.DOCTOR: "💉",
        Role.POLICE: "🔍"
    }
    
    role_tips = {
        Role.CITIZEN: "💡 **Quick Tips:**\n• Watch for suspicious behavior\n• Note who accuses whom\n• Trust your instincts!",
        Role.MAFIA: "💡 **Quick Tips:**\n• Blend in with citizens\n• Coordinate with fellow Mafia via DM\n• Create alibis during the day",
        Role.DOCTOR: "💡 **Quick Tips:**\n• Try to predict Mafia targets\n• Don't reveal your role easily\n• Remember: Can't self-save twice in a row",
        Role.POLICE: "💡 **Quick Tips:**\n• Investigate suspicious players\n• Be careful revealing findings\n• Share info wisely to avoid being targeted"
    }
    
    for player in player_list:
        role_desc = get_role_description(player.role)
        icon = role_icons.get(player.role, "🎭")
        tips = role_tips.get(player.role, "")
        
        embed = discord.Embed(
            title=f"{icon} Your Role: {player.role.value}",
            description=f"{role_desc}",
            color=get_role_color(player.role)
        )
        
        # Add tips
        if tips:
            embed.add_field(name="\u200b", value=tips, inline=False)
        
        # If mafia, tell them who other mafias are
        if player.role == Role.MAFIA:
            other_mafia = [p.name for p in player_list if p.role == Role.MAFIA and p.member.id != player.member.id]
            if other_mafia:
                embed.add_field(name="🔪 Fellow Mafia", value="\n".join([f"• {name}" for name in other_mafia]), inline=False)
            else:
                embed.add_field(name="🔪 Solo Mission", value="You are the only Mafia. Good luck!", inline=False)
        
        # Add player count
        embed.set_footer(text=f"👥 {len(player_list)} players in this game")
        
        try:
            await player.member.send(embed=embed)
        except:
            pass


def get_role_description(role: Role) -> str:
    descriptions = {
        Role.CITIZEN: "Your goal is to identify and eliminate all Mafia members through voting. Stay alive and observe carefully!",
        Role.MAFIA: "Your goal is to eliminate all citizens without being caught. During the night, you'll choose someone to eliminate. You can chat with other Mafia members during the night.",
        Role.DOCTOR: "Your goal is to save citizens from the Mafia. Each night, you can choose one person to protect. Note: If you save yourself, you cannot save yourself the next round!",
        Role.POLICE: "Your goal is to identify the Mafia. Each night, you can investigate one person to learn if they are Mafia or not."
    }
    return descriptions.get(role, "")


def get_role_color(role: Role) -> discord.Color:
    colors = {
        Role.CITIZEN: discord.Color.green(),
        Role.MAFIA: discord.Color.red(),
        Role.DOCTOR: discord.Color.blue(),
        Role.POLICE: discord.Color.gold()
    }
    return colors.get(role, discord.Color.light_grey())


async def check_all_night_actions_done(game: GameState):
    """Check if all night actions have been submitted, auto-end night after 10s delay"""
    if game.phase != GamePhase.NIGHT or game.night_auto_end_triggered:
        return
    
    if game.night_actions_received >= game.night_actions_expected:
        game.night_auto_end_triggered = True
        
        # Fire off the delayed processing as a background task
        # so the interaction callback returns immediately
        async def _delayed_night_end():
            try:
                await asyncio.sleep(10)
                
                # Check if game was force stopped during wait
                if game.phase == GamePhase.ENDED:
                    return
                
                await process_night_results(game)
            except Exception as e:
                logger.error(f"Error in delayed night end: {e}", exc_info=True)
        
        asyncio.create_task(_delayed_night_end())


async def start_night_phase(game: GameState):
    """Start the night phase"""
    # Check if game was force stopped
    if game.phase == GamePhase.ENDED:
        return
    
    game.phase = GamePhase.NIGHT
    game.round_number += 1
    game.mafia_votes.clear()
    game.mafia_target = None
    game.doctor_save = None
    game.police_investigation = None
    game.night_actions_submitted.clear()
    
    # Play "Night Has Come" announcement
    await play_announcement(game, "night_has_come")
    
    # Calculate game stats
    alive_count = sum(1 for p in game.players.values() if p.is_alive)
    dead_count = len(game.players) - alive_count
    
    # Announce night in text with game status
    embed = discord.Embed(
        title="🌙 Night Has Come",
        description="Everyone go to sleep... Close your eyes.\n\n*The night actions are now taking place in DMs.*",
        color=discord.Color.dark_purple()
    )
    embed.add_field(name="📊 Game Status", value=f"🔄 Round **{game.round_number}** | 🧑 **{alive_count}** alive | ☠️ **{dead_count}** dead", inline=False)
    
    await send_game_message(game, embed=embed)
    
    # Reset night action tracking
    game.night_actions_received = 0
    game.night_auto_end_triggered = False
    
    # Calculate expected night actions
    alive_mafia = [p for p in game.players.values() if p.role == Role.MAFIA and p.is_alive]
    alive_doctors = [p for p in game.players.values() if p.role == Role.DOCTOR and p.is_alive]
    alive_police = [p for p in game.players.values() if p.role == Role.POLICE and p.is_alive]
    
    # Count expected actions (only from real players, not bots in test mode)
    if game.settings.test_mode:
        real_mafia = [p for p in alive_mafia if not isinstance(p.member, DummyMember)]
        real_doctors = [p for p in alive_doctors if not isinstance(p.member, DummyMember)]
        real_police = [p for p in alive_police if not isinstance(p.member, DummyMember)]
        game.night_actions_expected = len(real_mafia) + len(real_doctors) + len(real_police)
    else:
        game.night_actions_expected = len(alive_mafia) + len(alive_doctors) + len(alive_police)
    
    # Mute all alive players during night
    for player in game.players.values():
        if player.is_alive and hasattr(player.member, 'voice') and player.member.voice:
            try:
                await player.member.edit(mute=True)
            except discord.errors.Forbidden:
                logger.warning(f"No permission to mute {player.name}")
            except Exception as e:
                logger.warning(f"Failed to mute {player.name}: {e}")
    
    # Mafia selection
    for mafia in alive_mafia:
        try:
            view = MafiaTargetView(game, mafia)
            embed = discord.Embed(
                title="🔪 Mafia Night Action",
                description="Choose your target to eliminate.\n\nYou can also type messages here to communicate with other Mafia members.",
                color=discord.Color.red()
            )
            await mafia.member.send(embed=embed, view=view)
        except:
            pass
    
    # In test mode, auto-target a random non-mafia player for bot mafia
    if game.settings.test_mode:
        bot_mafia = [p for p in alive_mafia if isinstance(p.member, DummyMember)]
        if bot_mafia:
            possible_targets = [p for p in game.players.values() 
                              if p.is_alive and p.role != Role.MAFIA]
            if possible_targets:
                target = random.choice(possible_targets)
                for mafia in bot_mafia:
                    game.mafia_votes[mafia.member.id] = target.member.id
                    game.night_actions_received += 1
                await send_game_message(game, content=f"🤖 *(Test Mode) Bot Mafia auto-targeted **{target.name}***")
    
    # Doctor selection
    for doctor in alive_doctors:
        try:
            view = DoctorSaveView(game, doctor)
            embed = discord.Embed(
                title="💉 Doctor Night Action",
                description="Choose who to save tonight.",
                color=discord.Color.blue()
            )
            if doctor.doctor_self_save_used:
                embed.add_field(name="⚠️ Note", value="You saved yourself last round, so you cannot save yourself this round.", inline=False)
            await doctor.member.send(embed=embed, view=view)
        except:
            pass
    
    # Police investigation
    for police in alive_police:
        try:
            view = PoliceInvestigateView(game, police)
            embed = discord.Embed(
                title="🔍 Police Night Action",
                description="Choose who to investigate tonight.",
                color=discord.Color.gold()
            )
            await police.member.send(embed=embed, view=view)
        except:
            pass
    
    # In test mode, auto-act for bot doctors and police
    if game.settings.test_mode:
        bot_doctors = [p for p in alive_doctors if isinstance(p.member, DummyMember)]
        for doctor in bot_doctors:
            possible_saves = [p for p in game.players.values() if p.is_alive]
            if possible_saves:
                save_target = random.choice(possible_saves)
                game.doctor_save = save_target.member.id
                game.night_actions_received += 1
                await send_game_message(game, content=f"🤖 *(Test Mode) Bot Doctor auto-saved **{save_target.name}***")
        
        bot_police = [p for p in alive_police if isinstance(p.member, DummyMember)]
        for police_p in bot_police:
            possible_targets = [p for p in game.players.values() if p.is_alive and p.member.id != police_p.member.id]
            if possible_targets:
                investigate_target = random.choice(possible_targets)
                game.police_investigation = investigate_target.member.id
                game.night_actions_received += 1
                is_mafia = investigate_target.role == Role.MAFIA
                result_text = "IS MAFIA" if is_mafia else "NOT Mafia"
                await send_game_message(game, content=f"🤖 *(Test Mode) Bot Police investigated **{investigate_target.name}** — {result_text}*")
    
    # Poll every 30 seconds — remind players who haven't chosen yet
    time_waited = 0
    while game.phase == GamePhase.NIGHT and not game.night_auto_end_triggered:
        if game.night_actions_received >= game.night_actions_expected:
            game.night_auto_end_triggered = True
            
            await asyncio.sleep(10)
            
            if game.phase == GamePhase.ENDED:
                return
            
            await process_night_results(game)
            return
        
        await asyncio.sleep(2)
        time_waited += 2
        
        # Every 30 seconds, remind players who haven't submitted
        if time_waited % 30 == 0 and time_waited > 0:
            pending = game.night_actions_expected - game.night_actions_received
            # DM reminder to players who haven't submitted
            for player in game.players.values():
                if not player.is_alive:
                    continue
                if player.member.id in game.night_actions_submitted:
                    continue
                if isinstance(player.member, DummyMember):
                    continue
                if player.role in (Role.MAFIA, Role.DOCTOR, Role.POLICE):
                    try:
                        await player.member.send(f"⏰ **Reminder:** Please make your night action choice! The game is waiting for you.")
                    except:
                        pass


async def process_night_results(game: GameState):
    """Process the results of night actions"""
    mafia_skipped = False
    
    # Determine mafia target (majority vote among mafia)
    if game.mafia_votes:
        vote_counts = {}
        for target_id in game.mafia_votes.values():
            vote_counts[target_id] = vote_counts.get(target_id, 0) + 1
        
        if vote_counts:
            max_count = max(vote_counts.values())
            tied_targets = [t for t, c in vote_counts.items() if c == max_count]
            top_vote = random.choice(tied_targets)
            if top_vote == -1:
                # Mafia chose to skip
                mafia_skipped = True
                game.mafia_skips_used += 1
                game.mafia_target = None
                if game.settings.test_mode:
                    await send_game_message(game, content="🤖 *(Test Mode) Mafia chose to skip their kill this round*")
            else:
                game.mafia_target = top_vote
    
    # Check if doctor saved the target
    saved = game.mafia_target is not None and game.mafia_target == game.doctor_save
    if saved and game.settings.test_mode:
        target = game.players[game.mafia_target]
        await send_game_message(game, content=f"🤖 *(Test Mode) Doctor saved **{target.name}** from Mafia attack*")
    
    # Start day phase
    await start_day_phase(game, saved, mafia_skipped)


async def start_day_phase(game: GameState, was_saved: bool, mafia_skipped: bool = False):
    """Start the day phase"""
    # Check if game was force stopped
    if game.phase == GamePhase.ENDED:
        return
    
    game.phase = GamePhase.DAY
    
    # Play "Night Is Over" announcement
    await play_announcement(game, "night_is_over")
    
    # Unmute ONLY alive players (dead stay muted throughout the game)
    for player in game.players.values():
        if hasattr(player.member, 'voice') and player.member.voice:
            try:
                if player.is_alive:
                    await player.member.edit(mute=False)
                else:
                    # Dead players stay muted
                    await player.member.edit(mute=True)
            except discord.errors.Forbidden:
                logger.warning(f"No permission to edit {player.name}")
            except Exception as e:
                logger.warning(f"Failed to edit {player.name}: {e}")
    
    # Play saved announcement if someone was saved (but don't reveal who)
    if was_saved:
        await play_announcement(game, "someone_saved")
    
    # Announce day in text
    if game.mafia_target:
        target = game.players[game.mafia_target]
        if was_saved:
            embed = discord.Embed(
                title="☀️ Morning Has Come",
                description="Everyone wake up! Open your eyes.\n\n😴 **Everyone is safe!** No one died during the night.",
                color=discord.Color.gold()
            )
        else:
            target.is_alive = False
            reveal_mode = game.settings.role_reveal_mode

            if reveal_mode == 1:
                # Mode 1: No reveal
                embed = discord.Embed(
                    title=f"💀  {target.name}  KILLED",
                    description=f"**{target.name}** was murdered by the Mafia during the night!",
                    color=discord.Color.dark_red()
                )
            elif reveal_mode == 2:
                # Mode 2: Mafia or not
                if target.role == Role.MAFIA:
                    role_hint = "They were **Mafia** 🔴"
                else:
                    role_hint = "They were **not Mafia** 💔"
                embed = discord.Embed(
                    title=f"💀  {target.name}  KILLED",
                    description=f"**{target.name}** was murdered by the Mafia during the night!\n\n{role_hint}",
                    color=discord.Color.dark_red()
                )
            else:
                # Mode 3: Full role reveal (default)
                embed = discord.Embed(
                    title=f"💀  {target.name}  KILLED",
                    description=f"**{target.name}** was murdered by the Mafia during the night!\nThey were a **{target.role.value}**.",
                    color=discord.Color.dark_red()
                )
    else:
        embed = discord.Embed(
            title="☀️ Morning Has Come",
            description="Everyone wake up! Open your eyes.\n\n😴 **Everyone is safe!** No one died during the night.",
            color=discord.Color.gold()
        )
    
    # Show alive players
    alive_players = [p.name for p in game.players.values() if p.is_alive]
    embed.add_field(name=f"🧍 Alive ({len(alive_players)})", value="\n".join(alive_players), inline=False)
    
    await send_game_message(game, embed=embed)
    
    # Check win conditions
    if await check_win_condition(game):
        return
    
    # Discussion phase - timer + manual button
    game.discussion_ended = False
    disc_time = game.settings.discussion_time
    discussion_view = DiscussionEndView(game, game.host_id, disc_time)
    disc_msg = await send_game_message(
        game,
        content=f"💬 **Discussion time!** ⏱️ **{format_time(disc_time)}** remaining\nHost can also click **🗳️ Start Voting** to skip.",
        view=discussion_view
    )
    discussion_view.discussion_message = disc_msg
    asyncio.create_task(discussion_view.start_timer())


async def start_voting_phase(game: GameState):
    """Start the voting phase"""
    # Check if game was force stopped
    if game.phase == GamePhase.ENDED:
        return
    
    game.phase = GamePhase.VOTING
    game.day_votes.clear()
    
    # Play voting announcement
    await play_announcement(game, "voting_time")
    
    embed = discord.Embed(
        title="🗳️ Voting Time",
        description=f"You have **{game.settings.voting_time}s** to vote.",
        color=discord.Color.orange()
    )
    
    view = VotingView(game, game.settings.voting_time)
    await send_game_message(game, embed=embed, view=view)
    
    # In test mode, auto-vote for bot players
    if game.settings.test_mode:
        alive_bots = [p for p in game.players.values() 
                     if p.is_alive and isinstance(p.member, DummyMember)]
        alive_players = [p for p in game.players.values() if p.is_alive]
        
        if alive_bots:
            bot_votes = []
            for bot in alive_bots:
                # Bots have 30% chance to skip, 70% chance to vote someone
                if random.random() < 0.3:
                    game.day_votes[bot.member.id] = None  # Skip
                    bot_votes.append(f"• {bot.name} → Skip")
                else:
                    # Vote for a random alive player (not themselves)
                    possible_targets = [p for p in alive_players if p.member.id != bot.member.id]
                    if possible_targets:
                        target = random.choice(possible_targets)
                        game.day_votes[bot.member.id] = target.member.id
                        bot_votes.append(f"• {bot.name} → {target.name}")
            
            if bot_votes:
                await send_game_message(game, content=f"🤖 *[Test Mode] Bot votes:*\n" + "\n".join(bot_votes))
    
    # Live countdown in text chat
    voting_time = game.settings.voting_time
    countdown_interval = 10  # Show countdown every 10 seconds
    elapsed = 0
    
    while elapsed < voting_time:
        # Check if game was force stopped
        if game.phase == GamePhase.ENDED:
            return
        
        remaining = voting_time - elapsed
        
        # Show countdown at specific intervals
        if remaining > 0 and remaining <= voting_time:
            if remaining == voting_time:
                # First message
                await send_game_message(game, content=f"⏱️ **{remaining}s** remaining to vote!")
            elif remaining <= 10:
                await send_game_message(game, content=f"⚠️ **{remaining}s** remaining!")
            elif remaining % countdown_interval == 0:
                bar = create_progress_bar(remaining, voting_time, 10)
                await send_game_message(game, content=f"⏱️ {bar} **{remaining}s** remaining")
        
        # Wait for next tick
        wait_time = min(countdown_interval, voting_time - elapsed)
        if wait_time > 0:
            await asyncio.sleep(wait_time)
        elapsed += wait_time
    
    # Final message
    await send_game_message(game, content="⏰ **Time's up!** Votes are being tallied...")
    await asyncio.sleep(2)
    
    # Check if game was force stopped during voting
    if game.phase == GamePhase.ENDED:
        return
    
    # Process votes
    await process_voting_results(game)


async def process_voting_results(game: GameState):
    """Process voting results"""
    # Check if game was force stopped
    if game.phase == GamePhase.ENDED:
        return
    
    vote_counts: Dict[Optional[int], int] = {}  # target_id -> count (None = skip)
    
    alive_players = [p for p in game.players.values() if p.is_alive]
    
    # Count votes (players who didn't vote are considered skipped)
    for player in alive_players:
        vote = game.day_votes.get(player.member.id, None)  # Default to skip if no vote
        vote_counts[vote] = vote_counts.get(vote, 0) + 1
    
    # Display vote results with visual bars
    embed = discord.Embed(
        title="📊 Voting Results",
        color=discord.Color.blue()
    )
    
    # Find max votes for scaling bars
    max_vote_count = max(vote_counts.values()) if vote_counts else 1
    
    results = []
    for target_id, count in sorted(vote_counts.items(), key=lambda x: x[1], reverse=True):
        bar = create_progress_bar(count, max_vote_count, 10)
        if target_id is None:
            results.append(f"⏭️ Skip {bar} **{count}**")
        else:
            target_name = game.players[target_id].name
            results.append(f"👤 {target_name} {bar} **{count}**")
    
    embed.description = "\n".join(results) if results else "No votes cast"
    
    # Find the highest voted (excluding skips for elimination)
    non_skip_votes = {k: v for k, v in vote_counts.items() if k is not None}
    skip_votes = vote_counts.get(None, 0)
    
    if non_skip_votes:
        max_votes = max(non_skip_votes.values())
        top_voted = [k for k, v in non_skip_votes.items() if v == max_votes]
        
        # Check if skip has more votes
        if skip_votes > max_votes:
            embed.add_field(name="📢 Result", value="The vote was skipped! No one is eliminated.", inline=False)
            await send_game_message(game, embed=embed)
        elif len(top_voted) == 1 and max_votes > skip_votes:
            eliminated_id = top_voted[0]
            eliminated = game.players[eliminated_id]
            eliminated.is_alive = False
            reveal_mode = game.settings.role_reveal_mode
            
            if reveal_mode == 1:
                # Mode 1: No reveal at all
                embed.add_field(
                    name=f"💀  {eliminated.name}  ELIMINATED",
                    value=f"**{eliminated.name}** has been voted out!",
                    inline=False
                )
                await send_game_message(game, embed=embed)
            
            elif reveal_mode == 2:
                # Mode 2: Just say mafia or not
                embed.add_field(
                    name=f"💀  {eliminated.name}  ELIMINATED",
                    value=f"**{eliminated.name}** has been voted out!",
                    inline=False
                )
                await send_game_message(game, embed=embed)
                
                await asyncio.sleep(2)
                await send_game_message(game, content="🥁 *The town holds its breath...*")
                await asyncio.sleep(2)
                
                if eliminated.role == Role.MAFIA:
                    reveal_embed = discord.Embed(
                        title="🔴 MAFIA CAUGHT!",
                        description=f"**{eliminated.name}** was **Mafia**! 🎯",
                        color=discord.Color.green()
                    )
                else:
                    reveal_embed = discord.Embed(
                        title="💔 Not Mafia...",
                        description=f"**{eliminated.name}** was **not Mafia** 😔",
                        color=discord.Color.red()
                    )
                await send_game_message(game, embed=reveal_embed)
            
            else:
                # Mode 3: Full role reveal with suspense (default)
                embed.add_field(
                    name=f"💀  {eliminated.name}  ELIMINATED",
                    value=f"**{eliminated.name}** has been voted out!",
                    inline=False
                )
                await send_game_message(game, embed=embed)
                
                # Count remaining mafia to check if this was the last mafia moment
                alive_mafia_count = sum(1 for p in game.players.values() if p.is_alive and p.role == Role.MAFIA)
                is_last_mafia_moment = (alive_mafia_count == 0 and eliminated.role == Role.MAFIA) or \
                                       (alive_mafia_count == 1 and eliminated.role != Role.MAFIA) or \
                                       (alive_mafia_count == 0)
                
                await asyncio.sleep(2)
                await send_game_message(game, content="🥁 *The town holds its breath...*")
                await asyncio.sleep(2)
                
                if is_last_mafia_moment:
                    await send_game_message(game, content="😰 *Could this be the final Mafia member?*")
                    await asyncio.sleep(2)
                else:
                    await send_game_message(game, content="🫣 *Revealing their identity...*")
                    await asyncio.sleep(2)
                
                if eliminated.role == Role.MAFIA:
                    reveal_embed = discord.Embed(
                        title="🔴 MAFIA CAUGHT!",
                        description=f"**{eliminated.name}** was indeed **MAFIA**! 🎯\n\nThe town made the right call!",
                        color=discord.Color.green()
                    )
                    reveal_embed.set_footer(text="✅ One less threat to the town.")
                else:
                    reveal_embed = discord.Embed(
                        title="💔 An Innocent Falls...",
                        description=f"**{eliminated.name}** was a **{eliminated.role.value}**... 😔\n\nThe town has made a terrible mistake!",
                        color=discord.Color.red()
                    )
                    reveal_embed.set_footer(text="❌ The Mafia lives on...")
                
                await send_game_message(game, embed=reveal_embed)
        else:
            embed.add_field(name="📢 Result", value="It's a tie! No one is eliminated.", inline=False)
            await send_game_message(game, embed=embed)
    else:
        embed.add_field(name="📢 Result", value="Everyone skipped! No one is eliminated.", inline=False)
        await send_game_message(game, embed=embed)
    
    # Check win conditions
    if await check_win_condition(game):
        return
    
    # Check if game was force stopped
    if game.phase == GamePhase.ENDED:
        return
    
    # Show "Start Night" button for host (no auto-transition)
    next_round_view = NextRoundView(game, game.host_id)
    await send_game_message(game, content="🌙 **Ready for next round?** Host: click the button below to start the night.", view=next_round_view)
    # Next night will be started by the NextRoundView button callback


async def check_win_condition(game: GameState) -> bool:
    """Check if the game has ended"""
    alive_mafia = sum(1 for p in game.players.values() if p.is_alive and p.role == Role.MAFIA)
    alive_citizens = sum(1 for p in game.players.values() if p.is_alive and p.role != Role.MAFIA)
    
    if alive_mafia == 0:
        # Citizens win - play announcement
        await play_announcement(game, "citizens_win")
        embed = discord.Embed(
            title="🎉 Game Over - Citizens Win!",
            description="All Mafia members have been eliminated!\nThe town is safe once again.",
            color=discord.Color.green()
        )
        await end_game(game, embed)
        return True
    
    if alive_mafia >= alive_citizens:
        # Mafia wins - play announcement
        await play_announcement(game, "mafia_wins")
        embed = discord.Embed(
            title="🔪 Game Over - Mafia Wins!",
            description="The Mafia has taken over the town!\nDarkness prevails.",
            color=discord.Color.red()
        )
        await end_game(game, embed)
        return True
    
    return False


async def end_game(game: GameState, embed: discord.Embed):
    """End the game and reveal all roles"""
    try:
        game.phase = GamePhase.ENDED
        logger.info(f"Game ended in guild {game.guild.name if game.guild else 'Unknown'}")
        
        # Reveal all roles
        role_reveal = []
        for player in game.players.values():
            status = "✅" if player.is_alive else "💀"
            role_reveal.append(f"{status} **{player.name}** - {player.role.value}")
        
        embed.add_field(name="🎭 Role Reveal", value="\n".join(role_reveal), inline=False)
        embed.add_field(name="📊 Stats", value=f"Rounds played: {game.round_number}", inline=False)
        embed.set_footer(text="Game messages will be deleted in 30 seconds...")
        
        final_message = await game.text_channel.send(embed=embed)
        
        # Unmute all players at game end
        for player in game.players.values():
            if hasattr(player.member, 'voice') and player.member.voice:
                try:
                    await player.member.edit(mute=False)
                except discord.errors.Forbidden:
                    logger.warning(f"No permission to unmute {player.name}")
                except Exception as e:
                    logger.warning(f"Failed to unmute {player.name}: {e}")
        
        # Disconnect from voice if connected
        if game.guild:
            voice_client = game.guild.voice_client
            if voice_client:
                try:
                    await voice_client.disconnect(force=True)
                except Exception as e:
                    logger.warning(f"Failed to disconnect from voice: {e}")
        
        # Wait before deleting messages so players can see the results
        await asyncio.sleep(30)
        
        # Delete all game messages
        await delete_game_messages(game)
        
        # Delete the final message too
        try:
            await final_message.delete()
        except:
            pass
        
        # Remove game from active games
        if game.guild and game.guild.id in active_games:
            del active_games[game.guild.id]
            
    except Exception as e:
        logger.error(f"Error in end_game: {e}", exc_info=True)
        # Try to clean up even if there was an error
        if game.guild and game.guild.id in active_games:
            del active_games[game.guild.id]


# ==================== DM MESSAGE HANDLER FOR MAFIA CHAT ====================

# Game command prefixes to track for deletion
GAME_COMMANDS = ['!mafia', '!testmafia', '!startgame', '!endgame', '!testroles', '!teststart', 
                 '!testkill', '!testsave', '!testvote', '!testskip', '!teststatus', '!testhelp',
                 '!gamestatus', '!gamesettings', '!setmafia', '!setdoctor', '!setpolice',
                 '!setvotetime', '!setdiscusstime', '!setnighttime', '!setregtime', '!mafiahelp']


@bot.event
async def on_message(message):
    # Track game-related user commands for deletion
    if message.guild and not message.author.bot:
        game = get_game(message.guild.id)
        if game and game.phase != GamePhase.ENDED:
            # Check if it's a game command
            content_lower = message.content.lower()
            if any(content_lower.startswith(cmd) for cmd in GAME_COMMANDS):
                game.game_messages.append(message)
    
    # Process commands
    await bot.process_commands(message)
    
    # Handle mafia chat relay
    if isinstance(message.channel, discord.DMChannel) and not message.author.bot:
        # Find if this user is a mafia in an active game
        for game in active_games.values():
            if game.phase == GamePhase.NIGHT:
                player = game.players.get(message.author.id)
                if player and player.role == Role.MAFIA and player.is_alive:
                    # Relay message to other mafia
                    await relay_mafia_message(game, player, message.content)
                    break


# ==================== VOICE OPERATOR COMMANDS ====================

# Dictionary to track voice channel operators
voice_operators = {}


@bot.event
async def on_ready():
    logger.info(f'{bot.user} has connected to Discord!')
    logger.info(f'Bot is ready to operate voice channels')
    logger.info(f'Connected to {len(bot.guilds)} guild(s)')
    # Pre-generate audio files
    try:
        await pre_generate_audio()
    except Exception as e:
        logger.error(f"Failed to pre-generate audio files: {e}")


@bot.event
async def on_command_error(ctx, error):
    """Global error handler for commands"""
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command!")
        logger.warning(f"Permission denied for {ctx.author} using {ctx.command}")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing required argument: `{error.param.name}`")
        logger.warning(f"Missing argument in {ctx.command}: {error.param.name}")
    elif isinstance(error, commands.BadArgument):
        await ctx.send(f"❌ Invalid argument provided. Please check your input.")
        logger.warning(f"Bad argument in {ctx.command}: {error}")
    elif isinstance(error, commands.CommandNotFound):
        pass  # Silently ignore unknown commands
    else:
        logger.error(f"Unhandled error in {ctx.command}: {error}", exc_info=True)
        await ctx.send("❌ An unexpected error occurred. The error has been logged.")


@bot.command(name='join', help='Join the voice channel you are in')
@commands.has_permissions(administrator=True)
async def join(ctx):
    """Join the voice channel of the command sender"""
    try:
        if not ctx.author.voice:
            await ctx.send("You need to be in a voice channel first!")
            return
        
        channel = ctx.author.voice.channel
        
        if ctx.voice_client is not None:
            await ctx.voice_client.move_to(channel)
        else:
            await channel.connect()
        
        await ctx.send(f"Joined {channel.name}! I'm now operating this voice channel.")
        logger.info(f"Joined voice channel {channel.name} in {ctx.guild.name}")
    except Exception as e:
        logger.error(f"Error joining voice channel: {e}")
        await ctx.send("❌ Failed to join voice channel.")


@bot.command(name='leave', help='Leave the current voice channel')
@commands.has_permissions(administrator=True)
async def leave(ctx):
    """Leave the voice channel"""
    if ctx.voice_client:
        await ctx.voice_client.disconnect()
        await ctx.send("Left the voice channel.")
    else:
        await ctx.send("I'm not in a voice channel!")


@bot.command(name='mute', help='Mute a user in voice channel')
@commands.has_permissions(administrator=True)
async def mute(ctx, member: discord.Member):
    """Mute a specific member in voice channel"""
    if not ctx.voice_client:
        await ctx.send("I'm not in a voice channel!")
        return
    
    if not member.voice:
        await ctx.send(f"{member.name} is not in a voice channel!")
        return
    
    await member.edit(mute=True)
    await ctx.send(f"Muted {member.name}")


@bot.command(name='unmute', help='Unmute a user in voice channel')
@commands.has_permissions(administrator=True)
async def unmute(ctx, member: discord.Member):
    """Unmute a specific member in voice channel"""
    if not ctx.voice_client:
        await ctx.send("I'm not in a voice channel!")
        return
    
    if not member.voice:
        await ctx.send(f"{member.name} is not in a voice channel!")
        return
    
    await member.edit(mute=False)
    await ctx.send(f"Unmuted {member.name}")


@bot.command(name='deafen', help='Deafen a user in voice channel')
@commands.has_permissions(administrator=True)
async def deafen(ctx, member: discord.Member):
    """Deafen a specific member in voice channel"""
    if not member.voice:
        await ctx.send(f"{member.name} is not in a voice channel!")
        return
    
    await member.edit(deafen=True)
    await ctx.send(f"Deafened {member.name}")


@bot.command(name='undeafen', help='Undeafen a user in voice channel')
@commands.has_permissions(administrator=True)
async def undeafen(ctx, member: discord.Member):
    """Undeafen a specific member in voice channel"""
    if not member.voice:
        await ctx.send(f"{member.name} is not in a voice channel!")
        return
    
    await member.edit(deafen=False)
    await ctx.send(f"Undeafened {member.name}")


@bot.command(name='move', help='Move a user to another voice channel')
@commands.has_permissions(administrator=True)
async def move(ctx, member: discord.Member, channel: discord.VoiceChannel):
    """Move a member to a different voice channel"""
    if not member.voice:
        await ctx.send(f"{member.name} is not in a voice channel!")
        return
    
    await member.move_to(channel)
    await ctx.send(f"Moved {member.name} to {channel.name}")


@bot.command(name='disconnect', help='Disconnect a user from voice channel')
@commands.has_permissions(administrator=True)
async def disconnect_user(ctx, member: discord.Member):
    """Disconnect a member from voice channel"""
    if not member.voice:
        await ctx.send(f"{member.name} is not in a voice channel!")
        return
    
    await member.move_to(None)
    await ctx.send(f"Disconnected {member.name} from voice channel")


@bot.command(name='muteall', help='Mute all users in your voice channel')
@commands.has_permissions(administrator=True)
async def mute_all(ctx):
    """Mute all members in the voice channel"""
    if not ctx.author.voice:
        await ctx.send("You need to be in a voice channel!")
        return
    
    channel = ctx.author.voice.channel
    muted_count = 0
    
    for member in channel.members:
        if not member.bot:
            await member.edit(mute=True)
            muted_count += 1
    
    await ctx.send(f"Muted {muted_count} members in {channel.name}")


@bot.command(name='unmuteall', help='Unmute all users in your voice channel')
@commands.has_permissions(administrator=True)
async def unmute_all(ctx):
    """Unmute all members in the voice channel"""
    if not ctx.author.voice:
        await ctx.send("You need to be in a voice channel!")
        return
    
    channel = ctx.author.voice.channel
    unmuted_count = 0
    
    for member in channel.members:
        if not member.bot:
            await member.edit(mute=False)
            unmuted_count += 1
    
    await ctx.send(f"Unmuted {unmuted_count} members in {channel.name}")


@bot.command(name='vcinfo', help='Get information about a voice channel')
async def voice_info(ctx, channel: discord.VoiceChannel = None):
    """Display information about a voice channel"""
    if channel is None:
        if ctx.author.voice:
            channel = ctx.author.voice.channel
        else:
            await ctx.send("Please specify a voice channel or join one!")
            return
    
    embed = discord.Embed(
        title=f"Voice Channel: {channel.name}",
        color=discord.Color.blue()
    )
    
    embed.add_field(name="Members", value=str(len(channel.members)), inline=True)
    embed.add_field(name="User Limit", value=str(channel.user_limit) if channel.user_limit else "No limit", inline=True)
    embed.add_field(name="Bitrate", value=f"{channel.bitrate // 1000} kbps", inline=True)
    
    members_list = "\n".join([f"• {member.name}" for member in channel.members[:10]])
    if len(channel.members) > 10:
        members_list += f"\n...and {len(channel.members) - 10} more"
    
    if members_list:
        embed.add_field(name="Current Members", value=members_list, inline=False)
    
    await ctx.send(embed=embed)


@bot.command(name='setlimit', help='Set user limit for a voice channel')
@commands.has_permissions(administrator=True)
async def set_limit(ctx, limit: int, channel: discord.VoiceChannel = None):
    """Set the user limit for a voice channel"""
    if channel is None:
        if ctx.author.voice:
            channel = ctx.author.voice.channel
        else:
            await ctx.send("Please specify a voice channel or join one!")
            return
    
    await channel.edit(user_limit=limit)
    await ctx.send(f"Set user limit to {limit} for {channel.name}")


@bot.event
async def on_voice_state_update(member, before, after):
    """Monitor voice channel events"""
    # Log when someone joins a voice channel
    if before.channel is None and after.channel is not None:
        print(f"{member.name} joined {after.channel.name}")
    
    # Log when someone leaves a voice channel
    elif before.channel is not None and after.channel is None:
        print(f"{member.name} left {before.channel.name}")
    
    # Log when someone moves between channels
    elif before.channel != after.channel:
        print(f"{member.name} moved from {before.channel.name} to {after.channel.name}")


@bot.command(name='lock', help='Lock a voice channel (only admins can join)')
@commands.has_permissions(administrator=True)
async def lock_channel(ctx, channel: discord.VoiceChannel = None):
    """Lock a voice channel to prevent new members from joining"""
    if channel is None:
        if ctx.author.voice:
            channel = ctx.author.voice.channel
        else:
            await ctx.send("Please specify a voice channel or join one!")
            return
    
    overwrite = channel.overwrites_for(ctx.guild.default_role)
    overwrite.connect = False
    await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    await ctx.send(f"🔒 Locked {channel.name}")


@bot.command(name='unlock', help='Unlock a voice channel')
@commands.has_permissions(administrator=True)
async def unlock_channel(ctx, channel: discord.VoiceChannel = None):
    """Unlock a voice channel"""
    if channel is None:
        if ctx.author.voice:
            channel = ctx.author.voice.channel
        else:
            await ctx.send("Please specify a voice channel or join one!")
            return
    
    overwrite = channel.overwrites_for(ctx.guild.default_role)
    overwrite.connect = True
    await channel.set_permissions(ctx.guild.default_role, overwrite=overwrite)
    await ctx.send(f"🔓 Unlocked {channel.name}")


# ==================== MAFIA GAME COMMANDS ====================

@bot.command(name='mafia', help='Start a new Mafia game')
async def start_mafia(ctx):
    """Start a new Mafia game in the current voice channel"""
    try:
        if not ctx.author.voice:
            await ctx.send("❌ You need to be in a voice channel to start a game!")
            return
        
        if ctx.guild.id in active_games and active_games[ctx.guild.id].phase != GamePhase.ENDED:
            await ctx.send("❌ A game is already in progress! Use `!endgame` to end it first.")
            return
        
        logger.info(f"New game started by {ctx.author.display_name} in guild {ctx.guild.name}")
        
        # Create new game
        game = create_game(ctx.guild.id)
        game.voice_channel = ctx.author.voice.channel
        game.text_channel = ctx.channel
        game.guild = ctx.guild
        game.phase = GamePhase.REGISTRATION
        game.host_id = ctx.author.id  # Set the host
        
        # Track the command message
        game.game_messages.append(ctx.message)
        
        # Join voice channel using safe connection helper
        connecting_msg = await ctx.send("🔄 Connecting to voice channel...")
        game.game_messages.append(connecting_msg)
        
        success, vc = await safe_voice_connect(ctx.author.voice.channel, ctx.guild)
        
        if success:
            game.voice_connected = True
            await connecting_msg.edit(content=f"🔊 Joined **{ctx.author.voice.channel.name}** (audio announcements enabled)")
            logger.info(f"Bot joined voice channel: {ctx.author.voice.channel.name}")
        else:
            game.voice_connected = False
            await connecting_msg.edit(content="✅ Voice connection skipped (muting still works, audio announcements disabled)")
        
        # Send registration message with new view
        min_players = game.settings.num_mafia + game.settings.num_doctor + game.settings.num_police + 1
        embed = discord.Embed(
            title="🌙 Night Has Come - Registration",
            description=f"Click the buttons below to join or leave the game!\n\n**Players (0):**\n*No players yet*",
            color=discord.Color.purple()
        )
        embed.add_field(name="📋 Requirements", value=f"Minimum {min_players} players to start", inline=True)
        embed.add_field(name="⚙️ Settings", value=f"Mafia: {game.settings.num_mafia} | Doctor: {game.settings.num_doctor} | Police: {game.settings.num_police}", inline=True)
        embed.set_footer(text=f"Host: {ctx.author.display_name} • Click 'Start Game' when ready")
        
        view = RegistrationView(ctx.guild.id, ctx.author.id)
        game.registration_message = await ctx.send(embed=embed, view=view)
        game.game_messages.append(game.registration_message)
        
        msg = await ctx.send(f"🎮 **Game registration started!** Join using the button above.\n💡 Host or admins can click **Start Game** when everyone has joined.")
        game.game_messages.append(msg)
    except Exception as e:
        logger.error(f"Error starting mafia game: {e}", exc_info=True)
        await ctx.send("❌ An error occurred while starting the game. Check logs for details.")


# Test mode dummy player names
TEST_PLAYER_NAMES = [
    "Alex", "Jordan", "Taylor", "Casey", "Morgan",
    "Riley", "Quinn", "Avery", "Parker", "Skyler"
]


@bot.command(name='testmafia', help='Start a test game with dummy players')
@commands.has_permissions(administrator=True)
async def test_mafia(ctx, num_players: int = 6):
    """Start a test Mafia game with dummy players for solo testing"""
    try:
        if num_players < 4:
            await ctx.send("❌ Need at least 4 players for a test game!")
            return
        
        if num_players > 10:
            await ctx.send("❌ Maximum 10 players for test mode!")
            return
        
        if ctx.guild.id in active_games and active_games[ctx.guild.id].phase != GamePhase.ENDED:
            await ctx.send("❌ A game is already in progress! Use `!endgame` to end it first.")
            return
        
        logger.info(f"Test game started by {ctx.author.display_name} in guild {ctx.guild.name} with {num_players} players")
        
        # Create new game in test mode
        game = create_game(ctx.guild.id)
        game.text_channel = ctx.channel
        game.guild = ctx.guild
        game.phase = GamePhase.REGISTRATION
        game.settings.test_mode = True
        game.tester_id = ctx.author.id
        game.host_id = ctx.author.id
        
        # Track the command message
        game.game_messages.append(ctx.message)
        
        # Join voice channel if user is in one (using safe connection helper)
        if ctx.author.voice:
            connecting_msg = await ctx.send("🔄 Connecting to voice channel...")
            game.game_messages.append(connecting_msg)
            
            success, vc = await safe_voice_connect(ctx.author.voice.channel, ctx.guild)
            
            if success:
                game.voice_connected = True
                game.voice_channel = ctx.author.voice.channel
                await connecting_msg.edit(content=f"🔊 Joined **{ctx.author.voice.channel.name}** (audio announcements enabled)")
                logger.info(f"Bot joined voice channel: {ctx.author.voice.channel.name}")
            else:
                game.voice_connected = False
                await connecting_msg.edit(content="✅ Voice connection skipped (muting still works, audio announcements disabled)")
        else:
            game.voice_connected = False
            msg = await ctx.send("💡 Tip: Join a voice channel before starting for the bot to join too!")
            game.game_messages.append(msg)
        
        # Reduce timers for faster testing
        game.settings.voting_time = 20
        game.settings.discussion_time = 15
        
        # Add the tester as a real player
        tester_player = Player(member=ctx.author, name=ctx.author.display_name)
        game.players[ctx.author.id] = tester_player
        
        # Add dummy players
        for i in range(num_players - 1):
            dummy_id = 100000 + i  # Fake IDs for dummy players
            dummy_member = DummyMember(
                id=dummy_id,
                display_name=TEST_PLAYER_NAMES[i],
                name=TEST_PLAYER_NAMES[i]
            )
            dummy_player = Player(member=dummy_member, name=TEST_PLAYER_NAMES[i])
            game.players[dummy_id] = dummy_player
        
        embed = discord.Embed(
            title="🧪 TEST MODE - Night Has Come",
            description="Test game started with dummy players!",
            color=discord.Color.orange()
        )
        
        player_list = "\n".join([f"• {p.name} {'(You)' if p.member.id == ctx.author.id else '(Bot)'}" for p in game.players.values()])
        embed.add_field(name=f"Players ({len(game.players)})", value=player_list, inline=False)
        embed.add_field(name="⚙️ Settings", value=f"Mafia: {game.settings.num_mafia} | Doctor: {game.settings.num_doctor} | Police: {game.settings.num_police}", inline=False)
        embed.add_field(name="⏱️ Timers (Reduced)", value=f"Vote: {game.settings.voting_time}s | Discuss: {game.settings.discussion_time}s", inline=False)
        
        msg = await ctx.send(embed=embed)
        game.game_messages.append(msg)
        msg = await ctx.send("🎮 Use `!testroles` to assign roles and see all of them, or `!teststart` to begin!")
        game.game_messages.append(msg)
    except Exception as e:
        logger.error(f"Error starting test mafia game: {e}", exc_info=True)
        await ctx.send("❌ An error occurred while starting the test game. Check logs for details.")


@bot.command(name='testroles', help='Assign and reveal all roles (test mode)')
@commands.has_permissions(administrator=True)
async def test_roles(ctx):
    """Assign roles and show them all to the tester"""
    game = get_game(ctx.guild.id)
    
    if not game or not game.settings.test_mode:
        await ctx.send("❌ No test game in progress! Use `!testmafia` to start one.")
        return
    
    # Track the command message
    game.game_messages.append(ctx.message)
    
    # Assign roles
    await assign_roles(game)
    
    # Show all roles to tester
    embed = discord.Embed(
        title="🎭 All Roles Revealed (Test Mode)",
        description="Here are all the assigned roles:",
        color=discord.Color.gold()
    )
    
    role_groups = {
        Role.MAFIA: [],
        Role.DOCTOR: [],
        Role.POLICE: [],
        Role.CITIZEN: []
    }
    
    for player in game.players.values():
        role_groups[player.role].append(player.name)
    
    if role_groups[Role.MAFIA]:
        embed.add_field(name="🔪 Mafia", value="\n".join(role_groups[Role.MAFIA]), inline=True)
    if role_groups[Role.DOCTOR]:
        embed.add_field(name="💉 Doctor", value="\n".join(role_groups[Role.DOCTOR]), inline=True)
    if role_groups[Role.POLICE]:
        embed.add_field(name="🔍 Police", value="\n".join(role_groups[Role.POLICE]), inline=True)
    if role_groups[Role.CITIZEN]:
        embed.add_field(name="👤 Citizens", value="\n".join(role_groups[Role.CITIZEN]), inline=True)
    
    # Show tester's role prominently
    tester_player = game.players.get(ctx.author.id)
    if tester_player:
        embed.add_field(name="⭐ Your Role", value=f"**{tester_player.role.value}**", inline=False)
    
    msg = await ctx.send(embed=embed)
    game.game_messages.append(msg)


@bot.command(name='teststart', help='Start the test game')
@commands.has_permissions(administrator=True)
async def test_start(ctx):
    """Start the test game"""
    game = get_game(ctx.guild.id)
    
    if not game or not game.settings.test_mode:
        await ctx.send("❌ No test game in progress! Use `!testmafia` to start one.")
        return
    
    # Track the command message
    game.game_messages.append(ctx.message)
    
    # Check if roles are assigned
    if all(p.role == Role.CITIZEN for p in game.players.values()):
        msg = await ctx.send("⚠️ Roles not assigned yet. Assigning now...")
        game.game_messages.append(msg)
        await assign_roles(game)
        
        # Show roles
        embed = discord.Embed(title="🎭 Roles Assigned", color=discord.Color.gold())
        for player in game.players.values():
            is_you = " (You)" if player.member.id == ctx.author.id else ""
            embed.add_field(name=player.name + is_you, value=player.role.value, inline=True)
        msg = await ctx.send(embed=embed)
        game.game_messages.append(msg)
        await asyncio.sleep(2)
    
    msg = await ctx.send("🎮 **Starting test game!**")
    game.game_messages.append(msg)
    await asyncio.sleep(1)
    
    # Start first night
    await start_night_phase(game)


@bot.command(name='testkill', help='Simulate mafia kill (test mode)')
@commands.has_permissions(administrator=True)
async def test_kill(ctx, target_name: str):
    """Simulate mafia choosing a target"""
    game = get_game(ctx.guild.id)
    
    if not game or not game.settings.test_mode:
        await ctx.send("❌ No test game in progress!")
        return
    
    # Track the command message
    game.game_messages.append(ctx.message)
    
    if game.phase != GamePhase.NIGHT:
        msg = await ctx.send("❌ It's not night time!")
        game.game_messages.append(msg)
        return
    
    # Find target by name
    target = None
    for player in game.players.values():
        if player.name.lower() == target_name.lower() and player.is_alive:
            target = player
            break
    
    if not target:
        msg = await ctx.send(f"❌ Player '{target_name}' not found or already dead!")
        game.game_messages.append(msg)
        return
    
    # Set all mafia votes to this target
    for player in game.players.values():
        if player.role == Role.MAFIA and player.is_alive:
            game.mafia_votes[player.member.id] = target.member.id
    
    msg = await ctx.send(f"🔪 Test: Mafia will target **{target.name}**")
    game.game_messages.append(msg)


@bot.command(name='testsave', help='Simulate doctor save (test mode)')
@commands.has_permissions(administrator=True)
async def test_save(ctx, target_name: str):
    """Simulate doctor saving a target"""
    game = get_game(ctx.guild.id)
    
    if not game or not game.settings.test_mode:
        await ctx.send("❌ No test game in progress!")
        return
    
    # Track the command message
    game.game_messages.append(ctx.message)
    
    if game.phase != GamePhase.NIGHT:
        msg = await ctx.send("❌ It's not night time!")
        game.game_messages.append(msg)
        return
    
    # Find target by name
    target = None
    for player in game.players.values():
        if player.name.lower() == target_name.lower() and player.is_alive:
            target = player
            break
    
    if not target:
        msg = await ctx.send(f"❌ Player '{target_name}' not found or already dead!")
        game.game_messages.append(msg)
        return
    
    game.doctor_save = target.member.id
    msg = await ctx.send(f"💉 Test: Doctor will save **{target.name}**")
    game.game_messages.append(msg)


@bot.command(name='testvote', help='Simulate voting (test mode)')
@commands.has_permissions(administrator=True)
async def test_vote(ctx, target_name: str = None):
    """Simulate all dummy players voting for a target"""
    game = get_game(ctx.guild.id)
    
    if not game or not game.settings.test_mode:
        await ctx.send("❌ No test game in progress!")
        return
    
    # Track the command message
    game.game_messages.append(ctx.message)
    
    if game.phase != GamePhase.VOTING:
        msg = await ctx.send("❌ It's not voting time!")
        game.game_messages.append(msg)
        return
    
    if target_name is None or target_name.lower() == "skip":
        # All dummy players skip
        for player in game.players.values():
            if player.member.id != ctx.author.id and player.is_alive:
                game.day_votes[player.member.id] = None
        msg = await ctx.send("⏭️ Test: All dummy players will skip")
        game.game_messages.append(msg)
    else:
        # Find target
        target = None
        for player in game.players.values():
            if player.name.lower() == target_name.lower() and player.is_alive:
                target = player
                break
        
        if not target:
            msg = await ctx.send(f"❌ Player '{target_name}' not found or already dead!")
            game.game_messages.append(msg)
            return
        
        # All dummy players vote for target
        for player in game.players.values():
            if player.member.id != ctx.author.id and player.is_alive:
                game.day_votes[player.member.id] = target.member.id
        
        msg = await ctx.send(f"🗳️ Test: All dummy players will vote for **{target.name}**")
        game.game_messages.append(msg)


@bot.command(name='testskip', help='Skip current phase timer (test mode)')
@commands.has_permissions(administrator=True)
async def test_skip_phase(ctx):
    """Skip the current phase timer"""
    game = get_game(ctx.guild.id)
    
    if not game or not game.settings.test_mode:
        await ctx.send("❌ No test game in progress!")
        return
    
    # Track the command message
    game.game_messages.append(ctx.message)
    
    # Set all timers to 1 second for quick skip
    game.settings.voting_time = 1
    game.settings.discussion_time = 1
    
    msg = await ctx.send("⏩ Test: Timers reduced to 1 second. Phase will end shortly.")
    game.game_messages.append(msg)


@bot.command(name='teststatus', help='Show detailed test game status')
@commands.has_permissions(administrator=True)
async def test_status(ctx):
    """Show detailed status of test game"""
    game = get_game(ctx.guild.id)
    
    if not game or not game.settings.test_mode:
        await ctx.send("❌ No test game in progress!")
        return
    
    # Track the command message
    game.game_messages.append(ctx.message)
    
    embed = discord.Embed(
        title="🧪 Test Game Status",
        color=discord.Color.orange()
    )
    
    embed.add_field(name="Phase", value=game.phase.value.title(), inline=True)
    embed.add_field(name="Round", value=str(game.round_number), inline=True)
    
    # Show all players with roles and status
    player_info = []
    for player in game.players.values():
        status = "✅" if player.is_alive else "💀"
        is_you = " ⭐" if player.member.id == ctx.author.id else ""
        player_info.append(f"{status} **{player.name}**{is_you} - {player.role.value}")
    
    embed.add_field(name="Players", value="\n".join(player_info), inline=False)
    
    # Night action status
    if game.phase == GamePhase.NIGHT:
        night_info = []
        if game.mafia_votes:
            targets = [game.players[tid].name for tid in game.mafia_votes.values() if tid in game.players]
            night_info.append(f"🔪 Mafia targeting: {', '.join(targets) if targets else 'Not decided'}")
        if game.doctor_save:
            saved = game.players.get(game.doctor_save)
            night_info.append(f"💉 Doctor saving: {saved.name if saved else 'Not decided'}")
        if night_info:
            embed.add_field(name="Night Actions", value="\n".join(night_info), inline=False)
    
    # Voting status
    if game.phase == GamePhase.VOTING:
        votes_info = []
        for voter_id, target_id in game.day_votes.items():
            voter = game.players.get(voter_id)
            if target_id is None:
                votes_info.append(f"{voter.name}: Skip")
            else:
                target = game.players.get(target_id)
                votes_info.append(f"{voter.name}: {target.name if target else 'Unknown'}")
        if votes_info:
            embed.add_field(name="Current Votes", value="\n".join(votes_info), inline=False)
    
    msg = await ctx.send(embed=embed)
    game.game_messages.append(msg)


@bot.command(name='testhelp', help='Show test mode commands')
async def test_help(ctx):
    """Show all test mode commands"""
    embed = discord.Embed(
        title="🧪 Test Mode Commands",
        description="Commands for testing the Mafia game solo",
        color=discord.Color.orange()
    )
    
    embed.add_field(
        name="🎮 Setup",
        value=(
            "`!testmafia [players]` - Start test game (default 6 players)\n"
            "`!testroles` - Assign and reveal all roles\n"
            "`!teststart` - Begin the game"
        ),
        inline=False
    )
    
    embed.add_field(
        name="🌙 Night Actions",
        value=(
            "`!testkill <name>` - Set mafia target\n"
            "`!testsave <name>` - Set doctor save target"
        ),
        inline=False
    )
    
    embed.add_field(
        name="🗳️ Day Actions",
        value=(
            "`!testvote <name>` - Make all bots vote for someone\n"
            "`!testvote skip` - Make all bots skip vote"
        ),
        inline=False
    )
    
    embed.add_field(
        name="⚙️ Control",
        value=(
            "`!testskip` - Speed up current phase\n"
            "`!teststatus` - Show detailed game status\n"
            "`!endgame` - End the test game"
        ),
        inline=False
    )
    
    await ctx.send(embed=embed)


@bot.command(name='startgame', help='Start the game after registration')
@commands.has_permissions(administrator=True)
async def force_start_game(ctx):
    """Force start the game after registration"""
    game = get_game(ctx.guild.id)
    
    if not game or game.phase != GamePhase.REGISTRATION:
        await ctx.send("❌ No game is in registration phase!")
        return
    
    # Track the command message
    game.game_messages.append(ctx.message)
    
    min_players = game.settings.num_mafia + game.settings.num_doctor + game.settings.num_police + 1
    
    if len(game.players) < min_players:
        msg = await ctx.send(f"❌ Need at least {min_players} players to start! Currently have {len(game.players)}.")
        game.game_messages.append(msg)
        return
    
    msg = await ctx.send("🎮 **Starting the game!**")
    game.game_messages.append(msg)
    
    # Assign roles
    await assign_roles(game)
    
    await asyncio.sleep(3)
    
    # Start first night
    await start_night_phase(game)


@bot.command(name='endgame', help='End the current game')
@commands.has_permissions(administrator=True)
async def end_current_game(ctx):
    """End the current game"""
    game = get_game(ctx.guild.id)
    
    if not game:
        await ctx.send("❌ No active game to end!")
        return
    
    # IMMEDIATELY set phase to ENDED to stop all async game loops
    game.phase = GamePhase.ENDED
    
    # Track the command message
    game.game_messages.append(ctx.message)
    
    embed = discord.Embed(
        title="🛑 Game Ended",
        description="The game has been manually ended by an admin.",
        color=discord.Color.red()
    )
    
    if game.players:
        # Check if roles were actually assigned (not all Citizens)
        roles_assigned = not all(p.role == Role.CITIZEN for p in game.players.values())
        
        if roles_assigned:
            role_reveal = []
            for player in game.players.values():
                status = "✅" if player.is_alive else "💀"
                role_reveal.append(f"{status} **{player.name}** - {player.role.value}")
            embed.add_field(name="🎭 Role Reveal", value="\n".join(role_reveal), inline=False)
        else:
            # Game ended during registration, roles never assigned
            player_list = [f"• {p.name}" for p in game.players.values()]
            embed.add_field(name="👥 Players", value="\n".join(player_list), inline=False)
            embed.add_field(name="ℹ️ Note", value="Game ended before roles were assigned.\nUse `!teststart` or `!testroles` to assign roles before playing.", inline=False)
    
    msg = await ctx.send(embed=embed)
    game.game_messages.append(msg)
    
    # Unmute all players (works even without bot in voice channel)
    for player in game.players.values():
        if hasattr(player.member, 'voice') and player.member.voice:
            try:
                await player.member.edit(mute=False)
            except:
                pass
    
    # Disconnect from voice if connected
    if ctx.voice_client:
        try:
            await ctx.voice_client.disconnect(force=True)
        except:
            pass
    
    # Send message about cleanup
    cleanup_msg = await ctx.send("🧹 Game messages will be deleted in 30 seconds...")
    game.game_messages.append(cleanup_msg)
    
    # Wait and then delete all game messages
    await asyncio.sleep(30)
    await delete_game_messages(game)
    
    # Remove game from active games
    if ctx.guild.id in active_games:
        del active_games[ctx.guild.id]


@bot.command(name='forcestop', aliases=['haltgame', 'killgame'], help='Force stop ALL games and reset ALL voice states immediately')
@commands.has_permissions(administrator=True)
async def force_stop_all_games(ctx):
    """
    Emergency command to halt ALL games and reset ALL voice states immediately.
    This command:
    1. Immediately ends all active games in this server
    2. Unmutes and undeafens ALL members in your current voice channel
    3. Does NOT wait for cleanup - immediate action
    """
    logger.info(f"Force stop initiated by {ctx.author.display_name} in guild {ctx.guild.name}")
    
    # Get the game for this guild
    game = get_game(ctx.guild.id)
    
    # Immediately mark game as ended to stop all async loops
    if game:
        game.phase = GamePhase.ENDED
        logger.info(f"Game phase set to ENDED")
    
    # Remove from active games IMMEDIATELY
    if ctx.guild.id in active_games:
        del active_games[ctx.guild.id]
        logger.info(f"Game removed from active_games")
    
    # Count of actions taken
    unmuted_count = 0
    errors = []
    
    # If user is in a voice channel, unmute ALL members in that channel
    if ctx.author.voice and ctx.author.voice.channel:
        channel = ctx.author.voice.channel
        for member in channel.members:
            if not member.bot:
                try:
                    needs_unmute = member.voice.mute if member.voice else False
                    
                    if needs_unmute:
                        await member.edit(mute=False)
                        unmuted_count += 1
                        logger.info(f"Unmuted {member.display_name}")
                except discord.errors.Forbidden:
                    errors.append(f"No permission to edit {member.display_name}")
                except Exception as e:
                    errors.append(f"Failed to reset {member.display_name}: {str(e)}")
    
    # Also try to unmute game players who might have left the channel
    if game and game.players:
        for player in game.players.values():
            if hasattr(player.member, 'voice') and player.member.voice:
                try:
                    needs_unmute = player.member.voice.mute if player.member.voice else False
                    
                    if needs_unmute:
                        await player.member.edit(mute=False)
                        unmuted_count += 1
                except:
                    pass
    
    # Disconnect bot from voice if connected
    if ctx.voice_client:
        try:
            await ctx.voice_client.disconnect(force=True)
        except:
            pass
    
    # Also check if guild has a voice client (backup check)
    if ctx.guild.voice_client:
        try:
            await ctx.guild.voice_client.disconnect(force=True)
        except:
            pass
    
    # Build response embed
    embed = discord.Embed(
        title="🛑 FORCE STOP - All Games Halted",
        description="Emergency stop executed. All game processes terminated.",
        color=discord.Color.dark_red()
    )
    
    # Status summary
    status_lines = []
    status_lines.append(f"✅ Game ended: {'Yes' if game else 'No active game'}")
    status_lines.append(f"🔊 Members unmuted: {unmuted_count}")
    
    embed.add_field(name="📊 Actions Taken", value="\n".join(status_lines), inline=False)
    
    if errors:
        error_text = "\n".join(errors[:5])
        if len(errors) > 5:
            error_text += f"\n...and {len(errors) - 5} more"
        embed.add_field(name="⚠️ Errors", value=error_text, inline=False)
    
    embed.set_footer(text="Use !mafia to start a new game")
    
    await ctx.send(embed=embed)
    logger.info(f"Force stop completed: {unmuted_count} unmuted")


@bot.command(name='gamesettings', help='View current game settings')
async def view_settings(ctx):
    """View current game settings"""
    game = get_game(ctx.guild.id)
    
    if game:
        settings = game.settings
    else:
        settings = GameSettings()
    
    embed = discord.Embed(
        title="⚙️ Mafia Game Settings",
        color=discord.Color.blue()
    )
    embed.add_field(name="🔪 Mafia Count", value=str(settings.num_mafia), inline=True)
    embed.add_field(name="💉 Doctor Count", value=str(settings.num_doctor), inline=True)
    embed.add_field(name="🔍 Police Count", value=str(settings.num_police), inline=True)
    embed.add_field(name="🗳️ Voting Time", value=f"{settings.voting_time}s", inline=True)
    embed.add_field(name="💬 Discussion Time", value=f"{settings.discussion_time}s", inline=True)
    embed.add_field(name="📝 Registration Time", value=f"{settings.registration_time}s", inline=True)
    embed.add_field(name="⏭️ Mafia Skip Kills", value=str(settings.mafia_skip_kills), inline=True)
    reveal_labels = {1: "Hidden", 2: "Mafia/Not", 3: "Full Role"}
    embed.add_field(name="👁️ Role Reveal", value=reveal_labels.get(settings.role_reveal_mode, "Full Role"), inline=True)
    
    await ctx.send(embed=embed)


@bot.command(name='setmafia', help='Set number of mafia (1-5)')
@commands.has_permissions(administrator=True)
async def set_mafia_count(ctx, count: int):
    """Set the number of mafia players"""
    if count < 1:
        await ctx.send("❌ There must be at least 1 Mafia!")
        return
    if count > 5:
        await ctx.send("❌ Maximum 5 Mafia allowed!")
        return
    
    game = get_game(ctx.guild.id)
    if game and game.phase == GamePhase.REGISTRATION:
        game.settings.num_mafia = count
    else:
        # Create settings for next game
        if ctx.guild.id not in active_games:
            game = create_game(ctx.guild.id)
        game = active_games[ctx.guild.id]
        game.settings.num_mafia = count
    
    await ctx.send(f"✅ Mafia count set to **{count}**")


@bot.command(name='setdoctor', help='Set number of doctors (0-3)')
@commands.has_permissions(administrator=True)
async def set_doctor_count(ctx, count: int):
    """Set the number of doctor players"""
    if count < 0:
        await ctx.send("❌ Doctor count cannot be negative!")
        return
    if count > 3:
        await ctx.send("❌ Maximum 3 Doctors allowed!")
        return
    
    game = get_game(ctx.guild.id)
    if game and game.phase == GamePhase.REGISTRATION:
        game.settings.num_doctor = count
    else:
        if ctx.guild.id not in active_games:
            game = create_game(ctx.guild.id)
        game = active_games[ctx.guild.id]
        game.settings.num_doctor = count
    
    await ctx.send(f"✅ Doctor count set to **{count}**")


@bot.command(name='setpolice', help='Set number of police (0-3)')
@commands.has_permissions(administrator=True)
async def set_police_count(ctx, count: int):
    """Set the number of police players"""
    if count < 0:
        await ctx.send("❌ Police count cannot be negative!")
        return
    if count > 3:
        await ctx.send("❌ Maximum 3 Police allowed!")
        return
    
    game = get_game(ctx.guild.id)
    if game and game.phase == GamePhase.REGISTRATION:
        game.settings.num_police = count
    else:
        if ctx.guild.id not in active_games:
            game = create_game(ctx.guild.id)
        game = active_games[ctx.guild.id]
        game.settings.num_police = count
    
    await ctx.send(f"✅ Police count set to **{count}**")


@bot.command(name='setvotetime', help='Set voting time in seconds (30-300)')
@commands.has_permissions(administrator=True)
async def set_vote_time(ctx, seconds: int):
    """Set the voting time"""
    if seconds < 30 or seconds > 300:
        await ctx.send("❌ Voting time must be between 30 and 300 seconds!")
        return
    
    game = get_game(ctx.guild.id)
    if game:
        game.settings.voting_time = seconds
    else:
        game = create_game(ctx.guild.id)
        game.settings.voting_time = seconds
    
    await ctx.send(f"✅ Voting time set to **{seconds}** seconds")


@bot.command(name='setdiscusstime', help='Set discussion time in seconds (30-600)')
@commands.has_permissions(administrator=True)
async def set_discuss_time(ctx, seconds: int):
    """Set the discussion time"""
    if seconds < 30 or seconds > 600:
        await ctx.send("❌ Discussion time must be between 30 and 600 seconds!")
        return
    
    game = get_game(ctx.guild.id)
    if game:
        game.settings.discussion_time = seconds
    else:
        game = create_game(ctx.guild.id)
        game.settings.discussion_time = seconds
    
    await ctx.send(f"✅ Discussion time set to **{seconds}** seconds")




@bot.command(name='setregtime', help='Set registration time in seconds (30-300)')
@commands.has_permissions(administrator=True)
async def set_reg_time(ctx, seconds: int):
    """Set the registration time"""
    if seconds < 30 or seconds > 300:
        await ctx.send("❌ Registration time must be between 30 and 300 seconds!")
        return
    
    game = get_game(ctx.guild.id)
    if game:
        game.settings.registration_time = seconds
    else:
        game = create_game(ctx.guild.id)
        game.settings.registration_time = seconds
    
    await ctx.send(f"✅ Registration time set to **{seconds}** seconds")


@bot.command(name='setskips', help='Set how many times mafia can skip killing (0-5)')
@commands.has_permissions(administrator=True)
async def set_skip_kills(ctx, count: int):
    """Set the number of times mafia can skip killing per game"""
    if count < 0 or count > 5:
        await ctx.send("❌ Mafia skip kills must be between 0 and 5!")
        return
    
    game = get_game(ctx.guild.id)
    if game:
        game.settings.mafia_skip_kills = count
    else:
        game = create_game(ctx.guild.id)
        game.settings.mafia_skip_kills = count
    
    await ctx.send(f"✅ Mafia can now skip killing **{count}** time(s) per game")


@bot.command(name='setreveal', help='Set role reveal mode (1=hidden, 2=mafia/not, 3=full role)')
@commands.has_permissions(administrator=True)
async def set_reveal_mode(ctx, mode: int):
    """Set the role reveal mode on elimination"""
    if mode not in (1, 2, 3):
        await ctx.send("❌ Reveal mode must be 1, 2, or 3!\n"
                       "**1** = No reveal (just voted out)\n"
                       "**2** = Mafia or Not Mafia\n"
                       "**3** = Full role with suspense")
        return
    
    game = get_game(ctx.guild.id)
    if game:
        game.settings.role_reveal_mode = mode
    else:
        game = create_game(ctx.guild.id)
        game.settings.role_reveal_mode = mode
    
    labels = {1: "Hidden (no reveal)", 2: "Mafia or Not Mafia", 3: "Full role with suspense"}
    await ctx.send(f"✅ Role reveal mode set to **{mode}** — {labels[mode]}")


@bot.command(name='gamestatus', help='Check current game status')
async def game_status(ctx):
    """Check the current game status"""
    game = get_game(ctx.guild.id)
    
    if not game:
        await ctx.send("❌ No active game!")
        return
    
    embed = discord.Embed(
        title="🎮 Game Status",
        color=discord.Color.purple()
    )
    
    embed.add_field(name="Phase", value=game.phase.value.title(), inline=True)
    embed.add_field(name="Round", value=str(game.round_number), inline=True)
    embed.add_field(name="Total Players", value=str(len(game.players)), inline=True)
    
    alive_players = [p.name for p in game.players.values() if p.is_alive]
    dead_players = [p.name for p in game.players.values() if not p.is_alive]
    
    embed.add_field(name=f"✅ Alive ({len(alive_players)})", value="\n".join(alive_players) if alive_players else "None", inline=True)
    embed.add_field(name=f"💀 Dead ({len(dead_players)})", value="\n".join(dead_players) if dead_players else "None", inline=True)
    
    await ctx.send(embed=embed)


@bot.command(name='mafiahelp', help='Show Mafia game commands')
async def mafia_help(ctx):
    """Show all Mafia game commands"""
    embed = discord.Embed(
        title="🌙 Night Has Come - Commands",
        description="Based on the K-Drama 'Night Has Come'",
        color=discord.Color.purple()
    )
    
    embed.add_field(
        name="🎮 Game Commands",
        value=(
            "`!mafia` - Start a new game (opens registration)\n"
            "`!startgame` - Force start game after registration\n"
            "`!endgame` - End current game\n"
            "`!forcestop` - **Emergency:** Halt all games & reset voice states\n"
            "`!gamestatus` - Check game status"
        ),
        inline=False
    )
    
    embed.add_field(
        name="🧪 Test Mode",
        value=(
            "`!testmafia [players]` - Start solo test game\n"
            "`!testhelp` - Show all test commands"
        ),
        inline=False
    )
    
    embed.add_field(
        name="⚙️ Settings Commands",
        value=(
            "`!gamesettings` - View current settings\n"
            "`!setmafia <1-5>` - Set number of mafia\n"
            "`!setdoctor <0-3>` - Set number of doctors\n"
            "`!setpolice <0-3>` - Set number of police\n"
            "`!setvotetime <30-300>` - Set voting time (seconds)\n"
            "`!setdiscusstime <30-600>` - Set discussion time\n"
            "`!setregtime <30-300>` - Set registration time\n"
            "`!setskips <0-5>` - Set mafia skip kills per game\n"
            "`!setreveal <1-3>` - Role reveal (1=hidden, 2=mafia/not, 3=full)"
        ),
        inline=False
    )
    
    embed.add_field(
        name="📜 Rules",
        value=(
            "• Citizens can skip voting\n"
            "• Doctor can't save themselves 2 rounds in a row\n"
            "• Mafia can chat privately during night\n"
            "• Majority vote eliminates a player\n"
            "• Citizens win if all Mafia eliminated\n"
            "• Mafia wins if they equal/outnumber citizens"
        ),
        inline=False
    )
    
    await ctx.send(embed=embed)


# Error handling
@bot.event
async def on_command_error(ctx, error):
    if isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You don't have permission to use this command!")
    elif isinstance(error, commands.MissingRequiredArgument):
        await ctx.send(f"❌ Missing required argument. Use `!help {ctx.command}` for more info.")
    elif isinstance(error, commands.BadArgument):
        await ctx.send("❌ Invalid argument. Please check your command.")
    else:
        logger.error(f"Legacy command error: {error}")


if __name__ == "__main__":
    if TOKEN is None:
        logger.error("DISCORD_BOT_TOKEN not found in environment variables!")
        print("Error: DISCORD_BOT_TOKEN not found in environment variables!")
        print("Please create a .env file with your bot token.")
    else:
        try:
            logger.info("Starting bot...")
            bot.run(TOKEN)
        except discord.LoginFailure:
            logger.error("Invalid bot token! Please check your .env file.")
        except Exception as e:
            logger.error(f"Failed to start bot: {e}", exc_info=True)
