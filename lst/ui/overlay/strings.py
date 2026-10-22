"""覆盖窗口的文案表（需求 CFG-05）。导入本模块时登记文案。"""

from lst.ui.i18n import Catalog, register_catalog

CATALOG: Catalog = {
    "overlay.recognizing": {"zh-Hans": "识别中…", "en": "Recognizing…"},
    "overlay.loading_model": {"zh-Hans": "识别模型加载中…", "en": "Loading the recognition model…"},
    "overlay.failed": {"zh-Hans": "识别失败：{reason}", "en": "Recognition failed: {reason}"},
    "overlay.retry": {"zh-Hans": "重试", "en": "Retry"},
    "overlay.no_text": {"zh-Hans": "没有识别到文字", "en": "No text was recognized"},
    "overlay.progress": {"zh-Hans": "{finished}/{total}", "en": "{finished}/{total}"},
    # 悬停提示（需求 OV-06）
    "overlay.tip.state.queued": {"zh-Hans": "排队", "en": "Queued"},
    "overlay.tip.state.running": {"zh-Hans": "翻译中", "en": "Translating"},
    "overlay.tip.state.done": {"zh-Hans": "完成", "en": "Done"},
    "overlay.tip.state.failed": {"zh-Hans": "失败", "en": "Failed"},
    "overlay.tip.state.cancelled": {"zh-Hans": "已取消", "en": "Cancelled"},
    "overlay.tip.latency": {"zh-Hans": "{ms} 毫秒", "en": "{ms} ms"},
    "overlay.tip.cached": {"zh-Hans": "缓存", "en": "Cached"},
    "overlay.tip.source": {"zh-Hans": "原文", "en": "Source"},
    "overlay.tip.skipped.no_text": {"zh-Hans": "无需翻译", "en": "Nothing to translate"},
    "overlay.tip.skipped.same_language": {
        "zh-Hans": "与目标语言相同，不翻译",
        "en": "Already in the target language; not translated",
    },
    "overlay.tip.skipped.no_engine": {
        "zh-Hans": "没有支持这个语言对的引擎",
        "en": "No engine supports this language pair",
    },
}

register_catalog(CATALOG)
