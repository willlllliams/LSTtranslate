"""语言代码的规范写法（架构文档 2.6 节）。

全程序统一用 BCP 47 的一个子集：中文区分 zh-Hans 和 zh-Hant，其他语言只用主语言子标签（en、ja、de）。
NMT 进程在两处用到它：把 Argos 模型包里的语言码映射成规范写法，以及把请求里的语言码按同一规则整理。
"""

from lst.contracts.langs import normalize_lang

# Argos 的私有代码：zt 表示繁体中文。zh 在 Argos 里指简体中文。
_ARGOS_SPECIAL = {"zh": "zh-Hans", "zt": "zh-Hant"}

# 句子之间不加空格的目标语言。
_NO_SPACE_LANGUAGES = {"zh-Hans", "zh-Hant", "ja"}


def canonical_code(code: str) -> str:
    """把一个语言码整理成规范写法，规则与主进程相同（contracts.langs.normalize_lang）。

    NMT 进程面对的是模型包和主进程传来的代码，不能因为一个怪异的代码就让请求失败，
    所以非法输入不抛异常，退回“主语言子标签转小写”。
    """
    try:
        return normalize_lang(code)
    except ValueError:
        return code.strip().replace("_", "-").split("-")[0].lower()


def from_argos_code(code: str) -> str:
    """把 Argos 模型包 metadata.json 里的 from_code、to_code 映射成规范写法。"""
    key = code.strip().lower()
    return _ARGOS_SPECIAL.get(key) or canonical_code(key)


def sentence_joiner(target: str) -> str:
    """译文句子之间的分隔：中文、日文不加空格，其他语言加一个空格。"""
    return "" if canonical_code(target) in _NO_SPACE_LANGUAGES else " "
