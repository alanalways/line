import os
import time
import json
import psycopg2
import logging
import httpx
import hashlib
import requests
import datetime
import base64
import io
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
# from PIL import Image # 暫時不用

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    ImageMessage, ImageSendMessage
)

from dotenv import load_dotenv
from openai import OpenAI, RateLimitError, APIConnectionError, AuthenticationError, APITimeoutError, APIStatusError
from threading import Thread

# --- 載入環境變數 & 基本設定 ---
load_dotenv()
app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# --- 環境變數讀取與驗證 ---
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
    if grok_api_key_from_env != grok_api_key_from_env.strip():
         app.logger.warning("偵測到 xAI API Key 前後有空格，已自動移除。")
         grok_api_key = grok_api_key_from_env.strip()
    else:
         grok_api_key = grok_api_key_from_env
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
    ai_client = OpenAI(
        api_key=grok_api_key,
        base_url=XAI_API_BASE_URL,
        timeout=httpx.Timeout(60.0, connect=10.0)
    )
    # (省略 API Key 測試程式碼)
    app.logger.info("OpenAI client for xAI Grok 初始化流程完成。")
except Exception as e: app.logger.error(f"無法初始化 OpenAI client for xAI: {e}", exc_info=True)

# --- 其他設定與函數 ---
MAX_HISTORY_TURNS = 5
SEARCH_KEYWORDS = ["天氣", "新聞", "股價", "今日", "今天", "最新", "誰是", "什麼是", "介紹", "查詢", "搜尋"]
IMAGE_GEN_TRIGGER_HALF = "畫一張:" # 半形冒號
IMAGE_GEN_TRIGGER_FULL = "畫一張：" # 全形冒號
TEXT_MODEL = "grok-3-mini-beta"
VISION_MODEL = "grok-vision-beta"
IMAGE_GEN_MODEL = "grok-2-image-1212"

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
    finally:
        if conn and not conn.closed: conn.close()


# (fetch_and_extract_text 函數保持不變)
def fetch_and_extract_text(url):
    try:
        headers = {'User-Agent': 'Mozilla/5.0 ...'}
        response = requests.get(url, headers=headers, timeout=15); response.raise_for_status()
        content_type = response.headers.get('content-type', '').lower()
        if 'html' in content_type:
             soup = BeautifulSoup(response.content, 'html.parser'); # ... (省略解析) ...
             text = '\n'.join(filter(None, (line.strip() for line in soup.stripped_strings)))
             summary = text[:2500]; return summary if summary else "無法提取有效摘要。"
        elif 'text' in content_type: return response.text[:3000]
        else: return f"獲取失敗：無法處理的內容類型 ({content_type})"
    except Exception as e: return f"處理獲取時發生錯誤：{str(e)}"


# --- 背景處理函數 (修正圖片生成呼叫) ---
def process_and_push(user_id, event):
    user_text_original = ""
    image_data_b64 = None
    is_image_gen_request = False
    image_gen_prompt = ""
    final_response = "抱歉，系統發生錯誤或無法連接 AI 服務。"
    final_response_message = None

    # 判斷訊息類型
    if isinstance(event.message, TextMessage):
        user_text_original = event.message.text; app.logger.info(f"收到文字訊息 (原始): {repr(user_text_original)}")
        cleaned_text = user_text_original.strip()
        app.logger.info(f"DEBUG: 清理後文字: {repr(cleaned_text)}")
        starts_with_half = cleaned_text.startswith(IMAGE_GEN_TRIGGER_HALF); app.logger.info(f"DEBUG: cleaned_text.startswith('{IMAGE_GEN_TRIGGER_HALF}') -> {starts_with_half}")
        if starts_with_half: is_image_gen_request = True; image_gen_prompt = cleaned_text[len(IMAGE_GEN_TRIGGER_HALF):].strip(); app.logger.info(f"偵測到圖片生成指令 (半形冒號): '{image_gen_prompt}'")
        if not is_image_gen_request:
            starts_with_full = cleaned_text.startswith(IMAGE_GEN_TRIGGER_FULL); app.logger.info(f"DEBUG: cleaned_text.startswith('{IMAGE_GEN_TRIGGER_FULL}') -> {starts_with_full}")
            if starts_with_full: is_image_gen_request = True; image_gen_prompt = cleaned_text[len(IMAGE_GEN_TRIGGER_FULL):].strip(); app.logger.info(f"偵測到圖片生成指令 (全形冒號): '{image_gen_prompt}'")
        app.logger.info(f"DEBUG: 觸發詞檢查完成後, is_image_gen_request = {is_image_gen_request}")
    elif isinstance(event.message, ImageMessage):
        message_id = event.message.id; app.logger.info(f"收到圖片訊息 (ID: {message_id})，嘗試下載...")
        try: message_content = line_bot_api.get_message_content(message_id); image_bytes = message_content.content; image_data_b64 = base64.b64encode(image_bytes).decode('utf-8'); user_text_original = "[圖片描述請求]"; app.logger.info(f"成功下載圖片並轉換為 Base64")
        except Exception as e: app.logger.error(f"處理圖片訊息 {message_id} 時出錯: {e}", exc_info=True); user_text_original = "[處理使用者圖片時出錯]"
    else: app.logger.info(f"忽略非文字/圖片訊息。"); return

    # --- 處理圖片生成 ---
    if is_image_gen_request:
        app.logger.info(f"開始處理圖片生成請求...")
        if ai_client is None: final_response_message = TextSendMessage(text="[AI Client 初始化失敗]")
        else:
            try:
                app.logger.info(f"嘗試呼叫 '{IMAGE_GEN_MODEL}' 生成圖片，提示: '{image_gen_prompt}'")
                # --- ▼▼▼ 移除 size 參數 ▼▼▼ ---
                response = ai_client.images.generate(
                    model=IMAGE_GEN_MODEL,
                    prompt=image_gen_prompt,
                    n=1
                    # size="1024x1024" # <-- 移除這一行
                )
                # --- ▲▲▲ 結束移除 ▲▲▲ ---
                image_url = response.data[0].url # 假設返回 URL
                if image_url: final_response_message = ImageSendMessage(original_content_url=image_url, preview_image_url=image_url)
                else: app.logger.error("圖片生成 API 未返回有效 URL。"); final_response_message = TextSendMessage(text="抱歉，圖片生成失敗 (未收到URL)。")
            except AuthenticationError as e: app.logger.error(f"圖片生成認證錯誤: {e}"); final_response_message = TextSendMessage(text="圖片生成服務認證失敗。")
            except Exception as e: app.logger.error(f"圖片生成時發生錯誤: {e}", exc_info=True); final_response_message = TextSendMessage(text=f"抱歉，圖片生成時發生錯誤: {type(e).__name__}") # 返回更詳細的錯誤類型
        try:
            if final_response_message: line_bot_api.push_message(user_id, messages=final_response_message); app.logger.info(f"圖片生成結果推送完成。")
            else: line_bot_api.push_message(user_id, TextSendMessage(text="[圖片生成處理完成，但無有效結果]"))
        except Exception as e: app.logger.error(f"推送圖片生成結果時出錯: {e}")
        app.logger.info(f"圖片生成請求處理完畢，結束任務。")
        return # <--- 確保有 return

    # --- 處理文字或圖片描述 ---
    start_process_time = time.time(); conn = None; history = []
    if ai_client is None: app.logger.error(f"AI Client 未初始化"); return

    try:
        # 1. 時間提示 + 讀取歷史
        now_utc = datetime.datetime.now(datetime.timezone.utc); now_taiwan = now_utc.astimezone(TAIWAN_TZ); current_time_str = now_taiwan.strftime("%Y年%m月%d日 %H:%M:%S")
        system_prompt = {"role": "system", "content": f"指令：請永遠使用『繁體中文』回答。目前時間是 {current_time_str} (台灣 UTC+8)，回答時間問題請以此為準。"}
        conn = get_db_connection()
        if conn:
             try: # 修正後的讀取邏輯
                 with conn.cursor() as cur:
                    cur.execute("SELECT history FROM conversation_history WHERE user_id = %s;", (user_id,))
                    result = cur.fetchone()
                    if result and result[0]:
                        db_data = result[0]
                        if isinstance(db_data, list): history = db_data
                        elif isinstance(db_data, str): history = json.loads(db_data)
                        else: history = []
                        app.logger.info(f"成功載入歷史，長度: {len(history)}")
                    else: app.logger.info(f"無歷史紀錄。")
             except Exception as db_err: app.logger.error(f"讀取歷史錯誤: {db_err}", exc_info=True); history = []; conn.rollback()
        else: app.logger.warning("無法連接資料庫，無歷史紀錄。")

        # 2. 自動搜尋判斷
        user_text_for_llm = user_text_original; web_info_for_llm = None; should_search_automatically = False
        if isinstance(event.message, TextMessage) and not is_image_gen_request:
             for keyword in SEARCH_KEYWORDS:
                 if keyword in user_text_original: should_search_automatically = True; break
        if should_search_automatically:
             query = user_text_original; search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"; search_result_summary = fetch_and_extract_text(search_url)
             if search_result_summary and not search_result_summary.startswith("獲取失敗"): web_info_for_llm = f"為了回答 '{query}'，進行了網路搜尋，摘要如下:\n```\n{search_result_summary}\n```\n\n"
             else: web_info_for_llm = f"嘗試自動搜尋 '{query}' 失敗:{search_result_summary}。"

        # 3. 組合提示
        prompt_messages = [system_prompt] + history
        if web_info_for_llm: prompt_messages.append({"role": "system", "content": web_info_for_llm})
        current_user_message_content = []
        if image_data_b64: # 加入圖片提示
             current_user_message_content.append({"type": "text", "text": "請描述這張圖片的內容。"})
             if len(image_data_b64) < 1500000: current_user_message_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data_b64}"}})
             else: current_user_message_content = [{"type": "text", "text": "[圖片過大無法處理]"}]
        else: current_user_message_content.append({"type": "text", "text": user_text_for_llm})
        prompt_messages.append({"role": "user", "content": current_user_message_content})
        # (省略歷史裁剪)

        # 4. 呼叫 AI API
        try:
            grok_start = time.time()
            target_model = VISION_MODEL if image_data_b64 else TEXT_MODEL
            app.logger.info(f"準備呼叫 xAI Grok ({target_model})...")
            chat_completion = ai_client.chat.completions.create(messages=prompt_messages, model=target_model, temperature=0.7, max_tokens=1500)
            final_response = chat_completion.choices[0].message.content.strip()
            app.logger.info(f"xAI Grok ({target_model}) 回應成功，用時 {time.time() - grok_start:.2f} 秒。")
        except Exception as e: app.logger.error(f"xAI Grok 未知錯誤: {e}", exc_info=True); final_response = "抱歉，系統發生錯誤。"

        # 5. 準備儲存歷史
        history_to_save = history
        history_to_save.append({"role": "user", "content": user_text_original})
        history_to_save.append({"role": "assistant", "content": final_response})
        if len(history_to_save) > MAX_HISTORY_TURNS * 2: history_to_save = history_to_save[-(MAX_HISTORY_TURNS * 2):]

        # 6. 存回資料庫 (保持日誌和 NULL fix)
        if conn:
            try:
                if conn.closed: conn = get_db_connection()
                with conn.cursor() as cur:
                    history_json_string = json.dumps(history_to_save, ensure_ascii=False)
                    cleaned_history_json_string = history_json_string.replace('\x00', '')
                    app.logger.info(f"準備儲存歷史 for user_id: {user_id}")
                    log_string = cleaned_history_json_string[:200] + "..." + cleaned_history_json_string[-200:] if len(cleaned_history_json_string) > 400 else cleaned_history_json_string
                    app.logger.info(f"History JSON string (cleaned, partial): {log_string}")
                    cur.execute("""INSERT INTO conversation_history (user_id, history) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET history = EXCLUDED.history;""", (user_id, cleaned_history_json_string))
                    conn.commit()
                app.logger.info(f"歷史儲存成功 (長度: {len(history_to_save)})。")
            except (Exception, psycopg2.DatabaseError) as db_err: app.logger.error(f"儲存歷史錯誤 for user {user_id}: {type(db_err).__name__} - {db_err}", exc_info=True); conn.rollback()
        else: app.logger.warning("無法連接資料庫，歷史紀錄未儲存。")

        # 7. 準備最終回覆訊息 (只有文字)
        final_response_message = TextSendMessage(text=final_response)

    except Exception as e:
        app.logger.error(f"處理 user {user_id} 時錯誤: {type(e).__name__}: {e}", exc_info=True)
        final_response_message = TextSendMessage(text="抱歉，處理您的請求時發生了嚴重錯誤。")
    finally:
        if conn and not conn.closed: conn.close()

    # 推送最終回應
    if final_response_message:
         app.logger.info(f"準備推送回應給 user {user_id}...")
         try: line_bot_api.push_message(user_id, messages=final_response_message); app.logger.info(f"訊息推送完成。")
         except Exception as e: app.logger.error(f"推送訊息錯誤: {e}", exc_info=True)
    else: app.logger.error(f"沒有準備好任何回應訊息可以推送給 user {user_id}")

    app.logger.info(f"任務完成，用時 {time.time() - start_process_time:.2f} 秒。")

# --- LINE Webhook 與事件處理器 (保持不變) ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']; body = request.get_data(as_text=True)
    app.logger.info(f"收到請求: {body[:100]}")
    try: handler.handle(body, signature)
    except InvalidSignatureError: app.logger.error("簽名錯誤。"); abort(400)
    except LineBotApiError as e: app.logger.error(f"LINE API 錯誤: {e.status_code} {e.error.message}"); abort(500)
