"""模型包的解压与磁盘操作。全部是同步函数，由模块放进线程执行。

PaddleX 的模型包是 ``<模型名>_infer.tar``。按 paddlex/utils/download.py 的 download_and_extract
（no_interm_dir=True）的做法推断包内结构：

- 解压后顶层只有一项时，这一项（通常是 ``<模型名>_infer/`` 目录）就是模型目录；
- 顶层有多项时，取名为 ``<模型名>`` 的那一项。

PaddleX 的注释承认它没有防御路径穿越（CVE-2007-4559）。这里逐项检查：只接受普通文件和目录，拒绝
链接、设备文件、绝对路径和含 ``..`` 的路径，再用 tarfile 的 ``data`` 过滤器解压，两层检查任一层不通过
都按无效包处理，解压目录整个删除。成员数量与解压后的总大小各有上限，读到超限的成员时立即停止。
"""

import logging
import os
import shutil
import stat
import tarfile
from pathlib import Path, PurePosixPath
from typing import Final

from lst._fileops import publish_directory
from lst.services._shared.download import dir_size
from lst.services.ocr_engines.layout import model_ready

__all__ = ["InvalidArchive", "dir_size", "extract_model", "remove_leftovers", "remove_tree"]

MAX_MEMBERS: Final = 10_000
"""一个模型包里成员数量的上限。真实的包只有几个文件。"""

MAX_EXTRACT_BYTES: Final = 4 << 30
"""一个模型包解压后的总大小上限（4 GiB）。最大的图表识别模型约 1.4 GB。"""


class InvalidArchive(Exception):
    """模型包不是合法的 tar、含有不安全的成员，或解压后找不到模型文件。"""


def _check_member(member: tarfile.TarInfo) -> None:
    name = member.name.replace("\\", "/")
    path = PurePosixPath(name)
    if not name or path.is_absolute() or name.startswith("/") or ":" in path.parts[0]:
        raise InvalidArchive(f"模型包含有绝对路径：{member.name!r}")
    if ".." in path.parts:
        raise InvalidArchive(f"模型包含有指向上级目录的路径：{member.name!r}")
    if not (member.isfile() or member.isdir()):
        raise InvalidArchive(f"模型包含有不支持的成员类型：{member.name!r}")


def _select_root(extract_dir: Path, model_name: str) -> Path:
    entries = list(extract_dir.iterdir())
    root = entries[0] if len(entries) == 1 else extract_dir / model_name
    if not root.is_dir():
        raise InvalidArchive("模型包里找不到模型目录")
    if not model_ready(root):
        raise InvalidArchive("模型包里缺少 inference.yml 或 inference.pdiparams")
    return root


def extract_model(archive: Path, extract_dir: Path, final_dir: Path, model_name: str) -> None:
    """把模型包解压到 extract_dir，校验后原子改名为 final_dir，最后删除 extract_dir 与模型包。

    任何一步失败都删除 extract_dir 并抛出（InvalidArchive 或 OSError），final_dir 不受影响。
    final_dir 已存在（上次留下的不完整目录）时先删除。
    """
    remove_tree(extract_dir)
    extract_dir.mkdir(parents=True)
    try:
        try:
            with tarfile.open(archive, "r:*") as tar:
                # 逐个读取成员：边读边计数、累计大小，超出上限立即停下，不先把整张成员表读进内存。
                total = 0
                for count, member in enumerate(tar, start=1):
                    if count > MAX_MEMBERS:
                        raise InvalidArchive("模型包成员数量超出上限")
                    _check_member(member)
                    total += member.size
                    if total > MAX_EXTRACT_BYTES:
                        raise InvalidArchive("模型包解压后的大小超出上限")
                    tar.extract(member, extract_dir, filter="data")
        except tarfile.FilterError as exc:
            raise InvalidArchive(f"模型包含有不安全的成员：{exc}") from exc
        except tarfile.TarError as exc:
            raise InvalidArchive(f"模型包不是合法的 tar 文件：{exc}") from exc
        root = _select_root(extract_dir, model_name)
        remove_tree(final_dir)
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        publish_directory(root, final_dir)
    finally:
        remove_tree(extract_dir)
    archive.unlink(missing_ok=True)


def _clear_readonly(function: object, path: str, _exc: BaseException) -> None:
    """rmtree 遇到只读文件时去掉只读属性再删一次（pip 装出的部分文件带只读属性）。"""
    del function
    os.chmod(path, stat.S_IWRITE)
    if os.path.isdir(path):
        os.rmdir(path)
    else:
        os.unlink(path)


def remove_tree(path: Path) -> None:
    """删除一个目录或文件；不存在时什么也不做。失败时抛 OSError。"""
    if path.is_symlink() or path.is_file():
        path.unlink()
    elif path.is_dir():
        shutil.rmtree(path, onexc=_clear_readonly)


def remove_leftovers(paths: list[Path], models_dir: Path | None, logger: logging.Logger) -> int:
    """删除上次安装中断留下的半成品：给出的路径，以及模型目录下以点开头的 .part 文件与解压目录。

    models_dir 为 None（引擎没有单独的模型目录，例如 RapidOCR）时只删给出的路径。
    返回删除的项数。删不掉的只记 WARNING（可能被别的进程占用），不影响启动。
    """
    targets = [path for path in paths if path.exists()]
    if models_dir is not None and models_dir.is_dir():
        targets.extend(
            entry
            for entry in models_dir.iterdir()
            if entry.name.startswith(".") and entry.name.endswith((".part", ".extract"))
        )
    removed = 0
    for target in targets:
        try:
            remove_tree(target)
            removed += 1
        except OSError:
            logger.warning("无法删除安装中断留下的 %s", target, exc_info=True)
    return removed
