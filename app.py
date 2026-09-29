from flask import Flask, render_template, jsonify
import gspread
from google.oauth2.service_account import Credentials

app = Flask(__name__)

SHEET_NAME = "調度室派單管理表"

def get_sheet():
    scopes = ["https://www.googleapis.com/auth/spreadsheets", "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file('credentials.json', scopes=scopes)
    gs_client = gspread.authorize(creds)
    return gs_client.open(SHEET_NAME).sheet1

# 提供 LIFF 網頁讀取未派單據的 API
@app.route('/api/get_unassigned', methods=['GET'])
def api_get_unassigned():
    sheet = get_sheet()
    records = sheet.get_all_values()
    
    unassigned_orders = []
    for row in records[1:]:
        if len(row) >= 2 and row[1] == "未派出":
            unassigned_orders.append(row[0])

    return jsonify({"orders": unassigned_orders})

# 提供 LIFF 頁面
@app.route('/liff')
def liff_page():
    return render_template('liff.html')

if __name__ == '__main__':
    app.run(port=5000)