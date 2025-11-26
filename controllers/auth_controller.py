from kiteconnect import KiteConnect
from utils.file_utils import read_from_file, write_to_file
import os

def connect(kite):
    login_url = kite.login_url()
    return f'<a href="{login_url}">Login with Zerodha</a>'

def callback(kite, request_token, api_secret):
    if not request_token:
        return "Missing request token", 400
    
    try:
        data = kite.generate_session(request_token, api_secret=api_secret)
        print("data: ", data)
        access_token = data["access_token"]
        refresh_token = data["refresh_token"]
        print("refresh token: ", refresh_token)
        print("access token: ", access_token)
        write_to_file("access_token.txt", access_token)
        write_to_file("refresh_token.txt", refresh_token)
        kite.set_access_token(access_token)
        return "Login successful"
    except Exception as e:
        return f"Error: {e}"

def profile(kite):
    print("profile req")
    return kite.profile()

