from typing import Optional

from langchain_openai import ChatOpenAI

from src.app_config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL


MODEL_REGISTRY = {
    "deepseek-v4-pro": "deepseek-v4-pro",
    "deepseek-v4-flash": "deepseek-v4-flash",
}


def get_llm(
    model: Optional[str] = None,
    api_key: Optional[str] = None,
    base_url: Optional[str] = None,
) -> ChatOpenAI:
    """Create the configured DeepSeek chat model through its OpenAI-compatible API."""
    model_name = MODEL_REGISTRY.get(model, model) if model else DEEPSEEK_MODEL
    resolved_api_key = api_key or DEEPSEEK_API_KEY
    if not resolved_api_key:
        raise ValueError("DEEPSEEK_API_KEY is not configured")

    return ChatOpenAI(
        api_key=resolved_api_key,
        base_url=base_url or DEEPSEEK_BASE_URL,
        model=model_name,
        temperature=0.2,
        timeout=30,
        max_retries=1,
    )
