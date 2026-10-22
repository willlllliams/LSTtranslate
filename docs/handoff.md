# 交接说明（2026-10-22）

本文保留 beta.2 之后的初始交接快照。后续审核、修复与原始 PNG 验收见 `project-review.md`、`ocr-quality.md` 和 `technical-debt.md`；下文“做到一半”描述交接时状态。

本文记录 0.2.0-beta.2 发布之后工作区里未提交改动的状态，供接手的 agent 使用。交接时最后一次提交是 `7145747`（0.2.0-beta.2，已推送并发布预发布 Release）。工作区里的改动都没有提交，其中有些已经完成，有些做到一半。

## 已完成：截屏丢块修复

真机截屏时，PaddleOCR 返回的块在主进程里被丢掉，只有标题被翻译。三个根因都已用真实 PaddleOCR 复现并修复，相关测试 1075 项通过。

1. 检测框阈值 `[ocr_paddle].text_det_box_thresh` 的上限改为 0.7（`MAX_DET_BOX_THRESH`，lst/services/ocr/config.py），超出范围的值回退到默认的 0.6。用户配置里的 1.0 使检测一个框都留不下，这是两段正文丢失的原因。
2. 版面检测判成 image 的块现在保留块内的文字行（lst/workers/ocr_worker/convert.py），行框大于块时也归入该块（`_assign_lines` 的第二条规则）。
3. 没有单元格网格的表格、没有数据表的图表和图片块，改为逐行出段（lst/services/ocr/segments.py 的 `_line_segments`）。

涉及的文件：lst/services/ocr/config.py、segments.py，lst/workers/ocr_worker/convert.py、settings.py，lst/ui/settings/strings_ocr.py，tests/services/ocr/test_config.py、test_normalize.py、test_segments.py，tests/workers/ocr/test_convert.py、test_settings.py，tests/workers/ocr/test_paddle_screenshot.py 与 fixtures/screenshot_test_page.jpg，docs 中的 OV-03 与绘制顺序。

## 做到一半：agent 自动化接口与 dev 版

用户要求：程序加入供 agent 调用的接口，设置里默认关闭；同时发布 dev 版和普通版，两者只差这个设置的默认值。设计见 docs/automation.md（未完成，需要核对）：

- 走只允许当前用户连接的 Windows 命名管道，消息是 JSON 行；
- 配置 `[automation].enabled` 的默认值由 installed.json 的 flavor 决定；
- 方法包括 status、capture（direct 或 mask）、wait_result、overlay、grab、settings、bindings、storage、logs、quit；
- `build_installer.py --flavor dev` 生成 `-dev-setup.exe`，应用内更新按 flavor 选择安装包。

已有的文件：lst/app/automation/、lst/automation/、lst/app/flavor.py、lst/contracts/automation.py、lst/ui/common/inject.py、lst/ui/settings/strings_automation.py；bootstrap、__main__、设置页（module、page_about、page_storage、presenter）、托盘、更新服务、构建脚本、iss、launcher 的修改；tests/automation/ 等测试；readme.md 的修改。

这部分的 agent 在中途被停止，没有跑全量测试，状态未知。接手时先跑 `uv run pytest -q`，再对照 docs/automation.md 检查每个方法是否实现完整。

## 做到一半：OCR 版面还原保真度

用户要求：用两组测试图，尽可能还原参考译图的版面。图在 tests/fidelity/fixtures/：

- addons-en.png 对应 addons-zh-hant.webp；
- blog-en.webp 对应 blog-zh-hant.webp。

计划如下：

1. 标注期望文字单元，存为 *.expected.json，每个单元带参考译文 zh 字段；
2. 对 PaddleOCR 的版面模式与文本模式、RapidOCR、Windows OCR，分别计算单元召回、粘连、拆碎、bbox IoU 与文字准确率；
3. 用假翻译引擎套参考译文，渲染覆盖窗口，与参考图并排比较字号；
4. 按指标改进，最后把指标下限写成测试断言。

这部分的 agent 在给 Windows OCR 测基线时被停止，tests/fidelity/ 下只有部分内容。

## 尚未在真机验证

- 键位修改后的实际效果；
- 存储位置迁移与“立即重启”；
- 卸载时“删除用户数据”；
- 显卡运行库（553 MB）与词典包、免费翻译接口的真实下载。

前两项原计划在本机装好 dev 版之后，通过自动化接口验证。

## 约定

- 提交时不带 run.bat、start.md。
- 仓库的 git 身份是 willlllliams。提交前检查不含本机用户名、私人邮箱与密钥。
- 用户的工作要求已写进项目记忆：删除功能时整条链路一起删；排查问题要逐函数追踪，并实际复现。
