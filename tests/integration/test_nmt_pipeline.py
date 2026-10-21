"""M3 集成测试：真实监管器 + 真实 NMT 子进程 + 本地 NMT 引擎 + 调度器。

- 真实翻译：用本机的两个 Argos 模型（assets/argosmodel，缺失时跳过）完成英→中、中→英。
- 崩溃恢复：假后端让 NMT 子进程在翻译中途退出，本地 NMT 的尝试记为推理进程崩溃，另一个引擎照常胜出；
  监管器重启进程后，下一个会话里本地 NMT 恢复并胜出（架构文档 8.2 节第 1、4、5 条）。
"""

import asyncio
import sys
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from lst.app.supervisor import SupervisorTimings, WorkerSupervisor
from lst.contracts.errors import ErrorCode
from lst.contracts.ids import EngineId, Entry, Lang, SegmentId, SessionId
from lst.contracts.ports import WorkerState
from lst.contracts.translation import (
    AttemptState,
    AttemptView,
    EngineInfo,
    EngineKind,
    Job,
    Segment,
    SegmentOutcome,
    SegmentView,
)
from lst.services.engines.local_nmt import LocalNmtSettings, create_local_nmt_engine
from lst.services.race import RaceScheduler

ROOT = Path(__file__).resolve().parents[2]
MODELS = ROOT / "assets" / "argosmodel"
EXTRACTED = ROOT / "data" / "models" / "nmt" / ".extracted"
EN, ZH = Lang("en"), Lang("zh-Hans")
ARGOS = EngineId("argos")
BACKUP = EngineId("backup")

FAST = SupervisorTimings(accept_timeout_s=30.0, ready_timeout_s=120.0, backoff_s=(0.05,))


class Sink:
    def __init__(self) -> None:
        self.views: dict[SegmentId, SegmentView] = {}

    def segments_updated(self, session_id: SessionId, views: tuple[SegmentView, ...]) -> None:
        for view in views:
            self.views[view.segment_id] = view

    def session_progress(self, session_id: SessionId, finished: int, total: int) -> None:
        pass

    def attempt_recorded(self, session_id: SessionId, segment_id: SegmentId, attempt: AttemptView) -> None:
        pass


class BackupEngine:
    """进程内的备用引擎，优先级低于本地 NMT。"""

    def __init__(self) -> None:
        self._info = EngineInfo(
            id=BACKUP, name="backup", kind=EngineKind.ONLINE_MT, priority=2, billing=False, timeout_s=10
        )

    @property
    def info(self) -> EngineInfo:
        return self._info

    def supports(self, source: Lang, target: Lang) -> bool:
        return source != target

    async def translate(self, job: Job) -> AsyncIterator[SegmentOutcome]:
        for segment in job.segments:
            await asyncio.sleep(0)
            yield SegmentOutcome(segment.id, text=f"backup:{segment.text}")


def nmt_supervisor(settings: dict[str, object], backend: str | None = None) -> WorkerSupervisor:
    command = [sys.executable, "-m", "lst.workers.nmt_worker", "--log-level", "WARNING"]
    if backend is not None:
        command += ["--backend", backend]
    return WorkerSupervisor("nmt-it", command, settings=settings, cwd=ROOT, timings=FAST)  # type: ignore[arg-type]


async def translate(
    scheduler: RaceScheduler, sink: Sink, session: str, texts: list[str], pair: tuple[Lang, Lang]
) -> list[SegmentView]:
    segments = [Segment(SegmentId(f"{session}-{i}"), text) for i, text in enumerate(texts)]
    await scheduler.start_session(
        Entry.SCREENSHOT, SessionId(session), segments, {s.id: pair for s in segments}
    )
    await scheduler.drain()
    return [sink.views[s.id] for s in segments]


@pytest.mark.skipif(not (MODELS / "translate-en_zh-1_9.argosmodel").is_file(), reason="本机没有 Argos 模型")
async def test_real_models_through_supervisor_and_race() -> None:
    supervisor = nmt_supervisor({"model_dir": str(MODELS), "extract_dir": str(EXTRACTED), "device": "cpu"})
    await supervisor.start()
    try:
        assert await supervisor.wait_for_state(WorkerState.READY, WorkerState.FAILED) is WorkerState.READY
        engine = create_local_nmt_engine(LocalNmtSettings(id="argos", name="Argos", priority=1), supervisor)
        assert engine.supports(EN, ZH) and engine.supports(ZH, EN)
        sink = Sink()
        scheduler = RaceScheduler([engine], sink, frame_interval=0.005)
        try:
            to_zh = await translate(
                scheduler, sink, "a", ["The weather is nice today.", "Thank you!"], (EN, ZH)
            )
            to_en = await translate(scheduler, sink, "b", ["今天天气很好。", "谢谢你！"], (ZH, EN))
        finally:
            await scheduler.aclose()
        for view in to_zh:
            assert (view.winner, view.finished) == (ARGOS, True)
            assert any("一" <= ch <= "鿿" for ch in view.display_text), view.display_text
        for view in to_en:
            assert (view.winner, view.finished) == (ARGOS, True)
            letters = [ch for ch in view.display_text if ch.isalpha()]
            assert letters and sum(ch.isascii() for ch in letters) / len(letters) > 0.9, view.display_text
    finally:
        await supervisor.stop()


async def test_crash_mid_race_backup_wins_then_worker_recovers() -> None:
    supervisor = nmt_supervisor({"exit_on": "CRASH"}, backend="tests.workers.fake_backend:FakeBackend")
    states: list[WorkerState] = []
    await supervisor.start()
    try:
        assert await supervisor.wait_for_state(WorkerState.READY, WorkerState.FAILED) is WorkerState.READY
        engine = create_local_nmt_engine(LocalNmtSettings(id="argos", name="Argos", priority=1), supervisor)
        sink = Sink()
        scheduler = RaceScheduler([engine, BackupEngine()], sink, frame_interval=0.005)
        try:
            before, crashed = await translate(scheduler, sink, "c", ["hello", "CRASH now"], (EN, ZH))
            # NMT 进程逐段发回结果：第一段在崩溃前已经返回，本地 NMT 优先级更高而胜出。
            assert (before.winner, before.display_text) == (ARGOS, "[en>zh-Hans]hello")
            # 第二段让进程退出：本地 NMT 的尝试记为推理进程崩溃，备用引擎胜出。
            assert crashed.winner == BACKUP
            nmt = next(a for a in crashed.attempts if a.engine_id == ARGOS)
            assert nmt.state is AttemptState.FAILED
            assert nmt.error_code is ErrorCode.WORKER_CRASHED

            states.append(await supervisor.wait_for_state(WorkerState.READY, WorkerState.FAILED))
            recovered = await translate(scheduler, sink, "d", ["hello again"], (EN, ZH))
            assert recovered[0].winner == ARGOS
            assert recovered[0].display_text == "[en>zh-Hans]hello again"
        finally:
            await scheduler.aclose()
    finally:
        await supervisor.stop()
    assert states == [WorkerState.READY]
