"""翻译框布局的双语文案。"""

from lst.ui.i18n import register_catalog

register_catalog(
    {
        "settings.overlay_layout.title": {"zh-Hans": "翻译框与字体", "en": "Translation boxes and text"},
        "settings.overlay_layout.intro": {
            "zh-Hans": "优先保持原文字号；扩框在截图内避让相邻内容，空间不足时再缩小文字。"
            "内外边距以原截图像素计。",
            "en": "Keep the original text size when space permits. Grow boxes within the screenshot "
            "before reducing text size. Padding uses source screenshot pixels.",
        },
        "settings.choice.overlay_layout.growth.fixed": {"zh-Hans": "固定框", "en": "Fixed box"},
        "settings.choice.overlay_layout.growth.vertical": {
            "zh-Hans": "固定横向范围，向纵向扩展",
            "en": "Keep width, grow height",
        },
        "settings.choice.overlay_layout.growth.both": {
            "zh-Hans": "同时扩宽和扩高",
            "en": "Grow width and height",
        },
        "settings.choice.overlay_layout.horizontal.left": {"zh-Hans": "左对齐", "en": "Left"},
        "settings.choice.overlay_layout.horizontal.center": {"zh-Hans": "居中", "en": "Center"},
        "settings.choice.overlay_layout.horizontal.right": {"zh-Hans": "右对齐", "en": "Right"},
        "settings.choice.overlay_layout.vertical.top": {"zh-Hans": "顶部对齐", "en": "Top"},
        "settings.choice.overlay_layout.vertical.center": {"zh-Hans": "居中", "en": "Center"},
        "settings.choice.overlay_layout.vertical.bottom": {"zh-Hans": "底部对齐", "en": "Bottom"},
    }
)

for _field, _title, _description in (
    (
        "growth",
        "Automatic box growth",
        "Choose a fixed box, keep its width and grow vertically, or grow both axes.",
    ),
    ("scale_x", "Box width multiplier", "Scale the drawing box width relative to the original box."),
    ("scale_y", "Box height multiplier", "Scale the drawing box height relative to the original box."),
    ("box_padding_x", "Horizontal outer padding", "Add source pixels to each side of the drawing box."),
    ("box_padding_y", "Vertical outer padding", "Add source pixels above and below the drawing box."),
    (
        "font_scale",
        "Target font multiplier",
        "Relative to the estimated original font size. Grow the box before reducing the text size.",
    ),
    ("horizontal", "Horizontal alignment", "Align the text and anchor horizontal box growth."),
    ("vertical", "Vertical alignment", "Top grows down, center grows both ways, bottom grows up."),
    (
        "text_padding_x",
        "Horizontal inner padding",
        "Text inset from each horizontal edge in source pixels; zero is allowed.",
    ),
    (
        "text_padding_y",
        "Vertical inner padding",
        "Text inset from each vertical edge in source pixels; zero is allowed.",
    ),
    (
        "max_growth",
        "Automatic growth limit",
        "Maximum automatic width or height multiplier after manual box adjustments.",
    ),
    (
        "avoid_overlap",
        "Avoid neighboring content",
        "Share whitespace between detected boxes and avoid detected images, tables and formulas.",
    ),
):
    register_catalog(
        {
            f"settings.f.overlay_layout.{_field}.title": {"en": _title},
            f"settings.f.overlay_layout.{_field}.desc": {"en": _description},
        }
    )
