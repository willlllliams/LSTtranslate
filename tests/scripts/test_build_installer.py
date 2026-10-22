"""scripts/build_installer.py 的纯函数：版本清单整理、sha256 校验文件、uv 许可证的来源。"""

import hashlib
import re
import subprocess
from pathlib import Path

import pytest

from lst.contracts.ocr_engines import OcrEngineKind, OcrRuntimeVariant
from lst.services.ocr_engines.runtime import RESOLVE_MARKER, requires_hashes, requires_resolution
from scripts import build_installer as build

FROZEN = """\
Using Python 3.13.15 environment at: .venv-ocr
numpy==2.3.0
PaddlePaddle==3.3.1
opencv-contrib-python==4.10.0.84
# 注释
aiohttp==3.12.0
"""

# lst/services/updates/releases.py 解析校验文件用的正则。
_SHA256_LINE = re.compile(r"^([0-9a-fA-F]{64})\s+\*?(\S.*?)\s*$")


def test_pinned_requirements_sorted_and_normalized() -> None:
    frozen = "\n".join(line for line in FROZEN.splitlines() if "environment at" not in line)
    assert build.pinned_requirements(frozen) == (
        "aiohttp==3.12.0\nnumpy==2.3.0\nopencv-contrib-python==4.10.0.84\nPaddlePaddle==3.3.1\n"
    )


@pytest.mark.parametrize(
    "line",
    ["-e file:///C:/repo", "lst @ file:///C:/repo", "Using Python 3.13 environment", "numpy>=2"],
)
def test_pinned_requirements_rejects_unreproducible_lines(line: str) -> None:
    with pytest.raises(ValueError):
        build.pinned_requirements(f"numpy==2.3.0\n{line}\n")


def test_pinned_requirements_rejects_empty() -> None:
    with pytest.raises(ValueError):
        build.pinned_requirements("# nothing\n")


def test_pinned_requirements_rejects_two_opencv_packages() -> None:
    with pytest.raises(SystemExit):
        build.pinned_requirements("opencv-contrib-python==4.10.0.84\nopencv-python-headless==5.0.0\n")


def test_requirements_header_mentions_hash_mode() -> None:
    assert "--require-hashes" in build.requirements_header(True)
    assert "--allow-unhashed" in build.requirements_header(False)
    assert all(line.startswith("#") for line in build.requirements_header(False).splitlines())


def test_sha256_file_is_sha256sum_format(tmp_path: Path) -> None:
    installer = tmp_path / "LSTtranslate-0.2.0-setup.exe"
    installer.write_bytes(b"MZ" + bytes(range(256)) * 10)
    checksum = build.write_sha256_file(installer)
    assert checksum.name == "LSTtranslate-0.2.0-setup.exe.sha256"
    raw = checksum.read_bytes()
    assert raw.endswith(b"\n") and b"\r" not in raw
    expected = hashlib.sha256(installer.read_bytes()).hexdigest()
    assert raw.decode("utf-8") == f"{expected}  {installer.name}\n"
    match = _SHA256_LINE.match(raw.decode("utf-8").strip())
    assert match is not None and match.group(1) == expected and match.group(2) == installer.name


def test_uv_license_dir_prefers_files_next_to_uv(tmp_path: Path) -> None:
    uv = tmp_path / "bin" / "uv.exe"
    uv.parent.mkdir()
    uv.write_bytes(b"MZ")
    assert build.uv_license_dir(uv) == build.UV_LICENSE_FALLBACK
    (uv.parent / "LICENSE-MIT").write_text("MIT", encoding="utf-8")
    assert build.uv_license_dir(uv) == uv.parent


def test_repository_keeps_uv_license() -> None:
    text = (build.UV_LICENSE_FALLBACK / "LICENSE-MIT").read_text(encoding="utf-8")
    assert text.startswith("MIT License") and "Astral" in text


# ---------------------------------------------------------------------- 版本号


@pytest.mark.parametrize("text", ["0.2.0", "0.2.0-beta.1", "1.10.3-rc.2"])
def test_checked_version_accepts_semver(text: str) -> None:
    assert build.checked_version(text) == text


@pytest.mark.parametrize("text", ["0.2.0b1", "0.2.0.dev3", "v0.2.0", "0.2.0+build.5", "1.0", " 0.2.0"])
def test_checked_version_rejects_what_updates_cannot_match(text: str) -> None:
    # 安装包名里的版本号必须是更新服务 parse_version 认得、且原样就是规范写法的 SemVer，
    # 否则 GitHub 上的附件名与更新服务按 tag 拼出的文件名对不上，应用内更新找不到安装包。
    with pytest.raises(SystemExit):
        build.checked_version(text)


def test_build_validates_version_with_update_service_parser() -> None:
    from lst.services.updates.version import parse_version

    assert build.parse_version is parse_version


# ---------------------------------------------------------------------- 版本清单


def test_ocr_compile_args_use_wheels_only() -> None:
    args = build.ocr_compile_args(Path("pinned.txt"), Path("out.txt"))
    assert args[:3] == ["uv", "pip", "compile"]
    assert "--no-config" in args
    assert args[args.index("--only-binary") + 1] == ":all:"
    assert "--generate-hashes" in args and "--no-deps" in args


HASHED = """\
# 头部注释
numpy==2.3.0 \\
    --hash=sha256:aaaa \\
    --hash=sha256:bbbb
paddlepaddle==3.3.1 \\
    --hash=sha256:cccc
"""


def test_check_requirements_accepts_matching_mode() -> None:
    assert build.check_requirements(HASHED, hashed=True) == 2
    assert build.check_requirements("# x\nnumpy==2.3.0\npaddlepaddle==3.3.1\n", hashed=False) == 2


@pytest.mark.parametrize(
    ("text", "hashed"),
    [
        (HASHED + "opencv-python==4.10.0.84\n", True),  # 有一个包没有哈希，--require-hashes 时整体失败
        (HASHED, False),  # 声明不带哈希却带了：与运行时的安装参数不一致
        ("# 只有注释\n", False),
    ],
)
def test_check_requirements_rejects_mismatch(text: str, hashed: bool) -> None:
    with pytest.raises(SystemExit):
        build.check_requirements(text, hashed=hashed)


# ---------------------------------------------------------------------- 自检


def test_smoke_imports_cover_windows_ocr() -> None:
    assert "winrt.windows.media.ocr" in build.SMOKE_IMPORTS
    assert "lst.workers.ocr_worker.winocr_backend" in build.SMOKE_IMPORTS


def test_smoke_code_checks_keyring_backend(tmp_path: Path) -> None:
    code = build.smoke_code(tmp_path / "app", tmp_path / "site" / "main", ("json",))
    assert "WinVaultKeyring" in code
    compile(code, "<smoke>", "exec")


def test_launcher_smoke_commands_need_no_network() -> None:
    commands = build.launcher_smoke_commands()
    assert ["ocr", "--ocr-site", "main"] in commands
    assert ["nmt"] in commands
    # 不带 --address 时推理进程在解析参数时以 CONFIG_ERROR 退出，此前已经按安装布局导入了进程入口。
    assert all("--address" not in command for command in commands)
    assert build.LAUNCHER_SMOKE_EXIT == 66


def test_shared_helpers_are_used_for_hashing(tmp_path: Path) -> None:
    from lst.services._shared.download import file_sha256

    path = tmp_path / "a.bin"
    path.write_bytes(b"x" * 10)
    assert build.sha256_line(path) == f"{file_sha256(path)}  a.bin\n"
    assert build.dir_size(tmp_path) == 10


# ---------------------------------------------------------------------- RapidOCR 的版本清单


def write_distribution(site: Path, name: str, version: str, requires: tuple[str, ...] = ()) -> None:
    """在 site 下放一个只有 METADATA 的 dist-info，importlib.metadata 能读到它。"""
    info = site / f"{name.replace('-', '_')}-{version}.dist-info"
    info.mkdir(parents=True)
    lines = ["Metadata-Version: 2.1", f"Name: {name}", f"Version: {version}"]
    lines += [f"Requires-Dist: {requirement}" for requirement in requires]
    (info / "METADATA").write_text("\n".join(lines) + "\n", encoding="utf-8")


@pytest.fixture
def fake_site(tmp_path: Path) -> Path:
    """仿照 .venv-ocr：rapidocr 依赖 headless 版 OpenCV，环境里只有 contrib 版；
    另有与 RapidOCR 无关的 paddle。"""
    site = tmp_path / "site-packages"
    write_distribution(
        site,
        "rapidocr",
        "3.10.0",
        (
            "numpy<3.0.0,>=1.19.5",
            "opencv-python-headless>=4.5.1.48",
            "Shapely!=2.0.4,>=1.7.1",
            "colorlog",
            'python-bidi<1.0,>=0.4.2; extra == "rtl"',
        ),
    )
    write_distribution(
        site, "onnxruntime", "1.31.0", ("numpy>=1.21.6", "packaging", 'sympy; extra == "symbolic"')
    )
    write_distribution(site, "numpy", "2.3.5")
    write_distribution(site, "opencv-contrib-python", "4.10.0.84", ("numpy>=1.21.2",))
    write_distribution(site, "shapely", "2.2.0", ("numpy>=1.21",))
    write_distribution(
        site, "colorlog", "6.12.0", ('colorama; sys_platform == "win32"', 'pytest; extra == "dev"')
    )
    write_distribution(site, "colorama", "0.4.6")
    write_distribution(site, "packaging", "26.3")
    write_distribution(site, "paddlepaddle", "3.3.1", ("numpy>=1.21",))
    write_distribution(site, "python-bidi", "0.6.0")
    return site


def test_rapid_requirements_are_the_dependency_closure(fake_site: Path) -> None:
    installed = build.installed_distributions(fake_site)
    text = build.rapid_pinned_requirements(installed)
    assert text == (
        "colorama==0.4.6\ncolorlog==6.12.0\nnumpy==2.3.5\nonnxruntime==1.31.0\n"
        "opencv-contrib-python==4.10.0.84\npackaging==26.3\nrapidocr==3.10.0\nshapely==2.2.0\n"
    )
    # 可选依赖（python-bidi、sympy）与无关的包（paddlepaddle）不在清单里；按平台求值的标记照常计入。
    assert "paddle" not in text and "bidi" not in text and "sympy" not in text
    assert build.check_requirements(build.requirements_header(False, "RapidOCR") + text, hashed=False) == 8


def test_rapid_requirements_fail_when_a_dependency_is_missing(fake_site: Path) -> None:
    installed = build.installed_distributions(fake_site)
    del installed["shapely"]
    with pytest.raises(ValueError, match="Shapely"):
        build.dependency_closure(installed, build.RAPID_ROOTS)
    with pytest.raises(ValueError, match="rapidocr"):
        build.dependency_closure({}, build.RAPID_ROOTS)


def test_rapid_requirements_reject_two_opencv_packages(fake_site: Path) -> None:
    write_distribution(fake_site, "opencv-python", "4.10.0.84")
    installed = build.installed_distributions(fake_site)
    with pytest.raises(ValueError, match="opencv-python-headless"):
        build.dependency_closure(installed, build.RAPID_ROOTS)


def test_requirements_header_names_the_engine() -> None:
    assert build.requirements_header(True, "RapidOCR").startswith("# RapidOCR 运行环境的版本清单")
    assert build.requirements_header(True).startswith("# PaddleOCR 运行环境的版本清单")


def test_requirement_file_names_match_the_engine_service() -> None:
    from lst.contracts.ocr_engines import OcrEngineKind, OcrRuntimeVariant
    from lst.services.ocr_engines import requirements_name

    assert build.RAPID_REQUIREMENTS_NAME == "ocr-rapid-cpu-requirements.txt"
    assert build.OCR_REQUIREMENTS_NAME == "ocr-paddle-cpu-requirements.txt"
    assert requirements_name(OcrEngineKind.RAPID, OcrRuntimeVariant.CPU) == build.RAPID_REQUIREMENTS_NAME
    for kind, variant in build.VARIANT_SWAPS:
        assert variant is not OcrRuntimeVariant.CPU
        assert requirements_name(kind, variant).startswith(f"ocr-{kind.value}-{variant.value}")


@pytest.mark.skipif(not build.OCR_SITE_PACKAGES.is_dir(), reason="本机没有 .venv-ocr")
def test_rapid_requirements_from_the_real_ocr_environment() -> None:
    text = build.rapid_pinned_requirements(build.installed_distributions(build.OCR_SITE_PACKAGES))
    names = [line.split("==")[0].lower() for line in text.splitlines()]
    assert {"rapidocr", "onnxruntime", "numpy", "pyclipper", "shapely", "omegaconf"} <= set(names)
    assert len([name for name in names if name.startswith("opencv-")]) == 1
    assert not any(name.startswith(("paddle", "torch")) for name in names)


# ---------------------------------------------------------------------- 运行环境的版本（OCRE-07）

CPU_PINS = "numpy==2.3.0\nonnxruntime==1.31.0\nrapidocr==3.10.0\n"
PADDLE_PINS = "numpy==2.3.0\npaddlepaddle==3.3.1\npaddlex==3.7.2\n"


def test_every_declared_gpu_variant_has_a_swap() -> None:
    from lst.services.ocr_engines import ENGINE_VARIANTS

    declared = {
        (kind, variant)
        for kind, variants in ENGINE_VARIANTS.items()
        for variant in variants
        if variant is not OcrRuntimeVariant.CPU
    }
    assert declared == set(build.VARIANT_SWAPS)


def test_swap_replaces_only_the_inference_library() -> None:
    swap = build.VARIANT_SWAPS[(OcrEngineKind.RAPID, OcrRuntimeVariant.CUDA)]
    kept, added = build.swap_requirements(CPU_PINS, swap)
    assert kept == "numpy==2.3.0\nrapidocr==3.10.0\n"
    assert added == ["onnxruntime-gpu[cuda,cudnn]==1.31.0"]
    kept, added = build.swap_requirements(
        CPU_PINS, build.VARIANT_SWAPS[(OcrEngineKind.RAPID, OcrRuntimeVariant.DIRECTML)]
    )
    assert added == ["onnxruntime-directml==1.24.4"]
    kept, added = build.swap_requirements(
        PADDLE_PINS, build.VARIANT_SWAPS[(OcrEngineKind.PADDLE, OcrRuntimeVariant.CUDA)]
    )
    assert "paddlepaddle==" not in kept and "paddlex==3.7.2" in kept
    assert added == [
        "paddlepaddle-gpu @ https://paddle-whl.cdn.bcebos.com/stable/cu129/paddlepaddle-gpu/"
        "paddlepaddle_gpu-3.3.1-cp313-cp313-win_amd64.whl"
    ]
    with pytest.raises(ValueError):
        build.swap_requirements("numpy==2.3.0\n", swap)


def test_check_requirements_accepts_direct_wheel_urls_and_rejects_unpinned() -> None:
    url = "paddlepaddle-gpu @ https://example.invalid/paddlepaddle_gpu-3.3.1-cp313-cp313-win_amd64.whl"
    assert build.check_requirements(f"{RESOLVE_MARKER}\nnumpy==2.3.0\n{url}\n", hashed=False) == 2
    with pytest.raises(SystemExit):
        build.check_requirements("numpy>=2\n", hashed=False)
    with pytest.raises(SystemExit):
        build.check_requirements(f"{RESOLVE_MARKER}\n" + HASHED, hashed=True)


def test_offline_gpu_variant_writes_a_resolve_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def offline(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        raise subprocess.CalledProcessError(2, args, "", "error: failed to fetch")

    monkeypatch.setattr(build, "BUILD", tmp_path)
    monkeypatch.setattr(build, "run", offline)
    path, hashed = build.variant_requirements(
        CPU_PINS, OcrEngineKind.RAPID, OcrRuntimeVariant.CUDA, allow_unhashed=True
    )
    text = path.read_text(encoding="utf-8")
    assert path.name == "ocr-rapid-cuda-requirements.txt" and not hashed
    assert requires_resolution(text) and not requires_hashes(text)
    assert "onnxruntime-gpu[cuda,cudnn]==1.31.0" in text and "onnxruntime==" not in text
    with pytest.raises(SystemExit):
        build.variant_requirements(
            CPU_PINS, OcrEngineKind.RAPID, OcrRuntimeVariant.CUDA, allow_unhashed=False
        )


def test_online_gpu_variant_resolves_then_hashes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[list[str]] = []

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        calls.append(args)
        output = Path(args[args.index("--output-file") + 1])
        if "--generate-hashes" in args:
            lines = Path(args[3]).read_text(encoding="utf-8").splitlines()
            output.write_text(
                "".join(f"{line} \\\n    --hash=sha256:{'a' * 64}\n" for line in lines), encoding="utf-8"
            )
        else:
            assert "--override" in args and "--no-deps" not in args
            output.write_text(
                "# via uv\nnumpy==2.3.0\nonnxruntime-directml==1.24.4\nrapidocr==3.10.0\n"
                "sympy==1.14.0\nmpmath==1.3.0\n",
                encoding="utf-8",
            )
        return subprocess.CompletedProcess(args, 0, "", "")

    monkeypatch.setattr(build, "BUILD", tmp_path)
    monkeypatch.setattr(build, "run", fake_run)
    path, hashed = build.variant_requirements(
        CPU_PINS, OcrEngineKind.RAPID, OcrRuntimeVariant.DIRECTML, allow_unhashed=False
    )
    assert hashed and len(calls) == 2
    text = path.read_text(encoding="utf-8")
    assert build.check_requirements(text, hashed=True) == 5
    assert "sympy==1.14.0" in text and not requires_resolution(text)
