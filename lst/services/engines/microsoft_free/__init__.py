"""微软免费翻译引擎（Edge 浏览器使用的接口，不需要 API key）。"""

from lst.services.engines.microsoft_free.engine import MicrosoftFreeSettings, create_microsoft_free_engine

__all__ = ["MicrosoftFreeSettings", "create_microsoft_free_engine"]
