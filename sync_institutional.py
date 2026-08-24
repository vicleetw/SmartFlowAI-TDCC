#!/usr/bin/env python3
"""
三大法人資料同步器（GitHub Actions 用）。

抓 TWSE / TPEx 官方 API，合併「三大法人買賣超」+「當日收盤價 OHLC」，
寫成 CSV 存到 data/twse/YYYYMMDD.csv 與 data/otc/YYYYMMDD.csv。

解析邏輯完全比照 SmartFlow iOS App 現有的
Rank/InstitutionalRankingService.swift（TWSE）與
Rank/OTCInstitutionalRankingService.swift（OTC），確保欄位語意、
過濾規則（排除權證/外股/特別股）、自營商合計算法都跟 App 端一致。

冪等設計：每次執行都掃描 data/ 底下已有哪些日期，只補缺漏的，
就算某次排程延遲、失敗、或重複觸發，都不會出錯或重複抓取。
"""

import csv
import json
import os
import re
import sys
import time
import http.client
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone

TAIPEI = timezone(timedelta(hours=8))
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA_DIR = os.path.join(ROOT, "data")

# 每次執行最多補幾天（idempotent 補漏，缺口大的話會分好幾次 Action 執行慢慢補完）
MAX_FETCH_PER_RUN = 15
# 往回找幾個交易日當候選清單上限（約一年）
MAX_CANDIDATE_DAYS = 260
# 每筆請求間隔秒數（放溫和一點，避免被 TWSE/TPEx 判定濫用）
REQUEST_DELAY = 2.0
# 連續幾次空資料就視為疑似被擋，本次執行提前結束
MAX_CONSECUTIVE_EMPTY = 3

CSV_HEADER = [
    "tradeDate", "stockCode", "stockName",
    "openPrice", "highPrice", "lowPrice", "closePrice",
    "changeSign", "change",
    "totalShares", "totalAmount",
    "foreignNetShares", "investmentNetShares", "dealerNetShares",
    "foreignBuyShares", "investmentBuyShares", "dealerBuyShares",
    "foreignSellShares", "investmentSellShares", "dealerSellShares",
]

UA = "Mozilla/5.0 (compatible; SmartFlowAI-DataSync/1.0)"


def http_get(url: str, timeout: int = 30) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read()


def clean_num(s):
    if s is None:
        return 0
    s = str(s).replace(",", "").strip()
    if s == "" or s == "--" or s == "----":
        return 0
    try:
        return int(float(s))
    except ValueError:
        return 0


def clean_float(s):
    if s is None:
        return 0.0
    s = str(s).replace(",", "").strip()
    if s == "" or s == "--" or s == "----":
        return 0.0
    try:
        return float(s)
    except ValueError:
        return 0.0


def is_valid_stock_code(code: str) -> bool:
    """比照 App 端過濾規則：1~5碼、頭尾皆數字，排除權證/外股/特別股。"""
    code = (code or "").strip()
    if not code or len(code) > 5:
        return False
    return code[0].isdigit() and code[-1].isdigit()


def trading_days_back(max_days: int):
    """今天（或昨天，視收盤時間）往回列出交易日（只跳週末，國定假日靠「空資料就不寫檔」自然略過）。"""
    now = datetime.now(TAIPEI)
    d = now.date()
    if now.hour < 15:
        d -= timedelta(days=1)
    out = []
    while len(out) < max_days:
        if d.weekday() < 5:  # 0=Mon ... 4=Fri
            out.append(d.strftime("%Y%m%d"))
        d -= timedelta(days=1)
    return out


def existing_dates(market: str) -> set:
    d = os.path.join(DATA_DIR, market)
    if not os.path.isdir(d):
        return set()
    return {f[:8] for f in os.listdir(d) if re.match(r"^\d{8}\.csv$", f)}


def write_csv(market: str, date: str, rows: list):
    d = os.path.join(DATA_DIR, market)
    os.makedirs(d, exist_ok=True)
    path = os.path.join(d, f"{date}.csv")
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(CSV_HEADER)
        for r in rows:
            w.writerow(r)
    print(f"  wrote {path} ({len(rows)} rows)")


# ────────────────────────────── TWSE ──────────────────────────────

def fetch_twse(date: str):
    """回傳 (rows, got_data)。got_data=False 代表這天疑似無資料/被擋（HTML 回應或 stat 非 OK）。"""
    t86_url = f"https://www.twse.com.tw/fund/T86?response=json&date={date}&selectType=ALL"
    mi_url = f"https://www.twse.com.tw/exchangeReport/MI_INDEX?response=json&date={date}&type=ALLBUT0999"

    try:
        t86_raw = http_get(t86_url)
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
        print(f"  TWSE T86 network error: {e}")
        return [], False

    if t86_raw.lstrip()[:1] == b"<":
        print("  TWSE T86 HTML response (blocked or maintenance)")
        return [], False

    try:
        t86 = json.loads(t86_raw)
    except json.JSONDecodeError:
        print("  TWSE T86 decode error")
        return [], False

    if t86.get("stat") != "OK" or not t86.get("data"):
        print(f"  TWSE T86 no data: {t86.get('stat')}")
        return [], True  # 真的是非交易日（官方明確回覆），不是被擋，視為「有回應」避免誤觸斷路器

    # T86 欄位（見 InstitutionalRankingService.swift 對照表）：
    #  [4]+[7]=外資淨 [2]+[5]=外資買 [3]+[6]=外資賣
    #  [10]=投信淨 [8]=投信買 [9]=投信賣
    #  [11]=自營商淨(合計) [12]+[15]=自營商買(自行+避險) [13]+[16]=自營商賣(自行+避險)
    inst_map = {}
    for cols in t86["data"]:
        if len(cols) < 18:
            continue
        code = str(cols[0]).strip()
        name = str(cols[1]).strip()
        if not is_valid_stock_code(code):
            continue
        v = lambda i: clean_num(cols[i])
        inst_map[code] = {
            "name": name,
            "foreignNet": v(4) + v(7), "foreignBuy": v(2) + v(5), "foreignSell": v(3) + v(6),
            "investNet": v(10), "investBuy": v(8), "investSell": v(9),
            "dealerNet": v(11), "dealerBuy": v(12) + v(15), "dealerSell": v(13) + v(16),
        }

    time.sleep(0.3)
    try:
        mi_raw = http_get(mi_url)
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
        print(f"  TWSE MI_INDEX network error: {e}")
        return [], False

    if mi_raw.lstrip()[:1] == b"<":
        print("  TWSE MI_INDEX HTML response (blocked or maintenance)")
        return [], False

    try:
        mi = json.loads(mi_raw)
    except json.JSONDecodeError:
        print("  TWSE MI_INDEX decode error")
        return [], False

    price_map = {}
    for table in mi.get("tables", []):
        fields = table.get("fields") or []
        if "收盤價" not in fields:
            continue
        for cols in table.get("data", []):
            if len(cols) < 11:
                continue
            code = str(cols[0]).strip()
            close = clean_float(cols[8])
            if close <= 0:
                continue
            sign_raw = cols[9]
            if ">+<" in sign_raw:
                sign = "+"
            elif ">-<" in sign_raw or ">－<" in sign_raw:
                sign = "-"
            else:
                sign = ""
            price_map[code] = {
                "open": clean_float(cols[5]), "high": clean_float(cols[6]),
                "low": clean_float(cols[7]), "close": close,
                "sign": sign, "change": clean_float(cols[10]),
                "shares": clean_num(cols[2]), "amount": clean_float(cols[4]),
            }
        break

    rows = []
    for code, inst in inst_map.items():
        p = price_map.get(code)
        if not p or p["close"] <= 0:
            continue
        rows.append([
            date, code, inst["name"],
            p["open"], p["high"], p["low"], p["close"], p["sign"], p["change"],
            p["shares"], p["amount"],
            inst["foreignNet"], inst["investNet"], inst["dealerNet"],
            inst["foreignBuy"], inst["investBuy"], inst["dealerBuy"],
            inst["foreignSell"], inst["investSell"], inst["dealerSell"],
        ])
    return rows, True


# ────────────────────────────── OTC (TPEx) ──────────────────────────────

def greg_to_roc(date: str) -> str:
    y, m, d = int(date[:4]), int(date[4:6]), int(date[6:8])
    return f"{y - 1911}/{m:02d}/{d:02d}"


def fetch_otc(date: str):
    roc = greg_to_roc(date)
    roc_enc = roc.replace("/", "%2F")
    inst_url = f"https://www.tpex.org.tw/web/stock/3insti/daily_trade/3itrade_hedge_result.php?l=zh-tw&se=AL&t=D&d={roc_enc}"
    price_url = f"https://www.tpex.org.tw/web/stock/aftertrading/otc_quotes_no1430/stk_wn1430_result.php?l=zh-tw&d={roc_enc}&se=AL"

    try:
        inst_raw = http_get(inst_url)
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
        print(f"  OTC 3insti network error: {e}")
        return [], False

    if inst_raw.lstrip()[:1] == b"<":
        print("  OTC 3insti HTML response (blocked or maintenance)")
        return [], False

    try:
        inst_resp = json.loads(inst_raw)
    except json.JSONDecodeError:
        print("  OTC 3insti decode error")
        return [], False

    tables = inst_resp.get("tables") or []
    rows_raw = tables[0].get("data") if tables else None
    if not rows_raw:
        print("  OTC 3insti no data (holiday)")
        return [], True

    # TPEx 三大法人 24 欄（見 OTCInstitutionalRankingService.swift 對照表）：
    #  [4]=外陸資淨 [2]=外陸資買 [3]=外陸資賣
    #  [13]=投信淨 [11]=投信買 [12]=投信賣
    #  [22]=自營商合計淨 [20]=自營商合計買 [21]=自營商合計賣
    inst_map = {}
    for cols in rows_raw:
        if len(cols) < 23:
            continue
        code = str(cols[0]).strip()
        name = str(cols[1]).strip()
        if not is_valid_stock_code(code):
            continue
        v = lambda i: clean_num(cols[i])
        inst_map[code] = {
            "name": name,
            "foreignNet": v(4), "foreignBuy": v(2), "foreignSell": v(3),
            "investNet": v(13), "investBuy": v(11), "investSell": v(12),
            "dealerNet": v(22), "dealerBuy": v(20), "dealerSell": v(21),
        }

    time.sleep(0.3)
    try:
        price_raw = http_get(price_url)
    except (urllib.error.URLError, http.client.HTTPException, OSError) as e:
        print(f"  OTC stk_wn1430 network error: {e}")
        return [], False

    if price_raw.lstrip()[:1] == b"<":
        print("  OTC stk_wn1430 HTML response (blocked or maintenance)")
        return [], False

    try:
        price_resp = json.loads(price_raw)
    except json.JSONDecodeError:
        print("  OTC stk_wn1430 decode error")
        return [], False

    tables2 = price_resp.get("tables") or []
    price_rows = tables2[0].get("data") if tables2 else None
    price_map = {}
    if price_rows:
        # [0]代號 [1]名稱 [2]收盤 [3]漲跌 [4]開盤 [5]最高 [6]最低 [7]成交股數 [8]成交金額
        for cols in price_rows:
            if len(cols) < 9:
                continue
            code = str(cols[0]).strip()
            close = clean_float(cols[2])
            if close <= 0:
                continue
            change_raw = clean_float(cols[3])
            sign = "+" if change_raw > 0 else ("-" if change_raw < 0 else "")
            price_map[code] = {
                "open": clean_float(cols[4]), "high": clean_float(cols[5]),
                "low": clean_float(cols[6]), "close": close,
                "sign": sign, "change": abs(change_raw),
                "shares": clean_num(cols[7]), "amount": clean_float(cols[8]),
            }

    rows = []
    for code, inst in inst_map.items():
        p = price_map.get(code)
        if not p or p["close"] <= 0:
            continue
        rows.append([
            date, code, inst["name"],
            p["open"], p["high"], p["low"], p["close"], p["sign"], p["change"],
            p["shares"], p["amount"],
            inst["foreignNet"], inst["investNet"], inst["dealerNet"],
            inst["foreignBuy"], inst["investBuy"], inst["dealerBuy"],
            inst["foreignSell"], inst["investSell"], inst["dealerSell"],
        ])
    return rows, True


# ────────────────────────────── 主流程 ──────────────────────────────

def sync_market(market: str, fetch_fn):
    print(f"=== {market.upper()} ===")
    existing = existing_dates(market)
    candidates = trading_days_back(MAX_CANDIDATE_DAYS)
    missing = [d for d in candidates if d not in existing][:MAX_FETCH_PER_RUN]

    if not missing:
        print("  nothing to do")
        return

    print(f"  {len(missing)} dates to fetch: {missing[-1]} .. {missing[0]}")
    consecutive_empty = 0
    for date in missing:
        if consecutive_empty >= MAX_CONSECUTIVE_EMPTY:
            print(f"  連續{consecutive_empty}次空資料，疑似被擋，本次執行提前結束（剩餘留到下次）")
            break
        print(f"  fetching {date} ...")
        rows, got_response = fetch_fn(date)
        if rows:
            write_csv(market, date, rows)
            consecutive_empty = 0
        elif got_response:
            # 官方明確回覆「非交易日」：不算被擋，但也沒東西可寫，不重置斷路器計數
            # （避免連續好幾天真的都是假日時，被誤判成堆積空結果）
            pass
        else:
            consecutive_empty += 1
        time.sleep(REQUEST_DELAY)


def main():
    sync_market("twse", fetch_twse)
    sync_market("otc", fetch_otc)


if __name__ == "__main__":
    main()
