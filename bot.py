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
    if not user or not user.get("token_expiry"):
        reply_func("⚠️ Please /login to continue.")
        return None
    try:
        expiry = datetime.strptime(user["token_expiry"], "%Y-%m-%d %H:%M:%S")
        if datetime.now() >= expiry:
            reply_func("⚠️ Your session has expired. Please /login again to continue.")
            return None
        return user
    except ValueError as e:
        logger.error(f"Error parsing token expiry: {e}")
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
        user = update.message.from_user.first_name
        update.message.reply_text(
            f"🌟 *Welcome to the Copperx Payout Bot, {user}!* 🌟\n"
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

        # Send the command as a message to trigger the appropriate handler
        context.bot.send_message(chat_id=chat_id, text=f"/{command}")
    except Exception as e:
        logger.error(f"Error in menu_callback: {e}")
        query.message.reply_text(
            "❌ *An error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
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
            # Capture the sid from the response
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
            data = response.json()
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
            reply_func(
                f"❌ *Error fetching profile:* {response.json().get('message', 'Unknown error')}\n"
                "Please try again or contact support: https://t.me/copperxcommunity/2183",
                parse_mode="Markdown"
            )
    except requests.RequestException as e:
        logger.error(f"Network error in profile: {e}")
        reply_func(
            f"❌ *Network error:* {str(e)}\n"
            "Please check your connection and try again.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error in profile: {e}")
        reply_func(
            "❌ *An error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
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
            kycs = response.json()
            if kycs and kycs[0]["status"] == "approved":
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
            reply_func(
                f"❌ *Error checking KYC:* {response.json().get('message', 'Unknown error')}\n"
                "Please try again or contact support: https://t.me/copperxcommunity/2183",
                parse_mode="Markdown"
            )
    except requests.RequestException as e:
        logger.error(f"Network error in kyc: {e}")
        reply_func(
            f"❌ *Network error:* {str(e)}\n"
            "Please check your connection and try again.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error in kyc: {e}")
        reply_func(
            "❌ *An error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
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
            balances = response.json()
            if not balances:
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
                message += f"- {b['amount']} USDC on {b['network']}\n"
            message += "\nUse the menu below to continue:"
            reply_func(message, parse_mode="Markdown", reply_markup=get_command_menu())
        else:
            reply_func(
                f"❌ *Error fetching balances:* {response.json().get('message', 'Unknown error')}\n"
                "Please try again or contact support: https://t.me/copperxcommunity/2183",
                parse_mode="Markdown"
            )
    except requests.RequestException as e:
        logger.error(f"Network error in balance: {e}")
        reply_func(
            f"❌ *Network error:* {str(e)}\n"
            "Please check your connection and try again.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error in balance: {e}")
        reply_func(
            "❌ *An error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
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
            wallets = response.json()
            if not wallets:
                reply_func(
                    "⚠️ *No wallets found.*\n"
                    "Please add a wallet on the Copperx platform: https://copperx.io\n\n"
                    "Use the menu below to continue:",
                    parse_mode="Markdown",
                    reply_markup=get_command_menu()
                )
                return
            keyboard = [
                [InlineKeyboardButton(w["network"], callback_data=f"default_{w['id']}")]
                for w in wallets
            ]
            reply_func(
                "🔧 *Select your default wallet:*\n"
                "This wallet will be used for transactions.",
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="Markdown"
            )
        else:
            reply_func(
                f"❌ *Error fetching wallets:* {response.json().get('message', 'Unknown error')}\n"
                "Please try again or contact support: https://t.me/copperxcommunity/2183",
                parse_mode="Markdown"
            )
    except requests.RequestException as e:
        logger.error(f"Network error in setdefault: {e}")
        reply_func(
            f"❌ *Network error:* {str(e)}\n"
            "Please check your connection and try again.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error in setdefault: {e}")
        reply_func(
            "❌ *An error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )

def setdefault_callback(update, context):
    try:
        query = update.callback_query
        wallet_id = query.data.split("_")[1]
        user = get_user(query.message.chat_id)
        update_default_wallet(query.message.chat_id, wallet_id)
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
            query.edit_message_text(
                f"❌ *Error setting default wallet:* {response.json().get('message', 'Unknown error')}\n"
                "Please try again or contact support: https://t.me/copperxcommunity/2183",
                parse_mode="Markdown"
            )
    except requests.RequestException as e:
        logger.error(f"Network error in setdefault_callback: {e}")
        query.edit_message_text(
            f"❌ *Network error:* {str(e)}\n"
            "Please check your connection and try again.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error in setdefault_callback: {e}")
        query.edit_message_text(
            "❌ *An error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
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
        logger.error(f"Error in deposit: {e}")
        reply_func(
            "❌ *An error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
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
            transfers = response.json()
            if not transfers:
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
                message += f"- {t['amount']} USDC ({t['type']}) on {t['createdAt'][:10]}\n"
            message += "\nUse the menu below to continue:"
            reply_func(message, parse_mode="Markdown", reply_markup=get_command_menu())
        else:
            reply_func(
                f"❌ *Error fetching history:* {response.json().get('message', 'Unknown error')}\n"
                "Please try again or contact support: https://t.me/copperxcommunity/2183",
                parse_mode="Markdown"
            )
    except requests.RequestException as e:
        logger.error(f"Network error in history: {e}")
        reply_func(
            f"❌ *Network error:* {str(e)}\n"
            "Please check your connection and try again.",
            parse_mode="Markdown"
        )
    except Exception as e:
        logger.error(f"Error in history: {e}")
        reply_func(
            "❌ *An error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
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
            [InlineKeyboardButton("Wallet", callback_data="send_wallet")]
        ]
        reply_func(
            "📤 *Send USDC:*\n"
            "Choose the recipient type:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown"
        )
        return SEND_TYPE
    except Exception as e:
        logger.error(f"Error in send: {e}")
        reply_func(
            "❌ *An error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
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
            "❌ *An error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
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
            "❌ *An error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
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
            "❌ *An error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

def send_confirm(update, context):
    try:
        query = update.callback_query
        query.answer()
        user = get_user(query.message.chat_id)
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
            query.edit_message_text(
                f"❌ *Transfer failed:* {response.json().get('message', 'Unknown error')}\n"
                "Please check the recipient details and your balance, then try again.",
                parse_mode="Markdown"
            )
        return ConversationHandler.END
    except requests.RequestException as e:
        logger.error(f"Network error in send_confirm: {e}")
        query.edit_message_text(
            f"❌ *Network error:* {str(e)}\n"
            "Please check your connection and try again.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in send_confirm: {e}")
        query.edit_message_text(
            "❌ *An error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
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
        reply_func(
            "🏦 *Withdraw to Bank:*\n"
            "Please enter the amount in USDC to withdraw:",
            parse_mode="Markdown"
        )
        return WITHDRAW_AMOUNT
    except Exception as e:
        logger.error(f"Error in withdraw: {e}")
        reply_func(
            "❌ *An error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
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
            "❌ *An error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

def withdraw_confirm(update, context):
    try:
        query = update.callback_query
        query.answer()
        user = get_user(query.message.chat_id)
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
            query.edit_message_text(
                f"❌ *Withdrawal failed:* {response.json().get('message', 'Check balance or KYC')}\n"
                "Please ensure your KYC is approved and you have sufficient balance.",
                parse_mode="Markdown"
            )
        return ConversationHandler.END
    except requests.RequestException as e:
        logger.error(f"Network error in withdraw_confirm: {e}")
        query.edit_message_text(
            f"❌ *Network error:* {str(e)}\n"
            "Please check your connection and try again.",
            parse_mode="Markdown"
        )
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"Error in withdraw_confirm: {e}")
        query.edit_message_text(
            "❌ *An error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
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
            "❌ *An error occurred.* Please try again or contact support: https://t.me/copperxcommunity/2183",
            parse_mode="Markdown"
        )
        return ConversationHandler.END

# Pusher for Deposit Notifications
def start_pusher(chat_id, token, org_id, context):
    try:
        if not PUSHER_KEY or not PUSHER_CLUSTER:
            logger.warning("Pusher credentials not found. Deposit notifications will not be enabled.")
            return
        pusher_client = Pusher(app_id='your-app-id', key=PUSHER_KEY, secret='your-secret', cluster=PUSHER_CLUSTER)
        channel = pusher_client.subscribe(f"private-org-{org_id}")
        channel.bind("deposit", lambda data: context.bot.send_message(
            chat_id,
            f"💰 *New Deposit Received!*\n\n"
            f"Amount: {data['amount']} USDC\n"
            f"Network: {data.get('network', 'Unknown')}\n\n"
            "Use /balance to check your updated balance.",
            parse_mode="Markdown"
        ))
        threading.Thread(target=lambda: pusher_client.connect(), daemon=True).start()
    except Exception as e:
        logger.error(f"Error in start_pusher: {e}")
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
        updater = Updater(TELEGRAM_TOKEN, use_context=True)
        dp = updater.dispatcher

        # Set Telegram command menu
        bot = updater.bot
        commands = [
            ("start", "Start the bot"),
            ("login", "Log in to Copperx"),
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