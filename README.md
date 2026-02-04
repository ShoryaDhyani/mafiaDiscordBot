# Discord Voice Chat Operator Bot

A Discord bot designed to manage and operate voice channels with comprehensive moderation features and a **Mafia (Night Has Come)** game system!

## Features

- 🎤 **Voice Channel Management**
  - Join and leave voice channels
  - Monitor user activity in voice channels
  - Get detailed voice channel information

- 🔇 **User Moderation**
  - Mute/unmute individual users
  - Deafen/undeafen users
  - Mute/unmute all users in a channel
  - Move users between channels
  - Disconnect users from voice channels

- 🔒 **Channel Controls**
  - Lock/unlock voice channels
  - Set user limits
  - Permission-based command access

- 🌙 **Mafia Game (Night Has Come)**
  - Based on the K-Drama "Night Has Come"
  - Automatic role assignment (Mafia, Doctor, Police, Citizens)
  - Night/Day phase management with automatic muting
  - Private DM interactions for night actions
  - Mafia can chat privately during night phase
  - Doctor self-save restriction (can't save themselves 2 rounds in a row)
  - Voting system with skip option
  - Fully configurable game settings

## Setup Instructions

1. **Create a Discord Bot**
   - Go to [Discord Developer Portal](https://discord.com/developers/applications)
   - Create a new application
   - Go to the "Bot" section and create a bot
   - Enable these Privileged Gateway Intents:
     - Server Members Intent
     - Message Content Intent
   - Copy your bot token

2. **Install Dependencies**

   ```bash
   pip install -r requirements.txt
   ```

3. **Configure Environment**
   - Copy `.env.example` to `.env`
   - Add your bot token to the `.env` file:
     ```
     DISCORD_BOT_TOKEN=your_actual_bot_token_here
     ```

4. **Invite Bot to Server**
   - Go to OAuth2 > URL Generator in Developer Portal
   - Select scopes: `bot`
   - Select permissions:
     - Send Messages
     - Embed Links
     - Mute Members
     - Deafen Members
     - Move Members
     - Manage Channels
   - Use the generated URL to invite the bot

5. **Run the Bot**
   ```bash
   python main.py
   ```

## Commands

### 🌙 Mafia Game Commands

| Command       | Description                           |
| ------------- | ------------------------------------- |
| `!mafia`      | Start a new game (opens registration) |
| `!startgame`  | Force start game after players join   |
| `!endgame`    | End the current game                  |
| `!gamestatus` | Check current game status             |
| `!mafiahelp`  | Show all Mafia game commands          |

### ⚙️ Mafia Game Settings

| Command                    | Description                 |
| -------------------------- | --------------------------- |
| `!gamesettings`            | View current settings       |
| `!setmafia <1-5>`          | Set number of mafia players |
| `!setdoctor <0-3>`         | Set number of doctors       |
| `!setpolice <0-3>`         | Set number of police        |
| `!setvotetime <30-300>`    | Set voting time (seconds)   |
| `!setdiscusstime <30-600>` | Set discussion time         |
| `!setnighttime <15-120>`   | Set night action time       |
| `!setregtime <30-300>`     | Set registration time       |

### Voice Channel Commands

- `!join` - Bot joins your current voice channel
- `!leave` - Bot leaves the voice channel
- `!vcinfo [channel]` - Display voice channel information
- `!setlimit <number> [channel]` - Set user limit for a voice channel

### User Moderation Commands

- `!mute <@user>` - Mute a specific user
- `!unmute <@user>` - Unmute a specific user
- `!deafen <@user>` - Deafen a specific user
- `!undeafen <@user>` - Undeafen a specific user
- `!move <@user> <channel>` - Move user to another channel
- `!disconnect <@user>` - Disconnect user from voice

### Bulk Operations

- `!muteall` - Mute all users in your voice channel
- `!unmuteall` - Unmute all users in your voice channel

### Channel Security

- `!lock [channel]` - Lock voice channel (prevent new joins)
- `!unlock [channel]` - Unlock voice channel

## 🌙 Mafia Game Rules

### Roles

- **Citizens** - Vote to eliminate Mafia during the day
- **Mafia** - Eliminate citizens at night, blend in during the day
- **Doctor** - Save one person each night from being eliminated
- **Police** - Investigate one person each night to learn if they're Mafia

### Game Flow

1. **Registration** - Players join by clicking the button
2. **Role Assignment** - Bot DMs each player their role
3. **Night Phase** - Players are muted; Mafia, Doctor, and Police perform actions via DM
4. **Day Phase** - Players are unmuted; discuss who might be Mafia
5. **Voting** - Vote to eliminate a suspect or skip
6. **Repeat** until one team wins

### Special Rules

- **Doctor Self-Save**: If a doctor saves themselves, they cannot save themselves the next round (but can save others)
- **Skip Voting**: Citizens can skip the vote instead of voting for someone
- **Mafia Chat**: Mafia members can communicate privately during the night phase by typing in their DMs
- **Ties**: If there's a tie in voting, no one is eliminated

### Win Conditions

- **Citizens Win**: All Mafia members are eliminated
- **Mafia Wins**: Mafia members equal or outnumber citizens

## Permissions

Most commands require Administrator permissions. Make sure to set up proper role permissions in your Discord server.

## Requirements

- Python 3.8+
- discord.py 2.3.2+
- python-dotenv
- PyNaCl (for voice support)

## Notes

- The bot needs "Administrator" or specific voice-related permissions to function properly
- Commands can be customized by modifying the command prefix in `main.py`
- Voice state changes are logged in the console for monitoring

## Troubleshooting

- **Bot not responding**: Check if Message Content Intent is enabled
- **Can't mute/move users**: Ensure bot has proper voice permissions
- **Connection issues**: Verify your bot token is correct in `.env`
