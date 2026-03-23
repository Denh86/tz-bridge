import clr
clr.AddReference("NodaTime")
from NodaTime import DateTimeZoneProviders
from datetime import datetime, time, timedelta
# End CLR Imports

# region Imports
from AlgorithmImports import *
import threading
import urllib.request
import json as _json
# endregion

class EODGainerShort(QCAlgorithm):
    def Initialize(self):
        self.SetStartDate(2026, 1, 1)
        self.SetEndDate(2026, 3, 13)
        self.SetCash(6055.47)

        self.enable_trading_logs = True

        self.WEBHOOK_URL = self.GetParameter("webhook_url") or ""
        if self.WEBHOOK_URL:
            self.Log(f"WEBHOOK ACTIVE: {self.WEBHOOK_URL}")
        else:
            self.Log("WEBHOOK: not set — dry-run mode (no TZ orders)")

        self.CANDLE_MINUTES = 5

        self.monthly_peak     = self.Portfolio.TotalPortfolioValue
        self.last_month_check = self.Time.month

        self.drawdown_thresholds = {
            12: 1,
            20: 1,
            30: 1,
            40: 1,
        }

        self.take_profit_pct    = 0.30
        self.stop_loss_pct      = 0.30
        self.limit_buffer_pct   = 0.010   # 1.0% below market for short entry
        self.exit_limit_buffer  = 0.010   # 1.0% above market for TP/SL cover
        self.force_close_buffer = 0.015   # 1.5% above market for force close (AH, hard deadline)

        self.min_day_gain            = 0.25
        self.max_day_gain            = 5.0
        self.min_day_dollar_volume   = 500_000

        self.min_stock_price         = 0.75
        self.max_stock_price         = 20.0
        self.min_shares_outstanding  = 100_000
        self.max_shares_outstanding  = 100_000_000
        self.min_dollar_volume       = 0

        self.ah_open  = time(16, 0)
        self.ah_close = time(19, 30)

        self.top_gainer_symbol    = None
        self.entry_tickets        = {}
        self.exit_tickets         = {}
        self.entry_prices         = {}
        self.entry_times          = {}
        self.liquid_universe_symbols = []
        self.exit_triggered       = False
        self.entry_executed       = False
        self.force_close_executed = False

        self._webhook_short_fired  = False
        self._webhook_cover_fired  = False

        self._consolidators = {}

        self.SetWarmUp(timedelta(days=1))
        self.UniverseSettings.Resolution = Resolution.Minute
        self.UniverseSettings.ExtendedMarketHours = True

        # FIX 2: use NodaTime instead of pytz
        self.nyse_tz = DateTimeZoneProviders.Tzdb.GetZoneOrNull("America/New_York")
        self.SetTimeZone(self.nyse_tz)

        self.AddUniverse(self.CoarseSelector, self.FineSelector)

        self.Schedule.On(self.DateRules.EveryDay(), self.TimeRules.At(15, 55), self.ScanDayGainers)
        self.Schedule.On(self.DateRules.EveryDay(), self.TimeRules.At(16, 0),  self.PlaceEntryOrder)
        self.Schedule.On(self.DateRules.EveryDay(), self.TimeRules.At(19, 30), self.ForceClosePositions)
        self.Schedule.On(self.DateRules.EveryDay(), self.TimeRules.At(19, 35), self.DailyReset)

    # ============================================================================
    # WARMUP FINISHED
    # ============================================================================
    def OnWarmupFinished(self):
        # FIX 6: log universe size on warmup finish so mid-session deploys
        # show immediately whether the universe is populated
        self.Log(f"OnWarmupFinished at {self.Time} — "
                 f"universe size: {len(self.liquid_universe_symbols)}")
        if len(self.liquid_universe_symbols) == 0:
            self.Log("WARNING: Universe empty after warmup — "
                     "ScanDayGainers will have nothing to scan")

    # ============================================================================
    # WEBHOOK HELPER
    # ============================================================================
    def _fire_webhook(self, action, symbol, quantity, price):
        """
        Fire-and-forget webhook to TZ bridge server.
        Only sends in live mode and outside warmup — never during backtests
        or warmup replay, which would place real orders on historical signals.
        Runs in a background thread so it never blocks the algorithm.
        """
        # FIX 1: always accept raw symbol object or string — strip SID here
        ticker = str(symbol).split()[0] if symbol else ""

        # Guard: never fire in backtest or during warmup replay
        if not self.LiveMode or self.IsWarmingUp:
            self.Log(f"WEBHOOK (backtest/warmup, not sent) → "
                     f"action={action} symbol={ticker} qty={quantity} price={price:.4f}")
            return

        if not self.WEBHOOK_URL:
            self.Log(f"WEBHOOK DRY-RUN: action={action} symbol={ticker} "
                     f"qty={quantity} price={price:.4f}")
            return

        payload = _json.dumps({
            "action":   action,
            "symbol":   ticker,
            "quantity": int(quantity),
            "price":    round(float(price), 4),
            "cycle":    1,   # EOD strategy: always 1 trade per day
        }).encode("utf-8")

        def _send():
            try:
                req = urllib.request.Request(
                    self.WEBHOOK_URL,
                    data=payload,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req, timeout=10) as resp:
                    body = resp.read().decode("utf-8", errors="replace")[:200]
                self.Log(f"WEBHOOK OK | {action} {ticker} | {resp.status} | {body}")
            except Exception as e:
                self.Log(f"WEBHOOK ERROR | {action} {ticker} | {e}")

        threading.Thread(target=_send, daemon=True).start()

    # ============================================================================
    # CONSOLIDATOR MANAGEMENT
    # ============================================================================
    def _AttachConsolidator(self, symbol):
        if self.CANDLE_MINUTES == 1 or symbol in self._consolidators:
            return
        consolidator = TradeBarConsolidator(timedelta(minutes=self.CANDLE_MINUTES))
        consolidator.DataConsolidated += self._OnConsolidatedBar
        self.SubscriptionManager.AddConsolidator(symbol, consolidator)
        self._consolidators[symbol] = consolidator
        self.Log(f"CONSOLIDATOR: {symbol} → {self.CANDLE_MINUTES}-min bars attached")

    def _DetachConsolidator(self, symbol):
        if symbol in self._consolidators:
            self.SubscriptionManager.RemoveConsolidator(symbol, self._consolidators[symbol])
            del self._consolidators[symbol]
            self.Log(f"CONSOLIDATOR: {symbol} detached")

    def _OnConsolidatedBar(self, sender, bar):
        self._EvaluateExits(bar.Symbol, bar.Close)

    # ============================================================================
    # DRAWDOWN PROTECTION
    # ============================================================================
    def GetMonthlyDrawdown(self):
        current = self.Portfolio.TotalPortfolioValue
        if self.Time.month != self.last_month_check:
            self.monthly_peak     = current
            self.last_month_check = self.Time.month
            self.Log(f"NEW MONTH: Peak reset to ${current:,.2f}")
        if current > self.monthly_peak:
            self.monthly_peak = current
        return (self.monthly_peak - current) / self.monthly_peak if self.monthly_peak > 0 else 0

    def GetPositionSizeMultiplier(self):
        dd         = self.GetMonthlyDrawdown()
        multiplier = 1.0
        for threshold in sorted(self.drawdown_thresholds.keys(), reverse=True):
            if dd * 100 >= threshold:
                multiplier = self.drawdown_thresholds[threshold]
                break
        return multiplier, dd

    # ============================================================================
    # UNIVERSE SELECTION
    # ============================================================================
    def CoarseSelector(self, coarse):
        filtered = [
            x.Symbol for x in coarse
            if x.HasFundamentalData
            and x.DollarVolume >= self.min_dollar_volume
            and self.min_stock_price <= x.Price <= self.max_stock_price
        ]
        if len(filtered) > 0:
            self.Log(f"COARSE: {len(filtered)} stocks")
        return filtered

    def FineSelector(self, fine):
        filtered = []
        for f in fine:
            sym = f.Symbol.Value
            if len(sym) == 5 and sym.endswith('X'):
                continue
            if not (self.min_shares_outstanding
                    <= f.CompanyProfile.SharesOutstanding
                    <= self.max_shares_outstanding):
                continue
            if not (self.min_stock_price <= f.Price <= self.max_stock_price):
                continue
            filtered.append(f.Symbol)
        self.Log(f"FINE: {len(filtered)} stocks")
        return filtered

    # ============================================================================
    # UNIVERSE TRACKING
    # ============================================================================
    def OnSecuritiesChanged(self, changes):
        for security in changes.AddedSecurities:
            sym = security.Symbol
            if sym not in self.liquid_universe_symbols:
                self.liquid_universe_symbols.append(sym)
        for security in changes.RemovedSecurities:
            sym = security.Symbol
            if sym in self.liquid_universe_symbols and sym != self.top_gainer_symbol:
                self.liquid_universe_symbols.remove(sym)
        if len(changes.AddedSecurities) > 0:
            self.Log(f"UNIVERSE: {len(self.liquid_universe_symbols)} symbols tracked "
                     f"(+{len(changes.AddedSecurities)} added, "
                     f"-{len(changes.RemovedSecurities)} removed)")

    # ============================================================================
    # DAY-GAINER SCAN  (3:55 PM)
    # ============================================================================
    def ScanDayGainers(self):
        if self.IsWarmingUp or self.IsWeekend():
            return

        self.top_gainer_symbol = None

        # FIX 2: use self.Time directly — already in ET via SetTimeZone
        today      = self.Time.date()
        scan_end   = datetime(today.year, today.month, today.day, 15, 55)
        scan_start = datetime(today.year, today.month, today.day,  9, 30)

        self.Log(f"DAY SCAN: 3:55 PM snapshot for {today} | "
                 f"Universe: {len(self.liquid_universe_symbols)} stocks")

        gainer_scores = []
        stats = {k: 0 for k in ('no_daily', 'no_rth', 'low_vol',
                                 'gain_high', 'gain_low', 'passed')}

        batch_size = 50
        for i in range(0, len(self.liquid_universe_symbols), batch_size):
            for symbol in self.liquid_universe_symbols[i : i + batch_size]:
                try:
                    daily = self.History(symbol, 1, Resolution.Daily)
                    if daily.empty:
                        stats['no_daily'] += 1
                        continue
                    prior_close = float(daily['close'].iloc[-1])

                    rth = self.History(
                        [symbol], scan_start, scan_end,
                        Resolution.Minute, extendedMarketHours=False
                    )
                    if rth.empty or symbol not in rth.index.get_level_values('symbol'):
                        stats['no_rth'] += 1
                        continue
                    sym_data = rth.loc[symbol]
                    if sym_data.empty:
                        stats['no_rth'] += 1
                        continue

                    current_price  = float(sym_data['close'].iloc[-1])
                    if not (self.min_stock_price <= current_price <= self.max_stock_price):
                        stats['gain_low'] += 1
                        continue
                    day_dollar_vol = (
                        float((sym_data['volume'] * sym_data['close']).sum())
                        if 'volume' in sym_data.columns else 0
                    )

                    if day_dollar_vol < self.min_day_dollar_volume:
                        stats['low_vol'] += 1
                        continue

                    day_gain = (current_price - prior_close) / prior_close
                    if day_gain > self.max_day_gain:
                        stats['gain_high'] += 1
                        continue
                    if day_gain < self.min_day_gain:
                        stats['gain_low'] += 1
                        continue

                    stats['passed'] += 1
                    gainer_scores.append({
                        'symbol'        : symbol,
                        'day_gain'      : day_gain,
                        'prior_close'   : prior_close,
                        'current_price' : current_price,
                        'day_dollar_vol': day_dollar_vol,
                    })

                except Exception as e:
                    self.Log(f"  Error {symbol}: {e}")

        self.Log(
            f"SCAN: passed={stats['passed']} | no_daily={stats['no_daily']} | "
            f"no_rth={stats['no_rth']} | low_vol={stats['low_vol']} | "
            f"gain_hi={stats['gain_high']} | gain_lo={stats['gain_low']}"
        )

        if not gainer_scores:
            self.Log("NO GAINERS FOUND – no trade today")
            return

        gainer_scores.sort(key=lambda x: x['day_gain'], reverse=True)

        self.Log("TOP 5 DAY GAINERS (short targets):")
        for idx, g in enumerate(gainer_scores[:5], 1):
            self.Log(
                f"  #{idx}: {g['symbol']} | "
                f"Gain: {g['day_gain']*100:.1f}% | "
                f"Prev Close: ${g['prior_close']:.2f} | "
                f"3:55 PM: ${g['current_price']:.2f} | "
                f"Vol: ${g['day_dollar_vol']:,.0f}"
            )

        top = gainer_scores[0]
        self.top_gainer_symbol = top['symbol']
        self.Log(
            f"\nSELECTED SHORT TARGET: {top['symbol']} | "
            f"Day Gain: {top['day_gain']*100:.1f}% | "
            f"Prev Close: ${top['prior_close']:.2f} | "
            f"3:55 PM Price: ${top['current_price']:.2f}"
        )

    # ============================================================================
    # ENTRY  –  4:00 PM short at the bell
    # ============================================================================
    def PlaceEntryOrder(self):
        if not self.top_gainer_symbol or self.entry_executed:
            return
        if self.IsWeekend():
            return

        symbol = self.top_gainer_symbol

        current_price = self.Securities[symbol].Price
        if current_price <= 0:
            self.Log(f"ENTRY REJECTED: No valid price for {symbol} at 4 PM")
            return

        if not (self.min_stock_price <= current_price <= self.max_stock_price):
            self.Log(
                f"ENTRY REJECTED: ${current_price:.2f} outside "
                f"[${self.min_stock_price}, ${self.max_stock_price}]"
            )
            return

        position_multiplier, monthly_dd = self.GetPositionSizeMultiplier()
        portfolio_value  = self.Portfolio.TotalPortfolioValue
        adjusted_capital = portfolio_value * position_multiplier
        quantity         = int(adjusted_capital / current_price)

        if quantity <= 0:
            self.Log(f"ENTRY REJECTED: quantity {quantity} invalid")
            return

        limit_price = round(current_price * (1 - self.limit_buffer_pct), 2)

        self.entry_executed = True

        self.Log(f"\nPLACING SHORT [4 PM Bell]")
        self.Log(f"  Symbol     : {symbol}")
        self.Log(f"  Qty        : -{quantity}")
        self.Log(f"  Limit      : ${limit_price:.2f}  (4PM: ${current_price:.2f})")
        self.Log(f"  TP target  : ${current_price*(1-self.take_profit_pct):.2f}  "
                 f"(-{self.take_profit_pct*100:.0f}%)")
        self.Log(f"  SL target  : ${current_price*(1+self.stop_loss_pct):.2f}  "
                 f"(+{self.stop_loss_pct*100:.0f}%)")
        self.Log(f"  Force close: 7:30 PM")
        self.Log(f"  Portfolio  : ${portfolio_value:,.2f}  "
                 f"(DD mult {position_multiplier*100:.0f}%, "
                 f"monthly DD {monthly_dd*100:.1f}%)")
        self.Log(f"  Resolution : {self.CANDLE_MINUTES}-min bars")

        ticket = self.LimitOrder(symbol, -quantity, limit_price)
        self.entry_tickets[symbol] = ticket
        self.Log(f"  OrderId    : {ticket.OrderId} | Status: {ticket.Status}")

        # FIX 1: pass symbol directly — _fire_webhook strips SID internally
        self._webhook_short_fired = True
        self._webhook_cover_fired = False
        self._fire_webhook("SHORT", symbol, quantity, limit_price)

        self._AttachConsolidator(symbol)

    # ============================================================================
    # OnData  –  raw 1-min path (only used when CANDLE_MINUTES == 1)
    # ============================================================================
    def OnData(self, data):
        if self.IsWarmingUp or self.CANDLE_MINUTES != 1:
            return
        if not self.top_gainer_symbol:
            return
        symbol = self.top_gainer_symbol
        if symbol not in data or not data[symbol]:
            return
        self._EvaluateExits(symbol, data[symbol].Close)

    # ============================================================================
    # SHARED EXIT EVALUATION
    # ============================================================================
    def _EvaluateExits(self, symbol, current_price):
        if (symbol not in self.entry_prices
                or not self.Portfolio[symbol].Invested
                or self.exit_triggered):
            return

        if current_price <= 0:
            return

        now = self.Time.time()
        if not (self.ah_open <= now <= self.ah_close):
            return

        entry_price = self.entry_prices[symbol]

        # ── Take Profit ──────────────────────────────────────────────────────────
        tp_trigger = entry_price * (1 - self.take_profit_pct)
        if current_price <= tp_trigger:
            cover_limit = round(current_price * (1 + self.exit_limit_buffer), 2)
            qty = abs(self.Portfolio[symbol].Quantity)
            if qty > 0:
                if symbol in self.exit_tickets:
                    self._CancelExitOrder(symbol)
                ticket = self.LimitOrder(symbol, qty, cover_limit)
                self.exit_tickets[symbol] = ticket
                self.exit_triggered       = True
                pnl_est = (entry_price - current_price) / entry_price * 100
                self.Log(
                    f"TP ORDER [{self.CANDLE_MINUTES}m]: {symbol} | "
                    f"Entry ${entry_price:.2f} | Current ${current_price:.2f} | "
                    f"Trigger ${tp_trigger:.2f} | Cover ${cover_limit:.2f} | "
                    f"Est P&L: +{pnl_est:.1f}%"
                )
                # FIX 1: pass symbol directly
                if self._webhook_short_fired and not self._webhook_cover_fired:
                    self._webhook_cover_fired = True
                    self._fire_webhook("COVER", symbol, qty, cover_limit)
            return

        # ── Stop Loss ────────────────────────────────────────────────────────────
        sl_trigger = entry_price * (1 + self.stop_loss_pct)
        if current_price >= sl_trigger:
            cover_limit = round(current_price * (1 + self.exit_limit_buffer), 2)
            qty = abs(self.Portfolio[symbol].Quantity)
            if qty > 0:
                if symbol in self.exit_tickets:
                    self._CancelExitOrder(symbol)
                ticket = self.LimitOrder(symbol, qty, cover_limit)
                self.exit_tickets[symbol] = ticket
                self.exit_triggered       = True
                pnl_est = (entry_price - current_price) / entry_price * 100
                self.Log(
                    f"SL ORDER [{self.CANDLE_MINUTES}m]: {symbol} | "
                    f"Entry ${entry_price:.2f} | Current ${current_price:.2f} | "
                    f"Trigger ${sl_trigger:.2f} | Cover ${cover_limit:.2f} | "
                    f"Est P&L: {pnl_est:.1f}%"
                )
                # FIX 1: pass symbol directly
                if self._webhook_short_fired and not self._webhook_cover_fired:
                    self._webhook_cover_fired = True
                    self._fire_webhook("COVER", symbol, qty, cover_limit)
            return

        log_every_n_minutes = max(1, self.CANDLE_MINUTES * 5)
        if self.Time.minute % log_every_n_minutes == 0:
            self.Log(
                f"HOLD [{self.CANDLE_MINUTES}m]: {symbol} | "
                f"${current_price:.2f} | Entry ${entry_price:.2f} | "
                f"TP ${tp_trigger:.2f} | SL ${sl_trigger:.2f}"
            )

    # ============================================================================
    # ORDER EVENTS
    # ============================================================================
    def OnOrderEvent(self, order_event):
        symbol     = order_event.Symbol
        status     = order_event.Status
        fill_price = order_event.FillPrice

        if status == OrderStatus.Filled:

            if (symbol in self.entry_tickets
                    and order_event.OrderId == self.entry_tickets[symbol].OrderId):
                self.entry_prices[symbol] = fill_price
                self.entry_times[symbol]  = self.Time
                qty        = abs(self.entry_tickets[symbol].Quantity)
                monthly_dd = self.GetMonthlyDrawdown()
                self.Log(
                    f"SHORT ENTRY FILLED: {symbol} | "
                    f"${fill_price:.2f} × {qty}sh | "
                    f"TP @ ${fill_price*(1-self.take_profit_pct):.2f} | "
                    f"SL @ ${fill_price*(1+self.stop_loss_pct):.2f} | "
                    f"Force close @ 7:30 PM | "
                    f"Monthly DD: {monthly_dd*100:.1f}%"
                )
                del self.entry_tickets[symbol]

            elif (symbol in self.exit_tickets
                    and order_event.OrderId == self.exit_tickets[symbol].OrderId):
                entry_price = self.entry_prices.get(symbol, 0)
                if entry_price > 0:
                    pnl    = (entry_price - fill_price) / entry_price * 100
                    result = "WIN" if pnl > 0 else "LOSS"
                    monthly_dd = self.GetMonthlyDrawdown()
                    self.Log(
                        f"EXIT FILLED: {symbol} | ${fill_price:.2f} | "
                        f"P&L: {pnl:+.2f}% | {result} | "
                        f"Monthly DD: {monthly_dd*100:.1f}%"
                    )
                del self.exit_tickets[symbol]
                self._Cleanup(symbol)

        elif status in [OrderStatus.Invalid, OrderStatus.Canceled]:
            self.Log(f"ORDER {status}: {symbol} | OrderId {order_event.OrderId}")
            if (symbol in self.entry_tickets
                    and order_event.OrderId == self.entry_tickets[symbol].OrderId):
                del self.entry_tickets[symbol]
                self.entry_executed = False
            elif (symbol in self.exit_tickets
                    and order_event.OrderId == self.exit_tickets[symbol].OrderId):
                del self.exit_tickets[symbol]
                self.exit_triggered = False
                self.Log(f"EXIT CANCELED: {symbol} – exit_triggered reset")

    # ============================================================================
    # FORCE CLOSE  (7:30 PM)
    # ============================================================================
    def ForceClosePositions(self):
        if self.force_close_executed or self.IsWeekend():
            return
        self.force_close_executed = True
        self.Log("7:30 PM FORCE CLOSE – buying back all shorts")

        for holding in self.Portfolio.Values:
            if not (holding.Invested and holding.IsShort):
                continue

            symbol        = holding.Symbol
            current_price = self.Securities[symbol].Price

            # FIX 5: only skip if a pending exit order exists, not just if flag is set
            if symbol in self.exit_tickets:
                self.Log(f"  SKIP: {symbol} already has exit order pending")
                continue
            if current_price <= 0:
                self.Log(f"  SKIP: {symbol} no valid price")
                continue

            qty         = abs(holding.Quantity)
            limit_price = round(current_price * (1 + self.force_close_buffer), 2)
            ticket      = self.LimitOrder(symbol, qty, limit_price)
            self.exit_tickets[symbol] = ticket
            self.exit_triggered       = True

            entry_price = self.entry_prices.get(symbol, 0)
            pnl_est = ((entry_price - current_price) / entry_price * 100) if entry_price > 0 else 0
            self.Log(
                f"  FORCE CLOSE: {symbol} | Buy {qty}sh | "
                f"Limit ${limit_price:.2f} | Est P&L: {pnl_est:+.1f}%"
            )

            # FIX 1: pass symbol directly
            if self._webhook_short_fired and not self._webhook_cover_fired:
                self._webhook_cover_fired = True
                self._fire_webhook("COVER", symbol, qty, limit_price)

        # FIX 4: unfilled entry orders at force close — just cancel, don't send CANCEL webhook
        for symbol in list(self.entry_tickets.keys()):
            self.Log(f"  CANCEL unfilled entry: {symbol}")
            self._CancelExitOrder(symbol)
            self._Cleanup(symbol)

    # ============================================================================
    # HELPERS
    # ============================================================================
    def _CancelExitOrder(self, symbol):
        if symbol in self.exit_tickets:
            t = self.exit_tickets[symbol]
            if t.Status in [OrderStatus.Submitted, OrderStatus.New, OrderStatus.PartiallyFilled]:
                self.Transactions.CancelOrder(t.OrderId)
                self.Log(f"CANCELLED exit order: {symbol} | OrderId {t.OrderId}")

    def _Cleanup(self, symbol):
        self._DetachConsolidator(symbol)
        for d in (self.entry_tickets, self.exit_tickets, self.entry_prices, self.entry_times):
            d.pop(symbol, None)
        self.exit_triggered = False

    def IsWeekend(self, date=None):
        return (date or self.Time.date()).weekday() >= 5

    # ============================================================================
    # DAILY RESET  (7:35 PM)
    # ============================================================================
    def DailyReset(self):
        monthly_dd = self.GetMonthlyDrawdown()
        multiplier = self.GetPositionSizeMultiplier()[0]
        equity     = self.Portfolio.TotalPortfolioValue
        self.Log(
            f"EOD: Equity ${equity:,.2f} | Peak ${self.monthly_peak:,.2f} | "
            f"Monthly DD {monthly_dd*100:.1f}% | Size {multiplier*100:.0f}%"
        )

        for sym in list(self._consolidators.keys()):
            self._DetachConsolidator(sym)

        self.force_close_executed  = False
        self.entry_executed        = False
        self.top_gainer_symbol     = None
        self._webhook_short_fired  = False
        self._webhook_cover_fired  = False
        self.entry_tickets.clear()
        self.exit_tickets.clear()
        self.entry_prices.clear()
        self.entry_times.clear()
        self.exit_triggered = False
        self.Log("Daily Reset Complete")

    # ============================================================================
    # LOG OVERRIDE
    # ============================================================================
    def Log(self, message):
        if self.enable_trading_logs:
            super().Log(message)
