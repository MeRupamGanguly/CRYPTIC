import tkinter as tk
from tkinter import ttk, messagebox
import websocket
import json
import threading
import pandas as pd
from ta.momentum import RSIIndicator
from ta.trend import EMAIndicator
from ta.volatility import BollingerBands

COLORS = {
    "background": "#1e1e1e",
    "text": "#ffffff",
    "accent": "#569cd6",
    "alert": "#d7ba7d",
    "green": "#608b4e",
    "red": "#d16969",
    "panel": "#252526",
    "purple": "#C586C0"
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
            self.app.status_var.set("Connected")

        def on_message(ws, message):
            data = json.loads(message)
            price = float(data['p'])
            self.current_price = price
            self.process_trade(price, data['T'])

        def on_error(ws, error):
            self.app.log(f"WebSocket error: {error}")

        def on_close(ws, close_status_code, close_msg):
            self.connected = False
            self.app.status_var.set("Disconnected")
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
    def __init__(self, root):
        self.root = root
        self.alerts = {tf: {ind: {'enabled': False, 'threshold': 0.5} 
                         for ind in INDICATORS} for tf in TIMEFRAMES}
        self.active_alerts = set()

    def check_alerts(self, indicators):
        current_price = self.root.app.ws.current_price
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
            self.root.app.log(f"ALERT: {message}")
            self.show_alert(message)

    def show_alert(self, message):
        alert_win = tk.Toplevel(self.root)
        alert_win.geometry("400x200")
        alert_win.configure(bg=COLORS['alert'])
        label = tk.Label(alert_win, text=message, font=('Arial', 24), bg=COLORS['alert'])
        label.pack(expand=True)
        tk.Button(alert_win, text="OK", command=lambda: self.close_alert(alert_win, message),
                 bg=COLORS['red']).pack(pady=10)

    def close_alert(self, window, message):
        self.active_alerts.remove(message)
        window.destroy()

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

class BTCAlertDashboard:
    def __init__(self, root):
        root.app = self
        self.root = root
        self.root.title("Binance BTC Dashboard")
        self.root.configure(bg=COLORS['background'])
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)
        # self.root.geometry("800x1200")

        # Initialize components
        self.ws = BinanceWebSocket(self)
        self.alert_manager = AlertManager(root)
        self.sltp_calculator = SLTPCalculator()

        # Status bar must be initialized before UI updates
        self.status_var = tk.StringVar(value="Connecting to Binance...")
        self.status_bar = tk.Label(root, textvariable=self.status_var, bd=1, 
                                 relief=tk.SUNKEN, anchor=tk.W, bg=COLORS['background'], fg=COLORS['text'])
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

        # UI setup
        self.setup_ui()
        self.update_ui()

        # Status bar
        self.status_var = tk.StringVar(value="Connecting to Binance...")
        self.status_bar = tk.Label(root, textvariable=self.status_var, bd=1, 
                                 relief=tk.SUNKEN, anchor=tk.W, bg=COLORS['background'], fg=COLORS['text'])
        self.status_bar.pack(side=tk.BOTTOM, fill=tk.X)

    def on_close(self):
            print("on_close Called")
            self.ws.running = False
            self.ws.ws.close()
            self.root.destroy()

    def setup_ui(self):
        main_frame = tk.Frame(self.root, bg=COLORS['background'])
        main_frame.pack(fill='both', expand=True, padx=10, pady=10)

        # Price display
        price_frame = tk.Frame(main_frame, bg=COLORS['background'])
        price_frame.pack(fill='x', pady=(0, 20))
        
        self.price_label = tk.Label(
            price_frame, 
            text="BTCUSDT.P: --", 
            font=('Helvetica', 24, 'bold'), 
            fg=COLORS['accent'], 
            bg=COLORS['background']
        )
        self.price_label.pack(side='left')

        # Alert Controls
        self.setup_alert_controls(main_frame)
        
        # SL/TP Calculator
        self.setup_sltp_calculator(main_frame)
        
        # Timeframe panels
        self.setup_timeframe_panels(main_frame)

        # Price alerts section
        self.setup_price_alerts(main_frame)



    def setup_price_alerts(self, parent):
        alert_frame = tk.LabelFrame(
            parent,
            text="Price Alerts",
            font=('Helvetica', 12, 'bold'),
            fg=COLORS['text'],
            bg=COLORS['background'],
            bd=2,
            relief='ridge',
            padx=10,
            pady=10
        )
        alert_frame.pack(fill='x', pady=10)

        self.price_alert_entries = []
        
        entry_frame = tk.Frame(alert_frame, bg=COLORS['background'])
        entry_frame.pack(fill='x', pady=5)

        # Create 8 entry fields in 2 rows and 4 columns
        for i in range(8):
            row = i // 4
            col = i % 4

            sub_frame = tk.Frame(entry_frame, bg=COLORS['background'])
            sub_frame.grid(row=row, column=col, padx=8, pady=5)

            var = tk.StringVar()
            entry = tk.Entry(
                sub_frame,
                width=10,
                textvariable=var,
                bg=COLORS['background'],
                fg=COLORS['text'],
                insertbackground=COLORS['text'],
                highlightbackground=COLORS['accent'],
                highlightthickness=1
            )
            entry.pack()
            self.price_alert_entries.append(var)

        # Button to set alerts
        btn_frame = tk.Frame(alert_frame, bg=COLORS['background'])
        btn_frame.pack(fill='x', pady=5)

        set_btn = tk.Button(
            btn_frame,
            text="Set All Alerts",
            command=self.set_price_alerts,
            bg=COLORS['accent'],
            fg='black',
            font=('Helvetica', 10, 'bold'),
            relief='raised',
            padx=5,
            pady=2
        )
        set_btn.pack(pady=5)

    def set_price_alerts(self):
        self.active_price_alerts = []
        for entry in self.price_alert_entries:
            price_str = entry.get()
            if price_str:
                try:
                    price = float(price_str)
                    self.active_price_alerts.append(price)
                    entry.set("")  # Clear the input field
                except ValueError:
                    messagebox.showerror("Error", f"Invalid price: {price_str}")
        
        if self.active_price_alerts:
            messagebox.showinfo("Alerts Set", 
                              f"Price alerts set at: {', '.join(map(str, self.active_price_alerts))}")

    def setup_alert_controls(self, parent):
        alert_frame = tk.LabelFrame(
            parent, 
            text="Alert Controls", 
            font=('Helvetica', 12, 'bold'), 
            fg=COLORS['text'], 
            bg=COLORS['background'], 
            bd=2, 
            relief='ridge'
        )
        alert_frame.pack(fill='x', pady=10)
        
        # Dictionary to store our variables
        self.alert_vars = {}
        
        # Create grid for alerts
        for i, indicator in enumerate(INDICATORS):
            tk.Label(
                alert_frame, 
                text=indicator, 
                font=('Helvetica', 10), 
                fg=COLORS['accent'], 
                bg=COLORS['background']
            ).grid(row=i+1, column=0, padx=5, pady=2, sticky='w')
            
            for j, timeframe in enumerate(TIMEFRAMES):
                if i == 0:
                    tk.Label(
                        alert_frame, 
                        text=timeframe.upper(), 
                        font=('Helvetica', 10), 
                        fg=COLORS['purple'], 
                        bg=COLORS['background']
                    ).grid(row=0, column=j+1, padx=5, pady=2)
                
                # Create unique key for this indicator-timeframe combo
                var_key = f"{indicator}_{timeframe}"
                
                # Enable checkbox with default checked
                self.alert_vars[var_key] = tk.BooleanVar(value=True)
                self.alert_manager.alerts[timeframe][indicator].update({'enabled': True})
                
                cb = tk.Checkbutton(
                    alert_frame, 
                    variable=self.alert_vars[var_key], 
                    command=lambda tf=timeframe, ind=indicator, vk=var_key: 
                        self.alert_manager.alerts[tf][ind].update({'enabled': self.alert_vars[vk].get()}),
                    bg=COLORS['background'], 
                    fg=COLORS['text'], 
                    selectcolor=COLORS['background'],
                    activebackground=COLORS['background']
                )
                cb.grid(row=i+1, column=j+1, padx=5, pady=2)
                                
                # Threshold entry
                threshold_var = tk.StringVar(value="0.01")
                entry = tk.Entry(
                    alert_frame, 
                    textvariable=threshold_var, 
                    width=5,
                    bg=COLORS['background'], 
                    fg=COLORS['text'], 
                    insertbackground=COLORS['text']
                )
                entry.grid(row=i+1, column=len(TIMEFRAMES)+1+j, padx=5, pady=2)
                entry.bind('<FocusOut>', lambda e, tf=timeframe, ind=indicator, tv=threshold_var: 
                        self.alert_manager.alerts[tf][ind].update({'threshold': float(tv.get())}))

    def setup_sltp_calculator(self, parent):
        sltp_frame = tk.LabelFrame(
            parent, 
            text="SL/TP Calculator", 
            font=('Helvetica', 12, 'bold'), 
            fg=COLORS['text'], 
            bg=COLORS['background'], 
            bd=2, 
            relief='ridge'
        )
        sltp_frame.pack(fill='x', pady=10)
        
        # Position type
        pos_frame = tk.Frame(sltp_frame, bg=COLORS['background'])
        pos_frame.pack(fill='x', pady=5)
        
        self.position_var = tk.StringVar(value='LONG')
        tk.Radiobutton(
            pos_frame, 
            text="LONG", 
            variable=self.position_var, 
            value='LONG',
            bg=COLORS['background'], 
            fg=COLORS['green'], 
            selectcolor=COLORS['background']
        ).pack(side='left', padx=5)
        
        tk.Radiobutton(
            pos_frame, 
            text="SHORT", 
            variable=self.position_var, 
            value='SHORT',
            bg=COLORS['background'], 
            fg=COLORS['red'], 
            selectcolor=COLORS['background']
        ).pack(side='left', padx=5)
        
        # Entry price
        entry_frame = tk.Frame(sltp_frame, bg=COLORS['background'])
        entry_frame.pack(fill='x', pady=5)
        
        tk.Label(
            entry_frame, 
            text="Entry Price:", 
            font=('Helvetica', 10), 
            fg=COLORS['text'], 
            bg=COLORS['background']
        ).pack(side='left')
        
        self.entry_price_var = tk.StringVar()
        entry_entry = tk.Entry(
            entry_frame, 
            textvariable=self.entry_price_var, 
            font=('Helvetica', 10), 
            bg=COLORS['background'], 
            fg=COLORS['text'], 
            insertbackground=COLORS['text']
        )
        entry_entry.pack(side='left', padx=5)
        
        set_entry_btn = tk.Button(
            entry_frame, 
            text="Set", 
            command=self.set_position, 
            font=('Helvetica', 10), 
            bg=COLORS['accent'], 
            fg='black'
        )
        set_entry_btn.pack(side='left')
        
        # SL controls
        sl_frame = tk.Frame(sltp_frame, bg=COLORS['background'])
        sl_frame.pack(fill='x', pady=5)
        
        tk.Label(
            sl_frame, 
            text="SL %:", 
            font=('Helvetica', 10), 
            fg=COLORS['text'], 
            bg=COLORS['background']
        ).pack(side='left')
        
        self.sl_percent_var = tk.StringVar(value="1.0")
        sl_entry = tk.Entry(
            sl_frame, 
            textvariable=self.sl_percent_var, 
            width=5,
            bg=COLORS['background'], 
            fg=COLORS['text'], 
            insertbackground=COLORS['text']
        )
        sl_entry.pack(side='left', padx=5)
        
        self.trailing_sl_var = tk.BooleanVar(value=False)
        trailing_sl_check = tk.Checkbutton(
            sl_frame, 
            text="Trailing", 
            variable=self.trailing_sl_var, 
            command=self.update_trailing_sl,
            bg=COLORS['background'], 
            fg=COLORS['text'], 
            selectcolor=COLORS['background']
        )
        trailing_sl_check.pack(side='left', padx=5)
        
        # TP controls
        tp_frame = tk.Frame(sltp_frame, bg=COLORS['background'])
        tp_frame.pack(fill='x', pady=5)
        
        tk.Label(
            tp_frame, 
            text="TP %:", 
            font=('Helvetica', 10), 
            fg=COLORS['text'], 
            bg=COLORS['background']
        ).pack(side='left')
        
        self.tp_percent_var = tk.StringVar(value="2.0")
        tp_entry = tk.Entry(
            tp_frame, 
            textvariable=self.tp_percent_var, 
            width=5,
            bg=COLORS['background'], 
            fg=COLORS['text'], 
            insertbackground=COLORS['text']
        )
        tp_entry.pack(side='left', padx=5)
        
        self.trailing_tp_var = tk.BooleanVar(value=False)
        trailing_tp_check = tk.Checkbutton(
            tp_frame, 
            text="Trailing", 
            variable=self.trailing_tp_var, 
            command=self.update_trailing_tp,
            bg=COLORS['background'], 
            fg=COLORS['text'], 
            selectcolor=COLORS['background']
        )
        trailing_tp_check.pack(side='left', padx=5)
        
        # Results
        result_frame = tk.Frame(sltp_frame, bg=COLORS['background'])
        result_frame.pack(fill='x', pady=5)
        
        self.sl_result_var = tk.StringVar(value="SL: --")
        sl_result = tk.Label(
            result_frame, 
            textvariable=self.sl_result_var, 
            font=('Helvetica', 10, 'bold'), 
            fg=COLORS['red'], 
            bg=COLORS['background']
        )
        sl_result.pack(side='left', expand=True)
        
        self.tp_result_var = tk.StringVar(value="TP: --")
        tp_result = tk.Label(
            result_frame, 
            textvariable=self.tp_result_var, 
            font=('Helvetica', 10, 'bold'), 
            fg=COLORS['green'], 
            bg=COLORS['background']
        )
        tp_result.pack(side='left', expand=True)

    def setup_timeframe_panels(self, parent):
        timeframe_frame = tk.Frame(parent, bg=COLORS['background'])
        timeframe_frame.pack(fill='both', expand=True)
        
        self.timeframe_panels = {}
        self.indicator_vars = {}
        
        for i, tf in enumerate(TIMEFRAMES):
            panel = self.create_timeframe_panel(timeframe_frame, tf)
            panel.grid(row=i//2, column=i%2, padx=5, pady=5, sticky='nsew')
            self.timeframe_panels[tf] = panel
            
        timeframe_frame.grid_rowconfigure(0, weight=1)
        timeframe_frame.grid_rowconfigure(1, weight=1)
        timeframe_frame.grid_columnconfigure(0, weight=1)
        timeframe_frame.grid_columnconfigure(1, weight=1)

    def create_timeframe_panel(self, parent, timeframe):
        panel = tk.Frame(parent, bg=COLORS['panel'], bd=2, relief='ridge', padx=10, pady=10)
        
        # Title
        title = tk.Label(
            panel, 
            text=f"{timeframe.upper()} Timeframe", 
            font=('Helvetica', 12, 'bold'), 
            fg=COLORS['text'], 
            bg=COLORS['panel']
        )
        title.pack(fill='x', pady=(0, 10))
        
        # Indicators frame
        indicators_frame = tk.Frame(panel, bg=COLORS['panel'])
        indicators_frame.pack(fill='both', expand=True)
        
        # Create indicator displays
        self.indicator_vars[timeframe] = {}
        for indicator in INDICATORS:
            frame = tk.Frame(indicators_frame, bg=COLORS['panel'])
            frame.pack(fill='x', pady=2)
            
            tk.Label(
                frame, 
                text=f"{indicator}:", 
                font=('Helvetica', 10), 
                fg=COLORS['accent'], 
                bg=COLORS['panel'], 
                width=8,
                anchor='w'
            ).pack(side='left')
            
            var = tk.StringVar(value="--")
            self.indicator_vars[timeframe][indicator] = var
            
            tk.Label(
                frame, 
                textvariable=var, 
                font=('Helvetica', 10), 
                fg=COLORS['text'], 
                bg=COLORS['panel'], 
                anchor='w'
            ).pack(side='left', fill='x', expand=True)
        
        return panel

    def set_position(self):
        try:
            entry_price = float(self.entry_price_var.get())
            position_type = self.position_var.get()
            self.sltp_calculator.set_position(entry_price, position_type)
            
            # Update SL/TP percentages
            self.sltp_calculator.sl_percent = float(self.sl_percent_var.get())
            self.sltp_calculator.tp_percent = float(self.tp_percent_var.get())
            
            self.update_sltp_display()
        except ValueError:
            messagebox.showerror("Error", "Invalid entry price")

    def update_trailing_sl(self):
        self.sltp_calculator.trailing_sl = self.trailing_sl_var.get()

    def update_trailing_tp(self):
        self.sltp_calculator.trailing_tp = self.trailing_tp_var.get()

    def update_sltp_display(self):
        current_price = self.ws.current_price
        sl = self.sltp_calculator.calculate_sl(current_price)
        tp = self.sltp_calculator.calculate_tp(current_price)
        
        self.sl_result_var.set(f"SL: {sl:.2f}")
        self.tp_result_var.set(f"TP: {tp:.2f}")

    def calculate_indicators(self):
        indicators = {}
        for tf in TIMEFRAMES:
            df = self.ws.get_ohlc_data(tf)
            if df.empty or len(df) < 20:
                continue
                
            indicators[tf] = {}
            
            # RSI
            rsi = RSIIndicator(df['close'], window=14).rsi()
            indicators[tf]['RSI'] = rsi.iloc[-1]
            self.indicator_vars[tf]['RSI'].set(f"{rsi.iloc[-1]:.2f}")
            
            # EMAs
            ema20 = EMAIndicator(df['close'], window=20).ema_indicator()
            indicators[tf]['EMA20'] = ema20.iloc[-1]
            self.indicator_vars[tf]['EMA20'].set(f"{ema20.iloc[-1]:.2f}")
            
            ema50 = EMAIndicator(df['close'], window=50).ema_indicator()
            indicators[tf]['EMA50'] = ema50.iloc[-1]
            self.indicator_vars[tf]['EMA50'].set(f"{ema50.iloc[-1]:.2f}")
            
            ema200 = EMAIndicator(df['close'], window=200).ema_indicator()
            indicators[tf]['EMA200'] = ema200.iloc[-1]
            self.indicator_vars[tf]['EMA200'].set(f"{ema200.iloc[-1]:.2f}")
            
            # Bollinger Bands   
            bb = BollingerBands(df['close'], window=20, window_dev=2)
            indicators[tf]['BB'] = {
                'upper': bb.bollinger_hband().iloc[-1],
                'middle': bb.bollinger_mavg().iloc[-1],
                'lower': bb.bollinger_lband().iloc[-1]
            }
            self.indicator_vars[tf]['BB'].set(
                f"U:{indicators[tf]['BB']['upper']:.2f} M:{indicators[tf]['BB']['middle']:.2f} L:{indicators[tf]['BB']['lower']:.2f}"
            )
            
        return indicators

    def update_ui(self):
        # Update price
        self.price_label.config(text=f"BTCUSDT.P: {self.ws.current_price:.2f}")
        
        # Calculate indicators
        indicators = self.calculate_indicators()
        
        # Check alerts
        self.alert_manager.check_alerts(indicators)
        
        # Update SL/TP if position is set
        if self.sltp_calculator.entry_price > 0:
            self.update_sltp_display()
        
        # Check price alerts
        if self.ws.current_price > 0 and hasattr(self, 'active_price_alerts'):
            current_price = self.ws.current_price
            for alert_price in self.active_price_alerts:
                diff = abs(current_price - alert_price)
                if diff <= (0.0001 * current_price):  # 1% threshold
                    self.trigger_price_alert(alert_price)
                    self.active_price_alerts.remove(alert_price)

        # Update status
        self.status_var.set("Connected" if self.ws.connected else "Disconnected")
        
        # Schedule next update
        self.root.after(1000, self.update_ui)

    def trigger_price_alert(self, price):
        message = f"Price near {price:.2f}!"
        self.alert_manager.trigger_alert(message)

    def log(self, message):
        print(message)


if __name__ == "__main__":
    root = tk.Tk()
    app = BTCAlertDashboard(root)
    root.mainloop()