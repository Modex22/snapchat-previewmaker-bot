import os
import asyncio
import tempfile
import shutil
import time
from pathlib import Path

from aiohttp import web
from dotenv import load_dotenv

from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from telegram.request import HTTPXRequest


# =========================================================
# CONFIG
# =========================================================

load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN")

if not BOT_TOKEN:
    raise RuntimeError(
        "BOT_TOKEN is missing.\n"
        "Add BOT_TOKEN to your .env file."
    )

PORT = int(os.getenv("PORT", "10000"))

# Render automatically provides this.
# Locally this will be empty.
RENDER_EXTERNAL_URL = os.getenv("RENDER_EXTERNAL_URL")


# =========================================================
# FIND FFMPEG
# =========================================================

def find_ffmpeg():

    # First check normal PATH
    ffmpeg = shutil.which("ffmpeg")

    if ffmpeg:
        print(f"FFmpeg found: {ffmpeg}")
        return ffmpeg

    # Check WinGet installation
    local_app_data = os.environ.get(
        "LOCALAPPDATA",
        ""
    )

    winget_path = (
        Path(local_app_data)
        / "Microsoft"
        / "WinGet"
        / "Packages"
    )

    if winget_path.exists():

        matches = list(
            winget_path.glob(
                "Gyan.FFmpeg*/**/bin/ffmpeg.exe"
            )
        )

        if matches:

            ffmpeg = str(matches[0])

            print(
                f"FFmpeg found through WinGet: {ffmpeg}"
            )

            return ffmpeg

    return None


FFMPEG = find_ffmpeg()

if not FFMPEG:

    print(
        "WARNING: FFmpeg was not found."
    )

else:

    print(
        f"Using FFmpeg: {FFMPEG}"
    )


# =========================================================
# FIND FFPROBE
# =========================================================

def find_ffprobe():

    ffprobe = shutil.which("ffprobe")

    if ffprobe:
        return ffprobe

    if FFMPEG:

        ffmpeg_path = Path(FFMPEG)

        possible = (
            ffmpeg_path.parent
            / "ffprobe.exe"
        )

        if possible.exists():
            return str(possible)

    return None


FFPROBE = find_ffprobe()


# =========================================================
# TELEGRAM REQUEST SETTINGS
# =========================================================

request = HTTPXRequest(
    connect_timeout=30,
    read_timeout=180,
    write_timeout=180,
    pool_timeout=30,
)


# =========================================================
# TELEGRAM APPLICATION
# =========================================================

application = (
    Application
    .builder()
    .token(BOT_TOKEN)
    .request(request)
    .build()
)


# =========================================================
# /START
# =========================================================

async def start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    await update.message.reply_text(
        "👋 Welcome to Snapchat Preview Maker!\n\n"

        "Send me a video and I'll convert it to:\n\n"

        "📱 720 × 1280\n"
        "📐 9:16\n"
        "🎬 Maximum 8 seconds\n"
        "🎥 MP4 / H.264\n\n"

        "Just send your video."
    )


# =========================================================
# GET VIDEO DURATION
# =========================================================

async def get_video_duration(
    input_path
):

    if not FFPROBE:
        return 8.0

    command = [
        FFPROBE,

        "-v",
        "error",

        "-show_entries",
        "format=duration",

        "-of",
        "default=noprint_wrappers=1:"
        "nokey=1",

        str(input_path),
    ]

    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stdout, stderr = await process.communicate()

    if process.returncode != 0:
        return 8.0

    try:

        duration = float(
            stdout.decode().strip()
        )

        return max(
            0.1,
            min(duration, 8.0)
        )

    except Exception:

        return 8.0


# =========================================================
# UPDATE PROGRESS MESSAGE
# =========================================================

async def update_progress(
    status,
    percent,
    last_update
):

    now = time.monotonic()

    # Don't hammer Telegram with messages.
    if (
        now - last_update[0] < 1.5
        and percent < 100
    ):
        return

    percent = max(
        0,
        min(100, int(percent))
    )

    blocks = int(
        percent / 10
    )

    progress_bar = (
        "█" * blocks
        + "░" * (10 - blocks)
    )

    try:

        await status.edit_text(
            "⚙️ Converting video...\n\n"
            f"{progress_bar} {percent}%\n\n"
            "📱 720 × 1280\n"
            "📐 9:16\n"
            "🎬 Maximum 8 seconds"
        )

        last_update[0] = now

    except Exception as error:

        print(
            "Progress update error:",
            repr(error)
        )


# =========================================================
# PROCESS VIDEO
# =========================================================

async def process_video(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE
):

    message = update.message

    if not message or not message.video:
        return

    if not FFMPEG:

        await message.reply_text(
            "❌ FFmpeg is not available."
        )

        return

    temp_dir = Path(
        tempfile.gettempdir()
    )

    input_path = (
        temp_dir
        / f"snap_input_{message.message_id}.mp4"
    )

    output_path = (
        temp_dir
        / f"snap_output_{message.message_id}.mp4"
    )

    status = await message.reply_text(
        "📥 Downloading video...\n\n"
        "0%"
    )

    try:

        # =================================================
        # DOWNLOAD
        # =================================================

        telegram_file = await context.bot.get_file(
            message.video.file_id
        )

        await telegram_file.download_to_drive(
            custom_path=str(input_path)
        )

        # =================================================
        # GET DURATION
        # =================================================

        duration = await get_video_duration(
            input_path
        )

        print(
            f"Video duration used: {duration:.2f}s"
        )

        await status.edit_text(
            "📥 Download complete!\n\n"
            "⚙️ Preparing conversion...\n"
            "0%"
        )

        # =================================================
        # FFMPEG
        # =================================================

        command = [

            FFMPEG,

            "-y",

            "-i",
            str(input_path),

            # Maximum 8 seconds
            "-t",
            "8",

            # 9:16 / 720x1280
            "-vf",
            (
                "scale=720:1280:"
                "force_original_aspect_ratio=increase,"
                "crop=720:1280"
            ),

            # 30 FPS
            "-r",
            "30",

            # H.264
            "-c:v",
            "libx264",

            "-preset",
            "veryfast",

            "-profile:v",
            "high",

            "-pix_fmt",
            "yuv420p",

            # Keep comfortably under 32 MB
            "-b:v",
            "2200k",

            "-maxrate",
            "2500k",

            "-bufsize",
            "5000k",

            # Audio
            "-c:a",
            "aac",

            "-b:a",
            "96k",

            "-ar",
            "48000",

            # Fast-start MP4
            "-movflags",
            "+faststart",

            # Progress output
            "-progress",
            "pipe:1",

            "-nostats",

            str(output_path),
        ]

        print(
            "Running FFmpeg..."
        )

        print(
            " ".join(command)
        )

        process = await asyncio.create_subprocess_exec(
            *command,

            stdout=asyncio.subprocess.PIPE,

            stderr=asyncio.subprocess.PIPE,
        )

        # =================================================
        # READ FFMPEG PROGRESS
        # =================================================

        last_update = [0]

        while True:

            line = await process.stdout.readline()

            if not line:
                break

            line = line.decode(
                errors="ignore"
            ).strip()

            if line.startswith(
                "out_time_ms="
            ):

                try:

                    time_ms = int(
                        line.split(
                            "=",
                            1
                        )[1]
                    )

                    current_seconds = (
                        time_ms / 1_000_000
                    )

                    percent = (
                        current_seconds
                        / duration
                    ) * 100

                    # Never exceed 99% before FFmpeg finishes
                    percent = min(
                        percent,
                        99
                    )

                    await update_progress(
                        status,
                        percent,
                        last_update
                    )

                except Exception:
                    pass

        # =================================================
        # WAIT FOR FFMPEG
        # =================================================

        stderr = await process.stderr.read()

        await process.wait()

        if process.returncode != 0:

            error = stderr.decode(
                errors="ignore"
            )

            print(
                "FFMPEG ERROR:"
            )

            print(error)

            await status.edit_text(
                "❌ Conversion failed.\n\n"
                "Please try another video."
            )

            return

        # =================================================
        # VERIFY OUTPUT
        # =================================================

        if not output_path.exists():

            await status.edit_text(
                "❌ FFmpeg did not create the output."
            )

            return

        # =================================================
        # 100%
        # =================================================

        await update_progress(
            status,
            100,
            [0]
        )

        await asyncio.sleep(
            0.5
        )

        # =================================================
        # FILE SIZE
        # =================================================

        size_mb = (
            output_path.stat().st_size
            / (1024 * 1024)
        )

        print(
            f"Output size: {size_mb:.2f} MB"
        )

        # =================================================
        # SIZE LIMIT
        # =================================================

        if size_mb > 32:

            await status.edit_text(
                f"⚠️ Conversion complete, "
                f"but the file is {size_mb:.1f} MB.\n\n"
                "That's above the 32 MB limit."
            )

            return

        # =================================================
        # UPLOAD
        # =================================================

        await status.edit_text(
            "✅ Conversion complete!\n\n"
            "100%\n\n"
            f"📦 {size_mb:.1f} MB\n"
            "⬆️ Uploading to Telegram..."
        )

        try:

            with open(
                output_path,
                "rb"
            ) as video_file:

                await message.reply_video(

                    video=video_file,

                    caption=(
                        "✅ Snapchat Preview Ready\n\n"
                        "🎬 8 seconds\n"
                        "📱 720 × 1280\n"
                        "📐 9:16\n"
                        "🎥 MP4 / H.264"
                    ),

                    supports_streaming=True,

                    read_timeout=180,

                    write_timeout=180,

                    connect_timeout=30,

                )

        except Exception as upload_error:

            print(
                "UPLOAD ERROR:",
                repr(upload_error)
            )

            # If Telegram timed out after receiving the
            # upload, don't falsely claim conversion failed.
            await status.edit_text(
                "⚠️ Video was converted successfully,\n"
                "but Telegram timed out while uploading it.\n\n"
                f"File size: {size_mb:.1f} MB"
            )

            return

        # =================================================
        # SUCCESS
        # =================================================

        try:

            await status.delete()

        except Exception:

            pass

    except Exception as error:

        print(
            "VIDEO PROCESSING ERROR:"
        )

        print(
            repr(error)
        )

        try:

            await status.edit_text(
                "❌ Something went wrong "
                "while processing the video."
            )

        except Exception:

            pass

    finally:

        try:

            input_path.unlink(
                missing_ok=True
            )

        except Exception:

            pass

        try:

            output_path.unlink(
                missing_ok=True
            )

        except Exception:

            pass


# =========================================================
# HEALTH CHECK
# =========================================================

async def health(request):

    return web.Response(
        text="Snapchat Preview Maker is running."
    )


# =========================================================
# TELEGRAM WEBHOOK
# =========================================================

async def telegram_webhook(request):

    try:

        data = await request.json()

        update = Update.de_json(
            data,
            application.bot
        )

        await application.process_update(
            update
        )

        return web.Response(
            text="OK"
        )

    except Exception as error:

        print(
            "WEBHOOK ERROR:",
            repr(error)
        )

        return web.Response(
            status=500,
            text="ERROR"
        )


# =========================================================
# RENDER MODE
# =========================================================

async def run_render():

    print(
        "Starting Render Web Service..."
    )

    await application.initialize()

    await application.start()

    if not RENDER_EXTERNAL_URL:

        raise RuntimeError(
            "RENDER_EXTERNAL_URL is missing."
        )

    webhook_url = (
        f"{RENDER_EXTERNAL_URL}/telegram"
    )

    print(
        f"Telegram webhook: {webhook_url}"
    )

    await application.bot.set_webhook(
        url=webhook_url
    )

    print(
        "Telegram webhook configured."
    )

    app = web.Application()

    app.router.add_get(
        "/",
        health
    )

    app.router.add_get(
        "/healthz",
        health
    )

    app.router.add_post(
        "/telegram",
        telegram_webhook
    )

    runner = web.AppRunner(
        app
    )

    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT
    )

    await site.start()

    print(
        f"Server running on port {PORT}"
    )

    while True:

        await asyncio.sleep(
            3600
        )


# =========================================================
# LOCAL MODE
# =========================================================

async def run_local():

    print(
        "Starting local Telegram bot..."
    )

    print(
        "Polling Telegram for messages."
    )

    await application.initialize()

    await application.start()

    await application.updater.start_polling()

    try:

        while True:

            await asyncio.sleep(
                3600
            )

    finally:

        await application.updater.stop()

        await application.stop()

        await application.shutdown()


# =========================================================
# HANDLERS
# =========================================================

application.add_handler(
    CommandHandler(
        "start",
        start
    )
)

application.add_handler(
    MessageHandler(
        filters.VIDEO,
        process_video
    )
)


# =========================================================
# START
# =========================================================

if __name__ == "__main__":

    if RENDER_EXTERNAL_URL:

        asyncio.run(
            run_render()
        )

    else:

        asyncio.run(
            run_local()
        )