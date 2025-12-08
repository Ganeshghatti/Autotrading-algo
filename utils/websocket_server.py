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

# ============================================================================
# GLOBAL STATE FOR TRADING STRATEGY
# ============================================================================
candles = []  # Store OHLC candles for RSI calculation (max 50 in memory, loaded from file on startup)
open_trade = None  # Current open trade (only 1 trade at a time)
pending_alert = None  # Alert candle waiting for next candle to check entry
current_candle = None  # Current 5-minute candle being built from ticks
current_candle_start = None  # Start time of current candle
instrument_token = None  # NIFTY futures instrument token
interval_minutes = 5  # 5-minute candles
kite = None  # KiteConnect instance for live trading
CANDLES_FILE = os.path.join(PROJECT_ROOT, "candles_data.json")  # File to store candles (persistent storage)
LOG_FILE = os.path.join(PROJECT_ROOT, "websocket_server.log")  # Log file
kws = None  # KiteTicker WebSocket instance
reconnect_interval = 600  # 10 minutes in seconds
should_reconnect = True  # Flag to control automatic reconnection
reconnect_thread = None  # Background thread for reconnection
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

# ============================================================================
# CANDLE DATA MANAGEMENT
# ============================================================================
# Candle Data Flow:
# 1. Ticks arrive via WebSocket -> on_ticks()
# 2. Ticks update current_candle -> update_candle_with_tick()
# 3. When 5 minutes complete, current_candle is finalized
# 4. Valid candles are added to candles list -> process_candle_complete()
# 5. Duplicates are removed and candles saved to JSON -> save_candles_to_file()
# 6. On startup, candles are loaded from JSON -> load_candles_from_file()
#
# Key Principles:
# - Only valid candles (with complete OHLC) are saved
# - Duplicates are removed based on timestamp
# - Maximum 50 candles kept in memory for RSI calculation
# - Candles are saved atomically (temp file + rename)
# ============================================================================

def format_time(dt):
    """Format datetime (uses system timezone)"""
    return dt.strftime('%Y-%m-%d %H:%M:%S')

def is_valid_candle(candle):
    """Validate that a candle has all required fields and valid data"""
    if not candle:
        return False
    
    # Must have timestamp
    if not candle.get('date') and not candle.get('timestamp'):
        return False
    
    # Must have OHLC data
    if candle.get('open') is None:
        return False
    if candle.get('high') is None:
        return False
    if candle.get('low') is None:
        return False
    if candle.get('close') is None:
        return False
    
    # Validate OHLC relationships
    try:
        o, h, l, c = candle['open'], candle['high'], candle['low'], candle['close']
        
        # High should be >= all other prices
        if h < o or h < l or h < c:
            return False
        
        # Low should be <= all other prices
        if l > o or l > h or l > c:
            return False
        
        # All prices should be positive
        if o <= 0 or h <= 0 or l <= 0 or c <= 0:
            return False
            
    except (TypeError, KeyError):
        return False
    
    return True

def initialize_candle(tick_time, initial_price=None):
    """Initialize a new candle with optional starting price"""
    if initial_price and initial_price > 0:
        # If we have an initial price, use it for OHLC
        return {
            'open': initial_price,
            'high': initial_price,
            'low': initial_price,
            'close': initial_price,
            'volume': 0,
            'date': tick_time,
            'timestamp': tick_time
        }
    else:
        # No price yet, set to None
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
    if not candle or not tick:
        return candle
    
    price = tick.get('last_price', 0)
    if not price or price <= 0:
        return candle
    
    # Initialize OHLC if not set (shouldn't happen with new initialization)
    if candle['open'] is None:
        candle['open'] = candle['high'] = candle['low'] = candle['close'] = price
    else:
        # Update high, low, close (open never changes after first set)
        candle['high'] = max(candle['high'], price)
        candle['low'] = min(candle['low'], price)
        candle['close'] = price
    
    # Update volume (use cumulative volume from exchange)
    candle['volume'] = tick.get('volume_traded', 0) or candle['volume']
    
    return candle

def serialize_candle(candle):
    """Convert candle datetime objects to strings for JSON storage"""
    serialized = candle.copy()
    for key in ['date', 'timestamp']:
        if isinstance(serialized.get(key), datetime):
            serialized[key] = serialized[key].isoformat()
    return serialized

def deserialize_candle(candle_dict):
    """Convert candle string dates back to datetime objects"""
    deserialized = candle_dict.copy()
    for key in ['date', 'timestamp']:
        if isinstance(deserialized.get(key), str):
            try:
                deserialized[key] = datetime.fromisoformat(deserialized[key])
            except:
                deserialized[key] = datetime.now()
    return deserialized

def load_candles_from_file():
    """Load candles from JSON file"""
    global candles
    
    # Always start with empty list
    candles = []
    
    if not os.path.exists(CANDLES_FILE):
        logger.info("Candles file not found, starting with empty candles list")
        return
    
    try:
        with open(CANDLES_FILE, 'r') as f:
            candles_data = json.load(f)
            
            # Validate that we have actual candle data, not empty or corrupted
            if not candles_data or not isinstance(candles_data, list):
                logger.warning("Candles file is empty or invalid, starting with empty list")
                return
            
            if len(candles_data) == 0:
                logger.info("Candles file contains no data, starting with empty list")
                return
            
            # Deserialize and validate each candle
            loaded_candles = []
            for idx, candle_dict in enumerate(candles_data):
                try:
                    candle = deserialize_candle(candle_dict)
                    
                    # Validate candle has required fields
                    if candle.get('open') is None or candle.get('close') is None:
                        logger.debug(f"Skipping invalid candle at index {idx} (missing OHLC)")
                        continue
                    
                    if not candle.get('date') and not candle.get('timestamp'):
                        logger.debug(f"Skipping candle at index {idx} (missing timestamp)")
                        continue
                    
                    loaded_candles.append(candle)
                except Exception as e:
                    logger.warning(f"Error deserializing candle at index {idx}: {e}")
                    continue
            
            if len(loaded_candles) == 0:
                logger.warning("No valid candles found in file, starting with empty list")
                return
            
            # Deduplicate loaded candles
            unique_candles = deduplicate_candles(loaded_candles)
            
            # Keep only last 50 candles in memory for RSI calculation
            if len(unique_candles) > 50:
                candles = unique_candles[-50:]
            else:
                candles = unique_candles
            
            logger.info(f"✅ Loaded {len(candles)} valid candles from {CANDLES_FILE}")
            
    except json.JSONDecodeError as e:
        logger.error(f"Error parsing candles JSON file: {e}. Starting with empty list.")
        candles = []
    except Exception as e:
        logger.error(f"Error loading candles from file: {e}")
        candles = []
        import traceback
        traceback.print_exc()

def deduplicate_candles(candles_list):
    """Remove duplicate candles based on timestamp, keeping the most recent version
    
    Returns:
        list: Deduplicated candles sorted chronologically
    """
    if not candles_list:
        return []
    
    seen_timestamps = {}
    invalid_count = 0
    duplicate_count = 0
    
    for idx, candle in enumerate(candles_list):
        # Validate candle has required fields
        if not is_valid_candle(candle):
            invalid_count += 1
            logger.debug(f"💾 Skipping invalid candle at index {idx}")
            continue
        
        candle_timestamp = candle.get('date') or candle.get('timestamp')
        if not candle_timestamp:
            invalid_count += 1
            logger.debug(f"💾 Skipping candle without timestamp at index {idx}")
            continue
        
        # Convert to string key for comparison
        if isinstance(candle_timestamp, datetime):
            timestamp_key = candle_timestamp.isoformat()
        else:
            timestamp_key = str(candle_timestamp)
        
        # Track duplicates
        if timestamp_key in seen_timestamps:
            duplicate_count += 1
            logger.debug(f"💾 Found duplicate candle with timestamp: {timestamp_key}")
        
        # Keep the latest version of each timestamp (later index overwrites)
        seen_timestamps[timestamp_key] = candle
    
    # Return deduplicated candles in chronological order
    unique_candles = list(seen_timestamps.values())
    
    # Sort by timestamp to ensure chronological order
    try:
        unique_candles.sort(key=lambda c: c.get('date') or c.get('timestamp'))
    except Exception as e:
        logger.warning(f"💾 Error sorting candles: {e}")
    
    if invalid_count > 0 or duplicate_count > 0:
        logger.info(f"💾 Deduplication: {len(candles_list)} input -> {len(unique_candles)} unique (removed {invalid_count} invalid, {duplicate_count} duplicates)")
    
    return unique_candles

def save_candles_to_file(force=False):
    """Save candles to JSON file (keep last 50 for RSI calculation)
    
    Args:
        force: If True, save even outside market hours (used on shutdown)
    """
    global candles
    
    # Only save during market hours unless forced
    if not force:
        current_time = datetime.now()
        if not is_market_hours(current_time):
            logger.debug(f"💾 Skipping candle save - outside market hours")
            return
    
    try:
        # Validate candles list
        if not candles or len(candles) == 0:
            logger.debug(f"💾 No candles to save")
            return
        
        # Deduplicate and validate candles
        unique_candles = deduplicate_candles(candles)
        
        if len(unique_candles) == 0:
            logger.warning(f"💾 No valid candles after deduplication")
            return
        
        # Keep only last 50 unique candles to save
        candles_to_save = unique_candles[-50:] if len(unique_candles) > 50 else unique_candles
        
        # Serialize candles (convert datetime to string)
        serialized_candles = [serialize_candle(c) for c in candles_to_save]
        
        # Atomic write: write to temp file first, then rename
        temp_file = CANDLES_FILE + ".tmp"
        with open(temp_file, 'w') as f:
            json.dump(serialized_candles, f, indent=2)
        
        # Rename temp file to actual file (atomic on most systems)
        os.replace(temp_file, CANDLES_FILE)
        
        duplicates_removed = len(candles) - len(unique_candles)
        if duplicates_removed > 0:
            logger.info(f"💾 Saved {len(candles_to_save)} unique candles (removed {duplicates_removed} duplicates)")
        else:
            logger.info(f"💾 Saved {len(candles_to_save)} candles to {CANDLES_FILE}")
            
        # Update global candles list to match what was saved (remove duplicates from memory)
        candles = candles_to_save.copy()
        
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
    return isinstance(candle_time, datetime) and candle_time.hour == 9 and candle_time.minute == 15

def is_after_325(candle_time):
    """Check if time is 3:25 PM or later"""
    return isinstance(candle_time, datetime) and (candle_time.hour > 15 or (candle_time.hour == 15 and candle_time.minute >= 25))

def is_market_hours(candle_time):
    """Check if time is within market hours (9:15 AM to 3:25 PM) on weekdays"""
    if not isinstance(candle_time, datetime):
        return False
    
    # Check if weekend (Saturday=5, Sunday=6)
    if candle_time.weekday() >= 5:
        return False
    
    hour, minute = candle_time.hour, candle_time.minute
    
    # Market: 9:15 AM to 3:25 PM
    if hour < 9 or (hour == 9 and minute < 15):
        return False
    if hour > 15 or (hour == 15 and minute >= 25):
        return False
    
    return True

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
    global pending_alert, open_trade
    
    # Validate candle
    if not candle or not is_valid_candle(candle):
        return
    
    candle_date = candle.get('date')
    if not candle_date:
        return
    
    logger.info(f"✅ Completed Candle: {format_time(candle_date)} | O={candle['open']:.2f} H={candle['high']:.2f} L={candle['low']:.2f} C={candle['close']:.2f} | Vol={candle.get('volume', 0)}")
    
    # Skip processing outside market hours, first candle, or after 3:25 PM
    if not is_market_hours(candle_date):
        return
    
    if is_first_candle_of_day(candle_date):
        logger.info("Skipping first candle of day")
        return
    
    if is_after_325(candle_date):
        if open_trade and candle['close'] > 0:
            exit_trade_at_325(open_trade, candle['close'], candle_date)
        pending_alert = None
        return
    
    # Add candle to list (check for duplicates) - IMPORTANT: Add a COPY, not reference!
    candle_timestamp = candle_date.isoformat() if isinstance(candle_date, datetime) else str(candle_date)
    
    # Create a copy of the candle to prevent mutation issues
    candle_copy = candle.copy()
    
    for idx, existing in enumerate(candles):
        existing_ts = existing.get('date')
        if existing_ts:
            existing_key = existing_ts.isoformat() if isinstance(existing_ts, datetime) else str(existing_ts)
            if existing_key == candle_timestamp:
                candles[idx] = candle_copy
                logger.debug(f"Updated existing candle at index {idx}")
                return  # Early return after update
    
    # Add new candle copy
    candles.append(candle_copy)
    if len(candles) > 50:
        candles.pop(0)
    
    logger.info(f"Candles in memory: {len(candles)}")
    
    # Calculate RSI and save candles
    rsi = calculate_rsi_from_candles(candles, period=14)
    save_candles_to_file()
    
    # Check for alert (RSI > 60 or RSI < 40, range < 40 points)
    if rsi:
        alert = check_alert_candle(candle, rsi)
        if alert and (alert['high'] - alert['low']) < 40:
            pending_alert = alert
            logger.info(f"⚠️  ALERT: {alert['alert_type']} | RSI={rsi:.2f} | Range={alert['high'] - alert['low']:.2f}")
    
    # Check trade entry from pending alert
    if pending_alert and not open_trade:
        check_trade_entry(candle, pending_alert)
        if open_trade:
            pending_alert = None
    
    # Check trade exit
    if open_trade:
        check_trade_exit(candle)

def check_tick_exit(price, tick_time):
    """Check and execute trade exit based on current tick price"""
    global open_trade
    
    if not open_trade or not price or price <= 0:
        return
    
    trade_type = open_trade['type']
    sl_hit = (trade_type == 'BUY' and price <= open_trade['stop_loss']) or \
             (trade_type == 'SELL' and price >= open_trade['stop_loss'])
    target_hit = (trade_type == 'BUY' and price >= open_trade['target']) or \
                 (trade_type == 'SELL' and price <= open_trade['target'])
    
    if sl_hit:
        open_trade['exit_price'] = open_trade['stop_loss']
        open_trade['status'] = 'STOP_LOSS'
    elif target_hit:
        open_trade['exit_price'] = open_trade['target']
        open_trade['status'] = 'TARGET'
    else:
        return
    
    # Calculate P&L
    open_trade['exit_date'] = tick_time
    if trade_type == 'BUY':
        open_trade['pnl'] = open_trade['exit_price'] - open_trade['entry_price']
    else:
        open_trade['pnl'] = open_trade['entry_price'] - open_trade['exit_price']
    
    logger.info(f"🔴 {trade_type} EXIT - {open_trade['status']}: Entry={open_trade['entry_price']:.2f}, Exit={open_trade['exit_price']:.2f}, P&L={open_trade['pnl']:.2f}")
    
    try:
        send_trade_notification('EXIT', open_trade.copy())
    except Exception as e:
        logger.error(f"Error sending exit notification: {e}")
    
    open_trade = None

def on_ticks(ws, ticks):
    """Handle incoming ticks"""
    global current_candle, current_candle_start, open_trade
    
    for tick in ticks:
        if tick.get('instrument_token') != instrument_token:
            continue
        
        # Get tick time
        tick_time = tick.get('exchange_timestamp') or tick.get('last_trade_time') or datetime.now()
        if isinstance(tick_time, str):
            try:
                tick_time = datetime.strptime(tick_time.split('+')[0].strip(), '%Y-%m-%d %H:%M:%S')
            except:
                tick_time = datetime.now()
        
        # Skip if outside market hours
        if not is_market_hours(tick_time):
            continue
        
        # Get current price for candle initialization
        price = tick.get('last_price', 0)
        
        # Round to 5-minute interval
        candle_start_time = tick_time.replace(second=0, microsecond=0)
        candle_start_time = candle_start_time.replace(minute=(candle_start_time.minute // interval_minutes) * interval_minutes)
        
        # Initialize first candle with current price
        if current_candle_start is None:
            current_candle_start = candle_start_time
            current_candle = initialize_candle(current_candle_start, price)
            logger.info(f"🕐 New candle started: {format_time(current_candle_start)} at {price}")
        
        # Check if candle completed
        candle_end = current_candle_start + timedelta(minutes=interval_minutes)
        if tick_time >= candle_end:
            if is_valid_candle(current_candle):
                process_candle_complete(current_candle)
            else:
                logger.warning(f"⚠️  Invalid candle skipped: O={current_candle.get('open')} H={current_candle.get('high')} L={current_candle.get('low')} C={current_candle.get('close')}")
            
            # Start new candle with current price
            current_candle_start = candle_end
            current_candle = initialize_candle(current_candle_start, price)
            logger.info(f"🕐 New candle started: {format_time(current_candle_start)} at {price}")
        
        # Update current candle
        current_candle = update_candle_with_tick(current_candle, tick)
        
        # Check for trade exits
        price = tick.get('last_price', 0)
        if open_trade and is_after_325(tick_time) and price > 0:
            exit_trade_at_325(open_trade, price, tick_time)
        elif open_trade:
            check_tick_exit(price, tick_time)

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
    """Save candles before exiting (always save on shutdown, regardless of market hours)"""
    global should_reconnect, kws
    
    logger.info(f"[SHUTDOWN] Saving candles before exit at {format_time(datetime.now())}...")
    should_reconnect = False  # Stop reconnection attempts
    
    # Force save on shutdown (bypass market hours check)
    save_candles_to_file(force=True)
    
    # Close websocket connection
    if kws:
        try:
            kws.close()
        except:
            pass
    
    logger.info("[SHUTDOWN] ✅ WebSocket closed")

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
