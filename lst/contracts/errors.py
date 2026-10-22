"""错误码。

业务模块只产生错误码和技术细节，界面文案由 ui.i18n 按错误码翻译（架构文档 10.3 节，需求 OBS-04）。
技术细节（SegmentOutcome.error）只写日志和诊断，不直接显示给用户。
"""

from enum import StrEnum


class ErrorCode(StrEnum):
    """翻译尝试失败的原因分类。"""

    # 引擎侧
    NETWORK = "engine.network"  # 连接失败、DNS 失败、传输中断
    TIMEOUT = "engine.timeout"  # 超过任务时限
    AUTH = "engine.auth"  # API key 无效或无权限（401、403）
    QUOTA = "engine.quota"  # 配额或余额耗尽（DeepL 456、HTTP 402）
    RATE_LIMITED = "engine.rate_limited"  # 限流，重试后仍失败（429、百度 54003）
    SERVER = "engine.server"  # 服务端错误（5xx）
    BAD_REQUEST = "engine.bad_request"  # 请求被拒绝（其他 4xx）
    BAD_RESPONSE = "engine.bad_response"  # 返回内容无法解析
    UNSUPPORTED_LANGUAGE = "engine.unsupported_language"  # 接口不支持该语言对
    IMAGE_UNSUPPORTED = "engine.image_unsupported"  # 模型不支持图片输入，应关闭该实例的“附带截图”（ENG-07）
    MODEL_MISSING = "engine.model_missing"  # 本地模型未安装
    NO_MATCH = "engine.no_match"  # 词典里查不到这一段的任何词，让其他引擎胜出
    WORKER_CRASHED = "engine.worker_crashed"  # 推理进程崩溃
    INTERNAL = "engine.internal"  # 引擎实现内部的未预期异常

    # 调度器侧
    MISSING_SEGMENT = "race.missing_segment"  # 引擎结束时没有返回该段
    ENGINE_REMOVED = "race.engine_removed"  # 实例因配置变更被移除或替换
