"""
除錯工具模組
"""
import os
from typing import Any

def debug_print(title: str, content: Any):
    """
    除錯輸出函數

    Args:
        title: 除錯標題
        content: 除錯內容
    """
    if os.getenv("DEBUG_MODE", "false").lower() == "true":
        print(f"\n--- DEBUG: {title} ---\n{content}\n--- END DEBUG ---")
