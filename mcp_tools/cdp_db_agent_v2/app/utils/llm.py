"""
LLM工具模組
"""
import os
from langchain_openai import ChatOpenAI

def get_llm_instance(temperature: float = 0.0) -> ChatOpenAI:
    """
    獲取LLM實例

    Args:
        temperature: 溫度參數

    Returns:
        ChatOpenAI實例
    """
    llm_provider = os.getenv("LLM_PROVIDER", "openai").lower()

    if llm_provider == "openai":
        return ChatOpenAI(
            model=os.getenv("OPENAI_MODEL", "gpt-4-turbo"),
            temperature=temperature,
            api_key=os.getenv("OPENAI_API_KEY")
        )
    elif llm_provider == "ollama":
        return ChatOpenAI(
            model=os.getenv("OLLAMA_MODEL", "llama3"),
            temperature=temperature,
            base_url=os.getenv("OLLAMA_API_BASE_URL", "http://localhost:11434"),
            api_key="ollama"
        )
    else:
        raise ValueError(f"不支援的LLM提供者: {llm_provider}")
