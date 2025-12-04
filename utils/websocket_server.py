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
from utils.email_utils import send_trade_notification

# Get project root directory (parent of utils folder)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

# Add project root to path for imports
sys.path.insert(0, PROJECT_ROOT)

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
        print(f"[INIT] Candles file not found, starting with empty candles list")
        candles = []
        return
    
    try:
        with open(CANDLES_FILE, 'r') as f:
            candles_data = json.load(f)
            candles = [deserialize_candle(c) for c in candles_data]
            # Keep only last 50 candles in memory for RSI calculation
            if len(candles) > 50:
                candles = candles[-50:]
            print(f"[INIT] ✅ Loaded {len(candles)} candles from {CANDLES_FILE}")
    except Exception as e:
        print(f"[ERROR] Error loading candles from file: {e}")
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
        
        print(f"[SAVE] 💾 Saved {len(candles_to_save)} candles to {CANDLES_FILE}")
    except Exception as e:
        print(f"[ERROR] Error saving candles to file: {e}")
        import traceback
        traceback.print_exc()

def calculate_rsi_from_candles(candles_list, period=14):
    """Calculate RSI using TA-Lib from candle list"""
    if len(candles_list) < period + 1:
        print(f"[DEBUG] Not enough candles for RSI: {len(candles_list)} < {period + 1}")
        return None
    
    closes = [candle['close'] for candle in candles_list if candle['close'] is not None]
    if len(closes) < period + 1:
        print(f"[DEBUG] Not enough valid closes for RSI: {len(closes)} < {period + 1}")
        return None
    
    closes_array = np.array(closes, dtype=float)
    rsi = talib.RSI(closes_array, timeperiod=period)
    
    if len(rsi) > 0 and not np.isnan(rsi[-1]):
        rsi_value = float(rsi[-1])
        print(f"[DEBUG] RSI calculated: {rsi_value:.2f} (from {len(candles_list)} candles)")
        return rsi_value
    print(f"[DEBUG] RSI calculation returned NaN or empty")
    return None

def is_first_candle_of_day(candle_time):
    """Check if this is the first candle of the trading day (9:15 AM IST)"""
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
        print(f"[DEBUG] RSI is None, cannot check alert")
        return None
    
    print(f"[DEBUG] Checking alert candle: RSI={rsi:.2f}, Candle OHLC: O={candle.get('open')}, H={candle.get('high')}, L={candle.get('low')}, C={candle.get('close')}")
    
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
        print(f"[DEBUG] ✅ BUY ALERT DETECTED: RSI={rsi:.2f} > 60")
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
        print(f"[DEBUG] ✅ SELL ALERT DETECTED: RSI={rsi:.2f} < 40")
        return alert
    
    print(f"[DEBUG] No alert: RSI={rsi:.2f} is between 40 and 60")
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
                print(f"[DEBUG] Found tradingsymbol: {tradingsymbol} on {exchange} for token {instrument_token}")
                return tradingsymbol, exchange
        
        print(f"[ERROR] Could not find tradingsymbol for instrument_token: {instrument_token}")
        return None, None
    except Exception as e:
        print(f"[ERROR] Error fetching instruments: {e}")
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
                print(f"[LIVE TRADING] ❌ Cannot place order: tradingsymbol not found")
                return None
            
            # Map product string to KiteConnect constant
            if TRADING_PRODUCT == "MIS":
                product = kite.PRODUCT_MIS
            elif TRADING_PRODUCT == "NRML":
                product = kite.PRODUCT_NRML
            else:
                product = kite.PRODUCT_MIS  # Default to MIS
            
            print(f"[LIVE TRADING] Placing {trade_type} order for NIFTY Futures:")
            print(f"  - Tradingsymbol: {tradingsymbol}")
            print(f"  - Exchange: {exchange}")
            print(f"  - Quantity: {TRADING_QUANTITY} ({TRADING_LOTS} lot(s))")
            print(f"  - Price: {entry_price}")
            print(f"  - Product: {TRADING_PRODUCT}")
            
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
            print(f"[LIVE TRADING] ✅ Order placed successfully: Order ID={order_id}")
            return order_id
        except Exception as e:
            print(f"[LIVE TRADING] ❌ Error placing order: {e}")
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
            
            print(f"[PAPER TRADING] ✅ Trade saved to {filename}: {trade_type} @ {entry_price}")
            return paper_trade['trade_id']
        except Exception as e:
            print(f"[PAPER TRADING] ❌ Error saving paper trade: {e}")
            return None

def check_trade_entry(current_candle, alert):
    """Check if we should enter a trade based on alert candle"""
    global open_trade, instrument_token
    
    # Risk management: Only 1 trade at a time
    if open_trade is not None:
        print(f"[DEBUG] ⚠️  Cannot enter trade: Already have open trade: {open_trade['type']} @ {open_trade['entry_price']}")
        return
    
    current_high = current_candle.get('high', 0)
    current_low = current_candle.get('low', 0)
    
    print(f"[DEBUG] Checking trade entry: Alert={alert['alert_type']}, Alert High={alert['high']}, Alert Low={alert['low']}, Current High={current_high}, Current Low={current_low}")
    
    if alert['alert_type'] == 'BUY':
        # BUY when high of next candle crosses high of alert candle
        if current_high > alert['high']:
            print(f"[DEBUG] ✅ BUY ENTRY CONDITION MET: Current High ({current_high}) > Alert High ({alert['high']})")
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
            print(f"[TRADE ENTRY] 🟢 BUY TRADE ENTERED: Entry={open_trade['entry_price']}, SL={open_trade['stop_loss']}, Target={open_trade['target']}")
            
            # Execute trade order
            order_id = execute_trade_order('BUY', open_trade['entry_price'], instrument_token)
            open_trade['order_id'] = order_id
            
            try:
                send_trade_notification('ENTRY', open_trade)
            except Exception as e:
                print(f"[ERROR] Error sending trade entry email: {e}")
        else:
            print(f"[DEBUG] ⏳ BUY entry condition not met: Current High ({current_high}) <= Alert High ({alert['high']})")
    
    elif alert['alert_type'] == 'SELL':
        # SELL when low of next candle crosses low of alert candle
        if current_low < alert['low']:
            print(f"[DEBUG] ✅ SELL ENTRY CONDITION MET: Current Low ({current_low}) < Alert Low ({alert['low']})")
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
            print(f"[TRADE ENTRY] 🔴 SELL TRADE ENTERED: Entry={open_trade['entry_price']}, SL={open_trade['stop_loss']}, Target={open_trade['target']}")
            
            # Execute trade order
            order_id = execute_trade_order('SELL', open_trade['entry_price'], instrument_token)
            open_trade['order_id'] = order_id
            
            try:
                send_trade_notification('ENTRY', open_trade)
            except Exception as e:
                print(f"[ERROR] Error sending trade entry email: {e}")
        else:
            print(f"[DEBUG] ⏳ SELL entry condition not met: Current Low ({current_low}) >= Alert Low ({alert['low']})")

def check_trade_exit(current_candle):
    """Check if we should exit the current trade (SL or Target)"""
    global open_trade
    
    if open_trade is None:
        return
    
    high = current_candle.get('high', 0)
    low = current_candle.get('low', 0)
    
    print(f"[DEBUG] Checking trade exit: Type={open_trade['type']}, Entry={open_trade['entry_price']}, SL={open_trade['stop_loss']}, Target={open_trade['target']}, Candle High={high}, Candle Low={low}")
    
    if open_trade['type'] == 'BUY':
        if low <= open_trade['stop_loss']:
            # Stop loss hit
            print(f"[DEBUG] 🛑 BUY STOP LOSS HIT: Candle Low ({low}) <= SL ({open_trade['stop_loss']})")
            open_trade['exit_price'] = open_trade['stop_loss']
            open_trade['exit_date'] = current_candle['date']
            open_trade['pnl'] = open_trade['exit_price'] - open_trade['entry_price']
            open_trade['status'] = 'STOP_LOSS'
            print(f"[TRADE EXIT] 🔴 BUY TRADE EXIT - STOP LOSS: Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']:.2f}")
            try:
                send_trade_notification('EXIT', open_trade.copy())
            except Exception as e:
                print(f"[ERROR] Error sending trade exit email: {e}")
            open_trade = None
        elif high >= open_trade['target']:
            # Target hit
            print(f"[DEBUG] 🎯 BUY TARGET HIT: Candle High ({high}) >= Target ({open_trade['target']})")
            open_trade['exit_price'] = open_trade['target']
            open_trade['exit_date'] = current_candle['date']
            open_trade['pnl'] = open_trade['exit_price'] - open_trade['entry_price']
            open_trade['status'] = 'TARGET'
            print(f"[TRADE EXIT] 🟢 BUY TRADE EXIT - TARGET: Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']:.2f}")
            try:
                send_trade_notification('EXIT', open_trade.copy())
            except Exception as e:
                print(f"[ERROR] Error sending trade exit email: {e}")
            open_trade = None
        else:
            print(f"[DEBUG] ⏳ BUY trade still open: Low ({low}) > SL ({open_trade['stop_loss']}) and High ({high}) < Target ({open_trade['target']})")
    
    elif open_trade['type'] == 'SELL':
        if high >= open_trade['stop_loss']:
            # Stop loss hit
            print(f"[DEBUG] 🛑 SELL STOP LOSS HIT: Candle High ({high}) >= SL ({open_trade['stop_loss']})")
            open_trade['exit_price'] = open_trade['stop_loss']
            open_trade['exit_date'] = current_candle['date']
            open_trade['pnl'] = open_trade['entry_price'] - open_trade['exit_price']
            open_trade['status'] = 'STOP_LOSS'
            print(f"[TRADE EXIT] 🔴 SELL TRADE EXIT - STOP LOSS: Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']:.2f}")
            try:
                send_trade_notification('EXIT', open_trade.copy())
            except Exception as e:
                print(f"[ERROR] Error sending trade exit email: {e}")
            open_trade = None
        elif low <= open_trade['target']:
            # Target hit
            print(f"[DEBUG] 🎯 SELL TARGET HIT: Candle Low ({low}) <= Target ({open_trade['target']})")
            open_trade['exit_price'] = open_trade['target']
            open_trade['exit_date'] = current_candle['date']
            open_trade['pnl'] = open_trade['entry_price'] - open_trade['exit_price']
            open_trade['status'] = 'TARGET'
            print(f"[TRADE EXIT] 🟢 SELL TRADE EXIT - TARGET: Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']:.2f}")
            try:
                send_trade_notification('EXIT', open_trade.copy())
            except Exception as e:
                print(f"[ERROR] Error sending trade exit email: {e}")
            open_trade = None
        else:
            print(f"[DEBUG] ⏳ SELL trade still open: High ({high}) < SL ({open_trade['stop_loss']}) and Low ({low}) > Target ({open_trade['target']})")

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
    print(f"TRADE EXIT - 3:25 PM: Type={open_trade['type']}, Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']}")
    try:
        send_trade_notification('EXIT', open_trade.copy())
    except Exception as e:
        print(f"Error sending trade exit email: {e}")
    open_trade = None

def process_candle_complete(candle):
    """Process a completed 5-minute candle"""
    global pending_alert, current_candle, open_trade
    
    # Rule 1: Skip first candle of day
    if is_first_candle_of_day(candle['date']):
        print(f"Skipping first candle of day: {candle['date']}")
        return
    
    # Time exit rule: At 3:25 PM, close all ongoing trades
    if is_after_325(candle['date']):
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
    
    # Save candles to file after each candle completion
    save_candles_to_file()
    
    # Calculate RSI
    rsi = calculate_rsi_from_candles(candles, period=14)
    
    # Rule 2: Mark alert candle when RSI > 60 or RSI < 40
    if rsi is not None:
        alert = check_alert_candle(candle, rsi)
        if alert:
            # Rule 3: High - Low of alert candle should be less than 40 points
            candle_range = alert['high'] - alert['low']
            print(f"[DEBUG] Alert candle range check: High={alert['high']}, Low={alert['low']}, Range={candle_range:.2f}")
            if candle_range < 40:
                pending_alert = alert
                print(f"[ALERT] ✅ ALERT CANDLE VALIDATED: Type={alert['alert_type']}, RSI={alert['rsi']:.2f}, High={alert['high']}, Low={alert['low']}, Range={candle_range:.2f} (< 40)")
            else:
                print(f"[ALERT] ❌ Alert candle REJECTED: Range={candle_range:.2f} >= 40 (too large)")
    
    # Rule 4 & 5: Check for trade entry from pending alert (next candle after alert)
    if pending_alert:
        if open_trade is None:
            print(f"[DEBUG] Checking trade entry for pending alert: Type={pending_alert['alert_type']}, Alert High={pending_alert['high']}, Alert Low={pending_alert['low']}")
            check_trade_entry(candle, pending_alert)
            if open_trade:
                pending_alert = None  # Alert used
                print(f"[DEBUG] ✅ Trade entered, pending alert cleared")
        else:
            print(f"[DEBUG] ⚠️  Pending alert exists but trade already open, skipping entry check")
    
    # Check for trade exit on completed candle
    if open_trade:
        print(f"[DEBUG] Checking trade exit on completed candle")
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
        
        # Get tick time
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
            print(f"New 5-minute candle started: {current_candle_start.strftime('%H:%M')}")
        
        # Check if we need to start a new candle
        candle_end = current_candle_start + timedelta(minutes=interval_minutes)
        if tick_time >= candle_end:
            # Process completed candle
            if current_candle['open'] is not None:
                print(f"5-minute candle completed: {current_candle_start.strftime('%H:%M')} - {candle_end.strftime('%H:%M')}, OHLC: O={current_candle.get('open')}, H={current_candle.get('high')}, L={current_candle.get('low')}, C={current_candle.get('close')}")
                process_candle_complete(current_candle)
            
            # Start new candle
            current_candle_start = candle_end
            current_candle = initialize_candle(current_candle_start)
            print(f"New 5-minute candle started: {current_candle_start.strftime('%H:%M')}")
        
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
                        print(f"BUY TRADE EXIT - STOP LOSS (TICK): Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']}")
                        try:
                            send_trade_notification('EXIT', open_trade.copy())
                        except Exception as e:
                            print(f"Error sending trade exit email: {e}")
                        open_trade = None
                    elif price >= open_trade['target']:
                        open_trade['exit_price'] = open_trade['target']
                        open_trade['exit_date'] = tick_time
                        open_trade['pnl'] = open_trade['exit_price'] - open_trade['entry_price']
                        open_trade['status'] = 'TARGET'
                        print(f"BUY TRADE EXIT - TARGET (TICK): Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']}")
                        try:
                            send_trade_notification('EXIT', open_trade.copy())
                        except Exception as e:
                            print(f"Error sending trade exit email: {e}")
                        open_trade = None
                
                elif open_trade['type'] == 'SELL':
                    if price >= open_trade['stop_loss']:
                        open_trade['exit_price'] = open_trade['stop_loss']
                        open_trade['exit_date'] = tick_time
                        open_trade['pnl'] = open_trade['entry_price'] - open_trade['exit_price']
                        open_trade['status'] = 'STOP_LOSS'
                        print(f"SELL TRADE EXIT - STOP LOSS (TICK): Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']}")
                        try:
                            send_trade_notification('EXIT', open_trade.copy())
                        except Exception as e:
                            print(f"Error sending trade exit email: {e}")
                        open_trade = None
                    elif price <= open_trade['target']:
                        open_trade['exit_price'] = open_trade['target']
                        open_trade['exit_date'] = tick_time
                        open_trade['pnl'] = open_trade['entry_price'] - open_trade['exit_price']
                        open_trade['status'] = 'TARGET'
                        print(f"SELL TRADE EXIT - TARGET (TICK): Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']}")
                        try:
                            send_trade_notification('EXIT', open_trade.copy())
                        except Exception as e:
                            print(f"Error sending trade exit email: {e}")
                        open_trade = None

def on_connect(ws, response):
    """Handle websocket connection"""
    global instrument_token
    print(f"WebSocket connected: {response}")
    
    instrument_token = int(os.getenv("INSTRUMENT_TOKEN", "12683010"))
    
    ws.subscribe([instrument_token])
    ws.set_mode(ws.MODE_FULL, [instrument_token])
    print(f"Subscribed to instrument_token: {instrument_token}")

def on_close(ws, code, reason):
    """Handle websocket close"""
    print(f"WebSocket closed: {code} - {reason}")
    ws.stop()

def start_websocket_server():
    """Start the websocket server with trading strategy"""
    global kite
    
    api_key = os.getenv("API_KEY")
    access_token_path = os.path.join(PROJECT_ROOT, "access_token.txt")
    access_token = read_from_file(access_token_path)
    
    if not api_key:
        raise ValueError("API_KEY must be set in .env file")
    if not access_token:
        raise ValueError("Access token not found. Please login first.")
    
    # Load candles from file on startup
    print("[INIT] Loading candles from file...")
    load_candles_from_file()
    
    # Initialize KiteConnect for live trading
    if LIVE_TRADING:
        print("[INIT] 🔴 LIVE TRADING MODE ENABLED - Real orders will be placed!")
        kite = KiteConnect(api_key=api_key)
        kite.set_access_token(access_token.strip())
        print("[INIT] ✅ KiteConnect initialized for live trading")
    else:
        print("[INIT] 📝 PAPER TRADING MODE - Trades will be saved to paper_trades.json")
        print("[INIT] ⚠️  To enable live trading, set LIVE_TRADING = True at the top of websocket_server.py")
    
    kws = KiteTicker(api_key, access_token.strip())
    kws.on_ticks = on_ticks
    kws.on_connect = on_connect
    kws.on_close = on_close
    
    print(f"[INIT] Starting WebSocket server with RSI trading strategy...")
    print(f"[INIT] Trading Configuration:")
    print(f"  - Lots: {TRADING_LOTS} lot(s)")
    print(f"  - Quantity: {TRADING_QUANTITY} shares (1 lot = 50 shares for NIFTY)")
    print(f"  - Product: {TRADING_PRODUCT} ({'Intraday' if TRADING_PRODUCT == 'MIS' else 'Overnight' if TRADING_PRODUCT == 'NRML' else 'Unknown'})")
    print(f"  - Mode: {'🔴 LIVE TRADING' if LIVE_TRADING else '📝 PAPER TRADING'}")
    print(f"  - Candles Storage: {CANDLES_FILE} ({len(candles)} candles loaded)")
    kws.connect()
    return kws

def cleanup_on_exit():
    """Save candles before exiting"""
    print("\n[SHUTDOWN] Saving candles before exit...")
    save_candles_to_file()
    print("[SHUTDOWN] ✅ Candles saved successfully")

def signal_handler(sig, frame):
    """Handle shutdown signals"""
    print("\n[SHUTDOWN] Received shutdown signal...")
    cleanup_on_exit()
    sys.exit(0)

if __name__ == "__main__":
    # Register cleanup handlers
    atexit.register(cleanup_on_exit)
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    kws = start_websocket_server()
    try:
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("\n[SHUTDOWN] Stopping WebSocket server...")
        cleanup_on_exit()
        kws.close()
