import os
import time
import json
import psycopg2
import logging
from flask import Flask, request, abort

# v2 SDK 的 import 方式
from linebot import LineBotApi, WebhookHandler # <--- 主要的 API 和 Handler
from linebot.exceptions import InvalidSignatureError, LineBotApiError # <--- v2 的錯誤類別
from linebot.models import MessageEvent, TextMessage, TextSendMessage # <--- v2 的模型 (注意是 TextSendMessage)

from dotenv import load_dotenv
from groq import Groq, Timeout, APIConnectionError, RateLimitError # Groq 部分不變
from threading import Thread # Threading 不變

# --- 載入環境變數 ---
load_dotenv()

app = Flask(__name__)
# --- 設定日誌記錄器 ---
app.logger.setLevel(logging.INFO)

# --- 設定 ---
channel_access_token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
channel_secret = os.getenv('LINE_CHANNEL_SECRET')
grok_api_key = os.getenv('GROK_API_KEY')
DATABASE_URL = os.getenv('DATABASE_URL')

if not all([channel_access_token, channel_secret, grok_api_key]):
    app.logger.error("錯誤：LINE Token 或 Grok API Key 未設定！")
    exit()
if not DATABASE_URL:
    app.logger.error("錯誤：DATABASE_URL 未設定！請在 Render 連接資料庫。")
    exit()

# --- v2 SDK 初始化 ---
try:
    line_bot_api = LineBotApi(channel_access_token) # <--- 初始化 LineBotApi
    handler = WebhookHandler(channel_secret)      # <--- 初始化 WebhookHandler
    app.logger.info("Line Bot SDK v2 初始化成功。")
except Exception as e:
    app.logger.error(f"無法初始化 Line Bot SDK: {e}")
    exit()

# Groq Client 初始化 (不變)
try:
    groq_client = Groq(api_key=grok_api_key)
    app.logger.info("Groq client 初始化成功。")
except Exception as e:
    app.logger.error(f"無法初始化 Groq client: {e}")
    exit()

# 對話記憶設定 (不變)
MAX_HISTORY_TURNS = 5

# --- 資料庫輔助函數 ---
# (get_db_connection 和 init_db 函數保持不變)
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
        app.logger.info("資料庫資料表 'conversation_history' 檢查/建立 完成。")
    except Exception as e:
        app.logger.error(f"無法初始化資料庫資料表: {e}")
        if conn: conn.rollback()
    finally:
        if conn and not conn.closed: conn.close()

# --- 背景處理函數 (核心邏輯) ---
def process_and_push(user_id, event): # 傳入整個 event 可能更方便獲取 reply_token (雖然這裡用不到)
    """在背景執行緒中處理訊息、呼叫 Grok、更新歷史、推送回覆"""
    user_text = event.message.text # 從 event 中獲取文字
    app.logger.info(f"開始背景處理 user {user_id} 的訊息: '{user_text[:50]}...'")
    start_process_time = time.time()
    conn = None
    history = []

    try:
        # 1. 從資料庫讀取歷史紀錄 (不變)
        conn = get_db_connection()
        if conn:
            # ... (省略 DB 讀取程式碼，與之前版本相同) ...
             try:
                with conn.cursor() as cur:
                    cur.execute("SELECT history FROM conversation_history WHERE user_id = %s;", (user_id,))
                    result = cur.fetchone()
                    if result and result[0]:
                        history = json.loads(result[0])
                        app.logger.info(f"成功載入 user {user_id} 的歷史，長度: {len(history)}")
                    else:
                         app.logger.info(f"無 user {user_id} 的歷史紀錄。")
             except (Exception, psycopg2.DatabaseError) as db_err:
                app.logger.error(f"讀取 user {user_id} 的 DB 歷史時出錯: {db_err}")
                history = []
                if conn and not conn.closed: conn.rollback()
        else:
             app.logger.warning("無法連接資料庫，將不使用歷史紀錄。")

        # 將新訊息加入歷史 (不變)
        history.append({"role": "user", "content": user_text})
        if len(history) > MAX_HISTORY_TURNS * 2:
            history = history[-(MAX_HISTORY_TURNS * 2):]

        # 2. 準備呼叫 Grok (不變)
        prompt_messages = history.copy()
        grok_response = "抱歉，系統發生錯誤，請稍後再試。"

        # 3. 呼叫 Grok API (不變)
        try:
            # ... (省略 Grok API 呼叫程式碼，與之前版本相同) ...
            grok_start_time = time.time()
            app.logger.info(f"準備呼叫 Grok API (model: grok-3-mini-beta) for user {user_id}...")
            chat_completion = groq_client.chat.completions.create(
                messages=prompt_messages,
                model="grok-3-mini-beta",
                temperature=0.7,
                max_tokens=1500,
                timeout=Timeout(read=60.0)
            )
            grok_response = chat_completion.choices[0].message.content.strip()
            grok_duration = time.time() - grok_start_time
            app.logger.info(f"Grok API 呼叫成功 for user {user_id}，耗時 {grok_duration:.2f} 秒。")

        # ... (省略 Grok API 錯誤處理，與之前版本相同) ...
        except RateLimitError:
             app.logger.warning(f"Grok 達到速率限制 for user {user_id}")
             grok_response = "抱歉，我的大腦有點過熱，請稍等一下再問我。"
        except APIConnectionError:
             app.logger.error(f"Grok 連接錯誤 for user {user_id}")
             grok_response = "抱歉，我現在連不上我的 AI 大腦，請稍後再試。"
        except Timeout:
             app.logger.warning(f"Grok 呼叫超時 for user {user_id}")
             grok_response = "抱歉，我想得有點久，可以試著換個問法或稍後再試嗎？"
        except Exception as e:
             app.logger.error(f"Grok API 未知錯誤 for user {user_id}: {e}", exc_info=True)

        # 4. 將 Grok 回應加入歷史 (不變)
        history.append({"role": "assistant", "content": grok_response})
        if len(history) > MAX_HISTORY_TURNS * 2:
             history = history[-(MAX_HISTORY_TURNS * 2):]

        # 5. 將更新後的歷史存回資料庫 (不變)
        if conn:
            # ... (省略 DB 儲存程式碼，與之前版本相同) ...
            try:
                if conn.closed:
                    app.logger.warning(f"DB 連接已關閉，無法儲存 user {user_id} 的歷史。嘗試重新連接...")
                    conn = get_db_connection()
                    if not conn: raise Exception("無法重新連接資料庫")

                with conn.cursor() as cur:
                    cur.execute("""
                        INSERT INTO conversation_history (user_id, history)
                        VALUES (%s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET history = EXCLUDED.history;
                    """, (user_id, json.dumps(history)))
                    conn.commit()
                app.logger.info(f"成功儲存 user {user_id} 的歷史。")
            except (Exception, psycopg2.DatabaseError) as db_err:
                app.logger.error(f"儲存 user {user_id} 的 DB 歷史時出錯: {db_err}")
                if conn and not conn.closed: conn.rollback()
        else:
             app.logger.warning("無法連接資料庫，歷史紀錄未儲存。")

        # --- 6. 使用 v2 的 Push API ---
        try:
            push_start_time = time.time()
            # v2 直接使用 line_bot_api 物件的 push_message 方法
            line_bot_api.push_message(
                user_id, # 直接傳 user_id
                messages=TextSendMessage(text=grok_response) # 使用 TextSendMessage
            )
            push_duration = time.time() - push_start_time
            app.logger.info(f"成功推送訊息給 user {user_id}，耗時 {push_duration:.2f} 秒。")
        except LineBotApiError as e: # <--- v2 使用 LineBotApiError
            # v2 的錯誤訊息格式可能不同，記錄原始錯誤
            app.logger.error(f"推送訊息給 user {user_id} 失敗: {e.status_code} {e.error.message} {e.error.details}")
        except Exception as e:
             app.logger.error(f"推送訊息給 user {user_id} 時發生未知錯誤: {e}", exc_info=True)

    except Exception as e:
        app.logger.error(f"背景任務處理 user {user_id} 時發生嚴重錯誤: {e}", exc_info=True)
    finally:
        if conn and not conn.closed:
            conn.close()
        process_duration = time.time() - start_process_time
        app.logger.info(f"背景任務 for user {user_id} 結束，總耗時 {process_duration:.2f} 秒。")


# --- LINE Webhook 主要進入點 ---
@app.route("/callback", methods=['POST'])
def callback():
    """接收來自 LINE 的 Webhook 請求"""
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info(f"收到來自 LINE 的請求 (Body 前 100 字): {body[:100]}")

    try:
        # --- v2 使用 handler.handle() ---
        handler.handle(body, signature)
    except InvalidSignatureError:
        app.logger.error("簽名驗證失敗！請檢查 Channel Secret。")
        abort(400)
    except LineBotApiError as e: # <--- v2 使用 LineBotApiError
        app.logger.error(f"處理 Webhook 時發生 LINE API 錯誤: {e.status_code} {e.error.message} {e.error.details}")
        abort(500)
    except Exception as e:
        app.logger.error(f"處理 Webhook 時發生未知錯誤: {e}", exc_info=True)
        abort(500)

    return 'OK'

# --- LINE 訊息事件處理器 ---
# --- v2 使用 @handler.add 和 TextMessage ---
@handler.add(MessageEvent, message=TextMessage) # <--- 注意是 TextMessage
def handle_message(event):
    """處理收到的文字訊息事件，啟動背景任務"""
    user_id = event.source.user_id
    app.logger.info(f"收到來自 user {user_id} 的文字訊息，準備啟動背景任務。")

    # 啟動背景執行緒，將整個 event 傳遞過去可能更方便 (雖然目前只用到 user_id 和 text)
    thread = Thread(target=process_and_push, args=(user_id, event)) # 傳遞 event
    thread.daemon = True
    thread.start()

# --- 主程式進入點與初始化 ---
# (這部分不變)
try:
    init_db()
    app.logger.info("資料庫初始化檢查完成。")
except Exception as e:
     app.logger.error(f"啟動時資料庫初始化失敗: {e}")

if __name__ == "__main__":
    app.logger.info("以 __main__ 方式啟動 (通常用於本機測試)。")
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port, debug=False)
else:
    app.logger.info("Flask 應用程式 (透過 Gunicorn 或其他 WSGI 伺服器) 啟動。")
