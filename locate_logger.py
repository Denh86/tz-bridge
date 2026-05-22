"""
locate_logger.py
================
Append-only Google Sheets logger for TradeZero locate quotes.

Designed to be readable: ~10 columns, one row per locate decision,
formatted to drop straight into an LLM prompt for periodic review
("here are 20 locate decisions, what patterns do you see?").

USAGE
-----
1. One-time setup:
   - Create a Google Cloud project, enable Google Sheets API
   - Create a service account, download the JSON key
   - Create a Google Sheet, share it with the service account's email
     (it ends in @<project>.iam.gserviceaccount.com)
   - Note the sheet ID from its URL: docs.google.com/spreadsheets/d/<SHEET_ID>/edit

2. Render env vars:
   - GOOGLE_SHEETS_SA_JSON: paste the entire service-account JSON
   - LOCATE_SHEET_ID:       the sheet ID from step 1
   - LOCATE_SHEET_TAB:      tab name (default 'locates')

3. requirements.txt:
   gspread>=6.0

4. In tz_webhook_server.py, import and call from each locate-decision exit:
       from locate_logger import log_locate

   One call per outcome branch. See `_call_sites` at the bottom of this file.
"""

import os
import json
import time
import queue
import threading
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger("locate_logger")

# ── Configuration ───────────────────────────────────────────────────────
SHEET_ID  = (os.getenv("LOCATE_SHEET_ID")  or "").strip()
SHEET_TAB = (os.getenv("LOCATE_SHEET_TAB") or "locates").strip()
SA_JSON   = (os.getenv("GOOGLE_SHEETS_SA_JSON") or "").strip()

# Behaviour — tuned for your low-volume reality (~5 events/day)
BATCH_SIZE         = 3
BATCH_MAX_WAIT_SEC = 5
WRITE_RETRIES      = 3
RETRY_BACKOFF_SEC  = 2
QUEUE_MAX_SIZE     = 500

# ── Schema (column order) ───────────────────────────────────────────────
HEADERS = [
    "ts_et",
    "symbol",
    "entry_price",
    "shares_offered",
    "locate_cost_per_share",
    "locate_cost_pct",
    "route",
    "outcome",
    "trade_outcome",
    "notes",
    "cover_fill_price",
    "realized_pnl_pct",
]

# ── Internal state ──────────────────────────────────────────────────────
_queue   = queue.Queue(maxsize=QUEUE_MAX_SIZE)
_started = False
_lock    = threading.Lock()


def _et_now():
    """Current Eastern time, human-readable. DST-aware."""
    try:
        from zoneinfo import ZoneInfo
        et = datetime.now(ZoneInfo("America/New_York"))
    except Exception:
        utc = datetime.now(timezone.utc)
        year = utc.year
        dst_start = datetime(year, 3, 1) + timedelta(
            days=(6 - datetime(year, 3, 1).weekday()) % 7 + 7)
        dst_end = datetime(year, 11, 1) + timedelta(
            days=(6 - datetime(year, 11, 1).weekday()) % 7)
        is_dst = (dst_start.replace(tzinfo=timezone.utc) <= utc
                  < dst_end.replace(tzinfo=timezone.utc))
        et = utc + timedelta(hours=-4 if is_dst else -5)
    return et.strftime("%Y-%m-%d %H:%M:%S ET")


def _build_client():
    if not SHEET_ID or not SA_JSON:
        log.warning("locate_logger: SHEET_ID or SA_JSON not set — DISABLED")
        return None
    try:
        import gspread
        creds = json.loads(SA_JSON)
        gc = gspread.service_account_from_dict(creds)
        sh = gc.open_by_key(SHEET_ID)
        try:
            ws = sh.worksheet(SHEET_TAB)
        except gspread.WorksheetNotFound:
            ws = sh.add_worksheet(title=SHEET_TAB, rows=1000, cols=len(HEADERS))
            ws.append_row(HEADERS, value_input_option="RAW")
            log.info(f"locate_logger: created tab '{SHEET_TAB}'")
        first = ws.row_values(1)
        if not first:
            ws.append_row(HEADERS, value_input_option="RAW")
        elif first != HEADERS:
            log.warning(f"locate_logger: header mismatch — "
                        f"sheet: {first[:5]}... | expected: {HEADERS[:5]}...")
        return ws
    except Exception as e:
        log.error(f"locate_logger: client build failed: {e}")
        return None


def _drain_loop():
    ws = None
    while True:
        if ws is None:
            ws = _build_client()
            if ws is None:
                try:
                    while True:
                        _queue.get_nowait()
                        _queue.task_done()
                except queue.Empty:
                    pass
                time.sleep(60)
                continue

        batch = []
        deadline = time.time() + BATCH_MAX_WAIT_SEC
        try:
            batch.append(_queue.get(timeout=BATCH_MAX_WAIT_SEC))
            _queue.task_done()
        except queue.Empty:
            continue

        while len(batch) < BATCH_SIZE and time.time() < deadline:
            try:
                batch.append(_queue.get(timeout=max(0.1, deadline - time.time())))
                _queue.task_done()
            except queue.Empty:
                break

        for attempt in range(1, WRITE_RETRIES + 1):
            try:
                ws.append_rows(batch, value_input_option="RAW")
                log.info(f"locate_logger: wrote {len(batch)} row(s)")
                break
            except Exception as e:
                if attempt < WRITE_RETRIES:
                    log.warning(f"locate_logger: attempt {attempt} failed: {e} — "
                                f"retrying in {RETRY_BACKOFF_SEC}s")
                    time.sleep(RETRY_BACKOFF_SEC)
                    ws = _build_client()
                    if ws is None:
                        break
                else:
                    log.error(f"locate_logger: write failed after {WRITE_RETRIES} attempts: "
                              f"{e} — DROPPING {len(batch)} row(s)")


def _start_once():
    global _started
    with _lock:
        if _started:
            return
        if not (SHEET_ID and SA_JSON):
            log.warning("locate_logger: missing env config — inactive")
            _started = True
            return
        threading.Thread(target=_drain_loop, daemon=True).start()
        _started = True
        log.info(f"locate_logger: drain thread started — "
                 f"sheet_id={SHEET_ID[:8]}... tab={SHEET_TAB}")


def log_locate(
    symbol,
    entry_price=0.0,
    shares_offered=0,
    locate_cost_per_share=0.0,
    route="PRIMARY",
    outcome="",
    trade_outcome="UNKNOWN_PENDING",
    notes="",
    cover_fill_price=None,
    realized_pnl_pct=None,
):
    """Enqueue a locate event. Returns immediately — never blocks."""
    _start_once()
    try:
        cost_pct = ((locate_cost_per_share / entry_price * 100.0)
                    if (entry_price and locate_cost_per_share) else 0.0)

        row = [
            _et_now(),
            str(symbol or ""),
            round(float(entry_price or 0.0), 4),
            int(shares_offered or 0),
            round(float(locate_cost_per_share or 0.0), 4),
            round(cost_pct, 3),
            str(route or "PRIMARY"),
            str(outcome or ""),
            str(trade_outcome or "UNKNOWN_PENDING"),
            str(notes or "")[:300],
            round(float(cover_fill_price), 4) if cover_fill_price else "",
            round(float(realized_pnl_pct), 3) if realized_pnl_pct is not None else "",
        ]

        try:
            _queue.put_nowait(row)
        except queue.Full:
            try:
                _queue.get_nowait()
                _queue.task_done()
            except queue.Empty:
                pass
            try:
                _queue.put_nowait(row)
            except queue.Full:
                log.warning("locate_logger: queue full — row dropped")
    except Exception as e:
        log.error(f"locate_logger: log_locate failed: {e}")


# ── Call-site cheat sheet ───────────────────────────────────────────────
_call_sites = """
WHERE TO CALL log_locate() IN tz_webhook_server.py
==================================================
One call per locate-flow exit. trade_outcome stays UNKNOWN_PENDING at log
time — fill in WIN/LOSS later by hand or from QC's trade CSV.

In locate_and_short(symbol, qc_quantity, entry_price):

A. Cost rejection:
       log_locate(symbol=symbol, entry_price=entry_price,
                  shares_offered=final_quantity,
                  locate_cost_per_share=locate_price,
                  route='SU_SECONDARY' if accept_id != quote_req_id else 'PRIMARY',
                  outcome='REJECTED_BY_COST',
                  trade_outcome='BLOCKED_NO_TRADE',
                  notes=f"{locate_cost_pct*100:.2f}% > {MAX_LOCATE_COST_PCT*100:.1f}%")

B. TZ rejection (status==56 or error==1):
       log_locate(symbol=symbol, entry_price=entry_price,
                  outcome='REJECTED_BY_TZ_NO_SHARES',
                  trade_outcome='BLOCKED_NO_TRADE',
                  notes=f"status={status} err={locate_error} text={locate_text}")

C. Timeout:
       log_locate(symbol=symbol, entry_price=entry_price,
                  outcome='TIMEOUT',
                  trade_outcome='BLOCKED_NO_TRADE',
                  notes=f"no actionable status after {LOCATE_POLL_TIMEOUT}s")

D. Accepted and traded (right after place_order_with_stepdown succeeds):
       log_locate(symbol=symbol, entry_price=entry_price,
                  shares_offered=final_quantity,
                  locate_cost_per_share=locate_price,
                  route='SU_SECONDARY' if accept_id != quote_req_id else 'PRIMARY',
                  outcome='ACCEPTED_TRADED',
                  trade_outcome='UNKNOWN_PENDING',
                  notes='')

E. Other (expired, cancelled, weird error):
       log_locate(symbol=symbol, entry_price=entry_price,
                  outcome='OTHER',
                  trade_outcome='BLOCKED_NO_TRADE',
                  notes=f"status={status} text={locate_text}")

Skip the SIM path — phantom locates clutter the sheet. If you want them,
add notes='SIM' so you can filter later.
"""
