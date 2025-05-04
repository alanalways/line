import os
import time
import json
import psycopg2
import logging
import httpx
import hashlib
import requests
import datetime
import base64 # <--- 導入 base64 用於圖片編碼
from urllib.parse import quote_plus
from bs4 import BeautifulSoup
from PIL import Image # <--- 導入 Pillow (可選，用於檢查圖片等)
import io # <--- 導入 io 用於處理字節流

from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
# 加入 ImageMessage, ImageSendMessage
from linebot.models import (
    MessageEvent, TextMessage, TextSendMessage,
    ImageMessage, ImageSendMessage # <--- 加入圖片相關模型
)

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
# ... (省略 API Key 檢查與日誌記錄的程式碼) ...
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
SEARCH_KEYWORDS = ["天氣", "新聞", "股價", "今日", "今天", "最新", "誰是", "什麼是", "介紹", "查詢", "搜尋"]
IMAGE_GEN_TRIGGER = "畫一張："
VISION_MODEL = "grok-2-vision-1212" # 視覺模型 ID (根據你列表)
IMAGE_GEN_MODEL = "grok-2-image-1212" # 圖片生成模型 ID (根據你列表)
TEXT_MODEL = "grok-3-mini-beta" # 主要文字模型 ID

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

# --- 背景處理函數 (加入圖片處理框架、自動搜尋判斷、DB日誌修復嘗試) ---
def process_and_push(user_id, event):
    user_text_original = ""
    image_data_b64 = None # 用於儲存 Base64 圖片數據
    is_image_gen_request = False
    image_gen_prompt = ""
    final_response_message = None # 儲存最終要發送的 LINE Message 物件

    # --- 判斷訊息類型 ---
    if isinstance(event.message, TextMessage):
        user_text_original = event.message.text
        app.logger.info(f"收到文字訊息: '{user_text_original[:50]}...'")
        # 檢查是否為圖片生成指令
        if user_text_original.startswith(IMAGE_GEN_TRIGGER):
            is_image_gen_request = True
            image_gen_prompt = user_text_original[len(IMAGE_GEN_TRIGGER):].strip()
            app.logger.info(f"偵測到圖片生成指令: '{image_gen_prompt}'")

    elif isinstance(event.message, ImageMessage):
        message_id = event.message.id
        app.logger.info(f"收到圖片訊息 (ID: {message_id})。")
        try:
            message_content = line_bot_api.get_message_content(message_id)
            image_bytes = message_content.content # 直接讀取 bytes
            # 簡單驗證是否為圖片 (可選)
            # img = Image.open(io.BytesIO(image_bytes))
            # img.verify() # 檢查是否為有效圖片檔
            image_data_b64 = base64.b64encode(image_bytes).decode('utf-8')
            user_text_original = "[使用者傳送了一張圖片，請描述它]" # 固定提示或可讓使用者輸入搭配文字
            app.logger.info(f"成功下載圖片並轉換為 Base64，長度: {len(image_data_b64)}")
        except LineBotApiError as e: app.logger.error(f"下載圖片 {message_id} 失敗: {e}"); user_text_original = "[下載使用者圖片失敗]"
        except Exception as e: app.logger.error(f"處理圖片訊息 {message_id} 時出錯: {e}", exc_info=True); user_text_original = "[處理使用者圖片時出錯]"
    else:
        app.logger.info(f"忽略非文字/圖片訊息。"); return

    # --- 如果是圖片生成請求，直接處理 ---
    if is_image_gen_request:
        app.logger.info(f"開始處理圖片生成請求...")
        if ai_client is None: app.logger.error("AI Client 未初始化"); return
        try:
            # --- ▼▼▼ 呼叫圖片生成 API (假設與 OpenAI 類似) ▼▼▼ ---
            response = ai_client.images.generate(
                model=IMAGE_GEN_MODEL, # 使用指定的圖片生成模型
                prompt=image_gen_prompt,
                n=1, # 生成 1 張
                size="1024x1024" # 或模型支援的其他尺寸
            )
            # --- 假設 API 返回包含 URL 的結構 ---
            image_url = response.data[0].url
            if image_url:
                app.logger.info(f"圖片生成成功，URL: {image_url}")
                # --- 準備 ImageSendMessage ---
                final_response_message = ImageSendMessage(
                    original_content_url=image_url,
                    preview_image_url=image_url # 通常預覽和原始使用相同 URL
                )
            else:
                app.logger.error("圖片生成 API 未返回有效的 URL。")
                final_response_message = TextSendMessage(text="抱歉，圖片生成失敗了。")
            # --- ▲▲▲ 圖片生成處理結束 ▲▲▲ ---
        except AuthenticationError as e: app.logger.error(f"圖片生成認證錯誤: {e}"); final_response_message = TextSendMessage(text="圖片生成服務認證失敗。")
        except Exception as e: app.logger.error(f"圖片生成時發生錯誤: {e}", exc_info=True); final_response_message = TextSendMessage(text="抱歉，圖片生成時發生錯誤。")
        
        # 直接推送結果，不儲存歷史 (因為不是對話)
        try:
            if final_response_message: line_bot_api.push_message(user_id, messages=final_response_message)
            else: line_bot_api.push_message(user_id, TextSendMessage(text="[圖片生成處理完成，但無有效結果]"))
            app.logger.info(f"圖片生成結果推送完成。")
        except Exception as e: app.logger.error(f"推送圖片生成結果時出錯: {e}")
        return # 結束執行緒

    # --- 以下是處理文字或圖片描述請求的流程 ---
    start_process_time = time.time()
    conn = None; history = []; final_response = "抱歉，系統發生錯誤或無法連接 AI 服務。"

    if ai_client is None: app.logger.error(f"AI Client 未初始化 for user {user_id}"); return

    try:
        # 1. 獲取時間提示 + 固定繁中提示 (不變)
        now_utc = datetime.datetime.now(datetime.timezone.utc); now_taiwan = now_utc.astimezone(TAIWAN_TZ); current_time_str = now_taiwan.strftime("%Y年%m月%d日 %H:%M:%S")
        system_prompt = {"role": "system", "content": f"指令：請永遠使用『繁體中文』回答。目前時間是 {current_time_str} (台灣 UTC+8)，回答時間問題請以此為準。"}

        # 2. 讀取歷史紀錄 (不變)
        conn = get_db_connection()
        # ... (省略 DB 讀取程式碼) ...
        if conn:
             try:
                 with conn.cursor() as cur: cur.execute(...); result = cur.fetchone(); ...
             except Exception as db_err: app.logger.error(...); history = []; conn.rollback()
        else: app.logger.warning("無法連接資料庫，無歷史紀錄。")


        # 3. 自動搜尋判斷與執行 (不變)
        user_text_for_llm = user_text_original
        web_info_for_llm = None
        should_search_automatically = False
        if isinstance(event.message, TextMessage) and not user_text_original.startswith(IMAGE_GEN_TRIGGER): # 文字且非繪圖指令才判斷搜尋
            for keyword in SEARCH_KEYWORDS:
                if keyword in user_text_original: should_search_automatically = True; break
        
        if should_search_automatically:
            # ... (省略自動搜尋邏輯，同上一版本) ...
            query = user_text_original; search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"; search_result_summary = fetch_and_extract_text(search_url)
            if search_result_summary and not search_result_summary.startswith("獲取失敗") and not search_result_summary.startswith("處理"): web_info_for_llm = f"為了回答關於 '{query}' 的問題，進行了網路搜尋，摘要如下，請參考：\n```\n{search_result_summary}\n```\n\n"
            else: web_info_for_llm = f"嘗試自動搜尋 '{query}' 失敗了：{search_result_summary}。請告知使用者。"


        # 4. 組合提示訊息列表 (加入圖片處理)
        prompt_messages = [system_prompt] + history
        if web_info_for_llm: prompt_messages.append({"role": "system", "content": web_info_for_llm})

        # --- ▼▼▼ 處理圖片輸入的提示 ▼▼▼ ---
        if image_data_b64:
             # 建立 OpenAI Vision API 相容的格式
             vision_message_content = [
                 {"type": "text", "text": user_text_original} # 圖片附帶的文字提示
             ]
             # 檢查 Base64 字串長度，過長可能需要處理或告知錯誤
             max_base64_len = 1000000 # 示例限制，實際限制需查閱 API 文件
             if len(image_data_b64) < max_base64_len:
                  vision_message_content.append({
                      "type": "image_url",
                      "image_url": {"url": f"data:image/jpeg;base64,{image_data_b64}"} # 假設是 JPEG
                  })
                  prompt_messages.append({"role": "user", "content": vision_message_content})
                  app.logger.info("已將圖片 Base64 加入提示。")
             else:
                  app.logger.warning(f"圖片 Base64 過長 ({len(image_data_b64)} bytes)，無法加入提示。")
                  prompt_messages.append({"role": "user", "content": user_text_original + "\n\n[系統註：使用者傳送的圖片過大，無法處理。]"})
        else: # 如果沒有圖片，正常加入文字訊息
            prompt_messages.append({"role": "user", "content": user_text_for_llm})
        # --- ▲▲▲ 結束處理圖片輸入提示 ▲▲▲ ---

        # (歷史裁剪邏輯保持不變)
        if len(prompt_messages) > (MAX_HISTORY_TURNS * 2 + 3):
             # ... (省略裁剪程式碼) ...
              num_history_to_keep = MAX_HISTORY_TURNS * 2; head_prompt = prompt_messages[0]; tail_prompts = prompt_messages[-(1 + (1 if web_info_for_llm else 0)):] # 注意 web_info 可能不存在
              # 重新計算尾部需要保留的訊息數 (考慮圖片訊息結構)
              tail_count = 1 # 至少有最後的 user message
              if web_info_for_llm: tail_count += 1
              tail_prompts = prompt_messages[-tail_count:]
              middle_history = prompt_messages[1:-tail_count]; trimmed_middle_history = middle_history[-num_history_to_keep:]
              prompt_messages = [head_prompt] + trimmed_middle_history + tail_prompts
              app.logger.info(f"歷史記錄過長，已裁剪。裁剪後總長度: {len(prompt_messages)}")


        # 5. 呼叫 xAI Grok API (根據是否有圖片選擇模型)
        try:
            grok_start = time.time()
            # --- ▼▼▼ 根據是否有圖片選擇模型 ▼▼▼ ---
            if image_data_b64:
                target_model = VISION_MODEL
                app.logger.info(f"準備呼叫 xAI Grok Vision ({target_model})...")
            else:
                target_model = TEXT_MODEL
                app.logger.info(f"準備呼叫 xAI Grok ({target_model}) for user {user_id}...")
            # --- ▲▲▲ 結束模型選擇 ▲▲▲ ---

            chat_completion = ai_client.chat.completions.create(
                messages=prompt_messages,
                model=target_model,
                temperature=0.7,
                max_tokens=1500, # Vision 模型可能需要調整 max_tokens
            )
            final_response = chat_completion.choices[0].message.content.strip()
            app.logger.info(f"xAI Grok ({target_model}) 回應成功，用時 {time.time() - grok_start:.2f} 秒。")
        # (錯誤處理保持不變)
        except AuthenticationError as e: app.logger.error(f"xAI Grok API 認證錯誤: {e}", exc_info=False); final_response = "抱歉，AI 服務鑰匙錯誤。"
        # ... 其他 except 區塊 ...
        except Exception as e: app.logger.error(f"xAI Grok 未知錯誤: {e}", exc_info=True); final_response = "抱歉，系統發生錯誤。"

        # 6. 儲存對話歷史 (只存文字部分的原始輸入)
        history_to_save = history
        # 如果是圖片訊息，user_text_original 會是 "[使用者傳送...]"
        history_to_save.append({"role": "user", "content": user_text_original})
        history_to_save.append({"role": "assistant", "content": final_response})
        if len(history_to_save) > MAX_HISTORY_TURNS * 2: history_to_save = history_to_save[-(MAX_HISTORY_TURNS * 2):]

        # 7. 存回資料庫 (保持 DB 日誌 和 NULL byte 修復嘗試)
        if conn:
            try:
                if conn.closed: conn = get_db_connection()
                with conn.cursor() as cur:
                    history_json_string = json.dumps(history_to_save, ensure_ascii=False)
                    # --- Debug Log (保持) ---
                    app.logger.info(f"準備儲存歷史 for user_id: {user_id}")
                    log_string = history_json_string[:200] + "..." + history_json_string[-200:] if len(history_json_string) > 400 else history_json_string
                    app.logger.info(f"History JSON string (partial): {log_string}")
                    # --- NULL Byte Fix (保持) ---
                    cleaned_history_json_string = history_json_string.replace('\x00', '')
                    if history_json_string != cleaned_history_json_string: app.logger.warning("儲存前移除了 JSON 中的 NULL bytes!")
                    cur.execute("""INSERT INTO conversation_history ... ON CONFLICT ...""", (user_id, cleaned_history_json_string))
                    conn.commit()
                app.logger.info(f"歷史儲存成功 (長度: {len(history_to_save)})。")
            except (Exception, psycopg2.DatabaseError) as db_err: app.logger.error(f"儲存歷史錯誤: {type(db_err).__name__} - {db_err}", exc_info=True); conn.rollback()
        else: app.logger.warning("無法連接資料庫，歷史紀錄未儲存。")

        # 8. 推送回覆 (文字)
        final_response_message = TextSendMessage(text=final_response) # 預設回覆文字

    except Exception as e:
        app.logger.error(f"處理 user {user_id} 時錯誤: {type(e).__name__}: {e}", exc_info=True)
        final_response_message = TextSendMessage(text="抱歉，處理您的請求時發生了嚴重錯誤。") # 確保有預設的回覆訊息物件
    finally:
        if conn and not conn.closed: conn.close()
        # 推送訊息移到 finally 外面，在 try 區塊成功處理完畢後才推送

    # --- 推送最終回應 ---
    if final_response_message: # 確保有訊息可以推送
         app.logger.info(f"準備推送回應給 user {user_id}: ({type(final_response_message).__name__}) {str(final_response_message)[:100]}...")
         try:
             line_bot_api.push_message(user_id, messages=final_response_message)
             app.logger.info(f"訊息推送完成。")
         except LineBotApiError as e: app.logger.error(f"LINE API 錯誤: {e.status_code} {e.error.message}")
         except Exception as e: app.logger.error(f"推送訊息錯誤: {e}", exc_info=True)
    else:
         app.logger.error(f"沒有準備好任何回應訊息可以推送給 user {user_id}")


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
if __name__ == "__main__":
    init_db()
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
else:
     init_db()
