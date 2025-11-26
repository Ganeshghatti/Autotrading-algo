from kiteconnect import KiteTicker

def on_ticks(ws, ticks):
    print(ticks)

def on_connect(ws, response):
    ws.subscribe([738561])
    ws.set_mode(ws.MODE_FULL, [738561])

def on_close(ws, code, reason):
    ws.stop()

def start_websocket_server(api_key, access_token):
    kws = KiteTicker(api_key, access_token)
    kws.on_ticks = on_ticks
    kws.on_connect = on_connect
    kws.on_close = on_close
    kws.connect()
    return kws

