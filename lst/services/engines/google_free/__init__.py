"""谷歌免费翻译引擎（translate.googleapis.com 的 client=gtx 接口，不需要 API key）。"""

from lst.services.engines.google_free.engine import GoogleFreeSettings, create_google_free_engine

__all__ = ["GoogleFreeSettings", "create_google_free_engine"]
