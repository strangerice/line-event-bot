from flask import Flask, request
import os
import requests
import json
import re
import gspread
from google.oauth2.service_account import Credentials
from datetime import datetime

app = Flask(__name__)

LINE_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")

# ==============================
# Google Sheets認証（Render用）
# ==============================

scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]

credentials_info = json.loads(os.environ["GOOGLE_CREDENTIALS"])

credentials = Credentials.from_service_account_info(
    credentials_info,
    scopes=scopes
)

gc = gspread.authorize(credentials)

sheet = gc.open("LINEイベントDB").sheet1


@app.route("/")
def home():
    return "bot running"


@app.route("/webhook", methods=["POST"])
def webhook():

    body = request.json

    for event in body["events"]:

        if event["type"] == "message":

            reply_token = event["replyToken"]
            text = event["message"]["text"]
            user = event["source"]["userId"]

            result = register_event(text, user)

            reply(reply_token, result)

    return "OK"


# ==============================
# イベント登録
# ==============================

def register_event(text, user):

    pattern = r"(\d+)/(\d+)\s(\d+:\d+)\s(.+)"

    match = re.match(pattern, text)

    if match:

        month = match.group(1)
        day = match.group(2)
        time = match.group(3)
        event = match.group(4)

        year = datetime.now().year
        date = f"{year}/{month}/{day}"

        sheet.append_row([event, date, time, user])

        return f"イベント登録しました\n{event} {date} {time}"

    else:

        return "イベント形式:\n6/10 19:00 飲み会"


# ==============================
# LINE返信
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

    requests.post(url, headers=headers, json=data)


# ==============================
# LINE自動送信（Push）
# ==============================

def push(user, text):

    url = "https://api.line.me/v2/bot/message/push"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_TOKEN}"
    }

    data = {
        "to": user,
        "messages": [{"type": "text", "text": text}]
    }

    requests.post(url, headers=headers, json=data)


# ==============================
# リマインドチェック
# ==============================
def check_reminders():

    from datetime import datetime, timedelta

    data = sheet.get_all_values()

    today = datetime.now().date()

    for row in data:

        event = row[0]
        date = row[1]
        time = row[2]
        user = row[3]

        event_date = datetime.strptime(date, "%Y/%m/%d").date()

        days = (event_date - today).days

        if days == 14:
            push(user, f"【2週間前】{event} {date} {time}")

        elif days == 7:
            push(user, f"【1週間前】{event} {date} {time}")

        elif days == 0:
            push(user, f"【今日】{event} {date} {time}")

# ==============================
# cron用エンドポイント
# ==============================

@app.route("/cron")
def cron():

    check_reminders()

    return "ok"


if __name__ == "__main__":
    app.run()