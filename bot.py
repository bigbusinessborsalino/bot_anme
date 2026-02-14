import os
import glob
import asyncio
import logging
import sys
from pyrogram import Client, filters
from pyrogram.types import Message
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Configuration
# We use a helper function to safely get integers without crashing
def get_env_int(var_name, default=None):
    val = os.getenv(var_name)
    if val and val.strip().lstrip("-").isdigit():
        return int(val)
    return default

API_ID = get_env_int("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
MAIN_CHANNEL = get_env_int("MAIN_CHANNEL")

# Initialize DB_CHANNEL as None (will be set via forward later)
DB_CHANNEL = get_env_int("DB_CHANNEL")

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

app = Client("anime_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

@app.on_message(filters.command("start"))
async def start(client, message):
    status_text = "Bot is online!\n"
    if DB_CHANNEL:
        status_text += f"✅ DB Channel set to: `{DB_CHANNEL}`"
    else:
        status_text += "⚠️ **DB Channel NOT set.**\nPlease forward a message from your DB Channel to this bot to set it."
    
    await message.reply_text(status_text)

# --- NEW FEATURE: Set DB Channel via Forwarding ---
@app.on_message(filters.forwarded & filters.private)
async def set_db_channel_via_forward(client, message: Message):
    global DB_CHANNEL
    if message.forward_from_chat:
        chat_id = message.forward_from_chat.id
        chat_title = message.forward_from_chat.title
        DB_CHANNEL = chat_id
        await message.reply_text(f"✅ **Success!**\nDB Channel set to: **{chat_title}** (`{chat_id}`)")
        logger.info(f"DB_CHANNEL set to {chat_id} via forward")
    else:
        await message.reply_text("❌ Could not detect channel ID. Make sure you are forwarding from a **Public Channel** or that the bot is an Admin there.")

@app.on_message(filters.command("anime"))
async def anime_download(client, message: Message):
    global DB_CHANNEL
    
    # Check if DB Channel is set before starting
    if DB_CHANNEL is None:
        await message.reply_text("⚠️ **Error:** DB Channel is not set.\nPlease forward a message from the DB Channel to me first!")
        return

    command_text = message.text.split(" ", 1)
    
    if len(command_text) < 2:
        await message.reply_text("Usage: /anime <name> -e <episode> -r <resolution|all>")
        return

    args = command_text[1]
    
    try:
        if "-e" not in args or "-r" not in args:
             await message.reply_text("Error: Missing -e or -r flags.")
             return

        parts = args.split("-e")
        anime_name = parts[0].strip()
        rest = parts[1].split("-r")
        episode = rest[0].strip()
        resolution_arg = rest[1].strip()

        resolutions = []
        if resolution_arg.lower() == "all":
            resolutions = ["360", "720", "1080"]
        else:
            resolutions = [resolution_arg]

        status_msg = await message.reply_text(f"Queueing **{anime_name}** Episode **{episode}**...")

        for res in resolutions:
            await status_msg.edit_text(f"Processing **{anime_name}** - Episode {episode} [{res}p]...")
            
            cmd = f"./animepahe-dl.sh -d -a '{anime_name}' -e {episode} -r {res}"
            
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT 
            )

            # Stream logs
            while True:
                line = await process.stdout.readline()
                if not line:
                    break
                print(f"[SCRIPT] {line.decode().strip()}")

            await process.wait()
            
            if process.returncode != 0:
                await message.reply_text(f"Failed to download {res}p. Check logs.")
                continue

            # Find the downloaded file
            files = glob.glob("**/*.mp4", recursive=True)
            if not files:
                await message.reply_text(f"Download completed but file not found for {res}p.")
                continue
            
            latest_file = max(files, key=os.path.getctime)
            
            # --- RENAMING LOGIC ---
            # Format: Ep_1_Jujutsu_Kaisen_360p.mp4
            safe_name = anime_name.replace(" ", "_").replace(":", "").replace("/", "")
            final_filename = f"Ep_{episode}_{safe_name}_{res}p.mp4"
            new_file_path = os.path.join(os.path.dirname(latest_file), final_filename)
            
            try:
                os.rename(latest_file, new_file_path)
            except OSError:
                new_file_path = latest_file

            # Upload to Main Channel
            await status_msg.edit_text(f"Uploading {final_filename}...")
            
            sent_msg = await app.send_document(
                chat_id=MAIN_CHANNEL,
                document=new_file_path,
                caption=final_filename,
                force_document=True
            )

            # Forward to DB Channel
            try:
                await sent_msg.copy(chat_id=DB_CHANNEL)
            except Exception as e:
                await message.reply_text(f"⚠️ Failed to forward to DB Channel: {e}")

            # Delete
            os.remove(new_file_path)
            # Cleanup folder
            parent_dir = os.path.dirname(new_file_path)
            if not os.listdir(parent_dir):
                os.rmdir(parent_dir)

        await status_msg.edit_text("All done!")

    except Exception as e:
        logger.error(f"Error: {e}")
        await message.reply_text(f"Error: {str(e)}")

if __name__ == "__main__":
    print("Bot Starting...")
    app.run()
