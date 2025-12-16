#!/usr/bin/env python3
"""
Kite Data Fetcher Server with RSI-based Trading

Features:
1. Fetches NIFTY futures (current month) data every 5 minutes at aligned intervals
2. Calculates RSI (14-period) 
3. Places orders based on RSI strategy (real or paper trade)
4. Sends email notifications on trades

Run: python websocket_server.py
"""

import os
import sys
import time
import json
import logging
from datetime import datetime, timedelta
from kiteconnect import KiteConnect, KiteTicker
from dotenv import load_dotenv
import pandas as pd
import talib
import numpy as np
import threading

def read_from_file(filename):
    """Read content from a file"""
    try:
        with open(filename, 'r') as file:
            return file.read().strip()
    except FileNotFoundError:
        return None

# Import email utilities for trade notifications
try:
    sys.path.append(os.path.dirname(os.path.abspath(__file__)))
    from utils.email_utils import send_trade_notification
    EMAIL_ENABLED = True
except ImportError:
    EMAIL_ENABLED = False
    def send_trade_notification(*args, **kwargs):
        pass

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler('websocket_server.log'),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

class KiteDataFetcher:
    def __init__(self):
        """Initialize the Kite Data Fetcher"""
        logger.info("="*80)
        logger.info("Initializing Kite Data Fetcher Server")
        logger.info("="*80)
        
        # Load environment variables
        logger.info("Loading environment variables from .env file")
        load_dotenv()
        
        self.api_key = os.getenv("API_KEY")
        self.api_secret = os.getenv("API_SECRET")
        
        if not self.api_key or not self.api_secret:
            logger.error("API_KEY and API_SECRET must be set in .env file")
            raise ValueError("API_KEY and API_SECRET must be set in .env file")
        
        logger.info(f"API_KEY loaded: {self.api_key[:8]}...")
        
        self.kite = None
        self.kws = None  # KiteTicker WebSocket
        self.access_token = None
        self.is_connected = False
        self.ws_connected = False
        
        # Configuration
        self.retry_interval = 300  # 5 minutes in seconds
        self.fetch_interval = 300  # 5 minutes in seconds
        self.candle_processing_delay = 10  # Wait 10 seconds after interval (configurable)
        self.candles_data_file = "candles_data.json"
        self.trades_file = "trades.json"  # All trades (paper and real) saved here
        
        # Trading configuration
        self.trading_enabled = os.getenv("TRADING_ENABLED", "paper").lower()  # "real", "paper", or "disabled"
        self.trading_lots = int(os.getenv("TRADING_LOTS", "1"))  # Number of lots to trade
        self.lot_size = 75  # NIFTY futures lot size (will be updated when instrument is fetched)
        self.quantity = self.trading_lots * self.lot_size  # Total units to trade
        
        # Instrument will be fetched dynamically (current month NIFTY futures)
        self.instrument_token = None
        self.instrument_name = None
        self.tradingsymbol = None
        
        # Trading state
        self.previous_rsi = None
        self.open_trade = None  # Track current open trade
        self.alert_candle = None  # Store alert candle waiting for entry on next candle
        self.last_tick_price = None  # Last price from WebSocket
        self.first_candle_time = None  # Track first candle of the day (9:15 AM)
        
        logger.info(f"📊 Trading Mode: {self.trading_enabled.upper()}")
        logger.info(f"📦 Trade Quantity: {self.trading_lots} lot(s)")
        logger.info(f"🔄 Retry Interval: {self.retry_interval} seconds")
        logger.info(f"⏱️  Fetch Interval: {self.fetch_interval} seconds (5-min aligned + {self.candle_processing_delay}s delay)")
        logger.info(f"📁 Candles Data File: {self.candles_data_file}")
        logger.info(f"📁 Trades Log File: {self.trades_file}")
        logger.info(f"📧 Email Notifications: {'Enabled' if EMAIL_ENABLED else 'Disabled'}")
        logger.info("="*80)
    
    def get_current_month_nifty_futures(self):
        """
        Fetch current month NIFTY futures instrument token
        Returns the nearest expiry NIFTY futures contract
        """
        try:
            logger.info("Fetching NIFTY futures instruments...")
            instruments = self.kite.instruments("NFO")
            
            # Filter for NIFTY futures only
            nifty_futures = []
            for inst in instruments:
                name = inst.get('name', '').strip().upper()
                instrument_type = inst.get('instrument_type', '')
                
                if name == 'NIFTY' and instrument_type == 'FUT':
                    nifty_futures.append({
                        "instrument_token": inst.get('instrument_token'),
                        "tradingsymbol": inst.get('tradingsymbol'),
                        "name": inst.get('name'),
                        "expiry": inst.get('expiry'),
                        "exchange": inst.get('exchange')
                    })
            
            # Sort by expiry (nearest first)
            nifty_futures.sort(key=lambda x: x.get('expiry', ''))
            
            if not nifty_futures:
                logger.error("✗ No NIFTY futures found")
                return None
            
            # Get nearest expiry (current month)
            nearest = nifty_futures[0]
            self.instrument_token = str(nearest['instrument_token'])
            self.instrument_name = f"NIFTY FUT {nearest['expiry'].strftime('%d-%b-%Y') if hasattr(nearest['expiry'], 'strftime') else nearest['expiry']}"
            self.tradingsymbol = nearest['tradingsymbol']
            
            # NIFTY futures have a lot size of 75
            self.lot_size = 75
            self.quantity = self.trading_lots * self.lot_size
            
            logger.info(f"✓ Selected NIFTY Futures: {self.tradingsymbol}")
            logger.info(f"  Instrument Token: {self.instrument_token}")
            logger.info(f"  Expiry: {nearest['expiry']}")
            logger.info(f"  Lot Size: {self.lot_size} units per lot")
            logger.info(f"  Trading: {self.trading_lots} lot(s) = {self.quantity} units")
            
            return nearest
            
        except Exception as e:
            logger.error(f"✗ Error fetching NIFTY futures: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            return None
    
    def connect_to_kite(self):
        """Connect to Kite API using access token"""
        logger.info("Attempting to connect to Kite API...")
        
        try:
            # Read access token from file
            logger.info("Reading access token from file: access_token.txt")
            self.access_token = read_from_file("access_token.txt")
            
            if not self.access_token:
                logger.error("Access token is empty or not found")
                return False
            
            logger.info(f"Access token loaded: {self.access_token[:10]}...")
            
            # Initialize KiteConnect
            logger.info("Initializing KiteConnect instance")
            self.kite = KiteConnect(api_key=self.api_key)
            
            # Set access token
            logger.info("Setting access token")
            self.kite.set_access_token(self.access_token)
            
            # Fetch current month NIFTY futures
            if not self.get_current_month_nifty_futures():
                logger.error("✗ Failed to fetch NIFTY futures instrument")
                return False
            
            # Setup WebSocket for real-time monitoring
            if not self.setup_websocket():
                logger.warning("⚠ WebSocket setup failed, continuing without real-time monitoring")
                # Don't fail connection, just continue without WebSocket
            
            # Connection established
            self.is_connected = True
            logger.info("✓ Kite API connection established successfully")
            return True
            
        except FileNotFoundError as e:
            logger.error(f"✗ Access token file not found: {e}")
            self.is_connected = False
            return False
        except Exception as e:
            logger.error(f"✗ Error connecting to Kite API: {str(e)}")
            logger.error(f"Error type: {type(e).__name__}")
            self.is_connected = False
            return False
    
    def setup_websocket(self):
        """Setup WebSocket connection for real-time tick data"""
        try:
            logger.info("🔌 Setting up WebSocket connection...")
            
            # Initialize KiteTicker
            self.kws = KiteTicker(self.api_key, self.access_token)
            
            # Define callbacks
            def on_ticks(ws, ticks):
                """Callback for tick data"""
                for tick in ticks:
                    if tick['instrument_token'] == int(self.instrument_token):
                        self.last_tick_price = tick.get('last_price', 0)
                        last_traded_price = tick.get('last_price', 0)
                        ohlc = tick.get('ohlc', {})
                        current_high = ohlc.get('high', last_traded_price)
                        current_low = ohlc.get('low', last_traded_price)
                        
                        # Check if we have a pending alert candle waiting for entry
                        if self.alert_candle and not self.open_trade:
                            self.check_entry_trigger_realtime(last_traded_price, current_high, current_low)
                        
                        # Check if we have an open trade to monitor for SL/Target
                        if self.open_trade:
                            self.check_exit_conditions(last_traded_price)
            
            def on_connect(ws, response):
                """Callback when WebSocket connects"""
                logger.info("✓ WebSocket connected successfully")
                self.ws_connected = True
                
                # Subscribe to instrument with FULL mode to get OHLC data
                if self.instrument_token:
                    ws.subscribe([int(self.instrument_token)])
                    ws.set_mode(ws.MODE_FULL, [int(self.instrument_token)])  # FULL mode for OHLC
                    logger.info(f"📡 Subscribed to {self.tradingsymbol} ({self.instrument_token})")
                    logger.info(f"📊 WebSocket Mode: FULL (real-time OHLC + LTP for entry triggers)")
            
            def on_close(ws, code, reason):
                """Callback when WebSocket closes"""
                logger.warning(f"⚠ WebSocket closed: {code} - {reason}")
                self.ws_connected = False
            
            def on_error(ws, code, reason):
                """Callback on error"""
                logger.error(f"✗ WebSocket error: {code} - {reason}")
            
            # Assign callbacks
            self.kws.on_ticks = on_ticks
            self.kws.on_connect = on_connect
            self.kws.on_close = on_close
            self.kws.on_error = on_error
            
            # Start WebSocket in a separate thread
            ws_thread = threading.Thread(target=self.kws.connect, daemon=True)
            ws_thread.start()
            
            # Wait a moment for connection
            time.sleep(2)
            
            logger.info("✓ WebSocket setup complete")
            return True
            
        except Exception as e:
            logger.error(f"✗ Error setting up WebSocket: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            return False
    
    def check_exit_conditions(self, current_price):
        """Check if SL or Target is hit for open trade"""
        if not self.open_trade or not current_price:
            return
        
        trade = self.open_trade
        trade_type = trade.get('transaction_type')
        stop_loss = trade.get('stop_loss')
        target = trade.get('target')
        
        # Check for BUY trade
        if trade_type == 'BUY':
            if current_price <= stop_loss:
                logger.info(f"🛑 STOP LOSS HIT! Price: ₹{current_price:.2f} <= SL: ₹{stop_loss:.2f}")
                self.exit_trade('STOP_LOSS', current_price)
            elif current_price >= target:
                logger.info(f"🎯 TARGET HIT! Price: ₹{current_price:.2f} >= Target: ₹{target:.2f}")
                self.exit_trade('TARGET', current_price)
        
        # Check for SELL trade
        elif trade_type == 'SELL':
            if current_price >= stop_loss:
                logger.info(f"🛑 STOP LOSS HIT! Price: ₹{current_price:.2f} >= SL: ₹{stop_loss:.2f}")
                self.exit_trade('STOP_LOSS', current_price)
            elif current_price <= target:
                logger.info(f"🎯 TARGET HIT! Price: ₹{current_price:.2f} <= Target: ₹{target:.2f}")
                self.exit_trade('TARGET', current_price)
    
    def check_time_based_exit(self):
        """Check if current time is 3:25 PM or later - force exit all trades"""
        now = datetime.now()
        exit_time = now.replace(hour=15, minute=25, second=0, microsecond=0)
        
        if now >= exit_time and self.open_trade:
            logger.info("⏰ 3:25 PM - Time-based exit triggered")
            # Use last tick price or try to get current LTP
            exit_price = self.last_tick_price
            if not exit_price and self.kite:
                try:
                    ltp_data = self.kite.ltp([self.instrument_token])
                    exit_price = ltp_data.get(self.instrument_token, {}).get('last_price', 0)
                except:
                    exit_price = self.open_trade.get('entry_price', 0)
            
            self.exit_trade('TIME_EXIT_325PM', exit_price)
    
    def exit_trade(self, exit_reason, exit_price):
        """Exit the current open trade"""
        if not self.open_trade:
            return
        
        logger.info("="*80)
        logger.info(f"📤 EXITING TRADE - {exit_reason}")
        logger.info("-"*80)
        
        trade = self.open_trade
        trade['exit_price'] = exit_price
        trade['exit_time'] = datetime.now().isoformat()
        trade['exit_reason'] = exit_reason
        trade['status'] = 'CLOSED'
        
        # Calculate P&L
        if trade['transaction_type'] == 'BUY':
            pnl = (exit_price - trade['entry_price']) * trade['quantity']
        else:  # SELL
            pnl = (trade['entry_price'] - exit_price) * trade['quantity']
        
        trade['pnl'] = round(pnl, 2)
        
        logger.info(f"📊 Trade ID: {trade['trade_id']}")
        logger.info(f"📦 Quantity: {trade.get('lots', 'N/A')} lot(s) × {trade.get('lot_size', 'N/A')} = {trade.get('quantity', 'N/A')} units")
        logger.info(f"💰 Entry: ₹{trade['entry_price']:.2f}")
        logger.info(f"💵 Exit: ₹{exit_price:.2f}")
        logger.info(f"{'💚' if pnl > 0 else '❤️'} P&L: ₹{pnl:.2f}")
        logger.info(f"📝 Reason: {exit_reason}")
        
        # Place exit order for real trades
        if trade.get('trade_mode') == 'REAL':
            try:
                # Place opposite order to exit
                exit_order_type = 'SELL' if trade['transaction_type'] == 'BUY' else 'BUY'
                order_id = self.kite.place_order(
                    variety=self.kite.VARIETY_REGULAR,
                    exchange=self.kite.EXCHANGE_NFO,
                    tradingsymbol=self.tradingsymbol,
                    transaction_type=exit_order_type,
                    quantity=trade['quantity'],
                    product=self.kite.PRODUCT_MIS,
                    order_type=self.kite.ORDER_TYPE_MARKET
                )
                trade['exit_order_id'] = order_id
                logger.info(f"📋 Exit Order ID: {order_id}")
            except Exception as e:
                logger.error(f"✗ Error placing exit order: {str(e)}")
        
        # Update trade in file
        self.update_trade_in_file(trade)
        
        # Send email notification
        if EMAIL_ENABLED:
            send_trade_notification('EXIT', trade)
        
        logger.info(f"✅ Trade exited successfully")
        logger.info("="*80)
        
        # Clear open trade
        self.open_trade = None
    
    def update_trade_in_file(self, updated_trade):
        """Update a specific trade in the trades file"""
        try:
            trades = []
            if os.path.exists(self.trades_file):
                with open(self.trades_file, 'r') as f:
                    trades = json.load(f)
            
            # Find and update the trade
            for i, trade in enumerate(trades):
                if trade.get('trade_id') == updated_trade.get('trade_id'):
                    trades[i] = updated_trade
                    break
            
            # Save back to file
            with open(self.trades_file, 'w') as f:
                json.dump(trades, f, indent=2)
            
            logger.info(f"💾 Trade updated in {self.trades_file}")
            return True
            
        except Exception as e:
            logger.error(f"✗ Error updating trade in file: {str(e)}")
            return False
    
    def is_first_candle_of_day(self, candle_date):
        """Check if candle is the first candle of the day (9:15 AM)"""
        if isinstance(candle_date, str):
            try:
                # Parse ISO format with timezone
                if '+' in candle_date:
                    date_obj = datetime.fromisoformat(candle_date)
                else:
                    date_obj = datetime.strptime(candle_date.split('+')[0].strip(), '%Y-%m-%d %H:%M:%S')
            except:
                return False
        elif isinstance(candle_date, datetime):
            date_obj = candle_date
        else:
            return False
        
        # First candle is 9:15 AM
        return date_obj.hour == 9 and date_obj.minute == 15
    
    def get_date_range_for_candles(self):
        """
        Get date range for fetching data.
        Simple & robust approach:
        - Fetch from last 10 days up to NOW
        - Handles weekends, holidays, market hours automatically
        - Filter latest 14 candles from whatever is available
        - Works 24/7 without market hours checking
        """
        to_date = datetime.now()
        from_date = to_date - timedelta(days=10)
        return from_date, to_date
    
    def calculate_next_5min_interval(self):
        """
        Calculate seconds to wait until next 5-minute interval + processing delay
        (e.g., if current time is 9:17, wait until 9:20:30)
        The extra delay ensures the candle has fully formed and API has processed it
        """
        now = datetime.now()
        
        # Round up to next 5-minute mark
        minutes = now.minute
        next_interval_minute = ((minutes // 5) + 1) * 5
        
        if next_interval_minute >= 60:
            next_time = now.replace(hour=now.hour + 1 if now.hour < 23 else 0, 
                                   minute=0, second=0, microsecond=0)
        else:
            next_time = now.replace(minute=next_interval_minute, second=0, microsecond=0)
        
        # Add processing delay to ensure candle is complete
        next_time_with_delay = next_time + timedelta(seconds=self.candle_processing_delay)
        wait_seconds = (next_time_with_delay - now).total_seconds()
        
        logger.info(f"⏰ Current time: {now.strftime('%H:%M:%S')}")
        logger.info(f"⏰ Next candle completes at: {next_time.strftime('%H:%M:%S')}")
        logger.info(f"⏰ Will fetch at: {next_time_with_delay.strftime('%H:%M:%S')} (+{self.candle_processing_delay}s delay)")
        logger.info(f"⏰ Waiting {int(wait_seconds)} seconds...")
        
        return wait_seconds
    
    def calculate_rsi(self, candles):
        """
        Calculate RSI using TA-Lib (14-period, Wilder's smoothing)
        Returns: (rsi_values, latest_rsi)
        """
        if len(candles) < 15:  # Need at least 14+1 candles for RSI-14
            logger.warning("⚠ Not enough candles for RSI calculation (need 15+)")
            return None, None
        
        # Convert to pandas DataFrame
        df = pd.DataFrame(candles)
        closes = df["close"].values
        
        # Calculate RSI using TA-Lib (14-period)
        rsi_values = talib.RSI(closes, timeperiod=14)
        
        # Convert to list and handle NaN
        rsi_list = [float(val) if not np.isnan(val) else None for val in rsi_values]
        
        # Get latest RSI (last value that's not NaN)
        latest_rsi = None
        for val in reversed(rsi_list):
            if val is not None:
                latest_rsi = val
                break
        
        return rsi_list, latest_rsi
    
    def save_trade_to_file(self, trade):
        """Save trade to JSON file for tracking and reference"""
        try:
            trades = []
            if os.path.exists(self.trades_file):
                with open(self.trades_file, 'r') as f:
                    try:
                        trades = json.load(f)
                        logger.info(f"📂 Loaded {len(trades)} existing trades from {self.trades_file}")
                    except:
                        trades = []
                        logger.warning(f"⚠ Could not read existing trades, starting fresh")
            
            trades.append(trade)
            
            with open(self.trades_file, 'w') as f:
                json.dump(trades, f, indent=2)
            
            logger.info(f"💾 Trade saved to {self.trades_file} (Total trades: {len(trades)})")
            return True
            
        except Exception as e:
            logger.error(f"✗ Error saving trade to file: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            return False
    
    def place_paper_trade(self, trade_type, price, alert_candle):
        """Place a paper trade (simulated)"""
        try:
            logger.info("="*80)
            logger.info(f"📝 PLACING PAPER TRADE")
            logger.info("-"*80)
            
            trade = {
                "trade_id": f"PT_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}",
                "trade_mode": "PAPER",
                "instrument_token": self.instrument_token,
                "tradingsymbol": self.tradingsymbol,
                "lots": self.trading_lots,
                "lot_size": self.lot_size,
                "quantity": self.quantity,
                "transaction_type": trade_type,
                "order_type": "MARKET",
                "product": "MIS",
                "entry_price": price,
                "timestamp": datetime.now().isoformat(),
                "status": "OPEN",
                "alert_rsi": round(alert_candle.get('rsi'), 2) if alert_candle.get('rsi') else None,
                "alert_open": alert_candle.get('open'),
                "alert_high": alert_candle.get('high'),
                "alert_low": alert_candle.get('low'),
                "alert_close": alert_candle.get('close'),
                "stop_loss": alert_candle.get('low') if trade_type == "BUY" else alert_candle.get('high'),
                "target": price + 15 if trade_type == "BUY" else price - 15
            }
            
            logger.info(f"🎯 Type: {trade_type}")
            logger.info(f"📊 Symbol: {self.tradingsymbol}")
            logger.info(f"💰 Entry: ₹{price:.2f}")
            logger.info(f"🛑 Stop Loss: ₹{trade['stop_loss']:.2f}")
            logger.info(f"🎯 Target: ₹{trade['target']:.2f}")
            logger.info(f"📈 RSI: {trade['alert_rsi']}")
            logger.info(f"📦 Quantity: {self.trading_lots} lot(s) × {self.lot_size} = {self.quantity} units")
            
            # Save to file
            if self.save_trade_to_file(trade):
                logger.info(f"✅ PAPER TRADE PLACED SUCCESSFULLY")
                logger.info(f"📄 Trade ID: {trade['trade_id']}")
                logger.info(f"📁 Saved to: {self.trades_file}")
                logger.info("="*80)
                
                # Send email notification
                if EMAIL_ENABLED:
                    send_trade_notification('ENTRY', trade)
                
                return trade
            else:
                logger.error(f"✗ Trade placed but failed to save to file")
                return None
            
        except Exception as e:
            logger.error(f"✗ Error placing paper trade: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            logger.info("="*80)
            return None
    
    def place_real_trade(self, trade_type, price, alert_candle):
        """Place a real trade via Kite API"""
        try:
            logger.info("="*80)
            logger.info(f"🔴 PLACING REAL TRADE (LIVE)")
            logger.info("-"*80)
            
            # Place order via Kite API
            logger.info(f"📡 Sending order to Kite API...")
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.kite.EXCHANGE_NFO,
                tradingsymbol=self.tradingsymbol,
                transaction_type=trade_type,
                quantity=self.quantity,
                product=self.kite.PRODUCT_MIS,
                order_type=self.kite.ORDER_TYPE_MARKET
            )
            
            logger.info(f"✅ Order placed successfully!")
            logger.info(f"📋 Order ID: {order_id}")
            
            trade = {
                "order_id": order_id,
                "trade_id": f"RT_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}",
                "trade_mode": "REAL",
                "instrument_token": self.instrument_token,
                "tradingsymbol": self.tradingsymbol,
                "lots": self.trading_lots,
                "lot_size": self.lot_size,
                "quantity": self.quantity,
                "transaction_type": trade_type,
                "order_type": "MARKET",
                "product": "MIS",
                "entry_price": price,
                "timestamp": datetime.now().isoformat(),
                "status": "OPEN",
                "alert_rsi": round(alert_candle.get('rsi'), 2) if alert_candle.get('rsi') else None,
                "alert_open": alert_candle.get('open'),
                "alert_high": alert_candle.get('high'),
                "alert_low": alert_candle.get('low'),
                "alert_close": alert_candle.get('close'),
                "stop_loss": alert_candle.get('low') if trade_type == "BUY" else alert_candle.get('high'),
                "target": price + 15 if trade_type == "BUY" else price - 15
            }
            
            logger.info(f"🎯 Type: {trade_type}")
            logger.info(f"📊 Symbol: {self.tradingsymbol}")
            logger.info(f"💰 Entry: ₹{price:.2f}")
            logger.info(f"🛑 Stop Loss: ₹{trade['stop_loss']:.2f}")
            logger.info(f"🎯 Target: ₹{trade['target']:.2f}")
            logger.info(f"📈 RSI: {trade['alert_rsi']}")
            logger.info(f"📦 Quantity: {self.trading_lots} lot(s) × {self.lot_size} = {self.quantity} units")
            
            # Save to file
            if self.save_trade_to_file(trade):
                logger.info(f"✅ REAL TRADE PLACED & LOGGED")
                logger.info(f"📄 Trade ID: {trade['trade_id']}")
                logger.info(f"📁 Saved to: {self.trades_file}")
                logger.info("="*80)
                
                # Send email notification
                if EMAIL_ENABLED:
                    send_trade_notification('ENTRY', trade)
                
                return trade
            else:
                logger.warning(f"⚠ Order placed but failed to save to file")
                return trade  # Still return trade even if file save failed
            
        except Exception as e:
            logger.error(f"✗ Error placing real trade: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            logger.info("="*80)
            return None
    
    def check_and_place_order(self, latest_candle, rsi):
        """
        Complete trading logic:
        1. Skip first candle of day (9:15 AM)
        2. Mark alert candle when RSI crosses 60 or 40 AND range < 40
        3. On NEXT candle, check if price crosses alert candle trigger
        4. Place order if trigger is crossed
        """
        if self.trading_enabled == "disabled":
            logger.info("ℹ️  TRADE SKIPPED - Trading is disabled (TRADING_ENABLED=disabled)")
            return
        
        # Skip first candle of the day
        if self.is_first_candle_of_day(latest_candle.get('date')):
            logger.info("⚠ TRADE SKIPPED - First candle of day (9:15 AM) - Rule: Skip first candle")
            self.previous_rsi = rsi
            return
        
        # Check time-based exit (3:25 PM)
        self.check_time_based_exit()
        
        # If trade is open, skip new entries
        if self.open_trade is not None:
            logger.info("⚠ NEW TRADE SKIPPED - Trade already open")
            logger.info(f"   Current trade: {self.open_trade.get('trade_id')} - {self.open_trade.get('transaction_type')}")
            logger.info(f"   Entry: ₹{self.open_trade.get('entry_price'):.2f}")
            logger.info(f"   SL: ₹{self.open_trade.get('stop_loss'):.2f} | Target: ₹{self.open_trade.get('target'):.2f}")
            logger.info(f"   Reason: Only 1 trade allowed at a time")
            return
        
        # Check if we have a pending alert candle waiting for entry
        if self.alert_candle is not None:
            logger.info("🔍 Checking pending alert candle...")
            logger.info(f"   Alert Type: {self.alert_candle.get('type')}")
            logger.info(f"   Alert High: ₹{self.alert_candle.get('high'):.2f} | Alert Low: ₹{self.alert_candle.get('low'):.2f}")
            logger.info(f"   Trigger Price: ₹{self.alert_candle.get('trigger_price'):.2f}")
            logger.info(f"   Current RSI: {rsi:.2f}")
            
            # First check if alert should be discarded due to RSI reversal
            if self.should_discard_alert(rsi):
                # Alert was discarded, return early
                return
            
            # Alert is still valid, WebSocket is monitoring for entry
            logger.info("   ✓ Alert still valid")
            logger.info("   📡 WebSocket monitoring active for real-time entry trigger")
            logger.info(f"   ⏳ Waiting for WebSocket to detect price crossing ₹{self.alert_candle.get('trigger_price'):.2f}")
            return
        
        # Check for new RSI crossover to mark alert candle
        if rsi is None or self.previous_rsi is None:
            logger.info("ℹ️  TRADE SKIPPED - RSI not available or first calculation")
            self.previous_rsi = rsi
            return
        
        # Check for RSI crossover
        crossed_60_up = self.previous_rsi <= 60 and rsi > 60
        crossed_40_down = self.previous_rsi >= 40 and rsi < 40
        
        # Log why no trade if no crossover
        if not crossed_60_up and not crossed_40_down:
            logger.info(f"ℹ️  NO ALERT - No RSI crossover detected")
            logger.info(f"   Previous RSI: {self.previous_rsi:.2f} | Current RSI: {rsi:.2f}")
            if rsi > 60:
                logger.info(f"   RSI > 60 but didn't cross (already above)")
            elif rsi < 40:
                logger.info(f"   RSI < 40 but didn't cross (already below)")
            else:
                logger.info(f"   RSI between 40-60 (neutral zone)")
        
        if crossed_60_up:
            logger.info("="*80)
            logger.info(f"🔔 RSI CROSSED ABOVE 60!")
            logger.info(f"   Previous RSI: {self.previous_rsi:.2f}")
            logger.info(f"   Current RSI: {rsi:.2f}")
            
            # Check candle range condition (high - low < 30)
            candle_range = latest_candle['high'] - latest_candle['low']
            logger.info(f"   Candle range: {candle_range:.2f}")
            
            if candle_range < 30:
                logger.info(f"   ✓ Range condition met (< 30)")
                logger.info(f"   📌 ALERT CANDLE MARKED for BUY")
                logger.info(f"   🎯 Entry Trigger: HIGH > ₹{latest_candle['high']:.2f}")
                logger.info(f"   📡 WebSocket will monitor real-time price for entry")
                
                # Mark this as alert candle
                self.alert_candle = {
                    'type': 'BUY',
                    'rsi': rsi,
                    'date': latest_candle.get('date'),
                    'open': latest_candle['open'],
                    'high': latest_candle['high'],
                    'low': latest_candle['low'],
                    'close': latest_candle['close'],
                    'trigger_price': latest_candle['high'],  # Entry trigger
                    'stop_loss': latest_candle['low'],
                    'target': latest_candle['high'] + 15
                }
                logger.info("="*80)
            else:
                logger.info(f"   ✗ Range condition NOT met (>= 30), ignoring signal")
        
        elif crossed_40_down:
            logger.info("="*80)
            logger.info(f"🔔 RSI CROSSED BELOW 40!")
            logger.info(f"   Previous RSI: {self.previous_rsi:.2f}")
            logger.info(f"   Current RSI: {rsi:.2f}")
            
            # Check candle range condition
            candle_range = latest_candle['high'] - latest_candle['low']
            logger.info(f"   Candle range: {candle_range:.2f}")
            
            if candle_range < 30:
                logger.info(f"   ✓ Range condition met (< 30)")
                logger.info(f"   📌 ALERT CANDLE MARKED for SELL")
                logger.info(f"   🎯 Entry Trigger: LOW < ₹{latest_candle['low']:.2f}")
                logger.info(f"   📡 WebSocket will monitor real-time price for entry")
                
                # Mark this as alert candle
                self.alert_candle = {
                    'type': 'SELL',
                    'rsi': rsi,
                    'date': latest_candle.get('date'),
                    'open': latest_candle['open'],
                    'high': latest_candle['high'],
                    'low': latest_candle['low'],
                    'close': latest_candle['close'],
                    'trigger_price': latest_candle['low'],  # Entry trigger
                    'stop_loss': latest_candle['high'],
                    'target': latest_candle['low'] - 15
                }
                logger.info("="*80)
            else:
                logger.info(f"   ✗ ALERT NOT MARKED - Range condition NOT met (>= 30)")
                logger.info(f"   Reason: Candle range {candle_range:.2f} >= 30 (too volatile)")
                logger.info(f"   Rule: Only trade candles with range < 30 points")
        
        # Update previous RSI
        self.previous_rsi = rsi
    
    def should_discard_alert(self, current_rsi):
        """
        Check if pending alert should be discarded due to RSI reversal
        - BUY alert (RSI > 60): Discard if RSI drops back to <= 60
        - SELL alert (RSI < 40): Discard if RSI rises back to >= 40
        """
        if not self.alert_candle or current_rsi is None:
            return False
        
        alert = self.alert_candle
        
        if alert['type'] == 'BUY':
            # BUY alert: Discard if RSI drops back to 60 or below
            if current_rsi <= 60:
                logger.info("="*80)
                logger.info("❌ ALERT DISCARDED - RSI REVERSAL")
                logger.info(f"   Alert Type: BUY (RSI > 60)")
                logger.info(f"   Alert RSI: {alert['rsi']:.2f}")
                logger.info(f"   Current RSI: {current_rsi:.2f} (dropped back to <= 60)")
                logger.info(f"   Signal invalidated before entry")
                logger.info("="*80)
                self.alert_candle = None
                return True
        
        elif alert['type'] == 'SELL':
            # SELL alert: Discard if RSI rises back to 40 or above
            if current_rsi >= 40:
                logger.info("="*80)
                logger.info("❌ ALERT DISCARDED - RSI REVERSAL")
                logger.info(f"   Alert Type: SELL (RSI < 40)")
                logger.info(f"   Alert RSI: {alert['rsi']:.2f}")
                logger.info(f"   Current RSI: {current_rsi:.2f} (rose back to >= 40)")
                logger.info(f"   Signal invalidated before entry")
                logger.info("="*80)
                self.alert_candle = None
                return True
        
        return False
    
    def check_entry_trigger_realtime(self, ltp, current_high, current_low):
        """
        Check entry trigger using REAL-TIME WebSocket data
        Called from WebSocket on_ticks callback
        """
        if not self.alert_candle:
            return
        
        alert = self.alert_candle
        
        if alert['type'] == 'BUY':
            # BUY: Check if real-time HIGH crosses alert candle's HIGH
            if current_high > alert['trigger_price']:
                logger.info("="*80)
                logger.info(f"✅ ENTRY TRIGGER HIT (REAL-TIME)!")
                logger.info(f"   WebSocket High: ₹{current_high:.2f} > Alert High: ₹{alert['trigger_price']:.2f}")
                logger.info(f"   Current LTP: ₹{ltp:.2f}")
                logger.info(f"   Entry Method: Real-time WebSocket")
                logger.info("="*80)
                
                # Place BUY order at current LTP
                if self.trading_enabled == "paper":
                    trade = self.place_paper_trade("BUY", ltp, alert)
                else:  # real
                    trade = self.place_real_trade("BUY", ltp, alert)
                
                if trade:
                    self.open_trade = trade
                    self.alert_candle = None  # Clear alert candle
        
        elif alert['type'] == 'SELL':
            # SELL: Check if real-time LOW crosses alert candle's LOW
            if current_low < alert['trigger_price']:
                logger.info("="*80)
                logger.info(f"✅ ENTRY TRIGGER HIT (REAL-TIME)!")
                logger.info(f"   WebSocket Low: ₹{current_low:.2f} < Alert Low: ₹{alert['trigger_price']:.2f}")
                logger.info(f"   Current LTP: ₹{ltp:.2f}")
                logger.info(f"   Entry Method: Real-time WebSocket")
                logger.info("="*80)
                
                # Place SELL order at current LTP
                if self.trading_enabled == "paper":
                    trade = self.place_paper_trade("SELL", ltp, alert)
                else:  # real
                    trade = self.place_real_trade("SELL", ltp, alert)
                
                if trade:
                    self.open_trade = trade
                    self.alert_candle = None  # Clear alert candle
    
    def fetch_historical_data(self):
        """Fetch latest 15 candles of 5-minute interval historical data"""
        logger.info("-"*80)
        logger.info("Starting historical data fetch...")
        
        if not self.is_connected or not self.kite:
            logger.error("✗ Not connected to Kite API. Cannot fetch data.")
            return False
        
        try:
            # Convert instrument token to integer
            try:
                instrument_token = int(self.instrument_token)
                logger.info(f"Instrument Token (int): {instrument_token}")
            except (ValueError, TypeError) as e:
                logger.error(f"✗ Invalid instrument token format: {self.instrument_token}")
                return False
            
            # Get date range - always fetch from last 10 days up to now
            from_date, to_date = self.get_date_range_for_candles()
            
            logger.info(f"Fetching data from last 10 days (up to now)...")
            logger.info(f"From: {from_date.strftime('%Y-%m-%d %H:%M:%S')}")
            logger.info(f"To: {to_date.strftime('%Y-%m-%d %H:%M:%S')}")
            logger.info(f"Interval: 5minute")
            
            # Fetch historical data
            logger.info("Calling Kite API for historical data...")
            historical_data = self.kite.historical_data(
                instrument_token=instrument_token,
                from_date=from_date,
                to_date=to_date,
                interval="5minute"
            )
            
            if not historical_data:
                logger.warning("⚠ No historical data returned from API (might be extended holiday period)")
                logger.warning("ℹ️  Will retry in next cycle. Connection remains active.")
                # Don't return False - this is not a connection error
                return True  # Return True to keep connection alive
            
            logger.info(f"✓ Received {len(historical_data)} candles from API")
            
            # IMPORTANT: Exclude the last (current) candle as it's still forming
            # Current candle has incomplete data (partial close, volume)
            # This causes incorrect RSI calculation
            if len(historical_data) > 1:
                complete_candles = historical_data[:-1]  # Exclude last candle
                logger.info(f"ℹ️  Excluding current candle (still forming) - using {len(complete_candles)} complete candles")
            else:
                complete_candles = historical_data
            
            # Calculate RSI on complete candles only
            logger.info("Calculating RSI (14-period) on complete candles...")
            rsi_values, latest_rsi = self.calculate_rsi(complete_candles)
            
            if rsi_values and latest_rsi:
                logger.info(f"📊 Latest RSI: {latest_rsi:.2f}")
            else:
                logger.warning("⚠ Could not calculate RSI (insufficient data)")
                logger.warning("⚠ TRADE SKIPPED - Insufficient data for RSI calculation")
            
            # Get only the latest 14 COMPLETE candles from whatever is available
            # Since we already excluded the current candle, all these are complete
            latest_candles = complete_candles[-14:] if len(complete_candles) >= 14 else complete_candles
            latest_rsi_values = rsi_values[-14:] if rsi_values and len(rsi_values) >= 14 else rsi_values if rsi_values else [None] * len(latest_candles)
            
            logger.info(f"📊 Filtered latest {len(latest_candles)} candles")
            
            if latest_candles:
                oldest_candle_date = latest_candles[0].get('date')
                newest_candle_date = latest_candles[-1].get('date')
                if isinstance(oldest_candle_date, datetime):
                    oldest_candle_date = oldest_candle_date.strftime('%Y-%m-%d %H:%M')
                if isinstance(newest_candle_date, datetime):
                    newest_candle_date = newest_candle_date.strftime('%Y-%m-%d %H:%M')
                logger.info(f"📅 Data range: {oldest_candle_date} to {newest_candle_date}")
            
            # Convert datetime objects to ISO format and add RSI values
            logger.info("Processing candle data with RSI...")
            processed_candles = []
            for idx, candle in enumerate(latest_candles):
                # Convert date to ISO format string
                if isinstance(candle.get('date'), datetime):
                    candle['date'] = candle['date'].isoformat()
                
                # Add RSI value
                rsi_val = latest_rsi_values[idx] if idx < len(latest_rsi_values) else None
                candle['rsi'] = round(rsi_val, 2) if rsi_val is not None else None
                
                processed_candles.append(candle)
                logger.debug(f"Candle {idx+1}: Date={candle.get('date')}, "
                           f"O={candle.get('open')}, H={candle.get('high')}, "
                           f"L={candle.get('low')}, C={candle.get('close')}, "
                           f"RSI={candle.get('rsi')}")
            
            # Check for order conditions on latest candle
            if latest_rsi and processed_candles:
                latest_candle = processed_candles[-1]
                latest_candle['rsi'] = latest_rsi  # Ensure latest RSI is set
                logger.info(f"🔍 Checking order conditions...")
                self.check_and_place_order(latest_candle, latest_rsi)
            
            # Save to JSON file - just the array of candles
            logger.info(f"Saving {len(processed_candles)} candles to {self.candles_data_file}...")
            with open(self.candles_data_file, 'w') as f:
                json.dump(processed_candles, f, indent=2)
            
            logger.info(f"✓ Successfully saved {len(processed_candles)} candles to {self.candles_data_file}")
            logger.info(f"File size: {os.path.getsize(self.candles_data_file)} bytes")
            
            # Log summary of latest candle
            if processed_candles:
                latest = processed_candles[-1]
                logger.info("-"*80)
                logger.info("📊 LATEST CANDLE SUMMARY:")
                logger.info(f"  🕐 Date: {latest.get('date')}")
                logger.info(f"  💹 Open: {latest.get('open')}")
                logger.info(f"  ⬆️  High: {latest.get('high')}")
                logger.info(f"  ⬇️  Low: {latest.get('low')}")
                logger.info(f"  💵 Close: {latest.get('close')}")
                logger.info(f"  📦 Volume: {latest.get('volume')}")
                logger.info(f"  📈 RSI: {latest.get('rsi')}")
                logger.info("-"*80)
            
            return True
            
        except Exception as e:
            logger.error(f"✗ Error fetching historical data: {str(e)}")
            logger.error(f"Error type: {type(e).__name__}")
            import traceback
            logger.error(f"Traceback: {traceback.format_exc()}")
            return False
    
    def run(self):
        """Main server loop with 5-minute aligned intervals"""
        logger.info("="*80)
        logger.info("STARTING KITE DATA FETCHER & TRADING SERVER")
        logger.info("="*80)
        
        # Initial connection attempt
        connection_success = self.connect_to_kite()
        
        # Wait until next 5-minute interval before first fetch
        if connection_success:
            wait_time = self.calculate_next_5min_interval()
            logger.info(f"⏳ Waiting for next 5-minute interval before first fetch...")
            time.sleep(wait_time)
        
        while True:
            # If not connected, try to reconnect
            if not connection_success or not self.is_connected:
                logger.warning(f"⚠ Not connected. Retrying in {self.retry_interval} seconds...")
                time.sleep(self.retry_interval)
                connection_success = self.connect_to_kite()
                continue
            
            # Fetch historical data at aligned 5-minute interval
            logger.info(f"🔄 Starting data fetch at {datetime.now().strftime('%H:%M:%S')}")
            fetch_success = self.fetch_historical_data()
            
            if not fetch_success:
                logger.warning("⚠ Data fetch failed. Connection might be lost.")
                self.is_connected = False
                connection_success = False
                continue
            
            logger.info(f"✓ Data fetch complete at {datetime.now().strftime('%H:%M:%S')}")
            logger.info("="*80)
            
            # Wait until next 5-minute interval (e.g., 9:15, 9:20, 9:25, etc.)
            wait_time = self.calculate_next_5min_interval()
            time.sleep(wait_time)


def main():
    """Entry point for the server"""
    try:
        fetcher = KiteDataFetcher()
        fetcher.run()
    except KeyboardInterrupt:
        logger.info("\n" + "="*80)
        logger.info("Server stopped by user (Ctrl+C)")
        logger.info("="*80)
        sys.exit(0)
    except Exception as e:
        logger.error(f"\n{'='*80}")
        logger.error(f"FATAL ERROR: {str(e)}")
        logger.error(f"Error type: {type(e).__name__}")
        import traceback
        logger.error(f"Traceback: {traceback.format_exc()}")
        logger.error("="*80)
        sys.exit(1)


if __name__ == "__main__":
    main()

