from flask import request, jsonify
from controllers.auth_controller import connect, callback, profile

def register_auth_routes(app, kite, api_secret):
    @app.route('/connect')
    def connect_route():
        return connect(kite)
    
    @app.route('/callback')
    def callback_route():
        request_token = request.args.get('request_token')
        print(request.args)
        result = callback(kite, request_token, api_secret)
        if isinstance(result, tuple):
            return result[0], result[1]
        return result
    
    @app.route("/profile")
    def profile_route():
        return jsonify(profile(kite))

