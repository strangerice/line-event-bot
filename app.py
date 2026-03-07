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
SHEET_NAME = "LINEイベントDB_V2"

# ==============================
# Google Sheets 認証
# ==============================
scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

def get_sheet():
    credentials_info = json.loads(GOOGLE_CREDENTIALS)
    credentials = Credentials.from_service_account_info(
        credentials_info,
        scopes=scopes
    )
    gc = gspread.authorize(credentials)
    return gc.open(SHEET_NAME).sheet1

# ==============================
# シートヘッダー
# ==============================
HEADER = [
    "title",
    "date",
    "time",
    "conversation_key",
    "target_id",
    "target_type",
    "sent_14",
    "sent_7",
    "sent_0",
    "sent_weekly"
]

# ==============================
# 初期化
# ==============================
def ensure_header():
    sheet = get_sheet()
    values = sheet.get_all_values()

    if not values:
        sheet.append_row(HEADER)
        return

    # 1行目が違ったら強制的に上書き
    for idx, name in enumerate(HEADER, start=1):
        sheet.update_cell(1, idx, name)

ensure_header()

# ==============================
# 共通
# ==============================
def now_jst():
    return datetime.utcnow() + timedelta(hours=9)

def today_jst():
    return now_jst().date()

def get_target_info(event_source):
    source_type = event_source.get("type")

    if source_type == "user":
        target_id = event_source.get("userId")
        return target_id, "user", f"user:{target_id}"

    if source_type == "group":
        target_id = event_source.get("groupId")
        return target_id, "group", f"group:{target_id}"

    if source_type == "room":
        target_id = event_source.get("roomId")
        return target_id, "room", f"room:{target_id}"

    return None, None, None

def normalize_row(row):
    """
    行の列数不足を補う
    """
    row = row[:]
    while len(row) < len(HEADER):
        row.append("")
    return row

def parse_event_text(text):
    """
    対応例:
    3/20 18:00 歓送迎会
    3/20 18:00-20:00 歓送迎会
    """
    pattern = r"^\s*(\d{1,2})/(\d{1,2})\s+(\d{1,2}:\d{2}(?:-\d{1,2}:\d{2})?)\s+(.+?)\s*$"
    match = re.match(pattern, text)

    if not match:
        return None

    month = int(match.group(1))
    day = int(match.group(2))
    time_str = match.group(3)
    title = match.group(4)

    # 開始時刻だけ年判定に使う
    start_time = time_str.split("-")[0]

    year = now_jst().year
    event_dt = datetime.strptime(f"{year}/{month}/{day} {start_time}", "%Y/%m/%d %H:%M")

    if event_dt < now_jst() - timedelta(days=1):
        year += 1

    date_str = f"{year}/{month}/{day}"

    return {
        "title": title,
        "date": date_str,
        "time": time_str
    }

def get_data_rows():
    sheet = get_sheet()
    values = sheet.get_all_values()
    if len(values) <= 1:
        return []
    return [normalize_row(r) for r in values[1:]]

def get_week_key(d=None):
    if d is None:
        d = today_jst()
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week}"

# ==============================
# LINE送信
# ==============================
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

# ==============================
# イベント登録
# ==============================
def register_event(text, conversation_key, target_id, target_type):
    try:
        sheet = get_sheet()

        parsed = parse_event_text(text)
        if not parsed:
            return "イベント形式:\n3/20 18:00 歓送迎会\nまたは\n3/20 18:00-20:00 歓送迎会"

        rows = get_data_rows()
        for row in rows:
            row = normalize_row(row)
            if (
                row[0] == parsed["title"]
                and row[1] == parsed["date"]
                and row[2] == parsed["time"]
                and row[3] == conversation_key
            ):
                print("REGISTER DUPLICATE conversation_key =", conversation_key)
                return f"すでに登録済みです\n{parsed['title']} {parsed['date']} {parsed['time']}"

        row = [
            parsed["title"],
            parsed["date"],
            parsed["time"],
            conversation_key,
            target_id,
            target_type,
            "0",
            "0",
            "0",
            ""
        ]

        print("REGISTER conversation_key =", conversation_key)
        print("REGISTER ROW =", row)
        sheet.append_row(row)
        print("REGISTER DONE")

        return f"イベント登録しました\n{parsed['title']} {parsed['date']}\n{parsed['time']}"

    except Exception as e:
        print("REGISTER ERROR =", str(e))
        return f"登録エラー: {str(e)}"

# ==============================
# 一覧
# ==============================
def list_events(conversation_key):
    rows = get_data_rows()

    print("LIST conversation_key =", conversation_key)
    print("LIST all rows =", rows)

    my_rows = []
    for row in rows:
        row = normalize_row(row)
        if row[3] == conversation_key:
            my_rows.append(row)

    print("LIST matched rows =", my_rows)

    if not my_rows:
        return "イベントはありません"

    my_rows.sort(
        key=lambda r: datetime.strptime(
            f"{r[1]} {r[2].split('-')[0]}",
            "%Y/%m/%d %H:%M"
        )
    )

    lines = ["イベント一覧"]
    for idx, row in enumerate(my_rows, start=1):
        lines.append(f"{idx}. {row[0]} {row[1]} {row[2]}")

    return "\n".join(lines)

# ==============================
# 削除
# ==============================
def delete_event(text, target_id):
    sheet = get_sheet()
    match = re.match(r"^削除\s+(\d+)$", text.strip())
    if not match:
        return "削除形式:\n削除 1"

    delete_no = int(match.group(1))

    sheet = get_sheet()
    values = sheet.get_all_values()
    if len(all_values) <= 1:
        return "削除できるイベントがありません"

    indexed_rows = []
    for sheet_row_no, row in enumerate(all_values[1:], start=2):
        row = normalize_row(row)
        if row[3] == target_id:
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
        if row[3] != target_id:
            continue

        try:
            event_date = datetime.strptime(row[1], "%Y/%m/%d").date()
        except Exception:
            continue

        if start_date <= event_date <= end_date:
            result.append(row)

    if not result:
        return f"{title}の予定はありません"

    result.sort(key=lambda r: datetime.strptime(f"{r[1]} {r[2].split('-')[0]}", "%Y/%m/%d %H:%M"))

    lines = [f"{title}の予定"]
    for row in result:
        lines.append(f"- {row[0]} {row[1]} {row[2]}")

    return "\n".join(lines)

def build_weekly_summary_for_target(target_id):
    start = today_jst()
    end = start + timedelta(days=6)

    rows = get_data_rows()
    result = []

    for row in rows:
        if row[3] != target_id:
            continue

        try:
            event_date = datetime.strptime(row[1], "%Y/%m/%d").date()
        except Exception:
            continue

        if start <= event_date <= end:
            result.append(row)

    if not result:
        return "【今週の予定】\n今週の予定はありません"

    result.sort(key=lambda r: datetime.strptime(f"{r[1]} {r[2].split('-')[0]}", "%Y/%m/%d %H:%M"))

    lines = ["【今週の予定】"]
    for row in result:
        lines.append(f"- {row[0]} {row[1]} {row[2]}")

    return "\n".join(lines)

# ==============================
# リマインド
# ==============================
def update_sent_flag(sheet_row_no, col_index, value="1"):
    sheet = get_sheet()
    sheet.update_cell(sheet_row_no, col_index, value)

def check_daily_reminders():
    sheet = get_sheet()
    all_values = sheet.get_all_values()
    
    if len(all_values) <= 1:
        return

    today = today_jst()

    for sheet_row_no, row in enumerate(all_values[1:], start=2):
        row = normalize_row(row)

        title, date_str, time_str, target_id, target_type, sent_14, sent_7, sent_0, sent_weekly = row

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

def check_weekly_schedule():
    today = today_jst()

    # 月曜だけ送る
    # Monday = 0
    if today.weekday() != 0:
        return

    week_key = get_week_key(today)
    rows = get_data_rows()

    targets = {}
    for idx, row in enumerate(rows, start=2):
        row = normalize_row(row)
        target_id = row[3]
        if not target_id:
            continue
        targets.setdefault(target_id, []).append((idx, row))

    for target_id, row_items in targets.items():
        # そのターゲットですでに今週送っていれば送らない
        already_sent = False
        for _, row in row_items:
            if row[8] == week_key:
                already_sent = True
                break

        if already_sent:
            continue

        message = build_weekly_summary_for_target(target_id)
        push(target_id, message)

        # そのターゲットの全行に今週キーを記録
        for sheet_row_no, _ in row_items:
            update_sent_flag(sheet_row_no, 9, week_key)

def check_reminders():
    check_daily_reminders()
    check_weekly_schedule()
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

        target_id, target_type, conversation_key = get_target_info(event.get("source", {}))
        if not target_id or not conversation_key:
            continue

        if text == "一覧":
            result = list_events(conversation_key)

        elif text.startswith("削除"):
            result = delete_event(text, conversation_key)

        elif text == "今日":
            d = today_jst()
            result = filter_events_by_range(conversation_key, d, d, "今日")

        elif text == "明日":
            d = today_jst() + timedelta(days=1)
            result = filter_events_by_range(conversation_key, d, d, "明日")

        elif text == "今週":
            start = today_jst()
            end = start + timedelta(days=6)
            result = filter_events_by_range(conversation_key, start, end, "今週")

        else:
            if target_type in ["group", "room"]:
                if text.startswith("登録 "):
                    event_text = text.replace("登録 ", "", 1).strip()
                    result = register_event(event_text, conversation_key, target_id, target_type)
                else:
                    result = (
                        "グループでは次の形式で登録してください\n"
                        "登録 3/20 18:00 歓送迎会\n"
                        "登録 3/20 18:00-20:00 歓送迎会\n\n"
                        "使えるコマンド:\n"
                        "一覧\n削除 1\n今日\n明日\n今週"
                    )
            else:
                result = register_event(text, conversation_key, target_id, target_type)

        reply(reply_token, result)

    return "OK"

if __name__ == "__main__":
    app.run()