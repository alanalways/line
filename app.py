import os
import time
import json
import psycopg2
import logging
import httpx
import hashlib
import requests
import datetime
from urllib.parse import quote_plus
from bs4 import BeautifulSoup # <--- 導入 BeautifulSoup

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
# 加入 ImageMessage 用於接收圖片
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageMessage # <--- 加入 ImageMessage

from dotenv import load_dotenv
from openai import OpenAI, RateLimitError, APIConnectionError, AuthenticationError, APITimeoutError, APIStatusError
from threading import Thread

# --- 載入環境變數 & 基本設定 ---
load_dotenv()
app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# ... (省略環境變數讀取、驗證、API Key 記錄程式碼，與之前版本相同) ...
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

# --- 初始化 OpenAI Client (不變) ---
ai_client = None
try:
    app.logger.info(f"準備初始化 OpenAI client for xAI Grok，目標 URL: {XAI_API_BASE_URL}")
    ai_client = OpenAI(api_key=grok_api_key, base_url=XAI_API_BASE_URL)
    # (省略 API Key 測試程式碼)
    app.logger.info("OpenAI client for xAI Grok 初始化流程完成。")
except Exception as e: app.logger.error(f"無法初始化 OpenAI client for xAI: {e}", exc_info=True)

# --- 其他設定與函數 ---
MAX_HISTORY_TURNS = 5
SEARCH_KEYWORDS = ["天氣", "新聞", "股價", "今日", "今天", "最新", "誰是", "什麼是", "介紹", "查詢", "搜尋"] # 觸發自動搜尋的關鍵字 (可擴充)

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

# --- 網頁/搜尋結果獲取函數 (使用 BeautifulSoup 提取文字) ---
def fetch_and_extract_text(url):
    """從指定 URL 獲取內容並使用 BeautifulSoup 提取文字摘要"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9,zh-TW;q=0.8,zh;q=0.7',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
        }
        app.logger.info(f"開始獲取 URL 內容: {url}")
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        content_type = response.headers.get('content-type', '').lower()

        if 'html' in content_type:
             soup = BeautifulSoup(response.content, 'html.parser') # 使用 response.content 避免編碼問題
             # 移除不必要的標籤
             for element in soup(["script", "style", "header", "footer", "nav", "aside"]):
                 element.decompose()
             # 獲取主要文字內容
             text_parts = [p.get_text(strip=True) for p in soup.find_all(['p', 'h1', 'h2', 'h3', 'li', 'span', 'div'])] # 嘗試提取多種標籤
             text = '\n'.join(filter(None, text_parts))
             summary = text[:2500] # 限制摘要長度
             app.logger.info(f"成功獲取並解析 HTML: {url}, 摘要長度: {len(summary)}")
             return summary if summary else "無法從 HTML 中提取有效文字摘要。"
        elif 'text' in content_type:
             content = response.text[:3000]
             app.logger.info(f"成功獲取純文字: {url}, 長度: {len(content)}")
             return content
        else:
             app.logger.warning(f"URL: {url} 的內容類型不支援 ({content_type})")
             return f"獲取失敗：無法處理的內容類型 ({content_type})"

    except requests.exceptions.Timeout: app.logger.error(f"獲取 URL 超時: {url}"); return "獲取失敗：請求超時"
    except requests.exceptions.RequestException as e: app.logger.error(f"獲取 URL 內容失敗: {url}, Error: {e}"); return f"獲取失敗：{str(e)}"
    except Exception as e: app.logger.error(f"處理 URL 獲取時未知錯誤: {url}, Error: {e}", exc_info=True); return f"處理 URL 獲取時發生錯誤：{str(e)}"


# --- 背景處理函數 (加入自動搜尋判斷, DB日誌, 圖片框架) ---
def process_and_push(user_id, event):
    user_text_original = ""
    image_info = None # 用於未來處理圖片資訊

    # --- 判斷訊息類型 ---
    if isinstance(event.message, TextMessage):
        user_text_original = event.message.text
        app.logger.info(f"收到來自 user {user_id} 的文字訊息: '{user_text_original[:50]}...'")
    elif isinstance(event.message, ImageMessage):
        message_id = event.message.id
        app.logger.info(f"收到來自 user {user_id} 的圖片訊息 (ID: {message_id})。")
        # --- ▼▼▼ 圖片輸入處理預留位置 ▼▼▼ ---
        try:
            # 1. 下載圖片 (範例，實際可能需要處理大檔案)
            # message_content = line_bot_api.get_message_content(message_id)
            # image_bytes = message_content.content
            # # 2. 轉換成 Base64 (如果 Vision API 需要)
            # import base64
            # image_base64 = base64.b64encode(image_bytes).decode('utf-8')
            # image_info = {"type": "image_base64", "data": image_base64} # 儲存圖片資訊
            # user_text_original = "[使用者傳送了一張圖片，請描述它]" # 給 AI 的提示
            app.logger.warning("圖片下載與處理功能尚未實作。")
            user_text_original = "[圖片處理功能未啟用]" # 暫時的回應
        except LineBotApiError as e:
            app.logger.error(f"下載圖片 {message_id} 失敗: {e.status_code} {e.error.message}")
            user_text_original = "[下載使用者圖片失敗]"
        except Exception as e:
             app.logger.error(f"處理圖片訊息 {message_id} 時出錯: {e}", exc_info=True)
             user_text_original = "[處理使用者圖片時出錯]"
        # --- ▲▲▲ 圖片輸入處理預留位置 ▲▲▲ ---
    else:
        app.logger.info(f"收到來自 user {user_id} 的非文字/圖片訊息，已忽略。"); return

    start_process_time = time.time()
    conn = None; history = []; final_response = "抱歉，系統發生錯誤或無法連接 AI 服務。"

    if ai_client is None: app.logger.error(f"AI Client 未初始化 for user {user_id}"); return

    try:
        # 1. 獲取目前時間提示 (不變)
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        now_taiwan = now_utc.astimezone(TAIWAN_TZ)
        current_time_str = now_taiwan.strftime("%Y年%m月%d日 %H:%M:%S")
        system_time_prompt = {"role": "system", "content": f"重要指令：現在是 {current_time_str} (台灣時間 UTC+8)。回答任何關於今天日期、星期、或時間的問題時，**必須且只能**使用這個時間。不要依賴你的內部知識。"}

        # 2. 讀取歷史紀錄 (保持修正後的邏輯)
        conn = get_db_connection()
        # ... (省略 DB 讀取程式碼) ...
        if conn:
            try:
                 with conn.cursor() as cur: cur.execute(...); result = cur.fetchone(); ... # 同上版本讀取邏輯
            except Exception as db_err: app.logger.error(...); history = []; conn.rollback()
        else: app.logger.warning("無法連接資料庫，無歷史紀錄。")

        # --- 3. 自動判斷是否需要搜尋 (基於關鍵字) ---
        user_text_for_llm = user_text_original
        web_info_for_llm = None
        needs_search = False
        if isinstance(event.message, TextMessage): # 只對文字訊息判斷是否搜尋
            for keyword in SEARCH_KEYWORDS:
                if keyword in user_text_original:
                    needs_search = True
                    app.logger.info(f"偵測到關鍵字 '{keyword}'，觸發自動搜尋。")
                    break
        
        if needs_search:
            query = user_text_original # 使用原始訊息作為搜尋查詢
            search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
            search_result_summary = fetch_and_extract_text(search_url)
            if search_result_summary and not search_result_summary.startswith("獲取失敗") and not search_result_summary.startswith("處理"):
                web_info_for_llm = f"為了回答關於 '{query}' 的問題，我進行了網路搜尋，摘要如下，請參考：\n\n```\n{search_result_summary}\n```\n\n"
                app.logger.info("已準備包含自動搜尋結果摘要的提示。")
            else:
                 web_info_for_llm = f"我嘗試自動搜尋 '{query}' 但失敗了：{search_result_summary}。請告知使用者。"
                 app.logger.warning(f"自動搜尋失敗，將通知 AI。")

        # --- 4. 組合提示訊息列表 ---
        prompt_messages = [system_time_prompt] + history
        if web_info_for_llm: prompt_messages.append({"role": "system", "content": web_info_for_llm})
        # --- ▼▼▼ 圖片輸入(Vision) 提示預留 ▼▼▼ ---
        # if image_info and image_info["type"] == "image_base64":
        #     # 建立包含圖片的訊息結構 (需符合 OpenAI/xAI 格式)
        #     image_prompt_part = {
        #          "type": "image_url",
        #          "image_url": {"url": f"data:image/jpeg;base64,{image_info['data']}"} # 假設是 JPEG
        #     }
        #     user_message_content = [{"type": "text", "text": user_text_for_llm}, image_prompt_part]
        #     prompt_messages.append({"role": "user", "content": user_message_content})
        # else: # 如果沒有圖片，正常加入文字訊息
        # --- ▲▲▲ 圖片輸入(Vision) 提示預留 ▲▲▲ ---
        prompt_messages.append({"role": "user", "content": user_text_for_llm}) # 目前只處理文字

        # (歷史裁剪邏輯保持不變)
        if len(prompt_messages) > (MAX_HISTORY_TURNS * 2 + 3): # +3 for system_time, web_info(if any), current_user
             num_history_to_keep = MAX_HISTORY_TURNS * 2; head_prompt = prompt_messages[0]; tail_prompts = prompt_messages[-(1 + (1 if web_info_for_llm else 0)):]; middle_history = prompt_messages[1:-(1 + (1 if web_info_for_llm else 0))]; trimmed_middle_history = middle_history[-num_history_to_keep:]
             prompt_messages = [head_prompt] + trimmed_middle_history + tail_prompts
             app.logger.info(f"歷史記錄過長，已裁剪。裁剪後總長度: {len(prompt_messages)}")

        # 5. 呼叫 xAI Grok API
        try:
            grok_start = time.time()
            target_model = "grok-3-mini-beta" # 預設文字模型
            # --- ▼▼▼ 圖片輸入(Vision) 模型選擇預留 ▼▼▼ ---
            # if image_info:
            #    target_model = "grok-vision-beta" # 或其他你列表中的視覺模型
            #    app.logger.info(f"準備呼叫 xAI Grok Vision ({target_model})...")
            # else:
            # --- ▲▲▲ 圖片輸入(Vision) 模型選擇預留 ▲▲▲ ---
            app.logger.info(f"準備呼叫 xAI Grok ({target_model}) for user {user_id}...")

            # Debug: 印出最終提示的主要部分
            if len(prompt_messages) > 1:
                app.logger.debug(f"傳送給 AI 的最後 User/System 訊息: {prompt_messages[-2:]}")
            else:
                app.logger.debug(f"傳送給 AI 的訊息: {prompt_messages}")

            chat_completion = ai_client.chat.completions.create(
                messages=prompt_messages,
                model=target_model,
                temperature=0.7,
                max_tokens=1500,
            )
            final_response = chat_completion.choices[0].message.content.strip()
            app.logger.info(f"xAI Grok 回應成功，用時 {time.time() - grok_start:.2f} 秒。")
        # (錯誤處理保持不變)
        except AuthenticationError as e: app.logger.error(f"xAI Grok API 認證錯誤: {e}", exc_info=False); final_response = "抱歉，AI 服務鑰匙錯誤。"
        except RateLimitError as e: app.logger.warning(f"xAI Grok API 速率限制: {e}"); final_response = "抱歉，大腦過熱。"
        except APIConnectionError as e: app.logger.error(f"xAI Grok API 連接錯誤: {e}"); final_response = "抱歉，AI 無法連線。"
        except APITimeoutError as e: app.logger.warning(f"xAI Grok API 呼叫超時: {e}"); final_response = "抱歉，思考超時。"
        except APIStatusError as e: app.logger.error(f"xAI Grok API 狀態錯誤: status={e.status_code}, response={e.response}"); final_response = "抱歉，AI 服務異常。"
        except Exception as e: app.logger.error(f"xAI Grok 未知錯誤: {e}", exc_info=True); final_response = "抱歉，系統發生錯誤。"

        # 6. 儲存實際對話歷史 (加入 debug 日誌)
        history_to_save = history
        # --- ▼▼▼ 圖片輸入歷史記錄預留 ▼▼▼ ---
        # 如果是圖片輸入，user_text_original 可能是 "[使用者傳送圖片...]"
        # --- ▲▲▲ 圖片輸入歷史記錄預留 ▲▲▲ ---
        history_to_save.append({"role": "user", "content": user_text_original}) # 存原始 User 輸入 (或圖片提示)
        history_to_save.append({"role": "assistant", "content": final_response}) # 存 AI 回應
        if len(history_to_save) > MAX_HISTORY_TURNS * 2: history_to_save = history_to_save[-(MAX_HISTORY_TURNS * 2):]

        # 7. 存回資料庫 (加入 debug 日誌)
        if conn:
            try:
                if conn.closed: conn = get_db_connection()
                with conn.cursor() as cur:
                    history_json_string = json.dumps(history_to_save, ensure_ascii=False)
                    # --- ▼▼▼ DB 儲存前的日誌 (保持) ▼▼▼ ---
                    app.logger.info(f"準備儲存歷史 for user_id: {user_id}")
                    log_string = history_json_string[:200] + "..." + history_json_string[-200:] if len(history_json_string) > 400 else history_json_string
                    app.logger.info(f"History JSON string (cleaned, partial): {log_string}") #<-- 檢查這個日誌！
                    # --- ▲▲▲ 結束 DB 儲存前的日誌 ▲▲▲ ---
                    # --- ▼▼▼ 嘗試移除 NULL 字元修復 DB 錯誤 ▼▼▼ ---
                    cleaned_history_json_string = history_json_string.replace('\x00', '') # 移除 NULL byte
                    if history_json_string != cleaned_history_json_string:
                        app.logger.warning("在儲存前移除了 JSON 字串中的 NULL bytes!")
                    # --- ▲▲▲ 結束移除 NULL 字元 ▲▲▲ ---
                    cur.execute("""
                        INSERT INTO conversation_history (user_id, history)
                        VALUES (%s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET history = EXCLUDED.history;
                    """, (user_id, cleaned_history_json_string)) # 使用清理過的字串
                    conn.commit()
                app.logger.info(f"歷史儲存成功 (長度: {len(history_to_save)})。")
            except (Exception, psycopg2.DatabaseError) as db_err:
                app.logger.error(f"儲存歷史錯誤 for user {user_id}: {type(db_err).__name__} - {db_err}", exc_info=True)
                if conn and not conn.closed: conn.rollback()
        else: app.logger.warning("無法連接資料庫，歷史紀錄未儲存。")

        # 8. 推送回覆 (保持不變，圖片輸出預留)
        app.logger.info(f"準備推送回應給 user {user_id}: {final_response[:50]}...")
        try:
            # --- ▼▼▼ 圖片輸出預留位置 ▼▼▼ ---
            # if 需要回傳圖片:
            #    from linebot.models import ImageSendMessage
            #    line_bot_api.push_message(user_id, ImageSendMessage(original_content_url=圖片URL, preview_image_url=圖片URL))
            # else: # 回傳文字
            # --- ▲▲▲ 圖片輸出預留位置 ▲▲▲ ---
            line_bot_api.push_message(user_id, TextSendMessage(text=final_response))
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


# --- LINE Webhook 與事件處理器 (加入 ImageMessage 處理) ---
@app.route("/callback", methods=['POST'])
def callback():
    # ... (保持不變) ...
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info(f"收到請求: {body[:100]}")
    try: handler.handle(body, signature)
    except InvalidSignatureError: app.logger.error("簽名錯誤。"); abort(400)
    except LineBotApiError as e: app.logger.error(f"LINE API 錯誤: {e.status_code} {e.error.message}"); abort(500)
    except Exception as e: app.logger.error(f"Webhook 錯誤: {e}", exc_info=True); abort(500)
    return 'OK'

# --- 修改 handler 以接收圖片訊息 ---
@handler.add(MessageEvent, message=(TextMessage, ImageMessage)) # <--- 接收文字和圖片
def handle_message(event):
    user_id = event.source.user_id
    # 啟動背景執行緒，不論是文字或圖片都一樣處理 (具體邏輯在 process_and_push 裡面判斷)
    Thread(target=process_and_push, args=(user_id, event)).start()

# --- 主程式進入點 (保持不變) ---
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
else:
     init_db()
