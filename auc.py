import os
import re
import sqlite3
import json
import html
import socket
import sys
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto, ForceReply
from telegram import Update, Message 
from telegram import BotCommand, BotCommandScopeChat
from telegram.utils.helpers import escape_markdown
from telegram.ext import (
    Updater,
    CommandHandler,
    MessageHandler,
    CallbackContext,
    CallbackQueryHandler,
    Filters,
    ConversationHandler
)
from telegram.error import Conflict
from dotenv import load_dotenv
from datetime import datetime
from contextlib import contextmanager

# Debug function with timestamp
def debug_log(message):
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    print(f"[{timestamp}] DEBUG: {message}")

# Conversation states
SELECT_CATEGORY, GET_POKEMON_NAME, GET_NATURE, GET_IVS, GET_MOVESET, GET_BOOSTED, GET_BASE_PRICE, GET_TM_DETAILS = range(2, 10)

# Database context manager
@contextmanager
def db_connection(db_name='auctions.db'):
    conn = None
    try:
        conn = sqlite3.connect(db_name)
        conn.row_factory = sqlite3.Row
        yield conn
    except Exception as e:
        debug_log(f"Database error: {str(e)}")
        raise
    finally:
        if conn:
            conn.close()

# Initialize database with all tables
def init_db():
    try:
        with db_connection() as conn:
            c = conn.cursor()
            
            c.execute('''CREATE TABLE IF NOT EXISTS auctions
                         (auction_id INTEGER PRIMARY KEY AUTOINCREMENT,
                          item_text TEXT NOT NULL,
                          photo_id TEXT,
                          base_price REAL NOT NULL,
                          current_bid REAL,
                          current_bidder TEXT,
                          previous_bidder TEXT,
                          is_active BOOLEAN DEFAULT 1,
                          channel_message_id INTEGER,
                          discussion_message_id INTEGER,
                          created_at DATETIME DEFAULT CURRENT_TIMESTAMP)''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS bids
                         (bid_id INTEGER PRIMARY KEY AUTOINCREMENT,
                          auction_id INTEGER NOT NULL,
                          bidder_id INTEGER NOT NULL,
                          bidder_name TEXT NOT NULL,
                          amount REAL NOT NULL,
                          timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                          is_active BOOLEAN DEFAULT 1,
                          FOREIGN KEY(auction_id) REFERENCES auctions(auction_id))''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS submissions
                         (submission_id INTEGER PRIMARY KEY AUTOINCREMENT,
                          user_id INTEGER NOT NULL,
                          data TEXT NOT NULL,
                          status TEXT DEFAULT 'pending',
                          created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                          channel_message_id INTEGER)''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS temp_data
                         (user_id INTEGER PRIMARY KEY,
                          data TEXT,
                          timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
            
            c.execute('''CREATE TABLE IF NOT EXISTS system_status
                         (id INTEGER PRIMARY KEY,
                          submissions_open BOOLEAN DEFAULT 0,
                          auctions_open BOOLEAN DEFAULT 0)''')
            
            c.execute('''INSERT OR IGNORE INTO system_status (id, submissions_open, auctions_open)
                         VALUES (1, 0, 0)''')
            
            conn.commit()
            debug_log("Database initialized successfully")
    except Exception as e:
        debug_log(f"Database initialization failed: {str(e)}")
        raise

def init_verified_users_db():
    try:
        with db_connection('verified_users.db') as conn:
            c = conn.cursor()
            c.execute("PRAGMA foreign_keys = ON")
            
            tables = {
                'verified_users': '''
                    CREATE TABLE IF NOT EXISTS verified_users (
                        user_id INTEGER PRIMARY KEY,
                        username TEXT NOT NULL,
                        verified_by INTEGER NOT NULL,
                        verified_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        last_active DATETIME,
                        total_submissions INTEGER DEFAULT 0,
                        total_bids INTEGER DEFAULT 0,
                        FOREIGN KEY (verified_by) REFERENCES verified_users(user_id)
                    )''',
                    
                'verification_requests': '''
                    CREATE TABLE IF NOT EXISTS verification_requests (
                        request_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER UNIQUE NOT NULL,
                        username TEXT NOT NULL,
                        request_date DATETIME DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY (user_id) REFERENCES verified_users(user_id) ON DELETE CASCADE
                    )''',
                    
                'user_activity': '''
                    CREATE TABLE IF NOT EXISTS user_activity (
                        log_id INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id INTEGER NOT NULL,
                        action TEXT NOT NULL,
                        timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
                        details TEXT,
                        FOREIGN KEY (user_id) REFERENCES verified_users(user_id) ON DELETE CASCADE
                    )'''
            }

            for table_name, schema in tables.items():
                c.execute(schema)
                
                c.execute(f"PRAGMA table_info({table_name})")
                existing_columns = [col[1] for col in c.fetchall()]
                
                if table_name == 'verified_users' and 'last_active' not in existing_columns:
                    c.execute("ALTER TABLE verified_users ADD COLUMN last_active DATETIME")
                if table_name == 'verified_users' and 'total_bids' not in existing_columns:
                    c.execute("ALTER TABLE verified_users ADD COLUMN total_bids INTEGER DEFAULT 0")

            c.execute('''CREATE INDEX IF NOT EXISTS idx_verified_users_id ON verified_users(user_id)''')
            c.execute('''CREATE INDEX IF NOT EXISTS idx_activity_user ON user_activity(user_id)''')
            c.execute('''CREATE INDEX IF NOT EXISTS idx_requests_date ON verification_requests(request_date)''')
            
            conn.commit()
            debug_log("Verified users database initialized")
    except Exception as e:
        debug_log(f"Verified users DB init failed: {str(e)}")
        raise

load_dotenv("config.env")
TOKEN = os.getenv("BOT_TOKEN")
ADMINS = [int(admin_id) for admin_id in os.getenv("ADMIN_IDS", "").split(",") if admin_id]
CHANNEL_ID = int(os.getenv("CHANNEL_ID"))
CHANNEL_USERNAME = os.getenv("CHANNEL_USERNAME")
DISCUSSION_ID = int(os.getenv("DISCUSSION_ID"))

def ensure_single_instance():
    try:
        # Create a lock to prevent multiple instances
        lock_socket = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        lock_socket.bind('\0' + 'legendauc_bot_lock')  # Unique lock name
        return True
    except socket.error:
        print("Error: Another bot instance is already running")
        return False

def escape_markdown_v2(text):
    """Escape all special characters for MarkdownV2 parsing"""
    if not text:
        return ""
    escape_chars = '_*[]()~`>#+-=|{}.!'
    # First escape backslashes, then other characters
    text = text.replace('\\', '\\\\')
    return ''.join('\\' + char if char in escape_chars else char for char in text)

def set_bot_commands(updater):
    user_commands = [
        BotCommand('start', 'Start the bot'),
        BotCommand('help', 'Show all commands'),
        BotCommand('items', 'View active auctions'),
        BotCommand('myitems', 'View your approved items'),
        BotCommand('mybids', 'View your active bids'),
        BotCommand('add', 'Submit new item')
    ]
    
    admin_commands = [
        BotCommand('verify', 'Verify users'),
        BotCommand('startsubmission', 'Open submissions'),
        BotCommand('endsubmission', 'Close submissions'),
        BotCommand('startauction', 'Start auctions'),
        BotCommand('endauction', 'End auctions'),
        BotCommand('removebid', 'Remove last bid'),
        BotCommand('cleanup', 'Cleanup database')
    ]
    
    try:
        updater.bot.set_my_commands(user_commands)
        for admin_id in ADMINS:
            try:
                updater.bot.set_my_commands(
                    user_commands + admin_commands,
                    scope=BotCommandScopeChat(admin_id)
                )
            except Exception as e:
                debug_log(f"Failed to set admin commands for {admin_id}: {str(e)}")
    except Exception as e:
        debug_log(f"Error setting bot commands: {str(e)}")

def show_help(update: Update, context: CallbackContext):
    is_admin = update.effective_user.id in ADMINS
    
    help_text = [
        "🤖 *Bot Commands* 🤖",
        "",
        "🛠 *General Commands:*",
        "/start - Start the bot",
        "/help - Show this help message",
        "/items - View active auctions",
        "/myitems - View your approved items",
        "/mybids - View your active bids",
        "/add - Submit new item"
    ]
    
    if is_admin:
        help_text.extend([
            "",
            "🔐 *Admin Commands:*",
            "/verify - Verify users",
            "/startsubmission - Open submissions",
            "/endsubmission - Close submissions",
            "/startauction - Start auctions",
            "/endauction - End auctions",
            "/removebid - Remove last bid",
            "/cleanup - Cleanup database"
        ])
    
    update.message.reply_text("\n".join(help_text), parse_mode='Markdown')

def admin_only(func):
    def wrapper(update: Update, context: CallbackContext):
        if update.effective_user.id not in ADMINS:
            update.message.reply_text("🚫 Admin only command")
            return
        return func(update, context)
    return wrapper

def is_forwarded_from_hexamon(update: Update) -> bool:
    if not update.message or not update.message.forward_from:
        return False
    forward_from = update.message.forward_from
    return (forward_from.username and 
            forward_from.username.lower().replace(" ", "") == "hexamonbot")

def get_min_increment(current_bid):
    if current_bid is None or current_bid == 0:
        return 1000
    try:
        current_bid = float(current_bid)
        if current_bid < 20000:
            return 1000
        elif current_bid < 40000:
            return 2000
        elif current_bid < 70000:
            return 3000
        elif current_bid < 100000:
            return 4000
        else:
            return 5000
    except (TypeError, ValueError):
        return 1000

def extract_base_price(text):
    try:
        if not text:
            return None
            
        text = text.lower().replace("base:", "").replace(",", "").strip()
        
        if text == "0":
            return 0
            
        if text.endswith("k"):
            return int(float(text[:-1]) * 1000)
        return int(float(text))
    except (ValueError, AttributeError):
        return None

def save_auction(item_text, photo_id, base_price, channel_msg_id=None):
    """Save auction to database with proper validation and error handling"""
    try:
        if not item_text or base_price is None:
            raise ValueError("Missing required fields (item_text or base_price)")
            
        with db_connection() as conn:
            c = conn.cursor()
            
            # First verify the auction doesn't already exist
            if channel_msg_id:
                c.execute('''SELECT 1 FROM auctions WHERE channel_message_id=?''', (channel_msg_id,))
                if c.fetchone():
                    debug_log("Auction with this channel message ID already exists")
                    return None
            
            # Insert new auction
            c.execute('''INSERT INTO auctions 
                        (item_text, photo_id, base_price, channel_message_id, is_active)
                        VALUES (?, ?, ?, ?, 1)''',
                     (str(item_text), 
                      str(photo_id) if photo_id else None, 
                      float(base_price), 
                      channel_msg_id))
            
            auction_id = c.lastrowid
            conn.commit()
            debug_log(f"Successfully saved auction ID {auction_id}")
            return auction_id

    except Exception as e:
        debug_log(f"Critical error saving auction: {str(e)}")
        raise

def verify_auction_integrity():
    """Check for inconsistencies between submissions and auctions"""
    with db_connection() as conn:
        c = conn.cursor()
        
        # Find approved submissions without corresponding auctions
        c.execute('''SELECT s.submission_id 
                    FROM submissions s
                    LEFT JOIN auctions a ON s.channel_message_id = a.channel_message_id
                    WHERE s.status='approved' AND a.auction_id IS NULL''')
        orphaned = c.fetchall()
        
        if orphaned:
            debug_log(f"Found {len(orphaned)} approved submissions without auctions")
            return False
        
        # Find auctions without approved submissions
        c.execute('''SELECT a.auction_id 
                    FROM auctions a
                    LEFT JOIN submissions s ON a.channel_message_id = s.channel_message_id
                    WHERE s.submission_id IS NULL''')
        unlinked = c.fetchall()
        
        if unlinked:
            debug_log(f"Found {len(unlinked)} auctions without submissions")
            return False
            
        return True

def record_bid(auction_id, bidder_id, bidder_name, amount):
    """Record a bid with proper Markdown escaping"""
    try:
        with db_connection() as conn:
            c = conn.cursor()
            
            # Escape username before storing
            escaped_bidder_name = escape_markdown(bidder_name)
            
            # Get previous top bidder
            c.execute('''SELECT bidder_id, bidder_name, amount 
                         FROM bids 
                         WHERE auction_id=? AND is_active=1
                         ORDER BY amount DESC 
                         LIMIT 1''', (auction_id,))
            prev_bidder = c.fetchone()
            
            # Record new bid
            c.execute('''INSERT INTO bids (auction_id, bidder_id, bidder_name, amount)
                         VALUES (?, ?, ?, ?)''', 
                     (auction_id, bidder_id, escaped_bidder_name, amount))
            
            # Update auction
            c.execute('''UPDATE auctions SET 
                         current_bid=?,
                         previous_bidder=COALESCE(current_bidder, 'None'),
                         current_bidder=?
                         WHERE auction_id=?''',
                      (amount, escaped_bidder_name, auction_id))
            
            conn.commit()
            return prev_bidder
            
    except Exception as e:
        debug_log(f"Error in record_bid: {str(e)}")
        raise

def get_auction(auction_id):
    try:
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''SELECT * FROM auctions WHERE auction_id=?''', (auction_id,))
            result = c.fetchone()
            
            if result:
                return dict(result)
            return None
    except Exception as e:
        debug_log(f"Error in get_auction: {str(e)}")
        return None

def get_auction_by_channel_id(channel_message_id):
    try:
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''SELECT * FROM auctions 
                         WHERE channel_message_id=? AND is_active=1''', 
                     (channel_message_id,))
            result = c.fetchone()
            return dict(result) if result else None
    except Exception as e:
        debug_log(f"Error in get_auction_by_channel_id: {str(e)}")
        return None

def save_submission(user_id, data):
    try:
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''INSERT INTO submissions (user_id, data) 
                         VALUES (?, ?)''',
                     (user_id, json.dumps(data)))
            submission_id = c.lastrowid
            conn.commit()
            return submission_id
    except Exception as e:
        debug_log(f"Error saving submission: {str(e)}")
        raise

def get_submission(submission_id):
    try:
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''SELECT * FROM submissions WHERE submission_id=?''', (submission_id,))
            result = c.fetchone()
            if result:
                return {
                    'submission_id': result['submission_id'],
                    'user_id': result['user_id'],
                    'data': json.loads(result['data']),
                    'status': result['status'],
                    'created_at': result['created_at'],
                    'channel_message_id': result['channel_message_id']
                }
            return None
    except Exception as e:
        debug_log(f"Error getting submission: {str(e)}")
        return None

def save_temp_data(user_id, data):
    try:
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''INSERT OR REPLACE INTO temp_data (user_id, data)
                         VALUES (?, ?)''',
                     (user_id, json.dumps(data)))
            conn.commit()
    except Exception as e:
        debug_log(f"Temp data save failed: {str(e)}")
        raise

def load_temp_data(user_id):
    try:
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''SELECT data FROM temp_data WHERE user_id=?''', (user_id,))
            result = c.fetchone()
            return json.loads(result[0]) if result else {}
    except Exception as e:
        debug_log(f"Temp data load failed: {str(e)}")
        return {}

def cleanup_temp_data(user_id):
    try:
        with db_connection() as conn:
            conn.execute('''DELETE FROM temp_data WHERE user_id=?''', (user_id,))
            conn.commit()
    except Exception as e:
        debug_log(f"Cleanup failed: {str(e)}")

def get_user_active_bids(user_id):
    try:
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''SELECT a.auction_id, a.item_text, b.amount 
                         FROM bids b
                         JOIN auctions a ON b.auction_id = a.auction_id
                         WHERE b.bidder_id=? AND a.is_active=1 AND b.is_active=1
                         ORDER BY b.timestamp DESC''', (user_id,))
            return c.fetchall()
    except Exception as e:
        debug_log(f"Error getting user bids: {str(e)}")
        return []

def format_auction(auction):
    """Format auction message with bulletproof MarkdownV2 escaping"""
    try:
        # Escape all components individually
        auction_id = escape_markdown_v2(str(auction.get('auction_id', '?')))
        item_text = escape_markdown_v2(auction.get('item_text', 'No description available'))
        current_bid = auction.get('current_bid')
        current_bidder = escape_markdown_v2(auction.get('current_bidder', 'None'))
        base_price = auction.get('base_price', 0)
        
        # Format numbers before escaping
        current_bid_str = f"{current_bid:,}" if current_bid is not None else 'None'
        base_price_str = f"{int(base_price):,}" if base_price else '0'
        
        # Escape the formatted numbers
        current_bid_str = escape_markdown_v2(current_bid_str)
        base_price_str = escape_markdown_v2(base_price_str)
        
        # Build message with emojis (no escaping needed for emojis)
        return (
            "🏆 Auction \\#" + auction_id + "\n\n" +  # Escaped # here
            item_text + "\n\n" +
            "🔼 Current Bid: " + current_bid_str + "\n" +
            "👤 Bidder: " + current_bidder + "\n" +
            "💰 Base Price: " + base_price_str + "\n\n" +
            "💬 Click the button below to bid"
        )
    except Exception as e:
        debug_log(f"Error in format_auction: {str(e)}")
        return escape_markdown_v2("⚠️ Auction information unavailable")

def format_pokemon_auction_item(data):
    """Format Pokémon auction item with escaped Markdown"""
    return escape_markdown(
        f"🆕  {data.get('category', '').title()} Pokemon \n\n"
        f"🔤 Pokémon: {data['pokemon_name']}\n"
        f"🌿 Info:\n{data['nature'].get('text', '')}\n\n"
        f"📊 IVs/EVs:\n{data['ivs'].get('text', '')}\n\n"
        f"⚔️ Moveset:\n{data['moveset'].get('text', '')}\n\n"
        f"🔮 Boost Info: {'Boosted' if data.get('boosted') == 'yes' else 'Unboosted'}\n\n"
        f"👤 Seller: @{data.get('seller_username', 'Unknown')}\n\n"
        f"💰 Base Price: {data.get('base_price', 0):,}\n\n"
    )

def format_tm_auction_item(data):
    """Format TM auction item with consistent styling"""
    return (
        f"🆕 *New TM Auction*\n\n"
        f"{escape_markdown(data.get('tm_details', {}).get('text', 'TM details not available'))}\n\n"
        f"💰 *Base Price:* `{data.get('base_price', 0):,}`\n"
        f"👤 *Seller:* @{escape_markdown(data.get('seller_username', 'Unknown'))}"
    )

def start(update: Update, context: CallbackContext):
    if context.args and context.args[0].startswith('bid_'):
        try:
            auction_id = int(context.args[0].split('_')[1])
            auction = get_auction(auction_id)
            
            if not auction:
                update.message.reply_text("❌ Auction not found!")
                return
                
            current_amount = auction.get('current_bid') or auction.get('base_price', 0)
            min_bid = current_amount + get_min_increment(current_amount)
            
            context.user_data['bid_context'] = {
                'auction_id': auction_id,
                'channel_msg_id': auction['channel_message_id'],
                'min_bid': min_bid,
                'current_bidder': auction.get('current_bidder'),
                'item_text': auction['item_text']
            }
            
            update.message.reply_text(
                f"🏆 Auction #{auction_id}\n\n"
                f"Current Bid: {current_amount:,}\n"
                f"Minimum Bid: {min_bid:,}\n\n"
                "Please enter your bid amount:",
                reply_markup=ForceReply(selective=True)
            )
            return
        except Exception as e:
            debug_log(f"Error in deep link handling: {str(e)}")
    
    if update.effective_user.id not in ADMINS and not check_verification_status(update.effective_user.id):
        update.message.reply_text(
            "🔒 Verification Required\n\n"
            "Before using this bot, please contact an admin for verification.\n"
            "Make sure to Join the,\n"
            "Trade Group @legend_slow_auc\n"
            "Auction Channel @legend_auc"
        )
        return
    
    with db_connection() as conn:
        status = conn.execute("SELECT submissions_open, auctions_open FROM system_status WHERE id=1").fetchone()
    
    submissions_open = "🟢 OPEN" if status[0] else "🔴 CLOSED"
    auctions_open = "🟢 OPEN" if status[1] else "🔴 CLOSED"
    
    response = [
        "🏆 Legend Auction Bot 🏆",
        "",
        f"📝 Item Submissions: {submissions_open}",
        f"💰 Auctions: {auctions_open}",
        "",
        "Welcome to the 🐉Legend Auction Bot🐉\n\n"
        "Make sure to Join the,\n"
        "Trade Group @legend_slow_auc\n"
        "Auction Channel @legend_auc\n\n"
        "Use /help to see commands"
    ]
    
    update.message.reply_text("\n".join(response).strip())

@admin_only
def end_submission(update: Update, context: CallbackContext):
    with db_connection() as conn:
        conn.execute("UPDATE system_status SET submissions_open=0 WHERE id=1")
        conn.commit()
    update.message.reply_text("✅ Item submissions are now CLOSED")

@admin_only
def start_submission(update: Update, context: CallbackContext):
    with db_connection() as conn:
        conn.execute("UPDATE system_status SET submissions_open=1 WHERE id=1")
        conn.commit()
    update.message.reply_text("✅ Item submissions are now OPEN")

@admin_only
def start_auction(update: Update, context: CallbackContext):
    with db_connection() as conn:
        conn.execute("UPDATE system_status SET auctions_open=1 WHERE id=1")
        conn.commit()
    update.message.reply_text("✅ Auctions are now OPEN")

@admin_only
def end_auction(update: Update, context: CallbackContext):
    with db_connection() as conn:
        conn.execute("UPDATE system_status SET auctions_open=0 WHERE id=1")
        conn.commit()
    update.message.reply_text("✅ Auctions are now CLOSED")

@admin_only
def verify_user(update: Update, context: CallbackContext):
    if not update.message.reply_to_message:
        update.message.reply_text("❌ Please reply to a user's message with /verify")
        return
    
    target_user = update.message.reply_to_message.from_user
    
    try:
        with db_connection('verified_users.db') as conn:
            c = conn.cursor()
            
            c.execute('SELECT 1 FROM verified_users WHERE user_id=?', (target_user.id,))
            if c.fetchone():
                update.message.reply_text("⚠️ User is already verified")
                return
                
            c.execute('''INSERT INTO verified_users
                        (user_id, username, verified_by)
                        VALUES (?, ?, ?)''',
                    (target_user.id, 
                     target_user.username or target_user.first_name,
                     update.effective_user.id))
            
            conn.commit()
            
            context.bot.send_message(
                target_user.id,
                "✅ Verification Approved!\n\n"
                "You can now access all bot features.\n"
                "Please /start the bot again to refresh your status."
            )
            
            update.message.reply_text(f"✅ Verified @{target_user.username or target_user.id}")
            
    except Exception as e:
        debug_log(f"Verification error: {str(e)}")
        update.message.reply_text("❌ Failed to verify user")

def request_verification(update: Update, context: CallbackContext):
    user = update.effective_user
    
    try:
        with db_connection('verified_users.db') as conn:
            c = conn.cursor()
            
            c.execute('SELECT 1 FROM verified_users WHERE user_id=?', (user.id,))
            if c.fetchone():
                update.message.reply_text("✅ You're already verified!")
                return
                
            c.execute('SELECT 1 FROM verification_requests WHERE user_id=?', (user.id,))
            if c.fetchone():
                update.message.reply_text("⏳ Your verification request is pending. Please wait for admin approval.")
                return
                
            c.execute('''INSERT INTO verification_requests
                        (user_id, username)
                        VALUES (?, ?)''',
                    (user.id, user.username or user.first_name))
            conn.commit()
            
            for admin_id in ADMINS:
                try:
                    context.bot.send_message(
                        chat_id=admin_id,
                        text=f"🆕 Verification Request\n\n"
                             f"User: @{user.username or user.first_name} (ID: {user.id})\n"
                             f"Requested at: {datetime.now().strftime('%Y-%m-%d %H:%M')}\n\n"
                             f"Reply to this user's message with /verify to approve",
                        reply_to_message_id=update.message.message_id
                    )
                except Exception as e:
                    debug_log(f"Failed to notify admin {admin_id}: {str(e)}")
            
            update.message.reply_text(
                "📨 Verification request sent to admins!\n"
                "You'll be notified once approved."
            )
            
    except Exception as e:
        debug_log(f"Verification request error: {str(e)}")
        update.message.reply_text("❌ Failed to process verification request")

@admin_only
def list_verified_users(update: Update, context: CallbackContext):
    try:
        with db_connection('verified_users.db') as conn:
            users = conn.execute('''SELECT user_id, username, verified_at, last_active 
                                   FROM verified_users 
                                   ORDER BY verified_at DESC''').fetchall()
            
            response = ["✅ Verified Users:"]
            for user in users:
                user_id, username, verified_at, last_active = user
                response.append(
                    f"\n👤 @{username} (ID: {user_id})\n"
                    f"   Verified: {verified_at}\n"
                    f"   Last Active: {last_active if last_active else 'Never'}"
                )
                
            update.message.reply_text("\n".join(response))
    except Exception as e:
        debug_log(f"Error listing users: {str(e)}")
        update.message.reply_text("❌ Error fetching user list")

@admin_only
def remove_verification(update: Update, context: CallbackContext):
    if not context.args:
        update.message.reply_text("❌ Usage: /unverify <user_id>")
        return
        
    user_id = int(context.args[0])
    
    try:
        with db_connection('verified_users.db') as conn:
            conn.execute("DELETE FROM verified_users WHERE user_id=?", (user_id,))
            conn.commit()
        
        update.message.reply_text(f"✅ User {user_id} verification removed")
        
        try:
            context.bot.send_message(
                user_id,
                "⚠️ Your verification status has been removed by admin.\n"
                "You'll need to get verified again to use bot features."
            )
        except:
            pass
            
    except Exception as e:
        debug_log(f"Error removing verification: {str(e)}")
        update.message.reply_text("❌ Failed to remove verification")

def check_verification_status(user_id):
    try:
        with db_connection('verified_users.db') as conn:
            return conn.execute('''SELECT 1 FROM verified_users 
                                 WHERE user_id=?''', (user_id,)).fetchone() is not None
    except Exception as e:
        debug_log(f"Verification check error: {str(e)}")
        return False

def verified_only(func):
    def wrapper(update: Update, context: CallbackContext):
        user = update.effective_user
        
        if user.id in ADMINS:
            return func(update, context)
            
        try:
            with db_connection('verified_users.db') as conn:
                c = conn.cursor()
                
                c.execute('''SELECT user_id FROM verified_users 
                            WHERE user_id=?''', 
                         (user.id,))
                is_verified = c.fetchone()
                
                if not is_verified:
                    update.message.reply_text(
                        "🔒 Verification Required\n\n"
                        "Please contact an admin to get verified first.\n"
                        "Use /start to request verification."
                    )
                    return
                    
                try:
                    c.execute('''UPDATE verified_users SET
                                last_active=CURRENT_TIMESTAMP,
                                username=?
                                WHERE user_id=?''',
                             (user.username or user.first_name, user.id))
                    conn.commit()
                except sqlite3.OperationalError as e:
                    debug_log(f"Optional columns not available: {str(e)}")
                    conn.rollback()
                
                return func(update, context)
                
        except Exception as e:
            debug_log(f"Verification check failed: {str(e)}")
            update.message.reply_text(
                "⚠️ Temporary verification error\n"
                "Admins have been notified. Please try again later."
            )
            return
    return wrapper

def cleanup_verification_requests():
    try:
        with db_connection('verified_users.db') as conn:
            conn.execute('''DELETE FROM verification_requests 
                           WHERE request_date < datetime('now', '-30 days')''')
            conn.commit()
            debug_log("Cleaned up old verification requests")
    except Exception as e:
        debug_log(f"Verification cleanup failed: {str(e)}")

def check_system_status(status_type):
    def decorator(func):
        def wrapper(update: Update, context: CallbackContext):
            with db_connection() as conn:
                status = conn.execute(f"SELECT {status_type} FROM system_status WHERE id=1").fetchone()[0]
                
                if not status:
                    update.message.reply_text(
                        f"❌ This feature is currently disabled by admin.\n"
                        f"Use /{'start' + status_type[:-5]} to enable."
                    )
                    return
                return func(update, context)
        return wrapper
    return decorator

@verified_only
@check_system_status("submissions_open")
def start_add(update: Update, context: CallbackContext):
    if update.message.chat.type != "private":
        update.message.reply_text("❌ Please DM me to add items!")
        return ConversationHandler.END
        
    context.user_data.clear()
    keyboard = [
        [InlineKeyboardButton("Legendary", callback_data="cat_legendary")],
        [InlineKeyboardButton("Non-Legendary", callback_data="cat_nonlegendary")],
        [InlineKeyboardButton("Shiny", callback_data="cat_shiny")],
        [InlineKeyboardButton("TMs", callback_data="cat_tms")]
    ]
    update.message.reply_text(
        "📝 Select category for your item:",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return SELECT_CATEGORY

def handle_category(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    category = query.data.split("_")[1]
    context.user_data['category'] = category
    
    if category == 'tms':
        query.edit_message_text(
            "📝 Please forward the TM details from @HexaMonBot\n"
            "(This should include all TM information)"
        )
        return GET_TM_DETAILS
    else:
        query.edit_message_text("🔤 Please enter the Pokémon's name:")
        return GET_POKEMON_NAME

def is_tm_message(message: Message) -> bool:
    if not message:
        return False
    text = message.text or message.caption or ""
    return any(indicator in text for indicator in ['💿', 'TM:', 'Technical Machine'])

def handle_tm_details(update: Update, context: CallbackContext):
    try:
        if not update.message or not update.message.forward_from:
            update.message.reply_text("❌ Please forward the original message from @HexaMonBot")
            return GET_TM_DETAILS
        
        if update.message.forward_from.username.lower() != "hexamonbot":
            update.message.reply_text("❌ Please forward directly from @HexaMonBot")
            return GET_TM_DETAILS

        tm_text = update.message.text or update.message.caption or ""
        if not tm_text.strip():
            update.message.reply_text("❌ No TM details found in the message")
            return GET_TM_DETAILS

        context.user_data['tm_details'] = {'text': tm_text}
        
        update.message.reply_text(
            "💰 Please enter the starting price for this TM\n"
            "Examples:\n"
            "- 0\n"
            "- 5000\n"
            "- 10k\n"
            "- Base: 5k",
            reply_markup=ForceReply(selective=True)
        )
        return GET_BASE_PRICE
        
    except Exception as e:
        debug_log(f"Error in handle_tm_details: {str(e)}")
        update.message.reply_text("❌ Failed to process TM details. Please try /add again.")
        return ConversationHandler.END

def handle_pokemon_name(update: Update, context: CallbackContext):
    pokemon_name = update.message.text.strip()
    if not pokemon_name or len(pokemon_name) > 30:
        update.message.reply_text("❌ Invalid name! Please enter a valid Pokémon name (max 30 chars)")
        return GET_POKEMON_NAME
        
    context.user_data['pokemon_name'] = pokemon_name
    context.user_data['seller_id'] = update.effective_user.id
    save_temp_data(update.effective_user.id, context.user_data)
    update.message.reply_text(
        f"🌿 Now forward {pokemon_name}'s Nature page from @HexaMonBot",
        parse_mode='Markdown'
    )
    return GET_NATURE

def handle_nature(update: Update, context: CallbackContext):
    if not is_forwarded_from_hexamon(update):
        update.message.reply_text("❌ Please forward directly from @HexaMonBot!")
        return GET_NATURE
    
    try:
        context.user_data['nature'] = {
            'photo': update.message.photo[-1].file_id,
            'text': update.message.caption or "Nature details not available"
        }
        save_temp_data(update.effective_user.id, context.user_data)
        update.message.reply_text("📊 Now forward IVs/EVs page from @HexaMonBot")
        return GET_IVS
    except Exception as e:
        debug_log(f"Nature handling failed: {str(e)}")
        update.message.reply_text("❌ Error saving nature data. Please restart with /add")
        return ConversationHandler.END

def handle_ivs(update: Update, context: CallbackContext):
    if not is_forwarded_from_hexamon(update):
        update.message.reply_text(
            "❌ Invalid IV/EV page!\n"
            "Please forward the original message directly from @HexaMonBot"
        )
        return GET_IVS
    
    if not update.message.photo:
        update.message.reply_text("❌ No IV/EV photo detected!")
        return GET_IVS
    
    context.user_data['ivs'] = {
        'photo': update.message.photo[-1].file_id,
        'text': update.message.caption or "No IV/EV details provided"
    }
    save_temp_data(update.effective_user.id, context.user_data)
    update.message.reply_text("⚔️ Now forward the Moveset page from @HexaMonBot")
    return GET_MOVESET

def handle_moveset(update: Update, context: CallbackContext):
    if not is_forwarded_from_hexamon(update):
        update.message.reply_text(
            "❌ Invalid moveset page!\n"
            "1. Open @HexaMonBot\n"
            "2. Find the moveset\n"
            "3. Forward it here"
        )
        return GET_MOVESET
    
    if not update.message.photo:
        update.message.reply_text("❌ Where's the moveset photo?")
        return GET_MOVESET
    
    context.user_data['moveset'] = {
        'photo': update.message.photo[-1].file_id,
        'text': update.message.caption or "No moveset details provided"
    }
    save_temp_data(update.effective_user.id, context.user_data)
    
    keyboard = [
        [InlineKeyboardButton("✅ Boosted", callback_data="boosted_yes")],
        [InlineKeyboardButton("❌ Unboosted", callback_data="boosted_no")]
    ]
    update.message.reply_text(
        "🔮 Is this Pokémon boosted?",
        reply_markup=InlineKeyboardMarkup(keyboard)
    )
    return GET_BOOSTED

def handle_boosted(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    context.user_data['boosted'] = query.data.split("_")[1]
    save_temp_data(query.from_user.id, context.user_data)
    query.edit_message_text("💰 Now enter the base price (e.g. 'Base: 5k'):")
    return GET_BASE_PRICE

def handle_base_price(update: Update, context: CallbackContext):
    if not context.user_data:
        update.message.reply_text("❌ Session expired. Please start over with /add")
        return ConversationHandler.END
        
    if context.user_data.get('category') == 'tms':
        return handle_tm_price(update, context)
    else:
        return handle_pokemon_price(update, context)

def handle_tm_price(update: Update, context: CallbackContext):
    try:
        base_price = extract_base_price(update.message.text)
        
        if base_price is None:
            update.message.reply_text("❌ Please enter a valid price (e.g., '0', '5000' or 'Base: 5k')")
            return GET_BASE_PRICE

        # Store final details
        context.user_data.update({
            'base_price': base_price,
            'seller_username': update.effective_user.username or update.effective_user.first_name
        })

        # Format TM auction text
        tm_text = context.user_data['tm_details']['text']
        caption = (
            f"🆕 New TM Auction\n\n"
            f"{escape_markdown_v2(tm_text)}\n\n"
            f"💰 Base Price: {base_price:,}\n"
            f"👤 Seller: @{escape_markdown_v2(context.user_data['seller_username'])}"
        )

        # Submit to admins (text only)
        submission_id = save_submission(update.effective_user.id, context.user_data)
        
        # Send to all admins
        for admin_id in ADMINS:
            try:
                # First send the TM details
                message = context.bot.send_message(
                    chat_id=admin_id,
                    text=caption,
                    parse_mode='MarkdownV2'
                )
                
                # Then send the approval buttons
                context.bot.send_message(
                    chat_id=admin_id,
                    text="Verify this TM?",
                    reply_markup=InlineKeyboardMarkup([
                        [
                            InlineKeyboardButton("✅ Approve", callback_data=f"verify_{submission_id}"),
                            InlineKeyboardButton("❌ Reject", callback_data=f"reject_{submission_id}")
                        ]
                    ])
                )
            except Exception as e:
                debug_log(f"Failed to alert admin {admin_id}: {str(e)}")

        update.message.reply_text("✅ TM submitted for approval!")
        cleanup_temp_data(update.effective_user.id)
        return ConversationHandler.END

    except Exception as e:
        debug_log(f"TM submission failed: {str(e)}")
        update.message.reply_text("❌ Submission error. Please try /add again.")
        return ConversationHandler.END

def handle_pokemon_price(update: Update, context: CallbackContext):
    try:
        # Get base price from message
        base_price = extract_base_price(update.message.text)
        
        if base_price is None:
            update.message.reply_text("❌ Please enter a valid price (e.g., '0', '5000' or 'Base: 5k')")
            return GET_BASE_PRICE
            
        # Get data from user_data (not context directly)
        user_data = context.user_data
        if not user_data:
            update.message.reply_text("❌ Session expired. Please start over with /add")
            return ConversationHandler.END

        # Verify all required Pokémon data exists
        required_fields = {
            'nature': "Nature page",
            'ivs': "IV/EV page",
            'moveset': "Moveset page",
            'pokemon_name': "Pokémon name"
        }
        
        missing = [name for field, name in required_fields.items() if field not in user_data]
        if missing:
            update.message.reply_text(
                f"❌ Missing data: {', '.join(missing)}\n"
                "Please restart with /add"
            )
            return ConversationHandler.END

        # Add final details to user_data
        user_data['base_price'] = base_price
        user_data['seller_username'] = update.effective_user.username

        # Format the caption
        caption = format_pokemon_auction_item(user_data)
        
        # Prepare media for admin approval
        media = [
            InputMediaPhoto(
                media=user_data['nature']['photo'], 
                caption=caption
            ),
            InputMediaPhoto(media=user_data['ivs']['photo']),
            InputMediaPhoto(media=user_data['moveset']['photo'])
        ]
        
        # Save submission
        submission_id = save_submission(
            update.effective_user.id,
            user_data  # Save the complete user_data
        )
        
        # Send to admins for approval
        for admin_id in ADMINS:
            try:
                # Send media group first
                context.bot.send_media_group(
                    chat_id=admin_id,
                    media=media
                )
                # Then send approval buttons
                context.bot.send_message(
                chat_id=admin_id,
                text="Verify this submission?",
                reply_markup=InlineKeyboardMarkup([
                    [  # First row of buttons
                        InlineKeyboardButton("✅ Approve", callback_data=f"verify_{submission_id}"),
                        InlineKeyboardButton("❌ Reject", callback_data=f"reject_{submission_id}")
                    ]  # End of first row
                ])  # End of keyboard markup
            )
                
            except Exception as e:
                debug_log(f"Failed to send to admin {admin_id}: {str(e)}")

        update.message.reply_text("✅ Submission sent to admins for verification!")
        cleanup_temp_data(update.effective_user.id)
        return ConversationHandler.END

    except Exception as e:
        debug_log(f"Error in handle_pokemon_price: {str(e)}")
        update.message.reply_text("❌ An error occurred. Please try again.")
        return ConversationHandler.END


def handle_verification(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    action, submission_id = query.data.split('_')
    submission_id = int(submission_id)

    submission = get_submission(submission_id)
    if not submission:
        query.edit_message_text("❌ Submission not found in database!")
        return

    if submission['status'] != 'pending':
        query.edit_message_text(f"⚠️ This submission was already {submission['status']}!")
        return

    try:
        submission_data = submission['data']
        if isinstance(submission_data, str):
            submission_data = json.loads(submission_data)

        # Update status first
        with db_connection() as conn:
            status = 'processing' if action == 'verify' else 'rejected'
            conn.execute("UPDATE submissions SET status=? WHERE submission_id=?", 
                        (status, submission_id))
            conn.commit()

        if action == 'verify':
            try:
                # Save to auctions table
                if submission_data.get('category') == 'tms':
                    item_text = format_tm_auction_item(submission_data)
                    auction_id = save_auction(
                        item_text=item_text,
                        photo_id=None,
                        base_price=submission_data['base_price']
                    )
                else:
                    item_text = format_pokemon_auction_item(submission_data)
                    auction_id = save_auction(
                        item_text=item_text,
                        photo_id=submission_data['nature']['photo'],
                        base_price=submission_data['base_price']
                    )

                if not auction_id:
                    raise Exception("Failed to save auction")

                # Post to channel
                if submission_data.get('category') == 'tms':
                    message = context.bot.send_message(
                        chat_id=CHANNEL_ID,
                        text=item_text,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("💰 Place Bid", callback_data=f"bid_{auction_id}")]
                        ]),
                        parse_mode='Markdown'
                    )
                else:
                    message = context.bot.send_photo(
                        chat_id=CHANNEL_ID,
                        photo=submission_data['nature']['photo'],
                        caption=item_text,
                        reply_markup=InlineKeyboardMarkup([
                            [InlineKeyboardButton("💰 Place Bid", callback_data=f"bid_{auction_id}")]
                        ]),
                        parse_mode='Markdown'
                    )

                # Update records
                with db_connection() as conn:
                    conn.execute('''UPDATE submissions SET 
                                  status='approved',
                                  channel_message_id=?
                                  WHERE submission_id=?''',
                               (message.message_id, submission_id))
                    conn.execute('''UPDATE auctions SET
                                   channel_message_id=?
                                   WHERE auction_id=?''',
                                (message.message_id, auction_id))
                    conn.commit()

                # Notify submitter
                context.bot.send_message(
                    chat_id=submission['user_id'],
                    text="🎉 Your item has been approved and listed!"
                )

            except Exception as e:
                debug_log(f"Error during auction creation: {str(e)}")
                with db_connection() as conn:
                    conn.execute("UPDATE submissions SET status='failed' WHERE submission_id=?", 
                               (submission_id,))
                    conn.commit()
                raise

        # Update admin messages
        admin_text = f"{'✅ Approved' if action == 'verify' else '❌ Rejected'} by admin"
        for admin_id in ADMINS:
            try:
                context.bot.edit_message_text(
                    chat_id=admin_id,
                    message_id=submission['channel_message_id'],
                    text=admin_text
                )
            except Exception as e:
                debug_log(f"Couldn't update admin {admin_id}: {str(e)}")
                context.bot.send_message(
                    admin_id,
                    f"Submission {submission_id} was {admin_text}"
                )

    except Exception as e:
        debug_log(f"Verification failed: {str(e)}")
        query.edit_message_text("❌ Processing failed. Check logs.")

@verified_only
@check_system_status("auctions_open")
def handle_bid_amount(update: Update, context: CallbackContext):
    if not update.message.reply_to_message or 'bid_context' not in context.user_data:
        update.message.reply_text(escape_markdown_v2("❌ No active bid session found."))
        return
    
    try:
        bid_amount = float(update.message.text.replace(',', '').strip())
        bid_context = context.user_data['bid_context']
        
        auction = get_auction(bid_context['auction_id'])
        if not auction or not auction['is_active']:
            update.message.reply_text(escape_markdown_v2("❌ This auction is no longer active."))
            return
            
        current_amount = auction.get('current_bid') or auction.get('base_price', 0)
        min_bid = current_amount + get_min_increment(current_amount)
        
        if bid_amount < min_bid:
            update.message.reply_text(
                escape_markdown_v2(
                    f"❌ Bid must be at least {min_bid:,}\n"
                    f"Current bid: {current_amount:,}\n"
                    f"Minimum increment: {get_min_increment(current_amount):,}"
                )
            )
            return
            
        # Record bid with properly escaped username
        bidder_name = f"@{update.effective_user.username}" if update.effective_user.username else update.effective_user.first_name
        prev_bidder = record_bid(
            bid_context['auction_id'],
            update.effective_user.id,
            bidder_name,
            bid_amount
        )
        
        # Get updated auction info
        updated_auction = get_auction(bid_context['auction_id'])
        if not updated_auction:
            update.message.reply_text(escape_markdown_v2("❌ Error updating auction."))
            return
            
        # Format message with bulletproof escaping
        caption = format_auction(updated_auction)
        
        # Create bid button
        keyboard = [[InlineKeyboardButton("💰 Place Bid", callback_data=f"bid_{updated_auction['auction_id']}")]]
        
        # Update channel message with error handling
        try:
            if updated_auction.get('photo_id'):
                context.bot.edit_message_caption(
                    chat_id=CHANNEL_ID,
                    message_id=bid_context['channel_msg_id'],
                    caption=caption,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='MarkdownV2'
                )
            else:
                context.bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=bid_context['channel_msg_id'],
                    text=caption,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode='MarkdownV2'
                )
        except Exception as e:
            debug_log(f"Channel update failed: {str(e)}")
            # Try one more time with plain text as fallback
            try:
                plain_caption = escape_markdown_v2(caption).replace('\\', '')
                if updated_auction.get('photo_id'):
                    context.bot.edit_message_caption(
                        chat_id=CHANNEL_ID,
                        message_id=bid_context['channel_msg_id'],
                        caption=plain_caption,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode=None
                    )
                else:
                    context.bot.edit_message_text(
                        chat_id=CHANNEL_ID,
                        message_id=bid_context['channel_msg_id'],
                        text=plain_caption,
                        reply_markup=InlineKeyboardMarkup(keyboard),
                        parse_mode=None
                    )
            except Exception as fallback_error:
                debug_log(f"Fallback update failed: {str(fallback_error)}")
        
        # Notify outbid user with proper escaping
        if prev_bidder and prev_bidder[0] != update.effective_user.id:
            try:
                send_outbid_notification(
                    context,
                    prev_bidder,
                    bid_context['item_text'],
                    bid_amount
                )
            except Exception as e:
                debug_log(f"Couldn't notify outbid user: {str(e)}")
        
        update.message.reply_text(escape_markdown_v2("✅ Your bid has been placed!"))
        
    except ValueError:
        update.message.reply_text(escape_markdown_v2("❌ Please enter a valid number"))
    except Exception as e:
        debug_log(f"Error in handle_bid_amount: {str(e)}")
        update.message.reply_text(escape_markdown_v2("❌ An error occurred. Your bid was recorded but the display may not update."))

def send_outbid_notification(context, prev_bidder, item_text, bid_amount):
    """Send outbid notification with guaranteed delivery"""
    try:
        # First try with properly escaped MarkdownV2
        try:
            safe_text = (
                "⚠️ You've been outbid on " + escape_markdown_v2(item_text) + "\\!\n" +
                "New bid: " + escape_markdown_v2(f"{bid_amount:,}")
            )
            context.bot.send_message(
                chat_id=prev_bidder[0],
                text=safe_text,
                parse_mode='MarkdownV2'
            )
            return
        except Exception as e:
            debug_log(f"MarkdownV2 notification failed, trying HTML: {str(e)}")

        # Fallback to HTML formatting
        try:
            html_text = (
                "<b>⚠️ You've been outbid on</b> " + 
                html.escape(item_text) + "!\n" +
                "<b>New bid:</b> " + html.escape(f"{bid_amount:,}")
            )
            context.bot.send_message(
                chat_id=prev_bidder[0],
                text=html_text,
                parse_mode='HTML'
            )
            return
        except Exception as e:
            debug_log(f"HTML notification failed, trying plain text: {str(e)}")

        # Final fallback to plain text
        plain_text = (
            "⚠️ You've been outbid on " + item_text + "!\n" +
            "New bid: " + f"{bid_amount:,}"
        )
        context.bot.send_message(
            chat_id=prev_bidder[0],
            text=plain_text,
            parse_mode=None
        )

    except Exception as e:
        debug_log(f"All notification attempts failed: {str(e)}")

def handle_bid_button(update: Update, context: CallbackContext):
    query = update.callback_query
    query.answer()
    
    try:
        # Try to find auction by message ID
        auction = get_auction_by_channel_id(query.message.message_id)
        
        if not auction:
            # Try alternative lookup if direct message ID fails
            if query.data and '_' in query.data:
                auction_id = int(query.data.split('_')[1])
                auction = get_auction(auction_id)
            
            if not auction:
                try:
                    query.edit_message_text("❌ Auction not found. It may have expired or been closed.")
                except:
                    query.message.reply_text("❌ Auction not found. It may have expired or been closed.")
                return

        # Create deep link URL
        bot_username = context.bot.username
        deep_link = f"https://t.me/{bot_username}?start=bid_{auction['auction_id']}"
        
        # Create button with deep link
        keyboard = [[
            InlineKeyboardButton(
                "💰 Place Bid (Click Here)", 
                url=deep_link
            )
        ]]

        # Store bid context in case direct message is used
        context.user_data['bid_context'] = {
            'auction_id': auction['auction_id'],
            'channel_msg_id': query.message.message_id,
            'min_bid': (auction.get('current_bid') or auction.get('base_price', 0)) + get_min_increment(auction.get('current_bid')),
            'current_bidder': auction.get('current_bidder'),
            'item_text': auction['item_text']
        }

        try:
            # Try to update the existing message's buttons
            context.bot.edit_message_reply_markup(
                chat_id=query.message.chat.id,
                message_id=query.message.message_id,
                reply_markup=InlineKeyboardMarkup(keyboard)
            )
        except Exception as e:
            debug_log(f"Couldn't update buttons, sending new message: {str(e)}")
            context.bot.send_message(
                chat_id=query.message.chat.id,
                text=f"💰 Place your bid for Auction #{auction['auction_id']} by clicking below:",
                reply_markup=InlineKeyboardMarkup(keyboard),
                reply_to_message_id=query.message.message_id
            )
            
    except Exception as e:
        debug_log(f"Error in handle_bid_button: {str(e)}")
        try:
            query.message.reply_text("❌ Error processing bid request. Please try again.")
        except Exception as e2:
            debug_log(f"Couldn't send error message: {str(e2)}")

def get_active_auctions_by_category():
    try:
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''SELECT a.*, s.data 
                         FROM auctions a
                         LEFT JOIN submissions s ON a.channel_message_id = s.channel_message_id
                         WHERE a.is_active=1 
                         ORDER BY a.created_at DESC''')
            auctions = c.fetchall()
            
            categorized = {
                'nonlegendary': [],
                'shiny': [],
                'legendary': [],
                'tms': []
            }
            
            for auction in auctions:
                submission_data = json.loads(auction['data']) if auction['data'] else {}
                category = submission_data.get('category', 'nonlegendary')
                
                if category == 'legendary':
                    categorized['legendary'].append(auction)
                elif category == 'shiny':
                    categorized['shiny'].append(auction)
                elif category == 'tms':
                    categorized['tms'].append(auction)
                else:
                    categorized['nonlegendary'].append(auction)
                    
            return categorized
            
    except Exception as e:
        debug_log(f"Error getting active auctions: {str(e)}")
        return None

@verified_only
def handle_items(update: Update, context: CallbackContext):
    try:
        categorized = get_active_auctions_by_category()
        if not categorized:
            update.message.reply_text("ℹ️ No active auctions currently.")
            return
            
        response = ["🏆 Active Auctions 🏆\n"]
        
        def format_item(auction):
            item_text = auction['item_text']
            auction_id = auction['auction_id']
            
            current_bid = auction['current_bid'] if auction['current_bid'] is not None else auction['base_price']
            bid_text = f"{int(current_bid):,}" if current_bid is not None else "No bids"
            
            submission_data = json.loads(auction['data']) if auction['data'] else {}
            if submission_data.get('category') == 'tms':
                name = "TM " + (submission_data.get('tm_details', {}).get('text', 'Unknown').split('\n')[0])
            else:
                name = submission_data.get('pokemon_name', 'Unknown Pokémon')
            
            return f"{name} (Auction #{auction_id}) - Current: {bid_text}"

        if categorized['legendary']:
            response.append("\n🌟 Legendary Pokémon:")
            for i, auction in enumerate(categorized['legendary'], 1):
                response.append(f"  {i}. {format_item(auction)}")
            response.append("")

        if categorized['shiny']:
            response.append("\n✨ Shiny Pokémon:")
            for i, auction in enumerate(categorized['shiny'], 1):
                response.append(f"  {i}. {format_item(auction)}")
            response.append("")

        if categorized['nonlegendary']:
            response.append("\n🔹 Non-Legendary Pokémon:")
            for i, auction in enumerate(categorized['nonlegendary'], 1):
                response.append(f"  {i}. {format_item(auction)}")
            response.append("")

        if categorized['tms']:
            response.append("\n💿 Technical Machines:")
            for i, auction in enumerate(categorized['tms'], 1):
                response.append(f"  {i}. {format_item(auction)}")
            response.append("")

        if any(categorized.values()):
            response.append("💡 Join the channel @legend_auc")
        
        update.message.reply_text("\n".join(response))
        
    except Exception as e:
        debug_log(f"Error in /items: {str(e)}")
        update.message.reply_text("❌ Error fetching active items. Please try again.")

def get_user_approved_items(user_id):
    try:
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''SELECT * FROM submissions 
                         WHERE user_id=? AND status='approved'
                         ORDER BY created_at DESC''', (user_id,))
            return c.fetchall()
    except Exception as e:
        debug_log(f"Error getting user items: {str(e)}")
        return []

@verified_only
def handle_myitems(update: Update, context: CallbackContext):
    try:
        user_id = update.effective_user.id
        items = get_user_approved_items(user_id)
        
        if not items:
            update.message.reply_text("📭 You don't have any approved items in auctions yet.")
            return
            
        response = [
            f"📋 Your Approved Auction Items ({len(items)} total)",
            "--------------------------------"
        ]
        
        for i, item in enumerate(items, 1):
            try:
                data = json.loads(item['data'])
                category = data.get('category', 'unknown').title()
                
                if category.lower() == 'tms':
                    name = data.get('tm_details', {}).get('text', 'Unknown TM').split('\n')[0]
                else:
                    name = data.get('pokemon_name', 'Unknown Pokémon')
                
                auction_id = "Not listed yet"
                if item['channel_message_id']:
                    auction = get_auction_by_channel_id(item['channel_message_id'])
                    if auction:
                        auction_id = auction['auction_id']
                
                response.append(
                    f"\n{i}. [{category}] {name}\n"
                    f"   Auction ID: {auction_id}\n"
                    f"   Submitted: {item['created_at']}"
                )
                
            except Exception as e:
                debug_log(f"Error formatting item {i}: {str(e)}")
                continue
                
        response.append("\nℹ️ Items without Auction ID haven't been posted yet")
        
        update.message.reply_text("\n".join(response))
        
    except Exception as e:
        debug_log(f"Error in /myitems: {str(e)}")
        update.message.reply_text("❌ Error fetching your items. Please try again.")

def get_bid_history(auction_id):
    try:
        with db_connection() as conn:
            c = conn.cursor()
            c.execute('''SELECT bid_id, bidder_name, amount, timestamp 
                         FROM bids 
                         WHERE auction_id=? AND is_active=1
                         ORDER BY amount DESC''', (auction_id,))
            return c.fetchall()
    except Exception as e:
        debug_log(f"Error getting bid history: {str(e)}")
        return []

def show_bid_history(update: Update, context: CallbackContext):
    try:
        if not context.args:
            update.message.reply_text("❌ Usage: /history <auction_id>")
            return
            
        auction_id = int(context.args[0])
        history = get_bid_history(auction_id)
        
        if not history:
            update.message.reply_text(f"No bid history found for Auction #{auction_id}")
            return
            
        response = [f"📊 Bid History for Auction #{auction_id}"]
        for bid in history:
            bid_id, bidder, amount, time = bid
            response.append(f"🏷️ Bid #{bid_id}: {bidder} - {amount:,} at {time}")
            
        update.message.reply_text("\n".join(response))
        
    except Exception as e:
        debug_log(f"Error in show_bid_history: {str(e)}")
        update.message.reply_text("❌ Error fetching bid history")

def remove_last_bid(auction_id):
    try:
        with db_connection() as conn:
            c = conn.cursor()
            
            c.execute('''SELECT bid_id, bidder_name, amount 
                         FROM bids 
                         WHERE auction_id=? AND is_active=1
                         ORDER BY timestamp DESC, bid_id DESC 
                         LIMIT 1''', (auction_id,))
            last_bid = c.fetchone()
            
            if not last_bid:
                return None
                
            bid_id, bidder_name, amount = last_bid
            
            c.execute('''UPDATE bids SET is_active=0 WHERE bid_id=?''', (bid_id,))
            
            c.execute('''SELECT bidder_name, amount FROM bids
                         WHERE auction_id=? AND is_active=1
                         ORDER BY amount DESC 
                         LIMIT 1''', (auction_id,))
            new_top = c.fetchone()
            
            if new_top:
                new_bidder, new_amount = new_top
                c.execute('''UPDATE auctions SET
                             current_bid=?,
                             current_bidder=?,
                             previous_bidder=?
                             WHERE auction_id=?''',
                          (new_amount, new_bidder, bidder_name, auction_id))
            else:
                c.execute('''UPDATE auctions SET
                             current_bid=NULL,
                             current_bidder=NULL,
                             previous_bidder=?
                             WHERE auction_id=?''',
                          (bidder_name, auction_id))
                new_top = (None, None)
            
            conn.commit()
            return new_top
            
    except Exception as e:
        debug_log(f"Error in remove_last_bid: {str(e)}")
        return None

def handle_remove_bid(update: Update, context: CallbackContext):
    try:
        if update.effective_user.id not in ADMINS:
            update.message.reply_text("❌ Admin only command!")
            return
            
        if not context.args:
            update.message.reply_text("❌ Usage: /removebid <auction_id>")
            return
            
        auction_id = int(context.args[0])
        auction = get_auction(auction_id)
        
        if not auction:
            update.message.reply_text(f"❌ Auction #{auction_id} not found!")
            return
            
        result = remove_last_bid(auction_id)
        
        if not result:
            update.message.reply_text(f"❌ No active bids to remove for Auction #{auction_id}")
            return
            
        new_bidder, new_amount = result
        new_amount = new_amount if new_amount else auction['base_price']
        
        caption = format_auction({
            'auction_id': auction_id,
            'item_text': auction['item_text'],
            'base_price': auction['base_price'],
            'current_bid': new_amount,
            'current_bidder': new_bidder
        })
        
        keyboard = [[
            InlineKeyboardButton(
                "💰 Place Bid", 
                callback_data=f"bid_{auction_id}"
            )
        ]]
        
        update_success = True
        try:
            if auction.get('photo_id'):
                context.bot.edit_message_caption(
                    chat_id=CHANNEL_ID,
                    message_id=auction['channel_message_id'],
                    caption=caption,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
            else:
                context.bot.edit_message_text(
                    chat_id=CHANNEL_ID,
                    message_id=auction['channel_message_id'],
                    text=caption,
                    reply_markup=InlineKeyboardMarkup(keyboard)
                )
        except Exception as e:
            debug_log(f"Channel update failed: {str(e)}")
            update_success = False
            
        response = [
            f"✅ Last bid removed from Auction #{auction_id}",
            f"New top bid: {new_bidder or 'None'} with {new_amount:,}"
        ]
        
        if not update_success:
            response.append("\n⚠️ Note: Couldn't update auction message")
            
        update.message.reply_text("\n".join(response))
        
    except Exception as e:
        debug_log(f"Error in handle_remove_bid: {str(e)}")
        update.message.reply_text("❌ Error removing bid. Please check the auction ID.")

def get_user_leading_bids(user_id):
    try:
        with db_connection() as conn:
            c = conn.cursor()
            
            c.execute('''SELECT a.auction_id, a.item_text, b.amount 
                         FROM auctions a
                         JOIN bids b ON a.auction_id = b.auction_id
                         WHERE b.bidder_id = ? 
                         AND b.is_active = 1
                         AND a.is_active = 1
                         AND b.amount = (
                             SELECT MAX(amount) 
                             FROM bids 
                             WHERE auction_id = a.auction_id 
                             AND is_active = 1
                         )
                         ORDER BY b.timestamp DESC''', (user_id,))
            return c.fetchall()
    except Exception as e:
        debug_log(f"Error getting user leading bids: {str(e)}")
        return []

@verified_only
def handle_mybids(update: Update, context: CallbackContext):
    try:
        user_bids = get_user_leading_bids(update.effective_user.id)
        
        if not user_bids:
            update.message.reply_text("You're not currently the highest bidder on any active auctions.")
            return
            
        response = ["🏆 Your Current Winning Bids:"]
        
        for auction_id, item_text, amount in user_bids:
            if "tm auction" in item_text.lower() or "technical machine" in item_text.lower():
                tm_match = re.search(r"(TM:|Technical Machine:|💿)\s*(.*?)\n", item_text, re.IGNORECASE)
                item_name = f"TM {tm_match.group(2).strip()}" if tm_match else "the TM"
            else:
                pokemon_match = re.search(r"pokémon:\s*(.*?)\n", item_text, re.IGNORECASE)
                item_name = pokemon_match.group(1).strip() if pokemon_match else "the Pokémon"
            
            response.append(
                f"\n🔹 Auction #{auction_id}: {item_name}\n"
                f"   Your Bid: {amount:,}"
            )
            
        update.message.reply_text("\n".join(response))
        
    except Exception as e:
        debug_log(f"Error in /mybids: {str(e)}")
        update.message.reply_text("❌ Error fetching your current bids")

def handle_cleanup(update: Update, context: CallbackContext):
    if update.effective_user.id not in ADMINS:
        return
    
    try:
        with db_connection() as conn:
            conn.execute('''DELETE FROM submissions 
                           WHERE status='rejected' 
                           AND created_at < datetime('now', '-30 days')''')
            conn.commit()
        update.message.reply_text("✅ Database cleanup completed")
    except Exception as e:
        debug_log(f"Cleanup failed: {str(e)}")
        update.message.reply_text("❌ Cleanup failed")

def cancel_post_item(update: Update, context: CallbackContext):
    context.user_data.clear()
    update.message.reply_text(
        "🗑 Posting cancelled.\n"
        "You can start over with /add"
    )
    return ConversationHandler.END

def error_handler(update: Update, context: CallbackContext):
    error = context.error
    debug_log(f"Error: {str(error)}\nUpdate: {update}\nContext: {context}")
    
    if update and update.effective_message:
        if isinstance(error, sqlite3.Error):
            update.effective_message.reply_text("❌ Database error. Please try again later.")
        elif isinstance(error, telegram.error.NetworkError):
            update.effective_message.reply_text("⚠️ Network issue. Please try again.")
        else:
            update.effective_message.reply_text("❌ An unexpected error occurred. The admin has been notified.")
        
        if not isinstance(error, telegram.error.BadRequest):
            for admin_id in ADMINS:
                try:
                    context.bot.send_message(
                        admin_id,
                        f"⚠️ Bot Error:\n{str(error)}\n\nUpdate: {update}"
                    )
                except:
                    pass

def main():
    if not ensure_single_instance():
        sys.exit(1)
        
    try:
        init_db()
        init_verified_users_db()
        
        updater = Updater(TOKEN, use_context=True)
        dp = updater.dispatcher

        set_bot_commands(updater)
        
        try:
            bot = updater.bot
            chat = bot.get_chat(CHANNEL_ID)
            debug_log(f"Bot connected to channel: {chat.title}")
        except Exception as e:
            debug_log(f"FATAL: Channel access failed - {str(e)}")
            raise RuntimeError(f"Could not access channel {CHANNEL_ID}. Verify bot is admin.")
        
        dp.add_error_handler(error_handler)
        dp.add_handler(CommandHandler("start", start))
        dp.add_handler(CommandHandler("history", show_bid_history))
        dp.add_handler(CommandHandler("removebid", handle_remove_bid))
        dp.add_handler(CommandHandler("items", handle_items))
        dp.add_handler(CommandHandler("myitems", handle_myitems))
        dp.add_handler(CommandHandler("mybids", handle_mybids))
        dp.add_handler(CommandHandler("endsubmission", end_submission))
        dp.add_handler(CommandHandler("startsubmission", start_submission))
        dp.add_handler(CommandHandler("startauction", start_auction))
        dp.add_handler(CommandHandler("endauction", end_auction))
        dp.add_handler(CommandHandler("verify_me", request_verification))
        dp.add_handler(CommandHandler("verify", verify_user))
        dp.add_handler(CommandHandler("unverify", remove_verification))
        dp.add_handler(CommandHandler("listverified", list_verified_users))
        dp.add_handler(CommandHandler("help", show_help)),
        dp.add_handler(CommandHandler("cleanup", handle_cleanup))
        
        dp.add_handler(
            ConversationHandler(
                entry_points=[CommandHandler('add', start_add)],
                states={
                    SELECT_CATEGORY: [CallbackQueryHandler(handle_category)],
                    GET_POKEMON_NAME: [MessageHandler(Filters.text & ~Filters.command, handle_pokemon_name)],
                    GET_NATURE: [MessageHandler(Filters.photo & Filters.forwarded, handle_nature)],
                    GET_IVS: [MessageHandler(Filters.photo & Filters.forwarded, handle_ivs)],
                    GET_MOVESET: [MessageHandler(Filters.photo & Filters.forwarded, handle_moveset)],
                    GET_BOOSTED: [CallbackQueryHandler(handle_boosted)],
                    GET_TM_DETAILS: [MessageHandler(Filters.all & Filters.forwarded, handle_tm_details)],
                    GET_BASE_PRICE: [
                        MessageHandler(
                            Filters.text & ~Filters.command & 
                            Filters.regex(r'(?i)^(base:)?\s*(\d+k?|\d{1,3}(,\d{3})*)$'),
                            handle_base_price  # New unified handler
                )
            ]
        },
                fallbacks=[CommandHandler('cancel', cancel_post_item)],
                allow_reentry=True
    )
)

        
        dp.add_handler(CallbackQueryHandler(handle_verification, pattern='^(verify|reject)_'))
        dp.add_handler(CallbackQueryHandler(handle_bid_button, pattern='^bid_'))
        dp.add_handler(MessageHandler(Filters.reply & Filters.text & Filters.private, handle_bid_amount))
        
        debug_log("Bot starting with all features...")
        updater.start_polling()
        updater.idle()
        
    except Conflict:
        print("Error: Another instance is already polling updates")
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}")
        sys.exit(1)


if __name__ == '__main__':
    main()    # Start your bot