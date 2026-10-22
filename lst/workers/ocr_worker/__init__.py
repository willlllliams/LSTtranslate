"""OCR 推理进程（架构文档 2.4、8.4 节，需求 OCR-01 至 OCR-09、CAP-05）。

入口在 __main__.py，进程的通用部分（握手、排队、取消、退出）在 lst.workers._runtime。
PaddleOCR 后端运行在独立环境 .venv-ocr 中，Windows OCR 后端运行在主环境中。paddle_backend 在加载时才导入
推理库，winocr_backend 在加载时才导入 winrt，其余模块只依赖标准库与 numpy，主环境可以导入和测试；
选 Windows OCR 后端时整个导入链不碰 paddle、paddlex、cv2。本包的模块：

- handler：把后端接到运行时上，解包像素帧，映射错误码；
- backend：后端协议；
- paddle_backend：默认后端，版面模式用 PP-StructureV3，文本模式用 PaddleOCR 文本检测 + 识别；
- winocr_backend：Windows 自带 OCR（Windows.Media.Ocr）后端，只有文本模式；
- plain_text：无位置引擎的输出（整张截图一个块）与按文字系统拼接文本的规则；
- paddle_config：PaddleX 产线配置的构造（子能力、模型、模型目录、oneDNN）；
- settings：Configure 设置与请求 options 的校验；
- languages：识别语言到识别模型的选择；
- image：BGRA 像素帧解包与灰度化；
- geometry：包围盒运算与坐标换算（缩放、方向校正）；
- extract：从 PaddleX 结果对象取出转换用的数据；
- convert：转换成版面块（类别映射、文本行归属、表格、公式、图表）；
- labels：版面标签到块类别的映射表；
- table_html：pred_html 解析与单元格框对齐；
- blocks：OcrResult.blocks 的 JSON 输出；
- network_guard：进程内的网络拦截。
"""
