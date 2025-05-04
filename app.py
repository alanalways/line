import os
import time
import json
import psycopg2
import logging
import httpx
import hashlib
import requests # <--- 導入 requests (用於同步網頁查詢)
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from dotenv import load_dotenv
# --- 移除 groq 導入 ---
# from groq import Groq, RateLimitError, APIConnectionError, AuthenticationError #<--移除
# --- 加入 openai 導入 ---
from openai import OpenAI, RateLimitError, APIConnectionError, AuthenticationError, APITimeoutError, APIStatusError # <--- 加入 openai 及錯誤類型
from threading import Thread
# import asyncio # <--- 移除 asyncio

# --- 載入環境變數 ---
load_dotenv()

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

channel_access_token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
channel_secret = os.getenv('LINE_CHANNEL_SECRET')
grok_api_key_from_env = os.getenv('GROK_API_KEY') # <-- 從環境讀取 xAI 的 Key
DATABASE_URL = os.getenv('DATABASE_URL')
XAI_API_BASE_URL = os.getenv("XAI_API_BASE_URL", "https://api.x.ai/v1") # <-- xAI API 端點 (如果需要可自訂)

# --- 驗證與記錄環境變數 ---
if not all([channel_access_token, channel_secret, grok_api_key_from_env]):
    app.logger.error("錯誤：LINE Token 或 Grok/xAI API Key 未設定！")
    exit()
if not DATABASE_URL:
    app.logger.error("錯誤：DATABASE_URL 未設定！請在 Render 連接資料庫。")
    exit()

# --- 記錄 API Key 資訊 (保持你的 debug 碼) ---
grok_api_key = None
if grok_api_key_from_env:
    key_hash = hashlib.sha256(grok_api_key_from_env.encode()).hexdigest()
    app.logger.info(f"xAI API Key (來自 GROK_API_KEY): 前4位={grok_api_key_from_env[:4]}, 長度={len(grok_api_key_from_env)}, SHA-256={key_hash[:8]}...")
    if grok_api_key_from_env != grok_api_key_from_env.strip():
         app.logger.warning("偵測到 xAI API Key 前後有空格，已自動移除。")
         grok_api_key = grok_api_key_from_env.strip()
    else:
         grok_api_key = grok_api_key_from_env
else:
     app.logger.error("錯誤：環境變數 GROK_API_KEY 為空！")
     exit()

# --- Line Bot SDK 初始化 (v2) ---
try:
    line_bot_api = LineBotApi(channel_access_token)
    handler = WebhookHandler(channel_secret)
    app.logger.info("Line Bot SDK v2 初始化成功。") #<-- 修正日誌訊息為 v2
except Exception as e:
    app.logger.error(f"無法初始化 Line Bot SDK: {e}")
    exit()

# --- 初始化 OpenAI Client (用於連接 xAI Grok) ---
ai_client = None # 預設為 None
try:
    app.logger.info(f"準備初始化 OpenAI client for xAI Grok，目標 URL: {XAI_API_BASE_URL}")
    ai_client = OpenAI(
        api_key=grok_api_key, # 使用來自環境變數的 xAI Key
        base_url=XAI_API_BASE_URL # 指定 xAI API 端點
        # 注意：如果 xAI 不需要手動傳遞 http_client，就不要傳
    )
    # --- 使用新的 client 進行 API Key 測試 ---
    try:
        app.logger.info("嘗試使用 xAI API Key 獲取模型列表...")
        models = ai_client.models.list()
        app.logger.info(f">>> xAI API Key 測試 (模型列表) 成功，模型數: {len(models.data)}")
        if models.data:
            app.logger.info(f"    部分可用模型: {[m.id for m in models.data[:5]]}") # 列印前幾個模型 ID
    except AuthenticationError as e:
        app.logger.error(f"!!! xAI API Key 測試 (模型列表) 失敗: 認證錯誤 (401) - 請確認你的 API Key 對 xAI API ({XAI_API_BASE_URL}) 有效! {e}", exc_info=False) # 只記錄關鍵錯誤
    except Exception as e:
        app.logger.error(f"!!! xAI API Key 測試 (模型列表) 發生其他錯誤: {type(e).__name__}: {e}", exc_info=True)

    app.logger.info("OpenAI client for xAI Grok 初始化流程完成。")
except Exception as e:
    app.logger.error(f"無法初始化 OpenAI client for xAI: {e}", exc_info=True)
    # 初始化失敗，但讓程式繼續運行，由後續檢查處理

# --- 其他設定與函數 (DB, 歷史長度) ---
MAX_HISTORY_TURNS = 5

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
        app.logger.info("資料庫資料表 'conversation_history' 初始化完成。")
    except Exception as e:
        app.logger.error(f"無法初始化資料庫資料表: {e}")
        if conn: conn.rollback()
    finally:
        if conn and not conn.closed: conn.close()

# --- 同步網頁查詢函數 (取代 async 版本) ---
def sync_fetch_web_content(url):
    """從指定 URL 同步獲取網頁內容"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36'
        }
        app.logger.info(f"開始同步獲取網頁: {url}")
        response = requests.get(url, headers=headers, timeout=15) # 15 秒超時
        response.raise_for_status()
        content = response.text[:3000] # 限制抓取長度
        app.logger.info(f"成功獲取 URL: {url}, 內容長度: {len(content)}")
        return content
    except requests.exceptions.Timeout:
        app.logger.error(f"獲取網頁超時: {url}")
        return f"無法獲取網頁內容：請求超時"
    except requests.exceptions.RequestException as e:
        app.logger.error(f"獲取網頁內容失敗: {url}, Error: {e}")
        return f"無法獲取網頁內容：{str(e)}"
    except Exception as e:
        app.logger.error(f"處理網頁獲取時未知錯誤: {url}, Error: {e}", exc_info=True)
        return f"處理網頁獲取時發生錯誤：{str(e)}"

# --- 背景處理函數 (使用 OpenAI Client) ---
def process_and_push(user_id, event):
    user_text_original = event.message.text # 保留原始文字
    app.logger.info(f"開始處理 user {user_id} 的訊息: '{user_text_original[:50]}...'")
    start_process_time = time.time()
    conn = None
    history = []
    grok_response = "抱歉，系統發生錯誤或無法連接 AI 服務。" # 更新預設錯誤

    # --- 檢查 AI Client 是否成功初始化 ---
    if ai_client is None:
        app.logger.error(f"AI Client (for xAI) 未成功初始化，無法處理 user {user_id} 的請求。")
        try:
            line_bot_api.push_message(user_id, messages=TextSendMessage(text="抱歉，AI 服務設定錯誤，無法處理您的請求。"))
        except Exception as push_err:
             app.logger.error(f"推送 AI Client 錯誤訊息失敗: {push_err}")
        return
    # --- 結束檢查 ---

    try:
        # 1. 從資料庫讀取歷史紀錄 (不變)
        conn = get_db_connection()
        if conn:
             try:
                with conn.cursor() as cur:
                    cur.execute("SELECT history FROM conversation_history WHERE user_id = %s;", (user_id,))
                    result = cur.fetchone()
                    if result and result[0]:
                         # 嘗試解析 JSON，如果失敗則記錄錯誤並使用空歷史
                         try:
                             loaded_history = json.loads(result[0])
                             if isinstance(loaded_history, list):
                                 history = loaded_history
                                 app.logger.info(f"成功載入 user {user_id} 的歷史，長度: {len(history)}")
                             else:
                                 app.logger.warning(f"從 DB 讀取的歷史不是列表格式 for user {user_id}，將使用空歷史。")
                                 history = []
                         except json.JSONDecodeError:
                              app.logger.error(f"解析 user {user_id} 的 DB 歷史 (JSON) 時出錯，將使用空歷史。")
                              history = []
                    else:
                         app.logger.info(f"無 user {user_id} 的歷史紀錄。")
             except (Exception, psycopg2.DatabaseError) as db_err:
                app.logger.error(f"讀取 user {user_id} 的 DB 歷史時出錯: {db_err}")
                history = []
                if conn and not conn.closed: conn.rollback()
        else:
             app.logger.warning("無法連接資料庫，將不使用歷史紀錄。")

        # 2. 處理網頁查詢 (使用同步 requests)
        user_text_for_llm = user_text_original # 用於傳遞給 LLM 的文字
        if user_text_original.startswith("查詢："):
            url = user_text_original[3:].strip()
            if url:
                app.logger.info(f"偵測到查詢指令，目標 URL: {url}")
                web_content = sync_fetch_web_content(url) # 使用同步函數
                if web_content and not web_content.startswith("無法獲取") and not web_content.startswith("處理網頁"):
                    user_text_for_llm = f"請根據以下網頁內容回答 '{user_text_original[3:]}' 這個查詢的問題或提供總結：\n\n```html\n{web_content}\n```"
                    app.logger.info("已準備包含網頁內容的提示。")
                else:
                    # 如果抓取失敗，將失敗訊息回傳給使用者，或者僅通知 LLM
                    # grok_response = web_content # 直接回傳錯誤
                    user_text_for_llm = f"我嘗試查詢網址 '{url}' 但失敗了：{web_content}。請告知使用者這個情況。" # 讓 AI 知道查詢失敗
                    app.logger.warning(f"網頁查詢失敗，將通知 AI。")
                    # 注意：這裡選擇不直接推送錯誤，而是讓 AI 回應查詢失敗

        # 3. 將處理過的 user_text 加入歷史
        history.append({"role": "user", "content": user_text_for_llm})
        if len(history) > MAX_HISTORY_TURNS * 2:
            history = history[-(MAX_HISTORY_TURNS * 2):]

        # 4. 準備呼叫 xAI Grok (使用 OpenAI client)
        prompt_messages = history.copy()

        # 5. 呼叫 xAI Grok API
        try:
            grok_start = time.time()
            app.logger.info(f"準備呼叫 xAI Grok (model: grok-3-mini-beta) for user {user_id}...")
            # --- 使用 OpenAI Client ---
            chat_completion = ai_client.chat.completions.create(
                messages=prompt_messages,
                model="grok-3-mini-beta", # 使用 xAI 的模型 ID
                temperature=0.7,
                max_tokens=1500,
                # 注意：openai v1.x 的 timeout 是在 client 初始化時設定
            )
            grok_response = chat_completion.choices[0].message.content.strip()
            app.logger.info(f"xAI Grok 回應成功，用時 {time.time() - grok_start:.2f} 秒。")
        # --- 使用 OpenAI 的錯誤類型 ---
        except AuthenticationError as e:
            app.logger.error(f"xAI Grok API 認證錯誤 (請再次檢查 API Key!) for user {user_id}: {e}", exc_info=False) # 只印關鍵錯誤訊息
            grok_response = "抱歉，AI 服務的鑰匙好像錯了或失效了，請聯繫管理員檢查設定。"
        except RateLimitError as e:
            app.logger.warning(f"xAI Grok API 達到速率限制 for user {user_id}: {e}")
            grok_response = "抱歉，我的大腦有點過熱，請稍等一下再問我。"
        except APIConnectionError as e:
            app.logger.error(f"xAI Grok API 連接錯誤 for user {user_id}: {e}")
            grok_response = "抱歉，我現在連不上我的 AI 大腦，請稍後再試。"
        except APITimeoutError as e: # OpenAI 的超時錯誤
            app.logger.warning(f"xAI Grok API 呼叫超時 for user {user_id}: {e}")
            grok_response = "抱歉，我想得有點久，可以試著換個問法或稍後再試嗎？"
        except APIStatusError as e: # 其他 API 狀態錯誤 (4xx, 5xx)
             app.logger.error(f"xAI Grok API 狀態錯誤 for user {user_id}: status_code={e.status_code}, response={e.response}")
             grok_response = "抱歉，AI 服務好像不太舒服，請稍後再試。"
        except Exception as e: # 捕捉所有其他未預料的錯誤
            app.logger.error(f"xAI Grok API 或處理時發生未知錯誤 for user {user_id}: {e}", exc_info=True)
            grok_response = "抱歉，處理您的請求時發生了未預期的錯誤。"

        # 6. 將 Grok 回應加入歷史 (不變)
        history.append({"role": "assistant", "content": grok_response})
        if len(history) > MAX_HISTORY_TURNS * 2:
             history = history[-(MAX_HISTORY_TURNS * 2):]

        # 7. 將更新後的歷史存回資料庫 (不變)
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
        else:
             app.logger.warning("無法連接資料庫，歷史紀錄未儲存。")

        # 8. 使用 v2 的 Push API 推送回覆 (不變)
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

# --- LINE Webhook 主要進入點 ---
# (callback 函數保持不變)
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

# --- LINE 訊息事件處理器 ---
# (handle_message 函數保持不變)
@handler.add(MessageEvent, message=TextMessage)
def handle_message(event):
    user_id = event.source.user_id
    Thread(target=process_and_push, args=(user_id, event)).start()

# --- 主程式進入點與初始化 ---
# (這部分不變)
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
else:
     # 在 Gunicorn 啟動時也確保資料庫初始化被呼叫
     init_db()
