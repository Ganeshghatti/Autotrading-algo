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
from kiteconnect import KiteConnect
from dotenv import load_dotenv
import pandas as pd
import talib
import numpy as np

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
        self.access_token = None
        self.is_connected = False
        
        # Configuration
        self.retry_interval = 300  # 5 minutes in seconds
        self.fetch_interval = 300  # 5 minutes in seconds
        self.candles_data_file = "candles_data.json"
        self.trades_file = "paper_trades.json"
        
        # Trading configuration
        self.trading_enabled = os.getenv("TRADING_ENABLED", "paper").lower()  # "real", "paper", or "disabled"
        self.quantity = int(os.getenv("TRADING_LOTS", "1"))  # Lot size for NIFTY futures
        
        # Instrument will be fetched dynamically (current month NIFTY futures)
        self.instrument_token = None
        self.instrument_name = None
        self.tradingsymbol = None
        
        # RSI tracking
        self.previous_rsi = None
        self.open_trade = None  # Track current open trade
        
        logger.info(f"Trading Mode: {self.trading_enabled.upper()}")
        logger.info(f"Trade Quantity: {self.quantity}")
        logger.info(f"Retry Interval: {self.retry_interval} seconds")
        logger.info(f"Fetch Interval: {self.fetch_interval} seconds (aligned to 5-min intervals)")
        logger.info(f"Data File: {self.candles_data_file}")
        logger.info(f"Email Notifications: {'Enabled' if EMAIL_ENABLED else 'Disabled'}")
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
            
            logger.info(f"✓ Selected NIFTY Futures: {self.tradingsymbol}")
            logger.info(f"  Instrument Token: {self.instrument_token}")
            logger.info(f"  Expiry: {nearest['expiry']}")
            
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
    
    def get_date_range_for_candles(self):
        """
        Get date range for fetching data.
        Fetch enough data to calculate RSI (need at least 14+ candles for RSI-14)
        Always fetch from last 10 days to handle holidays and weekends.
        """
        to_date = datetime.now()
        from_date = to_date - timedelta(days=10)
        return from_date, to_date
    
    def calculate_next_5min_interval(self):
        """
        Calculate seconds to wait until next 5-minute interval
        (e.g., if current time is 9:17, wait until 9:20)
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
        
        wait_seconds = (next_time - now).total_seconds()
        
        logger.info(f"⏰ Current time: {now.strftime('%H:%M:%S')}")
        logger.info(f"⏰ Next 5-min interval: {next_time.strftime('%H:%M:%S')}")
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
    
    def place_paper_trade(self, trade_type, price, alert_candle):
        """Place a paper trade"""
        try:
            trade = {
                "trade_id": f"PT_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}",
                "instrument_token": self.instrument_token,
                "tradingsymbol": self.tradingsymbol,
                "quantity": self.quantity,
                "transaction_type": trade_type,
                "order_type": "MARKET",
                "product": "MIS",
                "price": price,
                "timestamp": datetime.now().isoformat(),
                "status": "PAPER_TRADE_OPEN",
                "alert_rsi": alert_candle.get('rsi'),
                "alert_high": alert_candle.get('high'),
                "alert_low": alert_candle.get('low'),
                "stop_loss": alert_candle.get('low') if trade_type == "BUY" else alert_candle.get('high'),
                "target": price + 10 if trade_type == "BUY" else price - 10
            }
            
            # Save to file
            trades = []
            if os.path.exists(self.trades_file):
                with open(self.trades_file, 'r') as f:
                    try:
                        trades = json.load(f)
                    except:
                        trades = []
            
            trades.append(trade)
            
            with open(self.trades_file, 'w') as f:
                json.dump(trades, f, indent=2)
            
            logger.info(f"✅ PAPER TRADE PLACED: {trade_type} @ ₹{price}")
            logger.info(f"   Stop Loss: ₹{trade['stop_loss']}, Target: ₹{trade['target']}")
            
            # Send email notification
            if EMAIL_ENABLED:
                send_trade_notification('ENTRY', trade)
            
            return trade
            
        except Exception as e:
            logger.error(f"✗ Error placing paper trade: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            return None
    
    def place_real_trade(self, trade_type, price, alert_candle):
        """Place a real trade via Kite API"""
        try:
            order_id = self.kite.place_order(
                variety=self.kite.VARIETY_REGULAR,
                exchange=self.kite.EXCHANGE_NFO,
                tradingsymbol=self.tradingsymbol,
                transaction_type=trade_type,
                quantity=self.quantity,
                product=self.kite.PRODUCT_MIS,
                order_type=self.kite.ORDER_TYPE_MARKET
            )
            
            trade = {
                "order_id": order_id,
                "trade_id": f"RT_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}",
                "instrument_token": self.instrument_token,
                "tradingsymbol": self.tradingsymbol,
                "quantity": self.quantity,
                "transaction_type": trade_type,
                "order_type": "MARKET",
                "product": "MIS",
                "price": price,
                "timestamp": datetime.now().isoformat(),
                "status": "REAL_TRADE_OPEN",
                "alert_rsi": alert_candle.get('rsi'),
                "alert_high": alert_candle.get('high'),
                "alert_low": alert_candle.get('low'),
                "stop_loss": alert_candle.get('low') if trade_type == "BUY" else alert_candle.get('high'),
                "target": price + 10 if trade_type == "BUY" else price - 10
            }
            
            logger.info(f"✅ REAL TRADE PLACED: {trade_type} @ ₹{price}")
            logger.info(f"   Order ID: {order_id}")
            logger.info(f"   Stop Loss: ₹{trade['stop_loss']}, Target: ₹{trade['target']}")
            
            # Send email notification
            if EMAIL_ENABLED:
                send_trade_notification('ENTRY', trade)
            
            return trade
            
        except Exception as e:
            logger.error(f"✗ Error placing real trade: {str(e)}")
            import traceback
            logger.error(traceback.format_exc())
            return None
    
    def check_and_place_order(self, latest_candle, rsi):
        """
        Check RSI crossover and place order if conditions met
        - BUY when RSI crosses above 60
        - SELL when RSI crosses below 40
        """
        if self.trading_enabled == "disabled":
            return
        
        if self.open_trade is not None:
            logger.info("⚠ Trade already open, skipping order check")
            return
        
        if rsi is None or self.previous_rsi is None:
            self.previous_rsi = rsi
            return
        
        # Check for RSI crossover
        crossed_60_up = self.previous_rsi <= 60 and rsi > 60
        crossed_40_down = self.previous_rsi >= 40 and rsi < 40
        
        if crossed_60_up:
            logger.info(f"🔔 RSI crossed above 60! Previous: {self.previous_rsi:.2f}, Current: {rsi:.2f}")
            
            # Check candle range condition (high - low < 40)
            candle_range = latest_candle['high'] - latest_candle['low']
            logger.info(f"   Candle range: {candle_range:.2f}")
            
            if candle_range < 40:
                logger.info(f"   ✓ Range condition met (< 40)")
                
                # Entry price is the high of the alert candle
                entry_price = latest_candle['high']
                
                alert_candle = {
                    'rsi': rsi,
                    'high': latest_candle['high'],
                    'low': latest_candle['low'],
                    'close': latest_candle['close'],
                    'open': latest_candle['open']
                }
                
                # Place order
                if self.trading_enabled == "paper":
                    trade = self.place_paper_trade("BUY", entry_price, alert_candle)
                else:  # real
                    trade = self.place_real_trade("BUY", entry_price, alert_candle)
                
                if trade:
                    self.open_trade = trade
            else:
                logger.info(f"   ✗ Range condition NOT met (>= 40), skipping order")
        
        elif crossed_40_down:
            logger.info(f"🔔 RSI crossed below 40! Previous: {self.previous_rsi:.2f}, Current: {rsi:.2f}")
            
            # Check candle range condition
            candle_range = latest_candle['high'] - latest_candle['low']
            logger.info(f"   Candle range: {candle_range:.2f}")
            
            if candle_range < 40:
                logger.info(f"   ✓ Range condition met (< 40)")
                
                # Entry price is the low of the alert candle
                entry_price = latest_candle['low']
                
                alert_candle = {
                    'rsi': rsi,
                    'high': latest_candle['high'],
                    'low': latest_candle['low'],
                    'close': latest_candle['close'],
                    'open': latest_candle['open']
                }
                
                # Place order
                if self.trading_enabled == "paper":
                    trade = self.place_paper_trade("SELL", entry_price, alert_candle)
                else:  # real
                    trade = self.place_real_trade("SELL", entry_price, alert_candle)
                
                if trade:
                    self.open_trade = trade
            else:
                logger.info(f"   ✗ Range condition NOT met (>= 40), skipping order")
        
        # Update previous RSI
        self.previous_rsi = rsi
    
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
            
            # Get date range - always fetch from last 10 days
            from_date, to_date = self.get_date_range_for_candles()
            
            logger.info(f"Fetching data from last 10 days...")
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
            
            logger.info(f"✓ Received {len(historical_data)} candles from API (from last 10 days)")
            
            # Calculate RSI on all data
            logger.info("Calculating RSI (14-period)...")
            rsi_values, latest_rsi = self.calculate_rsi(historical_data)
            
            if rsi_values and latest_rsi:
                logger.info(f"📊 Latest RSI: {latest_rsi:.2f}")
            else:
                logger.warning("⚠ Could not calculate RSI (insufficient data)")
            
            # Get only the latest 15 candles from whatever is available
            latest_candles = historical_data[-15:] if len(historical_data) >= 15 else historical_data
            latest_rsi_values = rsi_values[-15:] if rsi_values and len(rsi_values) >= 15 else rsi_values if rsi_values else [None] * len(latest_candles)
            
            logger.info(f"📊 Selected latest {len(latest_candles)} candles")
            
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
                logger.info("LATEST CANDLE SUMMARY:")
                logger.info(f"  Date: {latest.get('date')}")
                logger.info(f"  Open: {latest.get('open')}")
                logger.info(f"  High: {latest.get('high')}")
                logger.info(f"  Low: {latest.get('low')}")
                logger.info(f"  Close: {latest.get('close')}")
                logger.info(f"  Volume: {latest.get('volume')}")
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

