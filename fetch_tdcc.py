import requests
import pandas as pd
import io
import os
import socket
import sys
import time

import urllib3.util.connection as urllib3_cn

# GitHub runner 解析到集保所的 IPv6 位址時會噴 "Network is unreachable"（runner 沒有 IPv6 路由），
# 這裡強制只走 IPv4。
urllib3_cn.allowed_gai_family = lambda: socket.AF_INET

# 設定儲存目錄
output_dir = "data"
if not os.path.exists(output_dir):
    os.makedirs(output_dir)

url = 'https://smart.tdcc.com.tw/opendata/getOD.ashx?id=1-5'
headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/111.0.0.0 Safari/537.36",
    "Referer": "https://www.tdcc.com.tw/"
}

MAX_ATTEMPTS = 5


def fetch_with_retry():
    # 集保所伺服器偶爾很慢（實測單次可到 20 秒），連線/讀取分開給足時間，失敗就退避重試。
    last_error = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            response = requests.get(url, headers=headers, timeout=(30, 120))
            response.encoding = 'utf-8'
            if response.status_code == 200:
                return response
            last_error = f"HTTP {response.status_code}"
        except requests.RequestException as e:
            last_error = str(e)
        print(f"⚠️ 第 {attempt}/{MAX_ATTEMPTS} 次抓取失敗: {last_error}")
        if attempt < MAX_ATTEMPTS:
            time.sleep(15 * attempt)
    raise RuntimeError(f"重試 {MAX_ATTEMPTS} 次仍失敗: {last_error}")


try:
    print("📡 正在抓取集保所原始資料...")
    response = fetch_with_retry()

    # 解析日期
    df_sample = pd.read_csv(io.StringIO(response.text), nrows=1)
    date_col = [c for c in df_sample.columns if '資料日期' in c][0]
    raw_date = str(df_sample[date_col].iloc[0]).strip()

    # 定義路徑
    history_file = os.path.join(output_dir, f"{raw_date}.csv")
    latest_file = os.path.join(output_dir, "latest.csv")

    # 儲存原始內容 (UTF-8-SIG 確保 Excel 不亂碼)
    raw_content = response.text

    # 1. 存日期檔 (如果不存在才存)
    if not os.path.exists(history_file):
        with open(history_file, "w", encoding="utf-8-sig") as f:
            f.write(raw_content)
        print(f"✅ 已儲存歷史檔: {history_file}")
    else:
        print(f"⏭️ 日期 {raw_date} 已存在，跳過儲存。")

    # 2. 永遠更新一份 latest.csv 供 App 直接抓取
    with open(latest_file, "w", encoding="utf-8-sig") as f:
        f.write(raw_content)
    print(f"✅ 已更新 latest.csv")

except Exception as e:
    print(f"❌ 發生錯誤: {e}")
    sys.exit(1)
