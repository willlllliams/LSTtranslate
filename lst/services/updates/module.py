"""应用更新服务（lst/contracts/updates.py）：检查 GitHub Releases 上的新版本，下载、校验并启动安装包。

模块维护一份 ``UpdateStatus``，每次变化都发布完整的状态。三个命令的处理：

1. ``CheckForUpdates``：获取版本列表，选出高于当前版本、附件齐全的最高版本。自动检查失败时只记日志，
   状态回到检查之前；``manual=True`` 的检查失败时进入 ERROR，界面据此提示用户。
2. ``DownloadUpdate``：先取校验文件，再把安装包流式写入下载目录下的 ``<安装包名>.part``，边写边算 SHA-256；
   与校验文件一致才用 ``os.replace`` 改名并进入 READY，否则删除临时文件并进入 ERROR(CHECKSUM)。
   下载目录里已有通过校验的同名安装包时直接进入 READY。
3. ``InstallUpdate``：用启动器以分离进程运行安装包（参数见 launcher.INSTALLER_ARGS），成功后进入 INSTALLING
   并发布 ``QuitRequested``；启动失败进入 ERROR(LAUNCH_FAILED)。

源码运行（can_install 为假）时只检查，下载和安装命令一律进入 ERROR(NOT_INSTALLED)。

并发：下载命令在第一个 await 之前同步进入 DOWNLOADING，安装命令在第一个 await 之前同步设“正在启动”标志，
紧接着到达的重复命令因此被忽略，不会并发下载两份或启动两次安装程序。DOWNLOADING、READY、INSTALLING
状态下的检查命令被忽略，但仍重新发布一次当前状态，界面不会停在“正在检查”。stop 取消模块持有的全部任务。

下载目录的清理：启动时删除临时文件与版本不高于当前版本的安装包；每次检查成功后只保留当前可用版本的
安装包，已被更高版本取代的安装包随之删除。

自动检查：启动后等 startup_delay 秒检查一次，之后每隔 ``[updates].check_interval_hours`` 小时检查一次。
最近一次检查的时间记在 record_file 里，重启后按记录计算下一次到期时间，间隔内不重复检查。
自动检查发现新版本时发布一次 ``NotificationRequested``，同一版本只通知一次（也记在 record_file 里）。

线程：模块的状态只在事件循环线程上读写。读写文件、计算整份文件的 SHA-256、启动进程都放进线程池，
网络用 httpx 的异步流式接口。整个过程不需要 GitHub 认证，请求里没有任何令牌。
"""

import asyncio
import hashlib
import json
import logging
import os
import subprocess
import time
from collections.abc import Awaitable, Callable, Coroutine
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Final

import httpx
from pydantic import ValidationError

from lst.contracts.automation import BuildFlavor
from lst.contracts.config import AppConfig
from lst.contracts.events import NoticeLevel, NotificationRequested, QuitRequested
from lst.contracts.updates import (
    CheckForUpdates,
    DownloadUpdate,
    InstallUpdate,
    UpdateErrorCode,
    UpdateState,
    UpdateStatus,
)
from lst.kernel import ModuleContext
from lst.services._shared.aio import blocking
from lst.services._shared.download import content_length, file_sha256, open_for_write, stream_to_file
from lst.services.updates.config import UpdatesConfig
from lst.services.updates.launcher import INSTALLER_ARGS, Launcher, launch_detached
from lst.services.updates.net import (
    DOWNLOAD_TIMEOUT,
    GITHUB_ACCEPT,
    RELEASES_TIMEOUT,
    classify_transport_error,
    describe_error,
    safe_url,
    user_agent,
)
from lst.services.updates.releases import (
    DEFAULT_RELEASES_URL,
    Candidate,
    ReleaseFormatError,
    parse_checksum,
    parse_installer_name,
    parse_releases,
    select_update,
)
from lst.services.updates.store import UpdateRecord, load_record, save_record
from lst.services.updates.version import UNKNOWN_VERSION, Version, parse_version

STARTUP_DELAY_S: Final = 30.0
"""启动后等多久做第一次自动检查。避开启动时加载模型、启动推理进程的高峰。"""

MAX_SLEEP_S: Final = 3600.0
"""自动检查的调度循环最长睡这么久就重新计算一次到期时间，系统睡眠、改时钟之后最多晚一小时发现到期。"""

RETRY_AFTER: Final = timedelta(hours=1)
"""一次检查（无论成败）之后至少隔这么久才再自动检查。网络不通时不会每次循环都发请求。"""

PROGRESS_INTERVAL_S: Final = 0.2
"""下载进度的最短发布间隔。"""

WRITE_CHUNK_BYTES: Final = 1 << 20
"""攒够这么多数据再交给线程写盘并更新摘要，减少线程切换。"""

RELEASES_PER_PAGE: Final = 30
"""版本列表只取第一页。GitHub 按创建时间倒序返回，最新的版本一定在第一页。"""

MAX_RELEASES_BYTES: Final = 4 * 1024 * 1024
"""版本列表的大小上限。每个版本带发布说明，30 个版本也只有几百 KB。"""

MAX_CHECKSUM_BYTES: Final = 4096
"""校验文件的大小上限。sha256sum 的一行不到 200 字节。"""

NOTICE_TITLE_KEY: Final = "update.notify.available.title"
NOTICE_MESSAGE_KEY: Final = "update.notify.available.message"
NOTICE_MESSAGE_SOURCE_KEY: Final = "update.notify.available_source.message"
"""自动检查发现新版本时的通知文案键，文案登记在 ui.tray.strings。源码运行时正文换成 _SOURCE 那一条。"""

PART_SUFFIX: Final = ".part"

_HTTP_OK: Final = 200
_BLOCKING_STATES: Final = frozenset({UpdateState.DOWNLOADING, UpdateState.READY, UpdateState.INSTALLING})
"""这几个状态下不检查新版本：检查结果会覆盖下载进度或已就绪的安装包。"""


class _Failure(Exception):
    """检查或下载失败。code 是原因分类，detail 是技术细节（地址已脱敏）。"""

    def __init__(self, code: UpdateErrorCode, detail: str) -> None:
        super().__init__(detail)
        self.code = code
        self.detail = detail


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _file_sha256(path: Path) -> str | None:
    """整份文件的 SHA-256；文件不存在时返回 None。"""
    return file_sha256(path) if path.is_file() else None


def _cleanup_downloads(
    directory: Path, current: Version, logger: logging.Logger, *, keep: str | bool | None = True
) -> int:
    """删除下载目录里的临时文件与用不上的安装包，返回删除的文件数。

    keep 决定保留哪些安装包。启动时还不知道哪个版本可用，取默认值 True：只删版本不高于当前版本的
    安装包（已经装过了）。检查成功后传入当前可用版本的安装包文件名，没有可用版本时传 None，
    其余安装包（例如已被更高版本取代的安装包）一律删除。版本号无法解析的同名文件不动。
    """
    if not directory.is_dir():
        return 0
    removed = 0
    for path in directory.iterdir():
        if not path.is_file():
            continue
        obsolete = path.name.endswith(PART_SUFFIX)
        parsed = parse_installer_name(path.name)
        if parsed is not None:
            version = parse_version(parsed[0])
            if version is None:
                obsolete = False
            elif keep is True:
                obsolete = version <= current
            else:
                obsolete = path.name != keep
        if not obsolete:
            continue
        try:
            path.unlink()
        except OSError:
            logger.warning("无法删除更新下载目录里的旧文件 %s", path.name, exc_info=True)
        else:
            removed += 1
    return removed


class UpdatesModule:
    """应用更新服务，实现 kernel 的 Module 协议。

    构造参数：
        client：共用的 httpx 客户端，测试注入 MockTransport；
        current_version：正在运行的版本号（version.running_version 读取）；
        can_install：能否自动安装，安装版为真，源码运行为假；
        record_file：检查时间与已通知版本的记录文件，例如 ``<数据目录>/updates.json``；
        download_dir：安装包的下载目录，例如 ``<数据目录>/tmp/updates``；
        releases_url：GitHub 版本列表 API 的地址；
        flavor：正在运行的构建类型，只下载同一种构建类型的安装包（开发版是 ``-dev-setup.exe``）；
        launcher：启动安装程序的函数，测试注入记录调用的假函数；
        startup_delay：启动后第一次自动检查之前等待的秒数；
        sleep：异步等待函数，测试注入由测试控制何时返回的假函数；
        now：当前时间（带时区），测试注入假时钟检查自动检查的节奏；
        clock：单调时钟，用于下载进度节流；
        progress_interval：下载进度的最短发布间隔，单位秒；
        write_chunk_bytes：攒够多少字节交给线程写盘一次。
    """

    name = "updates"

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        current_version: str,
        can_install: bool,
        record_file: Path,
        download_dir: Path,
        releases_url: str = DEFAULT_RELEASES_URL,
        flavor: BuildFlavor = BuildFlavor.NORMAL,
        launcher: Launcher = launch_detached,
        startup_delay: float = STARTUP_DELAY_S,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
        now: Callable[[], datetime] = _utc_now,
        clock: Callable[[], float] = time.monotonic,
        progress_interval: float = PROGRESS_INTERVAL_S,
        write_chunk_bytes: int = WRITE_CHUNK_BYTES,
    ) -> None:
        self._client = client
        self._current_text = current_version
        self._can_install = can_install
        self._record_file = Path(record_file)
        self._download_dir = Path(download_dir)
        self._releases_url = releases_url
        self._flavor = flavor
        self._launcher = launcher
        self._startup_delay = max(0.0, startup_delay)
        self._sleep = sleep
        self._now = now
        self._clock = clock
        self._interval = progress_interval
        self._write_chunk = max(1, write_chunk_bytes)
        self._logger = logging.getLogger(__name__)
        self._ctx: ModuleContext | None = None
        self._current = parse_version(current_version) or Version((0, 0, 0, 1, ()), UNKNOWN_VERSION)
        self._config = UpdatesConfig()
        self._record = UpdateRecord()
        self._state = UpdateState.IDLE
        self._available: Candidate | None = None
        self._progress: float | None = None
        self._error_code: UpdateErrorCode | None = None
        self._ready_path: Path | None = None
        self._manual = False
        self._launching = False
        self._tasks: set[asyncio.Task[None]] = set()
        self._check_task: asyncio.Task[None] | None = None
        self._download_task: asyncio.Task[None] | None = None
        self._scheduler: asyncio.Task[None] | None = None
        self._startup_done = False
        self._closed = False

    # ------------------------------------------------------------------ 生命周期

    async def start(self, ctx: ModuleContext) -> None:
        """读取记录、清理下载目录、订阅消息、发布初始状态，然后开始自动检查的调度。"""
        if self._ctx is not None:
            raise RuntimeError("更新模块已经启动")
        self._ctx = ctx
        self._logger = ctx.logger
        if parse_version(self._current_text) is None:
            self._logger.warning(
                "当前版本号 %r 不是语义化版本号，按 %s 比较", self._current_text, UNKNOWN_VERSION
            )
        self._record = await blocking(load_record, self._record_file, self._logger)
        removed = await blocking(_cleanup_downloads, self._download_dir, self._current, self._logger)
        if removed:
            self._logger.info("已删除更新下载目录里的 %d 个旧文件", removed)
        config = ctx.bus.latest(AppConfig)
        if config is not None:
            self._config = self._parse(config)
        ctx.subscribe(AppConfig, self._on_config)
        ctx.subscribe(CheckForUpdates, self._on_check)
        ctx.subscribe(DownloadUpdate, self._on_download)
        ctx.subscribe(InstallUpdate, self._on_install)
        self._logger.info(
            "当前版本 %s，%s",
            self._current_text,
            "安装版，可以自动安装" if self._can_install else "源码运行，只检查",
        )
        self._publish()
        self._scheduler = self._spawn(self._schedule(self._startup_delay), "updates:schedule")

    async def stop(self) -> None:
        """取消模块持有的全部在途任务：调度、检查与下载（删除临时文件）。重复调用没有效果。"""
        self._closed = True
        tasks = [task for task in self._tasks if not task.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------ 状态

    def status(self) -> UpdateStatus:
        """当前状态的快照。"""
        last = self._record.last_checked
        return UpdateStatus(
            state=self._state,
            current_version=self._current_text,
            available=self._available.info if self._available is not None else None,
            progress=self._progress,
            last_checked=last.isoformat() if last is not None else None,
            error_code=self._error_code,
            can_install=self._can_install,
        )

    def _publish(self) -> None:
        if self._ctx is None or self._closed:
            return
        self._ctx.bus.publish(self.status())

    def _set(
        self, state: UpdateState, *, error: UpdateErrorCode | None = None, progress: float | None = None
    ) -> None:
        self._state, self._error_code, self._progress = state, error, progress
        self._publish()

    def _spawn(self, coro: Coroutine[Any, Any, None], name: str) -> asyncio.Task[None]:
        task = asyncio.create_task(coro, name=name)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(self._log_task_failure)
        return task

    def _log_task_failure(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        error = task.exception()
        if error is not None:
            self._logger.error("后台任务 %s 意外结束", task.get_name(), exc_info=error)

    async def _save_record(self) -> None:
        try:
            await blocking(save_record, self._record_file, self._record)
        except OSError:
            self._logger.warning("无法写入更新记录 %s", self._record_file, exc_info=True)

    def _headers(self, *, api: bool) -> dict[str, str]:
        headers = {"User-Agent": user_agent(self._current_text)}
        if api:
            headers["Accept"] = GITHUB_ACCEPT
        return headers

    # ------------------------------------------------------------------ 配置

    def _parse(self, config: AppConfig) -> UpdatesConfig:
        """解析 [updates] 节：缺失时用默认值；解析失败（组合根没有注册这一节）时记录错误并沿用当前值。"""
        raw = config.section("updates")
        if raw is None:
            return UpdatesConfig()
        try:
            return UpdatesConfig.model_validate(raw)
        except ValidationError:
            self._logger.exception("配置节 [updates] 无法解析，沿用当前值")
            return self._config

    def _on_config(self, message: AppConfig) -> None:
        new = self._parse(message)
        old, self._config = self._config, new
        schedule_changed = (old.auto_check, old.check_interval_hours) != (
            new.auto_check,
            new.check_interval_hours,
        )
        if schedule_changed and self._startup_done and not self._closed:
            # 启动延迟已经过去：按新配置立即重新计算到期时间。延迟期间调度任务醒来后自然读到新配置。
            scheduler = self._scheduler
            if scheduler is not None and not scheduler.done():
                scheduler.cancel()
            self._scheduler = self._spawn(self._schedule(0.0), "updates:schedule")

    # ------------------------------------------------------------------ 自动检查的调度

    def _due_time(self, now: datetime) -> datetime:
        """下一次自动检查的到期时间。记录里的时间晚于当前时间（时钟被调回）时视为没有记录。"""
        due = now
        last_checked = self._record.last_checked
        if last_checked is not None and last_checked <= now:
            due = last_checked + timedelta(hours=self._config.check_interval_hours)
        last_attempt = self._record.last_attempt
        if last_attempt is not None and last_attempt <= now:
            due = max(due, last_attempt + RETRY_AFTER)
        return due

    def _next_wait(self) -> float:
        """距离下一次自动检查的秒数，不超过 MAX_SLEEP_S；不大于 0 表示已经到期。"""
        if not self._config.auto_check or self._state in _BLOCKING_STATES:
            return MAX_SLEEP_S
        now = self._now()
        return min((self._due_time(now) - now).total_seconds(), MAX_SLEEP_S)

    async def _schedule(self, initial_delay: float) -> None:
        if initial_delay > 0:
            await self._sleep(initial_delay)
        self._startup_done = True
        while not self._closed:
            wait = self._next_wait()
            if wait <= 0:
                await self._request_check(manual=False)
                continue
            await self._sleep(wait)

    # ------------------------------------------------------------------ 检查

    async def _on_check(self, command: CheckForUpdates) -> None:
        await self._request_check(manual=command.manual)

    async def _request_check(self, *, manual: bool) -> None:
        """开始一次检查并等它结束。已经在检查时不重复请求；手动请求把正在进行的检查升级为手动检查。"""
        if self._closed:
            return
        task = self._check_task
        if task is None or task.done():
            if self._state in _BLOCKING_STATES:
                self._logger.info("更新状态为 %s，忽略检查请求", self._state.value)
                # 界面发出检查命令后在等新的状态，忽略时也重新发布当前状态，界面不会停在“正在检查”。
                self._publish()
                return
            self._manual = manual
            task = self._check_task = self._spawn(self._check(), "updates:check")
        elif manual:
            self._manual = True
        await asyncio.wait({task})

    async def _check(self) -> None:
        previous = (self._state, self._error_code, self._progress)
        self._set(UpdateState.CHECKING)
        attempt = self._now()
        failure: _Failure | None = None
        candidates: list[Candidate] = []
        try:
            candidates = await self._fetch_releases()
        except _Failure as exc:
            failure = exc
        except Exception as exc:
            self._logger.exception("检查更新时出现未预期的错误")
            failure = _Failure(UpdateErrorCode.INTERNAL, describe_error(exc))
        manual, self._manual = self._manual, False
        if self._closed:
            return
        if failure is not None:
            self._record = UpdateRecord(self._record.last_checked, attempt, self._record.notified_version)
            await self._save_record()
            if manual:
                self._logger.warning(
                    "检查更新失败（%s，%s）：%s",
                    failure.code.value,
                    safe_url(self._releases_url),
                    failure.detail,
                )
                self._set(UpdateState.ERROR, error=failure.code)
            else:
                self._logger.info(
                    "自动检查更新失败（%s，%s）：%s",
                    failure.code.value,
                    safe_url(self._releases_url),
                    failure.detail,
                )
                state, error, progress = previous
                self._set(state, error=error, progress=progress)
            return
        best = select_update(candidates, self._current, include_prerelease=self._config.include_prerelease)
        notified = self._record.notified_version
        notify = False
        if best is not None and best.version.text != notified:
            notify = not manual  # 手动检查的结果由界面提示，这里只记下“已经告诉过用户”
            notified = best.version.text
        self._record = UpdateRecord(attempt, attempt, notified)
        await self._save_record()
        if self._closed:
            return
        self._available = best
        keep = best.installer_name if best is not None else None
        removed = await blocking(
            _cleanup_downloads, self._download_dir, self._current, self._logger, keep=keep
        )
        if removed:
            self._logger.info("已删除更新下载目录里的 %d 个过期文件", removed)
        if self._closed:
            return
        if best is None:
            self._logger.info(
                "检查更新：当前版本 %s 已是最新（共 %d 个可安装版本）", self._current_text, len(candidates)
            )
            self._set(UpdateState.UP_TO_DATE)
            return
        self._logger.info("检查更新：发现新版本 %s（当前 %s）", best.version, self._current_text)
        self._set(UpdateState.AVAILABLE)
        if notify and self._ctx is not None:
            message_key = NOTICE_MESSAGE_KEY if self._can_install else NOTICE_MESSAGE_SOURCE_KEY
            self._ctx.bus.publish(
                NotificationRequested(
                    NOTICE_TITLE_KEY, message_key, NoticeLevel.INFO, {"version": best.version.text}
                )
            )

    async def _read_limited(self, url: str, limit: int, *, api: bool, timeout: httpx.Timeout) -> bytes:
        """GET 一个小文件并读完响应体，超过 limit 字节时按格式错误处理。"""
        body = bytearray()
        try:
            async with self._client.stream(
                "GET",
                url,
                params={"per_page": RELEASES_PER_PAGE} if api else None,
                headers=self._headers(api=api),
                timeout=timeout,
                follow_redirects=True,
            ) as response:
                if response.status_code != _HTTP_OK:
                    raise _Failure(UpdateErrorCode.HTTP, f"HTTP {response.status_code}")
                async for chunk in response.aiter_bytes():
                    body += chunk
                    if len(body) > limit:
                        raise _Failure(UpdateErrorCode.BAD_RELEASE, "响应大小超出上限")
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise _Failure(classify_transport_error(exc), describe_error(exc)) from exc
        return bytes(body)

    async def _fetch_releases(self) -> list[Candidate]:
        body = await self._read_limited(
            self._releases_url, MAX_RELEASES_BYTES, api=True, timeout=RELEASES_TIMEOUT
        )
        try:
            data: object = json.loads(body)
        except ValueError as exc:
            raise _Failure(UpdateErrorCode.BAD_RELEASE, "版本列表不是合法的 JSON") from exc
        try:
            return parse_releases(data, self._logger, self._flavor)
        except ReleaseFormatError as exc:
            raise _Failure(UpdateErrorCode.BAD_RELEASE, str(exc)) from exc

    # ------------------------------------------------------------------ 下载

    async def _on_download(self, _command: DownloadUpdate) -> None:
        if self._closed:
            return
        if not self._can_install:
            self._logger.warning("源码运行，不能下载安装包")
            self._set(UpdateState.ERROR, error=UpdateErrorCode.NOT_INSTALLED)
            return
        candidate = self._available
        if candidate is None:
            self._logger.warning("还没有发现新版本，忽略下载命令")
            return
        if self._state in (
            UpdateState.CHECKING,
            UpdateState.DOWNLOADING,
            UpdateState.READY,
            UpdateState.INSTALLING,
        ):
            self._logger.info("更新状态为 %s，忽略下载命令", self._state.value)
            return
        # 在第一个 await 之前同步进入 DOWNLOADING：紧接着到达的第二条下载命令看到这个状态就会被忽略。
        self._set(UpdateState.DOWNLOADING, progress=0.0)
        task = self._download_task = self._spawn(self._download(candidate), "updates:download")
        # 只有 stop 会取消下载任务，那时模块已经关闭，不再发布状态。
        await asyncio.wait({task})

    async def _download(self, candidate: Candidate) -> None:
        """下载并校验安装包。失败只记录在状态里，不抛出；取消时删除临时文件后把取消传出去。"""
        final = self._download_dir / candidate.installer_name
        part = final.with_name(final.name + PART_SUFFIX)
        self._logger.info("开始下载更新 %s：%s", candidate.version, safe_url(candidate.installer_url))
        try:
            expected = await self._fetch_checksum(candidate)
            existing = await blocking(_file_sha256, final)
            if existing == expected:
                self._logger.info("下载目录里已有通过校验的安装包 %s，不重新下载", final.name)
            else:
                digest = await self._fetch_installer(candidate, part)
                if digest != expected:
                    raise _Failure(UpdateErrorCode.CHECKSUM, f"SHA-256 为 {digest}，校验文件中为 {expected}")
                await blocking(os.replace, part, final)
        except asyncio.CancelledError:
            await self._discard(part)
            self._logger.info("已取消更新 %s 的下载", candidate.version)
            raise
        except _Failure as failure:
            await self._fail_download(part, failure.code, failure.detail)
            return
        except OSError as exc:
            await self._fail_download(part, UpdateErrorCode.DISK, describe_error(exc))
            return
        except Exception as exc:
            self._logger.exception("下载更新时出现未预期的错误")
            await self._fail_download(part, UpdateErrorCode.INTERNAL, describe_error(exc))
            return
        self._ready_path = final
        self._logger.info("更新 %s 已下载并通过校验：%s", candidate.version, final)
        self._set(UpdateState.READY, progress=1.0)

    async def _fetch_checksum(self, candidate: Candidate) -> str:
        body = await self._read_limited(
            candidate.checksum_url, MAX_CHECKSUM_BYTES, api=False, timeout=RELEASES_TIMEOUT
        )
        try:
            text = body.decode("utf-8-sig")
        except UnicodeDecodeError as exc:
            raise _Failure(UpdateErrorCode.BAD_RELEASE, "校验文件不是 UTF-8 文本") from exc
        try:
            return parse_checksum(text, candidate.installer_name)
        except ReleaseFormatError as exc:
            raise _Failure(UpdateErrorCode.BAD_RELEASE, str(exc)) from exc

    async def _fetch_installer(self, candidate: Candidate, part: Path) -> str:
        """把安装包流式写入临时文件，返回它的 SHA-256。"""
        sink = await blocking(open_for_write, part)
        digest = hashlib.sha256()
        try:
            async with self._client.stream(
                "GET",
                candidate.installer_url,
                headers=self._headers(api=False),
                timeout=DOWNLOAD_TIMEOUT,
                follow_redirects=True,
            ) as response:
                if response.status_code != _HTTP_OK:
                    raise _Failure(UpdateErrorCode.HTTP, f"HTTP {response.status_code}")
                total = content_length(response) or candidate.info.installer_bytes or None
                last = self._clock()

                def report(done: int) -> None:
                    nonlocal last
                    now = self._clock()
                    if now - last >= self._interval:
                        last = now
                        fraction = min(done / total, 1.0) if total else None
                        self._set(UpdateState.DOWNLOADING, progress=fraction)

                await stream_to_file(
                    response, sink, chunk_bytes=self._write_chunk, on_progress=report, digest=digest
                )
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            raise _Failure(classify_transport_error(exc), describe_error(exc)) from exc
        finally:
            await blocking(sink.close)
        return digest.hexdigest()

    async def _fail_download(self, part: Path, code: UpdateErrorCode, detail: str) -> None:
        await self._discard(part)
        self._logger.warning("下载更新失败（%s）：%s", code.value, detail)
        self._set(UpdateState.ERROR, error=code)

    async def _discard(self, part: Path) -> None:
        try:
            await blocking(part.unlink, missing_ok=True)
        except OSError:
            self._logger.warning("无法删除临时文件 %s", part.name, exc_info=True)

    # ------------------------------------------------------------------ 安装

    async def _on_install(self, _command: InstallUpdate) -> None:
        if self._closed:
            return
        if not self._can_install:
            self._logger.warning("源码运行，不能自动安装")
            self._set(UpdateState.ERROR, error=UpdateErrorCode.NOT_INSTALLED)
            return
        path = self._ready_path
        if self._state is not UpdateState.READY or path is None:
            self._logger.warning("安装包还没有就绪（状态 %s），忽略安装命令", self._state.value)
            return
        if self._launching:
            self._logger.info("安装程序正在启动，忽略重复的安装命令")
            return
        # 启动安装程序要进线程池；在 await 之前同步设标志，紧接着到达的第二条安装命令不会再启动一次。
        self._launching = True
        self._logger.info("启动安装程序 %s %s", path.name, " ".join(INSTALLER_ARGS))
        try:
            await blocking(self._launcher, path, INSTALLER_ARGS)
        except (OSError, ValueError, subprocess.SubprocessError) as exc:
            self._logger.warning("无法启动安装程序 %s：%s", path.name, describe_error(exc))
            self._set(UpdateState.ERROR, error=UpdateErrorCode.LAUNCH_FAILED)
            return
        finally:
            self._launching = False
        self._set(UpdateState.INSTALLING)
        if self._ctx is not None and not self._closed:
            self._ctx.bus.publish(QuitRequested())
