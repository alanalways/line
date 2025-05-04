from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage, ImageSendMessage
import os
import requests
import json
import sqlite3
from datetime import datetime

app = Flask(__name__)

# LINE 設定
line_bot_api = LineBotApi(os.environ['LINE_CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(os.environ['LINE_CHANNEL_SECRET'])

# Grok API 設定
GROK_API_KEY = os.environ['GROK_API_KEY']
GROK_API_URL = "https://api.x.ai/v1/chat/completions"
GROK_IMAGE_API_URL = "https://api.x.ai/v1/image/generations"

# 初始化 SQLite 資料庫（記憶體模式）
def init_db():
    conn = sqlite3.connect(':memory:')  # 使用記憶體資料庫
    c = conn.cursor()
    # 增加 `id` 作為主鍵，並確保 `user_id` 和 `message` 不為空
    c.execute('''CREATE TABLE conversations
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id TEXT NOT NULL,
                  message TEXT NOT NULL,
                  role TEXT NOT NULL,
                  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

# 儲存對話到資料庫
def save_message(user_id, message, role):
    conn = sqlite3.connect(':memory:')
    c = conn.cursor()
    c.execute("INSERT INTO conversations (user_id, message, role) VALUES (?, ?, ?)", (user_id, message, role))
    conn.commit()
    conn.close()

# 取得對話歷史
def get_conversation_history(user_id, limit=10):
    conn = sqlite3.connect(':memory:')
    c = conn.cursor()
    c.execute("SELECT role, message FROM conversations WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?", (user_id, limit))
    history = c.fetchall()
    conn.close()
    history.reverse()  # 反轉以按時間順序排列
    return [{"role": row[0], "content": row[1]} for row in history]

# 初始化資料庫
init_db()

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        abort(400)
    return 'OK'

# 調用 Grok API 的通用函數
def call_grok_api(messages, model, image_url=None):
    headers = {
        "Authorization": f"Bearer {GROK_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": model,
        "messages": messages,
        "max_tokens": 1000
    }
    if image_url and model == "grok-2-vision-1212":
        data["messages"].append({
            "role": "user",
            "content": [{"type": "image_url", "image_url": {"url": image_url}}]
        })
    try:
        response = requests.post(GROK_API_URL, headers=headers, json=data)
        response.raise_for_status()
        return response.json()['choices'][0]['message']['content']
    except requests.RequestException as e:
        return f"錯誤：無法連接到 xAI API - {str(e)}"

# 生成圖片的函數
def generate_image(prompt):
    headers = {
        "Authorization": f"Bearer {GROK_API_KEY}",
        "Content-Type": "application/json"
    }
    data = {
        "model": "grok-2-image-1212",
        "prompt": prompt
    }
    try:
        response = requests.post(GROK_IMAGE_API_URL, headers=headers, json=data)
        response.raise_for_status()
        return response.json()["data"][0]["url"]  # 假設 API 返回圖片 URL
    except requests.RequestException as e:
        return f"錯誤：無法生成圖片 - {str(e)}"

# 檢查是否為圖片生成請求
def is_image_generation_request(message):
    keywords = ["生成圖片", "畫", "圖片", "繪製", "create image", "draw"]
    return any(keyword in message.lower() for keyword in keywords)

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    user_id = event.source.user_id
    user_message = event.message.text

    # 儲存用戶訊息
    save_message(user_id, user_message, "user")

    # 取得對話歷史
    conversation_history = get_conversation_history(user_id)
    conversation_history.append({"role": "user", "content": user_message})

    # 檢查是否為圖片生成請求
    if is_image_generation_request(user_message):
        image_url = generate_image(user_message)
        if "錯誤" in image_url:
            reply = image_url
        else:
            save_message(user_id, "生成了一張圖片", "assistant")
            line_bot_api.reply_message(
                event.reply_token,
                ImageSendMessage(original_content_url=image_url, preview_image_url=image_url)
            )
            return
    else:
        # 使用 grok-3-beta 處理文字
        reply = call_grok_api(conversation_history, model="grok-3-beta")

    # 儲存模型回應
    save_message(user_id, reply, "assistant")

    # 回覆用戶
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply)
    )

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    user_id = event.source.user_id
    message_id = event.message.id

    # 取得圖片內容
    headers = {"Authorization": f"Bearer {os.environ['LINE_CHANNEL_ACCESS_TOKEN']}"}
    response = requests.get(f"https://api-data.line.me/v2/bot/message/{message_id}/content", headers=headers)
    if response.status_code != 200:
        reply = "錯誤：無法取得圖片內容"
        line_bot_api.reply_message(event.reply_token, TextSendMessage(text=reply))
        return

    # 假設圖片需要上傳到公開儲存（這裡簡化為直接使用 URL）
    image_url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"

    # 儲存用戶圖片訊息
    save_message(user_id, "用戶傳送了一張圖片", "user")

    # 取得對話歷史並使用 grok-2-vision-1212 處理圖片
    conversation_history = get_conversation_history(user_id)
    conversation_history.append({"role": "user", "content": "請描述這張圖片的內容"})
    reply = call_grok_api(conversation_history, model="grok-2-vision-1212", image_url=image_url)

    # 儲存模型回應
    save_message(user_id, reply, "assistant")

    # 回覆用戶
    line_bot_api.reply_message(
        event.reply_token,
        TextSendMessage(text=reply)
    )

if __name__ == "__main__":
    app.run()
