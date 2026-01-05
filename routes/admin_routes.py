from flask import render_template, request, redirect, session, jsonify
import json
import os

ADMIN_PASSWORD = "admin123"
CONFIG_FILE = "config.json"
LOG_FILE = "websocket_server.log"


def save_config(data):
    with open(CONFIG_FILE, "w") as f:
        json.dump(data, f, indent=4)


def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, "r") as f:
        return json.load(f)


def register_admin_routes(app):

    @app.route("/admin", methods=["GET", "POST"])
    def admin_login():
        if request.method == "POST":
            password = request.form.get("password")

            if password == ADMIN_PASSWORD:
                session["admin_logged_in"] = True
                return redirect("/dashboard")

            return redirect("/admin?error=invalid")

        return render_template("login.html")

    @app.route("/dashboard", methods=["GET", "POST"])
    def dashboard():
        if not session.get("admin_logged_in"):
            return redirect("/admin")

        if request.method == "POST":
            config_data = {
                "instrument_symbol": request.form.get("instrument_symbol").upper(),
                "exchange": request.form.get("exchange"),
                "instrument_type": request.form.get("instrument_type"),
                "high_low_diff": request.form.get("high_low_diff"),
                "target": request.form.get("target"),
                "lots": request.form.get("lots"),
                "lot_size": request.form.get("lot_size"),
            }
            print("✅ Config Data Updated:", config_data)

            save_config(config_data)
            return redirect("/dashboard?saved=true")

        config = load_config()
        return render_template("dashboard.html", config=config)
    
    @app.route("/logout")
    def logout():
        session.pop("admin_logged_in", None)
        return redirect("/admin")
    
    @app.route("/api/logs")
    def get_logs():
        """API endpoint to fetch latest logs"""
        if not session.get("admin_logged_in"):
            return jsonify({"error": "Unauthorized"}), 401
        
        try:
            # websocket_server.log is the CURRENT active log file
            # Older logs are rotated to websocket_server.log.YYYY-MM-DD
            if not os.path.exists(LOG_FILE):
                return jsonify({
                    "logs": "No logs available yet. Start the trading bot to see logs.", 
                    "lines": 0,
                    "total_lines": 0
                })
            
            # Read last 200 lines of CURRENT log file for better scrolling experience
            with open(LOG_FILE, 'r', encoding='utf-8', errors='ignore') as f:
                lines = f.readlines()
                last_lines = lines[-200:] if len(lines) > 200 else lines
                log_content = ''.join(last_lines)
                
            return jsonify({
                "logs": log_content,
                "lines": len(last_lines),
                "total_lines": len(lines)
            })
        except Exception as e:
            return jsonify({"error": str(e), "logs": "Error reading logs"}), 500