import os
import time
import json
import psycopg2
import logging
import httpx
import hashlib
import requests # 保持導入 requests
import datetime # <--- 導入 datetime
from urllib.parse import quote_plus # <--- 導入用於 URL 編碼

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from dotenv import load_dotenv
from openai import OpenAI, RateLimitError, APIConnectionError, AuthenticationError, APITimeoutError, APIStatusError # 保持導入 openai
from threading import Thread

# --- 載入環境變數 ---
load_dotenv()

app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# --- 環境變數與設定 (保持不變) ---
channel_access_token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
channel_secret = os.getenv('LINE_CHANNEL_SECRET')
grok_api_key_from_env = os.getenv('GROK_API_KEY')
DATABASE_URL = os.getenv('DATABASE_URL')
XAI_API_BASE_URL = os.getenv("XAI_API_BASE_URL", "https://api.x.ai/v1")
# 設定時區為台灣時間 (UTC+8)
TAIWAN_TZ = datetime.timezone(datetime.timedelta(hours=8))

# --- 驗證與記錄 API Key (保持不變) ---
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

if not all([channel_access_token, channel_secret, grok_api_key]):
    app.logger.error("錯誤：必要的環境變數 (LINE Token, Grok/xAI API Key) 未完整設定！")
    exit()
if not DATABASE_URL:
    app.logger.error("錯誤：DATABASE_URL 未設定！請在 Render 連接資料庫。")
    exit()

# --- Line Bot SDK 初始化 (保持不變) ---
try:
    line_bot_api = LineBotApi(channel_access_token)
    handler = WebhookHandler(channel_secret)
    app.logger.info("Line Bot SDK v2 初始化成功。")
except Exception as e:
    app.logger.error(f"無法初始化 Line Bot SDK: {e}")
    exit()

# --- 初始化 OpenAI Client (保持不變) ---
ai_client = None
try:
    app.logger.info(f"準備初始化 OpenAI client for xAI Grok，目標 URL: {XAI_API_BASE_URL}")
    ai_client = OpenAI(
        api_key=grok_api_key,
        base_url=XAI_API_BASE_URL
    )
    # --- API Key 測試 (保持不變) ---
    try:
        app.logger.info("嘗試使用 xAI API Key 獲取模型列表...")
        models = ai_client.models.list()
        app.logger.info(f">>> xAI API Key 測試 (模型列表) 成功，模型數: {len(models.data)}")
        if models.data:
            app.logger.info(f"    部分可用模型: {[m.id for m in models.data[:5]]}")
    except AuthenticationError as e:
        app.logger.error(f"!!! xAI API Key 測試 (模型列表) 失敗: 認證錯誤 (401)!", exc_info=False)
    except Exception as e:
        app.logger.error(f"!!! xAI API Key 測試 (模型列表) 發生其他錯誤: {type(e).__name__}: {e}", exc_info=True)
    app.logger.info("OpenAI client for xAI Grok 初始化流程完成。")
except Exception as e:
    app.logger.error(f"無法初始化 OpenAI client for xAI: {e}", exc_info=True)

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

# --- 網頁/搜尋結果獲取函數 (修改名稱，功能類似) ---
def fetch_content_from_url(url):
    """從指定 URL 同步獲取網頁文字內容 (用於查詢或搜尋結果頁)"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9,zh-TW;q=0.8,zh;q=0.7', # 增加語言偏好
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
        }
        app.logger.info(f"開始獲取 URL 內容: {url}")
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        # 這裡可以考慮用 BeautifulSoup 等工具解析 HTML 提取主要內容，但目前先返回部分原始文本
        content_type = response.headers.get('content-type', '').lower()
        if 'html' in content_type:
             # 簡單處理：返回前 3000 字元 (後續可優化為提取主要文本)
             content = response.text[:3000]
        elif 'text' in content_type:
             content = response.text[:3000]
        else:
             content = f"無法處理的內容類型: {content_type}"
             app.logger.warning(f"URL: {url} 的內容類型不支援 ({content_type})")

        app.logger.info(f"成功獲取 URL: {url}, 內容長度: {len(content)}")
        return content
    except requests.exceptions.Timeout:
        app.logger.error(f"獲取 URL 超時: {url}")
        return f"無法獲取內容：請求超時"
    except requests.exceptions.RequestException as e:
        app.logger.error(f"獲取 URL 內容失敗: {url}, Error: {e}")
        return f"無法獲取內容：{str(e)}"
    except Exception as e:
        app.logger.error(f"處理 URL 獲取時未知錯誤: {url}, Error: {e}", exc_info=True)
        return f"處理 URL 獲取時發生錯誤：{str(e)}"

# --- 背景處理函數 (加入目前時間 & 搜尋功能) ---
def process_and_push(user_id, event):
    user_text_original = event.message.text
    app.logger.info(f"開始處理 user {user_id} 的訊息: '{user_text_original[:50]}...'")
    start_process_time = time.time()
    conn = None
    history = []
    grok_response = "抱歉，系統發生錯誤或無法連接 AI 服務。"

    # --- 檢查 AI Client 是否成功初始化 (保持不變) ---
    if ai_client is None:
        app.logger.error(f"AI Client (for xAI) 未成功初始化，無法處理 user {user_id} 的請求。")
        try:
            line_bot_api.push_message(user_id, messages=TextSendMessage(text="抱歉，AI 服務設定錯誤，無法處理您的請求。"))
        except Exception as push_err:
             app.logger.error(f"推送 AI Client 錯誤訊息失敗: {push_err}")
        return
    # --- 結束檢查 ---

    try:
        # --- 1. 獲取並準備當前時間 ---
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        now_taiwan = now_utc.astimezone(TAIWAN_TZ)
        current_time_str = now_taiwan.strftime("%Y年%m月%d日 %H:%M:%S")
        # 建立系統時間提示
        system_time_prompt = {"role": "system", "content": f"目前的日期與時間是：{current_time_str} (台灣時間 UTC+8)。請根據此資訊回答時間相關問題，並注意這不是你的知識截止日期。"}
        app.logger.info(f"準備的系統時間提示: {system_time_prompt['content']}")

        # --- 2. 讀取歷史紀錄 (保持修正後的邏輯) ---
        conn = get_db_connection()
        if conn:
             try:
                with conn.cursor() as cur:
                    cur.execute("SELECT history FROM conversation_history WHERE user_id = %s;", (user_id,))
                    result = cur.fetchone()
                    if result and result[0]:
                        db_data = result[0]
                        if isinstance(db_data, list):
                            history = db_data
                            app.logger.info(f"成功直接使用 DB 返回的列表歷史，長度: {len(history)}")
                        elif isinstance(db_data, str):
                             try:
                                history = json.loads(db_data)
                                if isinstance(history, list): app.logger.info(f"成功解析 DB 返回的 JSON 字串歷史，長度: {len(history)}")
                                else: history = []
                             except json.JSONDecodeError: history = []
                        else: history = []
                    else: app.logger.info(f"無 user {user_id} 的歷史紀錄。")
             except (Exception, psycopg2.DatabaseError) as db_err:
                app.logger.error(f"讀取 user {user_id} 的 DB 歷史時發生錯誤: {db_err}", exc_info=True)
                history = []
                if conn and not conn.closed: conn.rollback()
        else:
             app.logger.warning("無法連接資料庫，將不使用歷史紀錄。")

        # --- 3. 處理聯網指令 (查詢 或 搜尋) ---
        user_text_for_llm = user_text_original
        web_info_prompt = None # 用於存放網頁或搜尋結果的提示部分

        if user_text_original.startswith("查詢："):
            url = user_text_original[3:].strip()
            if url:
                app.logger.info(f"偵測到查詢指令，目標 URL: {url}")
                web_content = fetch_content_from_url(url) # 使用通用函數
                if web_content and not web_content.startswith("無法獲取") and not web_content.startswith("處理網頁"):
                    web_info_prompt = f"這是你查詢的網址 '{url}' 的部分內容，請根據它來回答問題或提供總結：\n\n```html\n{web_content}\n```\n\n"
                    app.logger.info("已準備包含網頁內容的提示。")
                else:
                    web_info_prompt = f"我嘗試查詢網址 '{url}' 但失敗了：{web_content}。請告知使用者這個情況。"
                    app.logger.warning(f"網頁查詢失敗，將通知 AI。")
                # 將原始查詢問題保留給 LLM
                user_text_for_llm = f"使用者想查詢網址 '{url}' 的相關資訊。"

        elif user_text_original.startswith("搜尋："):
            query = user_text_original[3:].strip()
            if query:
                app.logger.info(f"偵測到搜尋指令，關鍵字: {query}")
                # 使用 DuckDuckGo 的 HTML 版本進行簡單搜尋
                search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
                search_result_content = fetch_content_from_url(search_url)
                if search_result_content and not search_result_content.startswith("無法獲取") and not search_result_content.startswith("處理網頁"):
                    # 傳遞部分原始 HTML 給 LLM，讓它嘗試提取重點
                    web_info_prompt = f"這是關於 '{query}' 的 DuckDuckGo 搜尋結果頁的部分內容，請根據這些資訊回答使用者的問題或提供總結：\n\n```html\n{search_result_content}\n```\n\n"
                    app.logger.info("已準備包含搜尋結果內容的提示。")
                else:
                     web_info_prompt = f"我嘗試搜尋 '{query}' 但失敗了：{search_result_content}。請告知使用者這個情況。"
                     app.logger.warning(f"搜尋失敗，將通知 AI。")
                # 將原始搜尋問題保留給 LLM
                user_text_for_llm = f"使用者想搜尋關於 '{query}' 的資訊。"

        # --- 4. 組合最終的提示訊息列表 ---
        # 先加入系統時間提示
        prompt_messages = [system_time_prompt]
        # 再加入歷史紀錄
        prompt_messages.extend(history)
        # 如果有網頁或搜尋結果，加入相關提示，然後才是使用者這次的訊息
        if web_info_prompt:
             prompt_messages.append({"role": "system", "content": web_info_prompt}) # 作為系統補充資訊
        # 最後加入使用者這次的訊息 (可能是原始訊息，或被聯網指令修改過的指示性訊息)
        prompt_messages.append({"role": "user", "content": user_text_for_llm})

        # --- 清理過長的歷史 (在加入系統提示和使用者訊息後再做一次，確保總長度) ---
        # 我們需要保留 system_time_prompt 和最後的 user message
        # 只對中間的 history 部分進行裁剪
        if len(prompt_messages) > (MAX_HISTORY_TURNS * 2 + 3): # +3 for system_time, web_info(if any), current_user
             # 計算要保留的 history 條數
             num_history_to_keep = MAX_HISTORY_TURNS * 2
             # 提取頭部(系統時間), 尾部(web_info+目前使用者)
             head_prompt = prompt_messages[0]
             tail_prompts = prompt_messages[-(1 + (1 if web_info_prompt else 0)):] # last 1 or 2 messages
             # 提取中間的歷史紀錄
             middle_history = prompt_messages[1:-(1 + (1 if web_info_prompt else 0))]
             # 裁剪中間歷史
             trimmed_middle_history = middle_history[-num_history_to_keep:]
             # 重新組合
             prompt_messages = [head_prompt] + trimmed_middle_history + tail_prompts
             app.logger.info(f"歷史記錄過長，已裁剪。裁剪後總長度: {len(prompt_messages)}")

        # --- 5. 呼叫 xAI Grok API (保持不變) ---
        try:
            grok_start = time.time()
            app.logger.info(f"準備呼叫 xAI Grok (model: grok-3-mini-beta) for user {user_id}...")
            app.logger.debug(f"傳送給 AI 的最終 messages: {prompt_messages}") # Debug: 印出最終訊息
            chat_completion = ai_client.chat.completions.create(
                messages=prompt_messages,
                model="grok-3-mini-beta",
                temperature=0.7,
                max_tokens=1500,
            )
            grok_response = chat_completion.choices[0].message.content.strip()
            app.logger.info(f"xAI Grok 回應成功，用時 {time.time() - grok_start:.2f} 秒。")
        # (錯誤處理保持不變)
        except AuthenticationError as e: app.logger.error(f"xAI Grok API 認證錯誤: {e}", exc_info=False); grok_response = "抱歉，AI 服務鑰匙錯誤。"
        except RateLimitError as e: app.logger.warning(f"xAI Grok API 速率限制: {e}"); grok_response = "抱歉，大腦過熱。"
        except APIConnectionError as e: app.logger.error(f"xAI Grok API 連接錯誤: {e}"); grok_response = "抱歉，AI 無法連線。"
        except APITimeoutError as e: app.logger.warning(f"xAI Grok API 呼叫超時: {e}"); grok_response = "抱歉，思考超時。"
        except APIStatusError as e: app.logger.error(f"xAI Grok API 狀態錯誤: status={e.status_code}, response={e.response}"); grok_response = "抱歉，AI 服務異常。"
        except Exception as e: app.logger.error(f"xAI Grok 未知錯誤: {e}", exc_info=True); grok_response = "抱歉，系統發生錯誤。"


        # --- 6. 將實際的「使用者原始輸入」和「AI回應」加入要儲存的歷史 ---
        # 注意：儲存時不包含系統時間提示或網頁內容提示，只存 user 和 assistant 的對話
        history_to_save = history # 讀取出來的歷史
        history_to_save.append({"role": "user", "content": user_text_original}) # 存原始 User 輸入
        history_to_save.append({"role": "assistant", "content": grok_response}) # 存 AI 回應
        # 裁剪要儲存的歷史
        if len(history_to_save) > MAX_HISTORY_TURNS * 2:
             history_to_save = history_to_save[-(MAX_HISTORY_TURNS * 2):]


        # --- 7. 將更新後的歷史存回資料庫 (保持不變) ---
        if conn:
            try:
                if conn.closed: conn = get_db_connection()
                with conn.cursor() as cur:
                    history_json_string = json.dumps(history_to_save, ensure_ascii=False)
                    cur.execute("""
                        INSERT INTO conversation_history (user_id, history)
                        VALUES (%s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET history = EXCLUDED.history;
                    """, (user_id, history_json_string))
                    conn.commit()
                app.logger.info(f"歷史儲存成功 (長度: {len(history_to_save)}, 已存為 JSON 字串)。")
            except Exception as db_err:
                app.logger.error(f"儲存歷史錯誤: {db_err}", exc_info=True)
                if conn and not conn.closed: conn.rollback()
        else:
             app.logger.warning("無法連接資料庫，歷史紀錄未儲存。")


        # --- 8. 推送回覆 (保持不變) ---
        app.logger.info(f"準備推送回應給 user {user_id}: {grok_response[:50]}...")
        try:
            line_bot_api.push_message(user_id, TextSendMessage(text=grok_response))
            app.logger.info(f"訊息推送完成。")
        except LineBotApiError as e: app.logger.error(f"LINE API 錯誤: {e.status_code} {e.error.message}")
        except Exception as e: app.logger.error(f"推送訊息錯誤: {e}", exc_info=True)

    except Exception as e:
        app.logger.error(f"處理 user {user_id} 時錯誤: {type(e).__name__}: {e}", exc_info=True)
    finally:
        if conn and not conn.closed:
            conn.close()
        app.logger.info(f"任務完成，用時 {time.time() - start_process_time:.2f} 秒。")


# --- LINE Webhook 與事件處理器 (保持不變) ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info(f"收到請求: {body[:100]}")
    try:
        handler.handle(body, signature)
    except InvalidSignatureError: app.logger.error("簽名錯誤。"); abort(400)
    except LineBotApiError as e: app.logger.error(f"LINE API 錯誤: {e.status_code} {e.error.message}"); abort(500)
    except Exception as e: app.logger.error(f"Webhook 錯誤: {e}", exc_info=True); abort(500)
    return 'OK'

@handler.add(MessageEvent, message=TextMessage)
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
