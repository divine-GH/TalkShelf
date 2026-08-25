"""生成 PWA 图标（设计文档 §38）：numpy 绘制 + stdlib 手写 PNG（无 Pillow 依赖）。

图形：蓝底（style.css 品牌色 --primary #2f6fed）+ 白色「书架与书」简笔（TalkShelf 寓意）；
全部元素位于 maskable 安全区（中心半径 40% 圆）内，同一张图同时声明 any / maskable。

输出（入库，改动设计后重新运行本脚本即可重新生成）：
  static/icons/icon-192.png（Android 安装图标）
  static/icons/icon-512.png（各尺寸源，manifest 主图标）
  static/icons/apple-touch-icon.png（180x180，iOS 添加到主屏幕用）

运行（numpy 是运行时依赖，用项目 venv）：
  & 'E:\\note-brain\\note-brain\\.venv\\Scripts\\python.exe' scripts/gen_icons.py
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import numpy as np

BG = (0x2F, 0x6F, 0xED)  # style.css --primary
FG = (0xFF, 0xFF, 0xFF)
SUPERSAMPLE = 4  # 4x 超采样 + 盒式降采样 = 边缘抗锯齿

# 「书架与书」几何（比例坐标 x0,y0,x1,y1，全部位于中心 80% 圆内，满足 maskable 安全区）
RECTS = [
    (0.26, 0.36, 0.36, 0.72),  # 左书
    (0.44, 0.28, 0.56, 0.72),  # 中书（更高）
    (0.64, 0.40, 0.74, 0.72),  # 右书
    (0.24, 0.72, 0.76, 0.78),  # 书架
]

ROOT = Path(__file__).resolve().parent.parent
OUTPUTS: dict[Path, int] = {
    ROOT / "static" / "icons" / "icon-192.png": 192,
    ROOT / "static" / "icons" / "icon-512.png": 512,
    ROOT / "static" / "icons" / "apple-touch-icon.png": 180,
}


def render(size: int) -> np.ndarray:
    """渲染 size x size RGB 图（uint8）：超采样布尔掩码（矩形并集）→ 盒式降采样。"""
    w = size * SUPERSAMPLE
    # mgrid 默认 int64；转 float32 控制内存（2048² x4B x2 = 32MB，一次性脚本可接受）
    yy, xx = np.mgrid[0:w, 0:w].astype(np.float32) / w
    mask = np.zeros((w, w), dtype=bool)
    for x0, y0, x1, y1 in RECTS:
        mask |= (xx >= x0) & (xx < x1) & (yy >= y0) & (yy < y1)
    cov = mask.reshape(size, SUPERSAMPLE, size, SUPERSAMPLE).mean(axis=(1, 3))
    rgb = np.empty((size, size, 3), dtype=np.uint8)
    for c in range(3):
        rgb[..., c] = np.rint(BG[c] * (1 - cov) + FG[c] * cov).astype(np.uint8)
    return rgb


def write_png(path: Path, rgb: np.ndarray) -> None:
    """手写 PNG（8-bit RGB）：IHDR + IDAT(zlib) + IEND，无第三方图像库依赖。"""
    h, w = rgb.shape[:2]
    raw = bytearray()
    for y in range(h):
        raw.append(0)  # 每行滤波器类型：None
        raw += rgb[y].tobytes()

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def main() -> None:
    for path, size in OUTPUTS.items():
        path.parent.mkdir(parents=True, exist_ok=True)
        write_png(path, render(size))
        data = path.read_bytes()
        assert data[:8] == b"\x89PNG\r\n\x1a\n", f"PNG 签名错误: {path}"
        print(f"OK  {path.relative_to(ROOT)}  {size}x{size}  {len(data)} bytes")


if __name__ == "__main__":
    main()
