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
LOCATE_POLL_INTERVAL = 2       # seconds between locate status polls
LOCATE_POLL_TIMEOUT  = 30      # seconds before giving up on locate
LIMIT_BUFFER         = 0.001   # 0.1% — short limit below market, cover limit above
SHORT_FILL_TIMEOUT   = 5       # minutes to wait for short entry to fill before cancelling
COVER_FILL_TIMEOUT   = 3       # minutes to wait for cover to fill before retrying aggressively
COVER_RETRY_BUFFER   = 0.005   # 0.5% above Yahoo price for aggressive cover retry

# ══════════════════════════════════════════════════════════════════════════════
# CREDENTIALS
# ══════════════════════════════════════════════════════════════════════════════
API_KEY    = (os.getenv("TZ_API_KEY")    or "").strip()
API_SECRET = (os.getenv("TZ_API_SECRET") or "").strip()

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
    set_state(symbol, state="BLOCKED", reason=reason)
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
    Pings own /health endpoint every 60 seconds.
    If 3 consecutive failures, forces gunicorn to recycle the worker
    by raising SIGTERM on the current process — gunicorn master will
    spawn a fresh worker automatically.
    """
    import signal as _signal, urllib.request as _req, os as _os
    time.sleep(30)  # wait for server to fully start before first ping

    port = int(_os.environ.get("PORT", 10000))
    url  = f"http://127.0.0.1:{port}/health"
    fails = 0

    while True:
        time.sleep(60)
        try:
            with _req.urlopen(url, timeout=10) as r:
                if r.status == 200:
                    fails = 0
                else:
                    fails += 1
                    log.warning(f"WATCHDOG: /health returned {r.status} ({fails}/3)")
        except Exception as e:
            fails += 1
            log.warning(f"WATCHDOG: /health unreachable — {e} ({fails}/3)")

        if fails >= 3:
            log.error("WATCHDOG: 3 consecutive health failures — recycling worker")
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
PRICE_SANITY_PCT = 0.10   # reject if QC price deviates >10% from Yahoo

# Ticker aliases — QC sometimes uses new tickers before TZ updates their system.
# Add entries here as needed: { "QC_TICKER": "TZ_TICKER" }
TICKER_ALIASES = {
    "FPGP": "ALTO",   # Alto Ingredients rebranded; TZ still uses ALTO
}

def get_yahoo_price(symbol):
    """
    Fetch the most relevant price from Yahoo Finance based on current market session.
    Uses postMarketPrice during AH, preMarketPrice during pre-market, regularMarketPrice during RTH.
    Returns (price, session_label) or (None, None) on failure.
    """
    import urllib.request as _req, datetime as _dt
    url = f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?interval=1m&range=1d"
    try:
        req = _req.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        })
        with _req.urlopen(req, timeout=5) as resp:
            meta = __import__('json').loads(resp.read())["chart"]["result"][0]["meta"]

        # Determine ET time (no pytz — manual DST offset)
        utc_now = _dt.datetime.now(_dt.timezone.utc)
        year = utc_now.year
        dst_start = _dt.datetime(year, 3, 1) + _dt.timedelta(
            days=(6 - _dt.datetime(year, 3, 1).weekday()) % 7 + 7)
        dst_end = _dt.datetime(year, 11, 1) + _dt.timedelta(
            days=(6 - _dt.datetime(year, 11, 1).weekday()) % 7)
        is_dst = dst_start <= utc_now.replace(tzinfo=None) < dst_end
        et_hour = (utc_now + _dt.timedelta(hours=-4 if is_dst else -5)).hour
        et_minute = (utc_now + _dt.timedelta(hours=-4 if is_dst else -5)).minute
        et_time = et_hour * 60 + et_minute  # minutes since midnight ET

        rth_open  = 9 * 60 + 30   # 9:30 AM
        rth_close = 16 * 60        # 4:00 PM
        pre_open  = 4 * 60         # 4:00 AM

        if et_time < pre_open or et_time >= 20 * 60:
            price = meta.get("regularMarketPrice")
            label = "regular(overnight)"
        elif et_time < rth_open:
            # Pre-market: only use preMarketPrice — do NOT fall back to prior close
            price = meta.get("preMarketPrice")
            label = "preMarket" if price else "regular(no-pre)"
        elif et_time < rth_close:
            price = meta.get("regularMarketPrice")
            label = "regular"
        else:
            price = meta.get("postMarketPrice")
            label = "postMarket" if price else "regular(no-post)"

        if price:
            return float(price), label
        return None, "regular(no-pre)"   # signal: no usable price for this session
    except Exception as e:
        log.warning(f"Yahoo price lookup failed for {symbol}: {e}")
        return None, None

def locate_and_short(symbol, qc_quantity, entry_price):
    log.info(f"[{symbol}] ── LOCATE THREAD STARTED ──────────────────────")
    log.info(f"[{symbol}] QC requested: qty={qc_quantity} entry_price=${entry_price}")

    # (ticker alias already applied at webhook entry point)

    # ── Step 0: Validate QC price against Yahoo Finance ───────────────────────
    log.info(f"[{symbol}] Step 0: Price sanity check via Yahoo Finance")
    yahoo_price, yahoo_session = get_yahoo_price(symbol)
    if yahoo_price is not None:
        deviation = abs(entry_price - yahoo_price) / yahoo_price
        log.info(f"[{symbol}] Price check: {yahoo_session}=${yahoo_price:.4f} | QC=${entry_price:.4f} | "
                 f"deviation={deviation*100:.2f}% (limit={PRICE_SANITY_PCT*100:.0f}%)")
        if deviation > PRICE_SANITY_PCT:
            block(symbol, f"price sanity FAILED: QC=${entry_price} vs {yahoo_session}=${yahoo_price:.4f} "
                          f"({deviation*100:.1f}% > {PRICE_SANITY_PCT*100:.0f}% limit — likely bad tick)")
            return
        log.info(f"[{symbol}] Price sanity OK ✓ ({yahoo_session})")
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

    # TZ margin requirement for short selling (as of Aug 2, 2024):
    # Stocks < $5.00: $5.00 per share flat (regardless of actual price)
    # Stocks >= $5.00: actual price per share (100% standard margin)
    if entry_price < 5.0:
        margin_per_share = 5.0
    else:
        margin_per_share = entry_price
    max_affordable = int(usable_capital / margin_per_share)
    log.info(f"[{symbol}] margin_per_share=${margin_per_share:.2f} | "
             f"max_affordable={max_affordable} "
             f"(usable=${usable_capital:,.2f} / margin=${margin_per_share:.2f}/share)")

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
        final_quantity = min(request_quantity, located_qty)
        log.info(f"[{symbol}] Step 11: Placing short | qty={final_quantity} | "
                 f"entry=${entry_price} | limit=${round(entry_price * (1 - LIMIT_BUFFER), 2)}")
        try:
            limit_price = round(entry_price * (1 - LIMIT_BUFFER), 2)
            result = place_order("Sell", symbol, final_quantity, limit_price, "SHORT_ORDER")
            set_state(symbol, state="ACTIVE", client_order_id=result.get("clientOrderId"))
            log.info(f"[{symbol}] STATE → ACTIVE")
            log.info(f"[{symbol}] SHORT PLACED (existing locate) | qty={final_quantity} | "
                     f"limit=${limit_price} | clientOrderId={result.get('clientOrderId')} | "
                     f"status={result.get('orderStatus')}")
            threading.Thread(
                target=monitor_short_fill,
                args=(symbol, result.get("clientOrderId"), SHORT_FILL_TIMEOUT),
                daemon=True
            ).start()
        except Exception as e:
            block(symbol, f"order placement failed: {e}")
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

    if get_state(symbol).get("state") != "LOCATING":
        log.warning(f"[{symbol}] State changed after accept — aborting order")
        return

    # ── Step 11: Place short limit order ──────────────────────────────────────
    limit_price = round(entry_price * (1 - LIMIT_BUFFER), 2)
    log.info(f"[{symbol}] Step 11: Placing short | qty={final_quantity} | "
             f"entry=${entry_price} | limit=${limit_price}")
    try:
        result = place_order("Sell", symbol, final_quantity, limit_price, "SHORT_ORDER")
        set_state(
            symbol,
            state="ACTIVE",
            entry_price=entry_price,
            quantity=final_quantity,
            client_order_id=result.get("clientOrderId"),
        )
        log.info(f"[{symbol}] SHORT PLACED | qty={final_quantity} | "
                 f"limit=${limit_price} | clientOrderId={result.get('clientOrderId')} | "
                 f"status={result.get('orderStatus')}")
    except Exception as e:
        block(symbol, f"order placement failed: {e}")


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

        # If state changed (e.g. COVER arrived and handled it), stop monitoring
        state = get_state(symbol).get("state")
        if state != "ACTIVE":
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
            cover_limit = round(last_price * (1 + LIMIT_BUFFER), 2)
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


@app.route("/health", methods=["GET", "HEAD"])
def health():
    url = f"{BASE_URL}/v1/api/accounts"
    try:
        r = requests.get(url, headers=tz_headers(), timeout=10)
        tz_ok     = r.status_code == 200
        tz_detail = f"http_{r.status_code}: {r.text[:200]}"
    except requests.exceptions.Timeout:
        tz_ok, tz_detail = False, "timeout"
    except Exception as e:
        tz_ok, tz_detail = False, f"error: {str(e)[:100]}"

    with state_lock:
        states = {k: v.get("state") for k, v in symbol_state.items()}

    # Check available routes
    try:
        r_routes = requests.get(
            f"{BASE_URL}/v1/api/accounts/{ACCOUNT_ID}/routes",
            headers=tz_headers(), timeout=5)
        routes_info = r_routes.text[:300]
    except Exception as e:
        routes_info = f"error: {e}"

    status = {
        "server":        "ok",
        "tz_api":        "ok" if tz_ok else "unreachable",
        "tz_detail":     tz_detail,
        "account":       ACCOUNT_ID,
        "key_preview":   API_KEY[:8] + "..." if API_KEY else "NOT SET",
        "symbol_states": states,
        "routes":        routes_info,
        "config": {
            "max_locate_cost_pct": MAX_LOCATE_COST_PCT,
            "min_locate_quantity": MIN_LOCATE_QUANTITY,
            "locate_poll_timeout": LOCATE_POLL_TIMEOUT,
            "limit_buffer":        LIMIT_BUFFER,
        },
    }
    log.info(f"HEALTH: {status}")
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

    if not qc_symbol:
        return jsonify({"error": "missing symbol"}), 400

    # Apply ticker alias at entry point so all state tracking uses TZ symbol
    symbol = TICKER_ALIASES.get(qc_symbol, qc_symbol)
    if symbol != qc_symbol:
        log.info(f"[{qc_symbol}] Ticker alias: QC={qc_symbol} → TZ={symbol}")

    s             = get_state(symbol)
    current_state = s.get("state", "FLAT")

    log.info(f"[{symbol}] action={action} | state={current_state} | "
             f"qty={quantity} | price=${price}")

    # ── SHORT ─────────────────────────────────────────────────────────────────
    if action == "SHORT":
        if current_state == "BLOCKED":
            log.info(f"[{symbol}] SHORT ignored — blocked today: {s.get('reason', '')}")
            return jsonify({"status": "ignored", "reason": "blocked"}), 200

        if current_state == "LOCATING":
            log.warning(f"[{symbol}] SHORT ignored — locate already in progress")
            return jsonify({"status": "ignored", "reason": "locate in progress"}), 200

        if current_state == "ACTIVE":
            log.warning(f"[{symbol}] SHORT rejected — already have active position")
            block(symbol, "duplicate SHORT received while ACTIVE")
            return jsonify({"status": "rejected", "reason": "already active"}), 200

        if quantity <= 0 or price <= 0:
            return jsonify({"error": "quantity and price required for SHORT"}), 400

        set_state(symbol, state="LOCATING", entry_price=price, quantity=quantity)
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
        cover_limit = round(float(price) * (1 + LIMIT_BUFFER), 2)

        log.info(f"[{symbol}] Covering {cover_qty} shares | "
                 f"qc_price=${price} | limit=${cover_limit} (buffer={LIMIT_BUFFER*100:.1f}%)")

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
    log.info(f"  Max locate cost:   {MAX_LOCATE_COST_PCT*100:.1f}%")
    log.info(f"  Min locate qty:    {MIN_LOCATE_QUANTITY}")
    log.info(f"  Locate timeout:    {LOCATE_POLL_TIMEOUT}s")
    log.info(f"  Limit buffer:      {LIMIT_BUFFER*100:.1f}%")
    log.info(f"  Listening on port: {port}")
    log.info("")
    app.run(host="0.0.0.0", port=port, debug=False)
