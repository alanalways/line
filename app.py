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

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage, ImageMessage # 加入 ImageMessage

from dotenv import load_dotenv
from openai import OpenAI, RateLimitError, APIConnectionError, AuthenticationError, APITimeoutError, APIStatusError
from threading import Thread

# --- 載入環境變數 & 基本設定 ---
load_dotenv()
app = Flask(__name__)
app.logger.setLevel(logging.INFO)

channel_access_token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
channel_secret = os.getenv('LINE_CHANNEL_SECRET')
grok_api_key_from_env = os.getenv('GROK_API_KEY')
DATABASE_URL = os.getenv('DATABASE_URL')
XAI_API_BASE_URL = os.getenv("XAI_API_BASE_URL", "https://api.x.ai/v1")
TAIWAN_TZ = datetime.timezone(datetime.timedelta(hours=8))

# --- 驗證與記錄 API Key ---
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
    app.logger.info(f"準備初始化 OpenAI client for xAI Grok，目標 URL: {XAI_API_BASE_URL}")
    ai_client = OpenAI(api_key=grok_api_key, base_url=XAI_API_BASE_URL)
    # (省略 API Key 測試程式碼)
    app.logger.info("OpenAI client for xAI Grok 初始化流程完成。")
except Exception as e: app.logger.error(f"無法初始化 OpenAI client for xAI: {e}", exc_info=True)

# --- 其他設定與函數 ---
MAX_HISTORY_TURNS = 5
SEARCH_KEYWORDS = ["天氣", "新聞", "股價", "今日", "今天", "最新", "誰是", "什麼是", "介紹", "搜尋", "查詢"] # 自動搜尋關鍵字
IMAGE_GEN_TRIGGER = "畫一張：" # 圖片生成觸發詞

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
        headers = {'User-Agent': 'Mozilla/5.0 ...'} # 保持不變
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

# --- 背景處理函數 (加入自動搜尋判斷, DB日誌修復嘗試, 圖片框架) ---
def process_and_push(user_id, event):
    user_text_original = ""
    image_info = None # 用於未來處理圖片資訊

    # 判斷訊息類型並提取內容
    if isinstance(event.message, TextMessage):
        user_text_original = event.message.text
        app.logger.info(f"收到文字訊息: '{user_text_original[:50]}...'")
    elif isinstance(event.message, ImageMessage):
        message_id = event.message.id
        app.logger.info(f"收到圖片訊息 (ID: {message_id})。")
        # 【圖片輸入處理預留】
        # 這裡應加入下載圖片、轉換格式、設定 image_info 的程式碼
        user_text_original = "[使用者傳送了一張圖片，請描述它]" # 設定給 AI 的提示
        app.logger.warning("圖片下載與處理功能尚未實作。")
    else:
        app.logger.info(f"忽略非文字/圖片訊息。"); return

    start_process_time = time.time()
    conn = None; history = []; final_response = "抱歉，系統發生錯誤或無法連接 AI 服務。"

    if ai_client is None: app.logger.error(f"AI Client 未初始化"); return

    try:
        # 1. 獲取時間提示 + 固定繁中提示
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        now_taiwan = now_utc.astimezone(TAIWAN_TZ)
        current_time_str = now_taiwan.strftime("%Y年%m月%d日 %H:%M:%S")
        # --- ▼▼▼ 加入固定繁體中文指令 ▼▼▼ ---
        system_prompt = {"role": "system", "content": f"指令：請永遠使用『繁體中文』回答。目前時間是 {current_time_str} (台灣 UTC+8)，回答時間問題請以此為準。"}
        # --- ▲▲▲ 結束加入繁中指令 ▲▲▲ ---
        app.logger.info(f"準備的系統提示: {system_prompt['content']}")

        # 2. 讀取歷史紀錄 (保持修正後的邏輯)
        conn = get_db_connection()
        # ... (省略 DB 讀取程式碼，同上一版本) ...
        if conn:
            try:
                 with conn.cursor() as cur: cur.execute(...); result = cur.fetchone(); ...
            except Exception as db_err: app.logger.error(...); history = []; conn.rollback()
        else: app.logger.warning("無法連接資料庫，無歷史紀錄。")

        # --- 3. 自動搜尋判斷與執行 (關鍵字) ---
        user_text_for_llm = user_text_original
        web_info_for_llm = None
        should_search_automatically = False
        is_image_gen_request = False

        if isinstance(event.message, TextMessage): # 只對文字訊息判斷
            if user_text_original.startswith(IMAGE_GEN_TRIGGER):
                is_image_gen_request = True
                image_prompt = user_text_original[len(IMAGE_GEN_TRIGGER):].strip()
                app.logger.info(f"偵測到圖片生成指令: '{image_prompt}'")
                # 【圖片生成功能預留】
                # 這裡應加入呼叫圖片生成 API 的程式碼
                final_response = "[圖片生成功能尚未實作]"
            else:
                # 檢查是否包含觸發搜尋的關鍵字
                for keyword in SEARCH_KEYWORDS:
                    if keyword in user_text_original:
                        should_search_automatically = True
                        app.logger.info(f"偵測到關鍵字 '{keyword}'，觸發自動搜尋。")
                        break
        # 【圖片輸入 Vision 預留】
        # elif image_info: # 如果收到了圖片
        #    should_search_automatically = False # 通常收到圖片不需要再搜尋

        if should_search_automatically and not is_image_gen_request:
            query = user_text_original
            search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
            search_result_summary = fetch_and_extract_text(search_url)
            if search_result_summary and not search_result_summary.startswith("獲取失敗") and not search_result_summary.startswith("處理"):
                web_info_for_llm = f"為了回答關於 '{query}' 的問題，進行了網路搜尋，摘要如下，請參考：\n```\n{search_result_summary}\n```\n\n"
                app.logger.info("已準備包含自動搜尋結果摘要的提示。")
            else:
                 web_info_for_llm = f"嘗試自動搜尋 '{query}' 失敗了：{search_result_summary}。請告知使用者。"
                 app.logger.warning(f"自動搜尋失敗，將通知 AI。")

        # --- 如果是圖片生成請求，直接跳過後續步驟 ---
        if is_image_gen_request:
            try: line_bot_api.push_message(user_id, TextSendMessage(text=final_response))
            except Exception as e: app.logger.error(f"推送圖片生成回應錯誤: {e}")
            if conn and not conn.closed: conn.close()
            app.logger.info(f"圖片生成請求處理完畢 (暫定)。")
            return

        # 4. 組合提示訊息列表
        prompt_messages = [system_prompt] + history
        if web_info_for_llm: prompt_messages.append({"role": "system", "content": web_info_for_llm})
        # --- 圖片輸入提示預留 ---
        # if image_info ... : prompt_messages.append({"role": "user", "content": [...]})
        # else:
        prompt_messages.append({"role": "user", "content": user_text_for_llm})
        # (歷史裁剪邏輯保持不變)
        if len(prompt_messages) > (MAX_HISTORY_TURNS * 2 + 3):
            # ... (省略裁剪程式碼) ...
             num_history_to_keep = MAX_HISTORY_TURNS * 2; head_prompt = prompt_messages[0]; tail_prompts = prompt_messages[-(1 + (1 if web_info_for_llm else 0)):]; middle_history = prompt_messages[1:-(1 + (1 if web_info_for_llm else 0))]; trimmed_middle_history = middle_history[-num_history_to_keep:]
             prompt_messages = [head_prompt] + trimmed_middle_history + tail_prompts
             app.logger.info(f"歷史記錄過長，已裁剪。裁剪後總長度: {len(prompt_messages)}")


        # 5. 呼叫 xAI Grok API (含 Vision 模型選擇預留)
        try:
            grok_start = time.time()
            target_model = "grok-3-mini-beta"
            # --- Vision 模型選擇預留 ---
            # if image_info: target_model = "grok-vision-beta"
            app.logger.info(f"準備呼叫 xAI Grok ({target_model}) for user {user_id}...")
            chat_completion = ai_client.chat.completions.create(messages=prompt_messages, model=target_model, ...) # ...
            final_response = chat_completion.choices[0].message.content.strip()
            app.logger.info(f"xAI Grok 回應成功，用時 {time.time() - grok_start:.2f} 秒。")
        # (錯誤處理保持不變)
        except AuthenticationError as e: app.logger.error(f"xAI Grok API 認證錯誤: {e}", exc_info=False); final_response = "抱歉，AI 服務鑰匙錯誤。"
        # ... 其他 except 區塊 ...
        except Exception as e: app.logger.error(f"xAI Grok 未知錯誤: {e}", exc_info=True); final_response = "抱歉，系統發生錯誤。"

        # 6. 儲存對話歷史 (保持不變)
        history_to_save = history
        history_to_save.append({"role": "user", "content": user_text_original}) # 存原始輸入
        history_to_save.append({"role": "assistant", "content": final_response})
        if len(history_to_save) > MAX_HISTORY_TURNS * 2: history_to_save = history_to_save[-(MAX_HISTORY_TURNS * 2):]

        # 7. 存回資料庫 (保持 DB 日誌 和 NULL byte 修復嘗試)
        if conn:
            try:
                if conn.closed: conn = get_db_connection()
                with conn.cursor() as cur:
                    history_json_string = json.dumps(history_to_save, ensure_ascii=False)
                    # --- ▼▼▼ DB 儲存前的日誌 (保持) ▼▼▼ ---
                    app.logger.info(f"準備儲存歷史 for user_id: {user_id}")
                    log_string = history_json_string[:200] + "..." + history_json_string[-200:] if len(history_json_string) > 400 else history_json_string
                    app.logger.info(f"History JSON string (partial): {log_string}") #<-- 檢查這個日誌！
                    # --- ▲▲▲ 結束 DB 儲存前的日誌 ▲▲▲ ---
                    # --- ▼▼▼ 嘗試移除 NULL 字元修復 DB 錯誤 (保持) ▼▼▼ ---
                    cleaned_history_json_string = history_json_string.replace('\x00', '')
                    if history_json_string != cleaned_history_json_string: app.logger.warning("在儲存前移除了 JSON 字串中的 NULL bytes!")
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

        # 8. 推送回覆 (加入圖片輸出預留)
        app.logger.info(f"準備推送回應給 user {user_id}: {final_response[:50]}...")
        try:
            # --- ▼▼▼ 圖片輸出預留位置 ▼▼▼ ---
            # is_image_url = False # 假設需要一個標誌判斷是否為圖片URL
            # if is_image_url:
            #    from linebot.models import ImageSendMessage
            #    line_bot_api.push_message(user_id, ImageSendMessage(original_content_url=final_response, preview_image_url=final_response))
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

# --- LINE Webhook 與事件處理器 (保持不變) ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']; body = request.get_data(as_text=True)
    app.logger.info(f"收到請求: {body[:100]}")
    try: handler.handle(body, signature)
    except InvalidSignatureError: app.logger.error("簽名錯誤。"); abort(400)
    except LineBotApiError as e: app.logger.error(f"LINE API 錯誤: {e.status_code} {e.error.message}"); abort(500)
    except Exception as e: app.logger.error(f"Webhook 錯誤: {e}", exc_info=True); abort(500)
    return 'OK'

@handler.add(MessageEvent, message=(TextMessage, ImageMessage)) # 保持接收文字和圖片
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
