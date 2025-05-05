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
from bs4 import BeautifulSoup
# --- ▼▼▼ 新增導入 ▼▼▼ ---
from psycopg2.extras import Json # <--- 導入 Json Adapter
# --- ▲▲▲ 結束導入 ▲▲▲ ---

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageMessage

from dotenv import load_dotenv
from openai import OpenAI, RateLimitError, APIConnectionError, AuthenticationError, APITimeoutError, APIStatusError
from threading import Thread

# --- 載入環境變數 & 基本設定 (保持不變) ---
# ... (省略) ...
load_dotenv()
app = Flask(__name__)
app.logger.setLevel(logging.INFO)
channel_access_token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
channel_secret = os.getenv('LINE_CHANNEL_SECRET')
grok_api_key_from_env = os.getenv('GROK_API_KEY')
DATABASE_URL = os.getenv('DATABASE_URL')
XAI_API_BASE_URL = os.getenv("XAI_API_BASE_URL", "https://api.x.ai/v1")
TAIWAN_TZ = datetime.timezone(datetime.timedelta(hours=8))
grok_api_key = None
if grok_api_key_from_env:
    # ... (API Key 檢查) ...
    grok_api_key = grok_api_key_from_env.strip() if grok_api_key_from_env != grok_api_key_from_env.strip() else grok_api_key_from_env
else: exit()
if not all([channel_access_token, channel_secret, grok_api_key]): exit()
if not DATABASE_URL: exit()


# --- Line Bot SDK 初始化 (保持不變) ---
try:
    line_bot_api = LineBotApi(channel_access_token)
    handler = WebhookHandler(channel_secret)
    app.logger.info("Line Bot SDK v2 初始化成功。")
except Exception as e: app.logger.error(f"無法初始化 Line Bot SDK: {e}"); exit()

# --- 初始化 OpenAI Client (保持不變) ---
ai_client = None
try:
    app.logger.info(f"準備初始化 OpenAI client for xAI Grok，目標 URL: {XAI_API_BASE_URL}")
    ai_client = OpenAI(api_key=grok_api_key, base_url=XAI_API_BASE_URL)
    app.logger.info("OpenAI client for xAI Grok 初始化流程完成。")
except Exception as e: app.logger.error(f"無法初始化 OpenAI client for xAI: {e}", exc_info=True)

# --- 其他設定與函數 (DB, 歷史長度, 搜尋關鍵字, 觸發詞, 模型ID) ---
MAX_HISTORY_TURNS = 5
SEARCH_KEYWORDS = ["天氣", "新聞", "股價", "今日", "今天", "最新", "誰是", "什麼是", "介紹", "查詢", "搜尋"]
IMAGE_GEN_TRIGGER = "畫一張："
VISION_MODEL = "grok-2-vision-1212"
IMAGE_GEN_MODEL = "grok-2-image-1212"
TEXT_MODEL = "grok-3-mini-beta"

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

# (fetch_and_extract_text 函數保持不變)
def fetch_and_extract_text(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 ...'}
        app.logger.info(f"開始獲取 URL 內容: {url}")
        response = requests.get(url, headers=headers, timeout=15)
        response.raise_for_status()
        content_type = response.headers.get('content-type', '').lower()
        if 'html' in content_type:
             soup = BeautifulSoup(response.content, 'html.parser')
             for element in soup(["script", "style", "header", "footer", "nav", "aside"]): element.decompose()
             text = '\n'.join(filter(None, (line.strip() for line in soup.stripped_strings)))
             summary = text[:2500]; app.logger.info(f"成功獲取並解析 HTML: {url}, 摘要長度: {len(summary)}")
             return summary if summary else "無法從 HTML 中提取有效文字摘要。"
        elif 'text' in content_type:
             content = response.text[:3000]; app.logger.info(f"成功獲取純文字: {url}, 長度: {len(content)}"); return content
        else: app.logger.warning(f"URL: {url} 的內容類型不支援 ({content_type})"); return f"獲取失敗：無法處理的內容類型 ({content_type})"
    except requests.exceptions.Timeout: app.logger.error(f"獲取 URL 超時: {url}"); return "獲取失敗：請求超時"
    except requests.exceptions.RequestException as e: app.logger.error(f"獲取 URL 內容失敗: {url}, Error: {e}"); return f"獲取失敗：{str(e)}"
    except Exception as e: app.logger.error(f"處理 URL 獲取時未知錯誤: {url}, Error: {e}", exc_info=True); return f"處理 URL 獲取時發生錯誤：{str(e)}"


# --- 背景處理函數 (修改了DB儲存部分) ---
def process_and_push(user_id, event):
    # ... (訊息類型判斷、時間提示、讀取歷史、自動搜尋判斷等邏輯保持不變) ...
    user_text_original = ""; image_data_b64 = None; is_image_gen_request = False; image_gen_prompt = ""
    if isinstance(event.message, TextMessage): user_text_original = event.message.text; ...
    elif isinstance(event.message, ImageMessage): ... ; user_text_original = "[圖片處理功能未啟用]"
    else: return
    if user_text_original.startswith(IMAGE_GEN_TRIGGER): is_image_gen_request = True; ...
    if is_image_gen_request: # 直接處理圖片生成請求並返回
        final_response_message = TextSendMessage(text="[圖片生成功能尚未實作]")
        try: line_bot_api.push_message(user_id, messages=final_response_message)
        except Exception as e: app.logger.error(f"推送圖片生成回應錯誤: {e}")
        return

    start_process_time = time.time()
    conn = None; history = []; final_response = "抱歉，系統發生錯誤或無法連接 AI 服務。"
    if ai_client is None: app.logger.error(f"AI Client 未初始化 for user {user_id}"); return

    try:
        now_utc = datetime.datetime.now(datetime.timezone.utc); now_taiwan = now_utc.astimezone(TAIWAN_TZ); current_time_str = now_taiwan.strftime("%Y年%m月%d日 %H:%M:%S")
        system_prompt = {"role": "system", "content": f"指令：請永遠使用『繁體中文』回答。目前時間是 {current_time_str} (台灣 UTC+8)，回答時間相關問題請以此為準。"}
        conn = get_db_connection()
        # ... (省略 DB 讀取程式碼) ...
        user_text_for_llm = user_text_original; web_info_for_llm = None; should_search_automatically = False
        if isinstance(event.message, TextMessage): # 自動搜尋判斷
             for keyword in SEARCH_KEYWORDS:
                 if keyword in user_text_original: should_search_automatically = True; break
        if should_search_automatically: # 執行自動搜尋
            # ... (省略自動搜尋邏輯) ...
            query = user_text_original; search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"; search_result_summary = fetch_and_extract_text(search_url)
            if search_result_summary and not search_result_summary.startswith("獲取失敗") and not search_result_summary.startswith("處理"): web_info_for_llm = f"為了回答 '{query}'，進行了網路搜尋，摘要如下:\n```\n{search_result_summary}\n```\n\n"
            else: web_info_for_llm = f"嘗試自動搜尋 '{query}' 失敗:{search_result_summary}。"

        prompt_messages = [system_prompt] + history
        if web_info_for_llm: prompt_messages.append({"role": "system", "content": web_info_for_llm})
        prompt_messages.append({"role": "user", "content": user_text_for_llm}) # 只加文字提示
        # (省略歷史裁剪)

        try: # 呼叫 AI
            target_model = TEXT_MODEL # 預設文字模型 (圖片輸入框架未啟用)
            chat_completion = ai_client.chat.completions.create(messages=prompt_messages, model=target_model, ...)
            final_response = chat_completion.choices[0].message.content.strip()
            app.logger.info(f"xAI Grok ({target_model}) 回應成功...")
        except Exception as e: # (省略詳細錯誤處理)
            app.logger.error(f"xAI Grok 呼叫錯誤: {e}", exc_info=True)
            final_response = "抱歉，AI 思考時發生錯誤。"

        # 儲存對話歷史
        history_to_save = history
        history_to_save.append({"role": "user", "content": user_text_original})
        history_to_save.append({"role": "assistant", "content": final_response})
        if len(history_to_save) > MAX_HISTORY_TURNS * 2: history_to_save = history_to_save[-(MAX_HISTORY_TURNS * 2):]

        # --- ▼▼▼ 7. 修正存回資料庫的部分 ▼▼▼ ---
        if conn:
            try:
                if conn.closed: conn = get_db_connection()
                with conn.cursor() as cur:
                    history_json_string = json.dumps(history_to_save, ensure_ascii=False)
                    # 移除 NULL byte (保持)
                    cleaned_history_json_string = history_json_string.replace('\x00', '')
                    if history_json_string != cleaned_history_json_string: app.logger.warning("儲存前移除了 JSON 中的 NULL bytes!")

                    # --- Debug Log (保持) ---
                    app.logger.info(f"準備儲存歷史 for user_id: {user_id}")
                    log_string = cleaned_history_json_string[:200] + "..." + cleaned_history_json_string[-200:] if len(cleaned_history_json_string) > 400 else cleaned_history_json_string
                    app.logger.info(f"History JSON string (cleaned, partial): {log_string}") #<-- 檢查這個日誌！

                    # --- 使用 Json Adapter ---
                    sql_query = """
                        INSERT INTO conversation_history (user_id, history)
                        VALUES (%s, %s)
                        ON CONFLICT (user_id) DO UPDATE SET history = EXCLUDED.history;
                    """
                    # 將 JSON 字串用 Json() 包裹起來
                    cur.execute(sql_query, (user_id, Json(cleaned_history_json_string)))
                    # --- 結束使用 Json Adapter ---

                    conn.commit()
                app.logger.info(f"歷史儲存成功 (長度: {len(history_to_save)})。")
            except (Exception, psycopg2.DatabaseError) as db_err:
                app.logger.error(f"儲存歷史錯誤 for user {user_id}: {type(db_err).__name__} - {db_err}", exc_info=True)
                if conn and not conn.closed: conn.rollback()
        else:
             app.logger.warning("無法連接資料庫，歷史紀錄未儲存。")
        # --- ▲▲▲ 結束修正存回資料庫的部分 ▲▲▲ ---


        # 8. 推送回覆 (保持不變)
        final_response_message = TextSendMessage(text=final_response)
        # ... (省略推送程式碼) ...
        app.logger.info(f"準備推送回應給 user {user_id}: {final_response[:50]}...")
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

# --- LINE Webhook 與事件處理器 (保持不變) ---
# ... (省略 @app.route("/callback") 和 @handler.add(...) 程式碼) ...
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']; body = request.get_data(as_text=True)
    app.logger.info(f"收到請求: {body[:100]}")
    try: handler.handle(body, signature)
    except InvalidSignatureError: app.logger.error("簽名錯誤。"); abort(400)
    except LineBotApiError as e: app.logger.error(f"LINE API 錯誤: {e.status_code} {e.error.message}"); abort(500)
    except Exception as e: app.logger.error(f"Webhook 錯誤: {e}", exc_info=True); abort(500)
    return 'OK'

@handler.add(MessageEvent, message=(TextMessage, ImageMessage))
def handle_message(event):
    user_id = event.source.user_id
    Thread(target=process_and_push, args=(user_id, event)).start()

# --- 主程式進入點 (保持不變) ---
if __name__ == "__main__": init_db(); port = int(os.environ.get("PORT", 5000)); app.run(host="0.0.0.0", port=port)
else: init_db()
