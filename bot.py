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
API_ID = int(os.getenv("API_ID"))
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
MAIN_CHANNEL = int(os.getenv("MAIN_CHANNEL"))
DB_CHANNEL = int(os.getenv("DB_CHANNEL"))

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
    await message.reply_text("Bot is online! Use /anime <name> -e <ep> -r <res> to download.")

@app.on_message(filters.command("anime"))
async def anime_download(client, message: Message):
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
            
            # --- FINAL RENAMING LOGIC ---
            # Format: Ep_1_Jujutsu_Kaisen_360p.mp4
            safe_name = anime_name.replace(" ", "_").replace(":", "").replace("/", "")
            final_filename = f"Ep_{episode}_{safe_name}_{res}p.mp4"
            new_file_path = os.path.join(os.path.dirname(latest_file), final_filename)
            
            try:
                os.rename(latest_file, new_file_path)
            except OSError:
                new_file_path = latest_file

            # Upload
            await status_msg.edit_text(f"Uploading {final_filename}...")
            
            sent_msg = await app.send_document(
                chat_id=MAIN_CHANNEL,
                document=new_file_path,
                caption=final_filename,  # Caption is now just the filename
                force_document=True
            )

            await sent_msg.copy(chat_id=DB_CHANNEL)

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
    print("Bot Started...")
    app.run()
