from kiteconnect import KiteConnect
from utils.file_utils import read_from_file
from datetime import datetime, timedelta
import csv
import os
import json
import pandas as pd
import talib
import numpy as np

def holdings(kite):
    access_token = read_from_file("access_token.txt")
    if not access_token:
        return None, "Missing access token"
    
    kite.set_access_token(access_token)
    holdings = kite.holdings()
    return holdings, None

def orders(kite):
    return kite.orders()

def positions(kite):
    return kite.positions()

def ltp(kite, symbol):
    return kite.ltp([symbol])

def get_instruments(kite, data=None):
    """
    Get futures instrument tokens for a specified symbol.
    Returns futures contracts sorted by expiry (nearest first).
    
    Parameters (via data dict):
    - symbol_name: Name of the symbol (e.g., 'NIFTY', 'CRUDEOIL', 'BANKNIFTY'). Defaults to 'NIFTY'
    - exchange: Exchange code (e.g., 'NFO', 'MCX'). Defaults to 'NFO'
    - instrument_type: Instrument type (e.g., 'FUT', 'OPT'). Defaults to 'FUT'
    """
    access_token = read_from_file("access_token.txt")
    if not access_token:
        return None, "Missing access token"
    
    kite.set_access_token(access_token)
    
    # Extract parameters from data dict, with defaults
    symbol_name = (data.get('symbol_name') or 'NIFTY').strip().upper() if data else 'NIFTY'
    exchange = (data.get('exchange') or 'NFO').strip().upper() if data else 'NFO'
    instrument_type = (data.get('instrument_type') or 'FUT').strip().upper() if data else 'FUT'
    
    try:
        # Get all instruments from specified exchange
        instruments = kite.instruments(exchange)
        
        # Filter for specified symbol and instrument type
        filtered_instruments = []
        for inst in instruments:
            name = inst.get('name', '').strip().upper()
            inst_type = inst.get('instrument_type', '').strip().upper()
            
            # Check if it matches the specified symbol and instrument type
            if name == symbol_name and inst_type == instrument_type:
                filtered_instruments.append({
                    "instrument_token": inst.get('instrument_token'),
                    "tradingsymbol": inst.get('tradingsymbol'),
                    "name": inst.get('name'),
                    "expiry": inst.get('expiry'),
                    "exchange": inst.get('exchange')
                })
        
        # Sort by expiry date (ascending) to get nearest expiry first
        filtered_instruments.sort(key=lambda x: x.get('expiry', ''))
        
        if not filtered_instruments:
            return None, f"No {symbol_name} {instrument_type} found in {exchange}"
        
        return filtered_instruments, None
    except Exception as e:
        return None, f"Error fetching instruments: {str(e)}"

def historical_data_with_alerts(kite, data):
    if not data.get("instrument_token"):
        return None, "Missing instrument_token"
    if not data.get("from_date"):
        return None, "Missing from_date"
    if not data.get("interval"):
        return None, "Missing interval"
    
    instrument_token = data["instrument_token"]
    
    # Convert to int if it's a string
    try:
        instrument_token = int(instrument_token)
    except (ValueError, TypeError):
        return None, f"Invalid instrument_token format: {instrument_token}. Must be a number."
    
    try:
        from_date = datetime.strptime(data["from_date"], "%Y-%m-%d")
    except ValueError:
        return None, "Invalid from_date format. Use YYYY-MM-DD"
    
    to_date = datetime.now()
    if data.get("to_date"):
        try:
            to_date = datetime.strptime(data["to_date"], "%Y-%m-%d")
        except ValueError:
            return None, "Invalid to_date format. Use YYYY-MM-DD"
    
    access_token = read_from_file("access_token.txt")
    if not access_token:
        return None, "Missing access token"

    kite.set_access_token(access_token)
    
    print("setting access token done ")
    
    try:
        historical_data = kite.historical_data(
            instrument_token=instrument_token,
            from_date=from_date,
            to_date=to_date,
            interval=data["interval"]
        )
    except Exception as e:
        print("error getting historical data: ", e)
        return None, f"Error getting historical data: {str(e)}"
    
    print("historical data: ", historical_data)
    
    if not historical_data:
        return None, "No historical data found"
    
    # Convert historical data to DataFrame and calculate RSI using TA-Lib
    # TA-Lib RSI uses Wilder's smoothing method (standard 14-period RSI)
    # Formula: 
    # - First 14 candles: Simple average of gains/losses
    # - Later candles: Wilder's smoothing: AvgGain = (Previous AvgGain * 13 + Current Gain) / 14
    # - RS = AvgGain / AvgLoss
    # - RSI = 100 - (100 / (1 + RS))
    df = pd.DataFrame(historical_data)
    # Convert to numpy array and ensure float64 type (double) for TA-Lib
    closes = np.array(df["close"].values, dtype=np.float64)
    
    # Calculate RSI using TA-Lib (14-period, Wilder's smoothing)
    rsi_values = talib.RSI(closes, timeperiod=14)
    
    # Convert numpy array to list and handle NaN values (first 14 candles will be NaN)
    rsi_values = [float(val) if not np.isnan(val) else None for val in rsi_values]
    
    alert_candles = []
    prev_rsi = None
    
    for i, candle in enumerate(historical_data):
        rsi = rsi_values[i] if i < len(rsi_values) else None
        candle["rsi"] = round(rsi, 2) if rsi is not None else None
        
        is_alert = False
        if rsi is not None:
            # Only alert when crossing thresholds, not when already beyond them
            # Alert when crossing 60 going up (from <= 60 to > 60)
            # Alert when crossing 40 going down (from >= 40 to < 40)
            if prev_rsi is not None:
                crossed_60_up = prev_rsi <= 60 and rsi > 60
                crossed_40_down = prev_rsi >= 40 and rsi < 40
                
                if crossed_60_up or crossed_40_down:
                    is_alert = True
                    alert_candles.append(i)
            
            # Update previous RSI for next iteration
            prev_rsi = rsi
        
        candle["is_alert"] = "ALERT" if is_alert else ""
    
    filename = f"historical_data_{data['instrument_token']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    
    if historical_data:
        with open(filename, 'w', newline='') as csvfile:
            fieldnames = ['date', 'date_simple', 'open', 'high', 'low', 'close', 'volume', 'rsi', 'is_alert']
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            for candle in historical_data:
                date_str = candle.get('date', '')
                date_simple = ''
                if date_str:
                    try:
                        if isinstance(date_str, str):
                            date_obj = datetime.strptime(date_str.split('+')[0].strip(), '%Y-%m-%d %H:%M:%S')
                            date_simple = date_obj.strftime('%Y-%m-%d %H:%M')
                        else:
                            date_simple = date_str.strftime('%Y-%m-%d %H:%M') if hasattr(date_str, 'strftime') else str(date_str)
                    except:
                        date_simple = date_str
                
                writer.writerow({
                    'date': date_str,
                    'date_simple': date_simple,
                    'open': candle.get('open', ''),
                    'high': candle.get('high', ''),
                    'low': candle.get('low', ''),
                    'close': candle.get('close', ''),
                    'volume': candle.get('volume', ''),
                    'rsi': candle.get('rsi', ''),
                    'is_alert': candle.get('is_alert', '')
                })
    
    return {"filename": filename, "total_candles": len(historical_data), "alert_count": len(alert_candles)}, None

def start_paper_trade(kite, data):
    if not data.get("instrument_token"):
        return None, "Missing instrument_token"
    if not data.get("quantity"):
        return None, "Missing quantity"
    if not data.get("transaction_type"):
        return None, "Missing transaction_type"
    
    transaction_type = data["transaction_type"].upper()
    if transaction_type not in ["BUY", "SELL"]:
        return None, "transaction_type must be BUY or SELL"
    
    access_token = read_from_file("access_token.txt")
    if not access_token:
        return None, "Missing access token"
    
    kite.set_access_token(access_token)
    
    instrument_token = data["instrument_token"]
    quantity = data["quantity"]
    price = data.get("price")
    order_type = data.get("order_type", "MARKET")
    product = data.get("product", "MIS")
    
    if not price and order_type == "LIMIT":
        return None, "price is required for LIMIT orders"
    
    try:
        if order_type == "MARKET" and not price:
            ltp_data = kite.ltp([instrument_token])
            print("ltp_data: ", ltp_data)
            if ltp_data:
                instrument_key = str(instrument_token)
                if instrument_key in ltp_data:
                    price = ltp_data[instrument_key].get("last_price", 0)
                    print("price: ", price)
            if not price or price == 0:
                return None, "Could not fetch LTP for MARKET order. Please provide price."
        else:
            price = float(price) if price else 0
        
        if price == 0:
            return None, "Invalid price"
        
        paper_trade = {
            "trade_id": f"PT_{datetime.now().strftime('%Y%m%d_%H%M%S_%f')}",
            "instrument_token": instrument_token,
            "quantity": int(quantity),
            "transaction_type": transaction_type,
            "order_type": order_type,
            "product": product,
            "price": float(price),
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
        
        return paper_trade, None
    except Exception as e:
        return None, f"Error starting paper trade: {str(e)}"

def backtest_strategy(kite, data):
    """
    Backtest RSI-based trading strategy:
    1. Skip first candle of each day
    2. Identify alert candles (RSI crossing 60 up or 40 down)
    3. Check if alert candle range (high - low) < 40
    4. For RSI > 60: BUY when next candle crosses high, SL = low, Target = entry + 10
    5. For RSI < 40: SELL when next candle crosses low, SL = high, Target = entry - 10
    """
    if not data.get("instrument_token"):
        return None, "Missing instrument_token"
    if not data.get("from_date"):
        return None, "Missing from_date"
    if not data.get("interval"):
        return None, "Missing interval"
    
    try:
        from_date = datetime.strptime(data["from_date"], "%Y-%m-%d")
    except ValueError:
        return None, "Invalid from_date format. Use YYYY-MM-DD"
    
    to_date = datetime.now()
    if data.get("to_date"):
        try:
            to_date = datetime.strptime(data["to_date"], "%Y-%m-%d")
        except ValueError:
            return None, "Invalid to_date format. Use YYYY-MM-DD"
    
    access_token = read_from_file("access_token.txt")
    if not access_token:
        return None, "Missing access token"
    
    kite.set_access_token(access_token)
    
    historical_data = kite.historical_data(
        instrument_token=data["instrument_token"],
        from_date=from_date,
        to_date=to_date,
        interval=data["interval"]
    )
    
    if not historical_data:
        return None, "No historical data found"
    
    # Convert historical data to DataFrame and calculate RSI using TA-Lib
    # TA-Lib RSI uses Wilder's smoothing method (standard 14-period RSI)
    # Formula: 
    # - First 14 candles: Simple average of gains/losses
    # - Later candles: Wilder's smoothing: AvgGain = (Previous AvgGain * 13 + Current Gain) / 14
    # - RS = AvgGain / AvgLoss
    # - RSI = 100 - (100 / (1 + RS))
    df = pd.DataFrame(historical_data)
    # Convert to numpy array and ensure float64 type (double) for TA-Lib
    closes = np.array(df["close"].values, dtype=np.float64)
    
    # Calculate RSI using TA-Lib (14-period, Wilder's smoothing)
    rsi_values = talib.RSI(closes, timeperiod=14)
    
    # Convert numpy array to list and handle NaN values (first 14 candles will be NaN)
    rsi_values = [float(val) if not np.isnan(val) else None for val in rsi_values]
    
    def is_after_325(candle_date):
        """Check if time is 3:25 PM or later (15:25)"""
        if isinstance(candle_date, str):
            try:
                date_obj = datetime.strptime(candle_date.split('+')[0].strip(), '%Y-%m-%d %H:%M:%S')
                return date_obj.hour > 15 or (date_obj.hour == 15 and date_obj.minute >= 25)
            except:
                return False
        elif isinstance(candle_date, datetime):
            return candle_date.hour > 15 or (candle_date.hour == 15 and candle_date.minute >= 25)
        return False
    
    def should_allow_trading(candle_date):
        """Check if trading is allowed (not after 3:25 PM)"""
        return not is_after_325(candle_date)
    
    # Identify first candle of each trading day (9:15 AM IST - market opening)
    first_candle_of_day = set()
    for i, candle in enumerate(historical_data):
        date_str = candle.get('date', '')
        if date_str:
            try:
                if isinstance(date_str, str):
                    date_obj = datetime.strptime(date_str.split('+')[0].strip(), '%Y-%m-%d %H:%M:%S')
                else:
                    date_obj = date_str
                
                # Check if it's 9:15 AM (first candle of trading day)
                # Market opens at 9:15 AM IST, so first 5-minute candle is 9:15-9:20
                if date_obj.hour == 9 and date_obj.minute == 15:
                    first_candle_of_day.add(i)
            except:
                pass
    
    # Identify alert candles using same logic as historical_data_with_alerts
    alert_candles = []
    prev_rsi = None
    
    for i, candle in enumerate(historical_data):
        # Skip first candle of day
        if i in first_candle_of_day:
            prev_rsi = rsi_values[i] if i < len(rsi_values) else None
            continue
        
        rsi = rsi_values[i] if i < len(rsi_values) else None
        
        if rsi is not None and prev_rsi is not None:
            crossed_60_up = prev_rsi <= 60 and rsi > 60
            crossed_40_down = prev_rsi >= 40 and rsi < 40
            
            if crossed_60_up or crossed_40_down:
                # Store alert candle info
                alert_candle = {
                    'index': i,
                    'date': candle.get('date', ''),
                    'open': candle.get('open', 0),
                    'high': candle.get('high', 0),
                    'low': candle.get('low', 0),
                    'close': candle.get('close', 0),
                    'rsi': rsi,
                    'alert_type': 'BUY' if crossed_60_up else 'SELL'
                }
                alert_candles.append(alert_candle)
        
        prev_rsi = rsi
    
    # Backtest trades
    trades = []
    open_trade = None
    pending_alerts = {}  # Track alerts waiting for next candle entry
    
    for i, candle in enumerate(historical_data):
        # Skip first candle of day for entry checks
        is_first_candle = i in first_candle_of_day
        
        # Check if time is 3:25 PM or later - exit any open trade
        candle_date = candle.get('date', '')
        if open_trade and is_after_325(candle_date):
            # Force exit at 3:25 PM
            close = candle.get('close', 0)
            if close > 0:
                open_trade['exit_price'] = close
                open_trade['exit_date'] = candle_date
                open_trade['exit_index'] = i
                if open_trade['type'] == 'BUY':
                    open_trade['pnl'] = open_trade['exit_price'] - open_trade['entry_price']
                else:
                    open_trade['pnl'] = open_trade['entry_price'] - open_trade['exit_price']
                open_trade['status'] = 'EXIT_325'
                trades.append(open_trade)
                open_trade = None
        
        # Check if we have an open trade
        if open_trade:
            high = candle.get('high', 0)
            low = candle.get('low', 0)
            close = candle.get('close', 0)
            
            if open_trade['type'] == 'BUY':
                # Check for stop loss or target
                if low <= open_trade['stop_loss']:
                    # Stop loss hit
                    open_trade['exit_price'] = open_trade['stop_loss']
                    open_trade['exit_date'] = candle.get('date', '')
                    open_trade['exit_index'] = i
                    open_trade['pnl'] = open_trade['exit_price'] - open_trade['entry_price']
                    open_trade['status'] = 'STOP_LOSS'
                    trades.append(open_trade)
                    open_trade = None
                elif high >= open_trade['target']:
                    # Target hit
                    open_trade['exit_price'] = open_trade['target']
                    open_trade['exit_date'] = candle.get('date', '')
                    open_trade['exit_index'] = i
                    open_trade['pnl'] = open_trade['exit_price'] - open_trade['entry_price']
                    open_trade['status'] = 'TARGET'
                    trades.append(open_trade)
                    open_trade = None
                elif i == len(historical_data) - 1:
                    # Last candle, exit at close
                    open_trade['exit_price'] = close
                    open_trade['exit_date'] = candle.get('date', '')
                    open_trade['exit_index'] = i
                    open_trade['pnl'] = open_trade['exit_price'] - open_trade['entry_price']
                    open_trade['status'] = 'EXIT_END_OF_DATA'
                    trades.append(open_trade)
                    open_trade = None
            
            elif open_trade['type'] == 'SELL':
                # Check for stop loss or target
                if high >= open_trade['stop_loss']:
                    # Stop loss hit
                    open_trade['exit_price'] = open_trade['stop_loss']
                    open_trade['exit_date'] = candle.get('date', '')
                    open_trade['exit_index'] = i
                    open_trade['pnl'] = open_trade['entry_price'] - open_trade['exit_price']
                    open_trade['status'] = 'STOP_LOSS'
                    trades.append(open_trade)
                    open_trade = None
                elif low <= open_trade['target']:
                    # Target hit
                    open_trade['exit_price'] = open_trade['target']
                    open_trade['exit_date'] = candle.get('date', '')
                    open_trade['exit_index'] = i
                    open_trade['pnl'] = open_trade['entry_price'] - open_trade['exit_price']
                    open_trade['status'] = 'TARGET'
                    trades.append(open_trade)
                    open_trade = None
                elif i == len(historical_data) - 1:
                    # Last candle, exit at close
                    open_trade['exit_price'] = close
                    open_trade['exit_date'] = candle.get('date', '')
                    open_trade['exit_index'] = i
                    open_trade['pnl'] = open_trade['entry_price'] - open_trade['exit_price']
                    open_trade['status'] = 'EXIT_END_OF_DATA'
                    trades.append(open_trade)
                    open_trade = None
        
        # Check for entry from pending alerts (alert was on previous candle)
        # We stored alerts with their index as key, so check if previous candle index exists
        # Only allow entry if trading is allowed (not after 3:25 PM)
        prev_candle_idx = i - 1
        if not open_trade and not is_first_candle and prev_candle_idx in pending_alerts and should_allow_trading(candle_date):
            alert = pending_alerts[prev_candle_idx]
            high = candle.get('high', 0)
            low = candle.get('low', 0)
            
            if alert['alert_type'] == 'BUY':
                # BUY when current candle crosses high of alert candle
                if high > alert['high']:
                    open_trade = {
                        'type': 'BUY',
                        'alert_date': alert['date'],
                        'alert_index': alert['index'],
                        'alert_open': alert['open'],
                        'alert_high': alert['high'],
                        'alert_low': alert['low'],
                        'alert_close': alert['close'],
                        'alert_rsi': alert['rsi'],
                        'entry_price': alert['high'],  # Entry at high of alert candle
                        'entry_date': candle.get('date', ''),
                        'entry_index': i,
                        'stop_loss': alert['low'],
                        'target': alert['high'] + 10,
                        'status': 'OPEN'
                    }
                    del pending_alerts[prev_candle_idx]
            
            elif alert['alert_type'] == 'SELL':
                # SELL when current candle crosses low of alert candle
                if low < alert['low']:
                    open_trade = {
                        'type': 'SELL',
                        'alert_date': alert['date'],
                        'alert_index': alert['index'],
                        'alert_open': alert['open'],
                        'alert_high': alert['high'],
                        'alert_low': alert['low'],
                        'alert_close': alert['close'],
                        'alert_rsi': alert['rsi'],
                        'entry_price': alert['low'],  # Entry at low of alert candle
                        'entry_date': candle.get('date', ''),
                        'entry_index': i,
                        'stop_loss': alert['high'],
                        'target': alert['low'] - 10,
                        'status': 'OPEN'
                    }
                    del pending_alerts[prev_candle_idx]
        
        # Check for new alert candles (only if trading is allowed - not after 3:25 PM)
        if should_allow_trading(candle_date):
            for alert in alert_candles:
                if alert['index'] == i and not is_first_candle:
                    # Check if range condition is met (high - low < 40)
                    candle_range = alert['high'] - alert['low']
                    if candle_range < 40:
                        # Store as pending alert for next candle entry check
                        pending_alerts[i] = alert
                        alert_candles.remove(alert)
                        break
    
    # Generate CSV with backtest results
    filename = f"backtest_{data['instrument_token']}_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
    
    with open(filename, 'w', newline='') as csvfile:
        fieldnames = [
            'trade_id', 'type', 'alert_date', 'alert_open', 'alert_high', 'alert_low', 'alert_close', 'alert_rsi',
            'entry_date', 'entry_price', 'exit_date', 'exit_price', 'stop_loss', 'target',
            'pnl', 'status'
        ]
        writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
        writer.writeheader()
        
        for idx, trade in enumerate(trades, 1):
            # Format dates
            alert_date_str = ''
            entry_date_str = ''
            exit_date_str = ''
            
            if trade.get('alert_date'):
                try:
                    if isinstance(trade['alert_date'], str):
                        alert_date_obj = datetime.strptime(trade['alert_date'].split('+')[0].strip(), '%Y-%m-%d %H:%M:%S')
                        alert_date_str = alert_date_obj.strftime('%Y-%m-%d %H:%M')
                    else:
                        alert_date_str = trade['alert_date'].strftime('%Y-%m-%d %H:%M') if hasattr(trade['alert_date'], 'strftime') else str(trade['alert_date'])
                except:
                    alert_date_str = str(trade.get('alert_date', ''))
            
            if trade.get('entry_date'):
                try:
                    if isinstance(trade['entry_date'], str):
                        entry_date_obj = datetime.strptime(trade['entry_date'].split('+')[0].strip(), '%Y-%m-%d %H:%M:%S')
                        entry_date_str = entry_date_obj.strftime('%Y-%m-%d %H:%M')
                    else:
                        entry_date_str = trade['entry_date'].strftime('%Y-%m-%d %H:%M') if hasattr(trade['entry_date'], 'strftime') else str(trade['entry_date'])
                except:
                    entry_date_str = str(trade.get('entry_date', ''))
            
            if trade.get('exit_date'):
                try:
                    if isinstance(trade['exit_date'], str):
                        exit_date_obj = datetime.strptime(trade['exit_date'].split('+')[0].strip(), '%Y-%m-%d %H:%M:%S')
                        exit_date_str = exit_date_obj.strftime('%Y-%m-%d %H:%M')
                    else:
                        exit_date_str = trade['exit_date'].strftime('%Y-%m-%d %H:%M') if hasattr(trade['exit_date'], 'strftime') else str(trade['exit_date'])
                except:
                    exit_date_str = str(trade.get('exit_date', ''))
            
            writer.writerow({
                'trade_id': idx,
                'type': trade.get('type', ''),
                'alert_date': alert_date_str,
                'alert_open': round(trade.get('alert_open', 0), 2),
                'alert_high': round(trade.get('alert_high', 0), 2),
                'alert_low': round(trade.get('alert_low', 0), 2),
                'alert_close': round(trade.get('alert_close', 0), 2),
                'alert_rsi': round(trade.get('alert_rsi', 0), 2),
                'entry_date': entry_date_str,
                'entry_price': round(trade.get('entry_price', 0), 2),
                'exit_date': exit_date_str,
                'exit_price': round(trade.get('exit_price', 0), 2),
                'stop_loss': round(trade.get('stop_loss', 0), 2),
                'target': round(trade.get('target', 0), 2),
                'pnl': round(trade.get('pnl', 0), 2),
                'status': trade.get('status', '')
            })
    
    # Calculate summary statistics
    total_trades = len(trades)
    winning_trades = [t for t in trades if t.get('pnl', 0) > 0]
    losing_trades = [t for t in trades if t.get('pnl', 0) < 0]
    total_pnl = sum(t.get('pnl', 0) for t in trades)
    win_rate = (len(winning_trades) / total_trades * 100) if total_trades > 0 else 0
    
    summary = {
        'filename': filename,
        'total_trades': total_trades,
        'winning_trades': len(winning_trades),
        'losing_trades': len(losing_trades),
        'win_rate': round(win_rate, 2),
        'total_pnl': round(total_pnl, 2),
        'avg_pnl_per_trade': round(total_pnl / total_trades, 2) if total_trades > 0 else 0
    }
    
    return summary, None

