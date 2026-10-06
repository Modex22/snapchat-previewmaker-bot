# Snapchat Preview Telegram Bot

Send a video to the bot. It creates the first 8 seconds as a 720x1280 (9:16) H.264 MP4 and sends it back.

## VS Code / Windows setup

1. Install Python 3.11+.
2. Install FFmpeg and make sure this works in the VS Code terminal:
   `ffmpeg -version`
3. Create a Telegram bot with @BotFather and get a **new** token.
4. In this folder, create a file named `.env`.
5. Put this in `.env`:
   `BOT_TOKEN=YOUR_NEW_TOKEN`
6. Install packages:
   `python -m pip install -r requirements.txt`
7. Start the bot:
   `python bot.py`
8. Send `/start` to your Telegram bot and then send a video.

The `.env` file is intentionally ignored by Git so your token is not accidentally uploaded.

## Output

- Duration: 8 seconds
- Resolution: 720x1280
- Aspect ratio: 9:16
- Video: H.264 MP4
- Audio: AAC
- FPS: 30
- Target maximum: 32 MB
