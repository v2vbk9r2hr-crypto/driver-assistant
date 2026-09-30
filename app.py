import os
import json
import base64
import re
from flask import Flask, render_template, jsonify, request, abort
import gspread
from google.oauth2.service_account import Credentials
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, TextSendMessage

app = Flask(__name__)

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
    
    # 支援 Render / 雲端環境變數 (Base64 或 raw JSON) 或 本地 credentials.json
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
    """清理地址內容，移除單號、時間數字、備註與 Emoji"""
    t = text
    t = re.sub(r'\[.*?\]', '', t)                          # 移除 [中括號]
    t = re.sub(r'#[a-zA-Z0-9/／]+', '', t)                 # 移除 #開頭單號
    t = re.sub(r'\d{1,2}[\.:：點]\d{2}?', '', t)             # 移除時間數字如 21.46 或 22.20
    t = re.sub(r'(轉帳|改兩台|客下街口|\+\d+)', '', t)       # 移除常見備註
    t = re.sub(r'[^\w\u4e00-\u9fa5]', '', t)                 # 移除標點符號與 Emoji
    return t.strip()

def parse_order_info(text):
    """
    從訊息第一行解析出：單號、預約時間、核心地址
    支援中文單號（如 #店77/、#ST/、#94/）
    """
    first_line = text.splitlines()[0].strip() if text else ""
    
    # 抓取 # 後面到 / 之間的單號 (支援英數、中文，如 店77, ST, 94, Y)
    code_match = re.search(r'#([a-zA-Z0-9\u4e00-\u9fa5]+)', first_line)
    order_code = code_match.group(1) if code_match else None

    # 抓取預約時間 (例: 12:30 或 1230)
    time_match = re.search(r'(\d{1,2}:\d{2}|\d{4})', first_line)
    booking_time = time_match.group(1) if time_match else None

    # 抓取地址核心 (去除表情符號與特殊字)
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

        # 必須包含 # 才處理
        if '#' not in msg_text:
            return

        # 拆分非空白行
        lines = [line.strip() for line in msg_text.splitlines() if line.strip()]

        # ---------------------------------------------------------
        # 【超級鐵壁判斷】是否為司機回報訊息：
        # 條件 1: 行數大於 1 行 (原單1行 + 司機回報1~2行)
        # 條件 2: 包含 4 位數字車號 (如 8051, 0273, 2507, 3691, 5791, 0990)
        # 條件 3: 包含司機動作/狀態關鍵字
        # ---------------------------------------------------------
        has_car_num = bool(re.search(r'\d{4}', msg_text))
        has_status_kw = bool(re.search(r'(到|上|下|收|取消|抵達|客上|客到|代駕|紙煙|紙菸|\d{1,2}\s*(分鐘|分|min))', msg_text))

        is_driver_report = (len(lines) > 1) or has_car_num or has_status_kw

        order_code, booking_time, core_address = parse_order_info(msg_text)

        try:
            sheet = get_sheet()
            records = sheet.get_all_values()

            target_row_idx = None

            # 搜尋試算表中是否有對應的原單
            for idx, row in enumerate(records[1:], start=2): # 從第 2 行開始 (比對 A 欄)
                if not row or not row[0]:
                    continue
                ex_text = row[0]
                ex_code, ex_time, ex_address = parse_order_info(ex_text)

                # 單號相同即匹配 (例如 店77 == 店77, 94 == 94)
                if order_code and ex_code and order_code.upper() == ex_code.upper():
                    if core_address and ex_address:
                        # 比對地址包含度
                        if (core_address in ex_address) or (ex_address in core_address):
                            target_row_idx = idx
                            break
                    else:
                        target_row_idx = idx
                        break

            # --------------------------------------------------
            # 情境 A：這是司機的回報 / 搶單 / 進度訊息
            # --------------------------------------------------
            if is_driver_report:
                if target_row_idx:
                    # 成功找到原單：更新 B 欄為「已派出」，C 欄寫入司機回報
                    sheet.update_cell(target_row_idx, 2, "已派出")  # B欄
                    sheet.update_cell(target_row_idx, 3, msg_text)   # C欄
                    print(f"🔄 [司機回報成功] 第 {target_row_idx} 列更新為【已派出】，C欄已紀錄。")
                else:
                    # 找不到原單：絕對直接 return 結束，絕不寫入 A 欄！
                    print(f"🛑 [強行攔截] 單號 #{order_code} 判定為司機回報，但 A 欄找不到原單，直接丟棄不寫入！")
                
                return # 司機訊息處理完畢，強制離開

            # --------------------------------------------------
            # 情境 B：這是管理員發出的「純原始單據」（僅限 1 行且無司機關鍵字）
            # --------------------------------------------------
            if target_row_idx:
                print(f"🔴 [重複原單] 單號 #{order_code} 已存在於第 {target_row_idx} 列，跳過！")
            else:
                # 寫入全新單據至 A 欄
                sheet.append_row([msg_text, "未派出"])
                print(f"⚡ [全新原單寫入] 成功新增至 A 欄: {msg_text}")

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