"""在 .venv-ocr 里运行的辅助脚本：按 JSON 描述渲染一张合成的“文档截图”，把 BGRA 像素写到标准输出。

测试进程用主环境运行，主环境没有 Pillow；OCR 环境有。所以测试把页面描述（JSON）写进这个脚本的标准输入，
用 `.venv-ocr\\Scripts\\python.exe tests/workers/ocr/render_document.py` 运行它，读回 width × height × 4 字节
的 BGRA 像素（行与行之间没有填充）。期望的识别结果由测试按同一份描述计算。

页面描述的格式：

```json
{
  "width": 1280, "height": 900, "font": "C:/Windows/Fonts/msyh.ttc",
  "items": [
    {"type": "text", "x": 80, "y": 60, "size": 40, "line_height": 56, "lines": ["第一行", "第二行"]},
    {"type": "table", "x": 80, "y": 400, "size": 24, "col_widths": [260, 260, 260], "row_height": 60,
     "border": 2, "rows": [["名称", "数量", "价格"], ["苹果", "12", "3.50"]]}
  ]
}
```

文字一律黑色，背景白色；表格画全部边框，单元格文字距左边框 20 像素、垂直方向大致居中。

本脚本只在 OCR 环境里运行，Pillow 通过 importlib 导入并按下面的协议使用，主环境的类型检查不依赖 Pillow。
"""

import importlib
import json
import sys
from typing import Protocol, cast


class _Font(Protocol): ...


class _Image(Protocol):
    def convert(self, mode: str) -> "_Image": ...

    def tobytes(self, encoder_name: str, *args: str) -> bytes: ...


class _Draw(Protocol):
    def text(self, xy: tuple[float, float], text: str, *, font: _Font, fill: str) -> None: ...

    def rectangle(self, xy: tuple[float, float, float, float], *, outline: str, width: int) -> None: ...


class _ImageModule(Protocol):
    def new(self, mode: str, size: tuple[int, int], color: str) -> _Image: ...


class _ImageDrawModule(Protocol):
    def Draw(self, im: _Image) -> _Draw: ...


class _ImageFontModule(Protocol):
    def truetype(self, font: str, size: int) -> _Font: ...


def render(spec: dict[str, object]) -> bytes:
    """按页面描述渲染，返回 BGRA 像素（stride = width × 4）。"""
    image_module = cast(_ImageModule, importlib.import_module("PIL.Image"))
    draw_module = cast(_ImageDrawModule, importlib.import_module("PIL.ImageDraw"))
    font_module = cast(_ImageFontModule, importlib.import_module("PIL.ImageFont"))

    width, height = cast(int, spec["width"]), cast(int, spec["height"])
    font_path = cast(str, spec["font"])
    image = image_module.new("RGB", (width, height), "white")
    draw = draw_module.Draw(image)
    for item in cast(list[dict[str, object]], spec["items"]):
        size = cast(int, item["size"])
        font = font_module.truetype(font_path, size)
        x, y = cast(int, item["x"]), cast(int, item["y"])
        if item["type"] == "text":
            line_height = cast(int, item["line_height"])
            for index, line in enumerate(cast(list[str], item["lines"])):
                draw.text((x, y + index * line_height), line, font=font, fill="black")
        elif item["type"] == "table":
            col_widths = cast(list[int], item["col_widths"])
            row_height = cast(int, item["row_height"])
            border = cast(int, item.get("border", 2))
            for r, row in enumerate(cast(list[list[str]], item["rows"])):
                left = x
                for c, text in enumerate(row):
                    top = y + r * row_height
                    draw.rectangle(
                        (left, top, left + col_widths[c], top + row_height), outline="black", width=border
                    )
                    draw.text(
                        (left + 20, top + (row_height - size) / 2 - size * 0.15),
                        text,
                        font=font,
                        fill="black",
                    )
                    left += col_widths[c]
        else:
            raise ValueError(f"未知的元素类型：{item['type']!r}")
    return image.convert("RGBA").tobytes("raw", "BGRA")


def main() -> None:
    spec = cast(dict[str, object], json.loads(sys.stdin.buffer.read().decode("utf-8")))
    sys.stdout.buffer.write(render(spec))
    sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
