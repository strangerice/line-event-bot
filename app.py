from flask import Flask, request, abort
import os
import json
import re
import requests
import gspread
import logging
import hashlib
import hmac
import base64
from datetime import datetime, timedelta, timezone
from google.oauth2.service_account import Credentials
from googleapiclient.discovery import build

app = Flask(__name__)

# ==============================
# Logging
# ==============================
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# ==============================
# Env
# ==============================
LINE_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN", "")
LINE_CHANNEL_SECRET = os.getenv("CHANNEL_SECRET", "")
GOOGLE_CREDENTIALS = os.getenv("GOOGLE_CREDENTIALS", "")
SHEET_NAME = os.getenv("SHEET_NAME", "LINEイベントDB_V3")
CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")

JST = timezone(timedelta(hours=9))

# ==============================
# Google Auth / Cache
# ==============================
SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/calendar",
]

_google_credentials = None
_gspread_client = None
_calendar_service = None


def get_google_credentials():
    global _google_credentials

    if _google_credentials is None:
        if not GOOGLE_CREDENTIALS:
            raise RuntimeError("GOOGLE_CREDENTIALS is empty")

        credentials_info = json.loads(GOOGLE_CREDENTIALS)
        _google_credentials = Credentials.from_service_account_info(
            credentials_info,
            scopes=SCOPES
        )
    return _google_credentials


def get_gspread_client():
    global _gspread_client
    if _gspread_client is None:
        _gspread_client = gspread.authorize(get_google_credentials())
    return _gspread_client


def get_sheet():
    gc = get_gspread_client()
    return gc.open(SHEET_NAME).sheet1


def get_calendar_service():
    global _calendar_service
    if _calendar_service is None:
        _calendar_service = build(
            "calendar",
            "v3",
            credentials=get_google_credentials(),
            cache_discovery=False
        )
    return _calendar_service


# ==============================
# Sheet Header
# ==============================
HEADER = [
    "title",              # 0
    "date",               # 1
    "time",               # 2
    "location",           # 3
    "conversation_key",   # 4
    "target_id",          # 5
    "target_type",        # 6
    "sent_14",            # 7
    "sent_7",             # 8
    "sent_0",             # 9
    "sent_weekly",        # 10
    "calendar_link",      # 11
    "calendar_event_id",  # 12
    "created_at",         # 13
    "updated_at",         # 14
    "raw_text",           # 15
]


def normalize_row(row):
    row = row[:]
    while len(row) < len(HEADER):
        row.append("")
    return row[:len(HEADER)]


def ensure_header():
    sheet = get_sheet()
    values = sheet.get_all_values()

    if not values:
        sheet.append_row(HEADER)
        logger.info("Header created")
        return

    current_header = values[0]
    if current_header[:len(HEADER)] != HEADER:
        sheet.update(f"A1:{column_letter(len(HEADER))}1", [HEADER])
        logger.info("Header ensured/updated")


def column_letter(n: int) -> str:
    result = ""
    while n > 0:
        n, rem = divmod(n - 1, 26)
        result = chr(65 + rem) + result
    return result


# ==============================
# Time / Text Utils
# ==============================
def now_jst():
    return datetime.now(JST)


def today_jst():
    return now_jst().date()


def normalize_text(text: str) -> str:
    if not text:
        return ""
    text = text.replace("\u3000", " ")
    text = text.replace("　", " ")
    text = text.replace("～", "~")
    text = text.replace("〜", "~")
    text = text.replace("—", "-")
    text = text.replace("−", "-")
    text = text.replace("ー", "ー")  # preserve katakana long bar
    text = re.sub(r"\s+", " ", text).strip()
    return text


def get_week_key(d=None):
    if d is None:
        d = today_jst()
    iso = d.isocalendar()
    return f"{iso.year}-W{iso.week}"


def format_display_datetime(date_str: str, time_str: str) -> str:
    return f"{date_str} {time_str}".strip()


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


def parse_date_str(date_str: str):
    return datetime.strptime(date_str, "%Y/%m/%d").date()


def sort_key_from_row(row):
    row = normalize_row(row)
    date_str = row[1]
    time_str = row[2]
    start_time = "00:00"
    if time_str:
        start_time = time_str.split("-")[0]
    return datetime.strptime(f"{date_str} {start_time}", "%Y/%m/%d %H:%M")


def build_event_line(row, prefix=""):
    row = normalize_row(row)
    location_text = f" / {row[3]}" if row[3] else ""
    if row[2]:
        return f"{prefix}{row[0]} {row[1]} {row[2]}{location_text}"
    return f"{prefix}{row[0]} {row[1]}{location_text}"


# ==============================
# LINE Signature Verify
# ==============================
def verify_line_signature(body_text: str, signature: str) -> bool:
    if not LINE_CHANNEL_SECRET:
        logger.warning("CHANNEL_SECRET is empty, signature verification skipped")
        return True

    if not signature:
        return False

    digest = hmac.new(
        LINE_CHANNEL_SECRET.encode("utf-8"),
        body_text.encode("utf-8"),
        hashlib.sha256
    ).digest()
    expected_signature = base64.b64encode(digest).decode("utf-8")
    return hmac.compare_digest(expected_signature, signature)


# ==============================
# Natural Language Parser
# ==============================
def parse_event_text(text):
    """
    抽出対象:
    - 日付:
        2026/3/9
        3/9
        今日
        明日
        次の土曜 / 次の土曜日
    - 時刻:
        15:00
        15:00-20:00
        15:00~20:00
        19時
        19時30分
    - 場所:
        @渋谷
        場所:渋谷
        場所：渋谷
    - 残りをタイトル
    """

    original_text = text
    text = normalize_text(text)
    now = now_jst()

    if not text:
        return None

    date_str = None
    time_str = ""
    location = ""

    # -----------------------------
    # 0. 明示場所抽出
    # -----------------------------
    m = re.search(r'@([^\s]+)', text)
    if m:
        location = m.group(1).strip()
        text = text.replace(m.group(0), " ")

    if not location:
        m = re.search(r'場所[:：]\s*([^\s]+)', text)
        if m:
            location = m.group(1).strip()
            text = text.replace(m.group(0), " ")

    text = normalize_text(text)

    # -----------------------------
    # 1. 日付抽出
    # -----------------------------
    m = re.search(r'(\d{4})/(\d{1,2})/(\d{1,2})', text)
    if m:
        year = int(m.group(1))
        month = int(m.group(2))
        day = int(m.group(3))
        try:
            _ = datetime(year, month, day)
            date_str = f"{year}/{month}/{day}"
            text = text.replace(m.group(0), " ")
        except ValueError:
            return None
    else:
        m = re.search(r'(?<!\d)(\d{1,2})/(\d{1,2})(?!\d)', text)
        if m:
            month = int(m.group(1))
            day = int(m.group(2))
            year = now.year
            try:
                candidate = datetime(year, month, day).date()
            except ValueError:
                return None

            # 昨日より前なら翌年扱い
            if candidate < now.date() - timedelta(days=1):
                year += 1
                try:
                    candidate = datetime(year, month, day).date()
                except ValueError:
                    return None

            date_str = f"{candidate.year}/{candidate.month}/{candidate.day}"
            text = text.replace(m.group(0), " ")
        else:
            if "今日" in text:
                d = now.date()
                date_str = f"{d.year}/{d.month}/{d.day}"
                text = text.replace("今日", " ")
            elif "明日" in text:
                d = now.date() + timedelta(days=1)
                date_str = f"{d.year}/{d.month}/{d.day}"
                text = text.replace("明日", " ")
            else:
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
                    text = text.replace(m.group(0), " ")

    text = normalize_text(text)

    # -----------------------------
    # 2. 時刻抽出
    # 優先: 範囲 -> 単時刻
    # -----------------------------
    # 15:00-20:00 / 15:00~20:00
    m = re.search(r'(\d{1,2}:\d{2})\s*[-~]\s*(\d{1,2}:\d{2})', text)
    if m:
        start_h, start_m = map(int, m.group(1).split(":"))
        end_h, end_m = map(int, m.group(2).split(":"))
        if (
            0 <= start_h <= 23 and 0 <= start_m <= 59 and
            0 <= end_h <= 23 and 0 <= end_m <= 59
        ):
            time_str = f"{start_h:02d}:{start_m:02d}-{end_h:02d}:{end_m:02d}"
            text = text.replace(m.group(0), " ")
    else:
        # 15時-20時 / 15時30分-20時
        m = re.search(
            r'(\d{1,2})時(?:([0-5]?\d)分?)?\s*[-~]\s*(\d{1,2})時(?:([0-5]?\d)分?)?',
            text
        )
        if m:
            sh = int(m.group(1))
            sm = int(m.group(2)) if m.group(2) else 0
            eh = int(m.group(3))
            em = int(m.group(4)) if m.group(4) else 0
            if (
                0 <= sh <= 23 and 0 <= sm <= 59 and
                0 <= eh <= 23 and 0 <= em <= 59
            ):
                time_str = f"{sh:02d}:{sm:02d}-{eh:02d}:{em:02d}"
                text = text.replace(m.group(0), " ")
        else:
            # 19時 / 19時30分
            m = re.search(r'(\d{1,2})時(?:([0-5]?\d)分?)?', text)
            if m:
                hh = int(m.group(1))
                mm = int(m.group(2)) if m.group(2) else 0
                if 0 <= hh <= 23 and 0 <= mm <= 59:
                    time_str = f"{hh:02d}:{mm:02d}"
                    text = text.replace(m.group(0), " ")
            else:
                # 15:00
                m = re.search(r'(\d{1,2}:\d{2})', text)
                if m:
                    hh, mm = map(int, m.group(1).split(":"))
                    if 0 <= hh <= 23 and 0 <= mm <= 59:
                        time_str = f"{hh:02d}:{mm:02d}"
                        text = text.replace(m.group(0), " ")

    text = normalize_text(text)

    # -----------------------------
    # 3. タイトル抽出
    # -----------------------------
    title = text.strip()

    # 場所自動推定はやりすぎると誤判定しやすいので弱める
    # どうしても必要なケースのみ:
    # 例 "3/22 TEST_A 越谷C" のように 2語以上残っていて、
    # 明示場所が無い場合は末尾1語を場所候補にする
    if title and not location:
        parts = title.split(" ")
        if len(parts) >= 2:
            maybe_location = parts[-1].strip()
            maybe_title = " ".join(parts[:-1]).strip()

            # タイトルが成立し、場所候補があまりに長文でない場合だけ採用
            if maybe_title and len(maybe_location) <= 20:
                location = maybe_location
                title = maybe_title

    title = normalize_text(title)
    location = normalize_text(location)

    if not date_str or not title:
        logger.info("parse_event_text failed: original=%s normalized=%s", original_text, text)
        return None

    return {
        "title": title,
        "date": date_str,
        "time": time_str,
        "location": location,
        "raw_text": original_text,
    }


# ==============================
# Data Access
# ==============================
def get_all_sheet_values():
    sheet = get_sheet()
    return sheet.get_all_values()


def get_data_rows():
    values = get_all_sheet_values()
    if len(values) <= 1:
        return []
    return [normalize_row(r) for r in values[1:]]


def find_rows_by_conversation_key(conversation_key):
    all_values = get_all_sheet_values()
    indexed_rows = []

    for sheet_row_no, row in enumerate(all_values[1:], start=2):
        row = normalize_row(row)
        if row[4] == conversation_key:
            indexed_rows.append((sheet_row_no, row))

    indexed_rows.sort(key=lambda x: sort_key_from_row(x[1]))
    return indexed_rows


def update_sent_flag(sheet, sheet_row_no, col_index, value="1"):
    sheet.update_cell(sheet_row_no, col_index, value)


def update_updated_at(sheet, sheet_row_no):
    sheet.update_cell(sheet_row_no, 15, now_jst().strftime("%Y/%m/%d %H:%M:%S"))


# ==============================
# LINE API
# ==============================
def line_headers():
    return {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_TOKEN}",
    }


def reply(token, text):
    if not LINE_TOKEN:
        raise RuntimeError("CHANNEL_ACCESS_TOKEN is empty")

    url = "https://api.line.me/v2/bot/message/reply"
    data = {
        "replyToken": token,
        "messages": [{"type": "text", "text": text}],
    }
    res = requests.post(url, headers=line_headers(), json=data, timeout=15)
    logger.info("LINE reply status=%s body=%s", res.status_code, res.text)
    res.raise_for_status()


def push(to, text):
    if not LINE_TOKEN:
        raise RuntimeError("CHANNEL_ACCESS_TOKEN is empty")

    url = "https://api.line.me/v2/bot/message/push"
    data = {
        "to": to,
        "messages": [{"type": "text", "text": text}],
    }
    res = requests.post(url, headers=line_headers(), json=data, timeout=15)
    logger.info("LINE push status=%s body=%s", res.status_code, res.text)
    res.raise_for_status()


# ==============================
# Google Calendar
# ==============================
def create_google_calendar_event(parsed):
    try:
        service = get_calendar_service()

        date_str = parsed["date"]
        time_str = parsed["time"]
        location = parsed.get("location", "")

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

            # 深夜またぎ対応
            if end_dt <= start_dt:
                end_dt += timedelta(days=1)

            event_body = {
                "summary": parsed["title"],
                "start": {
                    "dateTime": start_dt.isoformat(),
                    "timeZone": "Asia/Tokyo",
                },
                "end": {
                    "dateTime": end_dt.isoformat(),
                    "timeZone": "Asia/Tokyo",
                },
            }
        else:
            event_date = f"{y:04d}-{m:02d}-{d:02d}"
            next_day = (datetime(y, m, d) + timedelta(days=1)).strftime("%Y-%m-%d")
            event_body = {
                "summary": parsed["title"],
                "start": {"date": event_date},
                "end": {"date": next_day},
            }

        if location:
            event_body["location"] = location

        created = service.events().insert(calendarId=CALENDAR_ID, body=event_body).execute()

        return {
            "html_link": created.get("htmlLink", ""),
            "event_id": created.get("id", ""),
        }

    except Exception as e:
        logger.exception("CALENDAR ERROR: %s", str(e))
        return {
            "html_link": "",
            "event_id": "",
        }


# ==============================
# Event Register
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
                and row[3] == parsed["location"]
                and row[4] == conversation_key
            ):
                display_datetime = format_display_datetime(parsed["date"], parsed["time"])
                return f"すでに登録済みです\n{parsed['title']}\n{display_datetime}"

        calendar_info = create_google_calendar_event(parsed)
        calendar_link = calendar_info["html_link"]
        calendar_event_id = calendar_info["event_id"]

        timestamp = now_jst().strftime("%Y/%m/%d %H:%M:%S")

        row = [
            parsed["title"],
            parsed["date"],
            parsed["time"],
            parsed["location"],
            conversation_key,
            target_id,
            target_type,
            "0",   # sent_14
            "0",   # sent_7
            "0",   # sent_0
            "",    # sent_weekly
            calendar_link,
            calendar_event_id,
            timestamp,
            timestamp,
            parsed.get("raw_text", ""),
        ]

        sheet.append_row(row)

        display_datetime = format_display_datetime(parsed["date"], parsed["time"])
        location_line = f"\n場所: {parsed['location']}" if parsed["location"] else ""

        if calendar_link:
            return (
                f"イベント登録しました\n"
                f"{parsed['title']}\n"
                f"{display_datetime}"
                f"{location_line}\n"
                f"Googleカレンダーにも登録しました"
            )

        return (
            f"イベント登録しました\n"
            f"{parsed['title']}\n"
            f"{display_datetime}"
            f"{location_line}"
        )

    except Exception as e:
        logger.exception("REGISTER ERROR: %s", str(e))
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
            "明日 19時 飲み会\n"
            "次の土曜 13:00-17:00 越谷_C\n"
            "3/22 TEST_A 15:00-20:00"
        )
    return register_event_from_parsed(parsed, conversation_key, target_id, target_type)


# ==============================
# List / Delete
# ==============================
def list_events(conversation_key):
    indexed_rows = find_rows_by_conversation_key(conversation_key)

    if not indexed_rows:
        return "イベントはありません"

    lines = ["イベント一覧"]
    for idx, (_, row) in enumerate(indexed_rows, start=1):
        lines.append(build_event_line(row, prefix=f"{idx}. "))

    return "\n".join(lines)


def delete_event(text, conversation_key):
    sheet = get_sheet()

    match = re.match(r"^削除\s+(\d+)$", text.strip())
    if not match:
        return "削除形式:\n削除 1"

    delete_no = int(match.group(1))
    indexed_rows = find_rows_by_conversation_key(conversation_key)

    if not indexed_rows:
        return "削除できるイベントがありません"

    if delete_no < 1 or delete_no > len(indexed_rows):
        return "削除番号が正しくありません"

    sheet_row_no, row = indexed_rows[delete_no - 1]
    sheet.delete_rows(sheet_row_no)

    return f"イベント削除しました\n{build_event_line(row)}"


# ==============================
# Today / Tomorrow / This Week
# ==============================
def filter_events_by_range(conversation_key, start_date, end_date, title):
    rows = get_data_rows()
    result = []

    for row in rows:
        row = normalize_row(row)
        if row[4] != conversation_key:
            continue

        try:
            event_date = parse_date_str(row[1])
        except Exception:
            continue

        if start_date <= event_date <= end_date:
            result.append(row)

    if not result:
        return f"{title}の予定はありません"

    result.sort(key=sort_key_from_row)

    lines = [f"{title}の予定"]
    for row in result:
        lines.append(build_event_line(row, prefix="- "))
    return "\n".join(lines)


def build_weekly_summary_for_target(conversation_key):
    start = today_jst()
    end = start + timedelta(days=6)

    rows = get_data_rows()
    result = []

    for row in rows:
        row = normalize_row(row)
        if row[4] != conversation_key:
            continue

        try:
            event_date = parse_date_str(row[1])
        except Exception:
            continue

        if start <= event_date <= end:
            result.append(row)

    if not result:
        return "【今週の予定】\n今週の予定はありません"

    result.sort(key=sort_key_from_row)

    lines = ["【今週の予定】"]
    for row in result:
        lines.append(build_event_line(row, prefix="- "))

    return "\n".join(lines)


# ==============================
# Reminder
# ==============================
def check_daily_reminders():
    sheet = get_sheet()
    all_values = sheet.get_all_values()

    if len(all_values) <= 1:
        logger.info("daily reminders: no rows")
        return {
            "rows": 0,
            "sent14": 0,
            "sent7": 0,
            "sent0": 0,
            "errors": 0,
        }

    today = today_jst()
    sent14_count = 0
    sent7_count = 0
    sent0_count = 0
    error_count = 0

    for sheet_row_no, row in enumerate(all_values[1:], start=2):
        row = normalize_row(row)

        title = row[0]
        date_str = row[1]
        time_str = row[2]
        location = row[3]
        target_id = row[5]
        sent_14 = row[7]
        sent_7 = row[8]
        sent_0 = row[9]

        if not target_id or not date_str:
            continue

        try:
            event_date = parse_date_str(date_str)
        except Exception:
            logger.warning("invalid date row=%s date=%s", sheet_row_no, date_str)
            continue

        days = (event_date - today).days
        display_datetime = format_display_datetime(date_str, time_str)
        location_line = f"\n場所: {location}" if location else ""

        try:
            if days == 14 and sent_14 != "1":
                push(
                    target_id,
                    f"【2週間前リマインド】\n予定: {title}\n日時: {display_datetime}{location_line}"
                )
                update_sent_flag(sheet, sheet_row_no, 8, "1")
                update_updated_at(sheet, sheet_row_no)
                sent14_count += 1

            elif days == 7 and sent_7 != "1":
                push(
                    target_id,
                    f"【1週間前リマインド】\n予定: {title}\n日時: {display_datetime}{location_line}"
                )
                update_sent_flag(sheet, sheet_row_no, 9, "1")
                update_updated_at(sheet, sheet_row_no)
                sent7_count += 1

            elif days == 0 and sent_0 != "1":
                push(
                    target_id,
                    f"【本日9時リマインド】\n予定: {title}\n日時: {display_datetime}{location_line}"
                )
                update_sent_flag(sheet, sheet_row_no, 10, "1")
                update_updated_at(sheet, sheet_row_no)
                sent0_count += 1

        except Exception as e:
            error_count += 1
            logger.exception(
                "daily reminder error row=%s title=%s date=%s err=%s",
                sheet_row_no, title, date_str, str(e)
            )

    result = {
        "rows": len(all_values) - 1,
        "sent14": sent14_count,
        "sent7": sent7_count,
        "sent0": sent0_count,
        "errors": error_count,
    }
    logger.info("daily reminders result=%s", result)
    return result


def check_weekly_schedule():
    today = today_jst()

    if today.weekday() != 0:  # Monday only
        logger.info("weekly schedule skipped: today=%s weekday=%s", today, today.weekday())
        return {
            "sent_conversations": 0,
            "errors": 0,
            "skipped": True,
        }

    week_key = get_week_key(today)
    sheet = get_sheet()
    rows = get_data_rows()

    conversations = {}
    for idx, row in enumerate(rows, start=2):
        row = normalize_row(row)
        conversation_key = row[4]
        target_id = row[5]

        if not conversation_key or not target_id:
            continue

        conversations.setdefault(conversation_key, []).append((idx, row))

    sent_conversations = 0
    error_count = 0

    for conversation_key, row_items in conversations.items():
        already_sent = any(normalize_row(row)[10] == week_key for _, row in row_items)
        target_id = row_items[0][1][5]

        if already_sent:
            continue

        try:
            message = build_weekly_summary_for_target(conversation_key)
            push(target_id, message)

            for sheet_row_no, _ in row_items:
                update_sent_flag(sheet, sheet_row_no, 11, week_key)
                update_updated_at(sheet, sheet_row_no)

            sent_conversations += 1

        except Exception as e:
            error_count += 1
            logger.exception(
                "weekly schedule error conversation_key=%s err=%s",
                conversation_key, str(e)
            )

    result = {
        "sent_conversations": sent_conversations,
        "errors": error_count,
        "skipped": False,
    }
    logger.info("weekly schedule result=%s", result)
    return result


def check_reminders():
    daily_result = check_daily_reminders()
    weekly_result = check_weekly_schedule()

    result = {
        "status": "ok",
        "daily": daily_result,
        "weekly": weekly_result,
    }
    logger.info("check_reminders result=%s", result)
    return result


# ==============================
# Routes
# ==============================
@app.route("/")
def home():
    return "bot running"


@app.route("/health")
def health():
    return "ok"


@app.route("/cron")
def cron():
    try:
        ensure_header()
        result = check_reminders()
        return json.dumps(result, ensure_ascii=False), 200, {"Content-Type": "application/json; charset=utf-8"}
    except Exception as e:
        logger.exception("CRON ERROR: %s", str(e))
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False), 500, {
            "Content-Type": "application/json; charset=utf-8"
        }


@app.route("/webhook", methods=["POST"])
def webhook():
    ensure_header()

    body_text = request.get_data(as_text=True)
    signature = request.headers.get("X-Line-Signature", "")

    if not verify_line_signature(body_text, signature):
        logger.warning("Invalid LINE signature")
        abort(400)

    body = request.get_json(silent=True) or {}
    logger.info("webhook received events=%s", len(body.get("events", [])))

    for event in body.get("events", []):
        try:
            if event.get("type") != "message":
                continue
            if event.get("message", {}).get("type") != "text":
                continue

            reply_token = event.get("replyToken")
            text = normalize_text(event["message"]["text"])

            target_id, target_type, conversation_key = get_target_info(event.get("source", {}))
            if not target_id or not conversation_key:
                logger.warning("target info missing: event=%s", event)
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
                            "明日 19時 飲み会\n"
                            "次の土曜 13:00-17:00 越谷_C\n"
                            "3/22 TEST_A 15:00-20:00"
                        )

            if reply_token:
                reply(reply_token, result)

        except Exception as e:
            logger.exception("webhook event error: %s", str(e))
            try:
                if event.get("replyToken"):
                    reply(event["replyToken"], f"エラーが発生しました: {str(e)}")
            except Exception:
                logger.exception("failed to reply error message")

    return "OK"


if __name__ == "__main__":
    try:
        ensure_header()
    except Exception as e:
        logger.exception("HEADER INIT ERROR: %s", str(e))

    port = int(os.getenv("PORT", "5000"))
    app.run(host="0.0.0.0", port=port)