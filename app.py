import os
import time
import json
import psycopg2
import logging
import httpx # 保留，openai SDK 可能會用到
import hashlib
import requests
import datetime
import base64
import io
from urllib.parse import quote_plus
from bs4 import BeautifulSoup # 用於解析 HTML

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
# 加入 ImageMessage, ImageSendMessage
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    ImageMessage, ImageSendMessage
)

from dotenv import load_dotenv
from openai import OpenAI, RateLimitError, APIConnectionError, AuthenticationError, APITimeoutError, APIStatusError # 使用 OpenAI SDK
from threading import Thread

# --- 載入環境變數 & 基本設定 ---
load_dotenv()
app = Flask(__name__)
app.logger.setLevel(logging.INFO)

# --- 環境變數讀取與驗證 ---
channel_access_token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
channel_secret = os.getenv('LINE_CHANNEL_SECRET')
grok_api_key_from_env = os.getenv('GROK_API_KEY') # 這裡仍用 GROK_API_KEY 這個名字讀取 xAI 的 Key
DATABASE_URL = os.getenv('DATABASE_URL')
XAI_API_BASE_URL = os.getenv("XAI_API_BASE_URL", "https://api.x.ai/v1")
TAIWAN_TZ = datetime.timezone(datetime.timedelta(hours=8))

# 檢查 API Key
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
    app.logger.error("錯誤：環境變數 GROK_API_KEY 為空！"); exit()

# 檢查其他必要變數
if not all([channel_access_token, channel_secret, grok_api_key]):
    app.logger.error("錯誤：LINE Token 或 Grok/xAI API Key 未完整設定！"); exit()
if not DATABASE_URL:
    app.logger.error("錯誤：DATABASE_URL 未設定！請在 Render 連接資料庫。"); exit()

# --- Line Bot SDK 初始化 (v2) ---
try:
    line_bot_api = LineBotApi(channel_access_token)
    handler = WebhookHandler(channel_secret)
    app.logger.info("Line Bot SDK v2 初始化成功。")
except Exception as e:
    app.logger.error(f"無法初始化 Line Bot SDK: {e}"); exit()

# --- 初始化 OpenAI Client (for xAI) ---
ai_client = None
try:
    app.logger.info(f"準備初始化 OpenAI client for xAI Grok，目標 URL: {XAI_API_BASE_URL}")
    # 讓 OpenAI SDK 自動處理 http client
    ai_client = OpenAI(
        api_key=grok_api_key,
        base_url=XAI_API_BASE_URL,
        timeout=httpx.Timeout(60.0, connect=10.0) # 在這裡設定超時
    )
    # 簡單測試 API Key (可選，但建議保留)
    try:
        app.logger.info("嘗試使用 xAI API Key 獲取模型列表...")
        models = ai_client.models.list()
        app.logger.info(f">>> xAI API Key 測試 (模型列表) 成功，模型數: {len(models.data)}")
        if models.data:
            app.logger.info(f"    部分可用模型: {[m.id for m in models.data[:5]]}")
    except AuthenticationError as e:
        app.logger.error(f"!!! xAI API Key 測試 (模型列表) 失敗: 認證錯誤 (401)! 請確認 API Key 對 {XAI_API_BASE_URL} 有效。", exc_info=False)
    except Exception as e:
        app.logger.error(f"!!! xAI API Key 測試 (模型列表) 發生其他錯誤: {type(e).__name__}: {e}", exc_info=True)
    app.logger.info("OpenAI client for xAI Grok 初始化流程完成。")
except Exception as e:
    app.logger.error(f"無法初始化 OpenAI client for xAI: {e}", exc_info=True)
    # 初始化失敗，後續會在 process_and_push 中檢查 ai_client 是否為 None

# --- 其他設定與函數 ---
MAX_HISTORY_TURNS = 5
SEARCH_KEYWORDS = ["天氣", "新聞", "股價", "今日", "今天", "最新", "誰是", "什麼是", "介紹", "查詢", "搜尋"]
IMAGE_GEN_TRIGGER = "畫一張："
TEXT_MODEL = "grok-3-mini-beta"
VISION_MODEL = "grok-vision-beta" # 請確認這是你可用的視覺模型 ID
IMAGE_GEN_MODEL = "grok-2-image-1212" # 請確認這是你可用的圖片生成模型 ID

# --- 資料庫輔助函數 ---
def get_db_connection():
    try:
        conn = psycopg2.connect(DATABASE_URL)
        conn.set_client_encoding('UTF8')
        return conn
    except Exception as e:
        app.logger.error(f"資料庫連接失敗: {e}")
        return None

# --- ▼▼▼ 修正了縮排的 init_db 函數 ▼▼▼ ---
def init_db():
    """檢查並建立 conversation_history 資料表"""
    sql = "CREATE TABLE IF NOT EXISTS conversation_history (user_id TEXT PRIMARY KEY, history JSONB);"
    conn = get_db_connection()
    if not conn:
        app.logger.error("無法初始化資料庫 (無連接)。")
        return
    try:
        # with 語句需要自己一行並縮排
        with conn.cursor() as cur:
            cur.execute(sql)
            conn.commit()
        app.logger.info("資料庫資料表 'conversation_history' 初始化完成。")
    except Exception as e: # except 和 try 對齊
        app.logger.error(f"無法初始化資料庫資料表: {e}")
        if conn and not conn.closed:
             try:
                 conn.rollback()
             except Exception as rb_err:
                 app.logger.error(f"Rollback 失敗: {rb_err}")
    finally: # finally 和 try 對齊
        if conn and not conn.closed:
             conn.close()
# --- ▲▲▲ 結束修正 init_db ▲▲▲ ---


# --- 網頁內容/搜尋結果獲取函數 ---
def fetch_and_extract_text(url):
    """從指定 URL 獲取內容並使用 BeautifulSoup 提取文字摘要"""
    try:
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9,zh-TW;q=0.8,zh;q=0.7',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
        }
        app.logger.info(f"開始獲取 URL 內容: {url}")
        response = requests.get(url, headers=headers, timeout=15) # 15秒超時
        response.raise_for_status() # 檢查 HTTP 錯誤狀態碼
        content_type = response.headers.get('content-type', '').lower()

        if 'html' in content_type:
             soup = BeautifulSoup(response.content, 'html.parser') # 使用 response.content 避免編碼問題
             for element in soup(["script", "style", "header", "footer", "nav", "aside", "form", "button", "input"]): # 移除更多不相關標籤
                 element.decompose()
             text = '\n'.join(filter(None, (line.strip() for line in soup.stripped_strings)))
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

    except requests.exceptions.Timeout:
        app.logger.error(f"獲取 URL 超時: {url}")
        return "獲取失敗：請求超時"
    except requests.exceptions.RequestException as e:
        app.logger.error(f"獲取 URL 內容失敗: {url}, Error: {e}")
        return f"獲取失敗：{str(e)}"
    except Exception as e:
        app.logger.error(f"處理 URL 獲取時未知錯誤: {url}, Error: {e}", exc_info=True)
        return f"處理 URL 獲取時發生錯誤：{str(e)}"


# --- 背景處理函數 (整合所有功能) ---
def process_and_push(user_id, event):
    user_text_original = ""
    image_data_b64 = None
    is_image_gen_request = False
    image_gen_prompt = ""
    final_response = "抱歉，系統發生錯誤或無法連接 AI 服務。"
    final_response_message = None

    # 判斷訊息類型
    if isinstance(event.message, TextMessage):
        user_text_original = event.message.text; app.logger.info(f"收到文字訊息: '{user_text_original[:50]}...'")
        if user_text_original.startswith(IMAGE_GEN_TRIGGER): is_image_gen_request = True; image_gen_prompt = user_text_original[len(IMAGE_GEN_TRIGGER):].strip(); app.logger.info(f"偵測到圖片生成指令: '{image_gen_prompt}'")
    elif isinstance(event.message, ImageMessage):
        message_id = event.message.id; app.logger.info(f"收到圖片訊息 (ID: {message_id})，嘗試下載...")
        try: message_content = line_bot_api.get_message_content(message_id); image_bytes = message_content.content; image_data_b64 = base64.b64encode(image_bytes).decode('utf-8'); user_text_original = "[圖片描述請求]"; app.logger.info(f"成功下載圖片並轉為 Base64")
        except Exception as e: app.logger.error(f"處理圖片訊息 {message_id} 時出錯: {e}", exc_info=True); user_text_original = "[處理使用者圖片時出錯]"
    else: app.logger.info(f"忽略非文字/圖片訊息。"); return

    # --- 處理圖片生成 ---
    if is_image_gen_request:
        app.logger.info(f"開始處理圖片生成請求...")
        if ai_client is None: final_response_message = TextSendMessage(text="[AI Client 初始化失敗]")
        else:
            try:
                app.logger.info(f"嘗試呼叫 '{IMAGE_GEN_MODEL}' 生成圖片...")
                response = ai_client.images.generate(model=IMAGE_GEN_MODEL, prompt=image_gen_prompt, n=1, size="1024x1024") # 假設參數與 OpenAI 相同
                image_url = response.data[0].url
                if image_url: final_response_message = ImageSendMessage(original_content_url=image_url, preview_image_url=image_url)
                else: app.logger.error("圖片生成未返回 URL"); final_response_message = TextSendMessage(text="抱歉，圖片生成失敗。")
            except Exception as e: app.logger.error(f"圖片生成時發生錯誤: {e}", exc_info=True); final_response_message = TextSendMessage(text="抱歉，圖片生成時發生錯誤。")
        try:
            if final_response_message: line_bot_api.push_message(user_id, messages=final_response_message); app.logger.info(f"圖片生成結果推送完成。")
            else: line_bot_api.push_message(user_id, TextSendMessage(text="[圖片生成處理完成，但無有效結果]"))
        except Exception as e: app.logger.error(f"推送圖片生成結果時出錯: {e}")
        return # 圖片生成請求結束

    # --- 處理文字或圖片描述 ---
    start_process_time = time.time(); conn = None; history = []
    if ai_client is None: app.logger.error(f"AI Client 未初始化"); return

    try:
        # 1. 時間提示
        now_utc = datetime.datetime.now(datetime.timezone.utc); now_taiwan = now_utc.astimezone(TAIWAN_TZ); current_time_str = now_taiwan.strftime("%Y年%m月%d日 %H:%M:%S")
        system_prompt = {"role": "system", "content": f"指令：請永遠使用『繁體中文』回答。目前時間是 {current_time_str} (台灣 UTC+8)，回答時間問題請以此為準。"}

        # 2. 讀取歷史
        conn = get_db_connection()
        if conn:
            try: # 使用上次修正的讀取邏輯
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

        # 3. 自動搜尋判斷
        user_text_for_llm = user_text_original; web_info_for_llm = None; should_search_automatically = False
        if isinstance(event.message, TextMessage) and not is_image_gen_request:
             for keyword in SEARCH_KEYWORDS:
                 if keyword in user_text_original: should_search_automatically = True; break
        if should_search_automatically:
             query = user_text_original; search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"; search_result_summary = fetch_and_extract_text(search_url)
             if search_result_summary and not search_result_summary.startswith("獲取失敗"): web_info_for_llm = f"為了回答 '{query}'，進行了網路搜尋，摘要如下:\n```\n{search_result_summary}\n```\n\n"
             else: web_info_for_llm = f"嘗試自動搜尋 '{query}' 失敗:{search_result_summary}。"

        # 4. 組合提示
        prompt_messages = [system_prompt] + history
        if web_info_for_llm: prompt_messages.append({"role": "system", "content": web_info_for_llm})
        current_user_message_content = []
        if image_data_b64: # 加入圖片提示
             current_user_message_content.append({"type": "text", "text": "請描述這張圖片的內容。"})
             if len(image_data_b64) < 1500000: current_user_message_content.append({"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{image_data_b64}"}})
             else: current_user_message_content = [{"type": "text", "text": "[圖片過大無法處理]"}]
        else: current_user_message_content.append({"type": "text", "text": user_text_for_llm})
        prompt_messages.append({"role": "user", "content": current_user_message_content})
        # (歷史裁剪)
        if len(prompt_messages) > (MAX_HISTORY_TURNS * 2 + 3):
            num_history_to_keep = MAX_HISTORY_TURNS * 2; head_prompt = prompt_messages[0]; tail_prompts = prompt_messages[-1:]; middle_history = prompt_messages[1:-1]; trimmed_middle_history = middle_history[-num_history_to_keep:]
            prompt_messages = [head_prompt] + trimmed_middle_history + tail_prompts

        # 5. 呼叫 AI API
        try:
            grok_start = time.time()
            target_model = VISION_MODEL if image_data_b64 else TEXT_MODEL
            app.logger.info(f"準備呼叫 xAI Grok ({target_model})...")
            chat_completion = ai_client.chat.completions.create(messages=prompt_messages, model=target_model, temperature=0.7, max_tokens=1500)
            final_response = chat_completion.choices[0].message.content.strip()
            app.logger.info(f"xAI Grok ({target_model}) 回應成功，用時 {time.time() - grok_start:.2f} 秒。")
        # (錯誤處理)
        except Exception as e: app.logger.error(f"xAI Grok 未知錯誤: {e}", exc_info=True); final_response = "抱歉，系統發生錯誤。"

        # 6. 準備儲存歷史
        history_to_save = history
        history_to_save.append({"role": "user", "content": user_text_original}) # 存原始文字或圖片標識
        history_to_save.append({"role": "assistant", "content": final_response})
        if len(history_to_save) > MAX_HISTORY_TURNS * 2: history_to_save = history_to_save[-(MAX_HISTORY_TURNS * 2):]

        # 7. 存回資料庫 (含日誌和 NULL fix)
        if conn:
            try:
                if conn.closed: conn = get_db_connection()
                with conn.cursor() as cur:
                    history_json_string = json.dumps(history_to_save, ensure_ascii=False)
                    cleaned_history_json_string = history_json_string.replace('\x00', '')
                    app.logger.info(f"準備儲存歷史 for user_id: {user_id}")
                    log_string = cleaned_history_json_string[:200] + "..." + cleaned_history_json_string[-200:] if len(cleaned_history_json_string) > 400 else cleaned_history_json_string
                    app.logger.info(f"History JSON string (cleaned, partial): {log_string}") #<-- 檢查這個日誌！
                    cur.execute("""INSERT INTO conversation_history (user_id, history) VALUES (%s, %s) ON CONFLICT (user_id) DO UPDATE SET history = EXCLUDED.history;""", (user_id, cleaned_history_json_string))
                    conn.commit()
                app.logger.info(f"歷史儲存成功 (長度: {len(history_to_save)})。")
            except (Exception, psycopg2.DatabaseError) as db_err: app.logger.error(f"儲存歷史錯誤 for user {user_id}: {type(db_err).__name__} - {db_err}", exc_info=True); conn.rollback()
        else: app.logger.warning("無法連接資料庫，歷史紀錄未儲存。")

        # 8. 準備最終回覆訊息 (目前只有文字)
        final_response_message = TextSendMessage(text=final_response)

    except Exception as e:
        app.logger.error(f"處理 user {user_id} 時錯誤: {type(e).__name__}: {e}", exc_info=True)
        final_response_message = TextSendMessage(text="抱歉，處理您的請求時發生了嚴重錯誤。")
    finally:
        if conn and not conn.closed: conn.close()

    # 推送最終回應
    if final_response_message:
         app.logger.info(f"準備推送回應給 user {user_id}...")
         try:
             line_bot_api.push_message(user_id, messages=final_response_message)
             app.logger.info(f"訊息推送完成。")
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
    except Exception as e: app.logger.error(f"Webhook 錯誤: {e}", exc_info=True); abort(500)
    return 'OK'

@handler.add(MessageEvent, message=(TextMessage, ImageMessage))
def handle_message(event):
    user_id = event.source.user_id
    Thread(target=process_and_push, args=(user_id, event)).start()

# --- 主程式進入點 (保持不變) ---
if __name__ == "__main__": init_db(); port = int(os.environ.get("PORT", 5000)); app.run(host="0.0.0.0", port=port)
else: init_db()
