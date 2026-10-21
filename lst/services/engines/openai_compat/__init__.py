"""OpenAI 兼容接口的 LLM 引擎：在线 LLM（DeepSeek、OpenAI 等）与本地 LLM（Ollama、llama.cpp server）共用。"""

from lst.services.engines.openai_compat.engine import create_openai_compat_engine
from lst.services.engines.openai_compat.settings import OpenAICompatSettings

__all__ = ["OpenAICompatSettings", "create_openai_compat_engine"]
