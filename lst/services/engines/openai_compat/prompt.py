"""提示词、请求消息与输出格式（架构文档 2.7.1 节）。

一次请求有两条消息：[system, user]。user 消息的内容顺序固定为“会话全文 → 分隔标记 → 本批段落”：

- system 消息和 user 消息中分隔标记之前的部分只取决于实例配置和会话原文，同一会话各批请求的
  开头因此完全相同，能命中 DeepSeek 的上下文缓存（缓存按请求开头的 token 前缀匹配）；
- 语言对和本批段落这些随批次变化的内容都放在分隔标记之后；
- 只有一条 user 消息，兼容要求 user/assistant 严格交替的对话模板（例如 llama.cpp 用 --jinja
  加载的 Gemma、早期 Mistral 模板，它们拒绝连续两条 user 消息）。

分隔标记以换行开头，而本批段落以 JSON 编码、其中的换行都被转义，所以 user 消息里最后一次出现的
分隔标记就是真正的分界，即使会话原文里恰好含有同样的文字。

截屏翻译附带截图时（需求 ENG-07），with_image 把 user 消息的内容改成数组：第一项是与不带图时
逐字相同的文本，第二项是截图的 data URL。图片放在文本之后，文本前缀与不带图时一致；system 消息末尾
加一句 IMAGE_NOTE，说明截图的用途。一个会话里的各批请求附带同一张截图，前缀因此仍然相同。

模型的输出格式固定为 `{"r":[{"i":1,"t":"译文"},...]}`，i 是段在本批中的序号（从 1 开始）。
分隔标记之后总是写明这个格式并带示例，所以即使用户换掉系统提示词，json_object 模式的要求
（提示词里出现 "json" 并给出示例）也仍然满足。
"""

import json
from collections.abc import Mapping, Sequence
from typing import Final

from lst.services.engines.openai_compat.settings import OutputMode

# 内置提示词或消息模板改变时加一。它参与配置指纹，旧模板下的缓存译文因此不会被误用。
# IMAGE_NOTE 与 with_image 的消息结构也属于模板，修改它们同样要加一。
PROMPT_VERSION: Final = 2

# user 消息中会话全文与本批段落之间的分隔标记。
CONTEXT_SEPARATOR: Final = "\n\n=== Segments to translate ===\n"

DEFAULT_SYSTEM_PROMPT: Final = (
    "You are a professional translator.\n"
    "The user message starts with the full source text of the current document for context. After the "
    'line "=== Segments to translate ===" comes a json object with the segments to translate, in the form '
    '{"segments":[{"i":1,"t":"source text"}]}.\n'
    "Translate the t of every segment into the target language named there. Rules:\n"
    "- Translate each segment on its own. Do not merge, split, skip or reorder segments, and keep the i "
    "of each segment.\n"
    "- Use the full source text only to keep terminology, names and style consistent.\n"
    "- Keep line breaks, numbers, URLs, code and placeholders unchanged.\n"
    "- Output only translations, without explanations, notes or the source text.\n"
    'Reply with a single json object, for example {"r":[{"i":1,"t":"translation of segment 1"},'
    '{"i":2,"t":"translation of segment 2"}]}.'
)

# 附带截图时追加在 system 消息末尾的说明。
IMAGE_NOTE: Final = (
    "The attached screenshot shows the screen the source text was recognized from. Use it to understand "
    "the context and to correct text recognition errors. Translate only the given segments."
)

_CONTEXT_HEADER: Final = "Full source text of the document, for reference only:\n\n"

_REPLY_INSTRUCTION: Final = (
    'Reply with a json object only, in the form {"r":[{"i":1,"t":"translation of segment 1"},'
    '{"i":2,"t":"translation of segment 2"}]}, with exactly one item for each segment and the same i.'
)

# 输出格式的 JSON Schema。OpenAI 的 strict 模式要求每个对象都写 additionalProperties: false，
# 并且所有字段都列在 required 里。
REPLY_SCHEMA: Final[Mapping[str, object]] = {
    "type": "object",
    "properties": {
        "r": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"i": {"type": "integer"}, "t": {"type": "string"}},
                "required": ["i", "t"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["r"],
    "additionalProperties": False,
}

# BCP 47 语言代码到英文语言名。键为小写；模型对英文语言名的理解比对代码更稳定。
_LANGUAGE_NAMES: Final[Mapping[str, str]] = {
    "af": "Afrikaans",
    "ar": "Arabic",
    "az": "Azerbaijani",
    "be": "Belarusian",
    "bg": "Bulgarian",
    "bn": "Bengali",
    "ca": "Catalan",
    "cs": "Czech",
    "cy": "Welsh",
    "da": "Danish",
    "de": "German",
    "el": "Greek",
    "en": "English",
    "eo": "Esperanto",
    "es": "Spanish",
    "et": "Estonian",
    "eu": "Basque",
    "fa": "Persian",
    "fi": "Finnish",
    "fil": "Filipino",
    "fr": "French",
    "ga": "Irish",
    "gl": "Galician",
    "he": "Hebrew",
    "hi": "Hindi",
    "hr": "Croatian",
    "hu": "Hungarian",
    "hy": "Armenian",
    "id": "Indonesian",
    "is": "Icelandic",
    "it": "Italian",
    "ja": "Japanese",
    "ka": "Georgian",
    "kk": "Kazakh",
    "km": "Khmer",
    "ko": "Korean",
    "la": "Latin",
    "lo": "Lao",
    "lt": "Lithuanian",
    "lv": "Latvian",
    "mk": "Macedonian",
    "mn": "Mongolian",
    "ms": "Malay",
    "my": "Burmese",
    "nb": "Norwegian Bokmål",
    "ne": "Nepali",
    "nl": "Dutch",
    "nn": "Norwegian Nynorsk",
    "no": "Norwegian",
    "pl": "Polish",
    "pt": "Portuguese",
    "pt-br": "Brazilian Portuguese",
    "pt-pt": "European Portuguese",
    "ro": "Romanian",
    "ru": "Russian",
    "sk": "Slovak",
    "sl": "Slovenian",
    "sq": "Albanian",
    "sr": "Serbian",
    "sv": "Swedish",
    "sw": "Swahili",
    "ta": "Tamil",
    "th": "Thai",
    "tl": "Tagalog",
    "tr": "Turkish",
    "uk": "Ukrainian",
    "ur": "Urdu",
    "uz": "Uzbek",
    "vi": "Vietnamese",
    "yue": "Cantonese",
    "zh": "Chinese",
    "zh-hans": "Simplified Chinese",
    "zh-hant": "Traditional Chinese",
    "zh-cn": "Simplified Chinese",
    "zh-sg": "Simplified Chinese",
    "zh-tw": "Traditional Chinese",
    "zh-hk": "Traditional Chinese",
}


def language_name(code: str) -> str:
    """把 BCP 47 语言代码换成英文语言名。

    依次去掉末尾的子标签查表，例如 zh-Hans-CN → zh-Hans → zh；全都查不到时返回代码本身。
    """
    parts = code.replace("_", "-").lower().split("-")
    while parts:
        name = _LANGUAGE_NAMES.get("-".join(parts))
        if name is not None:
            return name
        parts.pop()
    return code


def effective_system_prompt(custom: str | None) -> str:
    """实际使用的系统提示词：配置了非空白的提示词时用它，否则用内置提示词。"""
    if custom is not None and custom.strip():
        return custom
    return DEFAULT_SYSTEM_PROMPT


def build_messages(
    system_prompt: str, context: str, texts: Sequence[str], source: str, target: str
) -> list[dict[str, str]]:
    """构造一次请求的 [system, user] 两条消息。texts 是本批段落的原文，按顺序编号为 1..n。"""
    segments = json.dumps(
        {"segments": [{"i": index, "t": text} for index, text in enumerate(texts, start=1)]},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    instruction = (
        f"Translate every segment below from {language_name(source)} to {language_name(target)}. "
        f"{_REPLY_INSTRUCTION}"
    )
    user = f"{_CONTEXT_HEADER}{context}{CONTEXT_SEPARATOR}{instruction}\n\n{segments}"
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user},
    ]


def with_image(messages: Sequence[Mapping[str, str]], data_url: str) -> list[dict[str, object]]:
    """把 build_messages 的结果改成附带截图的形式。

    system 消息末尾加一行 IMAGE_NOTE；user 消息的内容换成数组，先放原样的文本，再放图片：
    `[{"type":"text","text":...},{"type":"image_url","image_url":{"url":data_url}}]`。
    """
    system, user = messages
    return [
        {"role": "system", "content": f"{system['content']}\n{IMAGE_NOTE}"},
        {
            "role": "user",
            "content": [
                {"type": "text", "text": user["content"]},
                {"type": "image_url", "image_url": {"url": data_url}},
            ],
        },
    ]


def response_format(mode: OutputMode) -> dict[str, object]:
    """请求体里的 response_format 字段。"""
    if mode == "json_object":
        return {"type": "json_object"}
    return {
        "type": "json_schema",
        "json_schema": {"name": "translation", "strict": True, "schema": REPLY_SCHEMA},
    }
