# Audio Setup for Night Has Come Bot

The bot uses Text-to-Speech to announce game events like the K-Drama!

## Announcements

The bot will say:

- 🌙 **"Night has come. Everyone, go to sleep."** - When night starts
- ☀️ **"The night is over. Everyone, wake up."** - When morning comes
- 🗳️ **"It's time to vote. Choose who to eliminate."** - During voting
- 💉 **"The doctor saved a life tonight."** - When someone is saved
- 🎉 **"The Citizens win! The town is saved."** - Citizens victory
- 🔪 **"The Mafia wins! Darkness has fallen."** - Mafia victory

## Requirements

### 1. FFmpeg (REQUIRED for audio playback)

**Windows:**

1. Download from: https://github.com/BtbN/FFmpeg-Builds/releases
2. Download `ffmpeg-master-latest-win64-gpl.zip`
3. Extract to `C:\ffmpeg`
4. Add `C:\ffmpeg\bin` to your System PATH:
   - Search "Environment Variables" in Windows
   - Edit "Path" under System Variables
   - Add `C:\ffmpeg\bin`
5. Restart your terminal/VS Code

**Verify installation:**

```bash
ffmpeg -version
```

### 2. Python Package (auto-installed)

```bash
pip install gTTS
```

## Custom Audio Files

You can replace the generated audio with your own recordings!

Place `.mp3` files in the `audio/` folder with these names:

- `night_has_come.mp3`
- `night_is_over.mp3`
- `voting_time.mp3`
- `game_start.mp3`
- `mafia_wins.mp3`
- `citizens_win.mp3`
- `someone_eliminated.mp3`
- `someone_saved.mp3`

The bot will use your custom files instead of TTS!

## Troubleshooting

**No sound playing:**

- Make sure FFmpeg is installed and in PATH
- Check that the bot joined the voice channel
- Verify audio files exist in `audio/` folder

**TTS not generating:**

- Run `pip install gTTS`
- Check internet connection (gTTS needs internet)
