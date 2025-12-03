from kiteconnect import KiteTicker
from datetime import datetime, timedelta
import talib
import numpy as np
import pandas as pd
import os
import sys
import json
from dotenv import load_dotenv
import gspread
from google.oauth2.service_account import Credentials

# Get project root directory (parent of utils folder)
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.dirname(SCRIPT_DIR)

# Add project root to path for imports
sys.path.insert(0, PROJECT_ROOT)

# Simple file reading function (no need for complex imports)
def read_from_file(filename):
    """Read file from project root"""
    filepath = os.path.join(PROJECT_ROOT, filename) if not os.path.isabs(filename) else filename
    with open(filepath, 'r') as file:
        return file.read().strip()

# Load environment variables (from project root)
env_path = os.path.join(PROJECT_ROOT, '.env')
load_dotenv(env_path)

# Google Sheets configuration
SPREADSHEET_ID = "1louYs2BoLFTO7hbUngJOBW5zhNoIwta8eJHRNmOr4I0"
SHEET_NAME = None  # Will use the default sheet (gid=610077776)
gsheet = None
worksheet = None
tick_row = 2  # Start writing from row 2 (row 1 is headers)

# Global state for trading strategy
candles = []  # Store OHLC candles
rsi_values = []  # Store RSI values
prev_rsi = None
open_trade = None
pending_alert = None
current_candle = None
current_candle_start = None
instrument_token = None
interval_minutes = 5  # 5-minute candles

def init_google_sheets():
    """Initialize Google Sheets connection"""
    global gsheet, worksheet
    
    try:
        # Load credentials from credentials.json (in project root)
        creds_path = os.path.join(PROJECT_ROOT, "credentials.json")
        if not os.path.exists(creds_path):
            print(f"Warning: credentials.json not found at {creds_path}")
            return False
        
        scope = ['https://spreadsheets.google.com/feeds',
                 'https://www.googleapis.com/auth/drive']
        creds = Credentials.from_service_account_file(creds_path, scopes=scope)
        gc = gspread.authorize(creds)
        
        # Open the spreadsheet
        gsheet = gc.open_by_key(SPREADSHEET_ID)
        
        # Get the worksheet (use the specific gid if needed, or first sheet)
        worksheets = gsheet.worksheets()
        if worksheets:
            worksheet = worksheets[0]  # Use first sheet
        else:
            worksheet = gsheet.add_worksheet(title="Trading Data", rows=1000, cols=20)
        
        # Set up headers if sheet is empty
        if worksheet.row_count < 2:
            headers = [
                "Timestamp", "Price", "Volume", "Open", "High", "Low", "Close",
                "RSI", "Alert Type", "Alert RSI", "Alert High", "Alert Low",
                "Trade Type", "Entry Price", "Exit Price", "Stop Loss", "Target", "P&L", "Status"
            ]
            worksheet.append_row(headers)
            print("Initialized Google Sheet with headers")
        
        print("Google Sheets connection established")
        return True
    except Exception as e:
        print(f"Error initializing Google Sheets: {e}")
        return False

def write_tick_to_sheet(tick, candle=None, rsi=None, alert=None, trade=None):
    """Write 5-minute candle data to Google Sheet (called once per completed candle)"""
    global worksheet, tick_row
    
    if worksheet is None:
        print("Warning: worksheet is None, cannot write to sheet")
        return

    print(f"Writing tick to sheet")
    print(f"Candle: {candle}")
    print(f"RSI: {rsi}")
    print(f"Alert: {alert}")
    print(f"Trade: {trade}")
    
    try:
        # Use candle timestamp if available, otherwise current time
        if candle and candle.get('date'):
            timestamp = candle['date']
            if isinstance(timestamp, datetime):
                tick_time = timestamp
            else:
                tick_time = datetime.now()
        else:
            tick_time = datetime.now()
        
        # Prepare row data for 5-minute candle
        row_data = [
            tick_time.strftime('%Y-%m-%d %H:%M:%S'),  # Timestamp
            candle['close'] if candle and candle.get('close') else '',  # Price (use close)
            candle['volume'] if candle and candle.get('volume') else 0,  # Volume
            candle['open'] if candle and candle.get('open') else '',  # Open
            candle['high'] if candle and candle.get('high') else '',  # High
            candle['low'] if candle and candle.get('low') else '',  # Low
            candle['close'] if candle and candle.get('close') else '',  # Close
            round(rsi, 2) if rsi else '',  # RSI
        ]
        
        # Alert columns
        if alert:
            row_data.extend([
                alert.get('alert_type', ''),  # Alert Type
                round(alert.get('rsi', 0), 2),  # Alert RSI
                alert.get('high', ''),  # Alert High
                alert.get('low', ''),  # Alert Low
            ])
        else:
            row_data.extend(['', '', '', ''])  # Empty alert columns
        
        # Trade columns
        if trade:
            row_data.extend([
                trade.get('type', ''),  # Trade Type
                trade.get('entry_price', ''),  # Entry Price
                trade.get('exit_price', ''),  # Exit Price
                trade.get('stop_loss', ''),  # Stop Loss
                trade.get('target', ''),  # Target
                round(trade.get('pnl', 0), 2) if trade.get('pnl') else '',  # P&L
                trade.get('status', ''),  # Status
            ])
        else:
            row_data.extend(['', '', '', '', '', '', ''])  # Empty trade columns
        
        # Append row to sheet
        worksheet.append_row(row_data)
        tick_row += 1
        print(f"✓ Written to Google Sheet: {tick_time.strftime('%H:%M:%S')}, RSI={round(rsi, 2) if rsi else 'N/A'}, OHLC: O={candle.get('open') if candle else 'N/A'}, H={candle.get('high') if candle else 'N/A'}, L={candle.get('low') if candle else 'N/A'}, C={candle.get('close') if candle else 'N/A'}")
        
    except Exception as e:
        print(f"Error writing to Google Sheet: {e}")
        import traceback
        traceback.print_exc()

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
    # Use last_price from tick
    price = tick.get('last_price', 0)
    if price == 0:
        return candle
    
    # Use tick's OHLC data if available (for more accurate candle data)
    tick_ohlc = tick.get('ohlc', {})
    if tick_ohlc and tick_ohlc.get('open') is not None:
        # Use tick's OHLC data (this is the current candle's OHLC from exchange)
        tick_open = tick_ohlc.get('open')
        tick_high = tick_ohlc.get('high')
        tick_low = tick_ohlc.get('low')
        tick_close = tick_ohlc.get('close')
        
        # Set open if not set
        if candle['open'] is None and tick_open is not None:
            candle['open'] = tick_open
        
        # Update high (handle None values)
        if tick_high is not None:
            if candle['high'] is None:
                candle['high'] = tick_high
            else:
                candle['high'] = max(candle['high'], tick_high)
        
        # Update low (handle None values)
        if tick_low is not None:
            if candle['low'] is None:
                candle['low'] = tick_low
            else:
                candle['low'] = min(candle['low'], tick_low)
        
        # Update close
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
    
    # Update volume from tick
    volume_traded = tick.get('volume_traded', 0)
    if volume_traded > 0:
        candle['volume'] = volume_traded  # Use cumulative volume from exchange
    else:
        candle['volume'] += tick.get('volume', 0)  # Fallback to incremental volume
    
    return candle

def calculate_rsi_from_candles(candles_list, period=14):
    """Calculate RSI using TA-Lib from candle list"""
    if len(candles_list) < period + 1:
        return None
    
    # Extract closes
    closes = [candle['close'] for candle in candles_list if candle['close'] is not None]
    if len(closes) < period + 1:
        return None
    
    closes_array = np.array(closes, dtype=float)
    rsi = talib.RSI(closes_array, timeperiod=period)
    
    # Return the last RSI value
    if len(rsi) > 0 and not np.isnan(rsi[-1]):
        return float(rsi[-1])
    return None

def is_first_candle_of_day(candle_time):
    """Check if this is the first candle of the trading day (9:15 AM IST)"""
    if isinstance(candle_time, datetime):
        # Check if it's 9:15 AM (first candle of trading day)
        # Market opens at 9:15 AM IST, so first 5-minute candle is 9:15-9:20
        return candle_time.hour == 9 and candle_time.minute == 15
    return False

def is_after_325(candle_time):
    """Check if time is 3:25 PM or later (15:25)"""
    if isinstance(candle_time, datetime):
        return candle_time.hour > 15 or (candle_time.hour == 15 and candle_time.minute >= 25)
    return False

def should_allow_trading(candle_time):
    """Check if trading is allowed (not after 3:25 PM)"""
    return not is_after_325(candle_time)

def check_alert_candle(candle, rsi):
    """Check if current candle is an alert candle (RSI crossing 60 or 40)"""
    global prev_rsi
    
    if rsi is None or prev_rsi is None:
        prev_rsi = rsi
        return None
    
    crossed_60_up = prev_rsi <= 60 and rsi > 60
    crossed_40_down = prev_rsi >= 40 and rsi < 40
    
    prev_rsi = rsi
    
    if crossed_60_up or crossed_40_down:
        alert = {
            'date': candle['date'],
            'open': candle['open'],
            'high': candle['high'],
            'low': candle['low'],
            'close': candle['close'],
            'rsi': rsi,
            'alert_type': 'BUY' if crossed_60_up else 'SELL'
        }
        return alert
    return None

def check_trade_entry(current_candle, alert):
    """Check if we should enter a trade based on alert candle"""
    global open_trade
    
    if open_trade is not None:
        return  # Already in a trade
    
    # Check range condition (high - low < 40)
    candle_range = alert['high'] - alert['low']
    if candle_range >= 40:
        return  # Range too large, skip
    
    current_high = current_candle.get('high', 0)
    current_low = current_candle.get('low', 0)
    
    if alert['alert_type'] == 'BUY':
        # BUY when current candle crosses high of alert candle
        if current_high > alert['high']:
            open_trade = {
                'type': 'BUY',
                'alert_date': alert['date'],
                'alert_high': alert['high'],
                'alert_low': alert['low'],
                'alert_rsi': alert['rsi'],
                'entry_price': alert['high'],
                'entry_date': current_candle['date'],
                'stop_loss': alert['low'],
                'target': alert['high'] + 10,
                'status': 'OPEN'
            }
            print(f"BUY TRADE ENTERED: Entry={open_trade['entry_price']}, SL={open_trade['stop_loss']}, Target={open_trade['target']}")
    
    elif alert['alert_type'] == 'SELL':
        # SELL when current candle crosses low of alert candle
        if current_low < alert['low']:
            open_trade = {
                'type': 'SELL',
                'alert_date': alert['date'],
                'alert_high': alert['high'],
                'alert_low': alert['low'],
                'alert_rsi': alert['rsi'],
                'entry_price': alert['low'],
                'entry_date': current_candle['date'],
                'stop_loss': alert['high'],
                'target': alert['low'] - 10,
                'status': 'OPEN'
            }
            print(f"SELL TRADE ENTERED: Entry={open_trade['entry_price']}, SL={open_trade['stop_loss']}, Target={open_trade['target']}")

def check_trade_exit(current_candle):
    """Check if we should exit the current trade (SL or Target)"""
    global open_trade
    
    if open_trade is None:
        return
    
    high = current_candle.get('high', 0)
    low = current_candle.get('low', 0)
    close = current_candle.get('close', 0)
    
    if open_trade['type'] == 'BUY':
        # Check for stop loss or target
        if low <= open_trade['stop_loss']:
            # Stop loss hit
            open_trade['exit_price'] = open_trade['stop_loss']
            open_trade['exit_date'] = current_candle['date']
            open_trade['pnl'] = open_trade['exit_price'] - open_trade['entry_price']
            open_trade['status'] = 'STOP_LOSS'
            print(f"BUY TRADE EXIT - STOP LOSS: Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']}")
            # Write trade exit to sheet
            rsi = calculate_rsi_from_candles(candles, period=14) if len(candles) >= 14 else None
            write_tick_to_sheet({}, current_candle, rsi, trade=open_trade)
            closed_trade = open_trade.copy()
            open_trade = None
        elif high >= open_trade['target']:
            # Target hit
            open_trade['exit_price'] = open_trade['target']
            open_trade['exit_date'] = current_candle['date']
            open_trade['pnl'] = open_trade['exit_price'] - open_trade['entry_price']
            open_trade['status'] = 'TARGET'
            print(f"BUY TRADE EXIT - TARGET: Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']}")
            # Write trade exit to sheet
            rsi = calculate_rsi_from_candles(candles, period=14) if len(candles) >= 14 else None
            write_tick_to_sheet({}, current_candle, rsi, trade=open_trade)
            closed_trade = open_trade.copy()
            open_trade = None
    
    elif open_trade['type'] == 'SELL':
        # Check for stop loss or target
        if high >= open_trade['stop_loss']:
            # Stop loss hit
            open_trade['exit_price'] = open_trade['stop_loss']
            open_trade['exit_date'] = current_candle['date']
            open_trade['pnl'] = open_trade['entry_price'] - open_trade['exit_price']
            open_trade['status'] = 'STOP_LOSS'
            print(f"SELL TRADE EXIT - STOP LOSS: Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']}")
            # Write trade exit to sheet
            rsi = calculate_rsi_from_candles(candles, period=14) if len(candles) >= 14 else None
            write_tick_to_sheet({}, current_candle, rsi, trade=open_trade)
            closed_trade = open_trade.copy()
            open_trade = None
        elif low <= open_trade['target']:
            # Target hit
            open_trade['exit_price'] = open_trade['target']
            open_trade['exit_date'] = current_candle['date']
            open_trade['pnl'] = open_trade['entry_price'] - open_trade['exit_price']
            open_trade['status'] = 'TARGET'
            print(f"SELL TRADE EXIT - TARGET: Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']}")
            # Write trade exit to sheet
            rsi = calculate_rsi_from_candles(candles, period=14) if len(candles) >= 14 else None
            write_tick_to_sheet({}, current_candle, rsi, trade=open_trade)
            closed_trade = open_trade.copy()
            open_trade = None

def process_candle_complete(candle):
    """Process a completed 5-minute candle"""
    global pending_alert, current_candle, open_trade
    
    # Skip first candle of day
    if is_first_candle_of_day(candle['date']):
        print(f"Skipping first candle of day: {candle['date']}")
        return
    
    # Check if time is 3:25 PM or later - exit any open trade
    if is_after_325(candle['date']):
        if open_trade:
            # Force exit at 3:25 PM
            close_price = candle.get('close', 0)
            if close_price > 0:
                open_trade['exit_price'] = close_price
                open_trade['exit_date'] = candle['date']
                if open_trade['type'] == 'BUY':
                    open_trade['pnl'] = open_trade['exit_price'] - open_trade['entry_price']
                else:
                    open_trade['pnl'] = open_trade['entry_price'] - open_trade['exit_price']
                open_trade['status'] = 'EXIT_325'
                print(f"TRADE EXIT - 3:25 PM: Type={open_trade['type']}, Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']}")
                rsi = calculate_rsi_from_candles(candles, period=14) if len(candles) >= 14 else None
                write_tick_to_sheet({}, candle, rsi, trade=open_trade)
                open_trade = None
        # Don't allow new trades after 3:25 PM
        pending_alert = None
        return
    
    # Add to candles list (keep last 50 for RSI calculation)
    candles.append(candle)
    if len(candles) > 50:
        candles.pop(0)
    
    # Calculate RSI on completed 5-minute candle
    rsi = calculate_rsi_from_candles(candles, period=14)
    
    # Write completed 5-minute candle to sheet (only once per candle)
    # Always write candle data, even if RSI is None (for first few candles)
    if candle['open'] is not None:
        print(f"Writing candle to sheet: {candle['date']}, Open={candle['open']}, RSI={rsi}")
        write_tick_to_sheet({}, candle, rsi, trade=open_trade if open_trade else None)
    else:
        print(f"Warning: Candle open is None, skipping sheet write for {candle['date']}")
    
    if rsi is not None:
        # Check for alert candle (only if trading is allowed)
        if should_allow_trading(candle['date']):
            alert = check_alert_candle(candle, rsi)
            if alert:
                # Check range condition
                candle_range = alert['high'] - alert['low']
                if candle_range < 40:
                    pending_alert = alert
                    print(f"ALERT CANDLE: Type={alert['alert_type']}, RSI={alert['rsi']:.2f}, High={alert['high']}, Low={alert['low']}, Range={candle_range:.2f}")
                    # Write alert to sheet
                    write_tick_to_sheet({}, candle, rsi, alert=alert)
    
    # Check for trade entry from pending alert (on previous candle) - only if trading allowed
    if pending_alert and open_trade is None and should_allow_trading(candle['date']):
        check_trade_entry(candle, pending_alert)
        if open_trade:
            # Write trade entry to sheet
            write_tick_to_sheet({}, candle, rsi, alert=pending_alert, trade=open_trade)
            pending_alert = None  # Alert used
    
    # Check for trade exit on completed candle
    if open_trade:
        check_trade_exit(candle)
        # If trade was closed, it's already written in check_trade_exit
    
    # Store current candle for next tick processing
    current_candle = candle

def on_ticks(ws, ticks):
    """Handle incoming ticks"""
    global current_candle, current_candle_start, open_trade
    
    for tick in ticks:
        tick_token = tick.get('instrument_token')
        if tick_token != instrument_token:
            continue
        
        # Use exchange timestamp from tick (IST time from exchange)
        tick_time = tick.get('exchange_timestamp') or tick.get('last_trade_time')
        if not tick_time:
            tick_time = datetime.now()
        
        # Ensure tick_time is datetime object
        if isinstance(tick_time, str):
            try:
                tick_time = datetime.strptime(tick_time.split('+')[0].strip(), '%Y-%m-%d %H:%M:%S')
            except:
                tick_time = datetime.now()
        
        # Round down to 5-minute interval (e.g., 14:12 -> 14:10, 14:17 -> 14:15, 14:22 -> 14:20)
        # This ensures candles align to :00, :05, :10, :15, :20, :25, :30, :35, :40, :45, :50, :55
        candle_start_time = tick_time.replace(second=0, microsecond=0)
        candle_start_time = candle_start_time.replace(minute=(candle_start_time.minute // interval_minutes) * interval_minutes)
        
        # Determine candle start time (round down to 5-minute interval)
        if current_candle_start is None:
            # Start of new candle
            current_candle_start = candle_start_time
            current_candle = initialize_candle(current_candle_start)
            print(f"New 5-minute candle started: {current_candle_start.strftime('%H:%M')}")
        
        # Check if we need to start a new candle (when tick time crosses into next 5-minute interval)
        # Example: if current candle is 14:10-14:15, and tick is at 14:15 or later, start new candle
        candle_end = current_candle_start + timedelta(minutes=interval_minutes)
        if tick_time >= candle_end:
            # Process completed 5-minute candle
            if current_candle['open'] is not None:
                print(f"5-minute candle completed: {current_candle_start.strftime('%H:%M')} - {candle_end.strftime('%H:%M')}, OHLC: O={current_candle.get('open')}, H={current_candle.get('high')}, L={current_candle.get('low')}, C={current_candle.get('close')}")
                process_candle_complete(current_candle)
            
            # Start new candle
            current_candle_start = candle_end
            current_candle = initialize_candle(current_candle_start)
            print(f"New 5-minute candle started: {current_candle_start.strftime('%H:%M')}")
        
        # Update current candle with tick
        current_candle = update_candle_with_tick(current_candle, tick)
        
        # Check if time is 3:25 PM or later - exit any open trade
        if open_trade and is_after_325(tick_time):
            # Force exit at 3:25 PM
            price = tick.get('last_price', 0)
            if price > 0:
                open_trade['exit_price'] = price
                open_trade['exit_date'] = tick_time
                if open_trade['type'] == 'BUY':
                    open_trade['pnl'] = open_trade['exit_price'] - open_trade['entry_price']
                else:
                    open_trade['pnl'] = open_trade['entry_price'] - open_trade['exit_price']
                open_trade['status'] = 'EXIT_325'
                print(f"TRADE EXIT - 3:25 PM (TICK): Type={open_trade['type']}, Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']}")
                # Note: Trade exit will be written when current candle completes
                closed_trade = open_trade.copy()
                open_trade = None
        
        # Check for trade exit on current tick (for real-time SL/Target)
        if open_trade:
            price = tick.get('last_price', 0)
            if price > 0:
                if open_trade['type'] == 'BUY':
                    if price <= open_trade['stop_loss']:
                        # Stop loss hit
                        open_trade['exit_price'] = open_trade['stop_loss']
                        open_trade['exit_date'] = tick_time
                        open_trade['pnl'] = open_trade['exit_price'] - open_trade['entry_price']
                        open_trade['status'] = 'STOP_LOSS'
                        print(f"BUY TRADE EXIT - STOP LOSS (TICK): Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']}")
                        # Note: Trade exit will be written when current candle completes
                        closed_trade = open_trade.copy()
                        open_trade = None
                    elif price >= open_trade['target']:
                        # Target hit
                        open_trade['exit_price'] = open_trade['target']
                        open_trade['exit_date'] = tick_time
                        open_trade['pnl'] = open_trade['exit_price'] - open_trade['entry_price']
                        open_trade['status'] = 'TARGET'
                        print(f"BUY TRADE EXIT - TARGET (TICK): Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']}")
                        # Note: Trade exit will be written when current candle completes
                        closed_trade = open_trade.copy()
                        open_trade = None
                
                elif open_trade['type'] == 'SELL':
                    if price >= open_trade['stop_loss']:
                        # Stop loss hit
                        open_trade['exit_price'] = open_trade['stop_loss']
                        open_trade['exit_date'] = tick_time
                        open_trade['pnl'] = open_trade['entry_price'] - open_trade['exit_price']
                        open_trade['status'] = 'STOP_LOSS'
                        print(f"SELL TRADE EXIT - STOP LOSS (TICK): Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']}")
                        # Note: Trade exit will be written when current candle completes
                        closed_trade = open_trade.copy()
                        open_trade = None
                    elif price <= open_trade['target']:
                        # Target hit
                        open_trade['exit_price'] = open_trade['target']
                        open_trade['exit_date'] = tick_time
                        open_trade['pnl'] = open_trade['entry_price'] - open_trade['exit_price']
                        open_trade['status'] = 'TARGET'
                        print(f"SELL TRADE EXIT - TARGET (TICK): Entry={open_trade['entry_price']}, Exit={open_trade['exit_price']}, P&L={open_trade['pnl']}")
                        # Note: Trade exit will be written when current candle completes
                        closed_trade = open_trade.copy()
                        open_trade = None

def on_connect(ws, response):
    """Handle websocket connection"""
    global instrument_token
    print(f"WebSocket connected: {response}")
    
    # Get instrument token from environment or use default
    instrument_token = int(os.getenv("INSTRUMENT_TOKEN", "12683010"))  # Default to nearest NIFTY future
    
    ws.subscribe([instrument_token])
    ws.set_mode(ws.MODE_FULL, [instrument_token])
    print(f"Subscribed to instrument_token: {instrument_token}")

def on_close(ws, code, reason):
    """Handle websocket close"""
    print(f"WebSocket closed: {code} - {reason}")
    ws.stop()

def start_websocket_server():
    """Start the websocket server with trading strategy"""
    api_key = os.getenv("API_KEY")
    
    # Get access_token.txt path (in project root)
    access_token_path = os.path.join(PROJECT_ROOT, "access_token.txt")
    access_token = read_from_file(access_token_path)
    
    if not api_key:
        raise ValueError("API_KEY must be set in .env file")
    if not access_token:
        raise ValueError("Access token not found. Please login first.")
    
    # Initialize Google Sheets
    if not init_google_sheets():
        print("Warning: Google Sheets not initialized. Continuing without sheet logging.")
    
    kws = KiteTicker(api_key, access_token.strip())
    kws.on_ticks = on_ticks
    kws.on_connect = on_connect
    kws.on_close = on_close
    
    print("Starting WebSocket server with RSI trading strategy...")
    kws.connect()
    return kws

if __name__ == "__main__":
    # Run websocket server standalone
    kws = start_websocket_server()
    try:
        # Keep the connection alive
        import time
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        print("Stopping WebSocket server...")
        kws.close()
