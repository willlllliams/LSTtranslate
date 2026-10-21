"""测试用的合成 Argos 模型包：结构与真实包一致，模型文件是占位内容，只用于扫描、解压和 fingerprint 测试。"""

import json
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
ASSETS = ROOT / "assets" / "argosmodel"
REAL_EN_ZH = ASSETS / "translate-en_zh-1_9.argosmodel"
REAL_ZH_EN = ASSETS / "translate-zh_en-1_9.argosmodel"


def make_package(
    directory: Path,
    from_code: str,
    to_code: str,
    *,
    version: str = "1.0",
    name: str | None = None,
    sentencepiece: bool = True,
    model_bytes: bytes = b"model",
    extra_members: dict[str, bytes] | None = None,
) -> Path:
    """在 directory 下写一个 .argosmodel 并返回路径。sentencepiece=False 模拟 BPE 分词的包。"""
    root = name or f"translate-{from_code}_{to_code}-{version.replace('.', '_')}"
    path = directory / f"{root}.argosmodel"
    metadata = {"package_version": version, "from_code": from_code, "to_code": to_code}
    directory.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr(f"{root}/metadata.json", json.dumps(metadata))
        archive.writestr(f"{root}/README.md", "test package")
        if sentencepiece:
            archive.writestr(f"{root}/sentencepiece.model", b"spm")
        archive.writestr(f"{root}/model/model.bin", model_bytes)
        archive.writestr(f"{root}/model/config.json", json.dumps({"add_source_bos": False}))
        archive.writestr(f"{root}/model/shared_vocabulary.json", json.dumps(["</s>", "<unk>", "▁a"]))
        archive.writestr(f"{root}/stanza/resources.json", "{}")
        for member, data in (extra_members or {}).items():
            archive.writestr(f"{root}/{member}", data)
    return path
