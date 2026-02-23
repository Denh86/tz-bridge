# QuantConnect Algorithm — SQQQ Webhook Test
# ============================================
# Adapted from Telegram24x7Test.
# Instead of Telegram, fires a webhook to your local Flask server (via ngrok).
#
# SETUP:
#   1. Start your Flask server:  python tz_webhook_server.py
#   2. Start ngrok:              ngrok http 5000
#   3. Paste the ngrok URL below as WEBHOOK_URL  (e.g. https://abc123.ngrok-free.app)
#   4. Deploy this to a QC Live Algorithm (paper brokerage is fine — orders go to TZ)
#
# WHAT IT DOES:
#   - Buys 10 shares of SQQQ at market open  (fires BUY webhook)
#   - Sells after 2 minutes                  (fires SELL webhook)
#   - Repeats all day
#   - QC itself does NOT route orders to any broker — it just fires webhooks
#   - Your Flask server receives the webhook and places the real order on TZ

from AlgorithmImports import *
import json

class SQQQWebhookTest(QCAlgorithm):

    # ── Paste your ngrok URL here (no trailing slash) ──────────────────────
    WEBHOOK_URL = "https://anthological-monumentless-verlie.ngrok-free.dev/webhook"

    def Initialize(self):
        self.SetStartDate(2026, 2, 23)
        self.SetCash(100000)  # Paper cash — not used for real orders

        # SQQQ — equity, trades during market hours only
        equity = self.AddEquity("SQQQ", Resolution.Minute)
        equity.SetDataNormalizationMode(DataNormalizationMode.Raw)
        self.sqqq = equity.Symbol

        # State machine
        self.state           = "WAITING_TO_BUY"
        self.last_action_time = None
        self.buy_price       = None
        self.cycle_count     = 0
        self.order_quantity  = 10          # shares per trade
        self.wait_minutes    = 2

        self.Log("INITIALIZED: SQQQ webhook test. Fires BUY/SELL webhooks every 2 min.")
        self.Log(f"WEBHOOK_URL: {self.WEBHOOK_URL}")

    def OnData(self, data):
        if not data.ContainsKey(self.sqqq) or data[self.sqqq] is None:
            return

        # Only trade during regular market hours
        if not self.IsMarketOpen(self.sqqq):
            return

        current_price = data[self.sqqq].Close
        current_time  = self.Time

        if self.state == "WAITING_TO_BUY":
            if (self.last_action_time is None or
                    (current_time - self.last_action_time).total_seconds() / 60.0 >= self.wait_minutes):
                self.ExecuteBuy(current_price)

        elif self.state == "HOLDING":
            minutes_held = (current_time - self.last_action_time).total_seconds() / 60.0
            if minutes_held >= self.wait_minutes:
                self.ExecuteSell(current_price)

    def ExecuteBuy(self, price):
        self.cycle_count += 1
        self.state            = "HOLDING"
        self.last_action_time = self.Time
        self.buy_price        = price

        signal = {
            "action":   "BUY",
            "symbol":   "SQQQ",
            "quantity": self.order_quantity,
            "price":    round(price, 2),
            "cycle":    self.cycle_count,
        }
        self.Log(f"SIGNAL BUY #{self.cycle_count} | Price: {price:.2f} | Qty: {self.order_quantity}")
        self._fire_webhook(signal)

    def ExecuteSell(self, price):
        pnl = (price - self.buy_price) * self.order_quantity
        self.state            = "WAITING_TO_BUY"
        self.last_action_time = self.Time

        signal = {
            "action":   "SELL",
            "symbol":   "SQQQ",
            "quantity": self.order_quantity,
            "price":    round(price, 2),
            "cycle":    self.cycle_count,
            "pnl_est":  round(pnl, 2),
        }
        self.Log(f"SIGNAL SELL #{self.cycle_count} | Price: {price:.2f} | Est P&L: ${pnl:.2f}")
        self._fire_webhook(signal)

    def _fire_webhook(self, signal):
        """Send signal to Flask server via QC's built-in Notify.Web."""
        try:
            # QC's Notify.Web sends a POST with the string as the body
            # Flask server uses force=True on get_json() so this works fine
            self.Notify.Web(self.WEBHOOK_URL, json.dumps(signal))
            self.Log(f"WEBHOOK FIRED: {json.dumps(signal)}")
        except Exception as e:
            self.Log(f"WEBHOOK ERROR: {e}")

    def OnOrderEvent(self, orderEvent):
        # QC won't have real orders since we're not routing through QC's broker
        # This fires only if QC paper fills (which we don't use)
        if orderEvent.Status == OrderStatus.Filled:
            self.Debug(f"QC Paper Fill: {orderEvent.Symbol} @ {orderEvent.FillPrice}")
