r"""installer\lsttranslate.iss 的卸载步骤（需求 INS-06，架构文档 15.5 节）。

Inno Setup 的 [Code] 段在测试里无法执行，这里按文本检查选“删除用户数据”时的三步：先 purge-secrets、
再 purge-data（删除设置里另选的数据目录与模型目录中由程序创建的内容和位置文件），两步都在程序文件删除之前；
程序文件删除之后再整个删除默认数据目录与位置文件所在的目录。
"""

import re
from pathlib import Path

ISS = Path(__file__).resolve().parents[2] / "installer" / "lsttranslate.iss"


def _procedure(text: str, name: str) -> str:
    match = re.search(rf"procedure {name}\(.*?\n(.*?)\nend;\n", text, re.DOTALL)
    assert match is not None, name
    return match.group(1)


def test_uninstall_purges_secrets_then_chosen_dirs_before_files_are_removed() -> None:
    body = _procedure(ISS.read_text(encoding="utf-8"), "CurUninstallStepChanged")
    before, after = body.split("usPostUninstall", 1)
    assert before.index("purge-secrets") < before.index("purge-data")
    assert "if DeleteUserData then" in before
    assert "'\" purge-data'" not in after  # 程序文件删除之后解释器已经不在，不能再运行


def test_post_uninstall_deletes_the_default_dir_and_the_location_dir() -> None:
    body = _procedure(ISS.read_text(encoding="utf-8"), "CurUninstallStepChanged")
    after = body.split("usPostUninstall", 1)[1]
    assert "DelTree(UserDataDir(), True, True, True);" in after
    assert "DelTree(ExpandConstant('{userappdata}\\{#AppName}'), True, True, True);" in after


def test_header_documents_the_location_file() -> None:
    header = ISS.read_text(encoding="utf-8").split("#define", 1)[0]
    assert "%APPDATA%\\LSTtranslate\\location.json" in header and "purge-data" in header
