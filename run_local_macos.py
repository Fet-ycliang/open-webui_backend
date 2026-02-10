#!/usr/bin/env python3
import os
import sys
import uvicorn
from dotenv import load_dotenv
from pathlib import Path

def check_vector_database():
    """檢查向量資料庫配置和狀態"""
    print("=" * 60)
    print("OpenWebUI 向量資料庫檢查")
    print("=" * 60)

    # 檢查是否使用雲端 ChromaDB
    chroma_http_host = os.environ.get("CHROMA_HTTP_HOST", "").strip()

    if chroma_http_host:
        # 雲端模式
        print(f"🌐 使用雲端 ChromaDB 模式")
        print(f"✅ HTTP Host: {chroma_http_host}")

        chroma_http_port = os.environ.get("CHROMA_HTTP_PORT", "8000")
        chroma_http_ssl = os.environ.get("CHROMA_HTTP_SSL", "false").lower() == "true"
        chroma_tenant = os.environ.get("CHROMA_TENANT", "default")
        chroma_database = os.environ.get("CHROMA_DATABASE", "default")

        protocol = "https" if chroma_http_ssl else "http"
        print(f"🔗 連接 URL: {protocol}://{chroma_http_host}:{chroma_http_port}")
        print(f"🏢 Tenant: {chroma_tenant}")
        print(f"🗄️  Database: {chroma_database}")

        # 檢查驗證配置
        auth_provider = os.environ.get("CHROMA_CLIENT_AUTH_PROVIDER")
        if auth_provider:
            print(f"🔐 驗證方式: {auth_provider}")

        headers = os.environ.get("CHROMA_HTTP_HEADERS")
        if headers:
            print(f"📋 自定義 Headers: 已設定")

        print(f"ℹ️  雲端模式下 CHROMA_DATA_PATH 將被忽略")
        return True  # 假設雲端配置正確，實際連接會在運行時驗證

    else:
        # 本地模式
        print(f"💻 使用本地 ChromaDB 模式")
        chroma_data_path = os.environ.get("CHROMA_DATA_PATH")

        if chroma_data_path:
            print(f"✅ 發現環境變數 CHROMA_DATA_PATH: {chroma_data_path}")

            # 檢查路徑是否存在
            if os.path.exists(chroma_data_path):
                print(f"✅ 向量資料庫目錄存在: {chroma_data_path}")

                # 檢查 chroma.sqlite3 文件
                chroma_db_file = os.path.join(chroma_data_path, "chroma.sqlite3")
                if os.path.exists(chroma_db_file):
                    file_size = os.path.getsize(chroma_db_file)
                    file_size_mb = file_size / (1024 * 1024)
                    print(f"✅ 找到向量資料庫文件: chroma.sqlite3")
                    print(f"📊 資料庫文件大小: {file_size_mb:.2f} MB ({file_size} bytes)")

                    # 檢查是否有其他 Chroma 相關文件
                    chroma_files = [f for f in os.listdir(chroma_data_path) if f.startswith('chroma') or f.endswith('.sqlite3')]
                    if len(chroma_files) > 1:
                        print(f"📁 其他相關文件: {', '.join(chroma_files)}")

                    return True
                else:
                    print(f"⚠️  警告: 找不到 chroma.sqlite3 文件在 {chroma_data_path}")
                    print("   這可能表示向量資料庫尚未初始化或路徑不正確")
                    return False
            else:
                print(f"❌ 錯誤: 向量資料庫路徑不存在: {chroma_data_path}")
                print("   請確認路徑是否正確")
                return False
        else:
            # 使用預設路徑
            default_path = "./backend/data/vector_db"
            print(f"ℹ️  未設定 CHROMA_DATA_PATH，使用預設路徑: {default_path}")

            if os.path.exists(default_path):
                chroma_db_file = os.path.join(default_path, "chroma.sqlite3")
                if os.path.exists(chroma_db_file):
                    file_size = os.path.getsize(chroma_db_file)
                    file_size_mb = file_size / (1024 * 1024)
                    print(f"✅ 找到預設向量資料庫文件")
                    print(f"📊 資料庫文件大小: {file_size_mb:.2f} MB")
                    return True
                else:
                    print(f"⚠️  預設路徑中沒有向量資料庫文件")
                    return False
            else:
                print(f"⚠️  預設向量資料庫路徑不存在")
                return False

def setup_rag_logging():
    """設置 RAG 相關的日志配置"""
    print("\n" + "=" * 60)
    print("RAG 日志配置")
    print("=" * 60)

    # 設置 RAG 日志級別為 INFO，用於顯示搜尋結果
    os.environ["RAG_LOG_LEVEL"] = "INFO"
    os.environ["SRC_LOG_LEVELS"] = '{"RAG": "INFO"}'

    print(f"🔧 RAG Debug 日志: 已啟用")
    print(f"📝 向量搜尋結果將顯示在日志中")
    print(f"ℹ️  日志將輸出到終端")


# Load environment variables from .env file at the project root
load_dotenv()

# 檢查向量資料庫狀態
vector_db_ok = check_vector_database()

# 設置 RAG 日志
setup_rag_logging()

# Add the backend directory to the Python path
backend_dir = os.path.join(os.path.dirname(__file__), 'backend')
sys.path.insert(0, backend_dir)

# Set the FRONTEND_BUILD_DIR environment variable to the correct path
os.environ["FRONTEND_BUILD_DIR"] = os.path.join(backend_dir, "open_webui", "frontend")
print(f"\n🔧 FRONTEND_BUILD_DIR: {os.environ['FRONTEND_BUILD_DIR']}")

# 顯示啟動摘要
print("\n" + "=" * 60)
print("啟動摘要")
print("=" * 60)
print(f"{'✅' if vector_db_ok else '⚠️'} 向量資料庫: {'正常' if vector_db_ok else '需要檢查'}")
print(f"✅ RAG 詳細日志: 已啟用")
print(f"✅ Frontend 路徑: 已設置")

if not vector_db_ok:
    print(f"\n⚠️  注意: 向量資料庫狀態異常，RAG 功能可能無法正常工作")
    print(f"   請檢查 .env 文件中的 CHROMA_DATA_PATH 設置")

print(f"\n🚀 正在啟動 OpenWebUI...")
print("=" * 60)

# Now that the path is set, we can import the app
from open_webui.main import app

if __name__ == "__main__":
    # Get host, port, and workers from environment variables, with defaults
    host = os.environ.get("WEBUI_HOST", "0.0.0.0")
    port = int(os.environ.get("WEBUI_PORT", 9090))
    workers = int(os.environ.get("UVICORN_WORKERS", 1))

    uvicorn.run(
        "open_webui.main:app",
        host=host,
        port=port,
        workers=workers,
        reload=False,
    )