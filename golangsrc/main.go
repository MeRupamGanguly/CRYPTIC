package main

import (
	"bytes"
	"context"
	"encoding/json"
	"fmt"
	"html/template"
	"io"
	"log"
	"math"
	"net/http"
	"os"
	"os/signal"
	"strconv"
	"strings"
	"sync"
	"syscall"
	"time"

	"github.com/gin-gonic/gin"
	"github.com/gorilla/websocket"
	"github.com/markcheno/go-talib"
)

const (
	BinanceWSURL   = "wss://fstream.binance.com/ws/btcusdt@aggTrade"
	MaxCandles     = 250
	PricePrecision = 2
)

var (
	timeframes = []string{"1m", "30m", "1h", "4h"}
	indicators = []string{"RSI", "EMA20", "EMA50", "EMA200", "BB"}
)

type Candle struct {
	Time  time.Time
	Open  float64
	High  float64
	Low   float64
	Close float64
}

type BinanceWS struct {
	mu           sync.RWMutex
	connected    bool
	candles      map[string][]Candle
	currentPrice float64
	wsConn       *websocket.Conn
	ctx          context.Context
	cancel       context.CancelFunc
}

type IndicatorValues struct {
	RSI    float64
	EMA20  float64
	EMA50  float64
	EMA200 float64
	BB     struct {
		Upper  float64
		Middle float64
		Lower  float64
	}
}

type AlertConfig struct {
	Enabled   bool
	Threshold float64
}

type AlertManager struct {
	mu           sync.RWMutex
	alerts       map[string]map[string]AlertConfig
	activeAlerts map[string]bool
	priceAlerts  []float64
}

type SLTPCalculator struct {
	mu         sync.RWMutex
	entryPrice float64
	position   string
	slPercent  float64
	tpPercent  float64
	trailingSl bool
	trailingTp bool
}

type Client struct {
	conn *websocket.Conn
	send chan []byte
}

type Hub struct {
	mu      sync.RWMutex
	clients map[*Client]bool
}

type AggTradeMessage struct {
	EventType     string `json:"e"`
	EventTime     int64  `json:"E"`
	Symbol        string `json:"s"`
	AggTradeID    int64  `json:"a"`
	Price         string `json:"p"`
	Quantity      string `json:"q"`
	FirstTradeID  int64  `json:"f"`
	LastTradeID   int64  `json:"l"`
	TradeTime     int64  `json:"T"`
	IsMarketMaker bool   `json:"m"`
	Ignore        bool   `json:"M"`
}

var (
	binanceWS      *BinanceWS
	alertManager   *AlertManager
	sltpCalculator *SLTPCalculator
	hub            *Hub
)

func NewBinanceWS() *BinanceWS {
	ctx, cancel := context.WithCancel(context.Background())
	ws := &BinanceWS{
		candles: make(map[string][]Candle),
		ctx:     ctx,
		cancel:  cancel,
	}
	for _, tf := range timeframes {
		ws.candles[tf] = make([]Candle, 0, MaxCandles)
	}
	ws.fetchHistoricalData()
	return ws
}

func (ws *BinanceWS) fetchHistoricalData() {
	for _, tf := range timeframes {
		interval := map[string]string{
			"1m":  "1m",
			"30m": "30m",
			"1h":  "1h",
			"4h":  "4h",
		}[tf]

		url := fmt.Sprintf("https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=%s&limit=%d", interval, MaxCandles)
		resp, err := http.Get(url)
		if err != nil {
			log.Printf("Error fetching historical data for %s: %v", tf, err)
			continue
		}
		defer resp.Body.Close()

		var data [][]interface{}
		if err := json.NewDecoder(resp.Body).Decode(&data); err != nil {
			log.Printf("Error decoding historical data for %s: %v", tf, err)
			continue
		}

		candles := make([]Candle, 0, len(data))
		for _, d := range data {
			if len(d) < 5 {
				continue
			}
			open, _ := d[1].(string)
			high, _ := d[2].(string)
			low, _ := d[3].(string)
			closeVal, _ := d[4].(string)
			timestamp, _ := d[0].(float64)

			openF, _ := strconv.ParseFloat(open, 64)
			highF, _ := strconv.ParseFloat(high, 64)
			lowF, _ := strconv.ParseFloat(low, 64)
			closeF, _ := strconv.ParseFloat(closeVal, 64)

			candles = append(candles, Candle{
				Time:  time.Unix(int64(timestamp)/1000, 0),
				Open:  openF,
				High:  highF,
				Low:   lowF,
				Close: closeF,
			})
		}
		ws.mu.Lock()
		ws.candles[tf] = candles
		ws.mu.Unlock()
		log.Printf("Fetched %d %s candles from Binance", len(candles), tf)
	}
}

func (ws *BinanceWS) connect() {
	dialer := websocket.DefaultDialer
	conn, _, err := dialer.Dial(BinanceWSURL, nil)
	if err != nil {
		log.Printf("WebSocket connection error: %v", err)
		time.Sleep(5 * time.Second)
		go ws.connect()
		return
	}

	ws.mu.Lock()
	ws.wsConn = conn
	ws.connected = true
	ws.mu.Unlock()

	hub.broadcast([]byte(`{"type":"status","message":"Connected to Binance"}`))

	go ws.readMessages()
}

func (ws *BinanceWS) readMessages() {
	defer ws.wsConn.Close()

	for {
		select {
		case <-ws.ctx.Done():
			return
		default:
			_, message, err := ws.wsConn.ReadMessage()
			if err != nil {
				log.Printf("WebSocket read error: %v", err)
				ws.mu.Lock()
				ws.connected = false
				ws.mu.Unlock()
				hub.broadcast([]byte(`{"type":"status","message":"Disconnected from Binance"}`))
				time.Sleep(5 * time.Second)
				go ws.connect()
				return
			}

			var trade AggTradeMessage
			if err := json.Unmarshal(message, &trade); err != nil {
				log.Printf("Error parsing trade: %v", err)
				continue
			}

			price, err := strconv.ParseFloat(trade.Price, 64)
			if err != nil {
				log.Printf("Error parsing price: %v", err)
				continue
			}
			timestamp := time.Unix(0, trade.TradeTime*int64(time.Millisecond))

			ws.mu.Lock()
			ws.currentPrice = price
			ws.mu.Unlock()

			priceMsg := fmt.Sprintf(`{"type":"price_update","price":"%.2f"}`, price)
			hub.broadcast([]byte(priceMsg))

			ws.processTrade(price, timestamp)
		}
	}
}

func (ws *BinanceWS) processTrade(price float64, timestamp time.Time) {
	ws.mu.Lock()
	defer ws.mu.Unlock()

	for _, tf := range timeframes {
		candles := ws.candles[tf]
		if len(candles) == 0 {
			ws.addCandle(tf, timestamp, price)
			continue
		}

		lastCandle := candles[len(candles)-1]
		duration := timestamp.Sub(lastCandle.Time)
		threshold := time.Minute
		switch tf {
		case "30m":
			threshold = 30 * time.Minute
		case "1h":
			threshold = time.Hour
		case "4h":
			threshold = 4 * time.Hour
		}

		if duration >= threshold {
			ws.addCandle(tf, timestamp, price)
		} else {
			ws.updateLastCandle(tf, price)
		}
	}
}

func (ws *BinanceWS) addCandle(tf string, timestamp time.Time, price float64) {
	candles := ws.candles[tf]
	newCandle := Candle{
		Time:  timestamp,
		Open:  price,
		High:  price,
		Low:   price,
		Close: price,
	}

	if len(candles) >= MaxCandles {
		candles = candles[1:]
	}
	candles = append(candles, newCandle)
	ws.candles[tf] = candles
}

func (ws *BinanceWS) updateLastCandle(tf string, price float64) {
	candles := ws.candles[tf]
	lastIdx := len(candles) - 1
	candles[lastIdx].Close = price
	if price > candles[lastIdx].High {
		candles[lastIdx].High = price
	}
	if price < candles[lastIdx].Low {
		candles[lastIdx].Low = price
	}
}

func (ws *BinanceWS) getOHLCData(tf string) []Candle {
	ws.mu.RLock()
	defer ws.mu.RUnlock()
	return ws.candles[tf]
}

func NewAlertManager() *AlertManager {
	am := &AlertManager{
		alerts:       make(map[string]map[string]AlertConfig),
		activeAlerts: make(map[string]bool),
		priceAlerts:  make([]float64, 0),
	}

	for _, tf := range timeframes {
		am.alerts[tf] = make(map[string]AlertConfig)
		for _, ind := range indicators {
			am.alerts[tf][ind] = AlertConfig{Enabled: true, Threshold: 0.1}
		}
	}
	return am
}

func (am *AlertManager) checkAlerts(indicators map[string]IndicatorValues) {
	binanceWS.mu.RLock()
	currentPrice := binanceWS.currentPrice
	binanceWS.mu.RUnlock()

	am.mu.Lock()
	defer am.mu.Unlock()

	for tf, values := range indicators {
		// Check RSI alert
		if config, ok := am.alerts[tf]["RSI"]; ok && config.Enabled {
			key := tf + "_RSI"
			if math.Abs(currentPrice-values.RSI) <= (config.Threshold/100)*currentPrice {
				am.triggerAlert(key)
			}
		}

		// Check EMA alerts
		for _, ema := range []struct {
			name  string
			value float64
		}{
			{"EMA20", values.EMA20},
			{"EMA50", values.EMA50},
			{"EMA200", values.EMA200},
		} {
			if config, ok := am.alerts[tf][ema.name]; ok && config.Enabled {
				key := tf + "_" + ema.name
				if math.Abs(currentPrice-ema.value) <= (config.Threshold/100)*currentPrice {
					am.triggerAlert(key)
				}
			}
		}

		// Check Bollinger Bands alerts
		if config, ok := am.alerts[tf]["BB"]; ok && config.Enabled {
			for band, value := range map[string]float64{
				"upper":  values.BB.Upper,
				"middle": values.BB.Middle,
				"lower":  values.BB.Lower,
			} {
				key := fmt.Sprintf("%s_BB_%s", tf, band)
				if math.Abs(currentPrice-value) <= (config.Threshold/100)*currentPrice {
					am.triggerAlert(key)
				}
			}
		}
	}
}

func (am *AlertManager) triggerAlert(message string) {
	if !am.activeAlerts[message] {
		am.activeAlerts[message] = true
		alertMsg := fmt.Sprintf(`{"type":"alert","message":"%s"}`, message)
		hub.broadcast([]byte(alertMsg))
	}
}

func (am *AlertManager) addPriceAlert(price float64) {
	am.mu.Lock()
	defer am.mu.Unlock()

	am.priceAlerts = append(am.priceAlerts, price)
	alertMsg := fmt.Sprintf(`{"type":"price_alert_added","price":"%.2f"}`, price)
	hub.broadcast([]byte(alertMsg))
}

func (am *AlertManager) checkPriceAlerts(currentPrice float64) {
	am.mu.Lock()
	defer am.mu.Unlock()

	for i, alertPrice := range am.priceAlerts {
		diff := math.Abs(currentPrice - alertPrice)
		if diff <= 0.001*currentPrice {
			am.triggerAlert(fmt.Sprintf("Price reached %.2f", alertPrice))
			// Remove triggered alert
			am.priceAlerts = append(am.priceAlerts[:i], am.priceAlerts[i+1:]...)
			break
		}
	}
}

func NewSLTPCalculator() *SLTPCalculator {
	return &SLTPCalculator{
		position:   "LONG",
		slPercent:  1.0,
		tpPercent:  2.0,
		trailingSl: false,
		trailingTp: false,
	}
}

func (sc *SLTPCalculator) setPosition(entryPrice float64, positionType string) {
	sc.mu.Lock()
	defer sc.mu.Unlock()
	sc.entryPrice = entryPrice
	sc.position = positionType
}

func (sc *SLTPCalculator) calculateSL(currentPrice float64) float64 {
	sc.mu.RLock()
	defer sc.mu.RUnlock()

	if sc.position == "LONG" {
		return sc.entryPrice * (1 - sc.slPercent/100)
	}
	return sc.entryPrice * (1 + sc.slPercent/100)
}

func (sc *SLTPCalculator) calculateTP(currentPrice float64) float64 {
	sc.mu.RLock()
	defer sc.mu.RUnlock()

	if sc.position == "LONG" {
		return sc.entryPrice * (1 + sc.tpPercent/100)
	}
	return sc.entryPrice * (1 - sc.tpPercent/100)
}

func NewHub() *Hub {
	return &Hub{
		clients: make(map[*Client]bool),
	}
}

func (h *Hub) register(client *Client) {
	h.mu.Lock()
	defer h.mu.Unlock()
	h.clients[client] = true
}

func (h *Hub) unregister(client *Client) {
	h.mu.Lock()
	defer h.mu.Unlock()
	if _, ok := h.clients[client]; ok {
		delete(h.clients, client)
		close(client.send)
	}
}

func (h *Hub) broadcast(message []byte) {
	h.mu.RLock()
	defer h.mu.RUnlock()
	log.Printf("Broadcasting: %s", string(message))

	for client := range h.clients {
		select {
		case client.send <- message:
		default:
			go h.unregister(client)
		}
	}
}

func calculateIndicators() map[string]IndicatorValues {
	results := make(map[string]IndicatorValues)

	for _, tf := range timeframes {
		candles := binanceWS.getOHLCData(tf)
		if len(candles) < 200 {
			log.Printf("Not enough candles for %s (got %d)", tf, len(candles))
			continue
		}

		closes := make([]float64, len(candles))
		for i, candle := range candles {
			closes[i] = candle.Close
		}

		iv := IndicatorValues{}

		// RSI (14 periods)
		rsi := talib.Rsi(closes, 14)
		if len(rsi) > 0 {
			iv.RSI = rsi[len(rsi)-1]
		}

		// EMAs
		ema20 := talib.Ema(closes, 20)
		if len(ema20) > 0 {
			iv.EMA20 = ema20[len(ema20)-1]
		}

		ema50 := talib.Ema(closes, 50)
		if len(ema50) > 0 {
			iv.EMA50 = ema50[len(ema50)-1]
		}

		ema200 := talib.Ema(closes, 200)
		if len(ema200) > 0 {
			iv.EMA200 = ema200[len(ema200)-1]
		}

		// Bollinger Bands (20 periods, 2 stddev)
		upper, middle, lower := talib.BBands(closes, 20, 2.0, 2.0, talib.SMA)
		if len(upper) > 0 {
			iv.BB.Upper = upper[len(upper)-1]
			iv.BB.Middle = middle[len(middle)-1]
			iv.BB.Lower = lower[len(lower)-1]
		}

		results[tf] = iv
		log.Printf("Calculated %s indicators: RSI=%.2f, EMA20=%.2f, EMA50=%.2f, EMA200=%.2f",
			tf, iv.RSI, iv.EMA20, iv.EMA50, iv.EMA200)
	}

	return results
}

func backgroundTask() {
	ticker := time.NewTicker(5 * time.Second)
	defer ticker.Stop()

	for range ticker.C {
		// Calculate indicators
		indicators := calculateIndicators()
		indicatorMsg, _ := json.Marshal(map[string]interface{}{
			"type":       "indicators_update",
			"indicators": indicators,
		})
		hub.broadcast(indicatorMsg)

		// Check alerts
		alertManager.checkAlerts(indicators)

		// Check price alerts
		binanceWS.mu.RLock()
		currentPrice := binanceWS.currentPrice
		binanceWS.mu.RUnlock()
		alertManager.checkPriceAlerts(currentPrice)

		// Update SL/TP
		sltpCalculator.mu.RLock()
		entryPrice := sltpCalculator.entryPrice
		sltpCalculator.mu.RUnlock()

		if entryPrice > 0 {
			sl := sltpCalculator.calculateSL(currentPrice)
			tp := sltpCalculator.calculateTP(currentPrice)
			sltpMsg := fmt.Sprintf(`{"type":"sltp_update","sl":"%.2f","tp":"%.2f"}`, sl, tp)
			hub.broadcast([]byte(sltpMsg))
		}
	}
}

func handleWebSocket(c *gin.Context) {
	upgrader := websocket.Upgrader{
		CheckOrigin: func(r *http.Request) bool { return true },
	}

	conn, err := upgrader.Upgrade(c.Writer, c.Request, nil)
	if err != nil {
		log.Printf("WebSocket upgrade error: %v", err)
		return
	}

	client := &Client{
		conn: conn,
		send: make(chan []byte, 256),
	}
	hub.register(client)

	go client.writePump()
	client.readPump()
}

func (c *Client) readPump() {
	defer func() {
		hub.unregister(c)
		c.conn.Close()
	}()

	for {
		_, _, err := c.conn.ReadMessage()
		if err != nil {
			if websocket.IsUnexpectedCloseError(err, websocket.CloseGoingAway, websocket.CloseAbnormalClosure) {
				log.Printf("WebSocket read error: %v", err)
			}
			break
		}
	}
}

func (c *Client) writePump() {
	ticker := time.NewTicker(60 * time.Second)
	defer func() {
		ticker.Stop()
		c.conn.Close()
	}()

	for {
		select {
		case message, ok := <-c.send:
			if !ok {
				c.conn.WriteMessage(websocket.CloseMessage, []byte{})
				return
			}

			if err := c.conn.WriteMessage(websocket.TextMessage, message); err != nil {
				log.Printf("WebSocket write error: %v", err)
				return
			}
		case <-ticker.C:
			if err := c.conn.WriteMessage(websocket.PingMessage, nil); err != nil {
				return
			}
		}
	}
}

func indexHandler(c *gin.Context) {
	funcMap := template.FuncMap{
		"upper": strings.ToUpper,
	}

	tmpl := template.New("index.html").Funcs(funcMap)
	tmpl, err := tmpl.ParseFiles("static/index.html")
	if err != nil {
		c.AbortWithError(http.StatusInternalServerError, err)
		return
	}

	data := struct {
		Timeframes []string
		Indicators []string
	}{
		Timeframes: timeframes,
		Indicators: indicators,
	}

	err = tmpl.Execute(c.Writer, data)
	if err != nil {
		c.AbortWithError(http.StatusInternalServerError, err)
	}
}

func setPositionHandler(c *gin.Context) {
	var data struct {
		EntryPrice float64 `json:"entry_price"`
		Position   string  `json:"position_type"`
		SLPercent  float64 `json:"sl_percent"`
		TPPercent  float64 `json:"tp_percent"`
		TrailingSL bool    `json:"trailing_sl"`
		TrailingTP bool    `json:"trailing_tp"`
	}

	body, _ := c.GetRawData()
	log.Printf("Received position data: %s", string(body))
	c.Request.Body = io.NopCloser(bytes.NewBuffer(body))

	if err := c.ShouldBindJSON(&data); err != nil {
		log.Printf("Error binding position JSON: %v", err)
		c.JSON(http.StatusBadRequest, gin.H{"error": "Invalid request: " + err.Error()})
		return
	}

	sltpCalculator.setPosition(data.EntryPrice, data.Position)
	sltpCalculator.mu.Lock()
	sltpCalculator.slPercent = data.SLPercent
	sltpCalculator.tpPercent = data.TPPercent
	sltpCalculator.trailingSl = data.TrailingSL
	sltpCalculator.trailingTp = data.TrailingTP
	sltpCalculator.mu.Unlock()

	c.JSON(http.StatusOK, gin.H{"status": "success"})
}

func setAlertHandler(c *gin.Context) {
	var data struct {
		Timeframe string  `json:"timeframe"`
		Indicator string  `json:"indicator"`
		Enabled   bool    `json:"enabled"`
		Threshold float64 `json:"threshold"`
	}

	if err := c.BindJSON(&data); err != nil {
		c.JSON(http.StatusBadRequest, gin.H{"error": "Invalid request"})
		return
	}

	alertManager.mu.Lock()
	defer alertManager.mu.Unlock()

	if tfAlerts, ok := alertManager.alerts[data.Timeframe]; ok {
		if alert, ok := tfAlerts[data.Indicator]; ok {
			alert.Enabled = data.Enabled
			alert.Threshold = data.Threshold
			tfAlerts[data.Indicator] = alert
		}
	}

	c.JSON(http.StatusOK, gin.H{"status": "success"})
}

func setPriceAlertHandler(c *gin.Context) {
	var data struct {
		Price float64 `json:"price"`
	}

	body, _ := c.GetRawData()
	log.Printf("Received price alert request: %s", string(body))
	c.Request.Body = io.NopCloser(bytes.NewBuffer(body))

	if err := c.ShouldBindJSON(&data); err != nil {
		log.Printf("Error binding price alert JSON: %v", err)
		c.JSON(http.StatusBadRequest, gin.H{"error": "Invalid price format"})
		return
	}

	if data.Price <= 0 {
		c.JSON(http.StatusBadRequest, gin.H{"error": "Price must be positive"})
		return
	}

	alertManager.addPriceAlert(data.Price)
	c.JSON(http.StatusOK, gin.H{"status": "success"})
}

func main() {
	binanceWS = NewBinanceWS()
	alertManager = NewAlertManager()
	sltpCalculator = NewSLTPCalculator()
	hub = NewHub()

	go binanceWS.connect()
	go backgroundTask()

	router := gin.Default()
	router.Static("/static", "./static")
	router.GET("/", indexHandler)
	router.GET("/ws", handleWebSocket)
	router.POST("/set_position", setPositionHandler)
	router.POST("/set_alert", setAlertHandler)
	router.POST("/set_price_alert", setPriceAlertHandler)

	srv := &http.Server{
		Addr:    ":5001",
		Handler: router,
	}

	go func() {
		log.Println("Starting server on http://localhost:5001")
		if err := srv.ListenAndServe(); err != nil && err != http.ErrServerClosed {
			log.Fatal("Server error:", err)
		}
	}()

	quit := make(chan os.Signal, 1)
	signal.Notify(quit, syscall.SIGINT, syscall.SIGTERM)
	<-quit
	log.Println("Shutting down server...")

	binanceWS.cancel()

	ctx, cancel := context.WithTimeout(context.Background(), 5*time.Second)
	defer cancel()
	if err := srv.Shutdown(ctx); err != nil {
		log.Fatal("Server shutdown error:", err)
	}
	log.Println("Server exited")
}
