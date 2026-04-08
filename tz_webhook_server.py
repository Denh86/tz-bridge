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

ENDPOINTS:
  POST /webhook          ← real account: SHORT / COVER / CANCEL
  POST /sim/webhook      ← sim account:  SHORT / COVER / CANCEL
  GET  /health           ← server + TZ API status (real + sim states)
  GET  /state            ← real account symbol states
  GET  /sim/state        ← sim account symbol states
  POST /reset/<symbol>   ← manually reset real symbol to FLAT
  POST /sim/reset/<symbol> ← manually reset sim symbol to FLAT

HOW TO RUN:
  pip install flask requests python-dotenv
  .env:
    TZ_API_KEY=...
    TZ_API_SECRET=...
    TZ_SIM_API_KEY=...
    TZ_SIM_API_SECRET=...
    SIM_ACCOUNT_ID=...
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
MAX_LOCATE_COST_PCT  = 0.05    # 5%  — reject if locatePrice / entryPrice > this
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
# CREDENTIALS — REAL ACCOUNT
# ══════════════════════════════════════════════════════════════════════════════
API_KEY            = (os.getenv("TZ_API_KEY")          or "").strip()
API_SECRET         = (os.getenv("TZ_API_SECRET")       or "").strip()
TWELVEDATA_API_KEY = (os.getenv("TWELVEDATA_API_KEY")  or "").strip()

# ══════════════════════════════════════════════════════════════════════════════
# CREDENTIALS — SIM ACCOUNT
# ══════════════════════════════════════════════════════════════════════════════
SIM_API_KEY    = (os.getenv("TZ_SIM_API_KEY")   or "").strip()
SIM_API_SECRET = (os.getenv("TZ_SIM_API_SECRET") or "").strip()
SIM_ACCOUNT_ID = (os.getenv("SIM_ACCOUNT_ID")    or "").strip()

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
class _HealthCheckFilter(logging.Filter):
    def filter(self, record):
        msg = record.getMessage()
        return "GET /health" not in msg

for _gunicorn_logger in ("gunicorn.access", "gunicorn.error", "werkzeug"):
    logging.getLogger(_gunicorn_logger).addFilter(_HealthCheckFilter())

# ══════════════════════════════════════════════════════════════════════════════
# REAL ACCOUNT STATE MACHINE  (reset at midnight)
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
    last_cycle = symbol_state.get(symbol, {}).get("cycle", 0)
    set_state(symbol, state="BLOCKED", reason=reason, cycle=last_cycle)
    log.warning(f"[{symbol}] BLOCKED: {reason}")


# ══════════════════════════════════════════════════════════════════════════════
# SIM ACCOUNT STATE MACHINE  (separate dict — never collides with real states)
# ══════════════════════════════════════════════════════════════════════════════
sim_symbol_state = {}
sim_state_lock   = threading.Lock()


def sim_get_state(symbol):
    with sim_state_lock:
        return sim_symbol_state.get(symbol, {}).copy()


def sim_set_state(symbol, **kwargs):
    with sim_state_lock:
        if symbol not in sim_symbol_state:
            sim_symbol_state[symbol] = {"state": "FLAT"}
        sim_symbol_state[symbol].update(kwargs)
    log.info(f"[SIM:{symbol}] STATE → {sim_symbol_state[symbol]['state']}"
             + (f" ({kwargs.get('reason', '')})" if kwargs.get("reason") else ""))


def sim_block(symbol, reason):
    last_cycle = sim_symbol_state.get(symbol, {}).get("cycle", 0)
    sim_set_state(symbol, state="BLOCKED", reason=reason, cycle=last_cycle)
    log.warning(f"[SIM:{symbol}] BLOCKED: {reason}")


def midnight_reset_thread():
    global reset_date
    while True:
        time.sleep(30)
        today = date.today()
        if today != reset_date:
            with state_lock:
                symbol_state.clear()
            with sim_state_lock:
                sim_symbol_state.clear()
            reset_date = today
            log.info("MIDNIGHT RESET: all symbol states cleared (real + sim)")


threading.Thread(target=midnight_reset_thread, daemon=True).start()


def self_watchdog_thread():
    """
    Pings own /health endpoint every 90 seconds.
    If 5 consecutive failures, forces gunicorn to recycle the worker.
    Initial delay of 180s gives Render's port scanner time to confirm
    the port is open before the watchdog starts pinging.
    """
    import signal as _signal, urllib.request as _req, os as _os
    time.sleep(180)

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
            return


threading.Thread(target=self_watchdog_thread, daemon=True).start()


# ══════════════════════════════════════════════════════════════════════════════
# REAL ACCOUNT — TZ API HELPERS
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
# SIM ACCOUNT — TZ API HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def sim_headers():
    return {
        "TZ-API-KEY-ID":     SIM_API_KEY,
        "TZ-API-SECRET-KEY": SIM_API_SECRET,
        "Content-Type":      "application/json",
        "Accept":            "application/json",
    }


def sim_tz_get(path, label="GET"):
    url = f"{BASE_URL}{path}"
    log.info(f"  → [SIM] {label} GET {url}")
    r = requests.get(url, headers=sim_headers(), timeout=15)
    log.info(f"  ← [SIM] status: {r.status_code}  body: {r.text[:600]}")
    return r


def sim_tz_post(path, payload, label="POST"):
    url = f"{BASE_URL}{path}"
    log.info(f"  → [SIM] {label} POST {url}")
    log.info(f"    body: {json.dumps(payload)}")
    r = requests.post(url, headers=sim_headers(), json=payload, timeout=15)
    log.info(f"  ← [SIM] status: {r.status_code}  body: {r.text[:600]}")
    return r


def sim_tz_delete(path, label="DELETE"):
    url = f"{BASE_URL}{path}"
    log.info(f"  → [SIM] {label} DELETE {url}")
    r = requests.delete(url, headers=sim_headers(), timeout=15)
    log.info(f"  ← [SIM] status: {r.status_code}  body: {r.text[:200]}")
    return r


# ══════════════════════════════════════════════════════════════════════════════
# REAL ACCOUNT — ACCOUNT / POSITION / ORDER HELPERS
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
    side_str = "Sell" if side.lower() == "sell" else "Buy"
    payload = {
        "clientOrderId": client_id,
        "symbol":        symbol,
        "orderQuantity": int(quantity),
        "side":          side_str,
        "orderType":     "Limit",
        "securityType":  "Stock",
        "limitPrice":    round(limit_price, 2),
        "timeInForce":   "Day_Plus",
        "route":         "SMART",
    }
    r = tz_post(f"/v1/api/accounts/{ACCOUNT_ID}/order", payload, label)
    if not r.ok:
        log.error(f"  [{symbol}] Order FAILED {r.status_code} — raw body: {r.text[:500]}")
        r.raise_for_status()
    data = r.json()
    log.info(f"  [{symbol}] Order placed: clientOrderId={data.get('clientOrderId')} "
             f"status={data.get('orderStatus')}")
    return data


# ══════════════════════════════════════════════════════════════════════════════
# SIM ACCOUNT — ACCOUNT / POSITION / ORDER HELPERS
# ══════════════════════════════════════════════════════════════════════════════
def sim_get_account_details():
    r = sim_tz_get(f"/v1/api/account/{SIM_ACCOUNT_ID}", "SIM_ACCOUNT")
    if r.status_code == 200:
        return r.json()
    log.error(f"  [SIM] Failed to get account details: {r.status_code}")
    return None


def sim_get_position(symbol):
    r = sim_tz_get(f"/v1/api/accounts/{SIM_ACCOUNT_ID}/positions", "SIM_POSITIONS")
    if r.status_code != 200:
        log.error(f"  [SIM] Failed to get positions: {r.status_code}")
        return None
    positions = r.json()
    if not isinstance(positions, list):
        positions = positions.get("positions", [])
    for p in positions:
        if p.get("symbol", "").upper() == symbol.upper():
            log.info(f"  [SIM] Found position for {symbol}: {json.dumps(p)}")
            return p
    log.info(f"  [SIM] No open position found for {symbol}")
    return None


def sim_cancel_all_open_orders(symbol):
    r = sim_tz_get(f"/v1/api/accounts/{SIM_ACCOUNT_ID}/orders", "SIM_ORDERS")
    if r.status_code != 200:
        log.error(f"  [SIM] Failed to fetch orders: {r.status_code}")
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
    log.info(f"  [SIM] Found {len(open_orders)} open order(s) for {symbol}")
    cancelled = 0
    for o in open_orders:
        oid = o.get("clientOrderId")
        if oid:
            rc = sim_tz_delete(
                f"/v1/api/accounts/{SIM_ACCOUNT_ID}/orders/{oid}", "SIM_CANCEL_ORDER"
            )
            if rc.status_code in (200, 204):
                cancelled += 1
            else:
                log.warning(f"  [SIM] Failed to cancel {oid}: {rc.status_code}")
    return cancelled


def sim_place_order(side, symbol, quantity, limit_price, label="SIM_ORDER"):
    client_id = f"SIM_{side[:1].upper()}_{uuid.uuid4().hex[:8].upper()}"
    side_str  = "Sell" if side.lower() == "sell" else "Buy"
    payload = {
        "clientOrderId": client_id,
        "symbol":        symbol,
        "orderQuantity": int(quantity),
        "side":          side_str,
        "orderType":     "Limit",
        "securityType":  "Stock",
        "limitPrice":    round(limit_price, 2),
        "timeInForce":   "Day_Plus",
        "route":         "SMART",
    }
    r = sim_tz_post(f"/v1/api/accounts/{SIM_ACCOUNT_ID}/order", payload, label)
    if not r.ok:
        log.error(f"  [SIM:{symbol}] Order FAILED {r.status_code} — body: {r.text[:500]}")
        r.raise_for_status()
    data = r.json()
    log.info(f"  [SIM:{symbol}] Order placed: clientOrderId={data.get('clientOrderId')} "
             f"status={data.get('orderStatus')}")
    return data


# ══════════════════════════════════════════════════════════════════════════════
# REAL ACCOUNT — GRACEFUL ORDER STEPDOWN
# ══════════════════════════════════════════════════════════════════════════════
def place_order_with_stepdown(symbol, initial_quantity, limit_price, label="SHORT_ORDER"):
    """
    Attempt to place a short order at fixed percentage tiers of the located
    quantity. Steps: 100% → 80% → 60% → 40% → 20%.
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
# SIM ACCOUNT — GRACEFUL ORDER STEPDOWN
# ══════════════════════════════════════════════════════════════════════════════
def sim_place_order_with_stepdown(symbol, initial_quantity, limit_price, label="SIM_SHORT_ORDER"):
    """Same 100%→80%→60%→40%→20% stepdown as real account."""
    tiers    = [1.0, 0.8, 0.6, 0.4, 0.2]
    last_exc = None
    for attempt, pct in enumerate(tiers, 1):
        qty = max(1, int(initial_quantity * pct))
        try:
            result = sim_place_order("Sell", symbol, qty, limit_price, label)
            if attempt > 1:
                log.info(f"[SIM:{symbol}] Order accepted attempt {attempt} "
                         f"at {int(pct*100)}%: {qty}sh")
            return result
        except Exception as e:
            last_exc = e
            if attempt < len(tiers):
                log.warning(f"[SIM:{symbol}] Rejected at {qty}sh ({int(pct*100)}%): "
                            f"{e} — stepping down")
            else:
                log.warning(f"[SIM:{symbol}] Rejected at {qty}sh (20% min): {e} — giving up")
    raise last_exc or Exception(f"[SIM:{symbol}] All stepdown tiers failed")


# ══════════════════════════════════════════════════════════════════════════════
# LOCATE HELPERS  (real account — used by both real flow and phantom locate)
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
    su_id = f"{quote_req_id}.SU"
    deadline = time.time() + LOCATE_POLL_TIMEOUT
    while time.time() < deadline:
        url = f"{BASE_URL}/v1/api/accounts/{ACCOUNT_ID}/locates/history"
        log.info(f"  → LOCATE_POLL GET {url}")
        r = requests.get(url, headers=tz_headers(), timeout=15)
        log.info(f"  ← status: {r.status_code}  body: {r.text}")
        if r.status_code == 200:
            history = r.json().get("locateHistory", [])
            log.info(f"  [{symbol}] {len(history)} history entries — "
                     f"searching for {quote_req_id} or {su_id}")

            primary_item = None
            su_item      = None
            for item in history:
                qid = item.get("quoteReqID")
                if qid == quote_req_id:
                    primary_item = item
                elif qid == su_id:
                    su_item = item

            for label, item in (("PRIMARY", primary_item), ("SU", su_item)):
                if item:
                    log.info(f"  [{symbol}] {label} ({item.get('quoteReqID')}): "
                             f"status={item.get('locateStatus')} | "
                             f"shares={item.get('locateShares')} | "
                             f"price=${item.get('locatePrice')} | "
                             f"type={item.get('locateType')} | "
                             f"error={item.get('locateError')} | "
                             f"text={item.get('text', '')}")

            if primary_item and primary_item.get("locateStatus") in (65, 56, 67, 52):
                return primary_item

            primary_unavailable = (
                primary_item is not None
                and primary_item.get("locateError") == 11
            )
            if primary_unavailable and su_item:
                su_status = su_item.get("locateStatus")
                if su_status == 65:
                    log.info(f"  [{symbol}] Primary unavailable — using SU locate "
                             f"({su_id}) offered at ${su_item.get('locatePrice')}/share")
                    return su_item
                elif su_status in (56, 67, 52):
                    log.info(f"  [{symbol}] SU locate also rejected/expired/canceled "
                             f"(status={su_status}) — giving up")
                    return su_item

            log.info(f"  [{symbol}] {quote_req_id} not yet actionable — polling again")
        else:
            log.warning(f"  [{symbol}] Locate poll failed: {r.status_code}")
        time.sleep(LOCATE_POLL_INTERVAL)
    log.warning(f"  [{symbol}] Locate poll timed out after {LOCATE_POLL_TIMEOUT}s")
    return None


def accept_locate(quote_req_id):
    payload = {"accountId": ACCOUNT_ID, "quoteReqID": quote_req_id}
    return tz_post("/v1/api/accounts/locates/accept", payload, "LOCATE_ACCEPT")


# ══════════════════════════════════════════════════════════════════════════════
# REAL ACCOUNT — LOCATE + SHORT BACKGROUND THREAD
# ══════════════════════════════════════════════════════════════════════════════
def check_existing_locate(symbol, required_quantity):
    """
    Check the inventory endpoint for currently available locate shares.
    Returns (True, available_qty) if usable, (False, 0) otherwise.
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
# PRICE SANITY  (Yahoo Finance / Twelve Data)
# ══════════════════════════════════════════════════════════════════════════════
PRICE_SANITY_PCT = 0.15

TICKER_ALIASES = {
    "FPGP": "ALTO",
    "AGE":  "SER",
}

POLYGON_API_KEY = (os.getenv("POLYGON_API_KEY") or "").strip()

_polygon_rename_cache = {}
_polygon_cache_lock   = threading.Lock()


def resolve_symbol(qc_symbol):
    if qc_symbol in TICKER_ALIASES:
        resolved = TICKER_ALIASES[qc_symbol]
        log.info(f"[{qc_symbol}] Alias map → {resolved}")
        return resolved

    with _polygon_cache_lock:
        if qc_symbol in _polygon_rename_cache:
            resolved = _polygon_rename_cache[qc_symbol]
            log.info(f"[{qc_symbol}] Runtime cache → {resolved}")
            return resolved

    if not POLYGON_API_KEY:
        log.info(f"[{qc_symbol}] POLYGON_API_KEY not set — skipping rename check")
        return qc_symbol

    try:
        base = "https://api.polygon.io/v3/reference/tickers"

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
    import urllib.request as _req, datetime as _dt, json as _json

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
            price = meta.get("regularMarketPrice")
            label = "yahoo_v8(regular)" if price else None
        else:
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

            if price is None:
                price = meta.get("regularMarketPrice")
                label = "yahoo_v8(lastClose)" if price else None
                if price:
                    log.warning(f"Yahoo: no session bars found within {lookback_s//3600}h — "
                                f"using lastClose ${price:.4f} (may be stale)")
                total_bars   = len(closes)
                last_ts      = timestamps[-1] if timestamps else 0
                last_ts_age  = int(now_ts - last_ts)
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
    import datetime as _dt
    now_et = _dt.datetime.now(_dt.timezone.utc).astimezone(
        _dt.timezone(_dt.timedelta(hours=-4)))
    t = now_et.time()

    rth_start = _dt.time(9, 30)
    rth_end   = _dt.time(16, 0)

    if rth_start <= t < rth_end:
        price, label = _get_twelvedata_price(symbol)
        if price:
            return price, label
        log.info(f"[{symbol}] Twelve Data unavailable — trying Yahoo fallback")
        return _get_yahoo_price(symbol)
    else:
        log.info(f"[{symbol}] Outside RTH ({t.strftime('%H:%M')} ET) — using Yahoo session price directly")
        return _get_yahoo_price(symbol)


def get_yahoo_price(symbol):
    return get_live_price(symbol)


def locate_and_short(symbol, qc_quantity, entry_price):
    log.info(f"[{symbol}] ── LOCATE THREAD STARTED ──────────────────────")
    log.info(f"[{symbol}] QC requested: qty={qc_quantity} entry_price=${entry_price}")

    # ── Step 0: Price sanity check ─────────────────────────────────────────
    log.info(f"[{symbol}] Step 0: Price sanity check (QC=${entry_price:.4f})")
    live_price, live_session = get_live_price(symbol)
    if live_price is not None:
        deviation = abs(entry_price - live_price) / live_price
        log.info(f"[{symbol}] Price check: {live_session}=${live_price:.4f} | QC=${entry_price:.4f} | "
                 f"deviation={deviation*100:.2f}% (limit={PRICE_SANITY_PCT*100:.0f}%)")

        if live_session == "yahoo_v8(lastClose)":
            log.info(f"[{symbol}] Price sanity SKIPPED — {live_session} is yesterday's close")
        elif deviation > PRICE_SANITY_PCT:
            block(symbol, f"price sanity FAILED: QC=${entry_price} vs {live_session}=${live_price:.4f} "
                          f"({deviation*100:.1f}% > {PRICE_SANITY_PCT*100:.0f}% limit)")
            return
        else:
            log.info(f"[{symbol}] Price sanity OK ✓")
    else:
        log.warning(f"[{symbol}] All price sources exhausted — proceeding unvalidated")

    # ── Step 1: Check buying power ─────────────────────────────────────────
    log.info(f"[{symbol}] Step 1: Checking buying power and margin")
    account = get_account_details()
    if account is None:
        block(symbol, "failed to retrieve account details")
        return

    available_cash   = float(account.get("buyingPower",     account.get("availableCash", 0)))
    margin_available = float(account.get("marginAvailable", available_cash))
    log.info(f"[{symbol}] buyingPower=${available_cash:,.2f} | marginAvailable=${margin_available:,.2f}")

    usable_capital = min(available_cash, margin_available)

    HIGH_RISK_BUFFER = 1.25

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
        elif entry_price < 5.00:
            margin_per_share = entry_price / 6.0
        else:
            margin_per_share = (entry_price / 6.0) * HIGH_RISK_BUFFER
    elif in_ah:
        if entry_price < 2.50:
            margin_per_share = 5.00
        elif entry_price < 5.00:
            margin_per_share = 5.00
        else:
            margin_per_share = (entry_price / 2.0) * HIGH_RISK_BUFFER
    else:
        margin_per_share = max(5.0, (entry_price / 2.0) * HIGH_RISK_BUFFER)

    max_affordable = int(usable_capital / margin_per_share)
    session_label = 'pre/RTH' if in_premarket_or_rth else 'AH' if in_ah else 'closed'
    buffer_note   = f" (incl {int((HIGH_RISK_BUFFER-1)*100)}% safety buffer)" if entry_price >= 5.0 else ""
    log.info(f"[{symbol}] session={session_label} | "
             f"margin_per_share=${margin_per_share:.4f}{buffer_note} | "
             f"max_affordable={max_affordable}")

    if max_affordable < MIN_LOCATE_QUANTITY:
        block(symbol, f"cannot afford minimum {MIN_LOCATE_QUANTITY} shares — "
                      f"max_affordable={max_affordable} at ${entry_price}")
        return

    raw_quantity = min(qc_quantity, max_affordable)
    request_quantity = max(MIN_LOCATE_QUANTITY, int(raw_quantity / 100) * 100)
    if request_quantity > max_affordable:
        request_quantity = int(max_affordable / 100) * 100
    if request_quantity < MIN_LOCATE_QUANTITY:
        block(symbol, f"cannot afford minimum {MIN_LOCATE_QUANTITY} shares after rounding")
        return
    log.info(f"[{symbol}] request_quantity={request_quantity} "
             f"(floored to 100s | qc={qc_quantity}, raw={raw_quantity}, max_affordable={max_affordable})")

    # ── Step 3b: Check existing locate ────────────────────────────────────
    log.info(f"[{symbol}] Step 3b: Checking for existing locate today")
    has_locate, located_qty = check_existing_locate(symbol, request_quantity)
    if has_locate:
        log.info(f"[{symbol}] Skipping locate request — using existing locate "
                 f"({located_qty} shares already accepted today)")

        affordable_qty = min(located_qty, max_affordable)
        if affordable_qty < MIN_SHORT_QUANTITY:
            block(symbol, f"existing locate ({located_qty}sh) but cannot afford "
                          f"even 1sh at ${entry_price}")
            return
        if affordable_qty < located_qty:
            log.info(f"[{symbol}] Capping existing locate from {located_qty}sh to "
                     f"{affordable_qty}sh — max_affordable={max_affordable}")
        final_quantity = affordable_qty

        log.info(f"[{symbol}] Step 11: Placing short | qty={final_quantity} | "
                 f"limit=${entry_price} (QC pre-buffered)")
        try:
            limit_price = entry_price
            result = place_order_with_stepdown(symbol, final_quantity, limit_price, "SHORT_ORDER")
            placed_qty = result.get("orderQuantity", final_quantity)
            set_state(symbol, state="ACTIVE", client_order_id=result.get("clientOrderId"))
            log.info(f"[{symbol}] STATE → ACTIVE")
            log.info(f"[{symbol}] SHORT PLACED (existing locate) | qty={placed_qty} | "
                     f"limit=${limit_price} | clientOrderId={result.get('clientOrderId')}")
            threading.Thread(
                target=monitor_short_fill,
                args=(symbol, result.get("clientOrderId"), SHORT_FILL_TIMEOUT),
                daemon=True
            ).start()
        except Exception as e:
            block(symbol, f"order placement failed after all stepdown attempts: {e}")
        return

    # ── Step 4: Request locate quote ───────────────────────────────────────
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

    # ── Step 5: Poll locate status ─────────────────────────────────────────
    log.info(f"[{symbol}] Step 5: Polling locate status (max {LOCATE_POLL_TIMEOUT}s)")

    if get_state(symbol).get("state") != "LOCATING":
        log.warning(f"[{symbol}] State changed during locate request — aborting")
        return

    locate = poll_locate_status(symbol, quote_req_id)

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

    final_quantity = min(request_quantity, offered_quantity) if offered_quantity > 0 else request_quantity
    log.info(f"[{symbol}] final_quantity={final_quantity}")

    if final_quantity < MIN_LOCATE_QUANTITY:
        block(symbol, f"final quantity {final_quantity} below minimum {MIN_LOCATE_QUANTITY}")
        return

    if entry_price > 0 and locate_price > 0:
        locate_cost_pct   = locate_price / entry_price
        total_locate_cost = locate_price * final_quantity
        log.info(f"[{symbol}] Locate cost: ${locate_price}/share x {final_quantity} = "
                 f"${total_locate_cost:.2f} total | "
                 f"{locate_cost_pct*100:.3f}% of entry ${entry_price}")
        if locate_cost_pct > MAX_LOCATE_COST_PCT:
            block(symbol, f"locate too expensive: {locate_cost_pct*100:.3f}% > "
                          f"{MAX_LOCATE_COST_PCT*100:.1f}% threshold")
            return
    else:
        log.warning(f"[{symbol}] Cannot calculate locate cost — proceeding")

    accept_id = locate.get("quoteReqID", quote_req_id)
    if accept_id != quote_req_id:
        log.info(f"[{symbol}] Step 10: Accepting SU locate quoteReqID={accept_id}")
    else:
        log.info(f"[{symbol}] Step 10: Accepting locate quoteReqID={accept_id}")
    r_accept = accept_locate(accept_id)
    if r_accept.status_code not in (200, 201, 202):
        block(symbol, f"locate accept failed: {r_accept.status_code} | {r_accept.text[:200]}")
        return
    log.info(f"[{symbol}] Locate accepted successfully")

    # ── Poll inventory until locate is registered (fixes R43) ─────────────
    log.info(f"[{symbol}] Step 10b: Polling inventory to confirm locate registered...")
    inventory_confirmed = False
    poll_deadline = time.time() + 15
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

    # ── Step 11: Place short limit order ───────────────────────────────────
    limit_price = entry_price
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
                 f"limit=${limit_price} | clientOrderId={result.get('clientOrderId')}")
        threading.Thread(
            target=monitor_short_fill,
            args=(symbol, result.get("clientOrderId"), SHORT_FILL_TIMEOUT),
            daemon=True
        ).start()
    except Exception as e:
        block(symbol, f"order placement failed after all stepdown attempts: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# SIM ACCOUNT — PHANTOM LOCATE + SIM SHORT
# ══════════════════════════════════════════════════════════════════════════════
def locate_and_short_sim(symbol, qc_quantity, entry_price):
    """
    Sim-mode equivalent of locate_and_short().

    Phantom locate flow (using REAL account credentials):
      ┌─ Request locate quote on real account
      ├─ Poll until offered / rejected / timeout
      │    Offered  → record cost, log it, DO NOT accept (expires at status 67)
      │    Rejected → block in sim state (realistic — TZ genuinely can't locate)
      │    Timeout  → log warning, proceed anyway (don't punish sim for API lag)
      └─ Place short order on SIM account (no locate required in sim)

    The phantom locate is NEVER accepted so no inventory is consumed on the real
    account and no locate fees are charged. The quote expires automatically.
    """
    log.info(f"[SIM:{symbol}] ── SIM LOCATE THREAD STARTED ─────────────────")
    log.info(f"[SIM:{symbol}] QC requested: qty={qc_quantity} entry_price=${entry_price}")

    # ── Step 0: Price sanity check ─────────────────────────────────────────
    log.info(f"[SIM:{symbol}] Step 0: Price sanity check (QC=${entry_price:.4f})")
    live_price, live_session = get_live_price(symbol)
    if live_price is not None:
        deviation = abs(entry_price - live_price) / live_price
        log.info(f"[SIM:{symbol}] Price check: {live_session}=${live_price:.4f} | "
                 f"QC=${entry_price:.4f} | deviation={deviation*100:.2f}%")
        if live_session == "yahoo_v8(lastClose)":
            log.info(f"[SIM:{symbol}] Price sanity SKIPPED — lastClose is stale reference")
        elif deviation > PRICE_SANITY_PCT:
            sim_block(symbol, f"price sanity FAILED: QC=${entry_price} vs "
                              f"{live_session}=${live_price:.4f} ({deviation*100:.1f}%)")
            return
        else:
            log.info(f"[SIM:{symbol}] Price sanity OK ✓")
    else:
        log.warning(f"[SIM:{symbol}] All price sources exhausted — proceeding unvalidated")

    # ── Step 1: Buying power check against SIM account ─────────────────────
    log.info(f"[SIM:{symbol}] Step 1: Checking SIM account buying power")
    account = sim_get_account_details()
    if account is None:
        sim_block(symbol, "failed to retrieve SIM account details")
        return

    available_cash   = float(account.get("buyingPower", account.get("availableCash", 0)))
    margin_available = float(account.get("marginAvailable", available_cash))
    log.info(f"[SIM:{symbol}] SIM buyingPower=${available_cash:,.2f} | "
             f"marginAvailable=${margin_available:,.2f}")
    usable_capital = min(available_cash, margin_available)

    from datetime import datetime as _datetime, time as _time
    now_et = _datetime.now(ZoneInfo("America/New_York"))
    et_time = now_et.time()
    in_premarket_or_rth = _time(4, 0)  <= et_time < _time(16, 0)
    in_ah               = _time(16, 0) <= et_time < _time(20, 0)

    HIGH_RISK_BUFFER = 1.25
    if in_premarket_or_rth:
        if entry_price < 2.50:    margin_per_share = 2.50
        elif entry_price < 5.00:  margin_per_share = entry_price / 6.0
        else:                     margin_per_share = (entry_price / 6.0) * HIGH_RISK_BUFFER
    elif in_ah:
        if entry_price < 5.00:    margin_per_share = 5.00
        else:                     margin_per_share = (entry_price / 2.0) * HIGH_RISK_BUFFER
    else:
        margin_per_share = max(5.0, (entry_price / 2.0) * HIGH_RISK_BUFFER)

    max_affordable = int(usable_capital / margin_per_share)
    log.info(f"[SIM:{symbol}] max_affordable={max_affordable} "
             f"(usable=${usable_capital:,.2f} / margin=${margin_per_share:.4f}/share)")

    if max_affordable < MIN_LOCATE_QUANTITY:
        sim_block(symbol, f"SIM cannot afford minimum {MIN_LOCATE_QUANTITY} shares "
                          f"(max_affordable={max_affordable} at ${entry_price})")
        return

    raw_quantity     = min(qc_quantity, max_affordable)
    request_quantity = max(MIN_LOCATE_QUANTITY, int(raw_quantity / 100) * 100)
    if request_quantity > max_affordable:
        request_quantity = int(max_affordable / 100) * 100
    if request_quantity < MIN_LOCATE_QUANTITY:
        sim_block(symbol, f"SIM cannot afford minimum {MIN_LOCATE_QUANTITY} shares after rounding")
        return

    log.info(f"[SIM:{symbol}] request_quantity={request_quantity} "
             f"(qc={qc_quantity} raw={raw_quantity} max_affordable={max_affordable})")

    # ── Step 2: Phantom locate on REAL account ─────────────────────────────
    locate_cost_per_share = 0.0
    total_phantom_cost    = 0.0
    locate_check_ok       = False

    quote_req_id = f"SIM{int(time.time() * 1000)}"
    log.info(f"[SIM:{symbol}] Step 2: Phantom locate on REAL account | "
             f"quoteReqID={quote_req_id} (will NOT be accepted)")
    try:
        r = request_locate_quote(symbol, request_quantity, quote_req_id)

        if r.status_code not in (200, 201, 202):
            log.warning(f"[SIM:{symbol}] Phantom locate request failed: "
                        f"{r.status_code} | {r.text[:200]} — proceeding without cost data")
        else:
            locate = poll_locate_status(symbol, quote_req_id)

            if locate is None:
                log.warning(f"[SIM:{symbol}] Phantom locate timed out — "
                            f"proceeding without cost data")

            elif locate.get("locateStatus") == 56:
                reason = locate.get("text", "no reason given")
                log.warning(f"[SIM:{symbol}] Phantom locate REJECTED by TZ: {reason} — "
                            f"logging only, proceeding with sim order (cost data: $0.00)")

            elif locate.get("locateStatus") in (67, 52):
                log.warning(f"[SIM:{symbol}] Phantom locate expired/cancelled — "
                            f"proceeding without cost data")

            elif locate.get("locateStatus") == 65:
                # Offered — record cost, DO NOT accept
                locate_cost_per_share = float(locate.get("locatePrice", 0))
                offered_qty           = int(locate.get("locateShares", 0))
                used_qty              = min(request_quantity, offered_qty) if offered_qty > 0 else request_quantity
                total_phantom_cost    = locate_cost_per_share * used_qty
                cost_pct              = (locate_cost_per_share / entry_price * 100) if entry_price > 0 else 0
                locate_check_ok       = True

                log.info(
                    f"\n[SIM:{symbol}] ╔══ PHANTOM LOCATE RESULT ═════════════════════╗\n"
                    f"[SIM:{symbol}] ║  Offered qty :  {offered_qty} shares\n"
                    f"[SIM:{symbol}] ║  Cost/share  :  ${locate_cost_per_share:.4f}  ({cost_pct:.3f}% of entry)\n"
                    f"[SIM:{symbol}] ║  Total cost  :  ${total_phantom_cost:.2f}  ({used_qty}sh)\n"
                    f"[SIM:{symbol}] ║  Threshold   :  {MAX_LOCATE_COST_PCT*100:.1f}%  "
                    f"→ {'✓ WITHIN' if cost_pct/100 <= MAX_LOCATE_COST_PCT else '✗ WOULD BLOCK'}\n"
                    f"[SIM:{symbol}] ║  Action      :  NOT accepted (sim mode — expires naturally)\n"
                    f"[SIM:{symbol}] ╚══════════════════════════════════════════════════╝"
                )

                if entry_price > 0 and locate_cost_per_share > 0:
                    if locate_cost_per_share / entry_price > MAX_LOCATE_COST_PCT:
                        sim_block(
                            symbol,
                            f"phantom locate too expensive: "
                            f"{locate_cost_per_share / entry_price * 100:.3f}% > "
                            f"{MAX_LOCATE_COST_PCT * 100:.1f}% threshold"
                        )
                        return
            else:
                log.warning(f"[SIM:{symbol}] Unexpected phantom locate status: "
                            f"{locate.get('locateStatus')} — proceeding")

    except Exception as e:
        log.warning(f"[SIM:{symbol}] Phantom locate exception: {e} — proceeding without cost data")

    if not locate_check_ok:
        log.info(f"[SIM:{symbol}] Locate check inconclusive — proceeding with sim order "
                 f"(phantom cost logged as $0.00)")

    if sim_get_state(symbol).get("state") != "LOCATING":
        log.warning(f"[SIM:{symbol}] State changed during phantom locate — aborting")
        return

    # ── Step 3: Place short on SIM account ────────────────────────────────
    limit_price    = entry_price
    final_quantity = request_quantity
    log.info(f"[SIM:{symbol}] Step 3: Placing SHORT on SIM account | "
             f"qty={final_quantity} | limit=${limit_price}")
    try:
        result     = sim_place_order_with_stepdown(symbol, final_quantity, limit_price)
        placed_qty = result.get("orderQuantity", final_quantity)
        sim_set_state(
            symbol,
            state="ACTIVE",
            entry_price=entry_price,
            quantity=placed_qty,
            client_order_id=result.get("clientOrderId"),
            phantom_locate_cost=total_phantom_cost,
            phantom_locate_cost_per_share=locate_cost_per_share,
        )
        log.info(f"[SIM:{symbol}] SIM SHORT PLACED | qty={placed_qty} | limit=${limit_price} | "
                 f"clientOrderId={result.get('clientOrderId')} | "
                 f"status={result.get('orderStatus')} | "
                 f"phantom_locate_cost=${total_phantom_cost:.2f}")
        threading.Thread(
            target=sim_monitor_short_fill,
            args=(symbol, result.get("clientOrderId"), SHORT_FILL_TIMEOUT),
            daemon=True,
        ).start()
    except Exception as e:
        sim_block(symbol, f"SIM order placement failed after all stepdown attempts: {e}")


# ══════════════════════════════════════════════════════════════════════════════
# REAL ACCOUNT — CANCEL + CLEANUP HELPER
# ══════════════════════════════════════════════════════════════════════════════
def monitor_short_fill(symbol, client_order_id, timeout_minutes):
    import time as _time
    deadline = _time.time() + timeout_minutes * 60
    poll_interval = 15

    log.info(f"[{symbol}] SHORT MONITOR started | order={client_order_id} | timeout={timeout_minutes}min")

    while _time.time() < deadline:
        _time.sleep(poll_interval)

        state = get_state(symbol).get("state")
        if state != "ACTIVE":
            if state == "BLOCKED":
                log.warning(f"[{symbol}] SHORT MONITOR: state=BLOCKED — cancelling orphaned order {client_order_id}")
                try:
                    cancelled = cancel_all_open_orders(symbol)
                    log.info(f"[{symbol}] SHORT MONITOR: cancelled {cancelled} open order(s) after BLOCKED state")
                    position = get_position(symbol)
                    if position and float(position.get("shares", 0)) < 0:
                        log.warning(f"[{symbol}] SHORT MONITOR: order filled BEFORE cancel — short position exists! "
                                    f"Manual action required or wait for COVER webhook.")
                except Exception as e:
                    log.error(f"[{symbol}] SHORT MONITOR: error cancelling order after BLOCKED: {e}")
            else:
                log.info(f"[{symbol}] SHORT MONITOR: state={state} — stopping monitor")
            return

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

                    if "R35" in reject_text or "Buying Power" in reject_text:
                        s = get_state(symbol)
                        prev_qty  = int(s.get("quantity", 0))
                        lp        = float(s.get("entry_price", 0))
                        if prev_qty > 0 and lp > 0:
                            next_qty = max(1, int(prev_qty * 0.8))
                            log.warning(f"[{symbol}] R35 — stepping down from {prev_qty}sh to {next_qty}sh")
                            set_state(symbol, state="LOCATING")
                            try:
                                result = place_order_with_stepdown(symbol, next_qty, lp, "SHORT_ORDER_RETRY")
                                placed_qty = result.get("orderQuantity", next_qty)
                                set_state(symbol, state="ACTIVE",
                                          entry_price=lp, quantity=placed_qty,
                                          client_order_id=result.get("clientOrderId"))
                                log.info(f"[{symbol}] SHORT RETRY PLACED | qty={placed_qty}")
                                import threading as _threading
                                _threading.Thread(
                                    target=monitor_short_fill,
                                    args=(symbol, result.get("clientOrderId"), timeout_minutes),
                                    daemon=True
                                ).start()
                            except Exception as e:
                                block(symbol, f"short order R35 retry failed: {e}")
                            return
                    block(symbol, f"short order {status}: {reject_text}")
                    return
        except Exception as e:
            log.warning(f"[{symbol}] SHORT MONITOR: poll error — {e}")

    log.warning(f"[{symbol}] SHORT MONITOR: timeout after {timeout_minutes}min — cancelling and blocking")
    cancel_and_cleanup(symbol, f"short entry did not fill within {timeout_minutes} minutes")


def monitor_cover_fill(symbol, client_order_id, timeout_minutes):
    import time as _time
    deadline = _time.time() + timeout_minutes * 60
    poll_interval = 15

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

    log.warning(f"[{symbol}] COVER MONITOR: cover did not fill — cancelling and retrying aggressively")
    cancel_all_open_orders(symbol)

    position = get_position(symbol)
    if not position or float(position.get("shares", 0)) >= 0:
        log.info(f"[{symbol}] COVER MONITOR: no short position remaining — marking FLAT")
        set_state(symbol, state="FLAT", reason="cover monitor: no position found on retry")
        return

    yahoo_price, yahoo_session = get_yahoo_price(symbol)
    if yahoo_price:
        aggressive_price = round(yahoo_price * (1 + COVER_RETRY_BUFFER), 2)
        log.info(f"[{symbol}] COVER MONITOR: aggressive retry at ${aggressive_price}")
    else:
        priceClose = float(position.get("priceClose") or 0)
        if priceClose > 0:
            aggressive_price = round(priceClose * (1 + COVER_RETRY_BUFFER), 2)
            log.warning(f"[{symbol}] COVER MONITOR: Yahoo unavailable — using priceClose fallback ${aggressive_price}")
        else:
            log.error(f"[{symbol}] COVER MONITOR: no price source — MANUAL ACTION REQUIRED")
            block(symbol, "aggressive cover failed: no price source available — MANUAL ACTION REQUIRED")
            return

    cover_qty = abs(int(float(position.get("shares", 0))))
    try:
        result = place_order("Buy", symbol, cover_qty, aggressive_price, "COVER_AGGRESSIVE")
        log.info(f"[{symbol}] COVER MONITOR: aggressive cover placed | "
                 f"clientOrderId={result.get('clientOrderId')}")
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
            last_price = float(position.get("priceClose") or 0)
            if last_price <= 0:
                yahoo_price, yahoo_session = get_yahoo_price(symbol)
                if yahoo_price:
                    last_price = yahoo_price
                    log.info(f"[{symbol}] priceClose=0, using Yahoo ${last_price} ({yahoo_session})")
                else:
                    log.error(f"[{symbol}] Cannot determine cover price — MANUAL ACTION REQUIRED")
                    block(symbol, "cleanup: no priceClose and Yahoo unavailable — MANUAL ACTION REQUIRED")
                    return
            cover_limit = round(last_price * 1.005, 2)
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
# SIM ACCOUNT — MONITOR + CLEANUP FUNCTIONS
# ══════════════════════════════════════════════════════════════════════════════
def sim_monitor_short_fill(symbol, client_order_id, timeout_minutes):
    """Watch sim short order until filled, cancelled/rejected, or timeout."""
    deadline      = time.time() + timeout_minutes * 60
    poll_interval = 15

    log.info(f"[SIM:{symbol}] SHORT MONITOR started | order={client_order_id} | "
             f"timeout={timeout_minutes}min")

    while time.time() < deadline:
        time.sleep(poll_interval)

        state = sim_get_state(symbol).get("state")
        if state != "ACTIVE":
            if state == "BLOCKED":
                log.warning(f"[SIM:{symbol}] MONITOR: BLOCKED — cancelling orphaned sim order")
                try:
                    sim_cancel_all_open_orders(symbol)
                except Exception as e:
                    log.error(f"[SIM:{symbol}] MONITOR: cancel error: {e}")
            else:
                log.info(f"[SIM:{symbol}] MONITOR: state={state} — stopping")
            return

        try:
            r = sim_tz_get(f"/v1/api/accounts/{SIM_ACCOUNT_ID}/orders", "SIM_MONITOR_ORDERS")
            orders = r.json() if r.ok else []
            if not isinstance(orders, list):
                orders = orders.get("orders", [])
            order = next(
                (o for o in orders if o.get("clientOrderId") == client_order_id), None
            )
            if order:
                status   = order.get("orderStatus", "")
                executed = int(order.get("executed", 0))
                log.info(f"[SIM:{symbol}] MONITOR: status={status} executed={executed}")
                if status == "Filled" or executed > 0:
                    log.info(f"[SIM:{symbol}] MONITOR: filled — done")
                    return
                if status in ("Canceled", "Rejected"):
                    reject_text = order.get("text") or order.get("rejectReason") or "no reason"
                    sim_block(symbol, f"SIM short {status}: {reject_text}")
                    return
        except Exception as e:
            log.warning(f"[SIM:{symbol}] MONITOR: poll error — {e}")

    log.warning(f"[SIM:{symbol}] MONITOR: timeout after {timeout_minutes}min — cancelling")
    sim_cancel_and_cleanup(symbol, f"SIM short did not fill within {timeout_minutes}min")


def sim_monitor_cover_fill(symbol, client_order_id, timeout_minutes):
    """Watch sim cover order until filled; retry aggressively if it stalls."""
    deadline      = time.time() + timeout_minutes * 60
    poll_interval = 15

    log.info(f"[SIM:{symbol}] COVER MONITOR started | order={client_order_id} | "
             f"timeout={timeout_minutes}min")

    while time.time() < deadline:
        time.sleep(poll_interval)

        if sim_get_state(symbol).get("state") == "FLAT":
            log.info(f"[SIM:{symbol}] COVER MONITOR: already FLAT — done")
            return

        try:
            r = sim_tz_get(f"/v1/api/accounts/{SIM_ACCOUNT_ID}/orders", "SIM_COVER_MONITOR")
            orders = r.json() if r.ok else []
            if not isinstance(orders, list):
                orders = orders.get("orders", [])
            order = next(
                (o for o in orders if o.get("clientOrderId") == client_order_id), None
            )
            if order:
                status   = order.get("orderStatus", "")
                executed = int(order.get("executed", 0))
                log.info(f"[SIM:{symbol}] COVER MONITOR: status={status} executed={executed}")
                if status == "Filled" or executed > 0:
                    s            = sim_get_state(symbol)
                    phantom_cost = s.get("phantom_locate_cost", 0)
                    log.info(f"[SIM:{symbol}] COVER MONITOR: filled ✓ | "
                             f"phantom_locate_cost=${phantom_cost:.2f}")
                    sim_set_state(symbol, state="FLAT", reason="cover filled (sim monitor)")
                    return
                if status in ("Canceled", "Rejected"):
                    log.warning(f"[SIM:{symbol}] COVER MONITOR: {status} — retrying aggressively")
                    break
        except Exception as e:
            log.warning(f"[SIM:{symbol}] COVER MONITOR: poll error — {e}")

    log.warning(f"[SIM:{symbol}] COVER MONITOR: retrying aggressively")
    sim_cancel_all_open_orders(symbol)

    position = sim_get_position(symbol)
    if not position or float(position.get("shares", 0)) >= 0:
        log.info(f"[SIM:{symbol}] COVER MONITOR: no short remaining — marking FLAT")
        sim_set_state(symbol, state="FLAT", reason="cover monitor: no position on retry")
        return

    yahoo_price, yahoo_session = get_yahoo_price(symbol)
    if yahoo_price:
        aggressive_price = round(yahoo_price * (1 + COVER_RETRY_BUFFER), 2)
        log.info(f"[SIM:{symbol}] COVER MONITOR: aggressive retry at ${aggressive_price}")
    else:
        priceClose = float(position.get("priceClose") or 0)
        if priceClose > 0:
            aggressive_price = round(priceClose * (1 + COVER_RETRY_BUFFER), 2)
            log.warning(f"[SIM:{symbol}] COVER MONITOR: Yahoo unavailable — "
                        f"using priceClose fallback ${aggressive_price}")
        else:
            log.error(f"[SIM:{symbol}] COVER MONITOR: no price source — MANUAL ACTION REQUIRED")
            sim_block(symbol, "SIM aggressive cover failed: no price source")
            return

    cover_qty = abs(int(float(position.get("shares", 0))))
    try:
        result = sim_place_order(
            "Buy", symbol, cover_qty, aggressive_price, "SIM_COVER_AGGRESSIVE"
        )
        log.info(f"[SIM:{symbol}] COVER MONITOR: aggressive cover placed | "
                 f"clientOrderId={result.get('clientOrderId')}")
        sim_set_state(symbol, state="FLAT", reason="SIM aggressive cover placed")
    except Exception as e:
        log.error(f"[SIM:{symbol}] COVER MONITOR: aggressive cover FAILED: {e}")
        sim_block(symbol, "SIM aggressive cover failed — MANUAL ACTION REQUIRED")


def sim_cancel_and_cleanup(symbol, reason):
    log.info(f"[SIM:{symbol}] CANCEL+CLEANUP: {reason}")

    cancelled = sim_cancel_all_open_orders(symbol)
    log.info(f"[SIM:{symbol}] Cancelled {cancelled} open order(s)")

    position = sim_get_position(symbol)
    if position:
        shares = float(position.get("shares", 0))
        if shares < 0:
            cover_qty  = abs(int(shares))
            last_price = float(position.get("priceClose") or 0)
            if last_price <= 0:
                yahoo_price, yahoo_session = get_yahoo_price(symbol)
                if yahoo_price:
                    last_price = yahoo_price
                    log.info(f"[SIM:{symbol}] priceClose=0, using Yahoo ${last_price}")
                else:
                    log.error(f"[SIM:{symbol}] Cannot determine cover price — MANUAL ACTION REQUIRED")
                    sim_block(symbol, "SIM cleanup: no price source — MANUAL ACTION REQUIRED")
                    return
            cover_limit = round(last_price * 1.005, 2)
            try:
                result = sim_place_order("Buy", symbol, cover_qty, cover_limit, "SIM_COVER_ON_CANCEL")
                log.info(f"[SIM:{symbol}] Cover placed: {result.get('clientOrderId')}")
            except Exception as e:
                log.error(f"[SIM:{symbol}] Cover FAILED: {e} — MANUAL ACTION MAY BE REQUIRED")
        else:
            log.info(f"[SIM:{symbol}] No short to cover (shares={shares})")
    else:
        log.info(f"[SIM:{symbol}] No sim position found")

    sim_block(symbol, reason)


# ══════════════════════════════════════════════════════════════════════════════
# FLASK APP
# ══════════════════════════════════════════════════════════════════════════════
app = Flask(__name__)


@app.route("/", methods=["GET"])
def root():
    return jsonify({"status": "ok", "server": "tz-eod-short-webhook"}), 200


_last_health_log = 0

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
            r2 = requests.get(f"{BASE_URL}/v1/api/account/{ACCOUNT_ID}", headers=tz_headers(), timeout=8)
            bp = r2.json().get("bp") if r2.ok else None
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
    with sim_state_lock:
        sim_states = {k: v.get("state") for k, v in sim_symbol_state.items()}
    with _tz_cache_lock:
        cache = dict(_tz_cache)

    status = {
        "server":            "ok",
        "tz_api":            "ok" if cache["ok"] else "unreachable",
        "tz_detail":         cache["detail"],
        "account":           ACCOUNT_ID,
        "key_preview":       API_KEY[:8] + "..." if API_KEY else "NOT SET",
        "symbol_states":     states,
        "sim_symbol_states": sim_states,
        "sim_account":       SIM_ACCOUNT_ID or "NOT SET",
        "sim_key_preview":   SIM_API_KEY[:8] + "..." if SIM_API_KEY else "NOT SET",
        "routes":            cache["routes"],
        "ticker_aliases":    TICKER_ALIASES,
        "polygon_key":       POLYGON_API_KEY[:8] + "..." if POLYGON_API_KEY else "NOT SET",
        "twelvedata_key":    TWELVEDATA_API_KEY[:8] + "..." if TWELVEDATA_API_KEY else "NOT SET",
        "config": {
            "max_locate_cost_pct": MAX_LOCATE_COST_PCT,
            "min_locate_quantity": MIN_LOCATE_QUANTITY,
            "locate_poll_timeout": LOCATE_POLL_TIMEOUT,
            "price_handling":      "QC pre-buffered — server uses received price directly",
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


@app.route("/sim/state", methods=["GET"])
def sim_state_endpoint():
    with sim_state_lock:
        return jsonify(dict(sim_symbol_state)), 200


@app.route("/reset/<symbol>", methods=["POST"])
def reset_symbol(symbol):
    """Emergency manual reset of a real symbol to FLAT."""
    symbol = symbol.upper()
    with state_lock:
        symbol_state[symbol] = {"state": "FLAT"}
    log.info(f"[{symbol}] MANUAL RESET to FLAT")
    return jsonify({"status": "ok", "symbol": symbol, "state": "FLAT"}), 200


@app.route("/sim/reset/<symbol>", methods=["POST"])
def sim_reset_symbol(symbol):
    """Emergency manual reset of a sim symbol to FLAT."""
    symbol = symbol.upper()
    with sim_state_lock:
        sim_symbol_state[symbol] = {"state": "FLAT"}
    log.info(f"[SIM:{symbol}] MANUAL RESET to FLAT")
    return jsonify({"status": "ok", "symbol": symbol, "state": "FLAT", "mode": "sim"}), 200


# ══════════════════════════════════════════════════════════════════════════════
# REAL ACCOUNT — /webhook
# ══════════════════════════════════════════════════════════════════════════════
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
    qc_symbol   = str(body.get("symbol", "")).upper().strip().split()[0]
    quantity    = int(body.get("quantity", 0))
    price       = float(body.get("price", 0))
    cycle       = int(body.get("cycle", 1))

    if not qc_symbol:
        return jsonify({"error": "missing symbol"}), 400

    symbol = resolve_symbol(qc_symbol)
    if symbol != qc_symbol:
        log.info(f"[{qc_symbol}→{symbol}] Ticker resolved")

    s             = get_state(symbol)
    current_state = s.get("state", "FLAT")

    log.info(f"[{symbol}] action={action} | state={current_state} | "
             f"qty={quantity} | price=${price}")

    # ── SHORT ────────────────────────────────────────────────────────────────
    if action == "SHORT":
        if current_state == "BLOCKED":
            last_cycle = s.get("cycle", 0)
            if cycle > last_cycle:
                log.info(f"[{symbol}] BLOCKED lifted — new cycle {cycle} > last cycle {last_cycle}")
                set_state(symbol, state="FLAT")
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

    # ── COVER ────────────────────────────────────────────────────────────────
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
        cover_limit = float(price)

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

    # ── CANCEL ───────────────────────────────────────────────────────────────
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
# SIM ACCOUNT — /sim/webhook
# ══════════════════════════════════════════════════════════════════════════════
@app.route("/sim/webhook", methods=["POST"])
def sim_webhook():
    log.info("=" * 60)
    log.info("SIM WEBHOOK RECEIVED")
    log.info(f"  raw body: {request.data.decode('utf-8', errors='replace')[:1000]}")

    if not SIM_API_KEY or not SIM_API_SECRET or not SIM_ACCOUNT_ID:
        log.error("SIM credentials not configured — set TZ_SIM_API_KEY, TZ_SIM_API_SECRET, SIM_ACCOUNT_ID")
        return jsonify({"error": "sim credentials not configured — check Render env vars"}), 503

    try:
        body = request.get_json(force=True)
        if isinstance(body, str):
            body = json.loads(body)
    except Exception as e:
        log.error(f"  Parse error: {e}")
        return jsonify({"error": "invalid JSON"}), 400

    log.info(f"  parsed: {json.dumps(body)}")

    action    = str(body.get("action", "")).upper()
    qc_symbol = str(body.get("symbol", "")).upper().strip().split()[0]
    quantity  = int(body.get("quantity", 0))
    price     = float(body.get("price", 0))
    cycle     = int(body.get("cycle", 1))

    if not qc_symbol:
        return jsonify({"error": "missing symbol"}), 400

    symbol = resolve_symbol(qc_symbol)
    if symbol != qc_symbol:
        log.info(f"[SIM:{qc_symbol}→{symbol}] Ticker resolved")

    s             = sim_get_state(symbol)
    current_state = s.get("state", "FLAT")

    log.info(f"[SIM:{symbol}] action={action} | state={current_state} | "
             f"qty={quantity} | price=${price} | cycle={cycle}")

    # ── SHORT ────────────────────────────────────────────────────────────────
    if action == "SHORT":
        if current_state == "BLOCKED":
            last_cycle = s.get("cycle", 0)
            if cycle > last_cycle:
                log.info(f"[SIM:{symbol}] BLOCKED lifted — new cycle {cycle} > last {last_cycle}")
                sim_set_state(symbol, state="FLAT")
                current_state = "FLAT"
            else:
                log.info(f"[SIM:{symbol}] SHORT ignored — blocked "
                         f"(cycle={cycle} last={last_cycle}): {s.get('reason', '')}")
                return jsonify({
                    "status": "ignored", "reason": "blocked",
                    "cycle": cycle, "last_cycle": last_cycle, "mode": "sim",
                }), 200

        if current_state == "LOCATING":
            log.warning(f"[SIM:{symbol}] SHORT ignored — locate already in progress")
            return jsonify({"status": "ignored", "reason": "locate in progress", "mode": "sim"}), 200

        if current_state == "ACTIVE":
            log.warning(f"[SIM:{symbol}] SHORT rejected — already active")
            sim_block(symbol, "duplicate SHORT while ACTIVE")
            return jsonify({"status": "rejected", "reason": "already active", "mode": "sim"}), 200

        if quantity <= 0 or price <= 0:
            return jsonify({"error": "quantity and price required for SHORT"}), 400

        sim_set_state(symbol, state="LOCATING", entry_price=price, quantity=quantity, cycle=cycle)
        threading.Thread(
            target=locate_and_short_sim, args=(symbol, quantity, price), daemon=True
        ).start()
        return jsonify({
            "status": "ok", "action": "SHORT", "symbol": symbol,
            "state": "LOCATING", "mode": "sim",
        }), 200

    # ── COVER ────────────────────────────────────────────────────────────────
    elif action == "COVER":
        if current_state == "BLOCKED":
            log.info(f"[SIM:{symbol}] COVER ignored — blocked: {s.get('reason', '')}")
            return jsonify({"status": "ignored", "reason": "blocked", "mode": "sim"}), 200

        if current_state == "FLAT":
            log.warning(f"[SIM:{symbol}] COVER rejected — no position (FLAT)")
            sim_block(symbol, "COVER received with no active position")
            return jsonify({"status": "rejected", "reason": "no position", "mode": "sim"}), 200

        if current_state == "LOCATING":
            log.warning(f"[SIM:{symbol}] COVER during locate — cancelling")
            threading.Thread(
                target=sim_cancel_and_cleanup,
                args=(symbol, "COVER received while still locating"),
                daemon=True,
            ).start()
            return jsonify({
                "status": "ok", "action": "cleanup_on_cover_during_locate", "mode": "sim"
            }), 200

        position = sim_get_position(symbol)
        if position is None:
            log.warning(f"[SIM:{symbol}] COVER: no sim position found")
            sim_block(symbol, "COVER received but no sim position found")
            return jsonify({"status": "ok", "note": "no position found — blocked", "mode": "sim"}), 200

        shares = float(position.get("shares", 0))
        if shares >= 0:
            log.warning(f"[SIM:{symbol}] COVER: not a short (shares={shares})")
            sim_block(symbol, f"COVER but position is not short (shares={shares})")
            return jsonify({"status": "ok", "note": "not a short position", "mode": "sim"}), 200

        cover_qty    = abs(int(shares))
        cover_limit  = float(price)
        phantom_cost = s.get("phantom_locate_cost", 0)

        log.info(f"[SIM:{symbol}] Covering {cover_qty}sh | limit=${cover_limit} | "
                 f"phantom_locate_cost=${phantom_cost:.2f}")

        try:
            result = sim_place_order("Buy", symbol, cover_qty, cover_limit, "SIM_COVER_ORDER")
            sim_set_state(symbol, state="FLAT", reason="covered")
            log.info(f"[SIM:{symbol}] COVER PLACED | qty={cover_qty} | limit=${cover_limit} | "
                     f"clientOrderId={result.get('clientOrderId')} | "
                     f"status={result.get('orderStatus')} | "
                     f"phantom_locate_cost=${phantom_cost:.2f}")
            threading.Thread(
                target=sim_monitor_cover_fill,
                args=(symbol, result.get("clientOrderId"), COVER_FILL_TIMEOUT),
                daemon=True,
            ).start()
            return jsonify({
                "status":              "ok",
                "action":              "COVER",
                "symbol":              symbol,
                "mode":                "sim",
                "quantity":            cover_qty,
                "limit_price":         cover_limit,
                "clientOrderId":       result.get("clientOrderId"),
                "orderStatus":         result.get("orderStatus"),
                "phantom_locate_cost": round(phantom_cost, 4),
            }), 200
        except Exception as e:
            log.error(f"[SIM:{symbol}] Cover order failed: {e}")
            return jsonify({"error": str(e)}), 500

    # ── CANCEL ───────────────────────────────────────────────────────────────
    elif action == "CANCEL":
        log.info(f"[SIM:{symbol}] CANCEL received")
        threading.Thread(
            target=sim_cancel_and_cleanup,
            args=(symbol, "CANCEL signal from QC"),
            daemon=True,
        ).start()
        return jsonify({"status": "ok", "action": "CANCEL", "symbol": symbol, "mode": "sim"}), 200

    else:
        log.warning(f"[SIM:{symbol}] Unknown action: {action}")
        return jsonify({"error": f"unknown action: {action}"}), 400


# ══════════════════════════════════════════════════════════════════════════════
# ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        log.warning("TZ_API_KEY or TZ_API_SECRET not set — real account orders will fail")
    if not SIM_API_KEY or not SIM_API_SECRET or not SIM_ACCOUNT_ID:
        log.warning("SIM credentials not set — /sim/webhook will return 503")

    port = int(os.environ.get("PORT", 5000))

    log.info("")
    log.info("╔══════════════════════════════════════════════════════════╗")
    log.info("║  TradeZero EOD Short Webhook Server                      ║")
    log.info("║                                                          ║")
    log.info("║  REAL ACCOUNT                                            ║")
    log.info("║  POST /webhook          ← SHORT / COVER / CANCEL        ║")
    log.info("║  GET  /state            ← view real symbol states       ║")
    log.info("║  POST /reset/<symbol>   ← manually reset symbol to FLAT ║")
    log.info("║                                                          ║")
    log.info("║  SIM ACCOUNT                                             ║")
    log.info("║  POST /sim/webhook      ← SHORT / COVER / CANCEL        ║")
    log.info("║  GET  /sim/state        ← view sim symbol states        ║")
    log.info("║  POST /sim/reset/<sym>  ← manually reset sim to FLAT    ║")
    log.info("║                                                          ║")
    log.info("║  GET  /health           ← server + TZ API status        ║")
    log.info("╚══════════════════════════════════════════════════════════╝")
    log.info(f"  Real account:      {ACCOUNT_ID}")
    log.info(f"  Real key:          {API_KEY[:8] + '...' if API_KEY else 'NOT SET'}")
    log.info(f"  Sim account:       {SIM_ACCOUNT_ID or 'NOT SET'}")
    log.info(f"  Sim key:           {SIM_API_KEY[:8] + '...' if SIM_API_KEY else 'NOT SET — /sim/webhook returns 503'}")
    log.info(f"  Ticker aliases:    {TICKER_ALIASES}")
    log.info(f"  Polygon key:       {POLYGON_API_KEY[:8] + '...' if POLYGON_API_KEY else 'NOT SET'}")
    log.info(f"  Twelve Data key:   {TWELVEDATA_API_KEY[:8] + '...' if TWELVEDATA_API_KEY else 'NOT SET'}")
    log.info(f"  Max locate cost:   {MAX_LOCATE_COST_PCT*100:.1f}%")
    log.info(f"  Min locate qty:    {MIN_LOCATE_QUANTITY}")
    log.info(f"  Locate timeout:    {LOCATE_POLL_TIMEOUT}s")
    log.info(f"  Price handling:    QC pre-buffered — server uses received price directly")
    log.info(f"  Listening on port: {port}")
    log.info("")
    app.run(host="0.0.0.0", port=port, debug=False)
