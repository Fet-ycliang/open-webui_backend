#!/usr/bin/env python3
import os
import sys

# 設置環境變數
os.environ['VECTOR_DB'] = 'chroma'
os.environ['WEBUI_SECRET_KEY'] = 'your-secret-key-here'

# 添加專案路徑
sys.path.insert(0, os.path.abspath('.'))

print("🚀 正在啟動 Open WebUI...")
print("📁 當前目錄:", os.getcwd())
print("🔧 VECTOR_DB:", os.environ.get('VECTOR_DB'))

try:
    import uvicorn
    print("✅ uvicorn 模組已載入")
    
    # 嘗試導入 open_webui
    import open_webui.main
    print("✅ open_webui.main 模組已載入")
    
    print("🌐 啟動伺服器在 http://127.0.0.1:8081")
    uvicorn.run(
        "open_webui.main:app",
        host="127.0.0.1",
        port=8081,
        forwarded_allow_ips="*",
        workers=1
    )
except Exception as e:
    print(f"❌ 啟動失敗: {e}")
    import traceback
    traceback.print_exc()
