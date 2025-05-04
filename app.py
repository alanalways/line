import os
import time
import json
import psycopg2
import logging
import httpx
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from dotenv import load_dotenv
from groq import Groq, RateLimitError, APIConnectionError, AuthenticationError
from threading import Thread
import asyncio

# --- 載入環境變數 ---
load_dotenv()

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

channel_access_token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
channel_secret = os.getenv('LINE_CHANNEL_SECRET')
grok_api_key = os.getenv('GROK_API_KEY')
DATABASE_URL = os.getenv('DATABASE_URL')

# 驗證環境變數並記錄 API Key 資訊（不洩漏完整 Key）
if not all([channel_access_token, channel_secret, grok_api_key]):
    app.logger.error("錯誤：LINE Token 或 Groq API Key 未設定！")
    exit()
if not DATABASE_URL:
    app.logger.error("錯誤：DATABASE_URL 未設定！請在 Render 連接資料庫。")
    exit()
app.logger.info(f"GROK_API_KEY 前4位: {grok_api_key[:4]}, 長度: {len(grok_api_key)}")

try:
    line_bot_api = LineBotApi(channel_access_token)
    handler = WebhookHandler(channel_secret)
    app.logger.info("Line Bot SDK v3 初始化成功。")
except Exception as e:
    app.logger.error(f"無法初始化 Line Bot SDK: {e}")
    exit()

try:
    custom_http_client = httpx.Client(
        verify=True,
        timeout=httpx.Timeout(60.0, connect=10.0)
    )
    app.logger.info("已手動建立 httpx.Client。")
    groq_client = Groq(
        api_key=grok_api_key,
        http_client=custom_http_client
    )
    app.logger.info("Groq client 初始化成功。")
except Exception as e:
    app.logger.error(f"無法初始化 Groq client: {e}", exc_info=True)
    exit()

MAX_HISTORY_TURNS = 5

def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        conn.set_client_encoding('UTF8')
        return conn
    except Exception as e:
        app.logger.error(f"資料庫連接失敗: {e}")
        return None

def init_db():
    sql = """
    CREATE TABLE IF NOT EXISTS conversation_history (
        user_id TEXT PRIMARY KEY,
        history JSONB
    );
    """
    conn = get_db_connection()
    if not conn:
        app.logger.error("無法初始化資料庫 (無連接)。")
        return
    try:
        with conn.cursor() as cur:
            cur.execute(sql)
            conn.commit()
        app.logger.info("資料庫資料表 'conversation_history' 初始化完成。")
    except Exception as e:
        app.logger.error(f"無法初始化資料庫資料表: {e}")
        if conn: conn.rollback()
    finally:
        if conn and not conn.closed: conn.close()

async def fetch_web_content(url):
    """從指定 URL 獲取網頁內容"""
    try:
        async with httpx.AsyncClient(verify=True, timeout=10.0) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text[:2000]  # 限制內容長度
    except Exception as e:
        app.logger.error(f"獲取網頁內容失敗: {e}")
        return f"無法獲取網頁內容：{str(e)}"

def process_and_push(user_id, event):
    user_text = event.message.text
    app.logger.info(f"開始處理 user {user_id} 的訊息: '{user_text[:50]}...'")
    start_process_time = time.time()
    conn = None
    history = []

    try:
        conn = get_db_connection()
        if conn:
            try:
                with conn.cursor() as cur:
                    cur.execute("SELECT history FROM conversation_history WHERE user_id = %s;", (user_id,))
                    result = cur.fetchone()
                    if result and result[0]:
                        # JSONB 欄位已由 psycopg2 自動解析為 Python 物件
                        app.logger.info(f"讀取原始歷史資料: {str(result[0])[:100]}...")
                        if isinstance(result[0], list):
                            history = result[0]
                        else:
                            history = json.loads(result[0])
                        app.logger.info(f"成功載入 user {user_id} 的歷史，長度: {len(history)}")
                    else:
                        app.logger.info(f"無歷史紀錄。")
            except Exception as db_err:
                app.logger.error(f"讀取歷史錯誤: {db_err}", exc_info=True)
                history = []
                if conn and not conn.closed: conn.rollback()

        # 檢查是否為網頁查詢請求
        web_content = None
        if user_text.startswith("查詢："):
            url = user_text[3:].strip()
            if url:
                # 在新的事件迴圈中運行異步請求
                try:
                    web_content = asyncio.run(fetch_web_content(url))
                except Exception as e:
                    app.logger.error(f"運行異步網頁查詢失敗: {e}", exc_info=True)
                    web_content = f"無法執行網頁查詢：{str(e)}"
                if web_content and not web_content.startswith("無法獲取"):
                    user_text = f"請根據以下網頁內容回答問題或提供總結：\n{web_content[:1000]}"

        history.append({"role": "user", "content": user_text})
        if len(history) > MAX_HISTORY_TURNS * 2:
            history = history[-(MAX_HISTORY_TURNS * 2):]

        prompt_messages = history.copy()
        grok_response = "系統錯誤，請稍後再試。"

        try:
            grok_start = time.time()
            chat_completion = groq_client.chat.completions.create(
                messages=prompt_messages,
                model="grok-3-mini-beta",
                temperature=0.7,
                max_tokens=1500,
            )
            grok_response = chat_completion.choices[0].message.content.strip()
            app.logger.info(f"Grok 回應成功，用時 {time.time() - grok_start:.2f} 秒。")
        except RateLimitError as e:
            grok_response = "大腦過熱，請稍後再試。"
            app.logger.warning(f"Grok API 速率限制觸發: {e}")
        except APIConnectionError as e:
            grok_response = "AI 無法連線或請求超時，請稍後再試。"
            app.logger.error(f"Grok API 連線或超時錯誤: {e}")
        except AuthenticationError as e:
            grok_response = "API 金鑰驗證失敗，請聯繫管理員。"
            app.logger.error(f"Grok API 認證錯誤: {e}", exc_info=True)
        except Exception as e:
            grok_response = "系統錯誤，請稍後再試。"
            app.logger.error(f"Grok API 錯誤: {type(e).__name__}: {e}", exc_info=True)

        history.append({"role": "assistant", "content": grok_response})
        if len(history) > MAX_HISTORY_TURNS * 2:
            history = history[-(MAX_HISTORY_TURNS * 2):]

        if conn:
            try:
                if conn.closed:
                    conn = get_db_connection()
                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO conversation_history (user_id, history)
                        VALUES (%s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET history = EXCLUDED.history;
                    """, (user_id, json.dumps(history)))
                    conn.commit()
                app.logger.info(f"歷史儲存成功。")
            except Exception as db_err:
                app.logger.error(f"儲存歷史錯誤: {db_err}", exc_info=True)
                if conn and not conn.closed: conn.rollback()

        app.logger.info(f"準備推送回應給 user {user_id}: {grok_response[:50]}...")
        try:
            line_bot_api.push_message(user_id, TextSendMessage(text=grok_response))
            app.logger.info(f"訊息推送完成。")
        except LineBotApiError as e:
            app.logger.error(f"LINE API 錯誤: {e.status_code} {e.error.message}")
        except Exception as e:
            app.logger.error(f"推送訊息錯誤: {e}", exc_info=True)

    except Exception as e:
        app.logger.error(f"處理 user {user_id} 時錯誤: {type(e).__name__}: {e}", exc_info=True)
    finally:
        if conn and not conn.closed:
            conn.close()
        app.logger.info(f"任務完成，用時 {time.time() - start_process_time:.2f} 秒。")

@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info(f"收到請求: {body[:100]}")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.error("簽名錯誤。")
        abort(400)
    except LineBotApiError as e:
        app.logger.error(f"LINE API 錯誤: {e.status_code} {e.error.message}")
        abort(500)
    except Exception as e:
        app.logger.error(f"Webhook 錯誤: {e}", exc_info=True)
        abort(500)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    Thread(target=process_and_push, args=(user_id, event)).start()

if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
