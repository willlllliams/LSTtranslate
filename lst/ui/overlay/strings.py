"""覆盖窗口的文案表（需求 CFG-05）。导入本模块时登记文案。"""

from lst.ui.i18n import Catalog, register_catalog

CATALOG: Catalog = {
    "overlay.recognizing": {"zh-Hans": "识别中…", "en": "Recognizing…"},
    "overlay.loading_model": {"zh-Hans": "识别模型加载中…", "en": "Loading the recognition model…"},
    "overlay.failed": {"zh-Hans": "识别失败：{reason}", "en": "Recognition failed: {reason}"},
    "overlay.retry": {"zh-Hans": "重试", "en": "Retry"},
    "overlay.no_text": {"zh-Hans": "没有识别到文字", "en": "No text was recognized"},
    "overlay.progress": {"zh-Hans": "{finished}/{total}", "en": "{finished}/{total}"},
}

register_catalog(CATALOG)
