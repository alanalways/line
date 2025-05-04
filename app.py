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
from flask import Flask, request, abort
from linebot import LineBotApi, WebhookHandler
from linebot.exceptions import InvalidSignatureError, LineBotApiError
from linebot.models import MessageEvent, TextMessage, TextSendMessage
from dotenv import load_dotenv
from openai import OpenAI, RateLimitError, APIConnectionError, AuthenticationError, APITimeoutError, APIStatusError
from threading import Thread

# --- 載入環境變數 & 基本設定 (與之前相同) ---
load_dotenv()
app = Flask(__name__)
app.logger.setLevel(logging.INFO)

channel_access_token = os.getenv('LINE_CHANNEL_ACCESS_TOKEN')
channel_secret = os.getenv('LINE_CHANNEL_SECRET')
grok_api_key_from_env = os.getenv('GROK_API_KEY')
DATABASE_URL = os.getenv('DATABASE_URL')
XAI_API_BASE_URL = os.getenv("XAI_API_BASE_URL", "https://api.x.ai/v1")
TAIWAN_TZ = datetime.timezone(datetime.timedelta(hours=8))

# --- 驗證與記錄環境變數 (與之前相同) ---
grok_api_key = None
# ... (省略 API Key 檢查與日誌記錄的程式碼，保持不變) ...
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
if not all([channel_access_token, channel_secret, grok_api_key]): exit()
if not DATABASE_URL: app.logger.error("錯誤：DATABASE_URL 未設定！"); exit()

# --- Line Bot SDK 初始化 (與之前相同) ---
try:
    line_bot_api = LineBotApi(channel_access_token)
    handler = WebhookHandler(channel_secret)
    app.logger.info("Line Bot SDK v2 初始化成功。")
except Exception as e: app.logger.error(f"無法初始化 Line Bot SDK: {e}"); exit()

# --- 初始化 OpenAI Client (與之前相同，包含測試) ---
ai_client = None
try:
    app.logger.info(f"準備初始化 OpenAI client for xAI Grok，目標 URL: {XAI_API_BASE_URL}")
    ai_client = OpenAI(api_key=grok_api_key, base_url=XAI_API_BASE_URL)
    try:
        app.logger.info("嘗試使用 xAI API Key 獲取模型列表...")
        models = ai_client.models.list()
        app.logger.info(f">>> xAI API Key 測試 (模型列表) 成功，模型數: {len(models.data)}")
        if models.data: app.logger.info(f"    部分可用模型: {[m.id for m in models.data[:5]]}")
    except AuthenticationError: app.logger.error("!!! xAI API Key 測試 (模型列表) 失敗: 認證錯誤 (401)!", exc_info=False)
    except Exception as e: app.logger.error(f"!!! xAI API Key 測試 (模型列表) 發生其他錯誤: {type(e).__name__}: {e}", exc_info=True)
    app.logger.info("OpenAI client for xAI Grok 初始化流程完成。")
except Exception as e: app.logger.error(f"無法初始化 OpenAI client for xAI: {e}", exc_info=True)

# --- 其他設定與函數 (DB, 歷史長度) ---
MAX_HISTORY_TURNS = 5
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

# --- ▼▼▼ Tool: 網頁搜尋 (模擬 DuckDuckGo) ▼▼▼ ---
def perform_web_search(query: str) -> str:
    """
    執行網路搜尋 (使用 DuckDuckGo HTML 介面) 並返回結果摘要。
    注意：這是一個簡化的實現，實際效果依賴於抓取和 AI 的解析能力。
    """
    try:
        search_url = f"https://html.duckduckgo.com/html/?q={quote_plus(query)}"
        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36',
            'Accept-Language': 'en-US,en;q=0.9,zh-TW;q=0.8,zh;q=0.7',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,image/apng,*/*;q=0.8',
        }
        app.logger.info(f"執行網頁搜尋，目標 URL: {search_url}")
        response = requests.get(search_url, headers=headers, timeout=15)
        response.raise_for_status()
        content_type = response.headers.get('content-type', '').lower()
        if 'html' in content_type:
             # 這裡可以加入 BeautifulSoup 解析，但為簡單起見先返回部分原始碼
             content = response.text[:3000] # 限制長度
             app.logger.info(f"搜尋成功 ({query})，返回內容長度: {len(content)}")
             # 可以考慮做一些基本的清理，移除 <script>, <style> 等標籤
             return content
        else:
             app.logger.warning(f"搜尋 ({query}) 返回非 HTML 內容: {content_type}")
             return f"搜尋失敗：無法處理的內容類型 ({content_type})"
    except requests.exceptions.Timeout:
        app.logger.error(f"搜尋 ({query}) 超時")
        return "搜尋失敗：請求超時"
    except requests.exceptions.RequestException as e:
        app.logger.error(f"搜尋 ({query}) 失敗: {e}")
        return f"搜尋失敗：{str(e)}"
    except Exception as e:
        app.logger.error(f"執行搜尋 ({query}) 時發生未知錯誤: {e}", exc_info=True)
        return f"搜尋時發生內部錯誤：{str(e)}"

# --- 定義給 AI 使用的工具 (Function Calling/Tool Use) ---
tools = [
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "當你需要獲取即時資訊、最新事件、特定事實、或確認你不確定的資訊時，使用這個工具進行網路搜尋。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "要搜尋的關鍵字或問題，例如 '台灣今天天氣如何' 或 'Grok模型是什麼'",
                    }
                },
                "required": ["query"],
            },
        },
    }
]

# --- 背景處理函數 (實現自動判斷聯網) ---
def process_and_push(user_id, event):
    user_text_original = event.message.text
    app.logger.info(f"開始處理 user {user_id} 的訊息: '{user_text_original[:50]}...'")
    start_process_time = time.time()
    conn = None; history = []; final_response = "抱歉，我遇到了一些問題，暫時無法回覆。"

    if ai_client is None: app.logger.error(f"AI Client 未初始化 for user {user_id}"); return

    try:
        # 1. 獲取當前時間提示
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        now_taiwan = now_utc.astimezone(TAIWAN_TZ)
        current_time_str = now_taiwan.strftime("%Y年%m月%d日 %H:%M:%S")
        system_time_prompt = {"role": "system", "content": f"重要指令：現在是 {current_time_str} (台灣時間 UTC+8)。回答時間相關問題時，**必須**使用此時間。"}

        # 2. 讀取歷史紀錄
        conn = get_db_connection()
        if conn:
            try:
                # ... (省略 DB 讀取程式碼，與之前版本相同) ...
                 with conn.cursor() as cur: cur.execute(...); result = cur.fetchone(); ...
            except Exception as db_err: app.logger.error(...); history = []; conn.rollback()
        else: app.logger.warning("無法連接資料庫，無歷史紀錄。")

        # 3. 準備第一次呼叫 AI (判斷是否需要工具)
        messages_for_llm = [system_time_prompt] + history + [{"role": "user", "content": user_text_original}]
        # 清理過長的歷史 (可選，取決於模型上下文限制)
        # ... (省略歷史裁剪邏輯，保持簡單) ...

        app.logger.info(f"第一次呼叫 xAI Grok (判斷是否使用工具) for user {user_id}...")
        try:
            response = ai_client.chat.completions.create(
                model="grok-3-mini-beta",
                messages=messages_for_llm,
                tools=tools, # <-- 提供工具列表
                tool_choice="auto", # <-- 讓 AI 自行決定是否使用工具
                temperature=0.7,
                max_tokens=1500,
            )
            response_message = response.choices[0].message
            tool_calls = response_message.tool_calls # <-- 檢查 AI 是否要求呼叫工具

        except Exception as e: # 第一次呼叫就失敗
            app.logger.error(f"第一次呼叫 xAI Grok 時出錯: {e}", exc_info=True)
            # 可以設定一個錯誤訊息，或者讓 final_response 維持預設值
            raise e # 重新拋出錯誤，讓外層 try-except 捕捉

        # 4. 檢查 AI 是否需要使用工具 (Web Search)
        if tool_calls:
            app.logger.info(f"AI 請求使用工具: {tool_calls}")
            # 將 AI 的意圖 (呼叫工具) 也加入到 messages 裡，以便進行下一步
            messages_for_llm.append(response_message)

            available_functions = {"web_search": perform_web_search} # 將工具名稱映射到 Python 函數
            
            for tool_call in tool_calls:
                function_name = tool_call.function.name
                function_to_call = available_functions.get(function_name)
                if function_to_call:
                    function_args = json.loads(tool_call.function.arguments)
                    search_query = function_args.get("query")
                    app.logger.info(f"準備執行工具 '{function_name}'，查詢: '{search_query}'")
                    try:
                        # --- 執行實際的搜尋 ---
                        function_response = function_to_call(query=search_query)
                        app.logger.info(f"工具 '{function_name}' 執行完成。")
                        # --- 將工具執行結果加入 messages ---
                        messages_for_llm.append(
                            {
                                "tool_call_id": tool_call.id,
                                "role": "tool",
                                "name": function_name,
                                "content": function_response, # 搜尋結果
                            }
                        )
                    except Exception as tool_err:
                         app.logger.error(f"執行工具 '{function_name}' 時出錯: {tool_err}", exc_info=True)
                         # 可以選擇回傳錯誤訊息給 AI
                         messages_for_llm.append(
                            {
                                "tool_call_id": tool_call.id,
                                "role": "tool",
                                "name": function_name,
                                "content": f"執行搜尋工具時發生錯誤: {str(tool_err)}",
                            }
                        )
                else:
                     app.logger.warning(f"AI 請求呼叫未知工具: {function_name}")
            
            # --- 第二次呼叫 AI，傳入工具的執行結果 ---
            app.logger.info(f"第二次呼叫 xAI Grok (包含工具結果) for user {user_id}...")
            try:
                second_response = ai_client.chat.completions.create(
                    model="grok-3-mini-beta",
                    messages=messages_for_llm, # 包含工具結果的完整訊息列表
                    temperature=0.7,
                    max_tokens=1500,
                )
                final_response = second_response.choices[0].message.content.strip()
                app.logger.info("第二次呼叫 xAI Grok 成功。")
            except Exception as e:
                 app.logger.error(f"第二次呼叫 xAI Grok 時出錯: {e}", exc_info=True)
                 final_response = "抱歉，在處理搜尋結果後發生錯誤。" # 提供一個特定的錯誤訊息

        else:
            # AI 認為不需要工具，直接回答
            final_response = response_message.content.strip()
            app.logger.info("AI 直接回答，未使用工具。")

        # --- 5. 儲存對話歷史 (只存 user 和最終 assistant 回應) ---
        history_to_save = history # 原始歷史
        history_to_save.append({"role": "user", "content": user_text_original})
        history_to_save.append({"role": "assistant", "content": final_response})
        if len(history_to_save) > MAX_HISTORY_TURNS * 2: history_to_save = history_to_save[-(MAX_HISTORY_TURNS * 2):]

        if conn:
            try:
                # ... (省略 DB 儲存程式碼，與之前版本相同) ...
                 if conn.closed: conn = get_db_connection()
                 with conn.cursor() as cur:
                     history_json_string = json.dumps(history_to_save, ensure_ascii=False)
                     cur.execute("""INSERT INTO conversation_history ... VALUES (%s, %s) ON CONFLICT ...""", (user_id, history_json_string))
                     conn.commit()
                 app.logger.info(f"歷史儲存成功 (長度: {len(history_to_save)})。")
            except Exception as db_err: app.logger.error(f"儲存歷史錯誤: {db_err}", exc_info=True); conn.rollback()
        else: app.logger.warning("無法連接資料庫，歷史紀錄未儲存。")

        # --- 6. 推送最終回應 ---
        app.logger.info(f"準備推送最終回應給 user {user_id}: {final_response[:50]}...")
        try:
            line_bot_api.push_message(user_id, TextSendMessage(text=final_response))
            app.logger.info(f"訊息推送完成。")
        except LineBotApiError as e: app.logger.error(f"LINE API 錯誤: {e.status_code} {e.error.message}")
        except Exception as e: app.logger.error(f"推送訊息錯誤: {e}", exc_info=True)

    except Exception as e:
        app.logger.error(f"處理 user {user_id} 時發生嚴重錯誤: {type(e).__name__}: {e}", exc_info=True)
        # 嘗試推送一個通用錯誤訊息
        try:
            line_bot_api.push_message(user_id, TextSendMessage(text="抱歉，處理您的請求時發生了嚴重錯誤。"))
        except Exception as push_err:
             app.logger.error(f"推送嚴重錯誤訊息失敗: {push_err}")
    finally:
        if conn and not conn.closed: conn.close()
        app.logger.info(f"任務完成，用時 {time.time() - start_process_time:.2f} 秒。")


# --- LINE Webhook 與事件處理器 (保持不變) ---
@app.route("/callback", methods=['POST'])
def callback():
    signature = request.headers['X-Line-Signature']
    body = request.get_data(as_text=True)
    app.logger.info(f"收到請求: {body[:100]}")
    try: handler.handle(body, signature)
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
