"""程序清单保留原有设置，并让原生文件接口接受 UTF-8 路径。"""

import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

from scripts.windows_manifest import enable_utf8_code_page, utf8_manifest


def test_utf8_manifest_retains_security_and_windows_settings() -> None:
    original = b"""<assembly xmlns="urn:schemas-microsoft-com:asm.v1" manifestVersion="1.0">
      <trustInfo xmlns="urn:schemas-microsoft-com:asm.v3"><security><requestedPrivileges>
        <requestedExecutionLevel level="asInvoker" uiAccess="false"/>
      </requestedPrivileges></security></trustInfo>
      <application xmlns="urn:schemas-microsoft-com:asm.v3"><windowsSettings>
        <longPathAware xmlns="http://schemas.microsoft.com/SMI/2016/WindowsSettings">true</longPathAware>
      </windowsSettings></application>
    </assembly>"""
    result = utf8_manifest(original)
    root = ET.fromstring(result)
    execution = root.find(".//{*}requestedExecutionLevel")
    assert execution is not None
    assert execution.attrib == {
        "level": "asInvoker",
        "uiAccess": "false",
    }
    assert root.findtext(".//{*}longPathAware") == "true"
    assert root.findtext(".//{*}activeCodePage") == "UTF-8"
    assert utf8_manifest(result) == result


def test_utf8_manifest_adds_application_settings() -> None:
    result = utf8_manifest(b'<assembly xmlns="urn:schemas-microsoft-com:asm.v1"/>')
    assert ET.fromstring(result).findtext(".//{*}activeCodePage") == "UTF-8"
    with pytest.raises(ValueError, match="程序清单"):
        utf8_manifest(b"<other/>")


@pytest.mark.skipif(sys.platform != "win32", reason="Windows PE 清单")
def test_patched_interpreter_uses_utf8_ansi_file_api(tmp_path: Path) -> None:
    # 通过 PATH 查找原运行时的 DLL，避免复制全部标准库。
    source = Path(sys.base_prefix) / "python.exe"
    target = tmp_path / "python-utf8.exe"
    shutil.copy2(source, target)
    enable_utf8_code_page(target)
    result = subprocess.run(
        [str(target), "-I", "-c", "import ctypes; print(ctypes.windll.kernel32.GetACP())"],
        capture_output=True,
        text=True,
        check=True,
        env={"PATH": str(source.parent), "PYTHONHOME": str(source.parent)},
    )
    assert result.stdout.strip() == "65001"
