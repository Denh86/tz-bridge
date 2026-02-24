"""
TradeZero Webhook Execution Server
====================================
Receives BUY/SELL/CANCEL signals from QuantConnect.

HOW TO RUN
----------
1.  pip install flask requests python-dotenv
2.  .env:  TZ_API_KEY=...  TZ_API_SECRET=...
3.  python tz_webhook_server.py
4.  ngrok http 5000  → paste URL into qc_sqqq_webhook.py

SIGNAL FORMAT:
  {"action": "BUY",    "symbol": "SQQQ", "quantity": 1, "price": 70.50}
  {"action": "SELL",   "symbol": "SQQQ", "quantity": 1, "price": 70.50}
  {"action": "CANCEL", "symbol": "SQQQ", "clientOrderId": "QC_B_XXXXXXXX"}
    (if no clientOrderId, cancels all open orders for that symbol)
"""

import os, json, uuid, logging
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

BASE_URL     = "https://webapi.tradezero.com"
API_KEY      = (os.getenv("TZ_API_KEY") or "").strip()
API_SECRET   = (os.getenv("TZ_API_SECRET") or "").strip()
ACCOUNT_ID   = "TZP190C7"
LIMIT_BUFFER = 0.005   # 0.5% price buffer so limit fills immediately

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("tz_server")
app = Flask(__name__)


def tz_headers():
    return {
        "TZ-API-KEY-ID":     API_KEY,
        "TZ-API-SECRET-KEY": API_SECRET,
        "Content-Type":      "application/json",
        "Accept":            "application/json",
    }


def place_order(side, symbol, quantity, limit_price=None, order_type="Limit"):
    client_id = f"QC_{side[:1]}_{uuid.uuid4().hex[:8].upper()}"
    payload = {
        "clientOrderId": client_id,
        "orderQuantity":  int(quantity),
        "orderType":      order_type,
        "securityType":   "Stock",
        "side":           side,
        "symbol":         symbol,
        "timeInForce":    "Day",
    }
    if limit_price is not None:
        payload["limitPrice"] = round(limit_price, 2)
    url = f"{BASE_URL}/v1/api/accounts/{ACCOUNT_ID}/order"
    log.info(f"  → POST {url}")
    log.info(f"    {json.dumps(payload)}")
    r = requests.post(url, headers=tz_headers(), json=payload, timeout=20)
    log.info(f"    status: {r.status_code}  body: {r.text[:400]}")
    r.raise_for_status()
    data = r.json()
    # Track the real clientOrderId the server assigned (may differ from our input)
    log.info(f"    assigned clientOrderId: {data.get('clientOrderId')}  status: {data.get('orderStatus')}")
    return data


def cancel_order(client_order_id):
    """Cancel a specific order by clientOrderId."""
    url = f"{BASE_URL}/v1/api/accounts/{ACCOUNT_ID}/orders/{client_order_id}"
    log.info(f"  → DELETE {url}")
    r = requests.delete(url, headers=tz_headers(), timeout=15)
    log.info(f"    status: {r.status_code}  body: {r.text[:200]}")
    return r.status_code in (200, 204)


def cancel_all_open_orders(symbol):
    """Fetch all open orders for symbol and cancel each one."""
    url = f"{BASE_URL}/v1/api/accounts/{ACCOUNT_ID}/orders"
    r = requests.get(url, headers=tz_headers(), timeout=15)
    r.raise_for_status()
    orders = r.json().get("orders", [])
    open_orders = [
        o for o in orders
        if o.get("symbol") == symbol
        and o.get("orderStatus") in ("PendingNew", "New", "PartiallyFilled")
    ]
    log.info(f"  Found {len(open_orders)} open order(s) for {symbol}")
    cancelled = 0
    for o in open_orders:
        oid = o.get("clientOrderId")
        if oid and cancel_order(oid):
            cancelled += 1
    return cancelled



def handle_qc_notification(body):
    """
    Translate a QC order notification into a TZ order.
    We log the full body first run so we can confirm the exact field names.
    QC notification format is not fully documented — this will be refined
    once we see the first real notification come through.
    """
    log.info(f"  QC notification body: {json.dumps(body, indent=2)}")

    # Common QC field names — we'll confirm these from the first real hit
    status        = str(body.get("status", body.get("orderStatus", ""))).lower()
    symbol        = str(body.get("symbol", "")).upper().replace(" ", "")
    fill_qty      = float(body.get("fillQuantity", body.get("quantity", 0)))
    fill_price    = float(body.get("fillPrice", body.get("price", 0)))
    direction     = str(body.get("direction", body.get("side", ""))).lower()

    log.info(f"  Parsed: status={status} symbol={symbol} qty={fill_qty} "
             f"price={fill_price} direction={direction}")

    # Only act on filled orders — ignore submitted/cancelled/etc for now
    if status != "filled":
        log.info(f"  Ignoring non-filled status: {status}")
        return jsonify({"status": "ignored", "reason": f"status={status}"}), 200

    if not symbol or fill_qty <= 0 or fill_price <= 0:
        log.error(f"  Missing required fields — cannot place TZ order")
        return jsonify({"error": "missing fields"}), 400

    # QC direction: "buy" maps to TZ "Buy", anything else (sell/short) maps to "Sell"
    side = "Buy" if "buy" in direction else "Sell"
    limit_price = round(fill_price * (1 + LIMIT_BUFFER if side == "Buy" else 1 - LIMIT_BUFFER), 2)

    log.info(f"  → Placing TZ {side} {fill_qty} × {symbol} @ ${limit_price}")
    try:
        result = place_order(side, symbol, int(fill_qty), limit_price)
        response = {
            "status":        "ok",
            "side":          side,
            "symbol":        symbol,
            "quantity":      fill_qty,
            "limit_price":   limit_price,
            "clientOrderId": result.get("clientOrderId"),
            "orderStatus":   result.get("orderStatus"),
        }
        log.info(f"  ✅  {response}")
        return jsonify(response), 200
    except Exception as e:
        log.error(f"  TZ order failed: {e}")
        return jsonify({"error": str(e)}), 500


# ── Main webhook ──────────────────────────────────────────────────────────────
@app.route("/webhook", methods=["POST"])
def webhook():
    log.info("=" * 56)
    log.info("WEBHOOK RECEIVED")

    # Log raw headers and body first so we can see exactly what QC sends
    log.info(f"  headers: {dict(request.headers)}")
    log.info(f"  raw body: {request.data.decode('utf-8', errors='replace')[:1000]}")

    try:
        body = request.get_json(force=True)
        if isinstance(body, str):
            body = json.loads(body)
    except Exception as e:
        log.error(f"  Parse error: {e}  raw: {request.data[:300]}")
        return jsonify({"error": "invalid JSON"}), 400

    log.info(f"  parsed: {json.dumps(body)}")

    # ── If this looks like a QC order notification (has "orderId" or "status") ──
    # translate it to a TZ action
    if "orderId" in body or "orderStatus" in body or "status" in body:
        log.info("  → Looks like a QC order notification — translating...")
        return handle_qc_notification(body)

    # ── Otherwise treat as our own manual signal format ──
    action = str(body.get("action", "")).upper()

    # ── CANCEL ────────────────────────────────────────────────────────────────
    if action == "CANCEL":
        symbol   = str(body.get("symbol", "SQQQ")).upper()
        oid      = body.get("clientOrderId")
        if oid:
            success = cancel_order(oid)
            log.info(f"  {'✅' if success else '⚠️'}  Cancel {oid}: {'ok' if success else 'failed'}")
            return jsonify({"status": "ok" if success else "failed", "cancelled": oid}), 200
        else:
            count = cancel_all_open_orders(symbol)
            log.info(f"  ✅  Cancelled {count} open order(s) for {symbol}")
            return jsonify({"status": "ok", "cancelled_count": count}), 200

    # ── BUY / SELL ────────────────────────────────────────────────────────────
    if action not in ("BUY", "SELL"):
        return jsonify({"error": f"unknown action: {action}"}), 400

    symbol   = str(body.get("symbol", "SQQQ")).upper()
    quantity = int(body.get("quantity", 1))
    price    = float(body.get("price", 0))

    if price <= 0:
        return jsonify({"error": "price must be > 0"}), 400

    if action == "BUY":
        side = "Buy"
    else:
        side = "Sell"

    # If the incoming price is a crypto price (BTC etc.) it will be way above
    # TZ's limit of $9999.99 — use a Market order instead so we don't need a price.
    # When QC sends a real SQQQ price this will switch back to limit automatically.
    if price > 9999:
        log.info(f"  Price ${price} looks like crypto — using Market order for {symbol}")
        use_market = True
    else:
        use_market = False
        limit_price = round(price * (1 + LIMIT_BUFFER if side == "Buy" else 1 - LIMIT_BUFFER), 2)

    log.info(f"  Executing: {side} {quantity} × {symbol} {'MARKET' if use_market else f'@ ${limit_price}'}")
    try:
        result = place_order(side, symbol, quantity, limit_price=None if use_market else limit_price, order_type="Market" if use_market else "Limit")
        response = {
            "status":        "ok",
            "action":        action,
            "side":          side,
            "symbol":        symbol,
            "quantity":      quantity,
            "limit_price":   limit_price,
            "clientOrderId": result.get("clientOrderId"),
            "orderStatus":   result.get("orderStatus"),
        }
        log.info(f"  ✅  {response}")
        return jsonify(response), 200
    except requests.exceptions.HTTPError as e:
        log.error(f"  TZ API error: {e}")
        return jsonify({"error": str(e)}), 502
    except Exception as e:
        log.error(f"  Unexpected error: {e}")
        return jsonify({"error": str(e)}), 500


@app.route("/health", methods=["GET"])
def health():
    try:
        r = requests.get(f"{BASE_URL}/v1/api/accounts/{ACCOUNT_ID}/positions", headers=tz_headers(), timeout=10)
        tz_ok = r.status_code == 200
        tz_detail = f"http_{r.status_code}"
    except requests.exceptions.Timeout:
        tz_ok = False
        tz_detail = "timeout"
    except requests.exceptions.ConnectionError as e:
        tz_ok = False
        tz_detail = f"connection_error: {str(e)[:100]}"
    except Exception as e:
        tz_ok = False
        tz_detail = f"error: {str(e)[:100]}"
    status = {
        "server": "ok",
        "tz_api": "ok" if tz_ok else "unreachable",
        "tz_detail": tz_detail,
        "account": ACCOUNT_ID
    }
    log.info(f"HEALTH: {status}")
    return jsonify(status), 200


@app.route("/test/<action>", methods=["GET"])
def test_order(action):
    price = float(request.args.get("price", 70.37))
    qty   = int(request.args.get("qty", 1))
    side  = "Buy" if action.upper() == "BUY" else "Sell"
    limit_price = round(price * (1 + LIMIT_BUFFER if side == "Buy" else 1 - LIMIT_BUFFER), 2)
    try:
        result = place_order(side, "SQQQ", qty, limit_price)
        return jsonify({"status": "ok", "limit_price": limit_price, "result": result}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route("/", methods=["GET"])
def root():
    """Root route — Railway health check hits this."""
    return jsonify({"status": "ok", "server": "tz-webhook"}), 200


if __name__ == "__main__":
    if not API_KEY or not API_SECRET:
        print("WARNING: TZ_API_KEY or TZ_API_SECRET not set — webhook will fail but server will start")

    port = int(os.environ.get("PORT", 5000))   # Railway sets PORT dynamically

    log.info("")
    log.info("╔══════════════════════════════════════════════════════╗")
    log.info("║  TradeZero Webhook Server — Ready                    ║")
    log.info("║                                                      ║")
    log.info("║  POST /webhook   ← BUY / SELL / CANCEL signals      ║")
    log.info("║  GET  /health    ← ping server + TZ API             ║")
    log.info("║  GET  /test/buy  ← manual test                      ║")
    log.info("║  GET  /test/sell ← manual test                      ║")
    log.info("╚══════════════════════════════════════════════════════╝")
    log.info(f"  Listening on port {port}")
    log.info("")
    app.run(host="0.0.0.0", port=port, debug=False)
