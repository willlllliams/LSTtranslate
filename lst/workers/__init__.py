"""推理子进程（架构文档第 3 章、第 8 章）。

每个推理进程是一个可以 `python -m` 运行的子包（nmt_worker、ocr_worker），共用 _runtime 里的握手、排队、
取消和退出逻辑。本包只依赖 lst.contracts、标准库和推理库，由 import-linter 检查。
"""
