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
    解析訊息中的單號、預約時間與核心地址
    例如: '#Y/ 21.46台中高鐵站🐈' -> order_code='Y/', booking_time='21.46', core_address='台中高鐵站'
    例如: '#66/ 台中機場進🐈' -> order_code='66/', booking_time='', core_address='台中機場進'
    """
    # 抓取 # 開頭的代號 (包含斜線)
    code_match = re.search(r'#([a-zA-Z0-9/／]+)', text)
    order_code = None
    if code_match:
        raw_code = code_match.group(1).upper()
        if '/' in raw_code:
            order_code = raw_code.split('/')[0] + '/'
        else:
            order_code = raw_code

    # 抓取預約時間 (例如 21.46, 22.20, 21.45)
    time_match = re.search(r'(\d{1,2}[\.:：]\d{2})', text)
    booking_time = time_match.group(1) if time_match else ""

    # 提取清洗後的地址
    core_address = extract_core_address(text)

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

        order_code, booking_time, core_address = parse_order_info(msg_text)

        if not order_code:
            return

        # ---------------------------------------------------------
        # 更全面的司機回報判定邏輯：
        # 1. 包含車號+顏色 (例: 3506橘, 9807白)
        # 2. 包含分鐘數 (例: 8分鐘, 5分)
        # 3. 包含狀態字詞 (例: 到, 上, 下, 收, 取消)
        # ---------------------------------------------------------
        is_driver_report = (
            bool(re.search(r'\d{3,4}[\u4e00-\u9fa5a-zA-Z]', msg_text)) or  # 車號+車色/英文
            bool(re.search(r'\d{1,2}\s*(分鐘|分|min)', msg_text)) or        # 幾分鐘/幾分
            bool(re.search(r'(到|上|下|收|取消)\s*$', msg_text))           # 結尾帶到/上/下
        )

        try:
            sheet = get_sheet()
            records = sheet.get_all_values()

            target_row_idx = None

            # 尋找試算表中是否已有對應的原單
            for idx, row in enumerate(records[1:], start=2): # 從第 2 行開始
                if not row or not row[0]:
                    continue
                ex_text = row[0]
                ex_code, ex_time, ex_address = parse_order_info(ex_text)

                if order_code and ex_code and order_code == ex_code:
                    if booking_time and ex_time:
                        if booking_time == ex_time and (core_address in ex_address or ex_address in core_address):
                            target_row_idx = idx
                            break
                    else:
                        if core_address and ex_address and (core_address in ex_address or ex_address in core_address):
                            target_row_idx = idx
                            break

            # --------------------------------------------------
            # 情境 A：這是司機的回報訊息
            # --------------------------------------------------
            if is_driver_report:
                if target_row_idx:
                    # 找到對應原單！更新 B 欄為「已派出」，將司機搶單回報寫入 C 欄
                    sheet.update_cell(target_row_idx, 2, "已派出") # B欄
                    sheet.update_cell(target_row_idx, 3, msg_text)  # C欄
                    print(f"🔄 [司機搶單更新] 第 {target_row_idx} 列已更新為【已派出】，C欄已寫入司機回報！")
                else:
                    print(f"⚠️ 找不到對應原單，無法更新司機回報: {msg_text}")
                return

            # --------------------------------------------------
            # 情境 B：這是派單人員發出的全新單據
            # --------------------------------------------------
            if target_row_idx:
                print(f"🔴 [重複單號跳過] 單號 #{order_code} 已存在，跳過不寫入！")
            else:
                # 寫入新單 (A欄: 派單內容, B欄: 未派出)
                sheet.append_row([msg_text, "未派出"])
                print(f"⚡ [全新單號寫入] 新增至 Sheet: {msg_text}")

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