import os
import logging
from dotenv import load_dotenv
from telegram.ext import Updater, CommandHandler, ConversationHandler, MessageHandler, Filters, CallbackQueryHandler
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
import requests
import mysql.connector
from pusher import Pusher
import threading
import re
from datetime import datetime, timedelta

# Set up logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)
logger = logging.getLogger(__name__)

# Load environment variables
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
if not TELEGRAM_TOKEN:
    raise ValueError("TELEGRAM_TOKEN not found in .env file")
COPPERX_API_TOKEN = os.getenv("COPPERX_API_TOKEN")
if not COPPERX_API_TOKEN:
    raise ValueError("COPPERX_API_TOKEN not found in .env file")
PUSHER_KEY = os.getenv("PUSHER_KEY")
PUSHER_CLUSTER = os.getenv("PUSHER_CLUSTER")
PUSHER_APP_ID = os.getenv("PUSHER_APP_ID")
PUSHER_SECRET = os.getenv("PUSHER_SECRET")
BASE_URL = "https://income-api.copperx.io/api"

# MySQL connection
db_config = {
    "host": os.getenv("MYSQL_HOST"),
    "user": os.getenv("MYSQL_USER"),
    "password": os.getenv("MYSQL_PASSWORD"),
    "database": os.getenv("MYSQL_DB")
}

# Conversation states
EMAIL, OTP, SEND_TYPE, SEND_RECIPIENT, SEND_AMOUNT, SEND_CONFIRM, WITHDRAW_AMOUNT, WITHDRAW_CONFIRM = range(8)

# Database helper functions
def get_db_connection():
    try:
        return mysql.connector.connect(**db_config)
    except mysql.connector.Error as e:
        logger.error(f"Database connection error: {e}")
        raise

def init_db():
    """
    Initialize the database by creating the 'users' table if it doesn't exist.
    """
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("SHOW TABLES LIKE 'users'")
        table_exists = cursor.fetchone()
        if not table_exists:
            cursor.execute("""
                CREATE TABLE users (
                    chat_id BIGINT PRIMARY KEY,
                    email VARCHAR(255),
                    token TEXT,
                    organization_id VARCHAR(255),
                    token_expiry VARCHAR(50),
                    default_wallet VARCHAR(255)
                )
            """)
            conn.commit()
            logger.info("Created 'users' table in the database.")
        else:
            logger.info("'users' table already exists in the database.")
    except mysql.connector.Error as e:
        logger.error(f"Error initializing database: {e}")
        raise
    finally:
        cursor.close()
        conn.close()

def save_user(chat_id, email, token, organization_id=None, token_expiry=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute(
            "REPLACE INTO users (chat_id, email, token, organization_id, token_expiry) VALUES (%s, %s, %s, %s, %s)",
            (chat_id, email, token, organization_id, token_expiry)
        )
        conn.commit()
    except mysql.connector.Error as e:
        logger.error(f"Error saving user: {e}")
        raise
    finally:
        cursor.close()
        conn.close()

def get_user(chat_id):
    conn = get_db_connection()
    cursor = conn.cursor(dictionary=True)
    try:
        cursor.execute("SELECT * FROM users WHERE chat_id = %s", (chat_id,))
        user = cursor.fetchone()
        return user
    except mysql.connector.Error as e:
        logger.error(f"Error fetching user: {e}")
        raise
    finally:
        cursor.close()
        conn.close()

def update_default_wallet(chat_id, wallet_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute("UPDATE users SET default_wallet = %s WHERE chat_id = %s", (wallet_id, chat_id))
        conn.commit()
    except mysql.connector.Error as e:
        logger.error(f"Error updating default wallet: {e}")
        raise
    finally:
        cursor.close()
        conn.close()

# Token refresh (basic implementation)
def refresh_token_if_needed(user, chat_id, reply_func):
    if not user:
        logger.warning(f"No user found for chat_id {chat_id}")
        reply_func("⚠️ Please /login to continue.")
        return None
    if not user.get("token_expiry"):
        logger.warning(f"No token expiry found for user {chat_id}")
        reply_func("⚠️ Please /login to continue.")
        return None
    try:
        expiry = datetime.strptime(user["token_expiry"], "%Y-%m-%d %H:%M:%S")
        if datetime.now() >= expiry:
            logger.info(f"Token expired for user {chat_id}, expiry: {user['token_expiry']}")
            reply_func("⚠️ Your session has expired. Please /login again to continue.")
            return None
        logger.info(f"Token is valid for user {chat_id}, expiry: {user['token_expiry']}")
        return user
    except ValueError as e:
        logger.error(f"Error parsing token expiry for user {chat_id}: {e}")
        reply_func("⚠️ Session error. Please /login again.")
        return None

# Command menu as inline keyboard
def get_command_menu():
    keyboard = [
        [
            InlineKeyboardButton("Login", callback_data="cmd_login"),
            InlineKeyboardButton("Profile", callback_data="cmd_profile"),
            InlineKeyboardButton("KYC", callback_data="cmd_kyc")
        ],
        [
            InlineKeyboardButton("Balance", callback_data="cmd_balance"),
            InlineKeyboardButton("Set Default", callback_data="cmd_setdefault"),
            InlineKeyboardButton("Deposit", callback_data="cmd_deposit")
        ],
        [
            InlineKeyboardButton("History", callback_data="cmd_history"),
            InlineKeyboardButton("Send", callback_data="cmd_send"),
            InlineKeyboardButton("Withdraw", callback_data="cmd_withdraw")
        ],
        [
            InlineKeyboardButton("Help", callback_data="cmd_help")
        ]
    ]
    return InlineKeyboardMarkup(keyboard)

# Start command
def start(update, context):
    try:
        chat_id = update.message.chat_id
        user = get_user(chat_id)
        user_name = update.message.from_user.first_name
        if user:
            update.message.reply_text(
                f"👋 *Welcome back, {user_name}!* 🌟\n"
                f"You’re logged in as {user['email']}. Use the menu below to manage your USDC transactions:",
                parse_mode="Markdown",
                reply_markup=get_command_menu()
            )
        else:
            update.message.reply_text(
                f"🌟 *Welcome to the Copperx Payout Bot, {user_name}!* 🌟\n"
                "Easily manage your USDC transactions directly in Telegram. To begin, please /login with your Copperx credentials or use /help to explore all available commands.",
                parse_mode="Markdown",
                reply_markup=get_command_menu()
            )
    except Exception as e:
        logger.error(f"Error in start command: {e}")
        update.message.reply_text(
            "❌ *An error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )

# Help command
def help_command(update, context):
    try:
        chat_id = update.message.chat_id if update.message else update.callback_query.message.chat_id
        reply_func = update.message.reply_text if update.message else update.callback_query.message.reply_text
        reply_func(
            "📋 *Copperx Payout Bot Commands:*\n\n"
            "🔐 *Account Management*\n"
            "/start - Start the bot\n"
            "/login - Log in to Copperx\n"
            "/logout - Log out of Copperx\n"
            "/profile - View your account details\n"
            "/kyc - Check your KYC/KYB status\n\n"
            "👛 *Wallet Management*\n"
            "/balance - Check your wallet balances\n"
            "/setdefault - Set your default wallet\n"
            "/deposit - Get instructions to deposit USDC\n"
            "/history - View your last 10 transactions\n\n"
            "💸 *Fund Transfers*\n"
            "/send - Send USDC to an email or wallet\n"
            "/withdraw - Withdraw USDC to your bank\n\n"
            "/help - Show this message\n\n"
            "📞 *Support:* https://t.me/copperxcommunity/2183",
            parse_mode="Markdown",
            reply_markup=get_command_menu()
        )
    except Exception as e:
        logger.error(f"Error in help command: {e}")
        reply_func(
            "❌ *An error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )

# Command menu callback
def menu_callback(update, context):
    try:
        query = update.callback_query
        command = query.data.split("_")[1]
        query.answer()
        chat_id = query.message.chat_id
        context.bot.send_message(chat_id=chat_id, text=f"/{command}")
    except Exception as e:
        logger.error(f"Error in menu_callback: {e}")
        query.message.reply_text(
            "❌ *An error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )

# Logout command
def logout(update, context):
    try:
        chat_id = update.message.chat_id
        reply_func = update.message.reply_text
        conn = get_db_connection()
        cursor = conn.cursor()
        cursor.execute("DELETE FROM users WHERE chat_id = %s", (chat_id,))
        conn.commit()
        cursor.close()
        conn.close()
        reply_func(
            "👋 *Logged out successfully!*\n"
            "You’ve been logged out of Copperx. Use /login to sign in again.\n\n"
            "Use the menu below to continue:",
            parse_mode="Markdown",
            reply_markup=get_command_menu()
        )
    except mysql.connector.Error as e:
        logger.error(f"Error in logout for user {chat_id}: {e}")
        reply_func(
            "❌ *Error logging out.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Unexpected error in logout for user {chat_id}: {e}")
        reply_func(
            "❌ *An unexpected error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )

# Authentication
def login(update, context):
    try:
        update.message.reply_text(
            "📧 *Let’s get you logged in!*\n"
            "Please enter your Copperx email address to receive an OTP:",
            parse_mode="Markdown"
        )
        return EMAIL
    except Exception as e:
        logger.error(f"Error in login command: {e}")
        update.message.reply_text(
            "❌ *An error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

def get_email(update, context):
    try:
        email = update.message.text
        if not re.match(r"[^@]+@[^@]+\.[^@]+", email):
            update.message.reply_text(
                "❌ *Invalid email format.* Please enter a valid email address:",
                parse_mode="Markdown"
            )
            return EMAIL
        context.user_data["email"] = email
        headers = {"Authorization": f"Bearer {COPPERX_API_TOKEN}"}
        logger.info(f"Sending OTP request for email: {email}")
        response = requests.post(f"{BASE_URL}/auth/email-otp/request", json={"email": email}, headers=headers)
        logger.info(f"API response status: {response.status_code}, response: {response.text}")
        if response.status_code == 200:
            response_data = response.json()
            sid = response_data.get("sid")
            if not sid:
                update.message.reply_text(
                    "❌ *Error:* No session ID received from Copperx. Please try again or contact support: https://t.me/copperxcommunity/2183",
                    parse_mode="Markdown"
                )
                return ConversationHandler.END
            context.user_data["sid"] = sid
            update.message.reply_text(
                "🔑 *OTP sent!* Please check your email (including spam/junk folder) and enter the 6-digit OTP here:",
                parse_mode="Markdown"
            )
            return OTP
        elif response.status_code == 429:
            update.message.reply_text(
                "⚠️ *Rate limit exceeded.* Please wait a few minutes and try again.",
                parse_mode="Markdown"
            )
            return ConversationHandler.END
        elif response.status_code == 404:
            update.message.reply_text(
                "❌ *Email not found.* Please ensure you’re using the email associated with your Copperx account, or sign up at https://copperx.io.",
                parse_mode="Markdown"
            )
            return ConversationHandler.END
        else:
            update.message.reply_text(
                f"❌ *Error sending OTP:* {response.json().get('message', 'Unknown error')}\n"
                "Please try again or contact support: https://t.me/copperxcommunity/2183",
                parse_mode="Markdown"
            )
            return ConversationHandler.END
    except requests.RequestException as e:
        logger.error(f"Network error in get_email: {e}")
        update.message.reply_text(
            f"❌ *Network error:* {str(e)}\n"
            "Please check your internet connection and try again.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in get_email: {e}")
        update.message.reply_text(
            "❌ *An error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

def verify_otp(update, context):
    try:
        otp = update.message.text
        if not otp.isdigit() or len(otp) != 6:
            update.message.reply_text(
                "❌ *Invalid OTP.* It must be a 6-digit number. Please try again:",
                parse_mode="Markdown"
            )
            return OTP
        email = context.user_data.get("email")
        sid = context.user_data.get("sid")
        if not email or not sid:
            update.message.reply_text(
                "❌ *Session error.* Please start the login process again with /login.",
                parse_mode="Markdown"
            )
            return ConversationHandler.END
        chat_id = update.message.chat_id
        headers = {"Authorization": f"Bearer {COPPERX_API_TOKEN}"}
        logger.info(f"Verifying OTP for email: {email}, OTP: {otp}, sid: {sid}")
        response = requests.post(
            f"{BASE_URL}/auth/email-otp/authenticate",
            json={"email": email, "otp": otp, "sid": sid},
            headers=headers
        )
        logger.info(f"API response status: {response.status_code}, response: {response.text}")
        if response.status_code == 200:
            token = response.json().get("accessToken")
            profile = requests.get(f"{BASE_URL}/auth/me", headers={"Authorization": f"Bearer {token}"}).json()
            org_id = profile.get("organizationId")
            token_expiry = (datetime.now() + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
            save_user(chat_id, email, token, org_id, token_expiry)
            update.message.reply_text(
                "✅ *Login successful!* You’re now connected to Copperx.\n"
                "Use the menu below to manage your USDC transactions:",
                parse_mode="Markdown",
                reply_markup=get_command_menu()
            )
            start_pusher(chat_id, token, org_id, context)
            return ConversationHandler.END
        else:
            update.message.reply_text(
                f"❌ *Invalid OTP:* {response.json().get('message', 'Unknown error')}\n"
                "Please try again or request a new OTP with /login.",
                parse_mode="Markdown"
            )
            return OTP
    except requests.RequestException as e:
        logger.error(f"Network error in verify_otp: {e}")
        update.message.reply_text(
            f"❌ *Network error:* {str(e)}\n"
            "Please check your connection and try again.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in verify_otp: {e}")
        update.message.reply_text(
            "❌ *An error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

# Profile and KYC
def profile(update, context):
    try:
        chat_id = update.message.chat_id
        reply_func = update.message.reply_text
        user = get_user(chat_id)
        user = refresh_token_if_needed(user, chat_id, reply_func)
        if not user:
            return
        headers = {"Authorization": f"Bearer {user['token']}"}
        response = requests.get(f"{BASE_URL}/auth/me", headers=headers)
        if response.status_code == 200:
            try:
                data = response.json()
            except ValueError as e:
                logger.error(f"Error parsing JSON response in profile: {e}, response: {response.text}")
                reply_func(
                    "❌ *Error:* Invalid response from Copperx. Please try again or contact support: https://t.me/copperxcommunity/2183",
                    parse_mode="Markdown"
                )
                return
            reply_func(
                f"👤 *Your Copperx Profile:*\n\n"
                f"📧 *Email:* {data['email']}\n"
                f"🏢 *Organization ID:* {data['organizationId']}\n"
                f"👛 *Wallet Address:* {data['walletAddress']}\n"
                f"🔐 *Wallet Type:* {data['walletAccountType']}\n\n"
                "Use the menu below to continue:",
                parse_mode="Markdown",
                reply_markup=get_command_menu()
            )
        else:
            try:
                error_msg = response.json().get('message', 'Unknown error')
            except ValueError:
                error_msg = "Invalid response from Copperx"
            logger.error(f"Error fetching profile for user {chat_id}: {response.status_code}, {error_msg}")
            reply_func(
                f"❌ *Error fetching profile:* {error_msg}\n"
                "Please try again or contact support: https://t.me/copperxcommunity/2183",
                parse_mode="Markdown"
            )
    except requests.RequestException as e:
        logger.error(f"Network error in profile for user {chat_id}: {e}")
        reply_func(
            f"❌ *Network error:* {str(e)}\n"
            "Please check your connection and try again.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Unexpected error in profile for user {chat_id}: {e}")
        reply_func(
            "❌ *An unexpected error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )

def kyc(update, context):
    try:
        chat_id = update.message.chat_id
        reply_func = update.message.reply_text
        user = get_user(chat_id)
        user = refresh_token_if_needed(user, chat_id, reply_func)
        if not user:
            return
        headers = {"Authorization": f"Bearer {user['token']}"}
        response = requests.get(f"{BASE_URL}/kycs", headers=headers)
        if response.status_code == 200:
            try:
                kycs = response.json()
            except ValueError as e:
                logger.error(f"Error parsing JSON response in kyc for user {chat_id}: {e}, response: {response.text}")
                reply_func(
                    "❌ *Error:* Invalid response from Copperx. Please try again or contact support: https://t.me/copperxcommunity/2183",
                    parse_mode="Markdown"
                )
                return
            if kycs and isinstance(kycs, list) and kycs[0].get("status") == "approved":
                reply_func(
                    "✅ *KYC/KYB Approved!*\n"
                    "You’re all set to perform transactions.\n\n"
                    "Use the menu below to continue:",
                    parse_mode="Markdown",
                    reply_markup=get_command_menu()
                )
            else:
                reply_func(
                    "⚠️ *KYC/KYB Not Approved.*\n"
                    "Please complete your KYC/KYB on the Copperx platform to enable full functionality: https://copperx.io\n\n"
                    "Use the menu below to continue:",
                    parse_mode="Markdown",
                    reply_markup=get_command_menu()
                )
        else:
            try:
                error_msg = response.json().get('message', 'Unknown error')
            except ValueError:
                error_msg = "Invalid response from Copperx"
            logger.error(f"Error checking KYC for user {chat_id}: {response.status_code}, {error_msg}")
            reply_func(
                f"❌ *Error checking KYC:* {error_msg}\n"
                "Please try again or contact support: https://t.me/copperxcommunity/2183",
                parse_mode="Markdown"
            )
    except requests.RequestException as e:
        logger.error(f"Network error in kyc for user {chat_id}: {e}")
        reply_func(
            f"❌ *Network error:* {str(e)}\n"
            "Please check your connection and try again.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Unexpected error in kyc for user {chat_id}: {e}")
        reply_func(
            "❌ *An unexpected error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )

# Wallet Management
def balance(update, context):
    try:
        chat_id = update.message.chat_id
        reply_func = update.message.reply_text
        user = get_user(chat_id)
        user = refresh_token_if_needed(user, chat_id, reply_func)
        if not user:
            return
        headers = {"Authorization": f"Bearer {user['token']}"}
        response = requests.get(f"{BASE_URL}/wallets/balances", headers=headers)
        if response.status_code == 200:
            try:
                balances = response.json()
            except ValueError as e:
                logger.error(f"Error parsing JSON response in balance for user {chat_id}: {e}, response: {response.text}")
                reply_func(
                    "❌ *Error:* Invalid response from Copperx. Please try again or contact support: https://t.me/copperxcommunity/2183",
                    parse_mode="Markdown"
                )
                return
            if not balances or not isinstance(balances, list):
                reply_func(
                    "⚠️ *No wallet balances found.*\n"
                    "Please deposit USDC to your wallet. Use /deposit for instructions.\n\n"
                    "Use the menu below to continue:",
                    parse_mode="Markdown",
                    reply_markup=get_command_menu()
                )
                return
            message = "💰 *Your Wallet Balances:*\n\n"
            for b in balances:
                amount = b.get('amount', '0')
                network = b.get('network', 'Unknown')
                message += f"- {amount} USDC on {network}\n"
            message += "\nUse the menu below to continue:"
            reply_func(message, parse_mode="Markdown", reply_markup=get_command_menu())
        else:
            try:
                error_msg = response.json().get('message', 'Unknown error')
            except ValueError:
                error_msg = "Invalid response from Copperx"
            logger.error(f"Error fetching balances for user {chat_id}: {response.status_code}, {error_msg}")
            reply_func(
                f"❌ *Error fetching balances:* {error_msg}\n"
                "Please try again or contact support: https://t.me/copperxcommunity/2183",
                parse_mode="Markdown"
            )
    except requests.RequestException as e:
        logger.error(f"Network error in balance for user {chat_id}: {e}")
        reply_func(
            f"❌ *Network error:* {str(e)}\n"
            "Please check your connection and try again.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Unexpected error in balance for user {chat_id}: {e}")
        reply_func(
            "❌ *An unexpected error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )

def setdefault(update, context):
    try:
        chat_id = update.message.chat_id
        reply_func = update.message.reply_text
        user = get_user(chat_id)
        user = refresh_token_if_needed(user, chat_id, reply_func)
        if not user:
            return
        headers = {"Authorization": f"Bearer {user['token']}"}
        response = requests.get(f"{BASE_URL}/wallets", headers=headers)
        if response.status_code == 200:
            try:
                wallets = response.json()
            except ValueError as e:
                logger.error(f"Error parsing JSON response in setdefault for user {chat_id}: {e}, response: {response.text}")
                reply_func(
                    "❌ *Error:* Invalid response from Copperx. Please try again or contact support: https://t.me/copperxcommunity/2183",
                    parse_mode="Markdown"
                )
                return
            if not wallets or not isinstance(wallets, list):
                reply_func(
                    "⚠️ *No wallets found.*\n"
                    "Please add a wallet on the Copperx platform: https://copperx.io\n\n"
                    "Use the menu below to continue:",
                    parse_mode="Markdown",
                    reply_markup=get_command_menu()
                )
                return
            keyboard = [
                [InlineKeyboardButton(w.get("network", "Unknown"), callback_data=f"default_{w['id']}")]
                for w in wallets if w.get("id")
            ]
            if not keyboard:
                reply_func(
                    "⚠️ *No valid wallets found.*\n"
                    "Please add a wallet on the Copperx platform: https://copperx.io\n\n"
                    "Use the menu below to continue:",
                    parse_mode="Markdown",
                    reply_markup=get_command_menu()
                )
                return
            reply_func(
                "🔧 *Select your default wallet:*\n"
                "This wallet will be used for transactions.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        else:
            try:
                error_msg = response.json().get('message', 'Unknown error')
            except ValueError:
                error_msg = "Invalid response from Copperx"
            logger.error(f"Error fetching wallets for user {chat_id}: {response.status_code}, {error_msg}")
            reply_func(
                f"❌ *Error fetching wallets:* {error_msg}\n"
                "Please try again or contact support: https://t.me/copperxcommunity/2183",
                parse_mode="Markdown"
            )
    except requests.RequestException as e:
        logger.error(f"Network error in setdefault for user {chat_id}: {e}")
        reply_func(
            f"❌ *Network error:* {str(e)}\n"
            "Please check your connection and try again.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Unexpected error in setdefault for user {chat_id}: {e}")
        reply_func(
            "❌ *An unexpected error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )

def setdefault_callback(update, context):
    try:
        query = update.callback_query
        wallet_id = query.data.split("_")[1]
        chat_id = query.message.chat_id
        user = get_user(chat_id)
        user = refresh_token_if_needed(user, chat_id, query.message.reply_text)
        if not user:
            return
        update_default_wallet(chat_id, wallet_id)
        response = requests.put(
            f"{BASE_URL}/wallets/default",
            json={"walletId": wallet_id},
            headers={"Authorization": f"Bearer {user['token']}"}
        )
        if response.status_code == 200:
            query.edit_message_text(
                "✅ *Default wallet set successfully!*\n"
                "Use the menu below to continue:",
                parse_mode="Markdown",
                reply_markup=get_command_menu()
            )
        else:
            try:
                error_msg = response.json().get('message', 'Unknown error')
            except ValueError:
                error_msg = "Invalid response from Copperx"
            logger.error(f"Error setting default wallet for user {chat_id}: {response.status_code}, {error_msg}")
            query.edit_message_text(
                f"❌ *Error setting default wallet:* {error_msg}\n"
                "Please try again or contact support: https://t.me/copperxcommunity/2183",
                parse_mode="Markdown"
            )
    except requests.RequestException as e:
        logger.error(f"Network error in setdefault_callback for user {chat_id}: {e}")
        query.edit_message_text(
            f"❌ *Network error:* {str(e)}\n"
            "Please check your connection and try again.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Unexpected error in setdefault_callback for user {chat_id}: {e}")
        query.edit_message_text(
            "❌ *An unexpected error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )

def deposit(update, context):
    try:
        chat_id = update.message.chat_id
        reply_func = update.message.reply_text
        user = get_user(chat_id)
        user = refresh_token_if_needed(user, chat_id, reply_func)
        if not user:
            return
        reply_func(
            "💸 *Deposit USDC:*\n\n"
            "To deposit USDC, please send it to your wallet address on the Copperx platform.\n"
            "1. Visit https://copperx.io and log in.\n"
            "2. Navigate to your wallet section.\n"
            "3. Copy your wallet address and send USDC to it.\n"
            "4. Use /balance to check your updated balance.\n\n"
            "You’ll receive a notification here once the deposit is confirmed.\n\n"
            "Use the menu below to continue:",
            parse_mode="Markdown",
            reply_markup=get_command_menu()
        )
    except Exception as e:
        logger.error(f"Error in deposit for user {chat_id}: {e}")
        reply_func(
            "❌ *An unexpected error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )

def history(update, context):
    try:
        chat_id = update.message.chat_id
        reply_func = update.message.reply_text
        user = get_user(chat_id)
        user = refresh_token_if_needed(user, chat_id, reply_func)
        if not user:
            return
        headers = {"Authorization": f"Bearer {user['token']}"}
        response = requests.get(f"{BASE_URL}/transfers?page=1&limit=10", headers=headers)
        if response.status_code == 200:
            try:
                transfers = response.json()
            except ValueError as e:
                logger.error(f"Error parsing JSON response in history for user {chat_id}: {e}, response: {response.text}")
                reply_func(
                    "❌ *Error:* Invalid response from Copperx. Please try again or contact support: https://t.me/copperxcommunity/2183",
                    parse_mode="Markdown"
                )
                return
            if not transfers or not isinstance(transfers, list):
                reply_func(
                    "📜 *Transaction History:*\n\n"
                    "No recent transactions found.\n"
                    "Use /send or /withdraw to start transacting.\n\n"
                    "Use the menu below to continue:",
                    parse_mode="Markdown",
                    reply_markup=get_command_menu()
                )
                return
            message = "📜 *Last 10 Transactions:*\n\n"
            for t in transfers:
                amount = t.get('amount', '0')
                transfer_type = t.get('type', 'Unknown')
                created_at = t.get('createdAt', 'Unknown')[:10] if t.get('createdAt') else 'Unknown'
                message += f"- {amount} USDC ({transfer_type}) on {created_at}\n"
            message += "\nUse the menu below to continue:"
            reply_func(message, parse_mode="Markdown", reply_markup=get_command_menu())
        else:
            try:
                error_msg = response.json().get('message', 'Unknown error')
            except ValueError:
                error_msg = "Invalid response from Copperx"
            logger.error(f"Error fetching history for user {chat_id}: {response.status_code}, {error_msg}")
            reply_func(
                f"❌ *Error fetching history:* {error_msg}\n"
                "Please try again or contact support: https://t.me/copperxcommunity/2183",
                parse_mode="Markdown"
            )
    except requests.RequestException as e:
        logger.error(f"Network error in history for user {chat_id}: {e}")
        reply_func(
            f"❌ *Network error:* {str(e)}\n"
            "Please check your connection and try again.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Unexpected error in history for user {chat_id}: {e}")
        reply_func(
            "❌ *An unexpected error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )

# Fund Transfers
def send(update, context):
    try:
        chat_id = update.message.chat_id
        reply_func = update.message.reply_text
        user = get_user(chat_id)
        user = refresh_token_if_needed(user, chat_id, reply_func)
        if not user:
            return ConversationHandler.END
        keyboard = [
            [InlineKeyboardButton("Email", callback_data="send_email")],
            [InlineKeyboardButton("Wallet", callback_data="send_wallet")],
            [InlineKeyboardButton("Cancel", callback_data="send_cancel")]
        ]
        reply_func(
            "📤 *Send USDC:*\n"
            "Choose the recipient type:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return SEND_TYPE
    except Exception as e:
        logger.error(f"Error in send for user {chat_id}: {e}")
        reply_func(
            "❌ *An unexpected error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

def send_type(update, context):
    try:
        query = update.callback_query
        query.answer()
        context.user_data["send_type"] = query.data.split("_")[1]
        query.message.reply_text(
            "📧 *Enter recipient:*\n"
            "Please provide the email address or wallet address of the recipient:",
            parse_mode="Markdown"
        )
        return SEND_RECIPIENT
    except Exception as e:
        logger.error(f"Error in send_type: {e}")
        query.message.reply_text(
            "❌ *An unexpected error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

def send_recipient(update, context):
    try:
        recipient = update.message.text
        send_type = context.user_data.get("send_type")
        if not send_type:
            update.message.reply_text(
                "❌ *Session error.* Please start the send process again with /send.",
                parse_mode="Markdown"
            )
            return ConversationHandler.END
        if send_type == "email" and not re.match(r"[^@]+@[^@]+\.[^@]+", recipient):
            update.message.reply_text(
                "❌ *Invalid email format.* Please enter a valid email address:",
                parse_mode="Markdown"
            )
            return SEND_RECIPIENT
        context.user_data["recipient"] = recipient
        update.message.reply_text(
            "💵 *Enter amount:*\n"
            "Please specify the amount in USDC to send:",
            parse_mode="Markdown"
        )
        return SEND_AMOUNT
    except Exception as e:
        logger.error(f"Error in send_recipient: {e}")
        update.message.reply_text(
            "❌ *An unexpected error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

def send_amount(update, context):
    try:
        amount = update.message.text
        try:
            amount = float(amount)
            if amount <= 0:
                raise ValueError
        except ValueError:
            update.message.reply_text(
                "❌ *Invalid amount.* Please enter a positive number:",
                parse_mode="Markdown"
            )
            return SEND_AMOUNT
        context.user_data["amount"] = amount
        recipient = context.user_data.get("recipient")
        if not recipient:
            update.message.reply_text(
                "❌ *Session error.* Please start the send process again with /send.",
                parse_mode="Markdown"
            )
            return ConversationHandler.END
        keyboard = [
            [InlineKeyboardButton("Confirm", callback_data="send_confirm")],
            [InlineKeyboardButton("Cancel", callback_data="send_cancel")]
        ]
        update.message.reply_text(
            f"📤 *Send {amount} USDC to {recipient}?*\n"
            "⚠️ Please note that transaction fees may apply.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return SEND_CONFIRM
    except Exception as e:
        logger.error(f"Error in send_amount: {e}")
        update.message.reply_text(
            "❌ *An unexpected error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

def send_confirm(update, context):
    try:
        query = update.callback_query
        query.answer()
        chat_id = query.message.chat_id
        user = get_user(chat_id)
        user = refresh_token_if_needed(user, chat_id, query.message.reply_text)
        if not user:
            return ConversationHandler.END
        send_type = context.user_data.get("send_type")
        recipient = context.user_data.get("recipient")
        amount = context.user_data.get("amount")
        if not all([send_type, recipient, amount]):
            query.message.reply_text(
                "❌ *Session error.* Please start the send process again with /send.",
                parse_mode="Markdown"
            )
            return ConversationHandler.END
        headers = {"Authorization": f"Bearer {user['token']}"}
        endpoint = "/transfers/send" if send_type == "email" else "/transfers/wallet-withdraw"
        data = {"amount": amount, "to": recipient} if send_type == "email" else {"amount": amount, "address": recipient}
        response = requests.post(f"{BASE_URL}{endpoint}", json=data, headers=headers)
        if response.status_code == 200:
            query.edit_message_text(
                "✅ *Transfer successful!*\n"
                f"You’ve sent {amount} USDC to {recipient}.\n\n"
                "Use the menu below to continue:",
                parse_mode="Markdown",
                reply_markup=get_command_menu()
            )
        else:
            try:
                error_msg = response.json().get('message', 'Unknown error')
            except ValueError:
                error_msg = "Invalid response from Copperx"
            logger.error(f"Error in send_confirm for user {chat_id}: {response.status_code}, {error_msg}")
            query.edit_message_text(
                f"❌ *Transfer failed:* {error_msg}\n"
                "Please check the recipient details and your balance, then try again.",
                parse_mode="Markdown"
            )
        return ConversationHandler.END
    except requests.RequestException as e:
        logger.error(f"Network error in send_confirm for user {chat_id}: {e}")
        query.edit_message_text(
            f"❌ *Network error:* {str(e)}\n"
            "Please check your connection and try again.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Unexpected error in send_confirm for user {chat_id}: {e}")
        query.edit_message_text(
            "❌ *An unexpected error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

def withdraw(update, context):
    try:
        chat_id = update.message.chat_id
        reply_func = update.message.reply_text
        user = get_user(chat_id)
        user = refresh_token_if_needed(user, chat_id, reply_func)
        if not user:
            return ConversationHandler.END
        keyboard = [
            [InlineKeyboardButton("Cancel", callback_data="withdraw_cancel")]
        ]
        reply_func(
            "🏦 *Withdraw to Bank:*\n"
            "Please enter the amount in USDC to withdraw:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return WITHDRAW_AMOUNT
    except Exception as e:
        logger.error(f"Error in withdraw for user {chat_id}: {e}")
        reply_func(
            "❌ *An unexpected error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

def withdraw_amount(update, context):
    try:
        amount = update.message.text
        try:
            amount = float(amount)
            if amount <= 0:
                raise ValueError
        except ValueError:
            update.message.reply_text(
                "❌ *Invalid amount.* Please enter a positive number:",
                parse_mode="Markdown"
            )
            return WITHDRAW_AMOUNT
        context.user_data["withdraw_amount"] = amount
        keyboard = [
            [InlineKeyboardButton("Confirm", callback_data="withdraw_confirm")],
            [InlineKeyboardButton("Cancel", callback_data="withdraw_cancel")]
        ]
        update.message.reply_text(
            f"🏦 *Withdraw {amount} USDC to your bank account?*\n"
            "⚠️ Please ensure your KYC is approved. Transaction fees may apply.",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return WITHDRAW_CONFIRM
    except Exception as e:
        logger.error(f"Error in withdraw_amount: {e}")
        update.message.reply_text(
            "❌ *An unexpected error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

def withdraw_confirm(update, context):
    try:
        query = update.callback_query
        query.answer()
        chat_id = query.message.chat_id
        user = get_user(chat_id)
        user = refresh_token_if_needed(user, chat_id, query.message.reply_text)
        if not user:
            return ConversationHandler.END
        amount = context.user_data.get("withdraw_amount")
        if not amount:
            query.message.reply_text(
                "❌ *Session error.* Please start the withdraw process again with /withdraw.",
                parse_mode="Markdown"
            )
            return ConversationHandler.END
        headers = {"Authorization": f"Bearer {user['token']}"}
        response = requests.post(
            f"{BASE_URL}/transfers/offramp",
            json={"amount": amount},
            headers=headers
        )
        if response.status_code == 200:
            query.edit_message_text(
                "✅ *Withdrawal requested!*\n"
                f"You’ve requested to withdraw {amount} USDC to your bank account.\n"
                "Processing may take a few business days.\n\n"
                "Use the menu below to continue:",
                parse_mode="Markdown",
                reply_markup=get_command_menu()
            )
        else:
            try:
                error_msg = response.json().get('message', 'Check balance or KYC')
            except ValueError:
                error_msg = "Invalid response from Copperx"
            logger.error(f"Error in withdraw_confirm for user {chat_id}: {response.status_code}, {error_msg}")
            query.edit_message_text(
                f"❌ *Withdrawal failed:* {error_msg}\n"
                "Please ensure your KYC is approved and you have sufficient balance.",
                parse_mode="Markdown"
            )
        return ConversationHandler.END
    except requests.RequestException as e:
        logger.error(f"Network error in withdraw_confirm for user {chat_id}: {e}")
        query.edit_message_text(
            f"❌ *Network error:* {str(e)}\n"
            "Please check your connection and try again.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Unexpected error in withdraw_confirm for user {chat_id}: {e}")
        query.edit_message_text(
            "❌ *An unexpected error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

def cancel(update, context):
    try:
        update.message.reply_text(
            "❌ *Operation cancelled.*\n"
            "Use the menu below to continue:",
            parse_mode="Markdown",
            reply_markup=get_command_menu()
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in cancel: {e}")
        update.message.reply_text(
            "❌ *An unexpected error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

# Pusher for Deposit Notifications
def start_pusher(chat_id, token, org_id, context):
    try:
        if not all([PUSHER_KEY, PUSHER_CLUSTER, PUSHER_APP_ID, PUSHER_SECRET]):
            logger.warning("Pusher credentials incomplete. Deposit notifications will not be enabled.")
            context.bot.send_message(
                chat_id,
                "⚠️ *Deposit notifications are disabled.* Pusher credentials are missing.\n"
                "You can still use the bot, but you won’t receive real-time deposit updates.",
                parse_mode="Markdown"
            )
            return
        pusher_client = Pusher(
            app_id=PUSHER_APP_ID,
            key=PUSHER_KEY,
            secret=PUSHER_SECRET,
            cluster=PUSHER_CLUSTER
        )
        channel = pusher_client.subscribe(f"private-org-{org_id}")
        channel.bind("deposit", lambda data: context.bot.send_message(
            chat_id,
            f"💰 *New Deposit Received!*\n\n"
            f"Amount: {data.get('amount', '0')} USDC\n"
            f"Network: {data.get('network', 'Unknown')}\n\n"
            "Use /balance to check your updated balance.",
            parse_mode="Markdown"
        ))
        threading.Thread(target=lambda: pusher_client.connect(), daemon=True).start()
        logger.info(f"Pusher connected for chat_id {chat_id} on organization {org_id}")
    except Exception as e:
        logger.error(f"Error in start_pusher for chat_id {chat_id}: {e}")
        context.bot.send_message(
            chat_id,
            f"⚠️ *Error setting up deposit notifications:* {str(e)}\n"
            "Please contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )

# Error handler
def error_handler(update, context):
    logger.error(f"Update {update} caused error {context.error}")
    try:
        chat_id = update.message.chat_id if update.message else update.callback_query.message.chat_id
        reply_func = update.message.reply_text if update.message else update.callback_query.message.reply_text
        reply_func(
            f"❌ *An error occurred:* {str(context.error)}\n"
            "Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error in error_handler: {e}")

# Main function
def main():
    try:
        init_db()
        updater = Updater(TELEGRAM_TOKEN, use_context=True)
        dp = updater.dispatcher
        bot = updater.bot
        commands = [
            ("start", "Start the bot"),
            ("login", "Log in to Copperx"),
            ("logout", "Log out of Copperx"),
            ("profile", "View account info"),
            ("kyc", "Check KYC status"),
            ("balance", "Check wallet balances"),
            ("setdefault", "Set default wallet"),
            ("deposit", "Get deposit instructions"),
            ("history", "View last 10 transactions"),
            ("send", "Send USDC"),
            ("withdraw", "Withdraw to bank"),
            ("help", "Show this message")
        ]
        bot.set_my_commands([(cmd, desc) for cmd, desc in commands])
        dp.add_handler(CommandHandler("start", start))
        dp.add_handler(CommandHandler("help", help_command))
        dp.add_handler(CallbackQueryHandler(menu_callback, pattern="^cmd_"))
        dp.add_handler(CommandHandler("logout", logout))
        dp.add_handler(CommandHandler("profile", profile))
        dp.add_handler(CommandHandler("kyc", kyc))
        dp.add_handler(CommandHandler("balance", balance))
        dp.add_handler(CommandHandler("setdefault", setdefault))
        dp.add_handler(CallbackQueryHandler(setdefault_callback, pattern="^default_"))
        dp.add_handler(CommandHandler("deposit", deposit))
        dp.add_handler(CommandHandler("history", history))
        send_conv = ConversationHandler(
            entry_points=[CommandHandler("send", send)],
            states={
                SEND_TYPE: [CallbackQueryHandler(send_type, pattern="^send_")],
                SEND_RECIPIENT: [MessageHandler(Filters.text & ~Filters.command, send_recipient)],
                SEND_AMOUNT: [MessageHandler(Filters.text & ~Filters.command, send_amount)],
                SEND_CONFIRM: [CallbackQueryHandler(send_confirm, pattern="^send_confirm$"),
                              CallbackQueryHandler(cancel, pattern="^send_cancel$")]
            },
            fallbacks=[CommandHandler("cancel", cancel)]
        )
        dp.add_handler(send_conv)
        withdraw_conv = ConversationHandler(
            entry_points=[CommandHandler("withdraw", withdraw)],
            states={
                WITHDRAW_AMOUNT: [MessageHandler(Filters.text & ~Filters.command, withdraw_amount)],
                WITHDRAW_CONFIRM: [CallbackQueryHandler(withdraw_confirm, pattern="^withdraw_confirm$"),
                                  CallbackQueryHandler(cancel, pattern="^withdraw_cancel$")]
            },
            fallbacks=[CommandHandler("cancel", cancel)]
        )
        dp.add_handler(withdraw_conv)
        login_conv = ConversationHandler(
            entry_points=[CommandHandler("login", login)],
            states={
                EMAIL: [MessageHandler(Filters.text & ~Filters.command, get_email)],
                OTP: [MessageHandler(Filters.text & ~Filters.command, verify_otp)]
            },
            fallbacks=[CommandHandler("cancel", cancel)]
        )
        dp.add_handler(login_conv)
        dp.add_error_handler(error_handler)
        updater.start_polling()
        print("Bot is running...")
        updater.idle()
    except Exception as e:
        logger.error(f"Error in main: {e}")
        print(f"Bot crashed: {e}")

if __name__ == "__main__":
    main()