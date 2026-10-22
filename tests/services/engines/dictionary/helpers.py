"""字典引擎测试用的合成词典包。

包的内容在内存里拼出，与发布页上的包格式相同：gzip 压缩的 UTF-8 文本，第 1 行是包头，
之后是 ``w`` 词条行与 ``f`` 变形行。测试不读取真实词典包，也不访问网络。
"""

import gzip
from collections.abc import Mapping
from pathlib import Path

from lst.contracts.models import dict_pack_file_name
from lst.services.engines.dictionary.pack import DictPack, parse_pack_text


def pack_text(
    source: str,
    target: str,
    entries: Mapping[str, str],
    forms: Mapping[str, str] | None = None,
    *,
    version: str = "20261003",
    source_name: str = "Test",
    license_id: str = "CC-BY-SA-4.0",
) -> str:
    """词典包解压后的文本。entries、forms 的 key 应当已经归一化，与构建脚本的输出相同。"""
    lines = [f"#dict\t{source}\t{target}\t{version}\t{source_name}\t{license_id}"]
    lines += [f"w\t{key}\t{value}" for key, value in entries.items()]
    lines += [f"f\t{key}\t{lemma}" for key, lemma in (forms or {}).items()]
    return "\n".join(lines) + "\n"


def pack_bytes(
    source: str,
    target: str,
    entries: Mapping[str, str],
    forms: Mapping[str, str] | None = None,
    **header: str,
) -> bytes:
    """gzip 压缩后的词典包文件内容。"""
    return gzip.compress(pack_text(source, target, entries, forms, **header).encode("utf-8"))


def make_pack(
    source: str, target: str, entries: Mapping[str, str], forms: Mapping[str, str] | None = None
) -> DictPack:
    """直接得到内存里的 DictPack，不经过文件。"""
    return parse_pack_text(pack_text(source, target, entries, forms))


def write_pack(
    directory: Path,
    source: str,
    target: str,
    entries: Mapping[str, str],
    forms: Mapping[str, str] | None = None,
    **header: str,
) -> Path:
    """把合成的词典包按标准文件名写进 directory，返回路径。"""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / dict_pack_file_name(source, target)
    path.write_bytes(pack_bytes(source, target, entries, forms, **header))
    return path


EN_ZH_ENTRIES: Mapping[str, str] = {
    "the": "那",
    "cat": "猫",
    "be": "是",
    "sleep": "睡眠",
    "warm": "暖和的",
    "in": "在里",
    "in front": "前方",
    "in front of": "在前面",
    "house": "房子",
    "new york": "纽约",
}
EN_ZH_FORMS: Mapping[str, str] = {"is": "be", "sleeping": "sleep", "cats": "cat"}

ZH_EN_ENTRIES: Mapping[str, str] = {
    "我": "I",
    "爱": "love",
    "你": "you",
    "中华": "China",
    "中华人民共和国": "PRC",
    "人民": "people",
    "万岁": "long live",
    "t恤": "T-shirt",
    "很": "very",
    "好": "good",
    "世界": "world",
    "你好": "hello",
}
