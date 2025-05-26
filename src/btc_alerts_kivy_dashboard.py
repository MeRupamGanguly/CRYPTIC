import json
import threading
from kivy.app import App
from kivy.clock import Clock, mainthread
from kivy.uix.boxlayout import BoxLayout
from kivy.uix.gridlayout import GridLayout
from kivy.uix.scrollview import ScrollView
from kivy.uix.label import Label
from kivy.uix.button import Button
from kivy.uix.checkbox import CheckBox
from kivy.uix.textinput import TextInput
from kivy.uix.popup import Popup
from kivy.uix.togglebutton import ToggleButton
from kivy.core.window import Window
from kivy.metrics import dp
import websocket
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
from ta.volatility import BollingerBands

COLORS = {
    "background": [0.12, 0.12, 0.12, 1],
    "text": [1, 1, 1, 1],
    "accent": [0.34, 0.61, 0.84, 1],
    "alert": [0.84, 0.73, 0.49, 1],
    "green": [0.38, 0.55, 0.31, 1],
    "red": [0.82, 0.41, 0.41, 1],
    "panel": [0.15, 0.15, 0.15, 1],
    "purple": [0.77, 0.53, 0.75, 1]
}

TIMEFRAMES = ['1m', '30m', '1h', '4h']
INDICATORS = ['RSI', 'EMA20', 'EMA50', 'EMA200', 'BB']

class BinanceWebSocket:
    def __init__(self, app):
        self.app = app
        self.connected = False
        self.candles = {tf: [] for tf in TIMEFRAMES}
        self.current_price = 0.0
        self.lock = threading.Lock()
        self.ws = None
        self.running = True
        self.connect()

    def connect(self):
        def on_open(ws):
            self.connected = True
            self.app.log("Connected to WebSocket")

        def on_message(ws, message):
            data = json.loads(message)
            price = float(data['p'])
            self.current_price = price
            self.process_trade(price, data['T'])

        def on_error(ws, error):
            self.app.log(f"WebSocket error: {error}")

        def on_close(ws, close_status_code, close_msg):
            self.connected = False
            self.app.log("WebSocket closed")
            if self.running:
                threading.Timer(5, self.connect).start()

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
            'open': price,
            'high': price,
            'low': price,
            'close': price
        })
        if len(self.candles[tf]) > 100:
            self.candles[tf].pop(0)

    def update_last_candle(self, candle, price):
        candle['close'] = price
        candle['high'] = max(candle['high'], price)
        candle['low'] = min(candle['low'], price)

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
    def __init__(self, app):
        self.app = app
        self.alerts = {tf: {ind: {'enabled': False, 'threshold': 0.5} 
                         for ind in INDICATORS} for tf in TIMEFRAMES}
        self.active_alerts = set()

    def check_alerts(self, indicators):
        if not indicators:
            return
            
        current_price = self.app.ws.current_price
        for tf in indicators:
            for name, value in indicators[tf].items():
                if name == 'BB':
                    for band, val in value.items():
                        self.check_single_alert(current_price, val, f"{tf}_{name}_{band}")
                else:
                    self.check_single_alert(current_price, value, f"{tf}_{name}")

    def check_single_alert(self, price, value, key):
        parts = key.split('_')
        tf = parts[0]
        indicator_name = parts[1]
        
        if len(parts) > 2:  # BB case
            indicator_name = f"{parts[1]}_{parts[2]}"

        if indicator_name.startswith('BB'):
            indicator_name = 'BB'
            
        alert_config = self.alerts[tf].get(indicator_name, {})
        if not alert_config:
            return

        if alert_config['enabled']:
            threshold = alert_config['threshold']
            if abs(price - value) <= (threshold / 100 * price):
                self.trigger_alert(key)

    def trigger_alert(self, message):
        if message not in self.active_alerts:
            self.active_alerts.add(message)
            self.app.log(f"ALERT: {message}")
            self.show_alert(message)

    def show_alert(self, message):
        content = BoxLayout(orientation='vertical')
        popup = Popup(title='Alert', content=content, size_hint=(0.8, 0.4))
        content.add_widget(Label(text=message, font_size=24))
        content.add_widget(Button(text='OK', on_press=popup.dismiss,
                                background_color=COLORS['red']))
        popup.open()

class SLTPCalculator:
    def __init__(self):
        self.entry_price = 0.0
        self.position_type = 'LONG'
        self.sl_percent = 1.0
        self.tp_percent = 2.0
        self.trailing_sl = False
        self.trailing_tp = False

    def set_position(self, entry_price, position_type):
        self.entry_price = entry_price
        self.position_type = position_type

    def calculate_sl(self, current_price):
        if self.position_type == 'LONG':
            return self.entry_price * (1 - self.sl_percent/100)
        return self.entry_price * (1 + self.sl_percent/100)

    def calculate_tp(self, current_price):
        if self.position_type == 'LONG':
            return self.entry_price * (1 + self.tp_percent/100)
        return self.entry_price * (1 - self.tp_percent/100)

class MainLayout(ScrollView):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.app = App.get_running_app()
        self.do_scroll_x = False
        self.bar_width = dp(10)
        
        main_box = BoxLayout(orientation='vertical', size_hint_y=None)
        main_box.bind(minimum_height=main_box.setter('height'))
        
        # Price Display
        price_box = BoxLayout(size_hint_y=None, height=dp(60))
        self.price_label = Label(text="BTCUSDT.P: --", font_size=24,
                                color=COLORS['accent'])
        price_box.add_widget(self.price_label)
        main_box.add_widget(price_box)
        
        # Alert Controls
        main_box.add_widget(AlertControls())
        
        # SL/TP Calculator
        self.sltp_calculator_ui = SLTPCalculatorUI()
        main_box.add_widget(self.sltp_calculator_ui)
        
        # Timeframe Panels
        self.timeframe_panels = {}
        timeframes_box = GridLayout(cols=2, size_hint_y=None, spacing=dp(10))
        timeframes_box.bind(minimum_height=timeframes_box.setter('height'))
        for tf in TIMEFRAMES:
            panel = TimeframePanel(tf=tf)
            self.timeframe_panels[tf] = panel
            timeframes_box.add_widget(panel)
        main_box.add_widget(timeframes_box)
        
        # Price Alerts
        main_box.add_widget(PriceAlertsSection())
        
        self.add_widget(main_box)

class AlertControls(GridLayout):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.app = App.get_running_app()
        self.cols = len(TIMEFRAMES) + 2  # Indicator + timeframes + threshold
        self.rows = len(INDICATORS) + 1
        self.size_hint_y = None
        self.height = dp(200)
        self.padding = dp(5)
        self.spacing = dp(5)
        
        # Header row
        self.add_widget(Label(text="Indicator"))
        for tf in TIMEFRAMES:
            self.add_widget(Label(text=tf.upper()))
        self.add_widget(Label(text="Threshold %"))
        
        # Indicator rows
        for ind in INDICATORS:
            self.add_widget(Label(text=ind))
            for tf in TIMEFRAMES:
                cb = CheckBox(active=False)
                cb.bind(active=lambda instance, value, tf=tf, ind=ind: 
                        self.on_checkbox_active(instance, value, tf, ind))
                self.add_widget(cb)
            threshold = TextInput(text='0.5', size_hint_x=None, width=dp(80))
            threshold.bind(text=lambda instance, value, ind=ind: 
                          self.on_threshold_change(instance, value, ind))
            self.add_widget(threshold)
    
    def on_checkbox_active(self, instance, value, tf, ind):
        self.app.alert_manager.alerts[tf][ind]['enabled'] = value
    
    def on_threshold_change(self, instance, value, ind):
        try:
            for tf in TIMEFRAMES:
                self.app.alert_manager.alerts[tf][ind]['threshold'] = float(value)
        except ValueError:
            pass

class SLTPCalculatorUI(BoxLayout):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.app = App.get_running_app()
        self.orientation = 'vertical'
        self.size_hint_y = None
        self.height = dp(250)
        self.padding = dp(5)
        self.spacing = dp(5)
        
        # Position Type
        pos_box = BoxLayout(size_hint_y=None, height=dp(30))
        self.long_btn = ToggleButton(text='LONG', group='position', state='down')
        self.short_btn = ToggleButton(text='SHORT', group='position')
        pos_box.add_widget(self.long_btn)
        pos_box.add_widget(self.short_btn)
        self.add_widget(pos_box)
        
        # Entry Price
        entry_box = BoxLayout(size_hint_y=None, height=dp(30))
        entry_box.add_widget(Label(text="Entry Price:"))
        self.entry_price = TextInput(multiline=False)
        entry_box.add_widget(self.entry_price)
        entry_box.add_widget(Button(text="Set", on_press=self.set_position))
        self.add_widget(entry_box)
        
        # SL Controls
        sl_box = BoxLayout(size_hint_y=None, height=dp(30))
        sl_box.add_widget(Label(text="SL %:"))
        self.sl_percent = TextInput(text='1.0', multiline=False)
        sl_box.add_widget(self.sl_percent)
        self.trailing_sl = CheckBox()
        sl_box.add_widget(Label(text="Trailing"))
        sl_box.add_widget(self.trailing_sl)
        self.add_widget(sl_box)
        
        # TP Controls
        tp_box = BoxLayout(size_hint_y=None, height=dp(30))
        tp_box.add_widget(Label(text="TP %:"))
        self.tp_percent = TextInput(text='2.0', multiline=False)
        tp_box.add_widget(self.tp_percent)
        self.trailing_tp = CheckBox()
        tp_box.add_widget(Label(text="Trailing"))
        tp_box.add_widget(self.trailing_tp)
        self.add_widget(tp_box)
        
        # Results
        result_box = BoxLayout(size_hint_y=None, height=dp(30))
        self.sl_result = Label(text="SL: --", color=COLORS['red'])
        self.tp_result = Label(text="TP: --", color=COLORS['green'])
        result_box.add_widget(self.sl_result)
        result_box.add_widget(self.tp_result)
        self.add_widget(result_box)
    
    def set_position(self, instance):
        try:
            entry_price = float(self.entry_price.text)
            position_type = 'LONG' if self.long_btn.state == 'down' else 'SHORT'
            self.app.sltp_calculator.set_position(entry_price, position_type)
            self.app.sltp_calculator.sl_percent = float(self.sl_percent.text)
            self.app.sltp_calculator.tp_percent = float(self.tp_percent.text)
            self.app.sltp_calculator.trailing_sl = self.trailing_sl.active
            self.app.sltp_calculator.trailing_tp = self.trailing_tp.active
            self.update_display()
        except ValueError:
            pass
    
    def update_display(self):
        current_price = self.app.ws.current_price
        sl = self.app.sltp_calculator.calculate_sl(current_price)
        tp = self.app.sltp_calculator.calculate_tp(current_price)
        self.sl_result.text = f"SL: {sl:.2f}"
        self.tp_result.text = f"TP: {tp:.2f}"

class TimeframePanel(BoxLayout):
    def __init__(self, tf, **kwargs):
        super().__init__(**kwargs)
        self.orientation = 'vertical'
        self.size_hint_y = None
        self.height = dp(150)
        self.padding = dp(5)
        self.spacing = dp(2)
        self.tf = tf
        
        self.add_widget(Label(text=f"{tf.upper()} Timeframe", bold=True))
        self.indicators = {}
        for ind in INDICATORS:
            row = BoxLayout(size_hint_y=None, height=dp(25))
            row.add_widget(Label(text=ind, size_hint_x=0.4))
            value_label = Label(text="--", size_hint_x=0.6)
            self.indicators[ind] = value_label
            row.add_widget(value_label)
            self.add_widget(row)

class PriceAlertsSection(BoxLayout):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.app = App.get_running_app()
        self.orientation = 'vertical'
        self.size_hint_y = None
        self.height = dp(150)
        self.padding = dp(5)
        self.spacing = dp(5)
        
        self.add_widget(Label(text="Price Alerts", bold=True))
        grid = GridLayout(cols=4, rows=2, size_hint_y=None, height=dp(80))
        self.alert_inputs = []
        for _ in range(8):
            ti = TextInput(multiline=False)
            self.alert_inputs.append(ti)
            grid.add_widget(ti)
        self.add_widget(grid)
        self.add_widget(Button(text="Set All Alerts", on_press=self.set_alerts))
    
    def set_alerts(self, instance):
        self.app.active_price_alerts = []
        for ti in self.alert_inputs:
            if ti.text:
                try:
                    price = float(ti.text)
                    self.app.active_price_alerts.append(price)
                    ti.text = ""
                except ValueError:
                    pass
class BTCApp(App):
    def build(self):
        Window.clearcolor = COLORS['background']
        self.ws = BinanceWebSocket(self)
        self.alert_manager = AlertManager(self)
        self.sltp_calculator = SLTPCalculator()
        self.active_price_alerts = []
        
        Clock.schedule_interval(self.update_ui, 1)
        return MainLayout()
    
    @mainthread
    def update_ui(self, dt):
        # Update price
        self.root.price_label.text = f"BTCUSDT.P: {self.ws.current_price:.2f}"
        
        # Calculate indicators
        indicators = self.calculate_indicators()
        
        # Update timeframe panels
        if indicators:
            for tf in TIMEFRAMES:
                panel = self.root.timeframe_panels.get(tf)
                if panel and indicators.get(tf):
                    for ind in INDICATORS:
                        if ind == 'BB':
                            bb = indicators[tf].get('BB', {})
                            panel.indicators[ind].text = f"U:{bb.get('upper', 0):.2f} M:{bb.get('middle', 0):.2f} L:{bb.get('lower', 0):.2f}"
                        else:
                            value = indicators[tf].get(ind, 0)
                            panel.indicators[ind].text = f"{value:.2f}"
        
        # Check alerts
        self.alert_manager.check_alerts(indicators)
        
        # Update SL/TP display
        self.root.sltp_calculator_ui.update_display()
        
        # Check price alerts
        current_price = self.ws.current_price
        for alert_price in self.active_price_alerts[:]:
            if abs(current_price - alert_price) <= (0.01 * current_price):
                self.alert_manager.trigger_alert(f"Price near {alert_price:.2f}!")
                self.active_price_alerts.remove(alert_price)
    
    def calculate_indicators(self):
        indicators = {}
        for tf in TIMEFRAMES:
            df = self.ws.get_ohlc_data(tf)
            if df.empty or len(df) < 20:
                continue
                
            indicators[tf] = {}
            
            # RSI
            try:
                rsi = RSIIndicator(df['close'], window=14).rsi()
                indicators[tf]['RSI'] = rsi.iloc[-1]
            except Exception as e:
                self.log(f"RSI Error ({tf}): {str(e)}")
                indicators[tf]['RSI'] = 0

            # EMAs
            try:
                ema20 = EMAIndicator(df['close'], window=20).ema_indicator()
                indicators[tf]['EMA20'] = ema20.iloc[-1]
            except Exception as e:
                self.log(f"EMA20 Error ({tf}): {str(e)}")
                indicators[tf]['EMA20'] = 0

            try:
                ema50 = EMAIndicator(df['close'], window=50).ema_indicator()
                indicators[tf]['EMA50'] = ema50.iloc[-1]
            except Exception as e:
                self.log(f"EMA50 Error ({tf}): {str(e)}")
                indicators[tf]['EMA50'] = 0

            try:
                ema200 = EMAIndicator(df['close'], window=200).ema_indicator()
                indicators[tf]['EMA200'] = ema200.iloc[-1]
            except Exception as e:
                self.log(f"EMA200 Error ({tf}): {str(e)}")
                indicators[tf]['EMA200'] = 0

            # Bollinger Bands
            try:
                bb = BollingerBands(df['close'], window=20, window_dev=2)
                indicators[tf]['BB'] = {
                    'upper': bb.bollinger_hband().iloc[-1],
                    'middle': bb.bollinger_mavg().iloc[-1],
                    'lower': bb.bollinger_lband().iloc[-1]
                }
            except Exception as e:
                self.log(f"BB Error ({tf}): {str(e)}")
                indicators[tf]['BB'] = {'upper': 0, 'middle': 0, 'lower': 0}
            
        return indicators
    
    def log(self, message):
        print(message)

if __name__ == '__main__':
    BTCApp().run()