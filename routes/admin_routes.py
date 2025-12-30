from flask import render_template, request, redirect, session
import json
import os

ADMIN_PASSWORD = "admin123"
CONFIG_FILE = "config.json"


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

            return "Invalid password", 401

        return render_template("login.html")

    @app.route("/dashboard", methods=["GET", "POST"])
    def dashboard():
        if not session.get("admin_logged_in"):
            return redirect("/admin")

        if request.method == "POST":
            config_data = {
                "high_low_diff": request.form.get("high_low_diff"),
                "instrument_token": request.form.get("instrument_token"),
                "target": request.form.get("target"),
                "lots": request.form.get("lots"),
            }
            print("Config Data ", config_data)

            save_config(config_data)

        config = load_config()
        return render_template("dashboard.html", config=config)