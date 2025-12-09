import smtplib
import os
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

# Gmail SMTP Configuration
SMTP_SERVER = "smtp.gmail.com"
SMTP_PORT = 587
EMAIL_ADDRESS = os.getenv("EMAIL_ADDRESS")
EMAIL_PASSWORD = os.getenv("EMAIL_PASSWORD")

# Static recipient email addresses
RECIPIENT_EMAILS = ["ganeshghatti6@gmail.com", "praveen.cp321@gmail.com"]

def send_email(subject, body, to_email=None):
    """
    Send email notification using Gmail SMTP
    
    Args:
        subject: Email subject
        body: Email body (can be HTML or plain text)
        to_email: Recipient email(s) - can be a string or list of strings (defaults to RECIPIENT_EMAILS if None)
    """
    if to_email is None:
        to_email = RECIPIENT_EMAILS
    
    # Convert single email to list if needed
    if isinstance(to_email, str):
        to_email = [to_email]
    
    try:
        # Create message
        msg = MIMEMultipart()
        msg['From'] = EMAIL_ADDRESS
        msg['To'] = ", ".join(to_email)  # Join multiple recipients with comma
        msg['Subject'] = subject
        
        # Add body to email
        msg.attach(MIMEText(body, 'html'))
        
        # Create SMTP session
        server = smtplib.SMTP(SMTP_SERVER, SMTP_PORT)
        server.starttls()  # Enable security
        server.login(EMAIL_ADDRESS, EMAIL_PASSWORD)
        
        # Send email to all recipients
        text = msg.as_string()
        server.sendmail(EMAIL_ADDRESS, to_email, text)
        server.quit()
        
        print(f"Email sent successfully to {', '.join(to_email)}")
        return True
    except Exception as e:
        print(f"Error sending email: {e}")
        import traceback
        traceback.print_exc()
        return False

def send_trade_notification(trade_type, trade_data):
    """
    Send email notification when a trade occurs
    
    Args:
        trade_type: Type of trade event ('ENTRY', 'EXIT', 'ALERT')
        trade_data: Dictionary containing trade information
    """
    timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    
    if trade_type == 'ALERT':
        subject = f"🚨 Trading Alert - {trade_data.get('alert_type', 'ALERT')} Signal"
        body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <h2 style="color: #2c3e50;">Trading Alert</h2>
            <p><strong>Time:</strong> {timestamp}</p>
            <p><strong>Alert Type:</strong> {trade_data.get('alert_type', 'N/A')}</p>
            <p><strong>RSI:</strong> {trade_data.get('rsi', 'N/A')}</p>
            <hr>
            <h3>Candle Details:</h3>
            <ul>
                <li><strong>Open:</strong> {trade_data.get('open', 'N/A')}</li>
                <li><strong>High:</strong> {trade_data.get('high', 'N/A')}</li>
                <li><strong>Low:</strong> {trade_data.get('low', 'N/A')}</li>
                <li><strong>Close:</strong> {trade_data.get('close', 'N/A')}</li>
            </ul>
        </body>
        </html>
        """
    
    elif trade_type == 'ENTRY':
        subject = f"✅ Trade Entered - {trade_data.get('type', 'TRADE')}"
        body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <h2 style="color: #27ae60;">Trade Entered</h2>
            <p><strong>Time:</strong> {timestamp}</p>
            <p><strong>Trade Type:</strong> {trade_data.get('type', 'N/A')}</p>
            <hr>
            <h3>Entry Details:</h3>
            <ul>
                <li><strong>Entry Price:</strong> ₹{trade_data.get('entry_price', 'N/A')}</li>
                <li><strong>Stop Loss:</strong> ₹{trade_data.get('stop_loss', 'N/A')}</li>
                <li><strong>Target:</strong> ₹{trade_data.get('target', 'N/A')}</li>
            </ul>
            <hr>
            <h3>Alert Candle Info:</h3>
            <ul>
                <li><strong>Alert RSI:</strong> {trade_data.get('alert_rsi', 'N/A')}</li>
                <li><strong>Alert High:</strong> ₹{trade_data.get('alert_high', 'N/A')}</li>
                <li><strong>Alert Low:</strong> ₹{trade_data.get('alert_low', 'N/A')}</li>
            </ul>
        </body>
        </html>
        """
    
    elif trade_type == 'EXIT':
        exit_reason = trade_data.get('status', 'EXIT')
        status_emoji = '🎯' if exit_reason == 'TARGET' else '🛑' if exit_reason == 'STOP_LOSS' else '⏰'
        subject = f"{status_emoji} Trade Exited - {exit_reason}"
        
        pnl = trade_data.get('pnl', 0)
        pnl_color = '#27ae60' if pnl > 0 else '#e74c3c' if pnl < 0 else '#7f8c8d'
        pnl_emoji = '💰' if pnl > 0 else '📉' if pnl < 0 else '➖'
        
        body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <h2 style="color: {pnl_color};">Trade Exited</h2>
            <p><strong>Time:</strong> {timestamp}</p>
            <p><strong>Trade Type:</strong> {trade_data.get('type', 'N/A')}</p>
            <p><strong>Exit Reason:</strong> {exit_reason}</p>
            <hr>
            <h3>Trade Summary:</h3>
            <ul>
                <li><strong>Entry Price:</strong> ₹{trade_data.get('entry_price', 'N/A')}</li>
                <li><strong>Exit Price:</strong> ₹{trade_data.get('exit_price', 'N/A')}</li>
                <li><strong>Stop Loss:</strong> ₹{trade_data.get('stop_loss', 'N/A')}</li>
                <li><strong>Target:</strong> ₹{trade_data.get('target', 'N/A')}</li>
                <li style="font-size: 18px; font-weight: bold; color: {pnl_color};">
                    <strong>{pnl_emoji} P&L:</strong> ₹{pnl:.2f}
                </li>
            </ul>
        </body>
        </html>
        """
    
    else:
        subject = f"Trading Notification - {trade_type}"
        body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; line-height: 1.6; color: #333;">
            <h2>Trading Notification</h2>
            <p><strong>Time:</strong> {timestamp}</p>
            <p><strong>Type:</strong> {trade_type}</p>
            <pre>{str(trade_data)}</pre>
        </body>
        </html>
        """
    
    return send_email(subject, body)