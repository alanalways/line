import os
import time
import json
import psycopg2
import logging
import httpx
import hashlib
import requests
import datetime
# from urllib.parse import quote_plus # <-- 好像沒用到 quote_plus 了，可以移除或保留
# from bs4 import BeautifulSoup # <-- 如果 fetch_and_extract_text 不再使用，也可移除
# from PIL import Image # <-- 圖片處理未實作，可暫時移除
import io
# from psycopg2.extras import Json # <-- 發現上次DB錯誤修復未使用此導入，暫時移除

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageMessage

from dotenv import load_dotenv
from openai import OpenAI, RateLimitError, APIConnectionError, AuthenticationError, APITimeoutError, APIStatusError
from threading import Thread

# --- 載入環境變數 & 基本設定 ---
load_dotenv()
app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# ... (省略環境變數讀取、驗證、API Key 記錄程式碼) ...
channel_access_token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
channel_secret = os.getenv('LINE_CHANNEL_SECRET')
grok_api_key_from_env = os.getenv('GROK_API_KEY')
DATABASE_URL = os.getenv('DATABASE_URL')
XAI_API_BASE_URL = os.getenv("XAI_API_BASE_URL", "https://api.x.ai/v1")
TAIWAN_TZ = datetime.timezone(datetime.timedelta(hours=8))
grok_api_key = None
if grok_api_key_from_env:
    key_hash = hashlib.sha256(grok_api_key_from_env.encode()).hexdigest()
    app.logger.info(f"xAI API Key (來自 GROK_API_KEY): 前4位={grok_api_key_from_env[:4]}, 長度={len(grok_api_key_from_env)}, SHA-256={key_hash[:8]}...")
    if grok_api_key_from_env != grok_api_key_from_env.strip(): grok_api_key = grok_api_key_from_env.strip(); app.logger.warning("已移除 API Key 前後空格。")
    else: grok_api_key = grok_api_key_from_env
else: app.logger.error("錯誤：環境變數 GROK_API_KEY 為空！"); exit()
if not all([channel_access_token, channel_secret, grok_api_key]): exit()
if not DATABASE_URL: app.logger.error("錯誤：DATABASE_URL 未設定！"); exit()


# --- Line Bot SDK 初始化 (v2) ---
try:
    line_bot_api = LineBotApi(channel_access_token)
    handler = WebhookHandler(channel_secret)
    app.logger.info("Line Bot SDK v2 初始化成功。")
except Exception as e: app.logger.error(f"無法初始化 Line Bot SDK: {e}"); exit()

# --- 初始化 OpenAI Client ---
ai_client = None
try:
    # 不再需要手動建立 httpx.Client，讓 openai SDK 自己處理
    # custom_http_client = httpx.Client(...) #<-- 移除手動建立
    app.logger.info(f"準備初始化 OpenAI client for xAI Grok，目標 URL: {XAI_API_BASE_URL}")
    ai_client = OpenAI(
        api_key=grok_api_key,
        base_url=XAI_API_BASE_URL
        # http_client=custom_http_client #<-- 移除傳遞 http_client
    )
    # (省略 API Key 測試程式碼)
    app.logger.info("OpenAI client for xAI Grok 初始化完成。")
except Exception as e: app.logger.error(f"無法初始化 OpenAI client for xAI: {e}", exc_info=True)

# --- 其他設定與函數 ---
MAX_HISTORY_TURNS = 5
# SEARCH_KEYWORDS = ["天氣", "新聞", ... ] # 暫時移除自動搜尋
IMAGE_GEN_TRIGGER = "畫一張："
TEXT_MODEL = "grok-3-mini-beta"
# VISION_MODEL = "grok-vision-beta" # Vision 模型暫不使用
# IMAGE_GEN_MODEL = "grok-2-image-1212" # 圖片生成模型暫不使用

# (get_db_connection 和 init_db 函數保持不變)
def get_db_connection():
    try: conn = psycopg2.connect(DATABASE_URL); conn.set_client_encoding('UTF8'); return conn
    except Exception as e: app.logger.error(f"資料庫連接失敗: {e}"); return None
def init_db():
    sql = "CREATE TABLE IF NOT EXISTS conversation_history (user_id TEXT PRIMARY KEY, history JSONB);"
    conn = get_db_connection();
    if not conn: app.logger.error("無法初始化資料庫 (無連接)。"); return
    try:
        with conn.cursor() as cur: cur.execute(sql); conn.commit()
        app.logger.info("資料庫資料表 'conversation_history' 初始化完成。")
    except Exception as e: app.logger.error(f"無法初始化資料庫資料表: {e}"); conn.rollback()
    finally: conn.close()

# --- 背景處理函數 (移除自動搜尋, 修正 AI 呼叫語法, 移除圖片處理) ---
def process_and_push(user_id, event):
    user_text_original = ""
    # 簡化：只處理文字訊息
    if isinstance(event.message, TextMessage):
        user_text_original = event.message.text
        app.logger.info(f"收到來自 user {user_id} 的文字訊息: '{user_text_original[:50]}...'")
    else:
        app.logger.info(f"收到來自 user {user_id} 的非文字訊息，暫不處理。")
        # 可以選擇推送一個提示訊息
        # try: line_bot_api.push_message(user_id, TextSendMessage(text="抱歉，我目前只處理文字訊息。"))
        # except Exception: pass
        return # 直接結束

    start_process_time = time.time()
    conn = None; history = []; final_response = "抱歉，系統發生錯誤或無法連接 AI 服務。"

    if ai_client is None: app.logger.error(f"AI Client 未初始化 for user {user_id}"); return

    try:
        # 1. 獲取目前時間提示 + 固定繁中提示
        now_utc = datetime.datetime.now(datetime.timezone.utc); now_taiwan = now_utc.astimezone(TAIWAN_TZ); current_time_str = now_taiwan.strftime("%Y年%m月%d日 %H:%M:%S")
        system_prompt = {"role": "system", "content": f"指令：請永遠使用『繁體中文』回答。目前時間是 {current_time_str} (台灣 UTC+8)，回答時間問題請以此為準。"}

        # 2. 讀取歷史紀錄 (保持修正後的邏輯)
        conn = get_db_connection()
        if conn:
            # ... (省略 DB 讀取程式碼，使用修正後的版本) ...
             try:
                with conn.cursor() as cur:
                    cur.execute("SELECT history FROM conversation_history WHERE user_id = %s;", (user_id,))
                    result = cur.fetchone()
                    if result and result[0]:
                        db_data = result[0]
                        if isinstance(db_data, list): history = db_data; app.logger.info(f"成功載入列表歷史，長度: {len(history)}")
                        elif isinstance(db_data, str):
                            try: history = json.loads(db_data); app.logger.info(f"成功解析JSON字串歷史，長度: {len(history)}")
                            except json.JSONDecodeError: history = []; app.logger.error("解析歷史JSON字串失敗")
                        else: history = []; app.logger.error("未知歷史格式")
                    else: app.logger.info(f"無歷史紀錄。")
             except Exception as db_err: app.logger.error(f"讀取歷史錯誤: {db_err}", exc_info=True); history = []; conn.rollback()
        else: app.logger.warning("無法連接資料庫，無歷史紀錄。")

        # 3. 準備提示訊息 (移除自動搜尋/圖片邏輯)
        history.append({"role": "user", "content": user_text_original}) # 直接加入使用者原始訊息
        if len(history) > MAX_HISTORY_TURNS * 2: history = history[-(MAX_HISTORY_TURNS * 2):] # 裁剪
        prompt_messages = [system_prompt] + history # 組合

        # 4. 呼叫 xAI Grok API (修正語法)
        try:
            grok_start = time.time()
            app.logger.info(f"準備呼叫 xAI Grok ({TEXT_MODEL}) for user {user_id}...")
            app.logger.debug(f"傳送給 AI 的 messages: {prompt_messages}")
            # --- ▼▼▼ 修正點：移除多餘的 ... 並確保參數正確 ▼▼▼ ---
            chat_completion = ai_client.chat.completions.create(
                messages=prompt_messages,
                model=TEXT_MODEL,
                temperature=0.7,
                max_tokens=1500
                # timeout 參數通常在 client 初始化時設定，此處不需再傳
            )
            # --- ▲▲▲ 結束修正 ▲▲▲ ---
            final_response = chat_completion.choices[0].message.content.strip()
            app.logger.info(f"xAI Grok ({TEXT_MODEL}) 回應成功，用時 {time.time() - grok_start:.2f} 秒。")
        # (錯誤處理保持不變)
        except AuthenticationError as e: app.logger.error(f"xAI Grok API 認證錯誤: {e}", exc_info=False); final_response = "抱歉，AI 服務鑰匙錯誤。"
        except RateLimitError as e: app.logger.warning(f"xAI Grok API 速率限制: {e}"); final_response = "抱歉，大腦過熱。"
        except APIConnectionError as e: app.logger.error(f"xAI Grok API 連接錯誤: {e}"); final_response = "抱歉，AI 無法連線。"
        except APITimeoutError as e: app.logger.warning(f"xAI Grok API 呼叫超時: {e}"); final_response = "抱歉，思考超時。"
        except APIStatusError as e: app.logger.error(f"xAI Grok API 狀態錯誤: status={e.status_code}, response={e.response}"); final_response = "抱歉，AI 服務異常。"
        except Exception as e: app.logger.error(f"xAI Grok 未知錯誤: {e}", exc_info=True); final_response = "抱歉，系統發生錯誤。"

        # 5. 儲存對話歷史 (保持不變，包含DB日誌和NULL修復)
        history_to_save = history # 從處理完的 history 開始
        # history_to_save.append({"role": "user", "content": user_text_original}) # User 訊息已在上面加入 history
        history_to_save.append({"role": "assistant", "content": final_response})
        if len(history_to_save) > MAX_HISTORY_TURNS * 2: history_to_save = history_to_save[-(MAX_HISTORY_TURNS * 2):]

        if conn:
            try:
                if conn.closed: conn = get_db_connection()
                with conn.cursor() as cur:
                    history_json_string = json.dumps(history_to_save, ensure_ascii=False)
                    cleaned_history_json_string = history_json_string.replace('\x00', '')
                    app.logger.info(f"準備儲存歷史 for user_id: {user_id}")
                    log_string = cleaned_history_json_string[:200] + "..." + cleaned_history_json_string[-200:] if len(cleaned_history_json_string) > 400 else cleaned_history_json_string
                    app.logger.info(f"History JSON string (cleaned, partial): {log_string}")
                    # 使用 Json Adapter 嘗試修復 DB 錯誤
                    # from psycopg2.extras import Json # 確保已導入
                    # cur.execute("""INSERT INTO conversation_history (user_id, history) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET history = EXCLUDED.history;""", (user_id, Json(cleaned_history_json_string)))
                    # --- 暫時改回不用 Json Adapter，直接傳字串，看看錯誤是否一樣 ---
                    cur.execute("""
                        INSERT INTO conversation_history (user_id, history)
                        VALUES (%s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET history = EXCLUDED.history;
                    """, (user_id, cleaned_history_json_string))
                    conn.commit()
                app.logger.info(f"歷史儲存成功 (長度: {len(history_to_save)})。")
            except (Exception, psycopg2.DatabaseError) as db_err:
                app.logger.error(f"儲存歷史錯誤 for user {user_id}: {type(db_err).__name__} - {db_err}", exc_info=True)
                if conn and not conn.closed: conn.rollback()
        else: app.logger.warning("無法連接資料庫，歷史紀錄未儲存。")

        # 6. 推送回覆 (只推送文字)
        final_response_message = TextSendMessage(text=final_response)
        app.logger.info(f"準備推送回應給 user {user_id}: ({type(final_response_message).__name__}) {str(final_response_message)[:100]}...")
        try:
            line_bot_api.push_message(user_id, messages=final_response_message)
            app.logger.info(f"訊息推送完成。")
        except LineBotApiError as e: app.logger.error(f"LINE API 錯誤: {e.status_code} {e.error.message}")
        except Exception as e: app.logger.error(f"推送訊息錯誤: {e}", exc_info=True)

    except Exception as e:
        app.logger.error(f"處理 user {user_id} 時錯誤: {type(e).__name__}: {e}", exc_info=True)
        try: line_bot_api.push_message(user_id, TextSendMessage(text="抱歉，處理您的請求時發生了嚴重錯誤。"))
        except Exception: pass
    finally:
        if conn and not conn.closed: conn.close()
        app.logger.info(f"任務完成，用時 {time.time() - start_process_time:.2f} 秒。")


# --- LINE Webhook 與事件處理器 (只處理文字) ---
@app.route("/callback", methods=['POST'])
def callback():
    # ... (保持不變) ...
    signature = request.headers['X-Line-Signature']; body = request.get_data(as_text=True)
    app.logger.info(f"收到請求: {body[:100]}")
    try: handler.handle(body, signature)
    except InvalidSignatureError: app.logger.error("簽名錯誤。"); abort(400)
    except LineBotApiError as e: app.logger.error(f"LINE API 錯誤: {e.status_code} {e.error.message}"); abort(500)
    except Exception as e: app.logger.error(f"Webhook 錯誤: {e}", exc_info=True); abort(500)
    return 'OK'

# --- 只註冊處理 TextMessage ---
@handler.add(MessageEvent, message=TextMessage) # <--- 只處理文字
def handle_message(event):
    user_id = event.source.user_id
    Thread(target=process_and_push, args=(user_id, event)).start()

# --- 主程式進入點 (保持不變) ---
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
else:
     init_db()
