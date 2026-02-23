import os, json, uuid, logging
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()

# ========================================
# CONFIGURATION
# ========================================
BASE_URL      = "https://webapi.tradezero.com"
API_KEY       = os.getenv("TZ_API_KEY")
API_SECRET    = os.getenv("TZ_API_SECRET")
ACCOUNT_ID    = os.getenv("ACCOUNT_ID", "TZP190C7")
LIMIT_BUFFER  = 0.005  # 0.5% price buffer

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

# ========================================
# CORE TRADING FUNCTIONS
# ========================================
def place_order(side, symbol, quantity, limit_price):
    client_id = f"QC_{side[:1]}_{uuid.uuid4().hex[:8].upper()}"
    payload = {
        "clientOrderId": client_id,
        "orderQuantity":  int(quantity),
        "orderType":      "Limit",
        "securityType":   "Stock",
        "side":           side,
        "symbol":         symbol,
        "timeInForce":    "Day",
        "limitPrice":     round(limit_price, 2),
    }
    url = f"{BASE_URL}/v1/api/accounts/{ACCOUNT_ID}/order"
    log.info(f"  → POST {url}")
    r = requests.post(url, headers=tz_headers(), json=payload, timeout=20)
    r.raise_for_status()
    return r.json()

# ========================================
# ROUTES
# ========================================
@app.route("/health", methods=["GET"])
def health():
    """
    Enhanced Health Check with Debug Logging
    """
    try:
        # Attempting to fetch account info to verify API link
        url = f"{BASE_URL}/v1/api/accounts"
        r = requests.get(url, headers=tz_headers(), timeout=10)
        
        # LOGGING FOR RAILWAY DEBUGGING
        log.info(f"DEBUG - TZ API URL: {url}")
        log.info(f"DEBUG - Status Code: {r.status_code}")
        log.info(f"DEBUG - Response Body: {r.text}")
        
        tz_ok = (r.status_code == 200)
        error_msg = r.text if not tz_ok else "None"
        
    except Exception as e:
        log.error(f"DEBUG - Connection Exception: {str(e)}")
        tz_ok = False
        error_msg = str(e)

    status = {
        "server": "ok", 
        "tz_api": "ok" if tz_ok else "unreachable", 
        "account": ACCOUNT_ID,
        "debug_info": error_msg[:300] # Captures first 300 chars of the error
    }
    return jsonify(status), 200

@app.route("/webhook", methods=["POST"])
def webhook():
    log.info("=" * 56)
    log.info("WEBHOOK RECEIVED")
    try:
        body = request.get_json(force=True)
        if isinstance(body, str):
            body = json.loads(body)
        
        action   = str(body.get("action", "")).upper()
        symbol   = str(body.get("symbol", "")).upper()
        quantity = int(body.get("quantity", 1))
        price    = float(body.get("price", 0))

        if action == "BUY":
            side, limit_price = "Buy", round(price * (1 + LIMIT_BUFFER), 2)
        else:
            side, limit_price = "Sell", round(price * (1 - LIMIT_BUFFER), 2)

        result = place_order(side, symbol, quantity, limit_price)
        return jsonify({"status": "ok", "result": result}), 200

    except Exception as e:
        log.error(f"Webhook Execution Error: {e}")
        return jsonify({"error": str(e)}), 500

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
