# ruff: noqa: E501  文案表里一条文案写在一行，方便对照中英文；折行后反而难读。
"""流程图与两类流水线页面的界面文案（需求 UI-05）。导入本模块时登记文案。

键的约定：

- 流程图组件的通用文案：``settings.flow.<名称>``（开关状态、第几步、键盘操作说明等）；
- 节点：``settings.flow.node.<节点>.title``、``.desc``（面板里的说明），节点 key 见 flow_specs；
  OCR 环节节点沿用 ``settings.ocr_stage.<环节>`` 的名称与说明，这里不重复；
- 节点上的状态行：``settings.flow.status.<名称>``。

界面不显示技术细节（架构文档 10.3 节）：文案里不出现配置字段名与错误码；引擎与库的名字（lingua、
SentencePiece、CTranslate2、Windows.Media.Ocr）是用户在“关于”页也能看到的名字，可以出现。
"""

from typing import Final

from lst.ui.i18n import Catalog, register_catalog


def _pair(zh: str, en: str) -> dict[str, str]:
    return {"zh-Hans": zh, "en": en}


def _node(key: str, zh: tuple[str, str], en: tuple[str, str]) -> Catalog:
    """一个节点的名称与说明。"""
    return {
        f"settings.flow.node.{key}.title": _pair(zh[0], en[0]),
        f"settings.flow.node.{key}.desc": _pair(zh[1], en[1]),
    }


CATALOG: Final[Catalog] = {
    # 流程图组件
    "settings.flow.switch_on": _pair("开关已打开", "Switch on"),
    "settings.flow.switch_off": _pair("开关已关闭", "Switch off"),
    "settings.flow.bypassed": _pair(
        "已跳过，数据直接流向下一步", "Skipped; data goes straight to the next step"
    ),
    "settings.flow.position": _pair("第 {index} 步，共 {count} 步", "Step {index} of {count}"),
    "settings.flow.opened": _pair("设置显示在流程图下方", "Its settings are shown below the chart"),
    "settings.flow.keys": _pair(
        "按 Enter 或空格在下方显示设置，方向键切换步骤",
        "Press Enter or Space to show its settings below; use the arrow keys to move between steps",
    ),
    "settings.flow.keys_switch": _pair(
        "按 Enter 在下方显示设置，按空格切换开关，方向键切换步骤",
        "Press Enter to show its settings below, Space to flip the switch; use the arrow keys to move between steps",
    ),
    "settings.flow.separator": _pair("；", "; "),
    "settings.flow.ocr.name": _pair("{name}的识别流程图", "{name} recognition flow chart"),
    "settings.flow.ocr.intro": _pair(
        "下图按实际执行顺序画出一次截屏识别经过的每一步。点击一个步骤，在图下方查看它的说明与设置；带开关的步骤可以直接在图上打开或关闭，关闭的步骤变灰，虚线表示数据绕过它。",
        "The chart shows every step a capture goes through, in the order they run. Click a step to see its description and settings below the chart. Steps with a switch can be turned on or off right on the chart; a step that is off is greyed out and the dashed line shows the data going around it.",
    ),
    "settings.flow.nmt.title": _pair("翻译流程", "Translation flow"),
    "settings.flow.nmt.name": _pair("本地翻译流程图", "Local translation flow chart"),
    "settings.flow.nmt.intro": _pair(
        "下图按实际执行顺序画出一段文字交给本地翻译模型后经过的每一步，前四步在主程序里，其余在本地翻译进程里。点击一个步骤，在图下方查看它的说明与设置。",
        "The chart shows every step a piece of text goes through with the local translation models, in the order they run. The first four steps run in the main program, the rest in the local translation process. Click a step to see its description and settings below the chart.",
    ),
    "settings.flow.stage_params": _pair("{stage}的参数", "{stage} parameters"),
    "settings.flow.stage_params.owner": _pair("{name}的{stage}", "{name} {stage}"),
    "settings.flow.params.reset": _pair("恢复默认(&S)…", "Re&set to defaults…"),
    "settings.flow.params.expand": _pair("展开(&X)", "Show (&X)"),
    "settings.flow.params.collapse": _pair("收起(&X)", "Hide (&X)"),
    "settings.flow.goto_options": _pair("转到截图与识别选项(&O)", "Go to capture &options"),
    "settings.flow.goto_normalize": _pair("转到结构归一化(&N)", "Go to structure &normalization"),
    "settings.flow.goto_engines": _pair("转到翻译引擎(&T)", "Go to &translation engines"),
    "settings.flow.goto_gpu": _pair("转到显卡加速(&G)", "Go to &GPU acceleration"),
    # 状态行
    "settings.flow.status.on": _pair("已开启", "On"),
    "settings.flow.status.off": _pair("已关闭", "Off"),
    "settings.flow.status.separator": _pair(" · ", " · "),
    "settings.flow.status.resize": _pair("长边 ≤ {size}", "Long side ≤ {size}"),
    "settings.flow.status.no_resize": _pair("不缩放", "No resizing"),
    "settings.flow.status.gray": _pair("灰度", "Grayscale"),
    "settings.flow.status.color": _pair("彩色", "Color"),
    "settings.flow.status.ocr_process": _pair("OCR 进程", "OCR process"),
    "settings.flow.status.main_process": _pair("主程序", "Main program"),
    "settings.flow.status.no_model": _pair("没有可选模型", "No models"),
    "settings.flow.status.gpu_only": _pair("CPU 上跳过", "Skipped on CPU"),
    "settings.flow.status.system_ocr": _pair("Windows.Media.Ocr", "Windows.Media.Ocr"),
    "settings.flow.status.langid": _pair("主程序 · lingua", "Main program · lingua"),
    "settings.flow.status.cache": _pair("命中即跳过翻译", "Hits skip translation"),
    "settings.flow.status.batch": _pair(
        "每批 ≤ {segments} 段、{chars} 字", "≤ {segments} segments, {chars} chars"
    ),
    "settings.flow.status.worker": _pair("进程：{state}", "Process: {state}"),
    "settings.flow.status.route": _pair("直连优先", "Direct first"),
    "settings.flow.status.sentences": _pair("按句末标点", "By sentence ends"),
    "settings.flow.status.direct": _pair("源语言 → 目标语言", "Source → target"),
    "settings.flow.status.pivot": _pair("源 → 英语 → 目标", "Source → English → target"),
    "settings.flow.status.model": _pair("最多常驻 {count} 个", "Up to {count} kept loaded"),
    "settings.flow.status.tokenize": _pair("SentencePiece", "SentencePiece"),
    "settings.flow.status.device_gpu": _pair("显卡", "GPU"),
    "settings.flow.status.device_cpu": _pair("CPU", "CPU"),
    "settings.flow.status.infer": _pair("{device} · {compute}", "{device} · {compute}"),
    "settings.flow.status.infer_pending": _pair("设备：{device}", "Device: {device}"),
    "settings.flow.status.detokenize": _pair("整理中文标点", "Tidies Chinese text"),
    "settings.flow.status.join": _pair("逐段发回", "Sent back per segment"),
    # 推理设备卡片
    "settings.flow.device.title": _pair("推理设备", "Inference device"),
    "settings.flow.device.desc": _pair(
        "本地翻译进程现在用{device}推理，计算类型 {compute}。推理设备与显卡运行库在“显卡加速”页设置。",
        "The local translation process now runs on the {device} with compute type {compute}. The inference device and the GPU runtime are set on the GPU acceleration page.",
    ),
    "settings.flow.device.desc_pending": _pair(
        "本地翻译进程还没有报告推理设备，设置里选的是“{device}”。推理设备与显卡运行库在“显卡加速”页设置。",
        "The local translation process has not reported its device yet; the setting is “{device}”. The inference device and the GPU runtime are set on the GPU acceleration page.",
    ),
    # [nmt].beam_size
    "settings.f.nmt.beam_size.title": _pair("集束宽度", "Beam size"),
    "settings.f.nmt.beam_size.desc": _pair(
        "生成译文时每一步保留的候选数。越大译文越稳定，翻译越慢。修改后本地翻译进程自动重启。",
        "How many candidate translations are kept at each step. Larger values give steadier translations and translate more slowly. The local translation process restarts after a change.",
    ),
    # OCR 流程的非环节节点
    **_node(
        "preprocess",
        (
            "截图预处理",
            "识别之前先处理截图：宽或高超过缩放上限时等比缩小，再按设置转成灰度图。识别结果的坐标在“转成版面块”时换算回原始截图。缩放上限与灰度在“截图与识别选项”页设置。",
        ),
        (
            "Image preprocessing",
            "Before recognition the capture is scaled down proportionally when its width or height exceeds the size limit, then turned into grayscale if set. Coordinates are mapped back to the original capture when the result is converted to blocks. The size limit and grayscale are set on the Capture & recognition options page.",
        ),
    ),
    **_node(
        "system_ocr",
        (
            "系统 OCR",
            "Windows 自带的文字识别一次完成文字检测与识别。它按识别语言创建识别器，不提供模型选择与参数。识别语言在“截图与识别选项”页设置，可用的语言取决于 Windows 装了哪些语言的光学字符识别功能。",
        ),
        (
            "System OCR",
            "The OCR built into Windows detects and recognizes text in one go. It creates a recognizer for the recognition language and offers no model choice or parameters. The recognition language is set on the Capture & recognition options page; the languages available depend on which optical character recognition features are installed in Windows.",
        ),
    ),
    **_node(
        "assemble",
        (
            "拼成文本行",
            "系统 OCR 按行给出每个词的位置。这一步把一行的词拼成一行文字：汉字之间不加空格，汉字与拉丁字母之间的间距足够大时加空格；行的范围取各个词的矩形的并集，并换算回原始截图的坐标。",
        ),
        (
            "Assemble lines",
            "The system OCR gives the position of every word, line by line. This step joins the words of a line into one line of text: no spaces between Chinese characters, a space between Chinese and Latin text when the gap is wide enough. The line covers the union of its word boxes, mapped back to the original capture.",
        ),
    ),
    **_node(
        "convert",
        (
            "转成版面块",
            "把引擎的输出整理成统一的版面块，坐标换算回原始截图。版面模式按阅读顺序输出正文、标题、表格、公式、图表等块；文本模式输出一个覆盖整张截图的块，里面是全部文本行。",
        ),
        (
            "Convert to blocks",
            "Turns the engine output into uniform layout blocks, with coordinates mapped back to the original capture. Layout mode outputs text, title, table, formula and chart blocks in reading order; text mode outputs one block covering the whole capture that holds all text lines.",
        ),
    ),
    **_node(
        "decode",
        (
            "解码校验",
            "主程序收到 OCR 进程的结果后先解码并检查：字段、类别或坐标不合格的结果按识别失败处理，不会带着错误的数据往下走。",
        ),
        (
            "Decode & check",
            "The main program decodes the result from the OCR process and checks it first. A result with invalid fields, categories or coordinates counts as a failed recognition instead of passing bad data on.",
        ),
    ),
    **_node(
        "clean",
        (
            "清洗",
            "去掉文字为空、面积过小、置信度过低的框，以及与另一个框高度重叠的重复框。阈值在“结构归一化”页设置。",
        ),
        (
            "Clean",
            "Removes boxes with empty text, boxes that are too small or below the confidence threshold, and duplicates that overlap another box almost completely. The thresholds are set on the Structure normalization page.",
        ),
    ),
    **_node(
        "split",
        (
            "拆分跨栏长行",
            "只在文本模式下起作用。一行文字横跨两栏之间的空白时，按空白拆成左右两行，后面合并段落时两栏才不会粘成一段。判断条件在“结构归一化”页设置。",
        ),
        (
            "Split cross-column lines",
            "Works in text mode only. When a line spans the gap between two columns, it is split at the gap into a left and a right line, so that merging paragraphs afterwards keeps the columns apart. The conditions are set on the Structure normalization page.",
        ),
    ),
    **_node(
        "aggregate",
        (
            "整理文字单元",
            "结合截图颜色、行距和对齐整理文字单元。文本模式下合并连续段落，版面模式下拆开粘连的菜单和控件。关闭后文本模式每行单独翻译。",
        ),
        (
            "Organize text units",
            "Uses screenshot colors, spacing and alignment to join paragraphs in text mode and separate merged controls in layout mode. When off, text mode translates each line separately.",
        ),
    ),
    **_node(
        "merge_blocks",
        (
            "合并相邻正文块",
            "版面模式下把上下紧挨、左右对齐的相邻正文块合并成一块。版面检测给出的正文块本身已经是段落，所以默认关闭。间距上限在“结构归一化”页设置。",
        ),
        (
            "Merge adjacent blocks",
            "In layout mode, merges neighbouring text blocks that sit right above each other and line up. The text blocks from layout detection are already paragraphs, so this is off by default. The gap limit is set on the Structure normalization page.",
        ),
    ),
    **_node(
        "segments",
        (
            "切分成段",
            "把版面块切成翻译用的段：正文与标题一块一段，表格每个单元格一段，图表的数据表每格一段；公式与图片不翻译，覆盖窗口显示原图。",
        ),
        (
            "Split into segments",
            "Cuts the layout blocks into the segments that get translated: one segment per text or title block, one per table cell and one per chart data cell. Formulas and pictures are not translated; the overlay shows the original image.",
        ),
    ),
    # 本地翻译流程
    **_node(
        "langid",
        (
            "语言检测",
            "主程序先判断每一段的源语言：出现假名判为日文，出现谚文判为韩文，其余交给 lingua 判断；判为中文时，再按简体、繁体专用字的个数区分简繁。双语窗口手动指定了源语言时跳过这一步。",
        ),
        (
            "Language detection",
            "The main program first works out the source language of every segment: kana means Japanese, Hangul means Korean, and the rest goes to lingua. Text detected as Chinese is then told apart as Simplified or Traditional by counting characters unique to each. Skipped when the bilingual window has a source language chosen.",
        ),
    ),
    **_node(
        "cache",
        (
            "查译文缓存",
            "同样的文字、同样的语言对、同样的引擎设置翻译过的段，直接取缓存里的译文，不再送去翻译。",
        ),
        (
            "Translation cache",
            "A segment already translated with the same text, language pair and engine settings takes its translation from the cache and is not sent again.",
        ),
    ),
    **_node(
        "batch",
        (
            "分批",
            "同一语言对的段按顺序打包成批，每批的段数与字数有上限，一批作为一个请求发给本地翻译进程。批越大，第一批译文出现得越晚。上限在“翻译引擎”页的本地翻译实例里设置。",
        ),
        (
            "Batching",
            "Segments of the same language pair are packed in order into batches with a limit on segments and characters; each batch becomes one request to the local translation process. Larger batches make the first translations appear later. The limits are set on the local translation instance in Translation engines.",
        ),
    ),
    **_node(
        "submit",
        (
            "提交到翻译进程",
            "本地翻译引擎把一批段转成一个翻译请求，交给独立运行的本地翻译进程。进程只有一个推理线程，请求按到达顺序处理；进程出错时由主程序自动重启。",
        ),
        (
            "Send to translation process",
            "The local translation engine turns a batch into one translation request for the separate local translation process. The process has a single inference thread and handles requests in order; the main program restarts it if it fails.",
        ),
    ),
    **_node(
        "route",
        (
            "路线规划",
            "有源语言到目标语言的直连模型时直接翻译；没有时，如果源语言到英语、英语到目标语言两个模型都有，就经英语中转；都没有时这一批的段报告缺少模型。源语言与目标语言相同时原文就是译文。",
        ),
        (
            "Route planning",
            "With a direct model from the source to the target language the text is translated directly. Otherwise, if there are models from the source to English and from English to the target, it goes through English; if not, the segments report a missing model. When source and target are the same, the original text is the translation.",
        ),
    ),
    **_node(
        "sentences",
        (
            "分句",
            "翻译模型是按句子训练的，整段送进去译文会变差。每段先按行切开，再在行内按句末标点切成句子；英文的点号结合上下文判断，缩写、人名首字母与小数不断句。",
        ),
        (
            "Sentence split",
            "The models are trained on sentences, and whole paragraphs translate worse. Each segment is cut into lines and each line into sentences at sentence-ending punctuation; an English full stop is judged in context, so abbreviations, initials and decimals do not end a sentence.",
        ),
    ),
    **_node(
        "direct",
        (
            "直连",
            "有直连模型：后面的取模型、分词、推理、还原执行一遍，源语言直接译成目标语言。",
        ),
        (
            "Direct",
            "A direct model exists: the following steps (load model, tokenize, inference, detokenize) run once and translate straight into the target language.",
        ),
    ),
    **_node(
        "pivot",
        (
            "经英语中转",
            "没有直连模型时先译成英语，英语译文直接送进第二个模型译成目标语言，不发回主程序。后面的取模型、分词、推理、还原执行两遍，所以耗时约为直连的两倍，两次翻译的误差也会叠加。",
        ),
        (
            "Via English",
            "Without a direct model the text is first translated into English, and the English output goes straight into a second model for the target language without returning to the main program. The following steps run twice, so it takes about twice as long and the errors of both passes add up.",
        ),
    ),
    **_node(
        "model",
        (
            "取模型",
            "从内存里常驻的模型中取出这一跳要用的模型，最多常驻 4 个，最久没用的先卸载。不在内存里时先解压模型包再加载，所以第一次用某个语言对时会慢一些。模型包从下面的目录里查找。",
        ),
        (
            "Load model",
            "Takes the model for this hop from the models kept in memory; up to 4 stay loaded and the least recently used is unloaded first. A model not in memory is unpacked and loaded first, so the first use of a language pair is slower. Model packages are looked up in the folder below.",
        ),
    ),
    **_node(
        "tokenize",
        (
            "分词",
            "用模型包里的 SentencePiece 模型把每句切成子词，一段的全部句子作为一批送进推理。",
        ),
        (
            "Tokenize",
            "Cuts every sentence into subwords with the SentencePiece model in the package; all sentences of a segment go into inference as one batch.",
        ),
    ),
    **_node(
        "infer",
        (
            "CTranslate2 推理",
            "CTranslate2 用集束搜索逐词生成译文，禁止生成语言标记与私用区字符，最大长度按本批最长的源句设上限，模型陷入重复时不会一直生成下去。下面是可用的翻译模型、推理设备与集束宽度。",
        ),
        (
            "CTranslate2 inference",
            "CTranslate2 generates the translation word by word with beam search. Language tags and private-use characters are suppressed, and the maximum length is capped by the longest source sentence in the batch so a model stuck in repetition stops. The available models, the inference device and the beam size are below.",
        ),
    ),
    **_node(
        "detokenize",
        (
            "还原与整理",
            "把子词拼回文字，去掉语言标记与私用区字符。目标语言是中文时再整理一次：去掉汉字之间多余的空格，紧跟汉字的英文标点换成中文标点。",
        ),
        (
            "Detokenize & tidy",
            "Joins the subwords back into text and drops language tags and private-use characters. For Chinese targets the text is tidied once more: extra spaces between Chinese characters are removed and English punctuation right after Chinese characters becomes Chinese punctuation.",
        ),
    ),
    **_node(
        "join",
        (
            "拼回段落",
            "按原文的换行与目标语言的句间分隔把译句拼回一段，每译完一段就发回主程序显示，不等整批结束。",
        ),
        (
            "Rejoin",
            "Joins the translated sentences back into one segment, keeping the original line breaks and the sentence separator of the target language. Each segment is sent back to the main program as soon as it is done, without waiting for the whole batch.",
        ),
    ),
}

register_catalog(CATALOG)
