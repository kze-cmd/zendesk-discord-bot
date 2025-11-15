import logging
import discord
from discord.ext import commands
import requests
import json
import os
from dotenv import load_dotenv
import sqlite3
import asyncio
from http.server import HTTPServer, BaseHTTPRequestHandler
import threading
import base64

load_dotenv()
logging.basicConfig(level=logging.INFO)

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
ZENDESK_SUBDOMAIN = os.getenv('ZENDESK_SUBDOMAIN')
ZENDESK_EMAIL = os.getenv('ZENDESK_EMAIL')
ZENDESK_TOKEN = os.getenv('ZENDESK_TOKEN')
MAIN_CHANNEL_ID = int(os.getenv('MAIN_CHANNEL_ID'))
WEBHOOK_USER = os.getenv('WEBHOOK_USER')
WEBHOOK_PASS = os.getenv('WEBHOOK_PASS')

ZENDESK_AUTH = requests.auth.HTTPBasicAuth(f"{ZENDESK_EMAIL}/token", ZENDESK_TOKEN)
ZENDESK_URL = f'https://{ZENDESK_SUBDOMAIN}.zendesk.com/api/v2'

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

intents = discord.Intents.default()
intents.message_content = True
intents.members = True
bot = commands.Bot(command_prefix='!', intents=intents)

discord_bot = None

@bot.event
async def on_ready():
    global discord_bot
    discord_bot = bot
    print(f'{bot.user} is online and ready!')

@bot.event
async def on_message(message):
    if message.author.bot:
        return

    user_id = message.author.id
    guild = message.guild

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

# === 100% WORKING WEBHOOK (NO FLASK, BUILT-IN) ===
from wsgiref.simple_server import make_server
import json
import threading

def webhook_app(environ, start_response):
    # === CHECK METHOD ===
    if environ['REQUEST_METHOD'] != 'POST':
        start_response('404 Not Found', [('Content-Type', 'application/json')])
        return [b'{"message":"Cannot POST /","error":"Not Found","statusCode":404}']

    # === AUTH ===
    auth = environ.get('HTTP_AUTHORIZATION')
    if auth != 'Basic ZGlzY29yZGJvdDpzdXBlcnNlY3JldDEyMw==':
        start_response('401 Unauthorized', [('Content-Type', 'application/json')])
        return [b'{"error":"Unauthorized"}']

    # === READ BODY ===
    try:
        length = int(environ.get('CONTENT_LENGTH', 0))
    except ValueError:
        length = 0
    body = environ['wsgi.input'].read(length)
    try:
        data = json.loads(body)
    except:
        start_response('400 Bad Request', [('Content-Type', 'application/json')])
        return [b'{"error":"Invalid JSON"}']

    # === SOLVED TICKET ===
    if data.get('ticket', {}).get('status') == 'solved':
        ticket_id = data['ticket']['id']
        cursor.execute("SELECT channel_id FROM tickets WHERE ticket_id = ?", (ticket_id,))
        row = cursor.fetchone()
        if row:
            channel = discord_bot.get_channel(row[0])
            if channel:
                asyncio.run_coroutine_threadsafe(
                    channel.send("**Ticket Solved**\nYour support request has been marked as resolved.\n"
                                 "Send a new message here to reopen support."),
                    discord_bot.loop
                )

    # === SUCCESS ===
    start_response('200 OK', [('Content-Type', 'application/json')])
    return [b'{"status":"ok"}']

def start_webhook():
    logging.info("Webhook server LIVE on http://0.0.0.0:8080")
    server = make_server('0.0.0.0', 8080, webhook_app)
    server.serve_forever()

threading.Thread(target=start_webhook, daemon=True).start()

# === START BOT ===
logging.info("Starting Discord bot...")
bot.run(DISCORD_TOKEN)