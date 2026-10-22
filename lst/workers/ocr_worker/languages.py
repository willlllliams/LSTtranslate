"""识别语言到识别模型的选择（需求 OCR-04）。

语言码是主进程规范化后的写法（lst.contracts.langs：zh-Hans、zh-Hant、en、ja……），或 "auto"。

- auto、中文（简、繁）、英文、日文：使用配置的通用识别模型（默认 PP-OCRv5_mobile_rec）。
  PP-OCRv5 的通用识别模型同时覆盖简体中文、繁体中文、英文和日文，满足“自动”档的要求。
- 其他语言：PaddleOCR 3.7 按文字系统提供的 PP-OCRv5 多语言识别模型，命名为 `<文字系统>_PP-OCRv5_mobile_rec`。
  语言到文字系统的归属取自 PaddleOCR 3.7 的 paddleocr/_utils/langs.py（LATIN_LANGS 等集合），
  这里只保留其中的 ISO 639-1 两字母代码。

这些多语言模型目前不在本机的模型目录里；选中它们时加载阶段报“模型目录中缺少……”（CONFIG_ERROR），
不会去下载。
"""

from typing import Final

GENERAL_LANGUAGES: Final = frozenset({"auto", "zh-Hans", "zh-Hant", "zh", "en", "ja"})

_SCRIPT_OF_LANGUAGE: Final[dict[str, str]] = {
    "ko": "korean",
    "th": "th",
    "el": "el",
    "te": "te",
    "ta": "ta",
    **dict.fromkeys(("ru", "be", "uk"), "eslav"),
    **dict.fromkeys(("bg", "mn", "kk", "ky", "tg", "mk", "tt", "cv", "ba", "kv", "os"), "cyrillic"),
    **dict.fromkeys(("ar", "fa", "ug", "ur", "ps", "sd", "ku"), "arabic"),
    **dict.fromkeys(("hi", "mr", "ne", "bh", "mai", "sa", "bho", "gom"), "devanagari"),
    **dict.fromkeys(
        ("af", "az", "bs", "cs", "cy", "da", "de", "es", "et", "fr", "ga", "hr", "hu", "id", "is", "it",
         "la", "lt", "lv", "mi", "ms", "mt", "nl", "no", "nb", "nn", "oc", "pl", "pt", "ro", "sk", "sl",
         "sq", "sv", "sw", "tl", "tr", "uz", "vi", "fi", "eu", "gl", "lb", "rm", "ca", "qu", "sr"),
        "latin",
    ),
}  # fmt: skip


class UnsupportedLanguage(ValueError):
    """没有覆盖该语言的识别模型。"""


def recognition_model(language: str, general_model: str) -> str:
    """按识别语言选择识别模型名。

    general_model 是配置选用的识别模型：auto 与中英日直接用它；其他语言时它本身就是该语言文字系统的专用模型
    （名字以 ``<文字系统>_`` 开头）才用它，否则用 ``<文字系统>_PP-OCRv5_mobile_rec``。
    """
    if language in GENERAL_LANGUAGES:
        return general_model
    script = _SCRIPT_OF_LANGUAGE.get(language)
    if script is None:
        raise UnsupportedLanguage(f"没有覆盖语言 {language!r} 的识别模型")
    if general_model.startswith(f"{script}_"):
        return general_model
    return f"{script}_PP-OCRv5_mobile_rec"
