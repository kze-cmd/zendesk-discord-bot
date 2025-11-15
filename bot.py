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


# === WEBHOOK SERVER ===
class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        auth = self.headers.get('Authorization')
        if not auth or not auth.startswith('Basic '):
            self.send_response(401)
            self.send_header('WWW-Authenticate', 'Basic realm="Zendesk"')
            self.end_headers()
            return

        try:
            credentials = base64.b64decode(auth.split(' ', 1)[1]).decode('utf-8')
            username, password = credentials.split(':', 1)
            if username != WEBHOOK_USER or password != WEBHOOK_PASS:
                raise ValueError("Invalid credentials")
        except:
            self.send_response(401)
            self.end_headers()
            return

        global discord_bot
        if not discord_bot:
            self.send_response(503)
            self.end_headers()
            return

        content_length = int(self.headers['Content-Length'])
        post_data = self.rfile.read(content_length)
        data = json.loads(post_data.decode('utf-8'))

        if 'ticket' in data and data['ticket'].get('status') == 'solved':
            ticket_id = data['ticket']['id']
            cursor.execute("SELECT channel_id FROM tickets WHERE ticket_id = ?", (ticket_id,))
            result = cursor.fetchone()
            if result:
                channel = discord_bot.get_channel(result[0])
                if channel:
                    asyncio.run_coroutine_threadsafe(
                        channel.send("**Ticket Solved**\nYour support request has been marked as resolved.\n"
                                     "Send a new message here to reopen support."),
                        discord_bot.loop
                    )

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"OK")

    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Zendesk -> Discord Webhook Active")


def run_webhook_server():
    server = HTTPServer(('0.0.0.0', 8080), WebhookHandler)
    logging.info("Webhook server running on port 8080...")
    server.serve_forever()

threading.Thread(target=run_webhook_server, daemon=True).start()

logging.info("Starting Discord bot...")
bot.run(DISCORD_TOKEN)