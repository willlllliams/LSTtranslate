"""词典目录里已安装的词典包，以及读入内存的包的缓存。

模型管理模块把词典包装到词典目录（模型目录下的 dict），文件名 ``dict-<src>-<tgt>.tsv.gz``。
字典引擎不订阅总线消息，自己发现目录的变化：

- 已安装的语言对按文件名得出。``installed_pairs`` 每次枚举目录，装好或删掉一个包后立即生效。
  目录修改时间会发生碰撞，不能作为文件列表缓存的唯一依据。枚举只读取名称和文件类型，
  包内容的解压与解析仍使用下面的独立缓存；
- 读入内存的包按（修改时间，大小）记下文件的状态。``load`` 每次核对文件状态，包被更新后重新读取，
  被删除后从缓存里去掉。解压与解析放在线程里执行（调用方用 ``asyncio.to_thread`` 调用 ``load``）。

缓存最多保留 MAX_LOADED_PACKS 个包，超出时丢掉最久没用的。一个全量的包读入后占几十 MB 内存。
一个词典目录只对应一个 PackLibrary（``shared_library``），配置变化导致引擎实例重建时不必重新读包。
"""

import logging
import os
import threading
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Final

from lst.contracts.ids import Lang
from lst.contracts.models import dict_pack_file_name, parse_dict_pack_file_name
from lst.services.engines.dictionary.pack import DictPack, PackFormatError, read_pack_file

MAX_LOADED_PACKS: Final = 4
"""同时留在内存里的词典包数。"""

type Pair = tuple[Lang, Lang]

logger = logging.getLogger(__name__)


class PackUnavailableError(Exception):
    """词典包没有安装、读不出来或格式不对。detail 是给日志用的技术细节。"""

    def __init__(self, detail: str) -> None:
        super().__init__(detail)
        self.detail = detail


@dataclass(frozen=True, slots=True)
class _Loaded:
    pack: DictPack
    stamp: tuple[int, int]  # 文件的（修改时间，大小）


def _stamp(path: Path) -> tuple[int, int]:
    info = path.stat()
    return info.st_mtime_ns, info.st_size


class PackLibrary:
    """一个词典目录。方法都可以从任意线程调用，内部用锁保护。"""

    def __init__(self, directory: Path, *, max_loaded: int = MAX_LOADED_PACKS) -> None:
        self.directory = Path(directory)
        self._max_loaded = max(1, max_loaded)
        self._lock = threading.Lock()
        self._load_lock = threading.Lock()
        self._loaded: OrderedDict[Pair, _Loaded] = OrderedDict()

    def installed_pairs(self) -> frozenset[Pair]:
        """词典目录里有包文件的语言对。目录不存在或无法读取时为空集。"""
        pairs: set[Pair] = set()
        try:
            with os.scandir(self.directory) as entries:
                for entry in entries:
                    pair = parse_dict_pack_file_name(entry.name)
                    if pair is not None and entry.is_file():
                        pairs.add(pair)
        except FileNotFoundError:
            return frozenset()
        except OSError:
            logger.warning("无法读取词典目录 %s", self.directory, exc_info=True)
            return frozenset()
        return frozenset(pairs)

    def has(self, source: str, target: str) -> bool:
        return (Lang(source), Lang(target)) in self.installed_pairs()

    def load(self, source: str, target: str) -> DictPack:
        """取一个语言对的词典包，必要时从磁盘读入。同步执行，由调用方放进线程。

        包没有安装、读取失败或格式不对时抛出 PackUnavailableError。同一时间只读一个包：两个任务同时要
        同一个还没读入的包时，后一个等前一个读完后直接用缓存。
        """
        pair = (Lang(source), Lang(target))
        path = self.directory / dict_pack_file_name(source, target)
        with self._load_lock:
            try:
                stamp = _stamp(path)
            except FileNotFoundError:
                self._forget(pair)
                raise PackUnavailableError(f"没有安装 {source}→{target} 的词典包") from None
            except OSError as exc:
                raise PackUnavailableError(f"无法读取词典包 {path.name}（{type(exc).__name__}）") from exc
            with self._lock:
                cached = self._loaded.get(pair)
                if cached is not None and cached.stamp == stamp:
                    self._loaded.move_to_end(pair)
                    return cached.pack
            try:
                pack = read_pack_file(path)
            except PackFormatError as exc:
                self._forget(pair)
                raise PackUnavailableError(f"词典包 {path.name} 格式不对：{exc}") from exc
            except OSError as exc:
                self._forget(pair)
                raise PackUnavailableError(f"无法读取词典包 {path.name}（{type(exc).__name__}）") from exc
            if (pack.source, pack.target) != pair:
                self._forget(pair)
                raise PackUnavailableError(
                    f"词典包 {path.name} 的包头是 {pack.source}→{pack.target}，与文件名不符"
                )
            with self._lock:
                self._loaded[pair] = _Loaded(pack, stamp)
                self._loaded.move_to_end(pair)
                while len(self._loaded) > self._max_loaded:
                    self._loaded.popitem(last=False)
            logger.info(
                "已读入词典包 %s：%d 个词条，%d 个变形", path.name, len(pack.entries), len(pack.forms)
            )
            return pack

    def loaded_pairs(self) -> tuple[Pair, ...]:
        """当前留在内存里的包，最久没用的在前。"""
        with self._lock:
            return tuple(self._loaded)

    def _forget(self, pair: Pair) -> None:
        with self._lock:
            self._loaded.pop(pair, None)


_LIBRARIES: dict[Path, PackLibrary] = {}
_LIBRARIES_LOCK = threading.Lock()


def shared_library(directory: Path) -> PackLibrary:
    """词典目录对应的 PackLibrary，同一个目录在进程里只有一个。"""
    key = Path(os.path.normcase(os.path.abspath(directory)))
    with _LIBRARIES_LOCK:
        library = _LIBRARIES.get(key)
        if library is None:
            library = _LIBRARIES[key] = PackLibrary(Path(directory))
        return library
