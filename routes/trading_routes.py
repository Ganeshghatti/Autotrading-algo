from flask import request, jsonify
from controllers.trading_controller import holdings, orders, positions, ltp, historical_data_with_alerts, start_paper_trade, get_instruments, backtest_strategy

def register_trading_routes(app, kite):
    @app.route('/holdings')
    def holdings_route():
        result, error = holdings(kite)
        if error:
            return error, 400
        return jsonify(result)
    
    @app.route("/orders")
    def orders_route():
        return jsonify(orders(kite))
    
    @app.route("/positions")
    def positions_route():
        return jsonify(positions(kite))
    
    @app.route("/ltp")
    def ltp_route():
        symbol = request.args.get("symbol")
        return jsonify(ltp(kite, symbol))
    
    @app.route("/historical/alerts", methods=["GET"])
    def historical_alerts_route():
        data = request.get_json()
        result, error = historical_data_with_alerts(kite, data)
        if error:
            return error, 400
        return jsonify(result)
    
    @app.route("/paper-trade", methods=["POST"])
    def paper_trade_route():
        data = request.get_json()
        result, error = start_paper_trade(kite, data)
        if error:
            return error, 400
        return jsonify(result)
    
    @app.route("/instruments", methods=["GET"])
    def instruments_route():
        exchange = request.args.get("exchange")  # Optional: NSE, NFO, etc.
        result, error = get_instruments(kite, exchange)
        if error:
            return error, 400
        return jsonify(result)
    
    @app.route("/backtest", methods=["POST"])
    def backtest_route():
        data = request.get_json()
        result, error = backtest_strategy(kite, data)
        if error:
            return error, 400
        return jsonify(result)

