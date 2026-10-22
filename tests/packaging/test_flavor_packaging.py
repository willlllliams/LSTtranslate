r"""安装脚本按构建类型装 installed.json，启动器的 automation 子命令（需求 DEV-02、DEV-07、DEV-08）。

Inno Setup 的编译在测试里不运行（需要 ISCC 与完整的暂存目录），这里按文本检查：开发版的安装包名多一段 -dev，
暂存目录里自检用的 installed.json 被排除，改装 MarkerDir 下按构建类型写好的那一份，AppId 只有一个。
启动器的测试与 test_launcher.py 相同，在临时目录里搭安装布局，app\lst\automation 是假的客户端。
"""

import json
import re
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
ISS = ROOT / "installer" / "lsttranslate.iss"
LAUNCHER = ROOT / "packaging" / "launcher" / "worker.py"


def test_installer_name_and_marker_follow_the_flavor() -> None:
    text = ISS.read_text(encoding="utf-8")
    assert "OutputBaseFilename=LSTtranslate-{#AppVersion}{#FlavorSuffix}-setup" in text
    assert re.search(r'#if Flavor == "dev"\s+#define FlavorSuffix "-dev"', text)
    assert re.search(r'#elif Flavor == "normal"\s+#define FlavorSuffix ""', text)
    files = text.split("[Files]", 1)[1].split("[Icons]", 1)[0]
    assert 'Excludes: "\\app\\installed.json"' in files
    assert 'Source: "{#MarkerDir}\\installed.json"; DestDir: "{app}\\app"' in files


def test_both_flavors_share_one_app_id() -> None:
    """AppId 相同：普通版与开发版互相覆盖安装，卸载信息也只有一份。"""
    text = ISS.read_text(encoding="utf-8")
    ids = re.findall(r"^AppId=(.+)$", text, re.MULTILINE)
    assert ids == ["{{435CC09B-5F36-4944-9918-62F7215D2057}"]
    assert "Flavor" not in text.split("[Setup]", 1)[1].split("AppId=", 1)[0]


_FAKE_CLIENT = """\
import json, sys
print(json.dumps({"argv": sys.argv[1:], "path": sys.path[:2]}))
sys.exit(3)
"""


@pytest.fixture
def layout(tmp_path: Path) -> Path:
    install = tmp_path / "LSTtranslate"
    (install / "launcher").mkdir(parents=True)
    shutil.copy2(LAUNCHER, install / "launcher" / "worker.py")
    package = install / "app" / "lst" / "automation"
    package.mkdir(parents=True)
    (install / "app" / "lst" / "__init__.py").write_text("", encoding="utf-8")
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "__main__.py").write_text(_FAKE_CLIENT, encoding="utf-8")
    (install / "site" / "main").mkdir(parents=True)
    return install


def test_launcher_runs_the_automation_client(layout: Path) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-X",
            "utf8",
            str(layout / "launcher" / "worker.py"),
            "automation",
            "capture",
            '{"rect": [0, 0, 10, 10]}',
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
        timeout=30,
    )
    assert result.returncode == 3, result.stderr  # 退出码由客户端给出
    output = json.loads(result.stdout)
    assert output["argv"] == ["capture", '{"rect": [0, 0, 10, 10]}']
    assert output["path"] == [str(layout / "app"), str(layout / "site" / "main")]
