# Use a lightweight Python image
FROM python:3.10-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    curl \
    jq \
    fzf \
    nodejs \
    ffmpeg \
    openssl \
    git \
    dos2unix \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Copy requirements and install
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy all files
COPY . .

# --- FIX: Deep Clean & Automate the Script ---
# 1. dos2unix: Fixes Windows line endings
# 2. sed (1st): Removes invisible space characters
# 3. sed (2nd): Replaces interactive 'fzf' with automatic 'head -n 1' (Fixes /dev/tty error)
# 4. chmod: Makes it executable
RUN dos2unix animepahe-dl.sh && \
    sed -i 's/\xC2\xA0/ /g' animepahe-dl.sh && \
    sed -i 's/"\$_FZF" -1/head -n 1/g' animepahe-dl.sh && \
    chmod +x animepahe-dl.sh

# Start the bot
CMD ["python3", "bot.py"]
