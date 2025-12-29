from flask import Flask
from kiteconnect import KiteConnect
import os
from dotenv import load_dotenv
from utils.file_utils import read_from_file
from routes.auth_routes import register_auth_routes
from routes.trading_routes import register_trading_routes
from routes.admin_routes import register_admin_routes

# Load environment variables from .env file
load_dotenv()

app = Flask(__name__)
app.secret_key = "supersecretkey"

API_KEY = os.getenv("API_KEY")
API_SECRET = os.getenv("API_SECRET")
REDIRECT_URL = os.getenv("REDIRECT_URL", "http://localhost:4000/callback")
ACCESS_TOKEN = read_from_file("access_token.txt")

# Validate that required credentials are loaded
if not API_KEY or not API_SECRET:
    raise ValueError("API_KEY and API_SECRET must be set in .env file")

kite = KiteConnect(api_key=API_KEY)

@app.route('/')
def home():
    return "Hello Flask"

register_auth_routes(app, kite, API_SECRET)
register_trading_routes(app, kite)

register_admin_routes(app)
    
if __name__ == "__main__":
    app.run(debug=True, port=4000)
