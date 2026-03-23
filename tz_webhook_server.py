"""
TradeZero Webhook Execution Server — EOD Short Strategy 
=======================================================
Receives SHORT / COVER / CANCEL signals from QuantConnect.
Manages the full locate → short → cover lifecycle with state machine.

SIGNAL FORMAT (from QC):
  {"action": "SHORT",  "symbol": "LRMR", "quantity": 500, "price": 5.70}
  {"action": "COVER",  "symbol": "LRMR", "quantity": 500, "price": 5.50}
  {"action": "CANCEL", "symbol": "LRMR"}

SYMBOL STATES:
  FLAT      → ready, will accept SHORT
  LOCATING  → locate request in progress
  ACTIVE    → short position placed, waiting for COVER
  BLOCKED   → dead for the day, all signals ignored

HOW TO RUN:
  pip install flask requests python-dotenv
  .env:  TZ_API_KEY=...  TZ_API_SECRET=...
  python tz_webhook_server.py
"""

import os, json, uuid, logging, threading, time
from datetime import date
from zoneinfo import ZoneInfo
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

# ══════════════════════════════════════════════════════════════════════════════
# CONFIG — adjust these without touching any other code
# ══════════════════════════════════════════════════════════════════════════════
BASE_URL             = "https://webapi.tradezero.com"
ACCOUNT_ID           = "DHA41998"
MAX_LOCATE_COST_PCT  = 0.02    # 2%  — reject if locatePrice / entryPrice > this
MIN_LOCATE_QUANTITY  = 100     # TZ minimum locate size
MIN_SHORT_QUANTITY   = 1       # TZ minimum short order size (any size accepted)
LOCATE_POLL_INTERVAL = 2       # seconds between locate status polls
LOCATE_POLL_TIMEOUT  = 30      # seconds before giving up on locate
LOCATE_ACCEPT_SLEEP  = 3       # seconds to wait after locate accept before placing order
                               # TZ sends "locateAcceptSent:true" before fully registering
                               # the locate — placing the order too fast causes R43 rejection
# QC strategies send already-buffered limit prices in webhooks — server uses them directly
SHORT_FILL_TIMEOUT   = 5       # minutes to wait for short entry to fill before cancelling
COVER_FILL_TIMEOUT   = 3       # minutes to wait for cover to fill before retrying aggressively
COVER_RETRY_BUFFER   = 0.005   # 0.5% above live price for aggressive cover retry

# ══════════════════════════════════════════════════════════════════════════════
# CREDENTIALS
# ══════════════════════════════════════════════════════════════════════════════
API_KEY         = (os.getenv("TZ_API_KEY")      or "").strip()
API_SECRET      = (os.getenv("TZ_API_SECRET")   or "").strip()
TWELVEDATA_API_KEY = (os.getenv("TWELVEDATA_API_KEY") or "").strip()

# ══════════════════════════════════════════════════════════════════════════════
# LOGGING
# ══════════════════════════════════════════════════════════════════════════════
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
    handlers=[logging.StreamHandler()]
)
log = logging.getLogger("tz_server")
log.info("=== tz_webhook_server module loading ===")

# Suppress gunicorn access log spam from Render's health check pings.
# These hit /health every 5s from 10.225.x.x and flood the logs.
class _HealthCheckFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return "GET /health" not in msg

for _gunicorn_logger in ("gunicorn.access", "gunicorn.error", "werkzeug"):
    logging.getLogger(_gunicorn_logger).addFilter(_HealthCheckFilter())

# ══════════════════════════════════════════════════════════════════════════════
# STATE MACHINE  (reset at midnight)
# ══════════════════════════════════════════════════════════════════════════════
symbol_state = {}
state_lock   = threading.Lock()
reset_date   = date.today()


def get_state(symbol):
    with state_lock:
        return symbol_state.get(symbol, {}).copy()


def set_state(symbol, **kwargs):
    with state_lock:
        if symbol not in symbol_state:
            symbol_state[symbol] = {"state": "FLAT"}
        symbol_state[symbol].update(kwargs)
    log.info(f"[{symbol}] STATE → {symbol_state[symbol]['state']}"
             + (f" ({kwargs.get('reason', '')})" if kwargs.get("reason") else ""))


def block(symbol, reason):
    # Preserve the last stored cycle so a higher cycle from any strategy
    # can lift this block. If no cycle was stored yet it stays 0,
    # meaning even cycle=1 from any strategy will lift it.
    last_cycle = symbol_state.get(symbol, {}).get("cycle", 0)
    set_state(symbol, state="BLOCKED", reason=reason, cycle=last_cycle)
    log.warning(f"[{symbol}] BLOCKED: {reason}")


def midnight_reset_thread():
    global reset_date
    while True:
        time.sleep(30)
        today = date.today()
        if today != reset_date:
            with state_lock:
                symbol_state.clear()
            reset_date = today
            log.info("MIDNIGHT RESET: all symbol states cleared")


threading.Thread(target=midnight_reset_thread, daemon=True).start()


def self_watchdog_thread():
    """
    Pings own /health endpoint every 90 seconds.
    If 5 consecutive failures, forces gunicorn to recycle the worker
    by raising SIGTERM on the current process — gunicorn master will
    spawn a fresh worker automatically.

    Initial delay of 180s gives Render's port scanner time to confirm
    the port is open before the watchdog starts pinging — avoids false-
    positive self-kills during cold start / deploy.
    """
    import signal as _signal, urllib.request as _req, os as _os
    time.sleep(180)  # wait for Render port scan + full startup before first ping

    port = int(_os.environ.get("PORT", 10000))
    url  = f"http://127.0.0.1:{port}/health"
    fails = 0
    log.info("WATCHDOG: starting health monitoring")

    while True:
        time.sleep(90)
        try:
            with _req.urlopen(url, timeout=10) as r:
                if r.status == 200:
                    if fails > 0:
                        log.info(f"WATCHDOG: /health recovered (was {fails} failures)")
                    fails = 0
                else:
                    fails += 1
                    log.warning(f"WATCHDOG: /health returned {r.status} ({fails}/5)")
        except Exception as e:
            fails += 1
            log.warning(f"WATCHDOG: /health unreachable — {e} ({fails}/5)")

        if fails >= 5:
            log.error("WATCHDOG: 5 consecutive health failures — recycling worker")
            _os.kill(_os.getpid(), _signal.SIGTERM)
            return  # thread exits; gunicorn spawns new worker


threading.Thread(target=self_watchdog_thread, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# TZ API HELPERS — every call logs full request + response
# ══════════════════════════════════════════════════════════════════════════════
def tz_headers():
    return {
        "TZ-API-KEY-ID":     API_KEY,
        "TZ-API-SECRET-KEY": API_SECRET,
        "Content-Type":      "application/json",
        "Accept":            "application/json",
    }


def tz_get(path, label="GET"):
    url = f"{BASE_URL}{path}"
    log.info(f"  → {label} GET {url}")
    r = requests.get(url, headers=tz_headers(), timeout=15)
    log.info(f"  ← status: {r.status_code}  body: {r.text[:600]}")
    return r


def tz_post(path, payload, label="POST"):
    url = f"{BASE_URL}{path}"
    log.info(f"  → {label} POST {url}")
    log.info(f"    body: {json.dumps(payload)}")
    r = requests.post(url, headers=tz_headers(), json=payload, timeout=15)
    log.info(f"  ← status: {r.status_code}  body: {r.text[:600]}")
    return r


def tz_delete(path, label="DELETE"):
    url = f"{BASE_URL}{path}"
    log.info(f"  → {label} DELETE {url}")
    r = requests.delete(url, headers=tz_headers(), timeout=15)
    log.info(f"  ← status: {r.status_code}  body: {r.text[:200]}")
    return r


# ══════════════════════════════════════════════════════════════════════════════
# ACCOUNT / POSITION HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def get_account_details():
    r = tz_get(f"/v1/api/account/{ACCOUNT_ID}", "ACCOUNT")
    if r.status_code == 200:
        return r.json()
    log.error(f"  Failed to get account details: {r.status_code}")
    return None


def get_position(symbol):
    r = tz_get(f"/v1/api/accounts/{ACCOUNT_ID}/positions", "POSITIONS")
    if r.status_code != 200:
        log.error(f"  Failed to get positions: {r.status_code}")
        return None
    positions = r.json()
    if not isinstance(positions, list):
        positions = positions.get("positions", [])
    for p in positions:
        if p.get("symbol", "").upper() == symbol.upper():
            log.info(f"  Found position for {symbol}: {json.dumps(p)}")
            return p
    log.info(f"  No open position found for {symbol}")
    return None


def cancel_all_open_orders(symbol):
    r = tz_get(f"/v1/api/accounts/{ACCOUNT_ID}/orders", "ORDERS")
    if r.status_code != 200:
        log.error(f"  Failed to fetch orders: {r.status_code}")
        return 0
    orders = r.json()
    if not isinstance(orders, list):
        orders = orders.get("orders", [])
    open_statuses = {"PendingNew", "New", "PartiallyFilled", "Submitted"}
    open_orders = [
        o for o in orders
        if o.get("symbol", "").upper() == symbol.upper()
        and o.get("orderStatus", "") in open_statuses
    ]
    log.info(f"  Found {len(open_orders)} open order(s) for {symbol}")
    cancelled = 0
    for o in open_orders:
        oid = o.get("clientOrderId")
        if oid:
            rc = tz_delete(f"/v1/api/accounts/{ACCOUNT_ID}/orders/{oid}", "CANCEL_ORDER")
            if rc.status_code in (200, 204):
                cancelled += 1
            else:
                log.warning(f"  Failed to cancel order {oid}: {rc.status_code}")
    return cancelled


def place_order(side, symbol, quantity, limit_price, label="ORDER"):
    client_id = f"QC_{side[:1].upper()}_{uuid.uuid4().hex[:8].upper()}"
    # Field names/casing confirmed from working tz_test.py paper account test
    side_str = "Sell" if side.lower() == "sell" else "Buy"
    payload = {
        "clientOrderId": client_id,
        "symbol":        symbol,
        "orderQuantity": int(quantity),   # ← orderQuantity not quantity
        "side":          side_str,        # ← "Sell"/"Buy" PascalCase
        "orderType":     "Limit",         # ← PascalCase
        "securityType":  "Stock",         # ← required field
        "limitPrice":    round(limit_price, 2),
        "timeInForce":   "Day_Plus",      # Day_Plus covers pre-market + AH; Day gets rejected at/after 4pm ET
        "route":         "SMART",         # ← required, confirmed from routes API
    }
    r = tz_post(f"/v1/api/accounts/{ACCOUNT_ID}/order", payload, label)
    if not r.ok:
        # Log raw response body before raising — may be HTML error page
        log.error(f"  [{symbol}] Order FAILED {r.status_code} — raw body: {r.text[:500]}")
        r.raise_for_status()
    data = r.json()
    log.info(f"  [{symbol}] Order placed: clientOrderId={data.get('clientOrderId')} "
             f"status={data.get('orderStatus')}")
    return data


# ══════════════════════════════════════════════════════════════════════════════
# GRACEFUL ORDER STEPDOWN
# ══════════════════════════════════════════════════════════════════════════════
def place_order_with_stepdown(symbol, initial_quantity, limit_price, label="SHORT_ORDER"):
    """
    Attempt to place a short order at fixed percentage tiers of the located
    quantity. Steps: 100% → 80% → 60% → 40% → 20%. Stops at 20% — if that
    is also rejected, the symbol is blocked. Never goes below 20% of located.

    Example with initial_quantity=100 located shares:
      attempt 1: 100sh (100%)
      attempt 2:  80sh  (80%)
      attempt 3:  60sh  (60%)
      attempt 4:  40sh  (40%)
      attempt 5:  20sh  (20%) ← minimum, quit if this fails
    """
    tiers    = [1.0, 0.8, 0.6, 0.4, 0.2]
    last_exc = None
    for attempt, pct in enumerate(tiers, 1):
        qty = max(1, int(initial_quantity * pct))
        try:
            result = place_order("Sell", symbol, qty, limit_price, label)
            if attempt > 1:
                log.info(f"[{symbol}] Order accepted on attempt {attempt} "
                         f"at {int(pct*100)}%: placed {qty}sh "
                         f"(located {initial_quantity}sh)")
            return result
        except Exception as e:
            last_exc = e
            if attempt < len(tiers):
                next_pct = int(tiers[attempt] * 100)
                log.warning(f"[{symbol}] Order rejected at {qty}sh "
                            f"({int(pct*100)}%): {e} — "
                            f"stepping down to {next_pct}%")
            else:
                log.warning(f"[{symbol}] Order rejected at {qty}sh "
                            f"(20% minimum): {e} — giving up")
    raise last_exc or Exception(f"[{symbol}] All stepdown tiers failed "
                                f"(located {initial_quantity}sh)")


# ══════════════════════════════════════════════════════════════════════════════
# LOCATE HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def request_locate_quote(symbol, quantity, quote_req_id):
    payload = {
        "account":    ACCOUNT_ID,
        "symbol":     symbol,
        "quantity":   int(quantity),
        "quoteReqID": quote_req_id,
    }
    return tz_post("/v1/api/accounts/locates/quote", payload, "LOCATE_REQUEST")


def poll_locate_status(symbol, quote_req_id):
    """
    Poll locate history every LOCATE_POLL_INTERVAL seconds.
    Returns locate dict on actionable status, None on timeout.
    Status 65=Offered, 56=Rejected, 67=Expired, 52=Canceled
    """
    deadline = time.time() + LOCATE_POLL_TIMEOUT
    while time.time() < deadline:
        url = f"{BASE_URL}/v1/api/accounts/{ACCOUNT_ID}/locates/history"
        log.info(f"  → LOCATE_POLL GET {url}")
        r = requests.get(url, headers=tz_headers(), timeout=15)
        # Log full untruncated response so we can see all history entries
        log.info(f"  ← status: {r.status_code}  body: {r.text}")
        if r.status_code == 200:
            history = r.json().get("locateHistory", [])
            log.info(f"  [{symbol}] {len(history)} history entries — searching for {quote_req_id}")
            for item in history:
                # TZ returns quoteReqID (capital D)
                if item.get("quoteReqID") == quote_req_id:
                    status = item.get("locateStatus")
                    log.info(f"  [{symbol}] FOUND: status={status} | "
                             f"shares={item.get('locateShares')} | "
                             f"filled={item.get('filledShares')} | "
                             f"price=${item.get('locatePrice')} | "
                             f"type={item.get('locateType')} | "
                             f"text={item.get('text', '')} | "
                             f"error={item.get('locateError')}")
                    if status in (65, 56, 67, 52):
                        return item
            log.info(f"  [{symbol}] {quote_req_id} not yet in history — polling again")
        else:
            log.warning(f"  [{symbol}] Locate poll failed: {r.status_code}")
        time.sleep(LOCATE_POLL_INTERVAL)
    log.warning(f"  [{symbol}] Locate poll timed out after {LOCATE_POLL_TIMEOUT}s")
    return None


def accept_locate(quote_req_id):
    payload = {"accountId": ACCOUNT_ID, "quoteReqID": quote_req_id}
    return tz_post("/v1/api/accounts/locates/accept", payload, "LOCATE_ACCEPT")


# ══════════════════════════════════════════════════════════════════════════════
# LOCATE + SHORT BACKGROUND THREAD
# ══════════════════════════════════════════════════════════════════════════════
def check_existing_locate(symbol, required_quantity):
    """
    Check the inventory endpoint for currently available locate shares.
    This is the authoritative source — expired, used, or single-use locates
    will not appear here. Returns (True, available_qty) if usable, (False, 0) otherwise.
    """
    r = tz_get(f"/v1/api/accounts/{ACCOUNT_ID}/locates/inventory", "LOCATE_INVENTORY")
    if r.status_code != 200:
        log.warning(f"[{symbol}] Could not check locate inventory: {r.status_code}")
        return False, 0
    inventory = r.json().get("locateInventory", [])
    for item in inventory:
        if item.get("symbol", "").upper() != symbol.upper():
            continue
        available = int(item.get("available", 0))
        sold      = int(item.get("sold", 0))
        log.info(f"[{symbol}] Inventory: available={available} sold={sold} "
                 f"unavailable={item.get('unavailable',0)} toBeSold={item.get('toBeSold',0)}")
        if available >= required_quantity:
            log.info(f"[{symbol}] Existing locate usable: {available} shares available in inventory")
            return True, available
        elif available > 0:
            log.info(f"[{symbol}] Locate inventory insufficient: {available} available < {required_quantity} required — requesting fresh locate")
        else:
            log.info(f"[{symbol}] No available inventory for {symbol} — requesting fresh locate")
        return False, 0
    log.info(f"[{symbol}] Symbol not found in locate inventory — requesting fresh locate")
    return False, 0


# ══════════════════════════════════════════════════════════════════════════════
# PRICE SANITY  (Yahoo Finance)
# ══════════════════════════════════════════════════════════════════════════════
PRICE_SANITY_PCT = 0.15   # reject if QC price deviates >15% from Twelve Data/Yahoo — wide enough for volatile stocks

# ── TICKER ALIAS MAP ─────────────────────────────────────────────────────────
# QC data sometimes lags on ticker renames. Static fallback — entries here
# are used instantly with zero network calls and survive any Polygon outage.
# New renames are caught automatically by resolve_symbol() via Polygon and
# logged with instructions to add them here permanently.
#
#   "OLD_QC_TICKER": "CURRENT_TZ_TICKER"
TICKER_ALIASES = {
    "FPGP": "ALTO",   # Alto Ingredients rebranded; TZ still uses ALTO
    "AGE":  "SER",    # AgeX Therapeutics merged into Serina Therapeutics (Mar 27 2024)
}

# Polygon.io API key — used for live ticker rename detection
POLYGON_API_KEY = (os.getenv("POLYGON_API_KEY") or "").strip()

# Runtime cache: resolved renames discovered via Polygon this session.
# Avoids repeat Polygon calls for the same symbol within a day.
_polygon_rename_cache = {}
_polygon_cache_lock   = threading.Lock()


def resolve_symbol(qc_symbol):
    """
    Resolve a QC ticker to the current TZ-tradeable ticker.

    Resolution order:
      1. Static TICKER_ALIASES dict  — instant, no network, catches known renames
      2. Runtime cache               — avoids repeat Polygon calls within a session
      3. Polygon two-step CIK lookup — live auto-detection for unknown renames:
           a. GET /v3/reference/tickers?ticker=OLD&active=false  → cik
           b. GET /v3/reference/tickers?cik=CIK&active=true      → current ticker
      4. QC symbol as-is             — Polygon unavailable or no rename found

    On a new Polygon-detected rename, logs a loud warning with the exact line
    to add to TICKER_ALIASES so it's cached permanently on next deploy.

    Returns resolved symbol string.
    """
    # Pass 1: static alias map
    if qc_symbol in TICKER_ALIASES:
        resolved = TICKER_ALIASES[qc_symbol]
        log.info(f"[{qc_symbol}] Alias map → {resolved}")
        return resolved

    # Pass 2: runtime cache (Polygon result from earlier in this session)
    with _polygon_cache_lock:
        if qc_symbol in _polygon_rename_cache:
            resolved = _polygon_rename_cache[qc_symbol]
            log.info(f"[{qc_symbol}] Runtime cache → {resolved}")
            return resolved

    # Pass 3: Polygon live lookup
    if not POLYGON_API_KEY:
        log.info(f"[{qc_symbol}] POLYGON_API_KEY not set — skipping rename check")
        return qc_symbol

    try:
        base = "https://api.polygon.io/v3/reference/tickers"

        # Step 3a: look up old ticker (may be delisted/renamed)
        r1 = requests.get(
            base,
            params={"ticker": qc_symbol, "active": "false", "apiKey": POLYGON_API_KEY},
            timeout=8,
        )
        if not r1.ok or not r1.json().get("results"):
            log.info(f"[{qc_symbol}] Polygon: ticker not found in delisted list — no rename")
            return qc_symbol

        cik = r1.json()["results"][0].get("cik")
        if not cik:
            log.info(f"[{qc_symbol}] Polygon: no CIK returned — cannot resolve rename")
            return qc_symbol

        # Step 3b: look up current active ticker by CIK
        r2 = requests.get(
            base,
            params={"cik": cik, "active": "true", "apiKey": POLYGON_API_KEY},
            timeout=8,
        )
        if not r2.ok or not r2.json().get("results"):
            log.info(f"[{qc_symbol}] Polygon: no active ticker found for CIK {cik}")
            return qc_symbol

        current_ticker = r2.json()["results"][0].get("ticker", "").upper()
        if not current_ticker or current_ticker == qc_symbol:
            log.info(f"[{qc_symbol}] Polygon: no rename detected (CIK={cik})")
            return qc_symbol

        # New rename found — cache it and log a loud warning
        with _polygon_cache_lock:
            _polygon_rename_cache[qc_symbol] = current_ticker

        log.warning(
            f"\n"
            f"╔══════════════════════════════════════════════════════════╗\n"
            f"║  TICKER RENAME DETECTED via Polygon                      ║\n"
            f"║  QC ticker : {qc_symbol:<10}  →  Current : {current_ticker:<10}       ║\n"
            f"║  CIK       : {cik:<46} ║\n"
            f"║  ACTION    : add to TICKER_ALIASES in server code:       ║\n"
            f'║    "{qc_symbol}": "{current_ticker}",{" " * max(0, 43 - len(qc_symbol) - len(current_ticker))}║\n'
            f"╚══════════════════════════════════════════════════════════╝"
        )
        return current_ticker

    except Exception as e:
        log.warning(f"[{qc_symbol}] Polygon rename lookup failed: {e} — using QC symbol as-is")
        return qc_symbol


def _get_twelvedata_price(symbol):
    """
    Fetch current price from Twelve Data.
    Returns (price, "twelvedata") or (None, None) on failure.
    Free tier: 800 calls/day, 8/min. No session logic needed — returns
    live price for whatever session is active (pre, RTH, AH).
    Signup: twelvedata.com — free, no credit card.
    """
    if not TWELVEDATA_API_KEY:
        return None, None
    import urllib.request as _req
    url = f"https://api.twelvedata.com/price?symbol={symbol}&apikey={TWELVEDATA_API_KEY}"
    try:
        req = _req.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with _req.urlopen(req, timeout=5) as resp:
            data = __import__('json').loads(resp.read())
        if "price" not in data:
            log.warning(f"Twelve Data error for {symbol}: {data.get('message', data)}")
            return None, None
        price = float(data["price"])
        if price > 0:
            return price, "twelvedata"
        return None, None
    except Exception as e:
        log.warning(f"Twelve Data price lookup failed for {symbol}: {e}")
        return None, None


def _get_yahoo_price(symbol):
    """
    Fetch price from Yahoo Finance — session-aware.
    Uses v7/finance/quote endpoint which carries live postMarketPrice/preMarketPrice
    even when the v8/chart endpoint only returns the prior RTH close.
    Falls back to v8/chart if v7 fails.
    Returns (price, session_label) or (None, None) on failure.
    """
    import urllib.request as _req, datetime as _dt, json as _json

    # Determine ET session
    utc_now   = _dt.datetime.now(_dt.timezone.utc)
    year      = utc_now.year
    dst_start = _dt.datetime(year, 3, 1) + _dt.timedelta(
        days=(6 - _dt.datetime(year, 3, 1).weekday()) % 7 + 7)
    dst_end   = _dt.datetime(year, 11, 1) + _dt.timedelta(
        days=(6 - _dt.datetime(year, 11, 1).weekday()) % 7)
    is_dst    = dst_start <= utc_now.replace(tzinfo=None) < dst_end
    offset    = -4 if is_dst else -5
    et_now    = utc_now + _dt.timedelta(hours=offset)
    et_time   = et_now.hour * 60 + et_now.minute
    rth_open  = 9 * 60 + 30
    rth_close = 16 * 60
    pre_open  = 4 * 60
    is_pre    = pre_open <= et_time < rth_open
    is_rth    = rth_open <= et_time < rth_close
    is_ah     = rth_close <= et_time < 20 * 60

    # ── v8 chart with includePrePost=true — read last bar, not meta ────────
    # Yahoo meta.regularMarketPrice never updates in AH even with hasPrePostMarketData=true.
    # The actual AH/pre-market prices are only in the bar arrays when includePrePost=true.
    # We take the last non-null close from the price array as the live price.
    try:
        url = (f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
               f"?interval=1m&range=2d&includePrePost=true")
        req = _req.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        with _req.urlopen(req, timeout=5) as resp:
            raw   = _json.loads(resp.read())
        result    = raw["chart"]["result"][0]
        meta      = result["meta"]
        closes    = result["indicators"]["quote"][0].get("close", [])
        timestamps = result.get("timestamp", [])

        if is_rth:
            # RTH: meta.regularMarketPrice is accurate during regular hours
            price = meta.get("regularMarketPrice")
            label = "yahoo_v8(regular)" if price else None
        else:
            # AH / pre-market: walk backwards through bars to find last non-null
            # close from the most recent trading session.
            #
            # Lookback window:
            #   AH (4pm-8pm):       4 hours  — only accept today's AH bars
            #   Pre-market (4am-9:30am): 13 hours — covers yesterday's AH session
            #     (yesterday AH ended 8pm ET = ~12h ago at 4am ET, so 13h catches it)
            #
            # This prevents pre-market entries from falling back to yesterday's
            # 4pm RTH close when a stock ran in AH and has no pre-market bars yet.
            now_ts     = utc_now.timestamp()
            lookback_s = 4 * 3600 if is_ah else 13 * 3600
            price  = None
            label  = None
            for i in range(len(closes) - 1, -1, -1):
                c = closes[i]
                if c is None:
                    continue
                ts = timestamps[i] if i < len(timestamps) else 0
                if now_ts - ts > lookback_s:
                    break
                price = float(c)
                label = "yahoo_v8(postMarket)" if is_ah else "yahoo_v8(preMarket)"
                break

            # If no session bar found, fall back to meta close but log clearly
            if price is None:
                price = meta.get("regularMarketPrice")
                label = "yahoo_v8(lastClose)" if price else None
                if price:
                    log.warning(f"Yahoo: no session bars found within {lookback_s//3600}h — "
                                f"using lastClose ${price:.4f} (may be stale)")
                # Log diagnostic info so we can debug future cases:
                # show total bar count, last timestamp in array, and last non-null close
                total_bars   = len(closes)
                last_ts      = timestamps[-1] if timestamps else 0
                last_ts_age  = int(now_ts - last_ts)
                # find last non-null close regardless of lookback
                last_nonnull = next((closes[i] for i in range(len(closes)-1, -1, -1) if closes[i] is not None), None)
                last_nonnull_ts = 0
                for i in range(len(closes)-1, -1, -1):
                    if closes[i] is not None:
                        last_nonnull_ts = timestamps[i] if i < len(timestamps) else 0
                        break
                log.warning(f"Yahoo diagnostic: total_bars={total_bars} | "
                            f"last_ts={last_ts} (age={last_ts_age}s) | "
                            f"last_nonnull_close=${last_nonnull} @ ts={last_nonnull_ts} "
                            f"(age={int(now_ts - last_nonnull_ts)}s) | "
                            f"lookback={lookback_s}s | "
                            f"url=?interval=1m&range=2d&includePrePost=true")

        if price and label:
            log.info(f"[{symbol}] Yahoo price: {label} = ${price:.4f}")
            return float(price), label
        return None, None
    except Exception as e:
        log.warning(f"[{symbol}] Yahoo v8 chart failed: {e}")
        return None, None


def get_live_price(symbol):
    """
    Get the best available real-time price for a symbol, session-aware.

    Twelve Data free tier FREEZES at the 4:00 PM close price — it does NOT
    update during pre-market or after-hours. Using it in AH would give the
    prior close, causing false sanity-check blocks on AH movers.

    Strategy:
      - RTH (9:30 AM - 4:00 PM ET): Twelve Data first (real-time) → Yahoo fallback
      - AH  (4:00 PM - 8:00 PM ET): Yahoo postMarketPrice only
      - Pre (4:00 AM - 9:30 AM ET): Yahoo preMarketPrice only

    Returns (price, source_label) or (None, None) if all sources fail.
    """
    import datetime as _dt
    now_et = _dt.datetime.now(_dt.timezone.utc).astimezone(
        _dt.timezone(_dt.timedelta(hours=-4)))  # ET (UTC-4 in summer, UTC-5 in winter)
    t = now_et.time()

    rth_start = _dt.time(9, 30)
    rth_end   = _dt.time(16, 0)

    if rth_start <= t < rth_end:
        # RTH: Twelve Data is reliable, try it first
        price, label = _get_twelvedata_price(symbol)
        if price:
            return price, label
        log.info(f"[{symbol}] Twelve Data unavailable — trying Yahoo fallback")
        return _get_yahoo_price(symbol)
    else:
        # Pre-market or AH: Twelve Data is stale — go straight to Yahoo
        log.info(f"[{symbol}] Outside RTH ({t.strftime('%H:%M')} ET) — using Yahoo session price directly")
        return _get_yahoo_price(symbol)


# Keep get_yahoo_price as an alias so cover monitor and cancel_and_cleanup
# still work without changes — they call get_yahoo_price for aggressive cover pricing
def get_yahoo_price(symbol):
    return get_live_price(symbol)



def locate_and_short(symbol, qc_quantity, entry_price):
    log.info(f"[{symbol}] ── LOCATE THREAD STARTED ──────────────────────")
    log.info(f"[{symbol}] QC requested: qty={qc_quantity} entry_price=${entry_price}")

    # (ticker alias already applied at webhook entry point)

    #── Step 0: Price sanity check (validation only — QC price is trusted) ──────
    # Twelve Data / Yahoo are used ONLY to catch wildly wrong QC prices
    # (e.g. stale warmup bar prices, bad ticks). They are NOT used to set the
    # entry price — both sources have 3-15 min delays on free tiers which would
    # give a worse price than QC's own live feed.
    # QC receives real-time data in live mode — it is the most accurate source.
    # PRICE_SANITY_PCT (15%) is deliberately wide to catch only genuine errors (e.g. 236% bad ticks),
    log.info(f"[{symbol}] Step 0: Price sanity check (QC=${entry_price:.4f})")
    live_price, live_session = get_live_price(symbol)
    if live_price is not None:
        deviation = abs(entry_price - live_price) / live_price
        log.info(f"[{symbol}] Price check: {live_session}=${live_price:.4f} | QC=${entry_price:.4f} | "
                 f"deviation={deviation*100:.2f}% (limit={PRICE_SANITY_PCT*100:.0f}%)")

        if live_session == "yahoo_v8(lastClose)":
            # lastClose is yesterday's RTH close — gap stocks will always deviate from it.
            # A deviation here means the stock moved overnight, which is exactly what we
            # are trading. Skip the sanity check entirely when lastClose is the only reference.
            log.info(f"[{symbol}] Price sanity SKIPPED — {live_session} is yesterday's close, "
                     f"not a valid reference for pre-market gap stocks. "
                     f"Proceeding with QC price ${entry_price:.4f}")
        elif deviation > PRICE_SANITY_PCT:
            block(symbol, f"price sanity FAILED: QC=${entry_price} vs {live_session}=${live_price:.4f} "
                          f"({deviation*100:.1f}% > {PRICE_SANITY_PCT*100:.0f}% limit — likely bad/stale tick)")
            return
        else:
            log.info(f"[{symbol}] Price sanity OK ✓ — using QC price ${entry_price:.4f} "
                     f"({live_session} ${live_price:.4f} is reference only, not used for order)")
    else:
        log.warning(f"[{symbol}] All price sources exhausted — proceeding with QC price (unvalidated)")

        # ── Step 1: Check buying power + margin ───────────────────────────────────
    log.info(f"[{symbol}] Step 1: Checking buying power and margin")
    account = get_account_details()
    if account is None:
        block(symbol, "failed to retrieve account details")
        return

    available_cash   = float(account.get("buyingPower",     account.get("availableCash", 0)))
    margin_available = float(account.get("marginAvailable", available_cash))
    log.info(f"[{symbol}] buyingPower=${available_cash:,.2f} | marginAvailable=${margin_available:,.2f}")

    usable_capital = min(available_cash, margin_available)

    # TZ margin requirements for short selling (session-aware):
    # https://tradezero.com/support/questions/what-are-your-margin-requirements-tradezero-international
    #
    # Session       < $2.50          $2.50–$5.00      >= $5.00
    # 4am–4pm       $2.50/share      16.67% (6×)      16.67% (6×)
    # 4pm–8pm       $5.00/share      $5.00/share      50%   (2×)
    #
    from datetime import datetime as _datetime, time as _time
    now_et = _datetime.now(ZoneInfo("America/New_York"))
    et_time = now_et.time()
    rth_open  = _time(4,  0)
    rth_close = _time(16, 0)
    ah_close  = _time(20, 0)
    in_premarket_or_rth = rth_open <= et_time < rth_close
    in_ah               = rth_close <= et_time < ah_close

    if in_premarket_or_rth:
        if entry_price < 2.50:
            margin_per_share = 2.50
        else:
            # 6× leverage → margin = price / 6
            margin_per_share = entry_price / 6.0
    elif in_ah:
        if entry_price < 5.0:
            margin_per_share = 5.0
        else:
            # 2× leverage → margin = price / 2
            margin_per_share = entry_price / 2.0
    else:
        # Outside trading hours — use conservative AH rate
        margin_per_share = max(5.0, entry_price / 2.0)

    max_affordable = int(usable_capital / margin_per_share)
    log.info(f"[{symbol}] session={'pre/RTH' if in_premarket_or_rth else 'AH' if in_ah else 'closed'} | "
             f"margin_per_share=${margin_per_share:.4f} | "
             f"max_affordable={max_affordable} "
             f"(usable=${usable_capital:,.2f} / margin=${margin_per_share:.4f}/share)")

    # ── Step 2: Can we afford minimum? ────────────────────────────────────────
    if max_affordable < MIN_LOCATE_QUANTITY:
        block(symbol, f"cannot afford minimum {MIN_LOCATE_QUANTITY} shares — "
                      f"max_affordable={max_affordable} at ${entry_price}")
        return

    # ── Step 3: Request quantity ───────────────────────────────────────────────
    # Floor to nearest 100 (never round up — must not exceed affordable)
    # e.g. 73→100 (min floor), 149→100, 150→100, 251→200
    raw_quantity = min(qc_quantity, max_affordable)
    request_quantity = max(MIN_LOCATE_QUANTITY, int(raw_quantity / 100) * 100)
    # Safety: after flooring to 100s, ensure we can still afford it
    if request_quantity > max_affordable:
        request_quantity = int(max_affordable / 100) * 100
    if request_quantity < MIN_LOCATE_QUANTITY:
        block(symbol, f"cannot afford minimum {MIN_LOCATE_QUANTITY} shares after rounding — "
                      f"max_affordable={max_affordable} at ${entry_price}")
        return
    log.info(f"[{symbol}] request_quantity={request_quantity} "
             f"(floored to 100s | qc={qc_quantity}, raw={raw_quantity}, max_affordable={max_affordable})")

    # ── Step 3b: Check if valid locate already exists for today ─────────────
    log.info(f"[{symbol}] Step 3b: Checking for existing locate today")
    has_locate, located_qty = check_existing_locate(symbol, request_quantity)
    if has_locate:
        log.info(f"[{symbol}] Skipping locate request — using existing locate "
                 f"({located_qty} shares already accepted today)")
        # Use located_qty directly — locate was already paid for at that quantity.
        # Do NOT cap by request_quantity: BP may have ticked down slightly since the
        # locate was obtained (locate cost itself reduces BP), which would cause the
        # order to be placed for fewer shares than we hold locates for.
        final_quantity = located_qty
        log.info(f"[{symbol}] Step 11: Placing short | qty={final_quantity} | "
                 f"limit=${entry_price} (QC pre-buffered)")
        try:
            limit_price = entry_price  # QC already applied its buffer
            result = place_order_with_stepdown(symbol, final_quantity, limit_price, "SHORT_ORDER")
            placed_qty = result.get("orderQuantity", final_quantity)
            set_state(symbol, state="ACTIVE", client_order_id=result.get("clientOrderId"))
            log.info(f"[{symbol}] STATE → ACTIVE")
            log.info(f"[{symbol}] SHORT PLACED (existing locate) | qty={placed_qty} | "
                     f"limit=${limit_price} | clientOrderId={result.get('clientOrderId')} | "
                     f"status={result.get('orderStatus')}")
            threading.Thread(
                target=monitor_short_fill,
                args=(symbol, result.get("clientOrderId"), SHORT_FILL_TIMEOUT),
                daemon=True
            ).start()
        except Exception as e:
            block(symbol, f"order placement failed after all stepdown attempts: {e}")
        return

    # ── Step 4: Request locate quote ──────────────────────────────────────────
    quote_req_id = f"Q{int(time.time() * 1000)}"
    set_state(symbol, quote_req_id=quote_req_id)
    log.info(f"[{symbol}] Step 4: Requesting locate | quoteReqID={quote_req_id}")
    r = request_locate_quote(symbol, request_quantity, quote_req_id)

    if r.status_code not in (200, 201, 202):
        body = r.text
        if "insufficient" in body.lower() or "inventory" in body.lower():
            block(symbol, f"locate request failed — insufficient inventory: {body[:200]}")
        else:
            block(symbol, f"locate request failed: {r.status_code} | {body[:200]}")
        return

    # ── Step 5: Poll locate status ────────────────────────────────────────────
    log.info(f"[{symbol}] Step 5: Polling locate status (max {LOCATE_POLL_TIMEOUT}s)")

    # Abort if state changed while we were requesting (e.g. CANCEL came in)
    if get_state(symbol).get("state") != "LOCATING":
        log.warning(f"[{symbol}] State changed during locate request — aborting")
        return

    locate = poll_locate_status(symbol, quote_req_id)

    # Abort if state changed during polling
    if get_state(symbol).get("state") != "LOCATING":
        log.warning(f"[{symbol}] State changed during locate poll — aborting")
        return

    if locate is None:
        block(symbol, f"locate timed out after {LOCATE_POLL_TIMEOUT}s")
        return

    status = locate.get("locateStatus")

    if status == 56:
        block(symbol, f"locate rejected by TZ | text: {locate.get('text', '')}")
        return
    if status == 67:
        block(symbol, "locate quote expired")
        return
    if status == 52:
        block(symbol, "locate cancelled")
        return
    if status != 65:
        block(symbol, f"unexpected locate status: {status}")
        return

    # Status 65 — Offered
    locate_price     = float(locate.get("locatePrice", 0))
    offered_quantity = int(locate.get("locateShares", 0))
    locate_type      = locate.get("locateType", "")
    locate_error     = locate.get("locateError", 0)
    locate_text      = locate.get("text", "")

    log.info(f"[{symbol}] Locate OFFERED: {offered_quantity} shares @ "
             f"${locate_price}/share | type={locate_type} | "
             f"error={locate_error} | text={locate_text}")

    if locate_error == 1:
        block(symbol, f"locate offered with error flag | text: {locate_text}")
        return

    if "insufficient" in locate_text.lower() or "inventory" in locate_text.lower():
        block(symbol, f"insufficient inventory in locate response: {locate_text}")
        return

    # ── Step 7: Final quantity ─────────────────────────────────────────────────
    final_quantity = min(request_quantity, offered_quantity) if offered_quantity > 0 else request_quantity
    log.info(f"[{symbol}] final_quantity={final_quantity} "
             f"(min of requested={request_quantity}, offered={offered_quantity})")

    # ── Step 8: Minimum quantity check ────────────────────────────────────────
    if final_quantity < MIN_LOCATE_QUANTITY:
        block(symbol, f"final quantity {final_quantity} below minimum {MIN_LOCATE_QUANTITY}")
        return

    # ── Step 9: Locate cost check ─────────────────────────────────────────────
    if entry_price > 0 and locate_price > 0:
        locate_cost_pct   = locate_price / entry_price
        total_locate_cost = locate_price * final_quantity
        log.info(f"[{symbol}] Locate cost: ${locate_price}/share x {final_quantity} = "
                 f"${total_locate_cost:.2f} total | "
                 f"{locate_cost_pct*100:.3f}% of entry ${entry_price} | "
                 f"threshold {MAX_LOCATE_COST_PCT*100:.1f}%")
        if locate_cost_pct > MAX_LOCATE_COST_PCT:
            block(symbol, f"locate too expensive: {locate_cost_pct*100:.3f}% > "
                          f"{MAX_LOCATE_COST_PCT*100:.1f}% threshold")
            return
    else:
        log.warning(f"[{symbol}] Cannot calculate locate cost "
                    f"(locate_price={locate_price} entry_price={entry_price}) — proceeding")

    # ── Step 10: Accept locate ────────────────────────────────────────────────
    log.info(f"[{symbol}] Step 10: Accepting locate quoteReqID={quote_req_id}")
    r_accept = accept_locate(quote_req_id)
    if r_accept.status_code not in (200, 201, 202):
        block(symbol, f"locate accept failed: {r_accept.status_code} | {r_accept.text[:200]}")
        return
    log.info(f"[{symbol}] Locate accepted successfully")

    # ── Poll inventory until locate is registered (fixes R43) ────────────
    # TZ sends "locateAcceptSent:true" before fully processing the accept.
    # Placing the order immediately causes R43 "not enough located shares".
    # Poll the inventory endpoint until the shares appear, hard timeout 15s.
    log.info(f"[{symbol}] Step 10b: Polling inventory to confirm locate registered...")
    inventory_confirmed = False
    poll_deadline = time.time() + 15   # max 15s wait
    poll_attempt  = 0
    while time.time() < poll_deadline:
        poll_attempt += 1
        time.sleep(1)
        r_inv = tz_get(f"/v1/api/accounts/{ACCOUNT_ID}/locates/inventory", "LOCATE_CONFIRM")
        if r_inv.status_code == 200:
            inventory = r_inv.json().get("locateInventory", [])
            for item in inventory:
                if item.get("symbol", "").upper() == symbol.upper():
                    available = int(item.get("available", 0))
                    if available >= final_quantity:
                        log.info(f"[{symbol}] Inventory confirmed: {available} shares available "
                                 f"(attempt {poll_attempt})")
                        inventory_confirmed = True
                        break
        if inventory_confirmed:
            break
        log.info(f"[{symbol}] Locate not yet in inventory — retrying (attempt {poll_attempt})")

    if not inventory_confirmed:
        block(symbol, f"locate inventory not confirmed after 15s — R43 risk too high, aborting")
        return

    if get_state(symbol).get("state") != "LOCATING":
        log.warning(f"[{symbol}] State changed after accept — aborting order")
        return

    # ── Step 11: Place short limit order ──────────────────────────────────────
    limit_price = entry_price  # QC already applied its buffer
    log.info(f"[{symbol}] Step 11: Placing short | qty={final_quantity} | "
             f"entry/limit=${limit_price} (QC pre-buffered)")
    try:
        result = place_order_with_stepdown(symbol, final_quantity, limit_price, "SHORT_ORDER")
        placed_qty = result.get("orderQuantity", final_quantity)
        set_state(
            symbol,
            state="ACTIVE",
            entry_price=entry_price,
            quantity=placed_qty,
            client_order_id=result.get("clientOrderId"),
        )
        log.info(f"[{symbol}] SHORT PLACED | qty={placed_qty} | "
                 f"limit=${limit_price} | clientOrderId={result.get('clientOrderId')} | "
                 f"status={result.get('orderStatus')}")
        threading.Thread(
            target=monitor_short_fill,
            args=(symbol, result.get("clientOrderId"), SHORT_FILL_TIMEOUT),
            daemon=True
        ).start()
    except Exception as e:
        block(symbol, f"order placement failed after all stepdown attempts: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# CANCEL + CLEANUP HELPER
# ══════════════════════════════════════════════════════════════════════════════

def monitor_short_fill(symbol, client_order_id, timeout_minutes):
    """
    After placing a short, wait up to timeout_minutes for it to fill.
    If it doesn't fill, cancel it and block the symbol.
    Runs in a background thread.
    """
    import time as _time
    deadline = _time.time() + timeout_minutes * 60
    poll_interval = 15  # seconds

    log.info(f"[{symbol}] SHORT MONITOR started | order={client_order_id} | timeout={timeout_minutes}min")

    while _time.time() < deadline:
        _time.sleep(poll_interval)

        # If state changed, handle carefully:
        # FLAT/COVER handled it → safe to exit
        # BLOCKED → order may still be live on TZ — cancel it before exiting
        state = get_state(symbol).get("state")
        if state != "ACTIVE":
            if state == "BLOCKED":
                log.warning(f"[{symbol}] SHORT MONITOR: state=BLOCKED — cancelling orphaned order {client_order_id}")
                try:
                    cancelled = cancel_all_open_orders(symbol)
                    log.info(f"[{symbol}] SHORT MONITOR: cancelled {cancelled} open order(s) after BLOCKED state")
                    # Check if the order actually filled before we could cancel
                    position = get_position(symbol)
                    if position and float(position.get("shares", 0)) < 0:
                        log.warning(f"[{symbol}] SHORT MONITOR: order filled BEFORE cancel — short position exists! "
                                    f"Manual action required or wait for COVER webhook.")
                except Exception as e:
                    log.error(f"[{symbol}] SHORT MONITOR: error cancelling order after BLOCKED: {e}")
            else:
                log.info(f"[{symbol}] SHORT MONITOR: state={state} — stopping monitor")
            return

        # Check order status
        try:
            r = tz_get(f"/v1/api/accounts/{ACCOUNT_ID}/orders", "SHORT_MONITOR_ORDERS")
            orders = r.json() if r.ok else []
            if not isinstance(orders, list):
                orders = orders.get("orders", [])
            order = next((o for o in orders if o.get("clientOrderId") == client_order_id), None)
            if order:
                status = order.get("orderStatus", "")
                executed = int(order.get("executed", 0))
                log.info(f"[{symbol}] SHORT MONITOR: status={status} executed={executed}")
                if status in ("Filled",) or executed > 0:
                    log.info(f"[{symbol}] SHORT MONITOR: order filled — monitor done")
                    return
                if status in ("Canceled", "Rejected"):
                    reject_text = order.get("text") or order.get("rejectReason") or "no reason given"
                    log.warning(f"[{symbol}] SHORT MONITOR: order {status} | reason: {reject_text}")
                    block(symbol, f"short order {status}: {reject_text}")
                    return
        except Exception as e:
            log.warning(f"[{symbol}] SHORT MONITOR: poll error — {e}")

    # Timeout reached — cancel and block
    log.warning(f"[{symbol}] SHORT MONITOR: timeout after {timeout_minutes}min — cancelling and blocking")
    cancel_and_cleanup(symbol, f"short entry did not fill within {timeout_minutes} minutes")


def monitor_cover_fill(symbol, client_order_id, timeout_minutes):
    """
    After placing a cover, wait up to timeout_minutes for it to fill.
    If it doesn't fill, cancel it and place an aggressive market-price cover.
    Runs in a background thread.
    """
    import time as _time
    deadline = _time.time() + timeout_minutes * 60
    poll_interval = 15  # seconds

    log.info(f"[{symbol}] COVER MONITOR started | order={client_order_id} | timeout={timeout_minutes}min")

    while _time.time() < deadline:
        _time.sleep(poll_interval)

        state = get_state(symbol).get("state")
        if state == "FLAT":
            log.info(f"[{symbol}] COVER MONITOR: already FLAT — done")
            return

        try:
            r = tz_get(f"/v1/api/accounts/{ACCOUNT_ID}/orders", "COVER_MONITOR_ORDERS")
            orders = r.json() if r.ok else []
            if not isinstance(orders, list):
                orders = orders.get("orders", [])
            order = next((o for o in orders if o.get("clientOrderId") == client_order_id), None)
            if order:
                status = order.get("orderStatus", "")
                executed = int(order.get("executed", 0))
                log.info(f"[{symbol}] COVER MONITOR: status={status} executed={executed}")
                if status in ("Filled",) or executed > 0:
                    log.info(f"[{symbol}] COVER MONITOR: cover filled — done")
                    set_state(symbol, state="FLAT", reason="cover filled (monitor confirmed)")
                    return
                if status in ("Canceled", "Rejected"):
                    log.warning(f"[{symbol}] COVER MONITOR: cover {status} — will retry aggressively")
                    break
        except Exception as e:
            log.warning(f"[{symbol}] COVER MONITOR: poll error — {e}")

    # Timeout or cancellation — cancel stale order and retry aggressively
    log.warning(f"[{symbol}] COVER MONITOR: cover did not fill — cancelling and retrying aggressively")
    cancel_all_open_orders(symbol)

    # Check still short
    position = get_position(symbol)
    if not position or float(position.get("shares", 0)) >= 0:
        log.info(f"[{symbol}] COVER MONITOR: no short position remaining — marking FLAT")
        set_state(symbol, state="FLAT", reason="cover monitor: no position found on retry")
        return

    # Get aggressive price from Yahoo (current market), fall back to priceClose
    yahoo_price, yahoo_session = get_yahoo_price(symbol)
    if yahoo_price:
        aggressive_price = round(yahoo_price * (1 + COVER_RETRY_BUFFER), 2)
        log.info(f"[{symbol}] COVER MONITOR: aggressive retry at ${aggressive_price} "
                 f"(Yahoo={yahoo_price} session={yahoo_session} + {COVER_RETRY_BUFFER*100:.1f}%)")
    else:
        # Yahoo unavailable — try priceClose from position as last resort
        priceClose = float(position.get("priceClose") or 0)
        if priceClose > 0:
            aggressive_price = round(priceClose * (1 + COVER_RETRY_BUFFER), 2)
            log.warning(f"[{symbol}] COVER MONITOR: Yahoo unavailable — using priceClose fallback ${aggressive_price}")
        else:
            log.error(f"[{symbol}] COVER MONITOR: no Yahoo and no priceClose — MANUAL ACTION REQUIRED")
            block(symbol, "aggressive cover failed: no price source available — MANUAL ACTION REQUIRED")
            return

    cover_qty = abs(int(float(position.get("shares", 0))))
    try:
        result = place_order("Buy", symbol, cover_qty, aggressive_price, "COVER_AGGRESSIVE")
        log.info(f"[{symbol}] COVER MONITOR: aggressive cover placed | "
                 f"clientOrderId={result.get('clientOrderId')} status={result.get('orderStatus')}")
        set_state(symbol, state="FLAT", reason="aggressive cover placed")
    except Exception as e:
        log.error(f"[{symbol}] COVER MONITOR: aggressive cover FAILED: {e} — MANUAL ACTION REQUIRED")
        block(symbol, "aggressive cover failed — MANUAL ACTION REQUIRED")


def cancel_and_cleanup(symbol, reason):
    log.info(f"[{symbol}] CANCEL+CLEANUP: {reason}")

    cancelled = cancel_all_open_orders(symbol)
    log.info(f"[{symbol}] Cancelled {cancelled} open order(s)")

    position = get_position(symbol)
    if position:
        shares = float(position.get("shares", 0))
        if shares < 0:
            cover_qty   = abs(int(shares))
            # priceClose = current market price (used for unrealised P&L)
            # priceAvg   = entry price — NOT useful for cover pricing
            last_price = float(position.get("priceClose") or 0)
            if last_price <= 0:
                # priceClose unavailable (e.g. AH with no last trade) — try Yahoo
                yahoo_price, yahoo_session = get_yahoo_price(symbol)
                if yahoo_price:
                    last_price = yahoo_price
                    log.info(f"[{symbol}] priceClose=0, using Yahoo ${last_price} ({yahoo_session})")
                else:
                    log.error(f"[{symbol}] Cannot determine cover price — MANUAL ACTION REQUIRED")
                    block(symbol, "cleanup: no priceClose and Yahoo unavailable — MANUAL ACTION REQUIRED")
                    return
            cover_limit = round(last_price * 1.005, 2)  # server retry: 0.5% above live price
            log.info(f"[{symbol}] Found short position: {cover_qty} shares | "
                     f"priceClose=${last_price} | covering at ${cover_limit}")
            try:
                result = place_order("Buy", symbol, cover_qty, cover_limit, "COVER_ON_CANCEL")
                log.info(f"[{symbol}] Cover placed: {result.get('clientOrderId')} "
                         f"status={result.get('orderStatus')}")
            except Exception as e:
                log.error(f"[{symbol}] Cover order FAILED: {e} — MANUAL ACTION MAY BE REQUIRED")
        elif shares > 0:
            log.warning(f"[{symbol}] Found LONG position ({shares} shares) — not touching it")
        else:
            log.info(f"[{symbol}] Position shares=0 — nothing to cover")
    else:
        log.info(f"[{symbol}] No position found — nothing to cover")

    block(symbol, reason)


# ══════════════════════════════════════════════════════════════════════════════
# FLASK APP
# ══════════════════════════════════════════════════════════════════════════════
app = Flask(__name__)


@app.route("/", methods=["GET"])
def root():
    return jsonify({"status": "ok", "server": "tz-eod-short-webhook"}), 200


_last_health_log = 0  # epoch seconds of last health log

# Cache TZ API status so health endpoint never blocks on external calls
_tz_cache = {"ok": True, "detail": "startup", "routes": "", "account_bp": None}
_tz_cache_lock = threading.Lock()
_last_tz_check = 0

def _refresh_tz_cache():
    """Background thread: refresh TZ API status every 60s."""
    global _last_tz_check
    while True:
        time.sleep(60)
        try:
            r = requests.get(f"{BASE_URL}/v1/api/accounts", headers=tz_headers(), timeout=8)
            ok = r.status_code == 200
            detail = f"http_{r.status_code}: {r.text[:200]}"
            # Also grab account bp
            r2 = requests.get(f"{BASE_URL}/v1/api/account/{ACCOUNT_ID}", headers=tz_headers(), timeout=8)
            bp = r2.json().get("bp") if r2.ok else None
            # Routes (less critical, longer cache ok)
            try:
                r3 = requests.get(f"{BASE_URL}/v1/api/accounts/{ACCOUNT_ID}/routes", headers=tz_headers(), timeout=5)
                routes = r3.text[:300]
            except Exception:
                routes = _tz_cache.get("routes", "")
            with _tz_cache_lock:
                _tz_cache.update({"ok": ok, "detail": detail, "routes": routes, "account_bp": bp})
            _last_tz_check = time.time()
        except Exception as e:
            with _tz_cache_lock:
                _tz_cache["ok"] = False
                _tz_cache["detail"] = f"error: {e}"

threading.Thread(target=_refresh_tz_cache, daemon=True).start()


@app.route("/health", methods=["GET", "HEAD"])
def health():
    with state_lock:
        states = {k: v.get("state") for k, v in symbol_state.items()}
    with _tz_cache_lock:
        cache = dict(_tz_cache)

    status = {
        "server":        "ok",
        "tz_api":        "ok" if cache["ok"] else "unreachable",
        "tz_detail":     cache["detail"],
        "account":       ACCOUNT_ID,
        "key_preview":   API_KEY[:8] + "..." if API_KEY else "NOT SET",
        "symbol_states": states,
        "routes":        cache["routes"],
        "ticker_aliases": TICKER_ALIASES,
        "polygon_key":   POLYGON_API_KEY[:8] + "..." if POLYGON_API_KEY else "NOT SET",
        "twelvedata_key": TWELVEDATA_API_KEY[:8] + "..." if TWELVEDATA_API_KEY else "NOT SET",
        "config": {
            "max_locate_cost_pct": MAX_LOCATE_COST_PCT,
            "min_locate_quantity": MIN_LOCATE_QUANTITY,
            "locate_poll_timeout": LOCATE_POLL_TIMEOUT,
            "price_handling": "QC pre-buffered — server uses received price directly",
        },
    }
    global _last_health_log
    import time as _t
    now_ts = _t.time()
    if now_ts - _last_health_log >= 300:
        log.info(f"HEALTH: {status}")
        _last_health_log = now_ts
    return jsonify(status), 200


@app.route("/state", methods=["GET"])
def state_endpoint():
    with state_lock:
        return jsonify(dict(symbol_state)), 200


@app.route("/reset/<symbol>", methods=["POST"])
def reset_symbol(symbol):
    """Emergency manual reset of a symbol to FLAT."""
    symbol = symbol.upper()
    with state_lock:
        symbol_state[symbol] = {"state": "FLAT"}
    log.info(f"[{symbol}] MANUAL RESET to FLAT")
    return jsonify({"status": "ok", "symbol": symbol, "state": "FLAT"}), 200


@app.route("/webhook", methods=["POST"])
def webhook():
    log.info("=" * 60)
    log.info("WEBHOOK RECEIVED")
    log.info(f"  headers: {dict(request.headers)}")
    log.info(f"  raw body: {request.data.decode('utf-8', errors='replace')[:1000]}")

    try:
        body = request.get_json(force=True)
        if isinstance(body, str):
            body = json.loads(body)
    except Exception as e:
        log.error(f"  Parse error: {e}")
        return jsonify({"error": "invalid JSON"}), 400

    log.info(f"  parsed: {json.dumps(body)}")

    action      = str(body.get("action", "")).upper()
    qc_symbol   = str(body.get("symbol", "")).upper().strip().split()[0]  # strip QC SID e.g. "DLXY YTYSIFTLVN8L" → "DLXY"
    quantity    = int(body.get("quantity", 0))
    price       = float(body.get("price", 0))
    cycle       = int(body.get("cycle", 1))  # trade cycle counter — used to lift BLOCKED on re-entry

    if not qc_symbol:
        return jsonify({"error": "missing symbol"}), 400

    # ── Resolve ticker: static alias map → Polygon live lookup ─────────────
    symbol = resolve_symbol(qc_symbol)
    if symbol != qc_symbol:
        log.info(f"[{qc_symbol}→{symbol}] Ticker resolved")

    s             = get_state(symbol)
    current_state = s.get("state", "FLAT")

    log.info(f"[{symbol}] action={action} | state={current_state} | "
             f"qty={quantity} | price=${price}")

    # ── SHORT ─────────────────────────────────────────────────────────────────
    if action == "SHORT":
        if current_state == "BLOCKED":
            last_cycle = s.get("cycle", 0)
            if cycle > last_cycle:
                log.info(f"[{symbol}] BLOCKED lifted — new cycle {cycle} > last cycle {last_cycle} (QC started new trade)")
                set_state(symbol, state="FLAT")  # reset so it falls through to LOCATING below
                current_state = "FLAT"
            else:
                log.info(f"[{symbol}] SHORT ignored — blocked today (cycle={cycle}, last={last_cycle}): {s.get('reason', '')}")
                return jsonify({"status": "ignored", "reason": "blocked", "cycle": cycle, "last_cycle": last_cycle}), 200

        if current_state == "LOCATING":
            log.warning(f"[{symbol}] SHORT ignored — locate already in progress")
            return jsonify({"status": "ignored", "reason": "locate in progress"}), 200

        if current_state == "ACTIVE":
            log.warning(f"[{symbol}] SHORT rejected — already have active position")
            block(symbol, "duplicate SHORT received while ACTIVE")
            return jsonify({"status": "rejected", "reason": "already active"}), 200

        if quantity <= 0 or price <= 0:
            return jsonify({"error": "quantity and price required for SHORT"}), 400

        set_state(symbol, state="LOCATING", entry_price=price, quantity=quantity, cycle=cycle)
        threading.Thread(target=locate_and_short, args=(symbol, quantity, price), daemon=True).start()
        log.info(f"[{symbol}] Locate thread launched — returning 200 immediately")
        return jsonify({"status": "ok", "action": "SHORT", "symbol": symbol, "state": "LOCATING"}), 200

    # ── COVER ─────────────────────────────────────────────────────────────────
    elif action == "COVER":
        if current_state == "BLOCKED":
            log.info(f"[{symbol}] COVER ignored — blocked today: {s.get('reason', '')}")
            return jsonify({"status": "ignored", "reason": "blocked"}), 200

        if current_state == "FLAT":
            log.warning(f"[{symbol}] COVER rejected — no position to cover (FLAT)")
            block(symbol, "COVER received with no active position")
            return jsonify({"status": "rejected", "reason": "no position"}), 200

        if current_state == "LOCATING":
            log.warning(f"[{symbol}] COVER received during locate — cancelling and cleaning up")
            threading.Thread(
                target=cancel_and_cleanup,
                args=(symbol, "COVER received while still locating"),
                daemon=True
            ).start()
            return jsonify({"status": "ok", "action": "cleanup_on_cover_during_locate"}), 200

        # ACTIVE — look up actual position size from TZ
        log.info(f"[{symbol}] COVER: looking up actual position size from TZ")
        position = get_position(symbol)

        if position is None:
            log.warning(f"[{symbol}] COVER: no TZ position found — entry may not have filled")
            block(symbol, "COVER received but no TZ position found")
            return jsonify({"status": "ok", "note": "no position found — blocked"}), 200

        shares = float(position.get("shares", 0))
        if shares >= 0:
            log.warning(f"[{symbol}] COVER: position shares={shares} — not a short | blocking")
            block(symbol, f"COVER received but position is not short (shares={shares})")
            return jsonify({"status": "ok", "note": "not a short position"}), 200

        cover_qty   = abs(int(shares))
        # Use QC's signal price — QC knows the current market price, server does not
        # Add a small buffer above to ensure fill (buying to cover, so we go higher)
        cover_limit = float(price)  # QC already applied its buffer

        log.info(f"[{symbol}] Covering {cover_qty} shares | "
                 f"qc_price=${price} | limit=${cover_limit} (QC pre-buffered)")

        try:
            result = place_order("Buy", symbol, cover_qty, cover_limit, "COVER_ORDER")
            set_state(symbol, state="FLAT", reason="covered")
            log.info(f"[{symbol}] COVER PLACED | qty={cover_qty} | limit=${cover_limit} | "
                     f"clientOrderId={result.get('clientOrderId')} | "
                     f"status={result.get('orderStatus')}")
            threading.Thread(
                target=monitor_cover_fill,
                args=(symbol, result.get("clientOrderId"), COVER_FILL_TIMEOUT),
                daemon=True
            ).start()
            return jsonify({
                "status":        "ok",
                "action":        "COVER",
                "symbol":        symbol,
                "quantity":      cover_qty,
                "limit_price":   cover_limit,
                "clientOrderId": result.get("clientOrderId"),
                "orderStatus":   result.get("orderStatus"),
            }), 200
        except Exception as e:
            log.error(f"[{symbol}] Cover order failed: {e}")
            return jsonify({"error": str(e)}), 500

    # ── CANCEL ────────────────────────────────────────────────────────────────
    elif action == "CANCEL":
        log.info(f"[{symbol}] CANCEL received — cancelling orders and closing any position")
        threading.Thread(
            target=cancel_and_cleanup,
            args=(symbol, "CANCEL signal received from QC"),
            daemon=True
        ).start()
        return jsonify({"status": "ok", "action": "CANCEL", "symbol": symbol}), 200

    else:
        log.warning(f"[{symbol}] Unknown action: {action}")
        return jsonify({"error": f"unknown action: {action}"}), 400


# ══════════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        log.warning("TZ_API_KEY or TZ_API_SECRET not set — orders will fail")

    port = int(os.environ.get("PORT", 5000))

    log.info("")
    log.info("╔══════════════════════════════════════════════════════════╗")
    log.info("║  TradeZero EOD Short Webhook Server                      ║")
    log.info("║                                                          ║")
    log.info("║  POST /webhook          ← SHORT / COVER / CANCEL        ║")
    log.info("║  GET  /health           ← server + TZ API status        ║")
    log.info("║  GET  /state            ← view all symbol states        ║")
    log.info("║  POST /reset/<symbol>   ← manually reset symbol to FLAT ║")
    log.info("╚══════════════════════════════════════════════════════════╝")
    log.info(f"  Account:           {ACCOUNT_ID}")
    log.info(f"  Ticker aliases:    {TICKER_ALIASES}")
    log.info(f"  Polygon key:       {POLYGON_API_KEY[:8] + '...' if POLYGON_API_KEY else 'NOT SET — rename auto-detection disabled'}")
    log.info(f"  Twelve Data key:   {TWELVEDATA_API_KEY[:8] + '...' if TWELVEDATA_API_KEY else 'NOT SET — price check falls back to Yahoo'}")
    log.info(f"  Max locate cost:   {MAX_LOCATE_COST_PCT*100:.1f}%")
    log.info(f"  Min locate qty:    {MIN_LOCATE_QUANTITY}")
    log.info(f"  Locate timeout:    {LOCATE_POLL_TIMEOUT}s")
    log.info(f"  Price handling: QC pre-buffered — server uses received price directly")
    log.info(f"  Listening on port: {port}")
    log.info("")
    app.run(host="0.0.0.0", port=port, debug=False)
