# Place these lines AT THE VERY TOP of your file, outside any class definitions.
import clr
clr.AddReference("NodaTime")
from NodaTime import DateTimeZoneProviders
# End CLR Imports
# region Imports
from AlgorithmImports import *
from datetime import timedelta, time, date
import numpy as np
import pandas as pd
from QuantConnect.Indicators import RelativeStrengthIndex, IntradayVwap
from QuantConnect.Data.Market import TradeBar
from QuantConnect.Orders.Fees import OrderFee
from QuantConnect.Securities import Security
# ── WEBHOOK ADDITIONS ──────────────────────────────────────────────────────────
import threading
import urllib.request
import json as _json
# ──────────────────────────────────────────────────────────────────────────────
# endregion


# ========================================
# TRADEZERO FEE MODEL
# ========================================
class TradeZeroFeeModel(FeeModel):
    def GetOrderFee(self, parameters):
        order = parameters.Order
        security = parameters.Security
        quantity = abs(order.Quantity)
        fill_price = security.Price
        is_marketable = True

        if order.Type == OrderType.Limit:
            limit_price = order.LimitPrice
            current_price = security.Price
            if order.Quantity < 0:
                is_marketable = (limit_price <= current_price)
            else:
                is_marketable = (limit_price >= current_price)

        if quantity < 200:
            return OrderFee(CashAmount(0.99, "USD"))

        if fill_price < 1.0:
            if quantity <= 250000:
                fee = max(0.99, min(7.95, quantity * 0.005))
            else:
                fee = 7.95 + ((quantity - 250000) * 0.005)
            return OrderFee(CashAmount(fee, "USD"))

        if is_marketable:
            fee = max(0.99, quantity * 0.005)
            return OrderFee(CashAmount(fee, "USD"))
        else:
            return OrderFee(CashAmount(0.0, "USD"))


# ========================================
# LOCATE FEE MODEL
# ========================================
class LocateFeeModel:
    def __init__(self):
        self.daily_locates_charged = {}
        self.last_reset_date = None

    def ResetDaily(self, current_date):
        if self.last_reset_date != current_date:
            self.daily_locates_charged = {}
            self.last_reset_date = current_date

    def GetLocateFeePercentage(self, price):
        if price >= 10.0:
            return 0.0003
        elif price >= 2.0:
            return 0.0035
        elif price < 1.0:
            return 0.015
        else:
            return 0.010

    def CalculateLocateFee(self, symbol, price, quantity):
        current_date = self.algorithm.Time.date() if hasattr(self, 'algorithm') else None
        if current_date:
            self.ResetDaily(current_date)

        if symbol in self.daily_locates_charged:
            return 0.0

        position_value = abs(price * quantity)
        fee_percentage = self.GetLocateFeePercentage(price)
        locate_fee = position_value * fee_percentage
        self.daily_locates_charged[symbol] = locate_fee
        return locate_fee


# ========================================
# MAIN STRATEGY: RSI CROSSDOWN SHORT
# WITH KALMAN-SMOOTHED ROLLING KELLY SIZING
# ========================================
class SimpleRsiCrossdownShort(QCAlgorithm):
    def Initialize(self):
        self.SetStartDate(2026, 1, 1)
        self.SetEndDate(2026, 3, 21)
        self.SetCash(30000)

        # ========================================
        # WEBHOOK CONFIG
        # ========================================
        self.webhook_url          = self.GetParameter("webhook_url") or "https://tz-bridge.onrender.com/webhook"
        self._webhook_short_fired = {}
        self._webhook_cover_fired = {}
        self._webhook_cycle       = {}

        # ========================================
        # CONSOLIDATION SETTINGS
        # ========================================
        self.consolidation_minutes = 1

        # ========================================
        # RSI SETTINGS
        # ========================================
        self.rsi_period         = 9
        self.rsi_entry_level    = 69
        self.rsi_oversold_level = 31

        # ========================================
        # UNIVERSE SELECTION
        # ========================================
        self.max_positions       = 1
        self.min_gainer_rank     = 1
        self.max_gainer_rank     = 7
        self.min_gain_percent    = 0.3
        self.min_absolute_volume = 10000

        # ========================================
        # ENTRY REQUIREMENTS
        # ========================================
        self.require_above_vwap             = False
        self.require_higher_than_last_entry = False

        # ========================================
        # EXIT CRITERIA
        # ========================================
        self.profit_target_percent = 0.2
        self.use_rsi_recross_exit  = False
        self.max_holding_minutes   = 120
        self.hard_stop_percent     = 0.05

        # ========================================
        # POSITION SIZING — TARGET AND HARD LIMITS
        # ========================================
        # Target position size in a NEUTRAL regime —
        # i.e. when Kalman signal = 1.0 and rolling
        # Kelly = global_kelly.  This is the "typical"
        # position size you want the strategy to deploy.
        # The Kalman/Kelly system scales around this.
        self.target_size_pct  = 0.20   # 20% of equity at baseline

        # Hard ceiling — the system cannot exceed this
        # even in exceptional regimes (signal > 1.0).
        # Set to 1.0 if you want uncapped upside.
        self.max_position_pct = 0.35   # 35% of equity absolute maximum

        # Hard floor — we still trade at this minimum
        # even in mildly unfavourable regimes.
        # Set to 0.0 to allow full system shutdown.
        self.min_position_pct = 0.05   # 5% of equity minimum

        # ========================================
        # KALMAN + ROLLING KELLY CONFIG
        # ========================================

        # Rolling window for Kelly estimation.
        # 200 trades ≈ 4–6 weeks of activity at
        # this strategy's typical trade frequency.
        # Shorter = more reactive, noisier.
        # Longer = smoother, slower to adapt.
        self.kelly_window     = 200

        # Minimum trades before we trust the
        # rolling estimate. Before this threshold
        # the fallback global_kelly is used.
        self.kelly_min_trades = 30

        # Pre-computed baseline Kelly fraction from
        # prior backtests on this strategy variant.
        # Used as:
        #   (a) fallback before min_trades reached
        #   (b) denominator for Kalman normalisation
        # Re-derive this after any major param change.
        self.global_kelly = 0.061   # 6.1% — from 2025 full-year backtest

        # Trade history ring buffer: list of
        # (profit_pct: float, is_win: bool).
        # Capped at kelly_window * 2 to avoid
        # unbounded memory growth over long runs.
        self.trade_history = []

        # ── Kalman Filter State ───────────────────
        # The Kalman filter tracks a DIMENSIONLESS
        # performance MULTIPLIER — not a Kelly
        # fraction, not a P&L percentage.
        #
        #   State:  kalman_x  = smoothed multiplier
        #                       1.0 = baseline (normal)
        #                       > 1 = outperforming
        #                       < 1 = underperforming
        #
        # The Kalman is updated ONCE per day in
        # DailyReset.  The observation fed to it is:
        #
        #   obs = ComputeRollingKelly() / global_kelly
        #
        # This is also a dimensionless ratio around 1.0,
        # so state and observation live on the SAME SCALE.
        # The filter smooths out the day-to-day noise in
        # the rolling Kelly estimate.
        #
        # CRITICAL: Do NOT feed daily_return_frac as the
        # observation.  Daily returns (~0.5–2%) are on a
        # completely different scale to Kelly (~6%), which
        # causes K≈0.95 to immediately collapse the state.
        #
        # Tuning:
        #   Q = process noise — how fast true performance
        #       can drift.  0.005 = allows ~0.07× drift
        #       per day at steady state.
        #   R = observation noise — how noisy the rolling
        #       Kelly ratio is as a daily signal.  0.10
        #       is appropriate for a 200-trade window at
        #       ~35% win rate.
        #   Increase Q → reacts faster to regime change
        #   Increase R → smoother, trusts observations less
        self.kalman_x = 1.0    # multiplier: 1.0 = baseline performance
        self.kalman_P = 0.50   # initial uncertainty: high (no history yet)
        self.kalman_Q = 0.005  # process noise: moderate daily drift allowed
        self.kalman_R = 0.10   # observation noise: rolling Kelly is noisy

        # ── Drawdown Gate ─────────────────────────
        # A hard scalar that cuts position size
        # when the portfolio is in a drawdown from
        # its all-time peak.  Applied on top of the
        # Kelly/Kalman signal.
        #
        #   DD from peak    →  scalar applied
        #   0% – 5%         →  1.00  (full size)
        #   5% – 15%        →  0.50  (half size)
        #   15% – 25%       →  0.25  (quarter size)
        #   > 25%           →  0.00  (stop trading)
        #
        # Adjust thresholds to taste.  Tighter
        # thresholds = more protection but more
        # missed recovery.
        self.dd_thresholds = [
            (-0.05,  1.00),   # < 5% DD  → full
            (-0.15,  0.50),   # < 15% DD → half
            (-0.25,  0.25),   # < 25% DD → quarter
        ]
        self.dd_stop_scalar = 0.00          # beyond worst threshold

        # Peak equity for DD calculation.
        # Initialised in OnWarmupFinished once
        # the starting equity is known.
        self.peak_equity = None

        # ========================================
        # RISK CONTROLS
        # ========================================
        self.max_daily_loss_percent   = 0.5
        self.daily_starting_equity    = None
        self.daily_loss_limit_hit     = False
        self.block_reentry_after_loss = False
        self.daily_traded_symbols     = {}
        self.ticker_trade_counts      = {}
        self.max_trades_per_ticker    = 10

        # ========================================
        # TRADING HOURS
        # ========================================
        self.entry_start_time   = time(9, 5)
        self.entry_end_time     = time(15, 25)
        self.monitor_start_time = time(9, 0)
        self.monitor_end_time   = time(16, 0)

        # ========================================
        # TRACKING & DATA
        # ========================================
        self.symbol_data           = {}
        self.gainer_rankings       = {}
        self.position_entry_times  = {}
        self.position_entry_prices = {}
        self.last_entry_prices     = {}
        self.closing_in_progress   = set()

        # Track current trade's entry price for
        # recording the result in OnOrderEvent.
        self._pending_entry_prices = {}

        # ========================================
        # FEE MODELS
        # ========================================
        self.locate_fee_model          = LocateFeeModel()
        self.locate_fee_model.algorithm = self
        self.total_locate_fees_paid    = 0.0

        # ========================================
        # DAILY SUMMARY
        # ========================================
        self.daily_trades_log          = []
        self.daily_fees_locate         = 0.0
        self.daily_starting_total_fees = 0.0
        self.daily_realized_pnl        = 0

        # ========================================
        # SETUP
        # ========================================
        self.SetWarmUp(timedelta(days=2))
        self.UniverseSettings.Resolution = Resolution.Minute
        self.UniverseSettings.ExtendedMarketHours = True

        self.nyse_tz = DateTimeZoneProviders.Tzdb.GetZoneOrNull("America/New_York")
        self.SetTimeZone(self.nyse_tz)
        self.Securities.DataTimeZone = self.nyse_tz

        self.AddUniverse(self.CoarseFilter, self.FineFilter)
        self.SetSecurityInitializer(lambda security: security.SetFeeModel(TradeZeroFeeModel()))

        # ========================================
        # SCHEDULING
        # ========================================
        self.Schedule.On(self.DateRules.EveryDay(), self.TimeRules.At(16, 10), self.DailyReset)
        self.Schedule.On(self.DateRules.EveryDay(), self.TimeRules.At(15, 55), self.NuclearLiquidation)
        self.Schedule.On(self.DateRules.EveryDay(), self.TimeRules.At(15, 59), self.NuclearLiquidation)

        self.last_top_time           = None
        self.cached_top_gainers      = []
        self.cached_gainer_rankings  = {}
        self._last_status_min        = -1

        self.Debug(f"WEBHOOK: {'SET -> ' + self.webhook_url if self.webhook_url else 'NOT SET (dry-run)'}")
        self.Debug(
            f"SIZING: target={self.target_size_pct*100:.0f}%  "
            f"Kelly window={self.kelly_window}  "
            f"global_kelly={self.global_kelly*100:.2f}%  "
            f"max={self.max_position_pct*100:.0f}%  "
            f"min={self.min_position_pct*100:.0f}%  "
            f"Kalman_x0={self.kalman_x:.1f}× Q={self.kalman_Q} R={self.kalman_R}"
        )

    # ============================================================
    # KALMAN + ROLLING KELLY SIZING SYSTEM
    # ============================================================

    def ComputeRollingKelly(self):
        """
        Compute the Kelly fraction from the most recent
        kelly_window trades.

        Returns the raw Kelly fraction (e.g. 0.061 = 6.1%).
        Returns self.global_kelly if insufficient history.

        Kelly formula:  f* = W - L/R
          W = win rate
          L = 1 - W
          R = avg_win_pct / avg_loss_pct  (both positive)
        """
        recent = self.trade_history[-self.kelly_window:]
        if len(recent) < self.kelly_min_trades:
            return self.global_kelly

        wins   = [p for p, w in recent if w]
        losses = [p for p, w in recent if not w]

        if not wins or not losses:
            return self.global_kelly

        W       = len(wins) / len(recent)
        L       = 1.0 - W
        avg_win = sum(wins) / len(wins)
        avg_los = abs(sum(losses) / len(losses))

        if avg_los < 1e-9:
            return self.global_kelly

        R = avg_win / avg_los
        k = W - (L / R)
        # Clamp: never negative (Kelly says don't trade),
        # never above 1.0 (that would be leverage).
        return max(0.0, min(k, 1.0))

    def KalmanUpdate(self, observation):
        """
        Update the Kalman filter with today's rolling Kelly ratio.

        observation = ComputeRollingKelly() / global_kelly

        Both the state (kalman_x) and the observation are
        dimensionless multipliers centred on 1.0, so they
        live on the same scale and the filter works correctly.

        The filter is conservative (Q=0.005, R=0.10) so a
        single bad day moves the state modestly.  After P
        settles (~1 week), K ≈ 0.25, meaning each new
        observation shifts the state by ~25% of the gap —
        smooth enough to ignore noise, responsive enough
        to catch genuine regime changes.
        """
        # Predict step (no dynamics: alpha drifts slowly)
        P_pred = self.kalman_P + self.kalman_Q

        # Update step
        K              = P_pred / (P_pred + self.kalman_R)
        self.kalman_x += K * (observation - self.kalman_x)
        self.kalman_P  = (1.0 - K) * P_pred

    def ComputeDrawdownScalar(self):
        """
        Return the drawdown gearing scalar based on current
        distance from peak equity.

        0% – 5%  drawdown  → 1.00 (full size)
        5% – 15% drawdown  → 0.50 (half)
        15% – 25% drawdown → 0.25 (quarter)
        > 25% drawdown     → 0.00 (stop)
        """
        if self.peak_equity is None or self.peak_equity <= 0:
            return 1.0

        current = self.Portfolio.TotalPortfolioValue
        dd      = (current - self.peak_equity) / self.peak_equity  # negative in drawdown

        for threshold, scalar in self.dd_thresholds:
            if dd >= threshold:
                return scalar

        return self.dd_stop_scalar  # beyond worst threshold → stop

    def ComputeDynamicSizePct(self):
        """
        Compute position size as a fraction of equity.

        Formula:
            size = target_size_pct × kalman_x × dd_scalar

        target_size_pct (20%)
            Anchor — the position size at baseline
            performance.  Tune this to change the
            "normal" size.  Not related to Kelly.

        kalman_x  (0 – 2×, baseline = 1.0)
            Smoothed regime multiplier from the Kalman
            filter.  Starts at 1.0 (= baseline = 20%).
            Updated daily with the rolling Kelly ratio
            (rk / global_kelly), so it drifts toward
            current strategy conditions.
              Good regime  → kalman_x > 1 → size > 20%
              Baseline     → kalman_x = 1 → size = 20%
              Bad regime   → kalman_x < 1 → size < 20%
            The Kalman filter smooths out the noisiness
            of the rolling Kelly estimate.

        dd_scalar  (0 – 1×)
            Drawdown gearing.  Applied as a final
            multiplier.  Returns 0.0 to fully stop
            trading past the worst threshold.

        Result is clamped to [min_position_pct, max_position_pct].
        """
        # Kalman multiplier — clamp to [0, 2×]
        kal_mult  = max(0.0, min(self.kalman_x, 2.0))

        # Drawdown gate
        dd_scalar = self.ComputeDrawdownScalar()
        if dd_scalar == 0.0:
            return 0.0

        raw_size = self.target_size_pct * kal_mult * dd_scalar

        return max(self.min_position_pct, min(raw_size, self.max_position_pct))

    def RecordTradeResult(self, symbol, fill_price, quantity):
        """
        Called from OnOrderEvent when a buy-to-cover fills.
        Records the completed trade (profit_pct, is_win) into
        the rolling trade_history buffer for Kelly estimation.

        Uses self._pending_entry_prices to retrieve the entry
        price that was saved at order placement.
        """
        entry_price = self._pending_entry_prices.pop(symbol, None)
        if entry_price is None or entry_price <= 0:
            return

        # Short P&L: positive when price fell
        profit_pct = (entry_price - fill_price) / entry_price * 100.0
        is_win     = profit_pct > 0.0

        self.trade_history.append((profit_pct, is_win))

        # Trim buffer to 2 × window to bound memory
        max_buffer = self.kelly_window * 2
        if len(self.trade_history) > max_buffer:
            self.trade_history = self.trade_history[-max_buffer:]

    def GetSizingDebugStr(self):
        rk       = self.ComputeRollingKelly()
        rk_ratio = rk / max(self.global_kelly, 1e-9)
        kal_mult = max(0.0, min(self.kalman_x, 2.0))
        dd_sc    = self.ComputeDrawdownScalar()
        final    = self.ComputeDynamicSizePct()
        n        = len(self.trade_history)
        peak_dd  = (
            (self.Portfolio.TotalPortfolioValue - self.peak_equity)
            / self.peak_equity * 100
            if self.peak_equity else 0
        )
        return (
            f"rk={rk*100:.2f}% ({rk_ratio:.2f}×base) | "
            f"Kal_x={self.kalman_x:.3f}× | "
            f"DD_gate={dd_sc:.2f} | "
            f"→ size={final*100:.2f}% | "
            f"n_hist={n} | DD_from_peak={peak_dd:.1f}%"
        )

    # ============================================================
    # WEBHOOK HELPERS
    # ============================================================

    def _fire_webhook(self, action, symbol, quantity, price):
        ticker = str(symbol).split(" ")[0]
        cycle  = self._webhook_cycle.get(ticker, 1)
        payload = _json.dumps({
            "action":   action,
            "symbol":   ticker,
            "quantity": int(quantity),
            "price":    round(float(price), 4),
            "cycle":    int(cycle),
        }).encode("utf-8")
        if not self.LiveMode or self.IsWarmingUp:
            self.Debug(
                f"WEBHOOK (backtest/warmup, not sent) → action={action} symbol={ticker} "
                f"qty={quantity} price={price:.4f} cycle={cycle}"
            )
            return
        if not self.webhook_url:
            self.Debug(f"WEBHOOK DRY-RUN | {action} {ticker} qty={quantity} price=${price:.4f}")
            return
        def _send():
            try:
                req = urllib.request.Request(
                    self.webhook_url, data=payload,
                    headers={"Content-Type": "application/json"}, method="POST"
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = resp.read().decode("utf-8", errors="replace")[:200]
                self.Debug(f"WEBHOOK OK | {action} {ticker} | {resp.status} | {body}")
            except Exception as e:
                self.Debug(f"WEBHOOK ERROR | {action} {ticker} | {e}")
        threading.Thread(target=_send, daemon=True).start()

    def _reset_webhook_flags(self, ticker):
        self._webhook_short_fired.pop(ticker, None)
        self._webhook_cover_fired.pop(ticker, None)

    # ============================================================
    # WARMUP FINISHED
    # ============================================================

    def OnWarmupFinished(self):
        self.Debug(f"OnWarmupFinished at {self.Time} — universe size: {len(self.symbol_data)}")
        seeded      = self.SeedDayOpenPrices(list(self.symbol_data.keys()))
        already_set = sum(1 for sd in self.symbol_data.values() if sd.day_open_price is not None)
        if seeded > 0:
            self.Debug(f"Seeded day_open_price for {seeded} symbol(s) via history fallback.")
        self.Debug(
            f"day_open_price populated: {already_set}/{len(self.symbol_data)} symbols "
            f"({'warmup replay' if seeded == 0 else 'warmup + seed'})"
        )

        equity = self.Portfolio.TotalPortfolioValue
        self.daily_starting_equity     = equity
        self.daily_starting_total_fees = self.Portfolio.TotalFees
        self.peak_equity               = equity   # initialise peak tracker

        self.Debug(f"daily_starting_equity set to ${equity:.2f}")
        self.Debug(f"Kalman initial state: x={self.kalman_x:.3f}× (multiplier) P={self.kalman_P:.4f}")
        if self.LiveMode:
            self.LogMarketStatus(force=True)

    def SeedDayOpenPrices(self, symbols):
        today        = self.Time.date()
        seeded_count = 0
        for symbol in symbols:
            sym_data = self.symbol_data.get(symbol)
            if sym_data is None or sym_data.day_open_price is not None:
                continue
            try:
                history = self.History(symbol, timedelta(hours=24), Resolution.Minute)
                if history is None or history.empty:
                    continue
                bar_times  = (history.index.get_level_values('time')
                              if isinstance(history.index, pd.MultiIndex)
                              else history.index)
                today_bars = history.iloc[[t.date() == today for t in bar_times]]
                if today_bars.empty:
                    continue
                sym_data.day_open_price = float(today_bars.iloc[0]['open'])
                seeded_count += 1
                self.Debug(f"  SeedDayOpen: {symbol} open=${sym_data.day_open_price:.4f}")
            except Exception as e:
                self.Debug(f"  SeedDayOpen ERROR for {symbol}: {e}")
        return seeded_count

    # ============================================================
    # UNIVERSE FILTERS
    # ============================================================

    def CoarseFilter(self, coarse):
        return [x.Symbol for x in coarse
                if x.HasFundamentalData
                and x.DollarVolume > 1
                and x.Price >= 0.2
                and x.Price <= 15]

    def FineFilter(self, fine):
        return [f.Symbol for f in fine
                if f.CompanyProfile.SharesOutstanding >= 50000
                and f.CompanyProfile.SharesOutstanding <= 30_000_000]

    # ============================================================
    # SECURITY MANAGEMENT
    # ============================================================

    def OnSecuritiesChanged(self, changes):
        for security in changes.RemovedSecurities:
            symbol = security.Symbol
            if symbol in self.Portfolio and self.Portfolio[symbol].Invested:
                continue
            if symbol in self.symbol_data:
                self.CancelOrdersForSymbol(symbol)
                del self.symbol_data[symbol]

        for security in changes.AddedSecurities:
            symbol = security.Symbol
            if symbol not in self.symbol_data:
                self.symbol_data[symbol] = SymbolData(self, symbol)

            if self.consolidation_minutes > 1:
                self.Consolidate(
                    symbol,
                    timedelta(minutes=self.consolidation_minutes),
                    self.OnConsolidatedBar
                )

            if not self.IsWarmingUp:
                sym_data = self.symbol_data[symbol]
                if sym_data.day_open_price is None:
                    seeded = self.SeedDayOpenPrices([symbol])
                    if seeded > 0:
                        self.Debug(
                            f"OnSecuritiesChanged: seeded {symbol} "
                            f"open=${sym_data.day_open_price:.4f}"
                        )

    # ============================================================
    # CONSOLIDATED BAR HANDLER (3/5/10 min)
    # ============================================================

    def OnConsolidatedBar(self, bar):
        symbol = bar.Symbol
        if symbol not in self.symbol_data:
            return
        sym_data = self.symbol_data[symbol]

        sym_data.rsi.Update(bar.EndTime, bar.Close)

        if bar.Time.date() == self.Time.date() and sym_data.day_open_price is None:
            sym_data.day_open_price = bar.Open

        self.ProcessRsiCross(symbol, bar, sym_data)

        if self.Portfolio[symbol].Invested:
            self.CheckExitConditions(symbol, bar)

    # ============================================================
    # ONDATA — MAIN LOOP
    # ============================================================

    def OnData(self, data):
        if (self.LiveMode
                and not self.IsWarmingUp
                and self.Time.minute % 5 == 0
                and self.Time.minute != self._last_status_min):
            self._last_status_min = self.Time.minute
            self.LogMarketStatus()

        for symbol in data.keys():
            if symbol not in self.symbol_data:
                continue
            if data[symbol] is None:
                continue
            bar = data[symbol]
            if not hasattr(bar, 'EndTime') or not hasattr(bar, 'Close'):
                continue

            sym_data = self.symbol_data[symbol]
            sym_data.vwap.Update(bar)

            if self.consolidation_minutes == 1:
                sym_data.rsi.Update(bar.EndTime, bar.Close)

                if bar.Time.date() == self.Time.date() and sym_data.day_open_price is None:
                    sym_data.day_open_price = bar.Open

                self.ProcessRsiCross(symbol, bar, sym_data)

                if self.Portfolio[symbol].Invested and symbol not in self.closing_in_progress:
                    self.CheckExitConditions(symbol, bar)

        # Cancel stale open orders (> 10 min old)
        if not self.IsWarmingUp:
            for order in self.Transactions.GetOpenOrders():
                if (self.UtcTime - order.Time).total_seconds() > 600:
                    self.Transactions.CancelOrder(order.Id)

        # Age-based time stop
        if not self.IsWarmingUp:
            for symbol in list(self.position_entry_times.keys()):
                if symbol in self.Portfolio and self.Portfolio[symbol].Invested:
                    minutes_held = (
                        (self.Time - self.position_entry_times[symbol]).total_seconds() / 60.0
                    )
                    if minutes_held > self.max_holding_minutes:
                        price = self.GetValidPrice(symbol)
                        self.daily_trades_log.append(
                            f"AGE-OUT {symbol}: held {minutes_held:.1f}min"
                        )
                        self.Debug(f"AGE-OUT {symbol}: held {minutes_held:.1f}min")
                        self.ClosePosition(symbol, f"AGE-OUT ({minutes_held:.1f}min)", price)

    # ============================================================
    # RSI CROSS PROCESSING
    # ============================================================

    def ProcessRsiCross(self, symbol, bar, sym_data):
        if not sym_data.rsi.IsReady:
            return

        rsi_value = sym_data.rsi.Current.Value
        prev_rsi  = sym_data.last_rsi_value

        if prev_rsi is None:
            sym_data.last_rsi_value = rsi_value
            return

        if prev_rsi >= self.rsi_entry_level and rsi_value < self.rsi_entry_level:
            sym_data.last_rsi_cross_time = self.Time
            if not self.Portfolio[symbol].Invested:
                self.CheckRsiCrossEntry(symbol, bar)

        if self.use_rsi_recross_exit:
            if prev_rsi < self.rsi_entry_level and rsi_value >= self.rsi_entry_level:
                if (self.Portfolio[symbol].Invested
                        and symbol not in self.closing_in_progress
                        and not self.Transactions.GetOpenOrders(symbol)):
                    self.ClosePosition(
                        symbol,
                        f"RSI RECROSS ABOVE {self.rsi_entry_level}",
                        bar.Close
                    )

        sym_data.last_rsi_value = rsi_value

    # ============================================================
    # ENTRY LOGIC
    # ============================================================

    def CheckRsiCrossEntry(self, symbol, bar):
        if self.IsWarmingUp:
            return
        if self.daily_loss_limit_hit or not self.IsInEntryHours():
            return
        if self.Portfolio.Invested or self.Transactions.GetOpenOrders():
            return

        if self.daily_starting_equity is not None:
            current_daily_pnl = self.Portfolio.TotalPortfolioValue - self.daily_starting_equity
            daily_loss_pct    = current_daily_pnl / self.daily_starting_equity
            if daily_loss_pct < -self.max_daily_loss_percent:
                if not self.daily_loss_limit_hit:
                    self.daily_loss_limit_hit = True
                    self.Debug(f"DAILY LOSS LIMIT HIT: {daily_loss_pct*100:.2f}%")
                return

        if self.ticker_trade_counts.get(symbol, 0) >= self.max_trades_per_ticker:
            return

        if symbol not in self.GetOrComputeTopGainers(self.CurrentSlice):
            return

        if self.block_reentry_after_loss:
            if self.daily_traded_symbols.get(symbol) == 'loss':
                return

        if self.CheckEntryConditions(symbol, bar):
            self.EnterShortPosition(symbol, bar)

    def CheckEntryConditions(self, symbol, bar):
        sym_data      = self.symbol_data[symbol]
        current_price = bar.Close

        if not sym_data.rsi.IsReady:
            return False
        if sym_data.day_open_price is None or sym_data.day_open_price <= 0:
            return False
        if sym_data.last_rsi_cross_time is None:
            return False

        minutes_since_cross = (
            (self.Time - sym_data.last_rsi_cross_time).total_seconds() / 60
        )
        if minutes_since_cross > self.consolidation_minutes * 2:
            return False

        if self.require_above_vwap:
            if not sym_data.vwap.IsReady or sym_data.vwap.Current.Value <= 0:
                return False
            if current_price <= sym_data.vwap.Current.Value:
                return False

        if self.require_higher_than_last_entry:
            if symbol in self.last_entry_prices:
                if current_price <= self.last_entry_prices[symbol]:
                    return False

        return True

    def EnterShortPosition(self, symbol, bar):
        price = bar.Close
        if price <= 0:
            return

        sym_data = self.symbol_data[symbol]
        rank     = self.gainer_rankings.get(symbol, 'N/A')

        gain_from_open = 0.0
        if sym_data.day_open_price and sym_data.day_open_price > 0:
            gain_from_open = (
                (price - sym_data.day_open_price) / sym_data.day_open_price * 100
            )

        try:
            float_shares   = self.Securities[symbol].Fundamentals.CompanyProfile.SharesOutstanding
            float_millions = float_shares / 1_000_000
        except Exception:
            float_millions = 0

        rsi_value          = sym_data.rsi.Current.Value if sym_data.rsi.IsReady else 0
        vwap_value         = sym_data.vwap.Current.Value if sym_data.vwap.IsReady else 0
        distance_from_vwap = (
            (price - vwap_value) / vwap_value * 100 if vwap_value > 0 else 0
        )

        # ── DYNAMIC POSITION SIZING ──────────────────────────
        # ComputeDynamicSizePct() combines:
        #   1. Rolling Kelly (200-trade window)
        #   2. Kalman alpha signal (smoothed regime estimate)
        #   3. Drawdown gate
        # and clamps the result to [min_position_pct, max_position_pct].
        sizing_pct = self.ComputeDynamicSizePct()

        if sizing_pct == 0.0:
            # Drawdown gate has stopped all trading.
            self.Debug(
                f"ENTRY BLOCKED by drawdown gate: {symbol} | "
                f"{self.GetSizingDebugStr()}"
            )
            return

        shares = int(self.Portfolio.TotalPortfolioValue * sizing_pct // price)

        if shares <= 0:
            self.Debug(
                f"ENTRY SKIPPED — computed 0 shares: {symbol} | "
                f"sizing_pct={sizing_pct*100:.2f}% equity=${self.Portfolio.TotalPortfolioValue:.0f} "
                f"price=${price:.4f}"
            )
            return

        # Charge locate fee (once per symbol per day)
        if not self.Portfolio[symbol].Invested:
            self.locate_fee_model.algorithm = self
            locate_fee = self.locate_fee_model.CalculateLocateFee(symbol, price, shares)
            if locate_fee > 0:
                self.Portfolio.CashBook["USD"].AddAmount(-locate_fee)
                self.total_locate_fees_paid += locate_fee
                self.daily_fees_locate      += locate_fee

        limit_price = round(price * 0.99, 2)
        self.LimitOrder(symbol, -shares, limit_price)

        self.position_entry_times[symbol]    = self.Time
        self.position_entry_prices[symbol]   = price
        self.last_entry_prices[symbol]       = price
        # Stash for RecordTradeResult in OnOrderEvent
        self._pending_entry_prices[symbol]   = price

        ticker = str(symbol).split(" ")[0]
        self._webhook_cycle[ticker]       = self._webhook_cycle.get(ticker, 0) + 1
        self._webhook_short_fired[ticker] = True
        self._webhook_cover_fired[ticker] = False
        self._fire_webhook("SHORT", symbol, shares, price)

        log_message = (
            f"RSI SHORT {symbol}: {shares} sh @ ${limit_price} | "
            f"Rank #{rank} | Gain: {gain_from_open:.1f}% | Float: {float_millions:.1f}M | "
            f"RSI: {rsi_value:.1f} | VWAP: ${vwap_value:.2f} ({distance_from_vwap:+.1f}%) | "
            f"SIZE: {sizing_pct*100:.2f}% [{self.GetSizingDebugStr()}]"
        )
        self.daily_trades_log.append(log_message)
        self.Debug(log_message)

    # ============================================================
    # EXIT LOGIC
    # ============================================================

    def CheckExitConditions(self, symbol, bar):
        if not self.Portfolio[symbol].Invested:
            return
        if symbol in self.closing_in_progress:
            return
        if self.Transactions.GetOpenOrders(symbol):
            return

        current_price = bar.Close
        entry_price   = self.position_entry_prices.get(symbol)
        if entry_price is None or entry_price <= 0:
            return

        pnl_pct = (entry_price - current_price) / entry_price

        if pnl_pct >= self.profit_target_percent:
            self.ClosePosition(symbol, f"PROFIT TARGET ({pnl_pct*100:.1f}%)", current_price)
            return

        if pnl_pct <= -self.hard_stop_percent:
            self.ClosePosition(symbol, f"HARD STOP ({pnl_pct*100:.1f}%)", current_price)
            return

        sym_data = self.symbol_data.get(symbol)
        if sym_data and sym_data.rsi.IsReady:
            rsi_value = sym_data.rsi.Current.Value
            if rsi_value < self.rsi_oversold_level:
                self.ClosePosition(
                    symbol,
                    f"RSI OVERSOLD ({rsi_value:.1f} < {self.rsi_oversold_level})",
                    current_price
                )
                return

    def ClosePosition(self, symbol, reason, current_price):
        if not self.Portfolio[symbol].Invested:
            return
        if symbol in self.closing_in_progress:
            return

        quantity = self.Portfolio[symbol].Quantity
        if quantity == 0:
            return

        self.Transactions.CancelOpenOrders(symbol)

        entry_price = self.position_entry_prices.get(symbol, 0)
        limit_price = round(current_price * 1.03, 2)

        self.closing_in_progress.add(symbol)
        self.LimitOrder(symbol, abs(quantity), limit_price)

        ticker = str(symbol).split(" ")[0]
        if self._webhook_short_fired.get(ticker) and not self._webhook_cover_fired.get(ticker):
            self._webhook_cover_fired[ticker] = True
            self._fire_webhook("COVER", symbol, abs(quantity), current_price)

        pnl_value   = (entry_price - current_price) * abs(quantity) if entry_price > 0 else 0
        pnl_percent = ((entry_price - current_price) / entry_price) * 100 if entry_price > 0 else 0
        trade_result = 'win' if pnl_value >= 0 else 'loss'

        self.daily_traded_symbols[symbol] = trade_result
        self.position_entry_times.pop(symbol, None)
        self.position_entry_prices.pop(symbol, None)

        self.daily_trades_log.append(
            f"EXIT {symbol} — {reason} | "
            f"P&L: ${pnl_value:.0f} ({pnl_percent:.1f}%) [{trade_result.upper()}]"
        )
        self.Debug(
            f"EXIT {symbol} — {reason} | P&L: ${pnl_value:.0f} ({pnl_percent:.1f}%)"
        )

    def NuclearLiquidation(self):
        if not self.Portfolio.Invested:
            return
        for symbol in list(self.Portfolio.Keys):
            if self.Portfolio[symbol].Invested:
                self.Transactions.CancelOpenOrders(symbol)
                qty   = self.Portfolio[symbol].Quantity
                price = self.GetValidPrice(symbol)
                self.MarketOrder(symbol, abs(qty))

                ticker = str(symbol).split(" ")[0]
                if self._webhook_short_fired.get(ticker) and not self._webhook_cover_fired.get(ticker):
                    self._webhook_cover_fired[ticker] = True
                    self._fire_webhook("COVER", symbol, abs(qty), price)

                self.closing_in_progress.discard(symbol)
                self.position_entry_times.pop(symbol, None)
                self.position_entry_prices.pop(symbol, None)
                self.daily_trades_log.append(f"EOD LIQUIDATION {symbol}")

    # ============================================================
    # ORDER EVENT HANDLER
    # ============================================================

    def OnOrderEvent(self, orderEvent):
        if orderEvent.Status not in (
            OrderStatus.Filled,
            OrderStatus.Canceled,
            OrderStatus.Invalid
        ):
            return

        order  = self.Transactions.GetOrderById(orderEvent.OrderId)
        symbol = orderEvent.Symbol

        if orderEvent.Status in (OrderStatus.Canceled, OrderStatus.Invalid):
            if order.Quantity > 0 and symbol in self.closing_in_progress:
                self.Debug(
                    f"CLOSE ORDER FAILED ({orderEvent.Status}) for {symbol} "
                    f"— will retry next bar"
                )
                self.closing_in_progress.discard(symbol)
                ticker = str(symbol).split(" ")[0]
                self._webhook_cover_fired[ticker] = False
                if symbol not in self.position_entry_prices and self.Portfolio[symbol].Invested:
                    entry = self.last_entry_prices.get(symbol, self.Securities[symbol].Price)
                    self.position_entry_prices[symbol] = entry
                    self.position_entry_times[symbol]  = self.Time
            return

        # Filled
        if order.Quantity > 0:
            # Guard against accidental long
            if symbol in self.Portfolio and self.Portfolio[symbol].Quantity > 0:
                self.Debug(
                    f"ACCIDENTAL LONG detected for {symbol} "
                    f"qty={self.Portfolio[symbol].Quantity} — liquidating immediately"
                )
                self.Transactions.CancelOpenOrders(symbol)
                accidental_qty = self.Portfolio[symbol].Quantity
                exit_price     = round(self.Securities[symbol].Price * 0.97, 2)
                self.LimitOrder(symbol, -accidental_qty, exit_price)
                self.closing_in_progress.discard(symbol)
                return

            # ── Normal buy-to-cover fill ─────────────────────
            self.closing_in_progress.discard(symbol)

            ticker = str(symbol).split(" ")[0]
            self._reset_webhook_flags(ticker)

            self.daily_realized_pnl += self.Portfolio[symbol].LastTradeProfit
            self.ticker_trade_counts[symbol] = self.ticker_trade_counts.get(symbol, 0) + 1

            # Record trade into Kelly history buffer.
            # fill_price comes from the order's AverageFillPrice.
            fill_price = orderEvent.FillPrice
            self.RecordTradeResult(symbol, fill_price, orderEvent.FillQuantity)

    # ============================================================
    # MARKET STATUS LOG
    # ============================================================

    def LogMarketStatus(self, force=False):
        in_entry = self.IsInEntryHours()
        if not force and not in_entry and not self.Portfolio.Invested:
            return

        gainers    = []
        near_miss  = []
        all_gainers = []
        no_open    = 0

        for symbol, sd in self.symbol_data.items():
            if sd.day_open_price is None or sd.day_open_price <= 0:
                no_open += 1
                continue
            try:
                price = self.Securities[symbol].Price
                if price <= 0:
                    continue
                gain_pct  = (price - sd.day_open_price) / sd.day_open_price * 100
                rsi_ready = sd.rsi.IsReady
                rsi_val   = sd.rsi.Current.Value if rsi_ready else None
                entry = {
                    'sym': str(symbol).split(" ")[0],
                    'price': price,
                    'gain': gain_pct,
                    'rsi': rsi_val,
                    'ready': rsi_ready,
                }
                all_gainers.append(entry)
                if gain_pct >= self.min_gain_percent * 100:
                    gainers.append(entry)
                elif gain_pct >= self.min_gain_percent * 100 * 0.67:
                    near_miss.append(entry)
            except Exception:
                continue

        gainers.sort(key=lambda x: x['gain'], reverse=True)
        near_miss.sort(key=lambda x: x['gain'], reverse=True)
        all_gainers.sort(key=lambda x: x['gain'], reverse=True)

        status = (
            "ACTIVE" if self.Portfolio.Invested
            else ("WATCHING" if in_entry else "WAITING")
        )
        self.Debug(f"{'='*60}")
        self.Debug(
            f"STATUS [{self.Time.strftime('%H:%M')}] | {status} | "
            f"Entry: {'OPEN' if in_entry else 'CLOSED'} | "
            f"Equity: ${self.Portfolio.TotalPortfolioValue:.2f} | "
            f"Universe: {len(self.symbol_data)}"
        )
        self.Debug(f"  SIZING | {self.GetSizingDebugStr()}")

        if self.Portfolio.Invested:
            for sym in self.Portfolio.Keys:
                if self.Portfolio[sym].Invested:
                    p       = self.Portfolio[sym]
                    pnl     = p.UnrealizedProfitPercent * 100
                    sd      = self.symbol_data.get(sym)
                    rsi_str = (
                        f"RSI={sd.rsi.Current.Value:.1f}"
                        if sd and sd.rsi.IsReady else "RSI=n/a"
                    )
                    self.Debug(
                        f"  POSITION: {str(sym).split()[0]} | "
                        f"{p.Quantity}sh @ ${p.AveragePrice:.2f} | "
                        f"Unreal: {pnl:+.1f}% | {rsi_str}"
                    )

        if gainers:
            self.Debug(
                f"  TOP GAINERS ({len(gainers)} at >={self.min_gain_percent*100:.0f}% — "
                f"top {self.max_gainer_rank} eligible):"
            )
            for i, g in enumerate(gainers[:10]):
                rsi_str  = f"RSI={g['rsi']:.1f}" if g['rsi'] is not None else "RSI=warming"
                eligible = "✓" if i < self.max_gainer_rank else " "
                self.Debug(
                    f"  {eligible} #{i+1} {g['sym']:8s} +{g['gain']:.1f}% "
                    f"@ ${g['price']:.2f} | {rsi_str}"
                )
        else:
            self.Debug(
                f"  No stocks >={self.min_gain_percent*100:.0f}% — best 3:"
            )
            for i, g in enumerate(all_gainers[:3]):
                rsi_str = f"RSI={g['rsi']:.1f}" if g['rsi'] is not None else "RSI=warming"
                self.Debug(
                    f"    #{i+1} {g['sym']:8s} +{g['gain']:.1f}% "
                    f"@ ${g['price']:.2f} | {rsi_str}"
                )

        if near_miss:
            self.Debug(f"  APPROACHING ({len(near_miss)} at 67–100% of threshold):")
            for g in near_miss[:3]:
                rsi_str = f"RSI={g['rsi']:.1f}" if g['rsi'] is not None else "RSI=warming"
                self.Debug(
                    f"    {g['sym']:8s} +{g['gain']:.1f}% "
                    f"@ ${g['price']:.2f} | {rsi_str}"
                )

        if no_open > 0:
            self.Debug(f"  {no_open}/{len(self.symbol_data)} symbols missing open price")
        else:
            self.Debug(f"  All {len(self.symbol_data)} symbols have open price ✓")
        self.Debug(f"{'='*60}")

    # ============================================================
    # HELPER METHODS
    # ============================================================

    def IsInEntryHours(self, time_to_check=None):
        current_time = (time_to_check or self.Time).time()
        return self.entry_start_time <= current_time < self.entry_end_time

    def GetOrComputeTopGainers(self, data):
        if self.last_top_time == self.Time:
            self.gainer_rankings = self.cached_gainer_rankings
            return self.cached_top_gainers

        gainers = []
        for symbol in data.keys():
            sym_data = self.symbol_data.get(symbol)
            if sym_data is None:
                continue
            try:
                current_price = data[symbol].Close
            except Exception:
                continue
            if current_price < 2.0:
                continue
            if sym_data.day_open_price is None or sym_data.day_open_price <= 0:
                continue
            try:
                gain_percent = (
                    (current_price - sym_data.day_open_price) / sym_data.day_open_price
                )
            except Exception:
                continue
            if gain_percent >= self.min_gain_percent:
                gainers.append({'symbol': symbol, 'gain_percent': gain_percent})

        gainers.sort(key=lambda x: x['gain_percent'], reverse=True)
        self.cached_gainer_rankings = {
            g['symbol']: i + 1 for i, g in enumerate(gainers)
        }
        self.gainer_rankings   = self.cached_gainer_rankings
        self.cached_top_gainers = [
            g['symbol']
            for g in gainers[self.min_gainer_rank - 1 : self.max_gainer_rank]
        ]
        self.last_top_time = self.Time
        return self.cached_top_gainers

    def GetValidPrice(self, symbol):
        price = self.Securities[symbol].Price
        if price > 0:
            return price
        avg = self.Portfolio[symbol].AveragePrice
        return avg if avg > 0 else 1.00

    def CancelOrdersForSymbol(self, symbol):
        for order in self.Transactions.GetOpenOrders(symbol):
            self.Transactions.CancelOrder(order.Id)

    # ============================================================
    # DAILY RESET
    # ============================================================

    def DailyReset(self):
        daily_commission = self.Portfolio.TotalFees - self.daily_starting_total_fees
        equity           = self.Portfolio.TotalPortfolioValue

        # ── Kalman update (once per day, end-of-day) ─────────
        # Only update after warmup — warmup has no trades so
        # rolling Kelly returns global_kelly (obs=1.0) which
        # would just confirm the prior with no new information.
        # More importantly, IsWarmingUp guards against the
        # scheduled DailyReset firing during the warmup period.
        #
        # Observation = rolling Kelly ratio (rk / global_kelly).
        # This is dimensionless and centred on 1.0, matching
        # the Kalman state (kalman_x, also a multiplier).
        # DO NOT use daily_return_frac here — daily returns
        # (~0.5-2%) are on a completely different scale to the
        # Kelly ratio (~1.0), which causes the filter to
        # immediately collapse the state to near zero.
        if not self.IsWarmingUp:
            rk_obs = self.ComputeRollingKelly() / max(self.global_kelly, 1e-9)
            self.KalmanUpdate(rk_obs)

        # ── Update peak equity for DD gate ───────────────────
        if self.peak_equity is None or equity > self.peak_equity:
            self.peak_equity = equity

        # ── Log daily summary ─────────────────────────────────
        self.Debug(f"\n{'='*55}")
        self.Debug(
            f"DAILY SUMMARY | Equity: ${equity:.2f} | "
            f"Realized P&L: ${self.daily_realized_pnl:.2f} | "
            f"Locate Fees: ${self.daily_fees_locate:.2f} | "
            f"Commission: ${daily_commission:.2f}"
        )
        self.Debug(f"  KALMAN STATE | {self.GetSizingDebugStr()}")
        for entry in (self.daily_trades_log or ["  No trades today."]):
            self.Debug(f"  {entry}")
        self.Debug(f"{'='*55}\n")

        # ── Reset daily state ────────────────────────────────
        self.gainer_rankings           = {}
        self.daily_starting_equity     = equity
        self.daily_starting_total_fees = self.Portfolio.TotalFees
        self.daily_loss_limit_hit      = False
        self.daily_traded_symbols      = {}
        self.daily_realized_pnl        = 0
        self.ticker_trade_counts       = {}
        self.daily_trades_log          = []
        self.daily_fees_locate         = 0.0
        self.closing_in_progress       = set()

        self._webhook_short_fired.clear()
        self._webhook_cover_fired.clear()

        symbols_to_keep = [s for s in self.Portfolio.Keys if self.Portfolio[s].Invested]
        for symbol in list(self.position_entry_times.keys()):
            if symbol not in symbols_to_keep:
                del self.position_entry_times[symbol]
        for symbol in list(self.position_entry_prices.keys()):
            if symbol not in symbols_to_keep:
                del self.position_entry_prices[symbol]

        for order in self.Transactions.GetOpenOrders():
            if order.Quantity < 0:
                self.Transactions.CancelOrder(order.Id)

        if symbols_to_keep:
            self.Debug(f"WARNING: {len(symbols_to_keep)} positions still held overnight")

        self.locate_fee_model.ResetDaily(self.Time.date())
        for sd in self.symbol_data.values():
            sd.reset_daily_data()

    def OnEndOfAlgorithm(self):
        if self.daily_trades_log:
            self.Debug(f"\nFINAL DAY TRADES:")
            for entry in self.daily_trades_log:
                self.Debug(f"  {entry}")
        self.Debug(f"\nFINAL SUMMARY:")
        self.Debug(f"  Total Locate Fees:  ${self.total_locate_fees_paid:.2f}")
        self.Debug(f"  Total Commission:   ${self.Portfolio.TotalFees:.2f}")
        self.Debug(f"  Final Equity:       ${self.Portfolio.TotalPortfolioValue:.2f}")
        self.Debug(f"  Peak Equity:        ${self.peak_equity:.2f}")
        self.Debug(f"  Final Kalman State: {self.GetSizingDebugStr()}")
        self.Debug(f"  Trade History Size: {len(self.trade_history)} entries")


# ========================================
# SYMBOL DATA  (RSI + VWAP TRACKING)
# ========================================
class SymbolData:
    def __init__(self, algorithm, symbol):
        self.algorithm = algorithm
        self.symbol    = symbol

        self.rsi  = RelativeStrengthIndex(
            algorithm.rsi_period,
            MovingAverageType.Wilders
        )
        self.vwap = IntradayVwap(f"VWAP_{symbol}")

        self.day_open_price      = None
        self.last_rsi_value      = None
        self.last_rsi_cross_time = None

    def reset_daily_data(self):
        self.day_open_price      = None
        self.last_rsi_value      = None
        self.last_rsi_cross_time = None
