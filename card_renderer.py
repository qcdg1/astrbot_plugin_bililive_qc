"""开播通知卡片渲染：把直播间封面 + 直播信息画成一张白框卡片图。

设计：
- 白色圆角卡片承载全部内容，卡片顶部是「圆形头像 + B 站昵称」，
  下面是直播标题、直播间封面（圆角、16:9 cover 裁剪）、人气/分区标签、
  累计观看/粉丝/房间号信息、开播时间。
- 卡片上不写"开播啦"和配置里的备注名（群消息正文已经带了）。
- 另有「长条形」单行图 `render_live_strip()`，供 /liveinfo 指令使用：
  每行一个主播，只显示圆形头像、直播标题、B 站昵称、累计观看、粉丝数和状态。
- 数字口径（别混）：
  * `online`  = **加权人气值**，B站官方标「人气」，非人数 → 卡片标「N 人气」+ 火苗
  * `watched_show` = "N人看过"，**真实去重人数** → 标「累计观看」
  B站不对外暴露真实并发在线人数，别把 online 当在线人数用。
- 中文字体自动探测（Windows 用微软雅黑，Linux 用 Noto CJK，macOS 用苹方）。
- **小尺寸图标用代码画矢量图形**（`ICON_FLAME` 等），不依赖 emoji 字体：
  Windows 的 seguiemj.ttf 是 COLR/CPAL 纯矢量彩色字体，Pillow 的 getmask()
  只能取到轮廓层（彩色底衬），放大后就是一个方块。详见 make_flame_icon。
- 大尺寸正文 emoji 仍走 seguiemj 位图回退（雅黑不含 emoji 字形）。

依赖：pillow；封面下载由 main.py 用 aiohttp 完成后把 bytes 传进来。
找不到中文字体时应变差（中文变方块），可在插件配置 font_path 指定。
"""

from __future__ import annotations

import asyncio
import io
import os
import platform
import unicodedata
from datetime import datetime

try:
    from PIL import Image, ImageDraw, ImageFont
except Exception:  # pragma: no cover
    Image = None

# 卡片尺寸（2 倍图，发到 QQ / 微信里更清晰）
CARD_WIDTH = 920
MARGIN = 30
PAD = 26           # 卡片内左右留白
RADIUS = 20
COVER_RADIUS = 14


# ---------------------------------------------------------------- 字体
_FONT_CACHE: dict[tuple[str, int], object] = {}
_RESOLVED_FONT_PATH: str | None = None
_EMOJI_FONT_PATH: str | None = None
_FONT_SEARCHED = False

_WIN_CN = [
    r"C:\Windows\Fonts\msyhbd.ttc",
    r"C:\Windows\Fonts\msyh.ttc",
    r"C:\Windows\Fonts\msyhl.ttc",
    r"C:\Windows\Fonts\Deng.ttf",
    r"C:\Windows\Fonts\SourceHanSansSC-Bold.otf",
    r"C:\Windows\Fonts\simhei.ttf",
]
_WIN_EMOJI = [
    r"C:\Windows\Fonts\seguiemj.ttf",
]
_LINUX_CN = [
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
    "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]
_LINUX_EMOJI = [
    "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf",
    "/usr/share/fonts/truetype/ancient-scripts/Symbola_hint.ttf",
]
_MAC_CN = [
    "/System/Library/Fonts/PingFang.ttc",
    "/System/Library/Fonts/STHeiti Medium.ttc",
    "/Library/Fonts/Arial Unicode.ttf",
]
_MAC_EMOJI = [
    "/System/Library/Fonts/Apple Color Emoji.ttc",
]


def _system_font_lists() -> tuple[list[str], list[str]]:
    system = platform.system()
    if system == "Windows":
        return _WIN_CN, _WIN_EMOJI
    if system == "Darwin":
        return _MAC_CN, _MAC_EMOJI
    return _LINUX_CN, _LINUX_EMOJI


def _pick(paths: list[str]) -> str | None:
    for p in paths:
        if p and os.path.isfile(p):
            return p
    return None


def resolve_font_path() -> str | None:
    """探测可用中文字体路径（只探测一次）。"""
    global _RESOLVED_FONT_PATH, _EMOJI_FONT_PATH, _FONT_SEARCHED
    if _FONT_SEARCHED:
        return _RESOLVED_FONT_PATH
    _FONT_SEARCHED = True
    cn, emoji = _system_font_lists()
    env = os.environ.get("BILI_LIVE_FONT")
    _RESOLVED_FONT_PATH = (env if env and os.path.isfile(env) else None) or _pick(cn)
    env_emoji = os.environ.get("BILI_LIVE_EMOJI_FONT")
    _EMOJI_FONT_PATH = (
        env_emoji if env_emoji and os.path.isfile(env_emoji) else None
    ) or _pick(emoji)
    return _RESOLVED_FONT_PATH


def set_font_path(path: str | None, emoji_path: str | None = None):
    """允许插件配置覆盖字体路径。"""
    global _RESOLVED_FONT_PATH, _EMOJI_FONT_PATH, _FONT_SEARCHED
    if path and os.path.isfile(path):
        _RESOLVED_FONT_PATH = path
        _FONT_SEARCHED = True
    if emoji_path and os.path.isfile(emoji_path):
        _EMOJI_FONT_PATH = emoji_path
    _FONT_CACHE.clear()


def get_font(size: int):
    key = (_RESOLVED_FONT_PATH or "", size)
    if key not in _FONT_CACHE:
        fp = resolve_font_path()
        font = None
        if fp:
            try:
                font = ImageFont.truetype(fp, size)
            except Exception:
                font = None
        if font is None:
            try:
                font = ImageFont.load_default(size)
            except Exception:
                font = ImageFont.load_default()
        _FONT_CACHE[key] = font
    return _FONT_CACHE[key]


def get_emoji_font(size: int):
    """emoji 字体；Windows 的 seguiemj 是固定 109px 点阵，不能按任意 size 载入。"""
    resolve_font_path()
    if not _EMOJI_FONT_PATH:
        return None
    key = ("EMOJI", size)
    if key in _FONT_CACHE:
        return _FONT_CACHE[key]
    font = None
    # Windows 段式 emoji 字体只接受固定尺寸
    sizes = [size] if platform.system() != "Windows" else [109]
    for s in sizes:
        try:
            font = ImageFont.truetype(_EMOJI_FONT_PATH, s)
            break
        except Exception:
            font = None
    _FONT_CACHE[key] = font
    return font


def _is_emoji(ch: str) -> bool:
    """粗略判断是否为 emoji / 符号类字符。"""
    cp = ord(ch)
    if cp < 0x2000:
        return False
    # 常见 emoji 区间 + 杂项符号
    return (
        0x1F000 <= cp <= 0x1FAFF
        or 0x2600 <= cp <= 0x27BF
        or 0x2B00 <= cp <= 0x2BFF
        or 0xFE0F == cp
        or 0x200D == cp
        or 0x2190 <= cp <= 0x21FF
    )


# ---------------------------------------------------------------- 数值格式化
def human_count(n) -> str:
    """12000 -> 1.2万；1080000 -> 108万；1.2e8 -> 1.2亿

    注意 1 亿的判定要在四舍五入**之前**：99999999 若先转成万会得到 "10000万"，
    所以先用 1 亿的 0.9995 倍做阈值（保证四舍五入后不进位到 1 亿时仍用万）。
    """
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "0"
    if n < 0:
        return "0"
    # 先算万的显示，若四舍五入到 >= 10000 万，就进位到亿
    if n >= 10000:
        wan = n / 10000
        if wan >= 9999.5:
            return f"{n / 100000000:.1f}亿"
        if wan < 100:
            return f"{wan:.1f}万"
        return f"{wan:.0f}万"
    return str(n)


# ---------------------------------------------------------------- 内置矢量图标
# 为什么不用 emoji 字体：
# Windows 的 seguiemj.ttf 是 COLR/CPAL **纯矢量彩色**字体（不含 CBDT 彩色点阵），
# Pillow 的 getmask() 只能取到它的**轮廓层** —— 也就是那个"彩色底衬"，
# 不是火苗图形本身；而且点阵只有 ~15px，放大到药丸字号就糊成一个方块。
# 所以小尺寸图标一律用代码画，不依赖任何外部字体。
def make_flame_icon(size: int, color=(255, 255, 255)) -> "Image.Image":
    """画一个小火苗图标，返回 RGBA（4 倍超采样后缩放，边缘平滑）。

    用于开播卡片「人气」药丸 —— 不再依赖 emoji 字体。
    """
    s = max(4, size) * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    cx = s * 0.5
    w = s * 0.52          # 火苗最大宽度
    top = s * 0.04        # 火苗尖端
    bot = s * 0.96        # 火苗底部

    def P(fx: float, fy: float) -> tuple[float, float]:
        return (cx + fx * w, top + fy * (bot - top))

    # 外焰：底部圆润饱满、顶部收成尖，左侧有一处内凹的焰舌
    outer = [
        P(-0.10, 0.62),
        P(-0.20, 0.80),
        P(-0.06, 0.97),
        P(0.22, 1.00),
        P(0.50, 0.92),
        P(0.60, 0.72),
        P(0.58, 0.50),
        P(0.40, 0.30),
        P(0.22, 0.12),
        P(0.10, 0.00),
        P(0.02, 0.24),
        P(-0.10, 0.36),
        P(-0.20, 0.48),
    ]
    d.polygon(outer, fill=tuple(color) + (255,))

    # 内焰：挖透明，做出"火芯"的月牙缺口，小尺寸下也能一眼认出是火焰
    inner = [
        P(-0.02, 0.60),
        P(-0.10, 0.76),
        P(0.06, 0.92),
        P(0.30, 0.88),
        P(0.36, 0.72),
        P(0.28, 0.58),
        P(0.16, 0.52),
    ]
    d.polygon(inner, fill=(0, 0, 0, 0))

    return img.resize((max(4, size), max(4, size)), Image.LANCZOS)


def make_live_dot(size: int, color=(255, 255, 255)) -> "Image.Image":
    """实心圆点，用于「直播中」等状态前缀。"""
    s = max(4, size) * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((0, 0, s - 1, s - 1), fill=tuple(color) + (255,))
    return img.resize((max(4, size), max(4, size)), Image.LANCZOS)


def make_thumb_up_icon(size: int, color=(255, 255, 255)) -> "Image.Image":
    """画一个「点赞」大拇指图标，返回 RGBA。

    用圆 + 圆角矩形拼出实心拇指轮廓；小尺寸下形状比细节重要，
    所以不做手指分缝，保证 21px 也认得出是点赞。
    """
    s = max(4, size) * 4
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    c = tuple(color) + (255,)

    # 掌心 + 下方手掌：一个竖圆角矩形
    palm = (s * 0.30, s * 0.42, s * 0.86, s * 0.98)
    d.rounded_rectangle(palm, radius=s * 0.10, fill=c)
    # 竖起的大拇指：左上斜出去的圆角矩形（用椭圆近似，小尺寸够看）
    d.ellipse((s * 0.28, s * 0.06, s * 0.62, s * 0.50), fill=c)
    # 拇指根部与手掌的过渡
    d.rounded_rectangle(
        (s * 0.30, s * 0.34, s * 0.56, s * 0.62), radius=s * 0.09, fill=c
    )
    # 左侧小臂/袖口：一条短竖条，让整体不像一个孤立的圆
    d.rounded_rectangle(
        (s * 0.10, s * 0.56, s * 0.30, s * 0.98), radius=s * 0.07, fill=c
    )

    return img.resize((max(4, size), max(4, size)), Image.LANCZOS)


def _paste_scaled(painter: "Painter", icon: "Image.Image", x: float, center_y: float, target=None) -> float:
    """把矢量图标按当前字号贴到 (x, center_y)，返回下一格 x。"""
    h = max(1, int(painter.emoji_size * 1.05))
    w = max(1, int(icon.width * h / icon.height)) if icon.height else h
    icon2 = icon.resize((w, h), Image.LANCZOS)
    dst = target if target is not None else painter.draw
    dst._image.paste(icon2, (int(x), int(center_y - h / 2)), icon2)
    return x + w + painter.emoji_size * 0.14


# ---------------------------------------------------------------- 文本绘制（含 emoji 回退）
# 内置矢量图标：文本里写这些私有区占位符，Painter 会替换成代码画的图形，
# 不依赖任何外部字体，小字号下也清晰。
# 用法示例：f"{ICON_FLAME} {human_count(online)} 人气"
ICON_FLAME = "\ue000"   # 小火苗（用于「人气」药丸）
ICON_DOT = "\ue001"     # 实心圆点（适合表示状态/时间轴）
ICON_LIKE = "\ue002"    # 点赞大拇指
_ICON_BUILDERS = {
    ICON_FLAME: make_flame_icon,
    ICON_DOT: make_live_dot,
    ICON_LIKE: make_thumb_up_icon,
}


class Painter:
    """封装带 emoji 回退的文本绘制与测宽。"""

    def __init__(self, draw: "ImageDraw.ImageDraw", base_font, emoji_size: int | None = None):
        self.draw = draw
        self.font = base_font
        self.emoji_size = emoji_size or getattr(base_font, "size", 24)
        self.emoji_font = get_emoji_font(self.emoji_size)
        self._icon_cache: dict[str, "Image.Image"] = {}

    def _icon(self, ch: str, fill=None) -> "Image.Image | None":
        """取内置矢量图标（按 字号+颜色 缓存）。

        颜色必须进缓存键：图标和文字同色才自然，
        如果只按字号缓存、复用白色图标，画在白底上就完全看不见。
        """
        if ch not in _ICON_BUILDERS:
            return None
        if fill is None:
            fill = (255, 255, 255)
        try:
            color = tuple(int(c) for c in fill[:3])
        except (TypeError, ValueError):
            color = (255, 255, 255)
        key = f"{ch}:{self.emoji_size}:{color}"
        if key not in self._icon_cache:
            self._icon_cache[key] = _ICON_BUILDERS[ch](self.emoji_size, color)
        return self._icon_cache[key]

    def _emoji_glyph(self, ch: str, fill) -> "Image.Image":
        """把 emoji 字形取出来做成一张已着色、已缩放到目标高度的 RGBA 小图。"""
        ef = self.emoji_font
        raw = ef.getmask(ch)
        src = Image.frombytes("L", raw.size, bytes(raw))
        # 描边抗锯齿：低阈值保留边缘，再把对比拉满，避免糊成一团
        src = src.point(lambda v: 255 if v > 60 else 0)
        bb = src.getbbox()
        if not bb:
            return None
        src = src.crop(bb)

        target_h = max(1, int(self.emoji_size * 1.05))
        w, h = src.size
        target_w = max(1, int(w * target_h / h)) if h else target_h
        src = src.resize((target_w, target_h), Image.LANCZOS)

        colored = Image.new("RGBA", src.size, tuple(fill) + (255,))
        colored.putalpha(src)
        return colored

    def width(self, text: str) -> float:
        w = 0.0
        for ch in text:
            if ch in _ICON_BUILDERS:
                w += self.emoji_size * 1.05 + self.emoji_size * 0.14
            elif _is_emoji(ch) and self.emoji_font is not None:
                w += self._emoji_adv(ch)
            else:
                w += self.font.getlength(ch)
        return w

    def _emoji_adv(self, ch: str) -> float:
        """emoji 占位宽度：和字形实际宽度 + 一点间距保持一致。"""
        ef = self.emoji_font
        if ef is None:
            return self.font.getlength(ch)
        try:
            raw = ef.getmask(ch)
            src = Image.frombytes("L", raw.size, bytes(raw))
            src = src.point(lambda v: 255 if v > 60 else 0)
            bb = src.getbbox()
            if bb:
                h = bb[3] - bb[1]
                w = bb[2] - bb[0]
                scale = (self.emoji_size * 1.05) / h if h else 1
                return w * scale + self.emoji_size * 0.14
        except Exception:
            pass
        return self.emoji_size * 1.1

    def set_target(self, draw: "ImageDraw.ImageDraw"):
        """切换绘制目标。"""
        self.draw = draw

    def draw_text(self, xy, text: str, fill, anchor: str | None = None, draw=None):
        """逐字绘制，emoji 用位图贴入；返回结束 x。

        draw 显式传入时以它为准，否则用 self.draw。
        """
        target = draw if draw is not None else self.draw
        x, y = xy
        baseline = y + self.emoji_size * 0.34  # 让 emoji 视觉居中于文字
        for ch in text:
            if ch in _ICON_BUILDERS:
                icon = self._icon(ch, fill)
                if icon is not None:
                    x = _paste_scaled(self, icon, x, y, target)
            elif _is_emoji(ch) and self.emoji_font is not None:
                glyph = self._emoji_glyph(ch, fill)
                if glyph is not None:
                    gw, gh = glyph.size
                    target._image.paste(
                        glyph, (int(x), int(baseline - gh / 2)), glyph
                    )
                x += self._emoji_adv(ch)
            else:
                target.text((x, y), ch, font=self.font, fill=fill, anchor="lm")
                x += self.font.getlength(ch)
        return x


def _wrap(text: str, painter: Painter, max_width: float, max_lines: int = 2) -> list[str]:
    """按像素宽度折行，超出 max_lines 时末行加省略号。"""
    if not text:
        return []
    lines: list[str] = []
    cur = ""
    for ch in text.replace("\r", ""):
        if ch == "\n":
            lines.append(cur)
            cur = ""
            if len(lines) == max_lines:
                break
            continue
        if painter.width(cur + ch) <= max_width:
            cur += ch
        else:
            if len(lines) + 1 == max_lines:
                cur += ch
                break
            lines.append(cur)
            cur = ch
    if cur and len(lines) < max_lines:
        lines.append(cur)
    elif cur and len(lines) == max_lines:
        pass

    used = "".join(lines)
    if len(used) < len(text.replace("\n", "")):
        last = lines[-1]
        while last and painter.width(last + "…") > max_width:
            last = last[:-1]
        lines[-1] = last + "…"
    return lines


def _rounded_mask(size: tuple[int, int], radius: int) -> "Image.Image":
    mask = Image.new("L", size, 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, size[0] - 1, size[1] - 1), radius=radius, fill=255
    )
    return mask


def _fit_cover(img: "Image.Image", size: tuple[int, int]) -> "Image.Image":
    """等比缩放 + 居中裁剪（cover）。"""
    tw, th = size
    sw, sh = img.size
    if sw <= 0 or sh <= 0:
        return Image.new("RGB", size, (242, 243, 245))
    scale = max(tw / sw, th / sh)
    nw, nh = max(1, int(sw * scale + 0.5)), max(1, int(sh * scale + 0.5))
    img = img.resize((nw, nh), Image.LANCZOS)
    left, top = (nw - tw) // 2, (nh - th) // 2
    return img.crop((left, top, left + tw, top + th))


def _circle_avatar(img: "Image.Image", size: int) -> "Image.Image":
    """把头像裁成圆形，返回 RGBA（带抗锯齿边缘）。"""
    s = size * 4  # 4 倍超采样，圆边更平滑
    src = _fit_cover(img, (s, s)).convert("RGBA")
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, s - 1, s - 1), fill=255)
    src.putalpha(mask)
    return src.resize((size, size), Image.LANCZOS)


def _circle_avatar_from_url(
    session_or_bytes, size: int, size_out: int | None = None
) -> "Image.Image | None":
    """从 bytes 构造圆形头像（同步工具函数）。"""
    if not session_or_bytes:
        return None
    try:
        raw = Image.open(io.BytesIO(session_or_bytes))
        if raw.mode != "RGBA":
            raw = raw.convert("RGB")
        return _circle_avatar(raw, size_out or size)
    except Exception:
        return None


def _pill(draw, painter: Painter, xy, text: str, bg, fg=(255, 255, 255), pad_y: int = 9):
    """画圆角药丸标签，返回 (右边界x, 底边界y)。

    pad_y 控制上下留白：长条图里胶囊夹在密集文字行之间，要调小（如 5）
    才不会顶到上下的字；开播卡片空间宽裕，用默认 9。
    """
    pad_x = 18
    w = int(painter.width(text)) + pad_x * 2
    h = painter.emoji_size + pad_y * 2
    x, y = xy
    draw.rounded_rectangle((x, y, x + w, y + h), radius=h // 2, fill=bg)
    painter.draw_text((x + pad_x, y + h // 2), text, fg, anchor="lm")
    return x + w, y + h


# ---------------------------------------------------------------- 主入口
def render_live_card(
    *,
    anchor_name: str = "主播",
    bili_name: str = "",
    title: str = "",
    cover_bytes: bytes | None = None,
    avatar_bytes: bytes | None = None,
    area_name: str = "",
    parent_area_name: str = "",
    online: int = 0,
    watched_show: int | None = 0,
    likes: int | None = 0,
    follower: int | None = 0,
    live_time_text: str = "",
    room_id: str = "",
    live_url: str = "",
) -> bytes:
    """渲染开播卡片，返回 PNG bytes。

    卡片顶部为「头像 + B 站昵称」，不放"开播啦"和配置里的备注名
    （这两者群消息正文已经带了）。

    anchor_name:  插件配置里给这个主播起的昵称，仅作为 bili_name 缺失时的兜底
    bili_name:    B 站上的真实昵称，卡片上优先显示它
    avatar_bytes: 主播头像图片字节；无则画占位圆
    online:       人气值（B站 online 字段）。
                  **这是加权热度，不是在线人数**，卡片上以「N 人气」+ 火苗展示；
                  真实人数口径见 watched_show。
    watched_show: 本场累计观看人数（"N人看过"），真实去重人数。
    likes:        本场点赞数（like_info_v3.total_likes），真实计数。

    watched_show / likes / follower 传 None 表示"拿不到"（显示 "—"），
    传 0 则显示 0。开播卡片当前只展示 follower，另两个留着保持口径一致。
    """
    if Image is None:
        raise RuntimeError("未安装 pillow，无法渲染卡片")

    inner_w = CARD_WIDTH - MARGIN * 2 - PAD * 2
    f_brand = get_font(38)
    f_title = get_font(33)
    f_body = get_font(23)
    f_small = get_font(20)

    d0 = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    p_brand = Painter(d0, f_brand)
    p_title = Painter(d0, f_title)
    p_body = Painter(d0, f_body)
    p_small = Painter(d0, f_small)

    bili_name = (bili_name or "").strip()
    title_lines = _wrap(title or "无标题", p_title, inner_w, max_lines=2)

    # 头像放左侧，昵称区在右
    avatar_d = 76
    avatar_gap = 18
    text_x_off = avatar_d + avatar_gap
    text_w = inner_w - text_x_off

    # 顶部只显示 B 站昵称（配置里的备注名和"开播啦"都不上卡片，正文里已经有了）
    display_name = bili_name or (anchor_name or "").strip() or "主播"
    name_lines = _wrap(display_name, p_brand, text_w - 60, max_lines=1)
    name_line = name_lines[0] if name_lines else display_name
    head_h = avatar_d + 18

    title_h = len(title_lines) * 46
    cover_w = inner_w
    cover_h = int(cover_w * 9 / 16)
    pills_h = 52
    info_h = 96
    # 开播时间已并入上面的信息行，底部不再单独占一行，只留一点下边距
    extra_h = 24

    card_h = MARGIN * 2 + 26 + head_h + title_h + 22 + cover_h + 18 + pills_h + info_h + extra_h

    canvas = Image.new("RGB", (CARD_WIDTH, card_h), (233, 235, 240))
    draw = ImageDraw.Draw(canvas)

    # 卡片底色（白色框）
    draw.rounded_rectangle(
        (MARGIN, MARGIN, CARD_WIDTH - MARGIN, card_h - MARGIN),
        radius=RADIUS,
        fill=(255, 255, 255),
    )

    x = MARGIN + PAD
    y = MARGIN + 26

    # ---- 顶部：头像 + 昵称 ----
    p_brand.set_target(draw)
    p_small.set_target(draw)

    avatar = None
    if avatar_bytes:
        avatar = _circle_avatar_from_url(avatar_bytes, avatar_d)
    if avatar is not None:
        canvas.paste(avatar, (x, y + 6), avatar)
    else:
        # 无头像兜底：浅粉圆底 + 圆点
        draw.ellipse(
            (x, y + 6, x + avatar_d, y + 6 + avatar_d), fill=(252, 228, 236)
        )
        r = 9
        cx = x + avatar_d // 2
        cyy = y + 6 + avatar_d // 2
        draw.ellipse((cx - r, cyy - r, cx + r, cyy + r), fill=(251, 114, 153))

    tx = x + text_x_off
    # 昵称与头像垂直居中对齐
    p_brand.draw_text(
        (tx, y + 6 + avatar_d // 2), name_line, (26, 26, 30), anchor="lm"
    )

    y += head_h

    # ---- 直播标题（在封面之上）----
    p_title.set_target(draw)
    for line in title_lines:
        p_title.draw_text((x, y + 22), line, (36, 36, 42), anchor="lm")
        y += 46
    y += 22

    # ---- 封面 ----
    cover_img = None
    if cover_bytes:
        try:
            raw = Image.open(io.BytesIO(cover_bytes))
            if raw.mode != "RGB":
                raw = raw.convert("RGB")
            cover_img = _fit_cover(raw, (cover_w, cover_h))
        except Exception:
            cover_img = None

    if cover_img is not None:
        canvas.paste(cover_img, (x, y), _rounded_mask((cover_w, cover_h), COVER_RADIUS))
    else:
        draw.rounded_rectangle(
            (x, y, x + cover_w, y + cover_h),
            radius=COVER_RADIUS,
            fill=(244, 245, 248),
            outline=(226, 228, 233),
            width=2,
        )
        p_body.draw_text(
            (x + cover_w // 2, y + cover_h // 2),
            "封面加载失败",
            (152, 154, 160),
            anchor="lm",
        )
    y += cover_h + 18

    # ---- 药丸：LIVE / 人气 / 分区 ----
    # 火苗用小矢量图标（ICON_FLAME），不依赖 emoji 字体 —— 见 make_flame_icon。
    #
    # 口径说明（重要）：这里标「人气」而不是「当前观看」。
    # B站的 online 是**加权人气值**（弹幕/礼物/活跃度/停留时长综合算出的热度），
    # 不是实时在线人数，实测比真实人数大 10~25 倍（如 10.4万人气 vs 4576人看过）。
    # 标成「当前观看」会让人误以为是人数，还会出现"当前比累计还多"的矛盾。
    # 真实人数口径请看下方信息列的「累计观看」（watched_show = "N人看过"）。
    p_small.set_target(draw)
    px, _ = _pill(draw, p_small, (x, y), "LIVE 直播中", (251, 114, 153))
    if online:
        px, _ = _pill(
            draw, p_small, (px + 10, y),
            f"{ICON_FLAME} {human_count(online)} 人气", (255, 140, 62),
        )
    area_text = (
        f"{parent_area_name} · {area_name}"
        if parent_area_name and area_name and parent_area_name != area_name
        else (area_name or parent_area_name or "")
    )
    if area_text:
        _pill(draw, p_small, (px + 10, y), area_text, (108, 122, 158))
    y += pills_h

    # ---- 信息双列 ----
    # 开播卡片不放「累计观看 / 点赞」：开播瞬间这两个值几乎为 0（尚未开播或刚开播），
    # 展示出来是噪音。它们的实时值放在 /liveinfo 长条图和下播结算图里。
    #
    # 开播时间和房间号一样，做成同一行的列（label 小灰字 + value 大字），
    # 比单独占一行更紧凑，视觉上也和上面几个指标成一排。
    stats = [
        # 粉丝用 is not None 判断：0 是有效值（显示 0），只有拿不到才显示 "—"
        ("主播粉丝", human_count(follower) if follower is not None else "—"),
        ("房间号", str(room_id) if room_id else "—"),
        ("开播时间", live_time_text if live_time_text and live_time_text != "未知" else "—"),
    ]
    col_w = inner_w // len(stats)
    p_body.set_target(draw)
    for i, (label, value) in enumerate(stats):
        cx = x + col_w * i
        p_small.draw_text((cx, y + 12), label, (156, 158, 165), anchor="lm")
        # 开播时间比纯数字长，超出列宽时按列宽裁掉尾巴（带省略号）
        shown = value
        if p_body.width(shown) > col_w - 12:
            ell_w = p_body.width("…")
            while shown and p_body.width(shown) + ell_w > col_w - 12:
                shown = shown[:-1]
            shown = shown.rstrip() + "…"
        p_body.draw_text((cx, y + 46), shown, (46, 46, 52), anchor="lm")
    y += info_h

    # 直播间地址不画进卡片：群消息正文里已经带了链接，避免重复

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


# ---------------------------------------------------------------- 长条信息卡（/liveinfo 用）
# 一行一个主播：圆形头像 | 直播标题 | B站昵称 · 累计观看 · 粉丝数 | 状态药丸
STRIP_WIDTH = 920
STRIP_ROW_H = 132
# 直播中的款多一行「人气 · 点赞 · 累计观看」，所以更高一些，避免和上下行挤在一起
STRIP_ROW_H_LIVE = 170
STRIP_ROW_H_OFFLINE = 176   # 下播款：标题 + 昵称/粉丝 + 开播/下播/时长（+胶囊）
STRIP_PAD_X = 28
STRIP_AVATAR = 80

# 在线款白底，下播款浅灰底（视觉上区分开播 / 下播）
STRIP_BG_ONLINE = (255, 255, 255)
STRIP_BG_OFFLINE = (238, 240, 243)

# 下播款胶囊的上下留白。长条图三行文字排得很密（行距约 27~30px），
# 用 _pill 默认的 pad_y=9 会让胶囊高到顶住上下两行字，这里压到 4 做得扁一点。
STRIP_CAP_PAD_Y = 4

_STATUS_COLORS = {
    0: (150, 154, 163),   # 未开播 - 灰
    1: (251, 114, 153),   # 直播中 - 粉
    2: (108, 122, 158),   # 轮播中 - 蓝灰
}


def _offline_caps(watched_show, likes):
    """下播款时长行右侧要画的胶囊列表，返回 [(文字, 底色), ...]。

    watched_show / likes 为 None 表示拿不到 → 显示 "—"；为 0 则显示 0。
    两者都拿不到时返回空列表，干脆不画胶囊，避免一整排 "--" 更难读。
    """
    caps = []
    if watched_show is not None:
        caps.append((f"累计观看 {human_count(watched_show)}", (94, 114, 164)))
    if likes is not None:
        caps.append((f"点赞 {human_count(likes)}", (232, 105, 138)))
    return caps


def render_live_strip(
    *,
    anchor_name: str = "主播",
    bili_name: str = "",
    title: str = "",
    live_status: int = 0,
    follower: int | None = 0,
    watched_show: int | None = 0,
    likes: int | None = 0,
    online: int | None = None,
    avatar_bytes: bytes | None = None,
    room_id: str = "",
    variant: str = "online",
    live_time_text: str = "",
    end_time_text: str = "",
    duration_text: str = "",
) -> bytes:
    """渲染单行「长条形」主播信息图。

    显示：圆形头像、直播标题、B 站昵称、累计观看、点赞、粉丝数、状态。
    返回 PNG bytes；调用方把多行纵向拼接后一起发出。

    展示规则：
      - 粉丝数：任何状态都展示（第一行，昵称后）
      - 人气 / 点赞 / 累计观看：**只在直播中（live_status==1）展示**，
        单独占第二行，顺序为「人气 · 点赞 · 累计观看」。
        未开播/轮播时累计观看是上一场残留值、点赞恒为 0、人气也是 0，
        展示出来是噪音甚至误导。
      - 下播款（variant="offline"）：这几个数改用胶囊画在别处
        （人气在粉丝后，累计观看/点赞在时长后）

    follower / watched_show / likes 传 None 表示"这项拿不到"，显示 "—"；
    传 0 则原样显示 "0"（0 是有意义的信息：本场还没人点赞）。

    online: 人气值（加权热度，**不是在线人数**）。直播中在第二行以「人气 N」展示；
            下播款则画成橙色火苗胶囊放在粉丝后面。

    variant="offline" 时改用浅灰底，并把开播/下播时间合成一行、外加直播时长，
    用于下播通知。
    """
    if Image is None:
        raise RuntimeError("未安装 pillow，无法渲染卡片")

    offline = variant == "offline"
    # 行高在算出 stat_text（是否需要第二行）后再定，见下方

    f_name = get_font(26)
    f_title = get_font(28)
    f_meta = get_font(21)
    f_cap = get_font(19)   # 下播款胶囊文字（比 meta 小一号）

    d0 = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    p_name = Painter(d0, f_name)
    p_title = Painter(d0, f_title)
    p_meta = Painter(d0, f_meta)
    p_cap = Painter(d0, f_cap)

    inner_w = STRIP_WIDTH - STRIP_PAD_X * 2
    text_x_off = STRIP_AVATAR + 22
    text_w = inner_w - text_x_off

    display_name = (bili_name or "").strip() or (anchor_name or "").strip() or "主播"

    # 右侧放状态药丸，给标题留出它的宽度
    # 药丸内不用 emoji（小字号下位图 emoji 会糊），状态文字本身已经够清楚
    status_label = {
        0: "未开播",
        1: "直播中",
        2: "轮播中",
    }.get(live_status, "未知")
    pill_pad_x, pill_pad_y = 16, 8
    pill_w = int(p_meta.width(status_label)) + pill_pad_x * 2
    pill_h = f_meta.size + pill_pad_y * 2
    title_max_w = text_w - pill_w - 18

    # 标题行：`备注名称 | 标题`（备注名取配置里的昵称，标题为直播间标题）
    remark_name = (anchor_name or "").strip() or "主播"
    title_text = (title or "").strip() or "无标题"
    head_line_raw = f"{remark_name} | {title_text}"
    title_line = (_wrap(head_line_raw, p_title, title_max_w, max_lines=1) or [head_line_raw])[0]

    # 元信息行（第一行）：昵称 · 粉丝
    # 第二行（仅直播中）：人气 · 点赞 · 累计观看
    #
    # 「人气」直接标成"人气"（不标"当前观看/在线人数"）：
    # B站的 online 是**加权人气值**（热度），不是在线人数，实测比真实人数大 10~25 倍。
    # 标成人数会和「累计观看」并列产生"当前比累计还多"的矛盾。
    #
    # 第二行只在本场直播中（live_status==1）展示：
    #   - 未开播/轮播时累计观看是上一场残留值、点赞恒为 0、人气也是 0，
    #     展示出来是噪音甚至误导
    #   - 下播款（offline）改用胶囊画在别处，这里也不再拼
    # 粉丝数则任何状态都展示（和是否在播无关）。
    #
    # 计数一律显示真实数字：拿不到（None）才给 "—"，返回 0 就显示 0。
    # 因为 0 本身是有意义的信息（本场还没人点赞 / 还没人看过）。
    show_counters = (not offline) and live_status == 1
    parts = [display_name]
    parts.append(f"粉丝 {human_count(follower)}" if follower is not None else "粉丝 —")
    meta_text = parts[0]
    meta_suffix = "  ·  " + "  ·  ".join(parts[1:])

    # 第二行内容（人气 · 点赞 · 累计观看），未开播/轮播/下播款时为空
    stat_parts = []
    if show_counters:
        stat_parts.append(
            f"人气 {human_count(online)}" if online is not None else "人气 —"
        )
        stat_parts.append(f"点赞 {human_count(likes)}" if likes is not None else "点赞 —")
        stat_parts.append(f"累计观看 {human_count(watched_show)}" if watched_show is not None else "累计观看 —")
    stat_text = "  ·  ".join(stat_parts)

    # 行高：下播款固定高；在线款有第二行(直播中)时更高，否则沿用矮版
    if offline:
        row_h = STRIP_ROW_H_OFFLINE
    elif stat_text:
        row_h = STRIP_ROW_H_LIVE
    else:
        row_h = STRIP_ROW_H

    bg = STRIP_BG_OFFLINE if offline else STRIP_BG_ONLINE
    canvas = Image.new("RGB", (STRIP_WIDTH, row_h), bg)
    draw = ImageDraw.Draw(canvas)

    p_name.set_target(draw)
    p_title.set_target(draw)
    p_meta.set_target(draw)
    p_cap.set_target(draw)

    if offline:
        # 三行：标题 / 昵称·粉丝(+人气胶囊) / 开播+下播时间、时长(+结算胶囊)
        title_y = row_h // 2 - 46
        meta_y = title_y + 36
        time_y = meta_y + 30
        dur_y = time_y + 27
        stat_y = None
        # 胶囊在所在行垂直居中（pill 高 = 字号 + pad_y*2）
        cap_h = f_cap.size + STRIP_CAP_PAD_Y * 2
        cap_y = dur_y - cap_h // 2
    else:
        # 两行（直播中）/ 一行（未开播）：标题 / 昵称·粉丝（+第二行 人气·点赞·累计观看）
        if stat_text:
            # 三行整体在行内垂直居中（行高 170，块高约 91）
            title_y = row_h // 2 - 31
            meta_y = title_y + 36
            stat_y = meta_y + 30
        else:
            title_y = row_h // 2 - 34
            meta_y = title_y + 40
            stat_y = None
        time_y = dur_y = cap_y = None

    # 头像（下播款与文字块中心对齐，在线款与整行居中）
    if offline:
        av_center = (title_y + dur_y) // 2
    else:
        av_center = row_h // 2
    av_y = av_center - STRIP_AVATAR // 2
    avatar = None
    if avatar_bytes:
        avatar = _circle_avatar_from_url(avatar_bytes, STRIP_AVATAR)
    if avatar is not None:
        canvas.paste(avatar, (STRIP_PAD_X, av_y), avatar)
    else:
        draw.ellipse(
            (STRIP_PAD_X, av_y, STRIP_PAD_X + STRIP_AVATAR, av_y + STRIP_AVATAR),
            fill=(252, 228, 236),
        )
        r = 10
        cx = STRIP_PAD_X + STRIP_AVATAR // 2
        cyy = av_y + STRIP_AVATAR // 2
        draw.ellipse((cx - r, cyy - r, cx + r, cyy + r), fill=(251, 114, 153))

    tx = STRIP_PAD_X + text_x_off

    p_title.draw_text((tx, title_y), title_line, (30, 30, 36), anchor="lm")

    # 昵称（深色）+ 元信息（灰色），接在同一行。
    # 元信息更长时按「扣除右侧状态药丸后的剩余宽度」裁掉尾巴，
    # 否则文字会钻到药丸底下。
    name_w = p_meta.width(meta_text)
    avail_w = max(0, text_w - pill_w - 18 - name_w)
    suffix = meta_suffix
    if p_meta.width(suffix) > avail_w:
        # 先留出省略号的宽度，再从尾部逐字裁，保证最后一定带省略号
        ell_w = p_meta.width("…")
        while suffix and p_meta.width(suffix) + ell_w > avail_w:
            suffix = suffix[:-1]
        suffix = suffix.rstrip("· ").rstrip() + "…"

    end_x = p_meta.draw_text((tx, meta_y), meta_text, (92, 96, 106), anchor="lm")
    p_meta.draw_text((end_x, meta_y), suffix, (156, 158, 165), anchor="lm")

    # 第二行（仅直播中）：人气 · 点赞 · 累计观看。
    # 这行没有别的元素占用，可用宽度是整条 text_w，超了才裁。
    if stat_text and stat_y is not None:
        stat_shown = stat_text
        if p_meta.width(stat_shown) > text_w:
            ell_w = p_meta.width("…")
            while stat_shown and p_meta.width(stat_shown) + ell_w > text_w:
                stat_shown = stat_shown[:-1]
            stat_shown = stat_shown.rstrip("· ").rstrip() + "…"
        p_meta.draw_text((tx, stat_y), stat_shown, (156, 158, 165), anchor="lm")

    # 下播款：在「粉丝」后面接一个和开播卡片一样的人气胶囊（火苗 + N 人气）。
    # 人气放这里而不是时长行，是因为它和昵称/粉丝同属"主播身份 & 热度"这一类；
    # 时长行留给「累计观看 / 点赞」这类"本场结算"数据。
    # online 为 None 表示整场都没拿到 → 不画；有值（含 0）就画。
    if offline and online is not None:
        online_cap_x = end_x + p_meta.width(suffix) + 16
        cap_right_edge = STRIP_WIDTH - STRIP_PAD_X - pill_w - 16
        cap_text = f"{ICON_FLAME} {human_count(online)} 人气"
        cap_h = f_cap.size + STRIP_CAP_PAD_Y * 2
        if online_cap_x + p_cap.width(cap_text) + 16 <= cap_right_edge:
            _pill(draw, p_cap, (online_cap_x, meta_y - cap_h // 2),
                  cap_text, (255, 140, 62), pad_y=STRIP_CAP_PAD_Y)

    if offline and time_y is not None:
        start_t = live_time_text or "未知"
        end_t = end_time_text or "未知"
        dur_t = duration_text or "未知"
        # 开播/下播合成一行（灰色小字），时长那行右侧接胶囊
        cap_line = f"开播：{start_t}    下播：{end_t}"
        fitted = (_wrap(cap_line, p_meta, text_w, max_lines=1) or [cap_line])[0]
        p_meta.draw_text((tx, time_y), fitted, (150, 153, 161), anchor="lm")

        # 时长行：文字 + 后面的胶囊（累计观看 / 点赞）。
        # 右侧状态药丸占位要扣掉，胶囊不能压到它下面。
        dur_line = f"本次直播时长：{dur_t}"
        p_meta.draw_text((tx, dur_y), dur_line, (150, 153, 161), anchor="lm")
        cap_x = tx + p_meta.width(dur_line) + 22
        cap_right = STRIP_WIDTH - STRIP_PAD_X - pill_w - 16
        for text, bgc in _offline_caps(watched_show, likes):
            cap_w_text = p_cap.width(text) + pill_pad_x * 2
            if cap_x + cap_w_text > cap_right:
                break
            cap_x, _ = _pill(draw, p_cap, (cap_x, cap_y), text, bgc,
                             pad_y=STRIP_CAP_PAD_Y)
            cap_x += 10

    # 状态药丸（右侧垂直居中）
    pill_x = STRIP_WIDTH - STRIP_PAD_X - pill_w
    pill_y = (row_h - pill_h) // 2
    draw.rounded_rectangle(
        (pill_x, pill_y, pill_x + pill_w, pill_y + pill_h),
        radius=pill_h // 2,
        fill=_STATUS_COLORS.get(live_status, (150, 154, 163)),
    )
    p_meta.draw_text(
        (pill_x + pill_pad_x, pill_y + pill_h // 2), status_label, (255, 255, 255), anchor="lm"
    )

    # 底部分隔线（最后一行由调用方处理，这里统一画，视觉上更像列表）
    draw.line(
        [(STRIP_PAD_X, row_h - 1), (STRIP_WIDTH - STRIP_PAD_X, row_h - 1)],
        fill=(238, 240, 244),
        width=1,
    )

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


def combine_vertical(images: list[bytes], gap: int = 0) -> bytes:
    """把多张同宽图片纵向拼接成一张（用于把多行长条合成一条消息图）。"""
    imgs = []
    for b in images:
        try:
            imgs.append(Image.open(io.BytesIO(b)).convert("RGB"))
        except Exception:
            continue
    if not imgs:
        return b""
    width = max(i.width for i in imgs)
    height = sum(i.height for i in imgs) + gap * max(0, len(imgs) - 1)
    canvas = Image.new("RGB", (width, height), (255, 255, 255))
    y = 0
    for i in imgs:
        canvas.paste(i, (0, y))
        y += i.height + gap
    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


async def render_live_strip_async(
    *,
    items: list[dict],
    session=None,
    headers: dict | None = None,
    gap: int = 0,
) -> bytes:
    """批量渲染长条并纵向拼接。

    items: [{anchor_name, bili_name, title, live_status, follower,
             watched_show, likes, online, avatar_url, room_id}, ...]

    其中 follower / watched_show / likes 传 None 表示"拿不到"，渲染成 "—"；
    传 0 则显示 0。
    """
    prepared: list[dict] = []
    for it in items:
        prepared.append(dict(it))

    # 并发下载所有头像
    if session is not None:
        tasks = [
            download_image(session, it.get("avatar_url") or "", headers)
            if it.get("avatar_url")
            else asyncio.sleep(0, result=None)
            for it in prepared
        ]
        avatars = await asyncio.gather(*tasks)
        for it, av in zip(prepared, avatars):
            it["avatar_bytes"] = av

    def _render_all() -> bytes:
        rows = []
        for it in prepared:
            rows.append(
                render_live_strip(
                    anchor_name=it.get("anchor_name") or "主播",
                    bili_name=it.get("bili_name") or "",
                    title=it.get("title") or "",
                    live_status=it.get("live_status") or 0,
                    # 计数类字段直接透传：None → 渲染成 "—"，0 → 显示 0。
                    # 这里**不能**用 `or 0`，否则拿不到的字段会被当成 0 显示。
                    follower=it.get("follower"),
                    watched_show=it.get("watched_show"),
                    likes=it.get("likes"),
                    online=it.get("online"),
                    avatar_bytes=it.get("avatar_bytes"),
                    room_id=str(it.get("room_id") or ""),
                )
            )
        return combine_vertical(rows, gap=gap)

    return await asyncio.to_thread(_render_all)


async def render_strip_row_async(
    *,
    session=None,
    avatar_url: str = "",
    headers: dict | None = None,
    **kwargs,
) -> bytes:
    """渲染单行长条（下载头像后在线程池渲染）。

    下播通知只需要一行，用这个比 render_live_strip_async 更直接。
    """
    avatar_bytes = None
    if session is not None and avatar_url:
        avatar_bytes = await download_image(session, avatar_url, headers)

    return await asyncio.to_thread(
        render_live_strip, avatar_bytes=avatar_bytes, **kwargs
    )


async def download_image(session, url: str, headers: dict | None = None) -> bytes | None:
    """下载图片字节流，带一次重试；失败返回 None。"""
    if not url:
        return None
    import aiohttp

    timeout = aiohttp.ClientTimeout(total=20)
    for attempt in range(2):
        try:
            async with session.get(
                url, headers=headers, timeout=timeout, allow_redirects=True
            ) as resp:
                if resp.status == 200:
                    data = await resp.read()
                    if data:
                        return data
                if attempt == 0 and resp.status in (403, 412, 429, 500, 502, 503):
                    await asyncio.sleep(1.0)
                    continue
                return None
        except Exception:
            if attempt == 0:
                await asyncio.sleep(0.8)
                continue
            return None
    return None


async def render_live_card_async(
    *,
    session=None,
    cover_url: str = "",
    avatar_url: str = "",
    headers: dict | None = None,
    **kwargs,
) -> bytes:
    """先下载封面与主播头像（并发），再放到线程池里渲染（PIL 是同步阻塞的）。"""
    cover_bytes = None
    avatar_bytes = None

    if session is not None:
        tasks = []
        if cover_url:
            tasks.append(download_image(session, cover_url, headers))
        else:
            tasks.append(asyncio.sleep(0, result=None))
        if avatar_url:
            tasks.append(download_image(session, avatar_url, headers))
        else:
            tasks.append(asyncio.sleep(0, result=None))
        cover_bytes, avatar_bytes = await asyncio.gather(*tasks)

    return await asyncio.to_thread(
        render_live_card,
        cover_bytes=cover_bytes,
        avatar_bytes=avatar_bytes,
        **kwargs,
    )


def now_text() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


__all__ = [
    "render_live_card",
    "render_live_card_async",
    "render_live_strip",
    "render_live_strip_async",
    "render_strip_row_async",
    "combine_vertical",
    "download_image",
    "human_count",
    "resolve_font_path",
    "set_font_path",
    "now_text",
    "CARD_WIDTH",
    "STRIP_WIDTH",
]
