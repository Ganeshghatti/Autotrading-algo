from kiteconnect import KiteTicker, KiteConnect
from datetime import datetime, timedelta
import talib
import numpy as np
import os
import sys
from dotenv import load_dotenv
import json
import signal
import atexit
import logging
import time
import threading

# Get project root directory (parent of utils folder)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

# Add project root to path for imports (must be before importing utils modules)
sys.path.insert(0, PROJECT_ROOT)
# Also add current directory in case running from utils/ directory
if SCRIPT_DIR not in sys.path:
    sys.path.insert(0, SCRIPT_DIR)

# Import after path setup to ensure it works from any directory
try:
    from utils.email_utils import send_trade_notification
except ImportError:
    # Fallback: try direct import if running from utils directory
    try:
        from email_utils import send_trade_notification
    except ImportError:
        # Last resort: add parent directory explicitly
        sys.path.insert(0, os.path.join(SCRIPT_DIR, '..'))
        from utils.email_utils import send_trade_notification

# ============================================================================
# TRADING MODE CONFIGURATION
# ============================================================================
# Set to False for paper trading (saves to JSON file)
# Set to True for live trading (places actual orders on Kite)
LIVE_TRADING = False  # CHANGE THIS TO True FOR LIVE TRADING

# ============================================================================
# TRADING PARAMETERS FOR NIFTY FUTURES
# ============================================================================
# TRADING_PRODUCT Explanation:
#   - MIS (Margin Intraday Square-off): Intraday trading, auto square-off at 3:20 PM
#   - NRML (Normal/Carry Forward): Position can be held overnight
#   - CNC (Cash and Carry): For equity delivery (not applicable for futures)
# 
# For NIFTY Futures: Use MIS for intraday or NRML for overnight positions
# 1 lot of NIFTY = 50 shares, so quantity should be 50 for 1 lot
TRADING_PRODUCT = os.getenv("TRADING_PRODUCT", "MIS")  # MIS (intraday) or NRML (overnight)
TRADING_LOTS = int(os.getenv("TRADING_LOTS", "1"))  # Number of lots (minimum 1 lot)
TRADING_QUANTITY = TRADING_LOTS * 50  # NIFTY lot size is 50, so 1 lot = 50 shares

# Simple file reading function
def read_from_file(filename):
    """Read file from project root"""
    filepath = os.path.join(PROJECT_ROOT, filename) if not os.path.isabs(filename) else filename
    with open(filepath, 'r') as file:
        return file.read().strip()

# Load environment variables (from project root)
env_path = os.path.join(PROJECT_ROOT, '.env')
load_dotenv(env_path)

# Global state for trading strategy
candles = []  # Store OHLC candles for RSI calculation (loaded from file on startup)
open_trade = None  # Current open trade (only 1 trade at a time)
pending_alert = None  # Alert candle waiting for next candle to check entry
current_candle = None  # Current 5-minute candle being built
current_candle_start = None  # Start time of current candle
instrument_token = None
interval_minutes = 5  # 5-minute candles
kite = None  # KiteConnect instance for live trading
CANDLES_FILE = os.path.join(PROJECT_ROOT, "candles_data.json")  # File to store candles
LOG_FILE = os.path.join(PROJECT_ROOT, "websocket_server.log")  # Log file
kws = None  # KiteTicker instance
reconnect_interval = 600  # 10 minutes in seconds
should_reconnect = True  # Flag to control reconnection
reconnect_thread = None  # Thread for reconnection
is_connected = False  # Track if WebSocket is currently connected

# Setup logging to both file and console
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

def format_time(dt):
    """Format datetime (uses system timezone)"""
    return dt.strftime('%Y-%m-%d %H:%M:%S')

def initialize_candle(tick_time):
    """Initialize a new candle"""
    return {
        'open': None,
        'high': None,
        'low': None,
        'close': None,
        'volume': 0,
        'date': tick_time,
        'timestamp': tick_time
    }

def update_candle_with_tick(candle, tick):
    """Update candle OHLC with new tick data"""
    price = tick.get('last_price', 0)
    if price == 0:
        return candle
    
    # Use tick's OHLC data if available (more accurate)
    tick_ohlc = tick.get('ohlc', {})
    if tick_ohlc and tick_ohlc.get('open') is not None:
        tick_open = tick_ohlc.get('open')
        tick_high = tick_ohlc.get('high')
        tick_low = tick_ohlc.get('low')
        tick_close = tick_ohlc.get('close')
        
        if candle['open'] is None and tick_open is not None:
            candle['open'] = tick_open
        
        if tick_high is not None:
            candle['high'] = tick_high if candle['high'] is None else max(candle['high'], tick_high)
        
        if tick_low is not None:
            candle['low'] = tick_low if candle['low'] is None else min(candle['low'], tick_low)
        
        if tick_close is not None:
            candle['close'] = tick_close
    else:
        # Fallback: build OHLC from last_price
        if candle['open'] is None:
            candle['open'] = price
            candle['high'] = price
            candle['low'] = price
            candle['close'] = price
        else:
            candle['high'] = max(candle['high'], price)
            candle['low'] = min(candle['low'], price)
            candle['close'] = price
    
    # Update volume
    volume_traded = tick.get('volume_traded', 0)
    if volume_traded > 0:
        candle['volume'] = volume_traded
    else:
        candle['volume'] += tick.get('volume', 0)
    
    return candle

def serialize_candle(candle):
    """Convert candle datetime objects to strings for JSON storage"""
    serialized = candle.copy()
    if isinstance(serialized.get('date'), datetime):
        serialized['date'] = serialized['date'].isoformat()
    if isinstance(serialized.get('timestamp'), datetime):
        serialized['timestamp'] = serialized['timestamp'].isoformat()
    return serialized

def deserialize_candle(candle_dict):
    """Convert candle string dates back to datetime objects"""
    deserialized = candle_dict.copy()
    if isinstance(deserialized.get('date'), str):
        try:
            deserialized['date'] = datetime.fromisoformat(deserialized['date'])
        except:
            try:
                deserialized['date'] = datetime.strptime(deserialized['date'].split('+')[0].strip(), '%Y-%m-%d %H:%M:%S')
            except:
                deserialized['date'] = datetime.now()
    if isinstance(deserialized.get('timestamp'), str):
        try:
            deserialized['timestamp'] = datetime.fromisoformat(deserialized['timestamp'])
        except:
            try:
                deserialized['timestamp'] = datetime.strptime(deserialized['timestamp'].split('+')[0].strip(), '%Y-%m-%d %H:%M:%S')
            except:
                deserialized['timestamp'] = datetime.now()
    return deserialized

def load_candles_from_file():
    """Load candles from JSON file"""
    global candles
    
    if not os.path.exists(CANDLES_FILE):
        logger.info("Candles file not found, starting with empty candles list")
        candles = []
        return
    
    try:
        with open(CANDLES_FILE, 'r') as f:
            candles_data = json.load(f)
            candles = [deserialize_candle(c) for c in candles_data]
            # Keep only last 50 candles in memory for RSI calculation
            if len(candles) > 50:
                candles = candles[-50:]
            logger.info(f"✅ Loaded {len(candles)} candles from {CANDLES_FILE}")
    except Exception as e:
        logger.error(f"Error loading candles from file: {e}")
        candles = []
        import traceback
        traceback.print_exc()

def save_candles_to_file():
    """Save candles to JSON file (keep last 50 for RSI calculation)"""
    global candles
    
    try:
        # Keep only last 50 candles to save
        candles_to_save = candles[-50:] if len(candles) > 50 else candles
        
        # Serialize candles (convert datetime to string)
        serialized_candles = [serialize_candle(c) for c in candles_to_save]
        
        with open(CANDLES_FILE, 'w') as f:
            json.dump(serialized_candles, f, indent=2)
        
        logger.debug(f"💾 Saved {len(candles_to_save)} candles to {CANDLES_FILE}")
    except Exception as e:
        logger.error(f"Error saving candles to file: {e}")
        import traceback
        traceback.print_exc()

def calculate_rsi_from_candles(candles_list, period=14):
    """Calculate RSI using TA-Lib from candle list"""
    if len(candles_list) < period + 1:
        logger.debug(f"Not enough candles for RSI: {len(candles_list)} < {period + 1}")
        return None
    
    closes = [candle['close'] for candle in candles_list if candle['close'] is not None]
    if len(closes) < period + 1:
        logger.debug(f"Not enough valid closes for RSI: {len(closes)} < {period + 1}")
        return None
    
    closes_array = np.array(closes, dtype=float)
    rsi = talib.RSI(closes_array, timeperiod=period)
    
    if len(rsi) > 0 and not np.isnan(rsi[-1]):
        rsi_value = float(rsi[-1])
        logger.info(f"[RSI] Calculated: {rsi_value:.2f} (from {len(candles_list)} candles)")
        return rsi_value
    logger.debug(f"RSI calculation returned NaN or empty")
    return None

def is_first_candle_of_day(candle_time):
    """Check if this is the first candle of the trading day (9:15 AM)"""
    if isinstance(candle_time, datetime):
        return candle_time.hour == 9 and candle_time.minute == 15
    return False

def is_after_325(candle_time):
    """Check if time is 3:25 PM or later (15:25)"""
    if isinstance(candle_time, datetime):
        return candle_time.hour > 15 or (candle_time.hour == 15 and candle_time.minute >= 25)
    return False

def check_alert_candle(candle, rsi):
    """Mark alert candle when RSI > 60 or RSI < 40"""
    if rsi is None:
        logger.debug(f"RSI is None, cannot check alert")
        return None
    
    logger.info(f"[ALERT CHECK] RSI={rsi:.2f}, Candle OHLC: O={candle.get('open')}, H={candle.get('high')}, L={candle.get('low')}, C={candle.get('close')}")
    
    if rsi > 60:
        alert = {
            'date': candle['date'],
            'open': candle['open'],
            'high': candle['high'],
            'low': candle['low'],
            'close': candle['close'],
            'rsi': rsi,
            'alert_type': 'BUY'
        }
        logger.info(f"[ALERT CHECK] ✅ BUY ALERT DETECTED: RSI={rsi:.2f} > 60")
        return alert
    elif rsi < 40:
        alert = {
            'date': candle['date'],
            'open': candle['open'],
            'high': candle['high'],
            'low': candle['low'],
            'close': candle['close'],
            'rsi': rsi,
            'alert_type': 'SELL'
        }
        logger.info(f"[ALERT CHECK] ✅ SELL ALERT DETECTED: RSI={rsi:.2f} < 40")
        return alert
    
    logger.debug(f"No alert: RSI={rsi:.2f} is between 40 and 60")
    return None

def get_tradingsymbol_from_token(instrument_token):
    """Get tradingsymbol from instrument_token for NIFTY futures"""
    global kite
    
    try:
        # Fetch all NFO instruments
        instruments = kite.instruments("NFO")
        
        # Find the instrument matching the token
        for inst in instruments:
            if inst['instrument_token'] == instrument_token:
                tradingsymbol = inst['tradingsymbol']
                exchange = inst['exchange']
                logger.debug(f"Found tradingsymbol: {tradingsymbol} on {exchange} for token {instrument_token}")
                return tradingsymbol, exchange
        
        logger.error(f"Could not find tradingsymbol for instrument_token: {instrument_token}")
        return None, None
    except Exception as e:
        logger.error(f"Error fetching instruments: {e}")
        import traceback
        traceback.print_exc()
        return None, None

def execute_trade_order(trade_type, entry_price, instrument_token):
    """Execute trade order - either paper trading or live trading"""
    global kite
    
    if LIVE_TRADING:
        # ========================================================================
        # LIVE TRADING: Place actual order on Kite for NIFTY Futures
        # ========================================================================
        try:
            # Get tradingsymbol from instrument_token
            tradingsymbol, exchange = get_tradingsymbol_from_token(instrument_token)
            if not tradingsymbol:
                logger.error(f"[LIVE TRADING] ❌ Cannot place order: tradingsymbol not found")
                return None
            
            # Map product string to KiteConnect constant
            if TRADING_PRODUCT == "MIS":
                product = kite.PRODUCT_MIS
            elif TRADING_PRODUCT == "NRML":
                product = kite.PRODUCT_NRML
            else:
                product = kite.PRODUCT_MIS  # Default to MIS
            
            logger.info(f"[LIVE TRADING] Placing {trade_type} order for NIFTY Futures:")
            logger.info(f"  - Tradingsymbol: {tradingsymbol}")
            logger.info(f"  - Exchange: {exchange}")
            logger.info(f"  - Quantity: {TRADING_QUANTITY} ({TRADING_LOTS} lot(s))")
            logger.info(f"  - Price: {entry_price}")
            logger.info(f"  - Product: {TRADING_PRODUCT}")
            
            order_id = kite.place_order(
                variety=kite.VARIETY_REGULAR,
                exchange=exchange,  # NFO for futures
                tradingsymbol=tradingsymbol,
                transaction_type=kite.TRANSACTION_TYPE_BUY if trade_type == 'BUY' else kite.TRANSACTION_TYPE_SELL,
                quantity=TRADING_QUANTITY,  # 50 for 1 lot of NIFTY
                price=round(entry_price, 2),  # Round to 2 decimal places
                product=product,
                order_type=kite.ORDER_TYPE_LIMIT,
                validity=kite.VALIDITY_DAY
            )
            logger.info(f"[LIVE TRADING] ✅ Order placed successfully: Order ID={order_id}")
            return order_id
        except Exception as e:
            logger.error(f"[LIVE TRADING] ❌ Error placing order: {e}")
            import traceback
            traceback.print_exc()
            return None
    else:
        # ========================================================================
        # PAPER TRADING: Save to JSON file
        # ========================================================================
        try:
            paper_trade = {
                "trade_id": f"PT_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}",
                "instrument_token": instrument_token,
                "quantity": TRADING_QUANTITY,
                "lots": TRADING_LOTS,
                "transaction_type": trade_type,
                "order_type": "LIMIT",
                "product": TRADING_PRODUCT,
                "price": entry_price,
                "timestamp": datetime.now().isoformat(),
                "status": "PAPER_TRADE_OPEN"
            }
            
            filename = "paper_trades.json"
            paper_trades = []
            if os.path.exists(filename):
                with open(filename, 'r') as f:
                    try:
                        paper_trades = json.load(f)
                    except:
                        paper_trades = []
            
            paper_trades.append(paper_trade)
            
            with open(filename, 'w') as f:
                json.dump(paper_trades, f, indent=2)
            
            logger.info(f"[PAPER TRADING] ✅ Trade saved to {filename}: {trade_type} @ {entry_price}")
            return paper_trade['trade_id']
        except Exception as e:
            logger.error(f"[PAPER TRADING] ❌ Error saving paper trade: {e}")
            return None

def check_trade_entry(current_candle, alert):
    """Check if we should enter a trade based on alert candle"""
    global open_trade, instrument_token
    
    # Risk management: Only 1 trade at a time
    if open_trade is not None:
        logger.debug(f"⚠️  Cannot enter trade: Already have open trade: {open_trade['type']} @ {open_trade['entry_price']}")
        return
    
    current_high = current_candle.get('high', 0)
    current_low = current_candle.get('low', 0)
    
    logger.info(f"[TRADE ENTRY] Checking: Alert={alert['alert_type']}, Alert High={alert['high']}, Alert Low={alert['low']}, Current High={current_high}, Current Low={current_low}")
    
    if alert['alert_type'] == 'BUY':
        # BUY when high of next candle crosses high of alert candle
        if current_high > alert['high']:
            logger.info(f"[TRADE ENTRY] ✅ BUY ENTRY CONDITION MET: Current High ({current_high}) > Alert High ({alert['high']})")
            open_trade = {
                'type': 'BUY',
                'alert_date': alert['date'],
                'alert_high': alert['high'],
                'alert_low': alert['low'],
                'alert_rsi': alert['rsi'],
                'entry_price': alert['high'],
                'entry_date': current_candle['date'],
                'stop_loss': alert['low'],  # Stop Loss: Low of alert candle
                'target': alert['high'] + 10,  # Target: +10 points
                'status': 'OPEN',
                'instrument_token': instrument_token
            }
            logger.info(f"[TRADE ENTRY] 🟢 BUY TRADE ENTERED at {format_time(datetime.now())}: Entry={open_trade['entry_price']}, SL={open_trade['stop_loss']}, Target={open_trade['target']}")
            
            # Execute trade order
            order_id = execute_trade_order('BUY', open_trade['entry_price'], instrument_token)
            open_trade['order_id'] = order_id
            
            try:
                send_trade_notification('ENTRY', open_trade)
            except Exception as e:
                logger.error(f"Error sending trade entry email: {e}")
        else:
            logger.debug(f"⏳ BUY entry condition not met: Current High ({current_high}) <= Alert High ({alert['high']})")
    
    elif alert['alert_type'] == 'SELL':
        # SELL when low of next candle crosses low of alert candle
        if current_low < alert['low']:
            logger.info(f"[TRADE ENTRY] ✅ SELL ENTRY CONDITION MET: Current Low ({current_low}) < Alert Low ({alert['low']})")
            open_trade = {
                'type': 'SELL',
                'alert_date': alert['date'],
                'alert_high': alert['high'],
                'alert_low': alert['low'],
                'alert_rsi': alert['rsi'],
                'entry_price': alert['low'],
                'entry_date': current_candle['date'],
                'stop_loss': alert['high'],  # Stop Loss: High of alert candle
                'target': alert['low'] - 10,  # Target: +10 points (downside)
                'status': 'OPEN',
                'instrument_token': instrument_token
            }
            logger.info(f"[TRADE ENTRY] 🔴 SELL TRADE ENTERED at {format_time(datetime.now())}: Entry={open_trade['entry_price']}, SL={open_trade['stop_loss']}, Target={open_trade['target']}")
            
            # Execute trade order
            order_id = execute_trade_order('SELL', open_trade['entry_price'], instrument_token)
            open_trade['order_id'] = order_id
            
            try:
                send_trade_notification('ENTRY', open_trade)
            except Exception as e:
                logger.error(f"Error sending trade entry email: {e}")
        else:
            logger.debug(f"⏳ SELL entry condition not met: Current Low ({current_low}) >= Alert Low ({alert['low']})")

def check_trade_exit(current_candle):
    """Check if we should exit the current trade (SL or Target)"""
    global open_trade
    
    if open_trade is None:
        return
    
    high = current_candle.get('high', 0)
    low = current_candle.get('low', 0)
    
    logger.info(f"[TRADE EXIT] Checking: Type={open_trade['type']}, Entry={open_trade['entry_price']}, SL={open_trade['stop_loss']}, Target={open_trade['target']}, Candle High={high}, Candle Low={low}")
    
    if open_trade['type'] == 'BUY':
        if low <= open_trade['stop_loss']:
            # Stop loss hit
            logger.info(f"[TRADE EXIT] 🛑 BUY STOP LOSS HIT: Candle Low ({low}) <= SL ({open_trade['stop_loss']})")
            open_trade['exit_price'] = open_trade['stop_loss']
            open_trade['exit_date'] = current_candle['date']
            open_trade['pnl'] = open_trade['exit_price'] - open_trade['entry_price']
            open_trade['status'] = 'STOP_LOSS'
            logger.info(f"[TRADE EXIT] 🔴 BUY TRADE EXIT - STOP LOSS at {format_time(datetime.now())}: Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']:.2f}")
            try:
                send_trade_notification('EXIT', open_trade.copy())
            except Exception as e:
                logger.error(f"Error sending trade exit email: {e}")
            open_trade = None
        elif high >= open_trade['target']:
            # Target hit
            logger.info(f"[TRADE EXIT] 🎯 BUY TARGET HIT: Candle High ({high}) >= Target ({open_trade['target']})")
            open_trade['exit_price'] = open_trade['target']
            open_trade['exit_date'] = current_candle['date']
            open_trade['pnl'] = open_trade['exit_price'] - open_trade['entry_price']
            open_trade['status'] = 'TARGET'
            logger.info(f"[TRADE EXIT] 🟢 BUY TRADE EXIT - TARGET at {format_time(datetime.now())}: Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']:.2f}")
            try:
                send_trade_notification('EXIT', open_trade.copy())
            except Exception as e:
                logger.error(f"Error sending trade exit email: {e}")
            open_trade = None
        else:
            logger.debug(f"⏳ BUY trade still open: Low ({low}) > SL ({open_trade['stop_loss']}) and High ({high}) < Target ({open_trade['target']})")
    
    elif open_trade['type'] == 'SELL':
        if high >= open_trade['stop_loss']:
            # Stop loss hit
            logger.info(f"[TRADE EXIT] 🛑 SELL STOP LOSS HIT: Candle High ({high}) >= SL ({open_trade['stop_loss']})")
            open_trade['exit_price'] = open_trade['stop_loss']
            open_trade['exit_date'] = current_candle['date']
            open_trade['pnl'] = open_trade['entry_price'] - open_trade['exit_price']
            open_trade['status'] = 'STOP_LOSS'
            logger.info(f"[TRADE EXIT] 🔴 SELL TRADE EXIT - STOP LOSS at {format_time(datetime.now())}: Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']:.2f}")
            try:
                send_trade_notification('EXIT', open_trade.copy())
            except Exception as e:
                logger.error(f"Error sending trade exit email: {e}")
            open_trade = None
        elif low <= open_trade['target']:
            # Target hit
            logger.info(f"[TRADE EXIT] 🎯 SELL TARGET HIT: Candle Low ({low}) <= Target ({open_trade['target']})")
            open_trade['exit_price'] = open_trade['target']
            open_trade['exit_date'] = current_candle['date']
            open_trade['pnl'] = open_trade['entry_price'] - open_trade['exit_price']
            open_trade['status'] = 'TARGET'
            logger.info(f"[TRADE EXIT] 🟢 SELL TRADE EXIT - TARGET at {format_time(datetime.now())}: Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']:.2f}")
            try:
                send_trade_notification('EXIT', open_trade.copy())
            except Exception as e:
                logger.error(f"Error sending trade exit email: {e}")
            open_trade = None
        else:
            logger.debug(f"⏳ SELL trade still open: High ({high}) < SL ({open_trade['stop_loss']}) and Low ({low}) > Target ({open_trade['target']})")

def exit_trade_at_325(trade, exit_price, exit_time):
    """Exit trade at 3:25 PM"""
    global open_trade
    
    open_trade['exit_price'] = exit_price
    open_trade['exit_date'] = exit_time
    if open_trade['type'] == 'BUY':
        open_trade['pnl'] = open_trade['exit_price'] - open_trade['entry_price']
    else:
        open_trade['pnl'] = open_trade['entry_price'] - open_trade['exit_price']
    open_trade['status'] = 'EXIT_325'
    logger.info(f"TRADE EXIT - 3:25 PM at {format_time(datetime.now())}: Type={open_trade['type']}, Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']:.2f}")
    try:
        send_trade_notification('EXIT', open_trade.copy())
    except Exception as e:
        logger.error(f"Error sending trade exit email: {e}")
    open_trade = None

def process_candle_complete(candle):
    """Process a completed 5-minute candle"""
    global pending_alert, current_candle, open_trade
    
    logger.info(f"[CANDLE PROCESS] Processing completed candle: {format_time(candle['date'])}, OHLC: O={candle.get('open')}, H={candle.get('high')}, L={candle.get('low')}, C={candle.get('close')}")
    
    # Rule 1: Skip first candle of day
    if is_first_candle_of_day(candle['date']):
        logger.info(f"[CANDLE PROCESS] Skipping first candle of day: {format_time(candle['date'])}")
        return
    
    # Time exit rule: At 3:25 PM, close all ongoing trades
    if is_after_325(candle['date']):
        logger.info(f"[CANDLE PROCESS] After 3:25 PM, closing trades if any")
        if open_trade:
            close_price = candle.get('close', 0)
            if close_price > 0:
                exit_trade_at_325(open_trade, close_price, candle['date'])
        pending_alert = None
        return
    
    # Add to candles list for RSI calculation
    candles.append(candle)
    if len(candles) > 50:
        candles.pop(0)  # Keep only last 50 in memory
    
    logger.info(f"[CANDLE PROCESS] Total candles in memory: {len(candles)}")
    
    # Save candles to file after each candle completion
    save_candles_to_file()
    
    # Calculate RSI
    logger.info(f"[CANDLE PROCESS] Calculating RSI from {len(candles)} candles...")
    rsi = calculate_rsi_from_candles(candles, period=14)
    
    # Rule 2: Mark alert candle when RSI > 60 or RSI < 40
    if rsi is not None:
        logger.info(f"[CANDLE PROCESS] RSI calculated: {rsi:.2f}, checking for alerts...")
        alert = check_alert_candle(candle, rsi)
        if alert:
            # Rule 3: High - Low of alert candle should be less than 40 points
            candle_range = alert['high'] - alert['low']
            logger.info(f"[ALERT CHECK] Range check: High={alert['high']}, Low={alert['low']}, Range={candle_range:.2f}")
            if candle_range < 40:
                pending_alert = alert
                logger.info(f"[ALERT] ✅ ALERT CANDLE VALIDATED at {format_time(datetime.now())}: Type={alert['alert_type']}, RSI={alert['rsi']:.2f}, High={alert['high']}, Low={alert['low']}, Range={candle_range:.2f} (< 40)")
            else:
                logger.info(f"[ALERT] ❌ Alert candle REJECTED: Range={candle_range:.2f} >= 40 (too large)")
        else:
            logger.debug(f"[CANDLE PROCESS] No alert conditions met (RSI={rsi:.2f})")
    else:
        logger.debug(f"[CANDLE PROCESS] RSI is None, cannot check for alerts")
    
    # Rule 4 & 5: Check for trade entry from pending alert (next candle after alert)
    if pending_alert:
        if open_trade is None:
            logger.info(f"[TRADE ENTRY] Checking trade entry for pending alert: Type={pending_alert['alert_type']}, Alert High={pending_alert['high']}, Alert Low={pending_alert['low']}")
            check_trade_entry(candle, pending_alert)
            if open_trade:
                pending_alert = None  # Alert used
                logger.info(f"[TRADE ENTRY] ✅ Trade entered, pending alert cleared")
        else:
            logger.debug(f"⚠️  Pending alert exists but trade already open, skipping entry check")
    
    # Check for trade exit on completed candle
    if open_trade:
        logger.info(f"[TRADE EXIT] Checking trade exit on completed candle")
        check_trade_exit(candle)
    
    # Store current candle for next tick processing
    current_candle = candle

def on_ticks(ws, ticks):
    """Handle incoming ticks"""
    global current_candle, current_candle_start, open_trade
    
    for tick in ticks:
        tick_token = tick.get('instrument_token')
        if tick_token != instrument_token:
            continue
        
        # Get tick time (uses system timezone)
        tick_time = tick.get('exchange_timestamp') or tick.get('last_trade_time')
        if not tick_time:
            tick_time = datetime.now()
        
        # Convert to datetime if string
        if isinstance(tick_time, str):
            try:
                tick_time = datetime.strptime(tick_time.split('+')[0].strip(), '%Y-%m-%d %H:%M:%S')
            except:
                tick_time = datetime.now()
        
        # Round down to 5-minute interval
        candle_start_time = tick_time.replace(second=0, microsecond=0)
        candle_start_time = candle_start_time.replace(minute=(candle_start_time.minute // interval_minutes) * interval_minutes)
        
        # Initialize first candle
        if current_candle_start is None:
            current_candle_start = candle_start_time
            current_candle = initialize_candle(current_candle_start)
            logger.info(f"New 5-minute candle started: {format_time(current_candle_start)}")
        
        # Check if we need to start a new candle
        candle_end = current_candle_start + timedelta(minutes=interval_minutes)
        if tick_time >= candle_end:
            # Process completed candle
            if current_candle['open'] is not None:
                logger.info(f"5-minute candle completed: {format_time(current_candle_start)} - {format_time(candle_end)}, OHLC: O={current_candle.get('open')}, H={current_candle.get('high')}, L={current_candle.get('low')}, C={current_candle.get('close')}")
                process_candle_complete(current_candle)
            
            # Start new candle
            current_candle_start = candle_end
            current_candle = initialize_candle(current_candle_start)
            logger.info(f"New 5-minute candle started: {format_time(current_candle_start)}")
        
        # Update current candle with tick
        current_candle = update_candle_with_tick(current_candle, tick)
        
        # Time exit rule: At 3:25 PM, close all ongoing trades
        if open_trade and is_after_325(tick_time):
            price = tick.get('last_price', 0)
            if price > 0:
                exit_trade_at_325(open_trade, price, tick_time)
        
        # Check for trade exit on current tick (real-time SL/Target)
        if open_trade:
            price = tick.get('last_price', 0)
            if price > 0:
                if open_trade['type'] == 'BUY':
                    if price <= open_trade['stop_loss']:
                        open_trade['exit_price'] = open_trade['stop_loss']
                        open_trade['exit_date'] = tick_time
                        open_trade['pnl'] = open_trade['exit_price'] - open_trade['entry_price']
                        open_trade['status'] = 'STOP_LOSS'
                        logger.info(f"BUY TRADE EXIT - STOP LOSS (TICK) at {format_time(tick_time)}: Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']:.2f}")
                        try:
                            send_trade_notification('EXIT', open_trade.copy())
                        except Exception as e:
                            logger.error(f"Error sending trade exit email: {e}")
                        open_trade = None
                    elif price >= open_trade['target']:
                        open_trade['exit_price'] = open_trade['target']
                        open_trade['exit_date'] = tick_time
                        open_trade['pnl'] = open_trade['exit_price'] - open_trade['entry_price']
                        open_trade['status'] = 'TARGET'
                        logger.info(f"BUY TRADE EXIT - TARGET (TICK) at {format_time(tick_time)}: Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']:.2f}")
                        try:
                            send_trade_notification('EXIT', open_trade.copy())
                        except Exception as e:
                            logger.error(f"Error sending trade exit email: {e}")
                        open_trade = None
                
                elif open_trade['type'] == 'SELL':
                    if price >= open_trade['stop_loss']:
                        open_trade['exit_price'] = open_trade['stop_loss']
                        open_trade['exit_date'] = tick_time
                        open_trade['pnl'] = open_trade['entry_price'] - open_trade['exit_price']
                        open_trade['status'] = 'STOP_LOSS'
                        logger.info(f"SELL TRADE EXIT - STOP LOSS (TICK) at {format_time(tick_time)}: Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']:.2f}")
                        try:
                            send_trade_notification('EXIT', open_trade.copy())
                        except Exception as e:
                            logger.error(f"Error sending trade exit email: {e}")
                        open_trade = None
                    elif price <= open_trade['target']:
                        open_trade['exit_price'] = open_trade['target']
                        open_trade['exit_date'] = tick_time
                        open_trade['pnl'] = open_trade['entry_price'] - open_trade['exit_price']
                        open_trade['status'] = 'TARGET'
                        logger.info(f"SELL TRADE EXIT - TARGET (TICK) at {format_time(tick_time)}: Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']:.2f}")
                        try:
                            send_trade_notification('EXIT', open_trade.copy())
                        except Exception as e:
                            logger.error(f"Error sending trade exit email: {e}")
                        open_trade = None

def on_connect(ws, response):
    """Handle websocket connection"""
    global instrument_token, reconnect_thread, is_connected
    
    logger.info(f"✅ WebSocket connected successfully: {response} at {format_time(datetime.now())}")
    
    # Mark as connected - this will stop reconnection attempts
    is_connected = True
    
    instrument_token = int(os.getenv("INSTRUMENT_TOKEN", "12683010"))
    
    try:
        ws.subscribe([instrument_token])
        ws.set_mode(ws.MODE_FULL, [instrument_token])
        logger.info(f"✅ Subscribed to instrument_token: {instrument_token} at {format_time(datetime.now())}")
        logger.info("✅ WebSocket connection fully established and ready for trading")
    except Exception as e:
        logger.error(f"❌ Error subscribing to instrument: {e}")

def on_close(ws, code, reason):
    """Handle websocket close"""
    global is_connected
    
    logger.warning(f"⚠️  WebSocket closed: code={code}, reason={reason} at {format_time(datetime.now())}")
    
    # Mark as disconnected
    is_connected = False
    
    # Trigger reconnection if we should reconnect
    if should_reconnect:
        logger.info(f"🔄 Connection lost. Will attempt to reconnect in {reconnect_interval // 60} minutes...")
        # Start reconnection thread if not already running
        start_reconnect_thread()
    else:
        ws.stop()

def on_error(ws, code, reason):
    """Handle websocket errors"""
    global is_connected
    
    logger.error(f"❌ WebSocket error: code={code}, reason={reason} at {format_time(datetime.now())}")
    
    # Mark as disconnected on error
    is_connected = False
    
    # Trigger reconnection if needed
    if should_reconnect:
        logger.info(f"🔄 Error detected. Will attempt to reconnect in {reconnect_interval // 60} minutes...")
        start_reconnect_thread()

def reconnect_websocket():
    """Reconnect to websocket with retry logic"""
    global kws, should_reconnect, reconnect_thread, is_connected
    
    # Wait for the reconnect interval before attempting
    logger.info(f"⏳ Waiting {reconnect_interval // 60} minutes before reconnection attempt...")
    time.sleep(reconnect_interval)
    
    if not should_reconnect:
        logger.info("🛑 Reconnection cancelled (should_reconnect=False)")
        return
    
    while should_reconnect and not is_connected:
        try:
            # Check if connection was established by another thread/process
            if is_connected:
                logger.info("✅ Connection already established, stopping reconnection thread")
                break
            
            logger.info(f"🔄 Attempting to reconnect WebSocket at {format_time(datetime.now())}...")
            
            api_key = os.getenv("API_KEY")
            access_token_path = os.path.join(PROJECT_ROOT, "access_token.txt")
            access_token = read_from_file(access_token_path)
            
            if not api_key or not access_token:
                logger.error("❌ Missing API credentials, cannot reconnect")
                logger.info(f"🔄 Will retry again in {reconnect_interval // 60} minutes...")
                time.sleep(reconnect_interval)
                continue
            
            # Close existing connection if any (only if not connected)
            if kws and not is_connected:
                try:
                    kws.close()
                except:
                    pass
            
            # Create new KiteTicker instance
            kws = KiteTicker(api_key, access_token.strip())
            kws.on_ticks = on_ticks
            kws.on_connect = on_connect
            kws.on_close = on_close
            kws.on_error = on_error
            
            # Attempt to connect
            kws.connect(threaded=True)
            logger.info("✅ Reconnection attempt initiated, waiting for connection confirmation...")
            
            # Wait a bit to see if connection succeeds
            # If on_connect is called, is_connected will be set to True
            time.sleep(10)
            
            # Check if connection was successful
            if is_connected:
                logger.info("✅ Reconnection successful! Connection established.")
                break
            
            # If still not connected, wait and retry
            if should_reconnect and not is_connected:
                logger.info(f"🔄 Connection attempt failed. Will retry again in {reconnect_interval // 60} minutes...")
                time.sleep(reconnect_interval)
            
        except Exception as e:
            logger.error(f"❌ Reconnection failed: {e}")
            if should_reconnect and not is_connected:
                logger.info(f"🔄 Will retry again in {reconnect_interval // 60} minutes...")
                time.sleep(reconnect_interval)
    
    if is_connected:
        logger.info("🛑 Reconnection loop stopped (connection established)")
    else:
        logger.info("🛑 Reconnection loop stopped (should_reconnect=False)")

def start_reconnect_thread():
    """Start reconnection thread (only if not already running)"""
    global reconnect_thread, should_reconnect
    
    if reconnect_thread is None or not reconnect_thread.is_alive():
        should_reconnect = True
        reconnect_thread = threading.Thread(target=reconnect_websocket, daemon=True)
        reconnect_thread.start()
        logger.info("🔄 Reconnection thread started")
    else:
        logger.debug("🔄 Reconnection thread already running")

def start_websocket_server():
    """Start the websocket server with trading strategy"""
    global kite, kws
    
    api_key = os.getenv("API_KEY")
    access_token_path = os.path.join(PROJECT_ROOT, "access_token.txt")
    access_token = read_from_file(access_token_path)
    
    if not api_key:
        raise ValueError("API_KEY must be set in .env file")
    if not access_token:
        raise ValueError("Access token not found. Please login first.")
    
    # Load candles from file on startup
    logger.info(f"[INIT] Loading candles from file at {format_time(datetime.now())}...")
    load_candles_from_file()
    
    # Initialize KiteConnect for live trading
    if LIVE_TRADING:
        logger.warning("[INIT] 🔴 LIVE TRADING MODE ENABLED - Real orders will be placed!")
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token.strip())
        logger.info("[INIT] ✅ KiteConnect initialized for live trading")
    else:
        logger.info("[INIT] 📝 PAPER TRADING MODE - Trades will be saved to paper_trades.json")
        logger.info("[INIT] ⚠️  To enable live trading, set LIVE_TRADING = True at the top of websocket_server.py")
    
    logger.info(f"[INIT] Starting WebSocket server with RSI trading strategy at {format_time(datetime.now())}")
    logger.info(f"[INIT] Trading Configuration:")
    logger.info(f"  - Lots: {TRADING_LOTS} lot(s)")
    logger.info(f"  - Quantity: {TRADING_QUANTITY} shares (1 lot = 50 shares for NIFTY)")
    logger.info(f"  - Product: {TRADING_PRODUCT} ({'Intraday' if TRADING_PRODUCT == 'MIS' else 'Overnight' if TRADING_PRODUCT == 'NRML' else 'Unknown'})")
    logger.info(f"  - Mode: {'🔴 LIVE TRADING' if LIVE_TRADING else '📝 PAPER TRADING'}")
    logger.info(f"  - Candles Storage: {CANDLES_FILE} ({len(candles)} candles loaded)")
    logger.info(f"  - Log File: {LOG_FILE}")
    logger.info(f"  - Reconnection: Enabled (retry every {reconnect_interval // 60} minutes on failure)")
    
    # Create and connect WebSocket
    try:
        kws = KiteTicker(api_key, access_token.strip())
        kws.on_ticks = on_ticks
        kws.on_connect = on_connect
        kws.on_close = on_close
        kws.on_error = on_error
        
        logger.info("🔄 Attempting to connect to Kite WebSocket...")
        kws.connect(threaded=True)
        logger.info("✅ WebSocket connection initiated")
        logger.info("ℹ️  If connection fails, automatic reconnection will be attempted every 10 minutes")
        
        # Wait a moment to see if initial connection succeeds
        time.sleep(5)
        
        # Only start reconnection thread if initial connection failed
        if not is_connected:
            logger.warning("⚠️  Initial connection may have failed, starting reconnection thread...")
            start_reconnect_thread()
        else:
            logger.info("✅ Initial connection successful, reconnection thread not needed")
        
    except Exception as e:
        logger.error(f"❌ Error starting WebSocket: {e}")
        logger.info(f"🔄 Will attempt to reconnect in {reconnect_interval // 60} minutes...")
        # Start reconnection thread to handle retry
        start_reconnect_thread()
    
    return kws

def cleanup_on_exit():
    """Save candles before exiting"""
    global should_reconnect, kws
    
    logger.info(f"[SHUTDOWN] Saving candles before exit at {format_time(datetime.now())}...")
    should_reconnect = False  # Stop reconnection attempts
    save_candles_to_file()
    
    # Close websocket connection
    if kws:
        try:
            kws.close()
        except:
            pass
    
    logger.info("[SHUTDOWN] ✅ Candles saved successfully, WebSocket closed")

def signal_handler(sig, frame):
    """Handle shutdown signals"""
    logger.info(f"[SHUTDOWN] Received shutdown signal at {format_time(datetime.now())}...")
    cleanup_on_exit()
    sys.exit(0)

if __name__ == "__main__":
    # Register cleanup handlers
    atexit.register(cleanup_on_exit)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    try:
        kws = start_websocket_server()
        
        # Keep the main thread alive and monitor connection
        logger.info("🟢 WebSocket server running. Monitoring connection...")
        while True:
            time.sleep(10)  # Check every 10 seconds
            
            # Check if websocket is still connected
            # Note: KiteTicker doesn't expose connection status directly
            # The on_close callback will handle reconnection
            
    except KeyboardInterrupt:
        logger.info("\n[SHUTDOWN] Stopping WebSocket server...")
        cleanup_on_exit()
    except Exception as e:
        logger.error(f"❌ Fatal error in main loop: {e}")
        logger.info("🔄 Will attempt to reconnect...")
        cleanup_on_exit()
        # Restart after a delay
        time.sleep(reconnect_interval)
        # The reconnection thread will handle retry
