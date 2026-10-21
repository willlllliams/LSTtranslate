"""本地 NMT 推理进程（架构文档 2.5、8.3 节）。

入口在 __main__.py，进程的通用部分（握手、排队、取消、退出）在 lst.workers._runtime。
本包的模块：

- backend：后端协议与能力清单格式；
- handler：把后端接到运行时上；
- ct2_backend：默认的 CTranslate2 后端；
- argos：Argos 模型包的扫描与解压；
- routing：中转路线规划与逐段翻译流程；
- sentences：规则句子切分；
- languages：语言代码的规范写法；
- lru：常驻模型的 LRU 缓存。
"""
