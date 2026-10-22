"""scripts/build_installer.py 的纯函数：版本清单整理、sha256 校验文件、uv 许可证的来源。"""

import hashlib
import re
from pathlib import Path

import pytest

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
