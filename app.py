import os
import time
import json
import psycopg2
import logging
from flask import Flask, request, abort

from linebot.v3.exceptions import InvalidSignatureError
from linebot.v3.messaging import (
    Configuration, ApiClient, MessagingApi, PushMessageRequest, TextMessage,
    ApiException
)
# --- 修正 Webhook 導入 ---
from linebot.v3.webhooks import MessageEvent, TextMessageContent, WebhookParser # <--- 改成 WebhookParser

from dotenv import load_dotenv
from groq import Groq, Timeout, APIConnectionError, RateLimitError
from threading import Thread

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

# LINE SDK 設定
configuration = Configuration(access_token=channel_access_token)
# --- 使用正確的類別名稱 ---
parser = WebhookParser(channel_secret) # <--- 將 WebhookHandler 改成 WebhookParser，變數名稱也改成 parser

# Groq Client 初始化
try:
    groq_client = Groq(api_key=grok_api_key)
    app.logger.info("Groq client 初始化成功。")
except Exception as e:
    app.logger.error(f"無法初始化 Groq client: {e}")
    exit()

# 對話記憶設定
MAX_HISTORY_TURNS = 5

# --- 資料庫輔助函數 ---
# (get_db_connection 和 init_db 函數保持不變，這裡省略以縮短篇幅，請確保它們還在)
def get_db_connection():
    """建立並返回一個 PostgreSQL 連接"""
    try:
        conn = psycopg2.connect(DATABASE_URL)
        conn.set_client_encoding('UTF8')
        return conn
    except Exception as e:
        app.logger.error(f"資料庫連接失敗: {e}")
        return None

def init_db():
    """檢查並建立 conversation_history 資料表"""
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
# (process_and_push 函數保持不變，這裡省略以縮短篇幅，請確保它還在)
def process_and_push(user_id, user_text):
    """在背景執行緒中處理訊息、呼叫 Grok、更新歷史、推送回覆"""
    app.logger.info(f"開始背景處理 user {user_id} 的訊息: '{user_text[:50]}...'")
    start_process_time = time.time()
    conn = None
    history = []

    try:
        # 1. 從資料庫讀取歷史紀錄
        conn = get_db_connection()
        if conn:
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

        # 將新訊息加入歷史
        history.append({"role": "user", "content": user_text})
        if len(history) > MAX_HISTORY_TURNS * 2:
            history = history[-(MAX_HISTORY_TURNS * 2):]

        # 2. 準備呼叫 Grok (Web Search 已移除)
        prompt_messages = history.copy()
        grok_response = "抱歉，系統發生錯誤，請稍後再試。"

        # 3. 呼叫 Grok API
        try:
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

        # 4. 將 Grok 回應加入歷史
        history.append({"role": "assistant", "content": grok_response})
        if len(history) > MAX_HISTORY_TURNS * 2:
             history = history[-(MAX_HISTORY_TURNS * 2):]

        # 5. 將更新後的歷史存回資料庫
        if conn:
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

        # 6. 使用 Push API 推送回覆給使用者
        try:
            push_start_time = time.time()
            with ApiClient(configuration) as api_client:
                line_bot_api = MessagingApi(api_client)
                line_bot_api.push_message(
                    PushMessageRequest(
                        to=user_id,
                        messages=[TextMessage(text=grok_response)]
                    )
                )
            push_duration = time.time() - push_start_time
            app.logger.info(f"成功推送訊息給 user {user_id}，耗時 {push_duration:.2f} 秒。")
        except ApiException as e:
            app.logger.error(f"推送訊息給 user {user_id} 失敗: Status={e.status}, Reason={e.reason}, Body={e.body}")
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
        # --- 使用 parser 來處理 ---
        parser.parse(body, signature) # <--- 使用 parser.parse() 而不是 handler.handle()
    except InvalidSignatureError:
        app.logger.error("簽名驗證失敗！請檢查 Channel Secret。")
        abort(400)
    except ApiException as e:
        app.logger.error(f"處理 Webhook 時發生 LINE API 錯誤: Status={e.status}, Reason={e.reason}, Body={e.body}")
        abort(500)
    except Exception as e:
        app.logger.error(f"處理 Webhook 時發生未知錯誤: {e}", exc_info=True)
        abort(500)

    return 'OK'

# --- LINE 訊息事件處理器 ---
# --- 使用 parser 的 decorator ---
@parser.add(MessageEvent, message=TextMessageContent) # <--- 將 @handler.add 改成 @parser.add
def handle_message(event):
    """處理收到的文字訊息事件，啟動背景任務"""
    user_id = event.source.user_id
    user_text = event.message.text
    app.logger.info(f"收到來自 user {user_id} 的文字訊息，準備啟動背景任務。")

    thread = Thread(target=process_and_push, args=(user_id, user_text))
    thread.daemon = True
    thread.start()

# --- 主程式進入點與初始化 ---
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
