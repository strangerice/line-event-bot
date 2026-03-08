from flask import Flask, request
import os
import json
import re
import requests
import gspread
from datetime import datetime, timedelta
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

app = Flask(__name__)

LINE_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS")
SHEET_NAME = "LINEイベントDB_V2"
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")

# ==============================
# Google 認証
# ==============================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar"
]

def get_google_credentials():
    credentials_info = json.loads(GOOGLE_CREDENTIALS)
    return Credentials.from_service_account_info(
        credentials_info,
        scopes=SCOPES
    )

def get_sheet():
    credentials = get_google_credentials()
    gc = gspread.authorize(credentials)
    return gc.open(SHEET_NAME).sheet1

def get_calendar_service():
    credentials = get_google_credentials()
    return build("calendar", "v3", credentials=credentials)

# ==============================
# シートヘッダー
# ==============================
HEADER = [
    "title",             # 0
    "date",              # 1 例: 2026/3/22
    "time",              # 2 例: 09:00 or 09:00-12:00 or ""
    "conversation_key",  # 3 例: group:Cxxx
    "target_id",         # 4 LINE push先
    "target_type",       # 5 user/group/room
    "sent_14",           # 6
    "sent_7",            # 7
    "sent_0",            # 8
    "sent_weekly",       # 9
    "calendar_link"      # 10
]

def normalize_row(row):
    row = row[:]
    while len(row) < len(HEADER):
        row.append("")
    return row

def ensure_header():
    sheet = get_sheet()
    values = sheet.get_all_values()

    if not values:
        sheet.append_row(HEADER)
        return

    for idx, name in enumerate(HEADER, start=1):
        sheet.update_cell(1, idx, name)

# 起動時ヘッダー確認
try:
    ensure_header()
except Exception as e:
    print("HEADER INIT ERROR =", str(e))

# ==============================
# 時刻・文字列処理
# ==============================
def now_jst():
    return datetime.utcnow() + timedelta(hours=9)

def today_jst():
    return now_jst().date()

def normalize_text(text):
    text = text.replace("\u3000", " ").strip()
    text = re.sub(r"\s+", " ", text)
    return text

def get_week_key(d=None):
    if d is None:
        d = today_jst()
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week}"

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

# ==============================
# 自然言語イベント解析
# ==============================
def parse_event_text(text):
    """
    順不同で以下を抽出
    - 日付: 2026/3/9, 3/9, 今日, 明日, 次の土曜
    - 時刻: 15:00, 15:00~20:00, 15:00-20:00, 19時
    - 残りをタイトル
    """

    text = normalize_text(text)
    now = now_jst()

    original_text = text

    date_str = None
    time_str = ""

    # -----------------------------
    # 1. 日付抽出
    # -----------------------------
    # YYYY/M/D
    m = re.search(r'(\d{4})/(\d{1,2})/(\d{1,2})', text)
    if m:
        year = int(m.group(1))
        month = int(m.group(2))
        day = int(m.group(3))
        date_str = f"{year}/{month}/{day}"
        text = text.replace(m.group(0), " ").strip()
    else:
        # M/D
        m = re.search(r'(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)', text)
        if m:
            month = int(m.group(1))
            day = int(m.group(2))
            year = now.year
            base_dt = datetime.strptime(f"{year}/{month}/{day}", "%Y/%m/%d")
            if base_dt.date() < now.date() - timedelta(days=1):
                year += 1
            date_str = f"{year}/{month}/{day}"
            text = text.replace(m.group(0), " ").strip()
        else:
            # 今日 / 明日
            if "今日" in text:
                d = now.date()
                date_str = f"{d.year}/{d.month}/{d.day}"
                text = text.replace("今日", " ").strip()
            elif "明日" in text:
                d = now.date() + timedelta(days=1)
                date_str = f"{d.year}/{d.month}/{d.day}"
                text = text.replace("明日", " ").strip()
            else:
                # 次の土曜
                weekdays = {
                    "月": 0, "火": 1, "水": 2, "木": 3,
                    "金": 4, "土": 5, "日": 6
                }
                m = re.search(r'次の([月火水木金土日])曜(?:日)?', text)
                if m:
                    target_weekday = weekdays[m.group(1)]
                    days_ahead = (target_weekday - now.weekday() + 7) % 7
                    if days_ahead == 0:
                        days_ahead = 7
                    d = now.date() + timedelta(days=days_ahead)
                    date_str = f"{d.year}/{d.month}/{d.day}"
                    text = text.replace(m.group(0), " ").strip()

    # -----------------------------
    # 2. 時刻抽出
    # -----------------------------
    # 15:00~20:00 / 15:00-20:00
    m = re.search(r'(\d{1,2}:\d{2})\s*[~〜\-]\s*(\d{1,2}:\d{2})', text)
    if m:
        time_str = f"{m.group(1)}-{m.group(2)}"
        text = text.replace(m.group(0), " ").strip()
    else:
        # 19時 / 19時30
        m = re.search(r'(\d{1,2})時(?:([0-5]?\d)分?)?', text)
        if m:
            hh = int(m.group(1))
            mm = m.group(2)
            mm = mm if mm else "00"
            time_str = f"{hh:02d}:{int(mm):02d}"
            text = text.replace(m.group(0), " ").strip()
        else:
            # 15:00
            m = re.search(r'(\d{1,2}:\d{2})', text)
            if m:
                time_str = m.group(1)
                text = text.replace(m.group(0), " ").strip()

    # -----------------------------
    # 3. タイトル抽出
    # -----------------------------
    title = normalize_text(text)

    if not date_str or not title:
        return None

    return {
        "title": title,
        "date": date_str,
        "time": time_str
    }

# ==============================
# データ取得
# ==============================
def get_data_rows():
    sheet = get_sheet()
    values = sheet.get_all_values()
    if len(values) <= 1:
        return []
    return [normalize_row(r) for r in values[1:]]

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
# Google Calendar 登録
# ==============================
def create_google_calendar_event(parsed):
    try:
        service = get_calendar_service()

        date_str = parsed["date"]
        time_str = parsed["time"]

        y, m, d = map(int, date_str.split("/"))

        if time_str:
            if "-" in time_str:
                start_time, end_time = time_str.split("-")
            else:
                start_time = time_str
                start_dt = datetime.strptime(f"{date_str} {start_time}", "%Y/%m/%d %H:%M")
                end_dt = start_dt + timedelta(hours=1)
                end_time = end_dt.strftime("%H:%M")

            start_dt = datetime.strptime(f"{date_str} {start_time}", "%Y/%m/%d %H:%M")
            end_dt = datetime.strptime(f"{date_str} {end_time}", "%Y/%m/%d %H:%M")

            event_body = {
                "summary": parsed["title"],
                "start": {
                    "dateTime": start_dt.isoformat(),
                    "timeZone": "Asia/Tokyo"
                },
                "end": {
                    "dateTime": end_dt.isoformat(),
                    "timeZone": "Asia/Tokyo"
                }
            }
        else:
            event_date = f"{y:04d}-{m:02d}-{d:02d}"
            next_day = (datetime(y, m, d) + timedelta(days=1)).strftime("%Y-%m-%d")

            event_body = {
                "summary": parsed["title"],
                "start": {"date": event_date},
                "end": {"date": next_day}
            }

        created = service.events().insert(calendarId=CALENDAR_ID, body=event_body).execute()
        return created.get("htmlLink", "")

    except Exception as e:
        print("CALENDAR ERROR =", str(e))
        return ""

# ==============================
# イベント登録
# ==============================
def register_event_from_parsed(parsed, conversation_key, target_id, target_type):
    try:
        sheet = get_sheet()

        rows = get_data_rows()
        for row in rows:
            row = normalize_row(row)
            if (
                row[0] == parsed["title"]
                and row[1] == parsed["date"]
                and row[2] == parsed["time"]
                and row[3] == conversation_key
            ):
                display_datetime = f"{parsed['date']} {parsed['time']}".strip()
                return f"すでに登録済みです\n{parsed['title']}\n{display_datetime}"

        calendar_link = create_google_calendar_event(parsed)

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
            "",
            calendar_link
        ]

        print("REGISTER conversation_key =", conversation_key)
        print("REGISTER ROW =", row)
        sheet.append_row(row)
        print("REGISTER DONE")

        display_datetime = f"{parsed['date']} {parsed['time']}".strip()

        if calendar_link:
            return (
                f"イベント登録しました\n"
                f"{parsed['title']}\n"
                f"{display_datetime}\n"
                f"Googleカレンダーにも登録しました"
            )

        return f"イベント登録しました\n{parsed['title']}\n{display_datetime}"

    except Exception as e:
        print("REGISTER ERROR =", str(e))
        return f"登録エラー: {str(e)}"

def register_event(text, conversation_key, target_id, target_type):
    parsed = parse_event_text(text)
    if not parsed:
        return (
            "予定として解釈できませんでした。\n"
            "例:\n"
            "3/22 越谷_C\n"
            "3/22 09:00 越谷_C\n"
            "3/22 09:00-12:00 越谷_C\n"
            "2026/3/22 09:00 越谷_C\n"
            "明日19時 飲み会\n"
            "次の土曜 13:00-17:00 越谷_C"
        )
    return register_event_from_parsed(parsed, conversation_key, target_id, target_type)

# ==============================
# 一覧
# ==============================
def list_events(conversation_key):
    rows = get_data_rows()

    my_rows = []
    for row in rows:
        row = normalize_row(row)
        if row[3] == conversation_key:
            my_rows.append(row)

    if not my_rows:
        return "イベントはありません"

    my_rows.sort(
        key=lambda r: datetime.strptime(
            f"{r[1]} {(r[2].split('-')[0] if r[2] else '00:00')}",
            "%Y/%m/%d %H:%M"
        )
    )

    lines = ["イベント一覧"]
    for idx, row in enumerate(my_rows, start=1):
        if row[2]:
            lines.append(f"{idx}. {row[0]} {row[1]} {row[2]}")
        else:
            lines.append(f"{idx}. {row[0]} {row[1]}")
    return "\n".join(lines)

# ==============================
# 削除
# ==============================
def delete_event(text, conversation_key):
    sheet = get_sheet()

    match = re.match(r"^削除\s+(\d+)$", text.strip())
    if not match:
        return "削除形式:\n削除 1"

    delete_no = int(match.group(1))

    all_values = sheet.get_all_values()
    if len(all_values) <= 1:
        return "削除できるイベントがありません"

    indexed_rows = []
    for sheet_row_no, row in enumerate(all_values[1:], start=2):
        row = normalize_row(row)
        if row[3] == conversation_key:
            indexed_rows.append((sheet_row_no, row))

    if delete_no < 1 or delete_no > len(indexed_rows):
        return "削除番号が正しくありません"

    sheet_row_no, row = indexed_rows[delete_no - 1]
    sheet.delete_rows(sheet_row_no)

    if row[2]:
        return f"イベント削除しました\n{row[0]} {row[1]} {row[2]}"
    return f"イベント削除しました\n{row[0]} {row[1]}"

# ==============================
# 今日 / 明日 / 今週
# ==============================
def filter_events_by_range(conversation_key, start_date, end_date, title):
    rows = get_data_rows()
    result = []

    for row in rows:
        row = normalize_row(row)
        if row[3] != conversation_key:
            continue

        try:
            event_date = datetime.strptime(row[1], "%Y/%m/%d").date()
        except Exception:
            continue

        if start_date <= event_date <= end_date:
            result.append(row)

    if not result:
        return f"{title}の予定はありません"

    result.sort(
        key=lambda r: datetime.strptime(
            f"{r[1]} {(r[2].split('-')[0] if r[2] else '00:00')}",
            "%Y/%m/%d %H:%M"
        )
    )

    lines = [f"{title}の予定"]
    for row in result:
        if row[2]:
            lines.append(f"- {row[0]} {row[1]} {row[2]}")
        else:
            lines.append(f"- {row[0]} {row[1]}")
    return "\n".join(lines)

def build_weekly_summary_for_target(conversation_key):
    start = today_jst()
    end = start + timedelta(days=6)

    rows = get_data_rows()
    result = []

    for row in rows:
        row = normalize_row(row)
        if row[3] != conversation_key:
            continue

        try:
            event_date = datetime.strptime(row[1], "%Y/%m/%d").date()
        except Exception:
            continue

        if start <= event_date <= end:
            result.append(row)

    if not result:
        return "【今週の予定】\n今週の予定はありません"

    result.sort(
        key=lambda r: datetime.strptime(
            f"{r[1]} {(r[2].split('-')[0] if r[2] else '00:00')}",
            "%Y/%m/%d %H:%M"
        )
    )

    lines = ["【今週の予定】"]
    for row in result:
        if row[2]:
            lines.append(f"- {row[0]} {row[1]} {row[2]}")
        else:
            lines.append(f"- {row[0]} {row[1]}")
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

        title, date_str, time_str, conversation_key, target_id, target_type, sent_14, sent_7, sent_0, sent_weekly, calendar_link = row

        try:
            event_date = datetime.strptime(date_str, "%Y/%m/%d").date()
        except Exception:
            continue

        days = (event_date - today).days
        display_datetime = f"{date_str} {time_str}" if time_str else date_str

        if days == 14 and sent_14 != "1":
            push(target_id, f"【2週間前リマインド】\n予定: {title}\n日時: {display_datetime}")
            update_sent_flag(sheet_row_no, 7)

        elif days == 7 and sent_7 != "1":
            push(target_id, f"【1週間前リマインド】\n予定: {title}\n日時: {display_datetime}")
            update_sent_flag(sheet_row_no, 8)

        elif days == 0 and sent_0 != "1":
            push(target_id, f"【本日9時リマインド】\n予定: {title}\n日時: {display_datetime}")
            update_sent_flag(sheet_row_no, 9)

def check_weekly_schedule():
    today = today_jst()

    if today.weekday() != 0:
        return

    week_key = get_week_key(today)
    rows = get_data_rows()

    conversations = {}
    for idx, row in enumerate(rows, start=2):
        row = normalize_row(row)

        conversation_key = row[3]
        target_id = row[4]

        if not conversation_key or not target_id:
            continue

        conversations.setdefault(conversation_key, []).append((idx, row))

    for conversation_key, row_items in conversations.items():
        already_sent = False
        target_id = row_items[0][1][4]

        for _, row in row_items:
            if row[9] == week_key:
                already_sent = True
                break

        if already_sent:
            continue

        message = build_weekly_summary_for_target(conversation_key)
        push(target_id, message)

        for sheet_row_no, _ in row_items:
            update_sent_flag(sheet_row_no, 10, week_key)

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
        text = normalize_text(event["message"]["text"])

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
            if text.startswith("登録 "):
                event_text = text.replace("登録 ", "", 1).strip()
                result = register_event(event_text, conversation_key, target_id, target_type)
            else:
                parsed = parse_event_text(text)
                if parsed:
                    result = register_event_from_parsed(parsed, conversation_key, target_id, target_type)
                else:
                    result = (
                        "予定として解釈できませんでした。\n"
                        "例:\n"
                        "3/22 越谷_C\n"
                        "3/22 09:00 越谷_C\n"
                        "3/22 09:00-12:00 越谷_C\n"
                        "2026/3/22 09:00 越谷_C\n"
                        "明日19時 飲み会\n"
                        "次の土曜 13:00-17:00 越谷_C"
                    )

        reply(reply_token, result)

    return "OK"

if __name__ == "__main__":
    app.run()