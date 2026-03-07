from flask import Flask, request
import os
import requests

app = Flask(__name__)

LINE_TOKEN = os.getenv("CHANNEL_ACCESS_TOKEN")

@app.route("/")
def home():
    return "bot running"

@app.route("/webhook", methods=["POST"])
def webhook():

    body = request.json

    for event in body["events"]:

        if event["type"] == "message":

            reply_token = event["replyToken"]
            user_message = event["message"]["text"]

            reply(reply_token, user_message)

    return "OK"


def reply(token, text):

    url = "https://api.line.me/v2/bot/message/reply"

    headers = {
        "Content-Type": "application/json",
        "Authorization": f"Bearer {LINE_TOKEN}"
    }

    data = {
        "replyToken": token,
        "messages": [
            {
                "type": "text",
                "text": f"受信: {text}"
            }
        ]
    }

    requests.post(url, headers=headers, json=data)


if __name__ == "__main__":
    app.run()