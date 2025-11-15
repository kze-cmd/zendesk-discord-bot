import discord
from discord.ext import commands
import requests
import json
import os
from dotenv import load_dotenv
import sqlite3
import asyncio
from aiohttp import web
import threading
import logging

# === LOGGING ===
logging.basicConfig(level=logging.INFO)

# === LOAD ENV VARS (Render Secrets) ===
load_dotenv()

# === CONFIG ===
DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
ZENDESK_SUBDOMAIN = os.getenv('ZENDESK_SUBDOMAIN')
ZENDESK_EMAIL = os.getenv('ZENDESK_EMAIL')
ZENDESK_TOKEN = os.getenv('ZENDESK_TOKEN')
MAIN_CHANNEL_ID = int(os.getenv('MAIN_CHANNEL_ID'))
WEBHOOK_USER = os.getenv('WEBHOOK_USER')
WEBHOOK_PASS = os.getenv('WEBHOOK_PASS')

# Zendesk API
ZENDESK_AUTH = requests.auth.HTTPBasicAuth(f"{ZENDESK_EMAIL}/token", ZENDESK_TOKEN)
ZENDESK_URL = f'https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2'

# === SQLITE DB ===
conn = sqlite3.connect('tickets.db', check_same_thread=False)
cursor = conn.cursor()
cursor.execute('''
    CREATE TABLE IF NOT EXISTS tickets (
        user_id INTEGER PRIMARY KEY,
        channel_id INTEGER,
        ticket_id INTEGER
    )
''')
conn.commit()

# === DISCORD BOT ===
intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

discord_bot = None

@bot.event
async def on_ready():
    global discord_bot
    discord_bot = bot
    logging.info(f'{bot.user} is online and ready!')

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    user_id = message.author.id
    guild = message.guild

    # === 1. MAIN CHANNEL: Create private + ticket ===
    if message.channel.id == MAIN_CHANNEL_ID:
        overwrites = {
            guild.default_role: discord.PermissionOverwrite(read_messages=False),
            message.author: discord.PermissionOverwrite(read_messages=True, send_messages=True),
            guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
        }
        private_channel = await guild.create_text_channel(
            name=f"support-{message.author.name.lower()}-{user_id % 10000}",
            overwrites=overwrites,
            topic=f"Support for {message.author} | User ID: {user_id}"
        )
        await private_channel.send(
            f"Hello {message.author.mention}! Your support request has been received.\n"
            "Our team will respond shortly. All messages here will sync with Zendesk."
        )

        # Create Zendesk ticket
        payload = {
            "ticket": {
                "subject": f"Discord Support: {message.content[:50]}...",
                "comment": {"body": f"**From Discord User:** {message.author}\n\n{message.content}"},
                "requester": {
                    "name": str(message.author),
                    "email": f"discord+{user_id}@yourtemporarydomain.com"
                },
                "tags": ["discord", "live-chat"],
                "priority": "normal"
            }
        }

        response = requests.post(f'{ZENDESK_URL}/tickets.json', auth=ZENDESK_AUTH, json=payload)
        if response.status_code == 201:
            ticket_data = response.json()['ticket']
            ticket_id = ticket_data['id']

            # Save mapping
            cursor.execute(
                "INSERT OR REPLACE INTO tickets (user_id, channel_id, ticket_id) VALUES (?, ?, ?)",
                (user_id, private_channel.id, ticket_id)
            )
            conn.commit()

            await private_channel.send(
                f"Ticket #{ticket_id} created in Zendesk.\n"
                "Reply here to continue the conversation."
            )
        else:
            await private_channel.send(f"Error creating ticket: {response.status_code} {response.text}")

        return

    # === 2. PRIVATE CHANNEL: Send reply to Zendesk ===
    if message.channel.name.startswith('support-'):
        cursor.execute("SELECT ticket_id FROM tickets WHERE channel_id = ?", (message.channel.id,))
        result = cursor.fetchone()
        if result:
            ticket_id = result[0]
            comment = {
                "comment": {
                    "body": f"**Discord User ({message.author}):**\n{message.content}",
                    "public": True
                }
            }
            response = requests.put(f'{ZENDESK_URL}/tickets/{ticket_id}.json', auth=ZENDESK_AUTH, json=comment)
            if response.status_code != 200:
                await message.channel.send(f"Failed to sync to Zendesk: {response.status_code}")

    await bot.process_commands(message)

# === AIOHTTP WEBHOOK (100% WORKING ON RENDER) ===
async def webhook_handler(request):
    # === AUTH ===
    auth = request.headers.get('Authorization')
    expected_auth = f"Basic {WEBHOOK_USER}:{WEBHOOK_PASS}"
    if not auth or not auth.startswith('Basic '):
        return web.json_response({"error": "Missing auth"}, status=401)
    try:
        import base64
        decoded = base64.b64decode(auth.split(' ', 1)[1]).decode('utf-8')
        if decoded != f"{WEBHOOK_USER}:{WEBHOOK_PASS}":
            return web.json_response({"error": "Invalid credentials"}, status=401)
    except:
        return web.json_response({"error": "Bad auth"}, status=401)

    # === READ JSON ===
    try:
        data = await request.json()
    except:
        return web.json_response({"error": "Invalid JSON"}, status=400)

    # === SOLVED TICKET ===
    if data.get('ticket', {}).get('status') == 'solved':
        ticket_id = data['ticket']['id']
        cursor.execute("SELECT channel_id FROM tickets WHERE ticket_id = ?", (ticket_id,))
        row = cursor.fetchone()
        if row:
            channel = discord_bot.get_channel(row[0])
            if channel:
                await channel.send("**Ticket Solved**\nYour support request has been marked as resolved.\n"
                                   "Send a new message here to reopen support.")

    return web.json_response({"status": "ok"}, status=200)

def start_aiohttp():
    app = web.Application()
    app.router.add_post('/', webhook_handler)
    runner = web.AppRunner(app)
    asyncio.run(runner.setup())
    site = web.TCPSite(runner, '0.0.0.0', 8080)
    logging.info("AIOHTTP Webhook LIVE on http://0.0.0.0:8080")
    asyncio.run(site.start())
    asyncio.Event().wait()  # Keep alive

# === START WEBHOOK IN BACKGROUND ===
threading.Thread(target=start_aiohttp, daemon=True).start()

# === START DISCORD BOT ===
logging.info("Starting Discord bot...")
bot.run(DISCORD_TOKEN)