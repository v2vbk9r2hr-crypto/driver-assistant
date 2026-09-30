import os
import json
import base64
import re
from flask import Flask, render_template, jsonify, request, abort
import gspread
from google.oauth2.service_account import Credentials
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage

app = Flask(__name__)

# ---------------------------------------------------------
# 全域記憶體/快取設定 (記錄重置後看過的第一單內容)
# ---------------------------------------------------------
ORIGINAL_ORDERS_CACHE = {}

def clear_memory_cache():
    """清空系統暫存記憶體，從第一單重新開始記錄"""
    global ORIGINAL_ORDERS_CACHE
    ORIGINAL_ORDERS_CACHE.clear()
    print("🧹 [記憶體清空] 系統暫存記憶體已成功重置！將從第一單重新開始記錄。")

# ---------------------------------------------------------
# 環境變數與 Google Sheet 設定
# ---------------------------------------------------------
LINE_CHANNEL_ACCESS_TOKEN = os.getenv('LINE_CHANNEL_ACCESS_TOKEN', '')
LINE_CHANNEL_SECRET = os.getenv('LINE_CHANNEL_SECRET', '')

line_bot_api = LineBotApi(LINE_CHANNEL_ACCESS_TOKEN) if LINE_CHANNEL_ACCESS_TOKEN else None
handler = WebhookHandler(LINE_CHANNEL_SECRET) if LINE_CHANNEL_SECRET else None

SHEET_NAME = "調度室派單管理表"

def get_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    
    creds_env = os.getenv("GOOGLE_CREDENTIALS")
    if creds_env:
        try:
            creds_json = json.loads(creds_env)
        except Exception:
            creds_json = json.loads(base64.b64decode(creds_env).decode("utf-8"))
        creds = Credentials.from_service_account_info(creds_json, scopes=scopes)
    else:
        creds = Credentials.from_service_account_file('credentials.json', scopes=scopes)
        
    gs_client = gspread.authorize(creds)
    return gs_client.open(SHEET_NAME).sheet1

# ---------------------------------------------------------
# 單號、時間與地址解析邏輯
# ---------------------------------------------------------
def extract_core_address(text):
    """清理地址內容"""
    t = text
    t = re.sub(r'\[.*?\]', '', t)                          
    t = re.sub(r'#[a-zA-Z0-9/／]+', '', t)                 
    t = re.sub(r'\d{1,2}[\.:：點]\d{2}?', '', t)             
    t = re.sub(r'(轉帳|改兩台|客下街口|\+\d+)', '', t)       
    t = re.sub(r'[^\w\u4e00-\u9fa5]', '', t)                 
    return t.strip()

def parse_order_info(text):
    """從訊息中解析出：單號、預約時間、核心地址"""
    if not text:
        return None, None, ""
        
    first_line = text.splitlines()[0].strip()
    
    # 1. 抓取 # 後面的單號
    code_match = re.search(r'#([a-zA-Z0-9\u4e00-\u9fa5]+)', first_line)
    order_code = code_match.group(1) if code_match else None

    # 2. 抓取預約時間
    time_match = re.search(r'(\d{1,2}[:.]\d{2}|\d{4})', first_line)
    booking_time = time_match.group(1).replace('.', ':') if time_match else None

    # 3. 抓取地址核心
    clean_text = re.sub(r'[\U00010000-\U0010ffff\u2600-\u27FF]', '', first_line)
    addr_match = re.search(r'/\s*([^\s+]+)', clean_text)
    core_address = addr_match.group(1) if addr_match else ""

    return order_code, booking_time, core_address

# ---------------------------------------------------------
# LINE Webhook 接收與寫入 Google Sheet
# ---------------------------------------------------------
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers.get('X-Line-Signature', '')
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

if handler:
    @handler.add(MessageEvent, message=TextMessage)
    def handle_message(event):
        msg_text = event.message.text.strip()

        # ---------------------------------------------------------
        # 手動清空記憶體功能指令
        # ---------------------------------------------------------
        if msg_text in ['!clear', '!清空記憶體', '清空記憶體']:
            clear_memory_cache()
            return

        # 訊息必須包含 # 才進行處理
        if '#' not in msg_text:
            return

        lines = [line.strip() for line in msg_text.splitlines() if line.strip()]
        incoming_first_line = lines[0] if lines else msg_text

        # 嘗試解析單號
        order_code, booking_time, core_address = parse_order_info(incoming_first_line)
        if not order_code:
            print(f"⚠️ 無法解析單號，跳過訊息: {msg_text}")
            return

        upper_code = order_code.upper()

        # ---------------------------------------------------------
        # 【過濾與驗證規則寫死】
        # ---------------------------------------------------------
        has_car_num = bool(re.search(r'\d{4}', msg_text))
        is_forwarded = msg_text.startswith('[')
        has_status_kw = bool(re.search(r'(客上|客到|到|上|下|收|取消|抵達|代駕|紙煙|紙菸)', msg_text))

        last_line = lines[-1] if lines else msg_text

        # 判斷最後一行是否為純「到/上/下/客上」
        is_pure_status_last_line = bool(re.fullmatch(r'^(到|客上|上|下|收|取消|抵達)$', last_line))

        # 判斷最後一行是否有時間/分鐘 (例: 6分, 12分, 10min, 15)
        has_minute_in_last_line = bool(re.search(r'(\d{1,2}\s*(分鐘|分|min)|\b\d{1,2}\b)', last_line))

        # 司機回報認定條件
        is_driver_report = (has_car_num or is_forwarded or has_status_kw) and has_minute_in_last_line and not is_pure_status_last_line

        # 遇到純「到/上/下」無時間回報 -> 直接拋棄不處理
        if (has_car_num or is_forwarded or has_status_kw) and is_pure_status_last_line:
            print(f"🛑 [過濾拋棄] 純狀態回報 ({last_line}) 且無時間，不寫入試算表: {msg_text}")
            return

        try:
            sheet = get_sheet()
            records = sheet.get_all_values()

            target_row_idx = None
            first_empty_row = len(records) + 1  # 預設第一個空白列

            # 遍歷試算表：尋找匹配單號 & 計算第一個空白列
            for idx, row in enumerate(records[1:], start=2):
                if not row or not row[0].strip():
                    if first_empty_row > idx:
                        first_empty_row = idx
                    continue

                ex_text = row[0]
                ex_code, ex_time, ex_address = parse_order_info(ex_text)

                if ex_code and upper_code == ex_code.upper():
                    if core_address and ex_address:
                        if (core_address in ex_address) or (ex_address in core_address):
                            target_row_idx = idx
                            break
                    else:
                        target_row_idx = idx
                        break

            # ---------------------------------------------------------
            # 【A 欄單號內容鎖定機制】
            # 優先使用記憶體中「重置後看過的第一單」內容，若無則存入快取
            # ---------------------------------------------------------
            if upper_code in ORIGINAL_ORDERS_CACHE:
                a_column_content = ORIGINAL_ORDERS_CACHE[upper_code]
            else:
                a_column_content = incoming_first_line
                ORIGINAL_ORDERS_CACHE[upper_code] = incoming_first_line
                print(f"📌 [記憶體紀錄] 單號 #{upper_code} 鎖定第一單A欄內容: {a_column_content}")

            # --------------------------------------------------
            # 情境 A：司機回報 (最後一行含時間)
            # --------------------------------------------------
            if is_driver_report:
                if target_row_idx:
                    # 原單存在：A 欄寫入鎖定的第一單內容，B 欄改已派出，C 欄寫入完整多行司機回報
                    sheet.update(f"A{target_row_idx}:C{target_row_idx}", [[a_column_content, "已派出", msg_text]])
                    print(f"🔄 [司機回報更新] 第 {target_row_idx} 列：A欄=[{a_column_content}]，B欄=已派出，C欄=多行回報")
                else:
                    # 原單不存在（補單）：寫入空行
                    sheet.update(f"A{first_empty_row}:C{first_empty_row}", [[a_column_content, "已派出", msg_text]])
                    print(f"🛠️ [司機回報補單] 第 {first_empty_row} 列：A欄=[{a_column_content}]，B欄=已派出，C欄=多行回報")
                return

            # --------------------------------------------------
            # 情境 B：管理員原始發單
            # --------------------------------------------------
            if target_row_idx:
                print(f"🔴 [重複原單] 單號 #{upper_code} 已存在於第 {target_row_idx} 列，不重複寫入！")
            else:
                order_type = "預約" if booking_time else "即時"
                # A 欄只寫入第一單的第一行內容
                sheet.update(f"A{first_empty_row}:F{first_empty_row}", [[a_column_content, "未派出", "", "", "", order_type]])
                print(f"⚡ [原始發單寫入] 第 {first_empty_row} 列：A欄=[{a_column_content}]，類型={order_type}")

        except Exception as e:
            print(f"❌ 處理單據時發生錯誤: {e}")

# ---------------------------------------------------------
# LIFF 網頁 API & 頁面路由
# ---------------------------------------------------------
@app.route('/api/get_unassigned', methods=['GET'])
def api_get_unassigned():
    try:
        sheet = get_sheet()
        records = sheet.get_all_values()

        unassigned_orders = []
        for row in records[1:]:
            if len(row) >= 2 and row[1] == "未派出":
                unassigned_orders.append(row[0])

        return jsonify({"orders": unassigned_orders})
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/liff')
def liff_page():
    return render_template('liff.html')

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)