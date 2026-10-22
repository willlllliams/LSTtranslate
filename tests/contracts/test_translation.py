"""翻译契约中与附带截图相关的部分：JobImage、Job.image、EngineInfo.image_max_side（需求 ENG-07）。"""

import hashlib

import pytest

from lst.contracts.ids import EngineId, Lang, SegmentId, SessionId
from lst.contracts.translation import PNG_MEDIA_TYPE, EngineInfo, EngineKind, Job, JobImage, Segment

PIXELS = b"\x89PNG-fake-image-bytes-SECRET"


def test_from_encoded_computes_digest() -> None:
    image = JobImage.from_encoded(PNG_MEDIA_TYPE, PIXELS, 10, 5)
    assert image.sha256 == hashlib.sha256(PIXELS).hexdigest()
    assert (image.media_type, image.width, image.height, image.data) == ("image/png", 10, 5, PIXELS)


def test_repr_hides_image_bytes() -> None:
    image = JobImage.from_encoded(PNG_MEDIA_TYPE, PIXELS, 10, 5)
    job = Job(
        SessionId("s"), EngineId("e"), (Segment(SegmentId("a"), "x"),), Lang("en"), Lang("ja"), image=image
    )
    for text in (repr(image), repr(job)):
        assert "SECRET" not in text
        assert image.sha256 in text


@pytest.mark.parametrize(
    ("data", "width", "height", "digest"),
    [
        (PIXELS, 0, 5, None),
        (PIXELS, 10, -1, None),
        (b"", 10, 5, None),
        (PIXELS, 10, 5, "abc"),
    ],
)
def test_rejects_invalid_image(data: bytes, width: int, height: int, digest: str | None) -> None:
    with pytest.raises(ValueError):
        if digest is None:
            JobImage.from_encoded(PNG_MEDIA_TYPE, data, width, height)
        else:
            JobImage(PNG_MEDIA_TYPE, width, height, digest, data)


def test_job_has_no_image_by_default() -> None:
    job = Job(SessionId("s"), EngineId("e"), (), Lang("en"), Lang("ja"))
    assert job.image is None


def info(image_max_side: int | None = None) -> EngineInfo:
    return EngineInfo(
        id=EngineId("e"),
        name="e",
        kind=EngineKind.ONLINE_LLM,
        priority=1,
        billing=True,
        image_max_side=image_max_side,
    )


def test_engine_info_image_max_side() -> None:
    assert info().image_max_side is None
    assert info(1568).image_max_side == 1568
    with pytest.raises(ValueError):
        info(0)
