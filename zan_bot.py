import telegram
import ccxt
import pandas as pd
import requests
from telegram.ext import Application, CommandHandler
import schedule
import time
from datetime import datetime
from threading import Thread
import os
from dotenv import load_dotenv


load_dotenv()

# Configuration
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
ALLOWED_TIMEFRAMES = ['1h', '4h', '12h', '1d']
CHAT_IDS = set()  # Store chat IDs for scheduled scans
DEFAULT_TOP = 50  # Default to top 50 coins by market cap

# Initialize Binance
binance = ccxt.binance({'enableRateLimit': True})

# Fetch OHLCV data from Binance public API
def get_ohlcv(symbol, timeframe, limit=250):
    try:
        url = f"https://api.binance.com/api/v3/klines?symbol={symbol.replace('/', '')}&interval={timeframe}&limit={limit}"
        response = requests.get(url, timeout=15)
        if response.status_code != 200:
            raise Exception(f"API request failed: {response.text}")
        data = response.json()
        df = pd.DataFrame(
            data,
            columns=['timestamp', 'open', 'high', 'low', 'close', 'volume', 'close_time', 'quote_volume', 'trades', 'taker_buy_base', 'taker_buy_quote', 'ignore']
        )
        df['timestamp'] = pd.to_datetime(df['timestamp'], unit='ms')
        numeric_columns = ['open', 'high', 'low', 'close', 'volume']
        df[numeric_columns] = df[numeric_columns].astype(float)
        df = df[['timestamp', 'open', 'high', 'low', 'close', 'volume']]
        return df
    except Exception as e:
        return None

# Calculate volume anomaly and return max ratio
def check_volume_anomaly(df):
    try:
        max_ratio = 0
        for i in range(-5, 0):  # Check last 5 candles
            current_volume = df['volume'].iloc[i]
            ma_20 = df['volume'].iloc[i-20:i].mean()
            if pd.notna(ma_20) and ma_20 > 0:
                ratio = current_volume / ma_20
                if ratio > 1.5:  # Volume > 1.5 * MA
                    max_ratio = max(max_ratio, ratio)
        return max_ratio if max_ratio > 1.5 else 0
    except Exception:
        return 0

# Fetch top coins by market cap from CoinGecko
def get_top_coins_by_market_cap(top=None):
    try:
        url = "https://api.coingecko.com/api/v3/coins/markets?vs_currency=usd&order=market_cap_desc&per_page=250&page=1&sparkline=false"
        response = requests.get(url, timeout=15)
        if response.status_code != 200:
            raise Exception(f"CoinGecko API request failed: {response.text}")
        coins = response.json()
        
        # Map CoinGecko IDs to Binance symbols
        markets = binance.load_markets()
        usdt_pairs = {symbol: markets[symbol]['base'] for symbol in markets if symbol.endswith('/USDT')}
        
        top_coins = []
        for coin in coins:
            coin_id = coin['symbol'].upper()
            for symbol, base in usdt_pairs.items():
                if base == coin_id:
                    top_coins.append(symbol)
                    break
            if top and len(top_coins) >= top:
                break
        
        return top_coins
    except Exception:
        return []

# Scan coins for volume anomalies
async def scan_coins(symbol, timeframe, top, chat_id, application):
    try:
        # Prepare list of symbols to scan
        if symbol:  # If specific symbol is provided
            symbols = [symbol]
        else:
            if top == "full":  # Scan all USDT pairs
                markets = binance.load_markets()
                symbols = [s for s in markets if s.endswith('/USDT')]
            else:  # Get top coins by market cap (default 50 or specified top)
                top_count = top if top else DEFAULT_TOP
                symbols = get_top_coins_by_market_cap(top_count)
                if not symbols:
                    raise Exception("Failed to fetch top coins by market cap")

        anomaly_coins = []
        for sym in symbols:
            df = get_ohlcv(sym, timeframe, 30)  # 5 (recent) + 20 (MA) + buffer
            if df is None or len(df) < 25:
                continue
            ratio = check_volume_anomaly(df)
            if ratio > 1.5:
                anomaly_coins.append((sym, ratio))
        
        # Sort by ratio descending
        anomaly_coins.sort(key=lambda x: x[1], reverse=True)
        
        # Prepare response
        if anomaly_coins:
            message = f"🚨 Volume Anomaly Signals ({timeframe}) at {datetime.now().strftime('%Y-%m-%d %H:%M')}:\n"
            for sym, ratio in anomaly_coins:
                symbol_clean = sym.replace('/USDT', '')
                tradingview_link = f"https://www.tradingview.com/chart/?symbol=BINANCE:{symbol_clean}USDT"
                message += f"- [{sym}]({tradingview_link}): +{(ratio * 100 - 100):.2f}%\n"
        else:
            message = f"No volume anomaly signals for {timeframe} at {datetime.now().strftime('%Y-%m-%d %H:%M')}"
        
        await application.bot.send_message(chat_id=chat_id, text=message, parse_mode='Markdown')
    except Exception as e:
        await application.bot.send_message(chat_id=chat_id, text=f"Error scanning coins: {str(e)}")

# /scan command
async def scan(update, context):
    try:
        args = context.args
        symbol = None
        timeframe = '1h'
        top = None

        # Parse arguments
        if args:
            if len(args) == 1:
                # Only timeframe provided: /scan 4h
                timeframe = args[0]
            elif len(args) == 2:
                # Timeframe and top: /scan 4h 100 or /scan 4h full
                timeframe, top_arg = args[0], args[1]
                if top_arg.lower() == 'full':
                    top = "full"
                else:
                    top = int(top_arg)
            elif len(args) == 3:
                # Symbol, timeframe, and top: /scan BTCUSDT 4h 100 or /scan BTCUSDT 4h full
                symbol, timeframe, top_arg = args[0].upper(), args[1], args[2]
                if top_arg.lower() == 'full':
                    top = "full"
                else:
                    top = int(top_arg)
                if not symbol.endswith('USDT'):
                    symbol += '/USDT'
            else:
                raise ValueError("Invalid arguments. Use: /scan [symbol] [timeframe] [top] (e.g., /scan 4h 100, /scan 4h full, or /scan BTCUSDT 4h full)")

        if timeframe not in ALLOWED_TIMEFRAMES:
            raise ValueError(f"Invalid timeframe. Use: {', '.join(ALLOWED_TIMEFRAMES)}")
        
        if isinstance(top, int) and top <= 0:
            raise ValueError("Top must be a positive number or 'full'")

        chat_id = update.effective_chat.id
        await update.message.reply_text(
            f"Scanning volume anomalies for {symbol if symbol else 'coins'} on {timeframe}" +
            (f" (top {top} by market cap)" if top and top != "full" else " (all coins)" if top == "full" else f" (top {DEFAULT_TOP} by market cap)") +
            "..."
        )
        await scan_coins(symbol, timeframe, top, chat_id, context.application)
    except Exception as e:
        await update.message.reply_text(f"Error: {str(e)}")

# /start command
async def start(update, context):
    chat_id = update.effective_chat.id
    CHAT_IDS.add(chat_id)  # Store chat ID for scheduled scans
    await update.message.reply_text(
        "Welcome to Crypto Breakout Bot!\n"
        "Commands:\n"
        "/scan [symbol] [timeframe] [top] - Scan volume anomalies manually (e.g., /scan 4h 100, /scan 4h full, or /scan BTCUSDT 4h)\n"
        "/help - Show this message\n\n"
        "Default: Scans top 50 coins by market cap unless 'full' is specified.\n"
        "Scanning volume anomalies (1h, 4h, 12h) every hour."
    )

# /help command
async def help_command(update, context):
    await start(update, context)

# Run scheduled scans for all chat IDs
async def run_scheduled_scan(timeframe, application):
    try:
        for chat_id in CHAT_IDS:
            await scan_coins(None, timeframe, DEFAULT_TOP, chat_id, application)  # Use default top 50 for scheduled scans
    except Exception:
        pass  # Ignore errors during scheduled scans

# Run schedule in a separate thread
def run_schedule(application):
    while True:
        try:
            schedule.run_pending()
            time.sleep(1)
        except Exception:
            time.sleep(60)  # Wait before retrying

# Main function
def main():
    try:
        # Initialize Telegram application
        application = Application.builder().token(TELEGRAM_TOKEN).build()
        application.add_handler(CommandHandler("start", start))
        application.add_handler(CommandHandler("scan", scan))
        application.add_handler(CommandHandler("help", help_command))
        
        # Schedule scans
        for tf in ['1h', '4h', '12h', '1d']:
            schedule.every().hour.at(":00").do(
                lambda timeframe=tf: application.run_async(run_scheduled_scan, timeframe, application)
            )
        
        # Start schedule in a separate thread
        Thread(target=run_schedule, args=(application,), daemon=True).start()
        
        # Run bot with polling
        application.run_polling(allowed_updates=telegram.Update.ALL_TYPES)
    except Exception as e:
        raise

if __name__ == '__main__':
    main()