"""语言代码的规范写法（架构文档 2.6 节）。主进程与推理进程共用这一份规则。

调度器按字符串比较语言代码，所以全程序统一用 BCP 47 的一个子集：中文区分 zh-Hans 与 zh-Hant，
其他语言只用主语言子标签（en、ja、de）。
"""

import re
from typing import Final

from lst.contracts.ids import Lang

ZH_HANS: Final = Lang("zh-Hans")
ZH_HANT: Final = Lang("zh-Hant")

_SUBTAG = re.compile(r"[a-z0-9]{1,8}")
_PRIMARY = re.compile(r"[a-z]{2,3}")

# BCP 47 的特殊主语言码：未定、多语言、未编码、无语言内容。它们不是具体语言，不能参与翻译。
_SPECIAL_PRIMARY: Final = frozenset({"und", "mul", "mis", "zxx"})

# 已废弃的 ISO 639-1 代码与现行代码的对应。no（挪威语宏语言）归到 nb：Argos、lingua 和
# Google 语言表都用 nb 表示书面挪威语。
_PRIMARY_ALIASES: Final = {"iw": "he", "in": "id", "ji": "yi", "jw": "jv", "mo": "ro", "no": "nb"}

# BCP 47 要求用最短的 ISO 639 代码。下表是 lingua 覆盖的 75 种语言的三字母码到两字母码的对应，
# 由 lingua 2.2 的语言枚举生成后写成常量（contracts 只依赖标准库）。
_ISO639_3_TO_1: Final = {
    "afr": "af", "ara": "ar", "aze": "az", "bel": "be", "ben": "bn", "bos": "bs", "bul": "bg",
    "cat": "ca", "ces": "cs", "cym": "cy", "dan": "da", "deu": "de", "ell": "el", "eng": "en",
    "epo": "eo", "est": "et", "eus": "eu", "fas": "fa", "fin": "fi", "fra": "fr", "gle": "ga",
    "guj": "gu", "heb": "he", "hin": "hi", "hrv": "hr", "hun": "hu", "hye": "hy", "ind": "id",
    "isl": "is", "ita": "it", "jpn": "ja", "kat": "ka", "kaz": "kk", "kor": "ko", "lat": "la",
    "lav": "lv", "lit": "lt", "lug": "lg", "mar": "mr", "mkd": "mk", "mon": "mn", "mri": "mi",
    "msa": "ms", "nld": "nl", "nno": "nn", "nob": "nb", "pan": "pa", "pol": "pl", "por": "pt",
    "ron": "ro", "rus": "ru", "slk": "sk", "slv": "sl", "sna": "sn", "som": "so", "sot": "st",
    "spa": "es", "sqi": "sq", "srp": "sr", "swa": "sw", "swe": "sv", "tam": "ta", "tel": "te",
    "tgl": "tl", "tha": "th", "tsn": "tn", "tso": "ts", "tur": "tr", "ukr": "uk", "urd": "ur",
    "vie": "vi", "xho": "xh", "yor": "yo", "zho": "zh", "zul": "zu",
}  # fmt: skip

# 中文的地区子标签中，使用繁体字的地区。
_HANT_REGIONS: Final = frozenset({"tw", "hk", "mo"})

# 微软接口沿用的旧写法。
_ZH_LEGACY: Final = {"chs": ZH_HANS, "cht": ZH_HANT}


def normalize_lang(code: str) -> Lang:
    """把一个语言码整理成规范写法，非法时抛出 ValueError。

    - 中文：zh、zh-CN、zh-SG、zh-hans、zho → zh-Hans；zh-TW、zh-HK、zh-MO、zh-Hant → zh-Hant。
      书写系统子标签优先于地区子标签，zh-Hans-TW 是 zh-Hans。
    - 其他语言取主语言子标签并转小写：en-US → en，PT_br → pt，sr-Latn-RS → sr；
      有两字母代码的三字母码换成两字母码：eng → en；废弃代码换成现行代码：iw → he。

    合法的写法：各子标签由连字符或下划线分隔，每个子标签 1 至 8 个 ASCII 字母或数字，
    主语言子标签 2 至 3 个字母，且不是 und、mul、mis、zxx 或 qaa–qtz 私用区代码。
    中文只接受 Hans、Hant 两种书写系统。
    """
    raw = code.strip()
    parts = raw.lower().replace("_", "-").split("-")
    if not raw or not raw.isascii() or any(_SUBTAG.fullmatch(part) is None for part in parts):
        raise ValueError(f"语言代码 {code!r} 不符合 BCP 47 格式")
    primary, rest = parts[0], parts[1:]
    if _PRIMARY.fullmatch(primary) is None:
        raise ValueError(f"语言代码 {code!r} 的主语言子标签应为 2 至 3 个字母")
    if primary in _SPECIAL_PRIMARY or "qaa" <= primary <= "qtz":
        raise ValueError(f"语言代码 {code!r} 不表示一种具体语言")
    primary = _PRIMARY_ALIASES.get(primary, primary)
    primary = _ISO639_3_TO_1.get(primary, primary)
    if primary != "zh":
        return Lang(primary)
    return _chinese_variant_of_tag(code, rest)


def _chinese_variant_of_tag(code: str, rest: list[str]) -> Lang:
    for subtag in rest:
        if subtag in _ZH_LEGACY:
            return _ZH_LEGACY[subtag]
        if len(subtag) == 4 and subtag.isalpha():  # 书写系统子标签
            if subtag == "hans":
                return ZH_HANS
            if subtag == "hant":
                return ZH_HANT
            raise ValueError(f"语言代码 {code!r}：中文只支持 Hans、Hant 两种书写系统")
    return ZH_HANT if any(subtag in _HANT_REGIONS for subtag in rest) else ZH_HANS
