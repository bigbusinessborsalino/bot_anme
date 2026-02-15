import os
import glob
import asyncio
import logging
import sys
import stat
import aiohttp
from pyrogram import Client, filters
from pyrogram.types import Message
from dotenv import load_dotenv
from aiohttp import web

# Load environment variables
load_dotenv()

# Configuration
def get_env_int(var_name, default=None):
    val = os.getenv(var_name)
    if val and val.strip().lstrip("-").isdigit():
        return int(val)
    return default

API_ID = get_env_int("API_ID")
API_HASH = os.getenv("API_HASH")
BOT_TOKEN = os.getenv("BOT_TOKEN")
MAIN_CHANNEL = get_env_int("MAIN_CHANNEL")
DB_CHANNEL = get_env_int("DB_CHANNEL")
PORT = get_env_int("PORT", 8000)

# Setup Logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
logger = logging.getLogger(__name__)

app = Client("anime_bot", api_id=API_ID, api_hash=API_HASH, bot_token=BOT_TOKEN)

# --- Jikan API Helper ---
async def get_anime_info(anime_name):
    url = f"https://api.jikan.moe/v4/anime?q={anime_name}&limit=1"
    async with aiohttp.ClientSession() as session:
        async with session.get(url) as response:
            if response.status == 200:
                data = await response.json()
                if data['data']:
                    anime = data['data'][0]
                    title = anime.get('title_english') or anime.get('title')
                    genres = ", ".join([g['name'] for g in anime.get('genres', [])])
                    status = anime.get('status', 'Unknown')
                    image_url = anime['images']['jpg']['large_image_url']
                    # Create a hashtag from the title (remove spaces/symbols)
                    hashtag = "".join(x for x in title if x.isalnum())
                    
                    caption = (
                        f"**{title}**\n\n"
                        f"➜ **Genres:** {genres}\n"
                        f"➜ **Status:** {status}\n\n"
                        f"#{hashtag}"
                    )
                    return caption, image_url
    return None, None

# --- DUMMY WEB SERVER ---
async def web_server():
    async def handle(request):
        return web.Response(text="Bot is running!")

    server = web.Application()
    server.router.add_get("/", handle)
    runner = web.AppRunner(server)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    logger.info(f"Web server started on port {PORT}")

@app.on_message(filters.command("start"))
async def start(client, message):
    status_text = "Bot is online!\n"
    if DB_CHANNEL:
        status_text += f"✅ DB Channel set to: `{DB_CHANNEL}`"
    else:
        status_text += "⚠️ **DB Channel NOT set.**\nPlease forward a message from your DB Channel to this bot to set it."
    await message.reply_text(status_text)

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
        await message.reply_text("❌ Could not detect channel ID.")

@app.on_message(filters.command("anime"))
async def anime_download(client, message: Message):
    global DB_CHANNEL
    
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

        # Resolution Order: 360 -> 720 -> 1080
        if resolution_arg.lower() == "all":
            resolutions = ["360", "720", "1080"]
        else:
            resolutions = [resolution_arg]

        status_msg = await message.reply_text(f"🔍 Searching info for **{anime_name}**...")

        # --- 1. SEND INFO POST ---
        caption, image_url = await get_anime_info(anime_name)
        if caption and image_url:
            # Send to Main Channel
            await app.send_photo(MAIN_CHANNEL, photo=image_url, caption=caption)
            # Send to DB Channel
            await app.send_photo(DB_CHANNEL, photo=image_url, caption=caption)
            await status_msg.edit_text(f"✅ Info Post Sent.\nQueueing **{anime_name}** Episode **{episode}**...")
        else:
            await status_msg.edit_text(f"⚠️ Could not find info for {anime_name}, proceeding with download only...")

        # --- SELF-REPAIR ---
        script_path = "./animepahe-dl.sh"
        if os.path.exists(script_path):
            st = os.stat(script_path)
            os.chmod(script_path, st.st_mode | stat.S_IEXEC)

        success_count = 0

        # --- 2. DOWNLOAD LOOP ---
        for res in resolutions:
            await status_msg.edit_text(f"Processing **{anime_name}** - Episode {episode} [{res}p]...")
            
            # Use -t 1 (Safe Mode) to keep RAM usage low
            cmd = f"./animepahe-dl.sh -d -t 1 -a '{anime_name}' -e {episode} -r {res}"
            logger.info(f"Executing: {cmd}")
            
            process = await asyncio.create_subprocess_shell(
                cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT 
            )

            # Log streaming (optional, keeps Koyeb alive)
            while True:
                line = await process.stdout.readline()
                if not line: break
                decoded_line = line.decode('utf-8', errors='ignore').strip()
                if decoded_line:
                    print(f"[SCRIPT] {decoded_line}")

            await process.wait()
            
            if process.returncode != 0:
                await message.reply_text(f"❌ Failed to download {res}p. (Exit Code: {process.returncode})")
                continue

            files = glob.glob("**/*.mp4", recursive=True)
            if not files:
                await message.reply_text(f"❌ File not found for {res}p.")
                continue
            
            latest_file = max(files, key=os.path.getctime)
            
            # Rename
            safe_name = anime_name.replace(" ", "_").replace(":", "").replace("/", "")
            final_filename = f"Ep_{episode}_{safe_name}_{res}p.mp4"
            new_file_path = os.path.join(os.path.dirname(latest_file), final_filename)
            try:
                os.rename(latest_file, new_file_path)
            except OSError:
                new_file_path = latest_file

            # Upload
            await status_msg.edit_text(f"Uploading {final_filename}...")
            try:
                sent_msg = await app.send_document(
                    chat_id=MAIN_CHANNEL,
                    document=new_file_path,
                    caption=final_filename,
                    force_document=True
                )
                await sent_msg.copy(chat_id=DB_CHANNEL)
                success_count += 1
            except Exception as e:
                await message.reply_text(f"⚠️ Upload Failed: {e}")

            # Cleanup
            try:
                os.remove(new_file_path)
                parent_dir = os.path.dirname(new_file_path)
                if not os.listdir(parent_dir):
                    os.rmdir(parent_dir)
            except Exception:
                pass

            # --- 3. COOL DOWN (30 Seconds) ---
            if res != resolutions[-1]: # Don't wait after the very last one
                await status_msg.edit_text(f"❄️ Cooling down for 30s to save memory...")
                await asyncio.sleep(30)

        # --- 4. FINAL STATUS & STICKER ---
        if success_count == len(resolutions):
            await status_msg.edit_text("✅ All done! Download successful.")
            # Send the sticker only if all downloads worked (or at least one worked)
            await message.reply_sticker("CAACAgUAAxkBAAEQJ6hpV0JDpDDOI68yH7lV879XbIWiFwACGAADQ3PJEs4sW1y9vZX3OAQ")
        else:
            await status_msg.edit_text(f"⚠️ Job finished. {success_count}/{len(resolutions)} successful.")

    except Exception as e:
        logger.error(f"Error: {e}")
        await message.reply_text(f"Error: {str(e)}")

if __name__ == "__main__":
    print("Bot Starting...")
    loop = asyncio.get_event_loop()
    loop.create_task(web_server())
    app.run()
