from flask import Flask, render_template, jsonify, request
from flask_socketio import SocketIO
import websocket
import json
import threading
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
from ta.volatility import BollingerBands
import time
import os
import requests

app = Flask(__name__)
app.config['SECRET_KEY'] = 'your-secret-key'
socketio = SocketIO(app, async_mode='threading')

TIMEFRAMES = ['1m', '30m', '1h', '4h']
INDICATORS = ['RSI', 'EMA20', 'EMA50', 'EMA200', 'BB']
MAX_CANDLES = 250  # Keep 250 candles in memory for each timeframe

class BinanceWebSocket:
    def __init__(self):
        self.connected = False
        self.candles = {tf: [] for tf in TIMEFRAMES}
        self.current_price = 0.0
        self.lock = threading.Lock()
        self.ws = None
        self.running = True
        # Fetch historical data on startup
        self.fetch_historical_data()
        self.connect()

    def fetch_historical_data(self):
        """Fetch historical candle data for all timeframes"""
        interval_map = {
            '1m': '1m',
            '30m': '30m',
            '1h': '1h',
            '4h': '4h'
        }
        
        for tf in TIMEFRAMES:
            try:
                url = "https://api.binance.com/api/v3/klines"
                params = {
                    'symbol': 'BTCUSDT',
                    'interval': interval_map[tf],
                    'limit': MAX_CANDLES
                }
                
                response = requests.get(url, params=params)
                data = response.json()
                self.candles[tf] = [{
                    'time': pd.to_datetime(candle[0], unit='ms'),
                    'open': float(candle[1]),
                    'high': float(candle[2]),
                    'low': float(candle[3]),
                    'close': float(candle[4])
                } for candle in data]
                
                print(f"Fetched {len(self.candles[tf])} {tf} candles from Binance")
                
            except Exception as e:
                print(f"Error fetching historical data for {tf}: {e}")
                socketio.emit('error', {'message': f"Error fetching {tf} historical data: {str(e)}"})

    def connect(self):
        def on_open(ws):
            self.connected = True
            socketio.emit('status', {'message': 'Connected to Binance'})

        def on_message(ws, message):
            data = json.loads(message)
            price = round(float(data['p']), 2)
            self.current_price = price
            self.process_trade(price, data['T'])
            socketio.emit('price_update', {'price': f"{price:.2f}"})

        def on_error(ws, error):
            socketio.emit('error', {'message': f"WebSocket error: {error}"})

        def on_close(ws, close_status_code, close_msg):
            self.connected = False
            socketio.emit('status', {'message': 'Disconnected from Binance'})
            if self.running:
                time.sleep(5)
                self.connect()

        self.ws = websocket.WebSocketApp(
            "wss://fstream.binance.com/ws/btcusdt@aggTrade",
            on_open=on_open,
            on_message=on_message,
            on_error=on_error,
            on_close=on_close
        )
        threading.Thread(target=self.ws.run_forever, daemon=True).start()

    def process_trade(self, price, timestamp):
        with self.lock:
            ts = pd.to_datetime(timestamp, unit='ms')
            for tf in self.candles:
                self.update_candles(tf, ts, price)

    def update_candles(self, tf, ts, price):
        if not self.candles[tf]:
            self.add_candle(tf, ts, price)
            return

        last_candle = self.candles[tf][-1]
        if (ts - last_candle['time']).total_seconds() >= self.get_seconds(tf):
            self.add_candle(tf, ts, price)
        else:
            self.update_last_candle(last_candle, price)

    def add_candle(self, tf, ts, price):
        self.candles[tf].append({
            'time': ts,
            'open': round(price, 2),
            'high': round(price, 2),
            'low': round(price, 2),
            'close': round(price, 2)
        })
        if len(self.candles[tf]) > MAX_CANDLES:
            self.candles[tf].pop(0)

    def update_last_candle(self, candle, price):
        candle['close'] = round(price, 2)
        candle['high'] = round(max(candle['high'], price), 2)
        candle['low'] = round(min(candle['low'], price), 2)

    def get_seconds(self, tf):
        return {
            '1m': 60,
            '30m': 1800,
            '1h': 3600,
            '4h': 14400
        }[tf]

    def get_ohlc_data(self, tf):
        with self.lock:
            return pd.DataFrame(self.candles[tf])

class AlertManager:
    def __init__(self):
        self.alerts = {tf: {ind: {'enabled': True, 'threshold': 0.1} 
                      for ind in INDICATORS} for tf in TIMEFRAMES}
        self.active_alerts = set()
        self.price_alerts = []

    def check_alerts(self, indicators):
        current_price = round(binance_ws.current_price, 2)
        for tf in indicators:
            for name, value in indicators[tf].items():
                if name == 'BB':
                    for band, val in value.items():
                        self.check_single_alert(current_price, val, f"{tf}_{name}_{band}")
                else:
                    self.check_single_alert(current_price, value, f"{tf}_{name}")

    def check_single_alert(self, price, value, key):
        tf, indicator = key.split('_', 1)
        alert_config = self.alerts[tf][indicator.split('_')[0]]
        
        if alert_config['enabled']:
            threshold = alert_config['threshold']
            if abs(price - value) <= (threshold / 100 * price):
                self.trigger_alert(key)

    def trigger_alert(self, message):
        if message not in self.active_alerts:
            self.active_alerts.add(message)
            socketio.emit('alert', {'message': message})

    def add_price_alert(self, price):
        price = round(float(price), 2)
        self.price_alerts.append(price)
        socketio.emit('price_alert_added', {'price': f"{price:.2f}"})

    def check_price_alerts(self, current_price):
        current_price = round(current_price, 2)
        for alert_price in self.price_alerts[:]:
            diff = abs(current_price - alert_price)
            if diff <= (0.001 * current_price):  # 1 cent tolerance
                self.trigger_alert(f"Price reached {alert_price:.2f}")
                self.price_alerts.remove(alert_price)

class SLTPCalculator:
    def __init__(self):
        self.entry_price = 0.0
        self.position_type = 'LONG'
        self.sl_percent = 1.0
        self.tp_percent = 2.0
        self.trailing_sl = False
        self.trailing_tp = False

    def set_position(self, entry_price, position_type):
        self.entry_price = round(float(entry_price), 2)
        self.position_type = position_type

    def calculate_sl(self, current_price):
        current_price = round(current_price, 2)
        if self.position_type == 'LONG':
            return round(self.entry_price * (1 - self.sl_percent/100), 2)
        return round(self.entry_price * (1 + self.sl_percent/100), 2)

    def calculate_tp(self, current_price):
        current_price = round(current_price, 2)
        if self.position_type == 'LONG':
            return round(self.entry_price * (1 + self.tp_percent/100), 2)
        return round(self.entry_price * (1 - self.tp_percent/100), 2)

# Global instances
binance_ws = BinanceWebSocket()
alert_manager = AlertManager()
sltp_calculator = SLTPCalculator()

def calculate_indicators():
    indicators = {}
    for tf in TIMEFRAMES:
        df = binance_ws.get_ohlc_data(tf)
        if df.empty or len(df) < 20:
            continue
            
        indicators[tf] = {}
        
        # RSI
        rsi = RSIIndicator(df['close'], window=14).rsi()
        indicators[tf]['RSI'] = round(rsi.iloc[-1], 2)
        
        # EMAs
        ema20 = EMAIndicator(df['close'], window=20).ema_indicator()
        indicators[tf]['EMA20'] = round(ema20.iloc[-1], 2)
        
        ema50 = EMAIndicator(df['close'], window=50).ema_indicator()
        indicators[tf]['EMA50'] = round(ema50.iloc[-1], 2)
        
        ema200 = EMAIndicator(df['close'], window=200).ema_indicator()
        indicators[tf]['EMA200'] = round(ema200.iloc[-1], 2)
        
        # Bollinger Bands   
        bb = BollingerBands(df['close'], window=20, window_dev=2)
        indicators[tf]['BB'] = {
            'upper': round(bb.bollinger_hband().iloc[-1], 2),
            'middle': round(bb.bollinger_mavg().iloc[-1], 2),
            'lower': round(bb.bollinger_lband().iloc[-1], 2)
        }
        
    return indicators

def background_thread():
    while True:
        time.sleep(1)
        
        # Calculate indicators
        indicators = calculate_indicators()
        
        # Check alerts
        alert_manager.check_alerts(indicators)
        
        # Check price alerts
        if binance_ws.current_price > 0:
            alert_manager.check_price_alerts(binance_ws.current_price)
        
        # Update SL/TP if position is set
        if sltp_calculator.entry_price > 0:
            current_price = binance_ws.current_price
            sl = sltp_calculator.calculate_sl(current_price)
            tp = sltp_calculator.calculate_tp(current_price)
            socketio.emit('sltp_update', {
                'sl': f"{sl:.2f}",
                'tp': f"{tp:.2f}"
            })
        
        # Send indicators to client
        if indicators:
            socketio.emit('indicators_update', {
                'indicators': {
                    tf: {
                        ind: str(val) if not isinstance(val, dict) 
                        else {k: str(v) for k, v in val.items()}
                        for ind, val in indicators[tf].items()
                    } 
                    for tf in TIMEFRAMES if tf in indicators
                }
            })

@app.route('/')
def index():
    return render_template('index.html', timeframes=TIMEFRAMES, indicators=INDICATORS)

@app.route('/set_position', methods=['POST'])
def set_position():
    data = request.json
    sltp_calculator.set_position(float(data['entry_price']), data['position_type'])
    sltp_calculator.sl_percent = round(float(data['sl_percent']), 2)
    sltp_calculator.tp_percent = round(float(data['tp_percent']), 2)
    sltp_calculator.trailing_sl = data.get('trailing_sl', False)
    sltp_calculator.trailing_tp = data.get('trailing_tp', False)
    return jsonify({'status': 'success'})

@app.route('/set_alert', methods=['POST'])
def set_alert():
    data = request.json
    tf = data['timeframe']
    indicator = data['indicator']
    alert_manager.alerts[tf][indicator]['enabled'] = data['enabled']
    alert_manager.alerts[tf][indicator]['threshold'] = round(float(data['threshold']), 2)
    return jsonify({'status': 'success'})

@app.route('/set_price_alert', methods=['POST'])
def set_price_alert():
    data = request.json
    alert_manager.add_price_alert(data['price'])
    return jsonify({'status': 'success'})

@socketio.on('connect')
def handle_connect():
    socketio.emit('status', {'message': 'Connected to server'})
    if binance_ws.connected:
        socketio.emit('status', {'message': 'Connected to Binance'})
    else:
        socketio.emit('status', {'message': 'Connecting to Binance...'})
    
    # Start background thread if it's not already running
    if not hasattr(app, 'background_thread_running'):
        app.background_thread_running = True
        threading.Thread(target=background_thread, daemon=True).start()

if __name__ == '__main__':
    # Create templates directory if it doesn't exist
    os.makedirs('templates', exist_ok=True)
    
    # Create the HTML template file
    with open('templates/index.html', 'w') as f:
        f.write('''<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BTC Dashboard</title>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/socket.io/4.0.1/socket.io.js"></script>
    <script src="https://cdn.tailwindcss.com"></script>
    <style>
        .alert-flash {
            animation: flash 1s infinite;
        }
        @keyframes flash {
            0% { background-color: #d7ba7d; }
            50% { background-color: #f0e0b0; }
            100% { background-color: #d7ba7d; }
        }
    </style>
</head>
<body class="bg-gray-900 text-white">
    <div class="container mx-auto p-4">
        <h1 class="text-3xl font-bold text-center mb-6 text-blue-400">Binance BTC Dashboard</h1>
        
        <!-- Status Bar -->
        <div id="status-bar" class="bg-gray-800 p-2 mb-4 rounded">
            <span id="status-message">Connecting...</span>
        </div>
        
        <!-- Price Display -->
        <div class="bg-gray-800 p-4 rounded-lg mb-6 flex justify-between items-center">
            <h2 class="text-xl font-bold">BTC/USDT</h2>
            <div id="price-display" class="text-2xl font-bold text-blue-400">--</div>
        </div>
        
        <!-- Alert Controls -->
        <div class="bg-gray-800 p-4 rounded-lg mb-6">
            <h2 class="text-xl font-bold mb-4">Alert Controls</h2>
            <div class="overflow-x-auto">
                <table class="w-full">
                    <thead>
                        <tr>
                            <th class="text-left p-2">Indicator</th>
                            {% for tf in timeframes %}
                            <th class="p-2">{{ tf.upper() }}</th>
                            {% endfor %}
                        </tr>
                    </thead>
                    <tbody>
                        {% for ind in indicators %}
                        <tr class="border-t border-gray-700">
                            <td class="p-2">{{ ind }}</td>
                            {% for tf in timeframes %}
                            <td class="p-2">
                                <div class="flex items-center justify-center">
                                    <input type="checkbox" id="{{ ind }}_{{ tf }}_enable" 
                                           class="mr-2 enable-checkbox" data-tf="{{ tf }}" data-ind="{{ ind }}"
                                           onchange="updateAlert('{{ tf }}', '{{ ind }}')" checked>
                                    <input type="number" id="{{ ind }}_{{ tf }}_threshold" 
                                           class="w-16 bg-gray-700 text-white p-1 rounded threshold-input"
                                           data-tf="{{ tf }}" data-ind="{{ ind }}" value="0.1" step="0.1" min="0"
                                           onchange="updateAlert('{{ tf }}', '{{ ind }}')">
                                </div>
                            </td>
                            {% endfor %}
                        </tr>
                        {% endfor %}
                    </tbody>
                </table>
            </div>
        </div>
        
        <!-- SL/TP Calculator -->
        <div class="bg-gray-800 p-4 rounded-lg mb-6">
            <h2 class="text-xl font-bold mb-4">SL/TP Calculator</h2>
            <div class="grid grid-cols-1 md:grid-cols-2 gap-4">
                <!-- Position Type -->
                <div class="bg-gray-700 p-3 rounded">
                    <label class="block mb-2">Position Type:</label>
                    <div class="flex space-x-4">
                        <label class="inline-flex items-center">
                            <input type="radio" name="position_type" value="LONG" checked 
                                   class="form-radio text-green-500">
                            <span class="ml-2 text-green-400">LONG</span>
                        </label>
                        <label class="inline-flex items-center">
                            <input type="radio" name="position_type" value="SHORT" 
                                   class="form-radio text-red-500">
                            <span class="ml-2 text-red-400">SHORT</span>
                        </label>
                    </div>
                </div>
                
                <!-- Entry Price -->
                <div class="bg-gray-700 p-3 rounded">
                    <label for="entry_price" class="block mb-2">Entry Price:</label>
                    <div class="flex">
                        <input type="number" id="entry_price" step="0.01" min="0" 
                               class="flex-1 bg-gray-600 text-white p-2 rounded-l">
                        <button onclick="setPosition()" 
                                class="bg-blue-500 hover:bg-blue-600 text-white px-4 py-2 rounded-r">
                            Set
                        </button>
                    </div>
                </div>
                
                <!-- SL Controls -->
                <div class="bg-gray-700 p-3 rounded">
                    <label for="sl_percent" class="block mb-2">SL %:</label>
                    <div class="flex items-center">
                        <input type="number" id="sl_percent" value="1.0" step="0.1" min="0" 
                               class="flex-1 bg-gray-600 text-white p-2 rounded-l">
                        <div class="bg-gray-600 p-2 px-4">%</div>
                        <label class="ml-4 flex items-center">
                            <input type="checkbox" id="trailing_sl" class="form-checkbox text-blue-400">
                            <span class="ml-2">Trailing</span>
                        </label>
                    </div>
                </div>
                
                <!-- TP Controls -->
                <div class="bg-gray-700 p-3 rounded">
                    <label for="tp_percent" class="block mb-2">TP %:</label>
                    <div class="flex items-center">
                        <input type="number" id="tp_percent" value="2.0" step="0.1" min="0" 
                               class="flex-1 bg-gray-600 text-white p-2 rounded-l">
                        <div class="bg-gray-600 p-2 px-4">%</div>
                        <label class="ml-4 flex items-center">
                            <input type="checkbox" id="trailing_tp" class="form-checkbox text-blue-400">
                            <span class="ml-2">Trailing</span>
                        </label>
                    </div>
                </div>
                
                <!-- Results -->
                <div class="bg-gray-700 p-3 rounded col-span-1 md:col-span-2">
                    <div class="grid grid-cols-2 gap-4">
                        <div class="bg-red-900 p-3 rounded text-center">
                            <div class="font-bold">Stop Loss</div>
                            <div id="sl-result">--</div>
                        </div>
                        <div class="bg-green-900 p-3 rounded text-center">
                            <div class="font-bold">Take Profit</div>
                            <div id="tp-result">--</div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
        
        <!-- Price Alerts -->
        <div class="bg-gray-800 p-4 rounded-lg mb-6">
            <h2 class="text-xl font-bold mb-4">Price Alerts</h2>
            <div class="flex mb-4">
                <input type="number" id="price_alert_input" step="0.01" min="0" 
                       class="flex-1 bg-gray-600 text-white p-2 rounded-l">
                <button onclick="setPriceAlert()" 
                        class="bg-purple-600 hover:bg-purple-700 text-white px-4 py-2 rounded-r">
                    Set Alert
                </button>
            </div>
            <div id="active-alerts" class="bg-gray-700 p-3 rounded">
                <div class="font-bold mb-2">Active Alerts:</div>
                <div id="alerts-list"></div>
            </div>
        </div>
        
        <!-- Timeframe Panels -->
        <div class="grid grid-cols-1 md:grid-cols-2 gap-4 mb-6">
            {% for tf in timeframes %}
            <div class="bg-gray-800 p-4 rounded-lg">
                <h2 class="text-xl font-bold mb-4">{{ tf.upper() }} Timeframe</h2>
                <div id="{{ tf }}-indicators">
                    {% for ind in indicators %}
                    <div class="flex justify-between py-2 border-b border-gray-700">
                        <span class="text-blue-400">{{ ind }}:</span>
                        <span id="{{ tf }}-{{ ind }}">--</span>
                    </div>
                    {% endfor %}
                </div>
            </div>
            {% endfor %}
        </div>
        
        <!-- Alerts Display -->
        <div id="alert-container" class="fixed bottom-4 right-4 w-64 space-y-2"></div>
    </div>

    <script>
        const socket = io();
        
        // Handle price updates
        socket.on('price_update', function(data) {
            document.getElementById('price-display').textContent = data.price;
        });
        
        // Handle status updates
        socket.on('status', function(data) {
            document.getElementById('status-message').textContent = data.message;
        });
        
        // Handle error messages
        socket.on('error', function(data) {
            showAlert(data.message, 'bg-red-600');
        });
        
        // Handle indicator updates
        socket.on('indicators_update', function(data) {
            for (const tf in data.indicators) {
                for (const ind in data.indicators[tf]) {
                    const value = data.indicators[tf][ind];
                    const element = document.getElementById(`${tf}-${ind}`);
                    if (element) {
                        if (typeof value === 'object') {
                            // Handle BB which is an object
                            let bbText = '';
                            for (const band in value) {
                                bbText += `${band}: ${value[band]} `;
                            }
                            element.textContent = bbText;
                        } else {
                            element.textContent = value;
                        }
                    }
                }
            }
        });
        
        // Handle SL/TP updates
        socket.on('sltp_update', function(data) {
            document.getElementById('sl-result').textContent = data.sl;
            document.getElementById('tp-result').textContent = data.tp;
        });
        
        // Handle alerts
        socket.on('alert', function(data) {
            showAlert(data.message, 'bg-yellow-600');
            addToAlertsList(data.message);
        });
        
        // Handle price alert added
        socket.on('price_alert_added', function(data) {
            showAlert(`Price alert set at ${data.price}`, 'bg-purple-600');
            addToAlertsList(`Price alert: ${data.price}`);
        });
        
        // Show alert notification
        function showAlert(message, bgClass) {
            const alertContainer = document.getElementById('alert-container');
            const alertDiv = document.createElement('div');
            alertDiv.className = `${bgClass} text-white p-3 rounded-lg shadow-lg alert-flash`;
            alertDiv.textContent = message;
            alertContainer.appendChild(alertDiv);
            
            setTimeout(() => {
                alertDiv.classList.remove('alert-flash');
                setTimeout(() => {
                    alertDiv.remove();
                }, 1000);
            }, 5000);
        }
        
        // Add to alerts list
        function addToAlertsList(message) {
            const alertsList = document.getElementById('alerts-list');
            const alertItem = document.createElement('div');
            alertItem.className = 'py-1 border-b border-gray-600';
            alertItem.textContent = message;
            alertsList.appendChild(alertItem);
        }
        
        // Set position for SL/TP calculator
        function setPosition() {
            const entryPrice = parseFloat(document.getElementById('entry_price').value);
            const positionType = document.querySelector('input[name="position_type"]:checked').value;
            const slPercent = parseFloat(document.getElementById('sl_percent').value);
            const tpPercent = parseFloat(document.getElementById('tp_percent').value);
            const trailingSL = document.getElementById('trailing_sl').checked;
            const trailingTP = document.getElementById('trailing_tp').checked;
            
            if (isNaN(entryPrice)) {
                showAlert('Please enter a valid entry price', 'bg-red-600');
                return;
            }
            
            fetch('/set_position', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    entry_price: entryPrice.toFixed(2),
                    position_type: positionType,
                    sl_percent: slPercent.toFixed(1),
                    tp_percent: tpPercent.toFixed(1),
                    trailing_sl: trailingSL,
                    trailing_tp: trailingTP
                }),
            });
        }
        
        // Update alert settings
        function updateAlert(tf, ind) {
            const enabled = document.getElementById(`${ind}_${tf}_enable`).checked;
            const threshold = parseFloat(document.getElementById(`${ind}_${tf}_threshold`).value);
            
            fetch('/set_alert', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    timeframe: tf,
                    indicator: ind,
                    enabled: enabled,
                    threshold: threshold.toFixed(1)
                }),
            });
        }
        
        // Set price alert
        function setPriceAlert() {
            const priceInput = document.getElementById('price_alert_input');
            const price = parseFloat(priceInput.value);
            
            if (isNaN(price)) {
                showAlert('Please enter a valid price', 'bg-red-600');
                return;
            }
            
            fetch('/set_price_alert', {
                method: 'POST',
                headers: {
                    'Content-Type': 'application/json',
                },
                body: JSON.stringify({
                    price: price.toFixed(2)
                }),
            });
            
            priceInput.value = '';
        }
        
        // Set all checkboxes to checked on page load
        document.addEventListener('DOMContentLoaded', function() {
            const checkboxes = document.querySelectorAll('.enable-checkbox');
            checkboxes.forEach(checkbox => {
                checkbox.checked = true;
            });
        });
    </script>
</body>
</html>''')
    
    print("Starting server on http://localhost:5001")
    print("On your Android device, connect to the same network and visit:")
    print("http://<your-computer-ip>:5001")
    
    socketio.run(app, host='0.0.0.0', port=5001)