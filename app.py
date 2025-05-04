# --- 極簡測試版 app.py ---
import os
import logging
from flask import Flask

# 基本日誌設定
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

logger.info("--- 極簡測試開始 ---")

problem_solved = False
try:
    # 嘗試標準導入
    from linebot.v3.webhooks import WebhookParser
    logger.info(">>> 標準導入 from linebot.v3.webhooks import WebhookParser 成功!")
    channel_secret = os.getenv('LINE_CHANNEL_SECRET', 'dummy_secret_for_test')
    parser = WebhookParser(channel_secret)
    logger.info(">>> WebhookParser 實例化成功!")
    problem_solved = True
except ImportError as e1:
    logger.error(f"!!! 標準導入 from linebot.v3.webhooks import WebhookParser 失敗: {e1}")
except Exception as e_other:
     logger.error(f"!!! 嘗試標準導入和實例化時發生其他錯誤: {e_other}", exc_info=True)

if not problem_solved:
    logger.info("--- 標準導入失敗，嘗試直接從 parser 模組導入 ---")
    try:
        from linebot.v3.webhooks.parser import WebhookParser
        logger.info(">>> 直接導入 from linebot.v3.webhooks.parser import WebhookParser 成功!")
        channel_secret = os.getenv('LINE_CHANNEL_SECRET', 'dummy_secret_for_test')
        parser = WebhookParser(channel_secret)
        logger.info(">>> WebhookParser 實例化成功 (透過直接導入)!")
        problem_solved = True # 雖然方法不同，但至少能用了
    except ImportError as e2:
        logger.error(f"!!! 直接導入 from linebot.v3.webhooks.parser import WebhookParser 也失敗: {e2}")
    except Exception as e_other_direct:
         logger.error(f"!!! 嘗試直接導入和實例化時發生其他錯誤: {e_other_direct}", exc_info=True)


app = Flask(__name__)

@app.route('/')
def hello():
    logger.info("根路徑 '/' 被訪問。")
    if problem_solved:
        return "極簡測試 App 運行中。WebhookParser 導入似乎成功了，請檢查 Render Logs 細節。"
    else:
        return "極簡測試 App 運行中。WebhookParser 導入失敗，請檢查 Render Logs 細節。"

# 這個測試版本不需要 callback 或 handler

if __name__ == "__main__":
    port = int(os.environ.get('PORT', 8080))
    app.run(host='0.0.0.0', port=port)
else:
    logger.info("極簡測試 App (透過 Gunicorn) 啟動。")
# --- 極簡測試版結束 ---
