from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError
from linebot.models import MessageEvent, TextMessage, ImageMessage, TextSendMessage, ImageSendMessage
import os
import requests
import json
import sqlite3
from datetime import datetime
from bs4 import BeautifulSoup  # 用於簡單 HTML 處理
import re  # 用於簡單數學運算檢查
import subprocess  # 用於啟動 Fetch MCP 伺服器
import asyncio

app = Flask(__name__)

# LINE 設定
line_bot_api = LineBotApi(os.environ['LINE_CHANNEL_ACCESS_TOKEN'])
handler = WebhookHandler(os.environ['LINE_CHANNEL_SECRET'])

# Grok API 設定
GROK_API_KEY = os.environ['GROK_API_KEY']
GROK_API_URL = "https://api.x.ai/v1/chat/completions"
GROK_IMAGE_API_URL = "https://api.x.ai/v1/image/generations"

# 初始化 SQLite 資料庫
DB_PATH = "conversations.db"

def init_db():
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS conversations
                 (id INTEGER PRIMARY KEY AUTOINCREMENT,
                  user_id TEXT NOT NULL,
                  message TEXT NOT NULL,
                  role TEXT NOT NULL,
                  timestamp DATETIME DEFAULT CURRENT_TIMESTAMP)''')
    conn.commit()
    conn.close()

# 確保資料庫在應用啟動時初始化
init_db()

# 儲存對話到資料庫
def save_message(user_id, message, role):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("INSERT INTO conversations (user_id, message, role) VALUES (?, ?, ?)", (user_id, message, role))
    conn.commit()
    conn.close()

# 取得對話歷史
def get_conversation_history(user_id, limit=10):
    conn = sqlite3.connect(DB_PATH)
    c = conn.cursor()
    c.execute("SELECT role, message FROM conversations WHERE user_id = ? ORDER BY timestamp DESC LIMIT ?", (user_id, limit))
    history = c.fetchall()
    conn.close()
    history.reverse()
    app.logger.info(f"Conversation history for {user_id}: {history}")
    return [{"role": row[0], "content": row[1]} for row in history]

# 檢查是否為簡單數學運算
def is_simple_math(message):
    pattern = r'^\s*(\d+)\s*([+\-*/])\s*(\d+)\s*(等於多少)?\s*$|^\s*(\d+)\s*([+\-*/])\s*(\d+)\s*$'
    match = re.match(pattern, message.strip())
    app.logger.info(f"Checking math pattern for '{message}': {match}")
    return match

# 計算簡單數學運算
def calculate_simple_math(message):
    match = is_simple_math(message)
    if not match:
        return None
    groups = match.groups()
    num1 = int(groups[0] if groups[0] else groups[4])
    num2 = int(groups[2] if groups[2] else groups[6])
    operator = groups[1] if groups[1] else groups[5]
    app.logger.info(f"Calculating: {num1} {operator} {num2}")
    if operator == '+':
        return str(num1 + num2)
    elif operator == '-':
        return str(num1 - num2)
    elif operator == '*':
        return str(num1 * num2)
    elif operator == '/':
        return str(num1 / num2) if num2 != 0 else "錯誤：除數不能為 0"
    return None

# 檢查是否為查詢歷史問題
def is_history_query(message):
    return message.strip() in ["我剛剛問什麼", "我之前問了什麼"]

# 從歷史中提取上一個問題
def get_last_question(user_id):
    history = get_conversation_history(user_id, limit=2)
    app.logger.info(f"Last question history: {history}")
    if len(history) >= 2 and history[1]["role"] == "user":
        return history[1]["content"]
    return "我沒有記錄到你的上一個問題。"

# 啟動 Fetch MCP 伺服器
def start_fetch_mcp_server():
    try:
        subprocess.Popen(["uvx", "mcp-server-fetch"], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        app.logger.info("Fetch MCP server started successfully")
        return True
    except Exception as e:
        app.logger.error(f"Failed to start Fetch MCP server: {e}")
        return False

# 調用 Fetch MCP 獲取網頁內容
async def fetch_web_content(url):
    try:
        response = requests.post("http://localhost:3000/fetch", json={"url": url, "max_length": 5000}, timeout=10)
        response.raise_for_status()
        content = response.json().get("content", "無法獲取網頁內容")
        app.logger.info(f"Fetched web content from {url}: {content[:100]}...")
        return content
    except Exception as e:
        app.logger.error(f"Failed to fetch web content from {url}: {e}")
        return f"獲取網頁內容失敗: {e}"

# 檢查是否為網頁查詢
def is_web_query(message):
    return message.strip().startswith("查詢網頁: ")

# 調用 Grok API 的通用函數
def call_grok_api(messages, model, image_url=None):
    headers = {"Authorization": f"Bearer {GROK_API_KEY}", "Content-Type": "application/json"}
    if image_url and model == "grok-2-vision-1212":
        messages.append({"role": "user", "content": [{"type": "text", "text": messages[-1]["content"]}, {"type": "image_url", "image_url": {"url": image_url}}]})
        messages[-2]["content"] = messages[-2]["content"]
    data = {"model": model, "messages": messages, "max_tokens": 500}
    try:
        response = requests.post(GROK_API_URL, headers=headers, json=data, timeout=10)
        response.raise_for_status()
        reply = response.json()['choices'][0]['message']['content']
        app.logger.info(f"Grok API response: {reply}")
        return reply
    except requests.RequestException as e:
        app.logger.error(f"Grok API error: {e}")
        return f"錯誤：無法連接到 xAI API - {e}"

# 生成圖片的函數
def generate_image(prompt):
    headers = {"Authorization": f"Bearer {GROK_API_KEY}", "Content-Type": "application/json"}
    data = {"model": "grok-2-image-1212", "prompt": prompt}
    try:
        response = requests.post(GROK_IMAGE_API_URL, headers=headers, json=data, timeout=10)
        response.raise_for_status()
        return response.json()["data"][0]["url"]
    except requests.RequestException as e:
        app.logger.error(f"Image generation error: {e}")
        return f"錯誤：無法生成圖片 - {e}"

# 檢查是否為圖片生成請求
def is_image_generation_request(message):
    keywords = ["生成圖片", "畫", "圖片", "繪製", "create image", "draw"]
    return any(keyword in message.lower() for keyword in keywords)

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info(f"Request body: {body}")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.error("Invalid signature.")
        abort(400)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_text_message(event):
    user_id = event.source.user_id
    user_message = event.message.text
    reply_token = event.reply_token

    # 儲存用戶訊息
    save_message(user_id, user_message, "user")

    # 啟動 Fetch MCP 伺服器（僅在應用啟動時執行一次）
    if not hasattr(app, 'fetch_mcp_started') or not app.fetch_mcp_started:
        app.fetch_mcp_started = start_fetch_mcp_server()

    # 檢查是否為簡單數學運算
    math_result = calculate_simple_math(user_message)
    if math_result:
        reply = f"計算結果：{math_result}"
    # 檢查是否為查詢歷史問題
    elif is_history_query(user_message):
        reply = f"你剛剛問：{get_last_question(user_id)}"
    # 檢查是否為網頁查詢
    elif is_web_query(user_message):
        url = user_message.replace("查詢網頁: ", "").strip()
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        reply = loop.run_until_complete(fetch_web_content(url))
        loop.close()
    else:
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
                line_bot_api.reply_message(reply_token, ImageSendMessage(original_content_url=image_url, preview_image_url=image_url))
                return
        else:
            # 使用 grok-3-beta 處理文字
            reply = call_grok_api(conversation_history, model="grok-3-beta")

    # 儲存模型回應
    save_message(user_id, reply, "assistant")

    # 回覆用戶
    try:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply))
    except Exception as e:
        app.logger.error(f"Failed to reply: {e}")

@handler.add(MessageEvent, message=ImageMessage)
def handle_image_message(event):
    user_id = event.source.user_id
    message_id = event.message.id
    reply_token = event.reply_token

    headers = {"Authorization": f"Bearer {os.environ['LINE_CHANNEL_ACCESS_TOKEN']}"}
    response = requests.get(f"https://api-data.line.me/v2/bot/message/{message_id}/content", headers=headers)
    if response.status_code != 200:
        reply = "錯誤：無法取得圖片內容"
        save_message(user_id, reply, "assistant")
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply))
        return

    image_url = f"https://api-data.line.me/v2/bot/message/{message_id}/content"
    save_message(user_id, "用戶傳送了一張圖片", "user")

    conversation_history = get_conversation_history(user_id)
    conversation_history.append({"role": "user", "content": "請描述這張圖片的內容"})
    reply = call_grok_api(conversation_history, model="grok-2-vision-1212", image_url=image_url)

    save_message(user_id, reply, "assistant")

    try:
        line_bot_api.reply_message(reply_token, TextSendMessage(text=reply))
    except Exception as e:
        app.logger.error(f"Failed to reply: {e}")

if __name__ == "__main__":
    app.run(host='0.0.0.0', port=int(os.environ.get('PORT', 10000)))
