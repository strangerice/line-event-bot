from flask import Flask, request
import os
import json
import re
import requests
import gspread
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials

app = Flask(__name__)

LINE_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")
SHEET_NAME = "LINEイベントDB"

# ==============================
# Google Sheets 認証
# ==============================
scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

credentials_info = json.loads(GOOGLE_CREDENTIALS)
credentials = Credentials.from_service_account_info(
    credentials_info,
    scopes=scopes
)
gc = gspread.authorize(credentials)
sheet = gc.open(SHEET_NAME).sheet1

# 列定義
HEADER = [
    "title",        # A
    "date",         # B 例: 2026/3/20
    "time",         # C 例: 18:00
    "target_id",    # D userId / groupId / roomId
    "target_type",  # E user / group / room
    "sent_14",      # F 0/1
    "sent_7",       # G 0/1
    "sent_0"        # H 0/1
]


# ==============================
# 初期化
# ==============================
def ensure_header():
    values = sheet.get_all_values()
    if not values:
        sheet.append_row(HEADER)
    elif values[0] != HEADER:
        sheet.insert_row(HEADER, 1)


ensure_header()


# ==============================
# 共通
# ==============================
def today_jst():
    return datetime.utcnow() + timedelta(hours=9)


def get_target_info(event_source):
    source_type = event_source.get("type")

    if source_type == "user":
        return event_source.get("userId"), "user"

    if source_type == "group":
        return event_source.get("groupId"), "group"

    if source_type == "room":
        return event_source.get("roomId"), "room"

    return None, None


def parse_event_text(text):
    """
    例:
    3/20 18:00 歓送迎会
    """
    pattern = r"^\s*(\d{1,2})/(\d{1,2})\s+(\d{1,2}:\d{2})\s+(.+?)\s*$"
    match = re.match(pattern, text)

    if not match:
        return None

    month = int(match.group(1))
    day = int(match.group(2))
    time_str = match.group(3)
    title = match.group(4)

    now = today_jst()
    year = now.year

    # 今年の日付として作り、過去なら翌年に回す
    event_dt = datetime.strptime(f"{year}/{month}/{day} {time_str}", "%Y/%m/%d %H:%M")
    if event_dt < now - timedelta(days=1):
        year += 1

    date_str = f"{year}/{month}/{day}"
    return {
        "title": title,
        "date": date_str,
        "time": time_str
    }


def reply(token, text):
    url = "https://api.line.me/v2/bot/message/reply"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_TOKEN}"
    }
    data = {
        "replyToken": token,
        "messages": [{"type": "text", "text": text}]
    }
    requests.post(url, headers=headers, json=data, timeout=15)


def push(to, text):
    url = "https://api.line.me/v2/bot/message/push"
    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_TOKEN}"
    }
    data = {
        "to": to,
        "messages": [{"type": "text", "text": text}]
    }
    requests.post(url, headers=headers, json=data, timeout=15)


def get_data_rows():
    """
    ヘッダーを除いた全データを返す
    """
    values = sheet.get_all_values()
    if len(values) <= 1:
        return []
    return values[1:]


# ==============================
# イベント登録
# ==============================
def register_event(text, target_id, target_type):
    parsed = parse_event_text(text)
    if not parsed:
        return "イベント形式:\n3/20 18:00 歓送迎会"

    row = [
        parsed["title"],
        parsed["date"],
        parsed["time"],
        target_id,
        target_type,
        "0",
        "0",
        "0"
    ]
    sheet.append_row(row)

    return f"イベント登録しました\n{parsed['title']} {parsed['date']} {parsed['time']}"


# ==============================
# 一覧表示
# ==============================
def list_events(target_id):
    rows = get_data_rows()
    my_rows = [row for row in rows if len(row) >= 4 and row[3] == target_id]

    if not my_rows:
        return "イベントはありません"

    # 日時順に並べる
    my_rows.sort(key=lambda r: datetime.strptime(f"{r[1]} {r[2]}", "%Y/%m/%d %H:%M"))

    message_lines = ["イベント一覧"]
    for idx, row in enumerate(my_rows, start=1):
        message_lines.append(f"{idx}. {row[0]} {row[1]} {row[2]}")

    return "\n".join(message_lines)


# ==============================
# 削除
# ==============================
def delete_event(text, target_id):
    match = re.match(r"^削除\s+(\d+)$", text.strip())
    if not match:
        return "削除形式:\n削除 1"

    delete_no = int(match.group(1))

    all_values = sheet.get_all_values()
    if len(all_values) <= 1:
        return "削除できるイベントがありません"

    # シート上の行番号を保持
    indexed_rows = []
    for sheet_row_no, row in enumerate(all_values[1:], start=2):
        if len(row) >= 4 and row[3] == target_id:
            indexed_rows.append((sheet_row_no, row))

    if delete_no < 1 or delete_no > len(indexed_rows):
        return "削除番号が正しくありません"

    sheet_row_no, row = indexed_rows[delete_no - 1]
    sheet.delete_rows(sheet_row_no)

    return f"イベント削除しました\n{row[0]} {row[1]} {row[2]}"


# ==============================
# 今日 / 明日 / 今週
# ==============================
def filter_events_by_range(target_id, start_date, end_date, title):
    rows = get_data_rows()
    result = []

    for row in rows:
        if len(row) < 4 or row[3] != target_id:
            continue

        try:
            event_date = datetime.strptime(row[1], "%Y/%m/%d").date()
        except Exception:
            continue

        if start_date <= event_date <= end_date:
            result.append(row)

    if not result:
        return f"{title}の予定はありません"

    result.sort(key=lambda r: datetime.strptime(f"{r[1]} {r[2]}", "%Y/%m/%d %H:%M"))
    lines = [f"{title}の予定"]
    for row in result:
        lines.append(f"- {row[0]} {row[1]} {row[2]}")
    return "\n".join(lines)


# ==============================
# リマインド
# ==============================
def update_sent_flag(sheet_row_no, col_index):
    # col_index: 6(F) / 7(G) / 8(H)
    sheet.update_cell(sheet_row_no, col_index, "1")


def check_reminders():
    all_values = sheet.get_all_values()
    if len(all_values) <= 1:
        return "ok"

    today = today_jst().date()

    # 2行目以降がデータ
    for sheet_row_no, row in enumerate(all_values[1:], start=2):
        if len(row) < 8:
            continue

        title, date_str, time_str, target_id, target_type, sent_14, sent_7, sent_0 = row[:8]

        try:
            event_date = datetime.strptime(date_str, "%Y/%m/%d").date()
        except Exception:
            continue

        days = (event_date - today).days

        if days == 14 and sent_14 != "1":
            push(target_id, f"【2週間前リマインド】\n{title}\n{date_str} {time_str}")
            update_sent_flag(sheet_row_no, 6)

        elif days == 7 and sent_7 != "1":
            push(target_id, f"【1週間前リマインド】\n{title}\n{date_str} {time_str}")
            update_sent_flag(sheet_row_no, 7)

        elif days == 0 and sent_0 != "1":
            push(target_id, f"【本日9時リマインド】\n{title}\n{date_str} {time_str}")
            update_sent_flag(sheet_row_no, 8)

    return "ok"


# ==============================
# ルート
# ==============================
@app.route("/")
def home():
    return "bot running"


@app.route("/cron")
def cron():
    try:
        result = check_reminders()
        return result
    except Exception as e:
        return f"error: {str(e)}", 500


@app.route("/webhook", methods=["POST"])
def webhook():
    body = request.json

    for event in body.get("events", []):
        if event.get("type") != "message":
            continue
        if event.get("message", {}).get("type") != "text":
            continue

        reply_token = event.get("replyToken")
        text = event["message"]["text"].strip()

        target_id, target_type = get_target_info(event.get("source", {}))
        if not target_id:
            continue

        if text == "一覧":
            result = list_events(target_id)

        elif text.startswith("削除"):
            result = delete_event(text, target_id)

        elif text == "今日":
            d = today_jst().date()
            result = filter_events_by_range(target_id, d, d, "今日")

        elif text == "明日":
            d = today_jst().date() + timedelta(days=1)
            result = filter_events_by_range(target_id, d, d, "明日")

        elif text == "今週":
            today = today_jst().date()
            end = today + timedelta(days=6)
            result = filter_events_by_range(target_id, today, end, "今週")

        else:
            result = register_event(text, target_id, target_type)

        reply(reply_token, result)

    return "OK"


if __name__ == "__main__":
    app.run()