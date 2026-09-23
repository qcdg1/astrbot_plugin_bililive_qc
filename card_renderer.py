"""开播通知卡片渲染：把直播间封面 + 直播信息画成一张白框卡片图。

设计：
- 白色圆角卡片承载全部内容，卡片顶部是「圆形头像 + B 站昵称」，
  下面是直播标题、直播间封面（圆角、16:9 cover 裁剪）、人气/分区标签、
  累计观看/粉丝/房间号信息、开播时间。
- 卡片上不写"开播啦"和配置里的备注名（群消息正文已经带了）。
- 另有「长条形」单行图 `render_live_strip()`，供 /liveinfo 指令使用：
  每行一个主播，只显示圆形头像、直播标题、B 站昵称、粉丝数和状态。
- 中文字体自动探测（Windows 用微软雅黑，Linux 用 Noto CJK，macOS 用苹方）。
- emoji 用 Windows 的 seguiemj.ttf 单独渲染并贴入（雅黑不含 emoji 字形）。

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
    """12000 -> 1.2万；1080000 -> 108万；1.2e8 -> 1.2亿"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "0"
    if n < 0:
        return "0"
    if n >= 100000000:
        return f"{n / 100000000:.1f}亿"
    if n >= 100000000 // 10:
        v = n / 10000
        return f"{v:.1f}万" if v < 100 else f"{v:.0f}万"
    if n >= 10000:
        return f"{n / 10000:.1f}万"
    return str(n)


# ---------------------------------------------------------------- 文本绘制（含 emoji 回退）
class Painter:
    """封装带 emoji 回退的文本绘制与测宽。"""

    def __init__(self, draw: "ImageDraw.ImageDraw", base_font, emoji_size: int | None = None):
        self.draw = draw
        self.font = base_font
        self.emoji_size = emoji_size or getattr(base_font, "size", 24)
        self.emoji_font = get_emoji_font(self.emoji_size)

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
            if _is_emoji(ch) and self.emoji_font is not None:
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
            if _is_emoji(ch) and self.emoji_font is not None:
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


def _pill(draw, painter: Painter, xy, text: str, bg, fg=(255, 255, 255)):
    """画圆角药丸标签，返回 (右边界x, 底边界y)。"""
    pad_x, pad_y = 18, 9
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
    watched_show: int = 0,
    follower: int = 0,
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
    # 底部只留开播时间一行（直播间地址不画进卡片）
    has_time = bool(live_time_text and live_time_text != "未知")
    extra_h = 44 if has_time else 20

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
    p_small.set_target(draw)
    px, _ = _pill(draw, p_small, (x, y), "LIVE 直播中", (251, 114, 153))
    if online:
        px, _ = _pill(draw, p_small, (px + 10, y), f"🔥 {human_count(online)} 人气", (255, 140, 62))
    area_text = (
        f"{parent_area_name} · {area_name}"
        if parent_area_name and area_name and parent_area_name != area_name
        else (area_name or parent_area_name or "")
    )
    if area_text:
        _pill(draw, p_small, (px + 10, y), area_text, (108, 122, 158))
    y += pills_h

    # ---- 信息三列 ----
    col_w = inner_w // 3
    stats = [
        ("累计观看", human_count(watched_show) if watched_show else "—"),
        ("主播粉丝", human_count(follower) if follower else "—"),
        ("房间号", str(room_id) if room_id else "—"),
    ]
    p_body.set_target(draw)
    for i, (label, value) in enumerate(stats):
        cx = x + col_w * i
        p_small.draw_text((cx, y + 12), label, (156, 158, 165), anchor="lm")
        p_body.draw_text((cx, y + 46), value, (46, 46, 52), anchor="lm")
    y += info_h

    # ---- 开播时间 ----
    if live_time_text and live_time_text != "未知":
        p_small.draw_text((x, y + 8), f"开播于 {live_time_text}", (138, 140, 147), anchor="lm")

    # 直播间地址不画进卡片：群消息正文里已经带了链接，避免重复

    out = io.BytesIO()
    canvas.save(out, format="PNG", optimize=True)
    return out.getvalue()


# ---------------------------------------------------------------- 长条信息卡（/liveinfo 用）
# 一行一个主播：圆形头像 | 直播标题 | B站昵称 · 累计观看 · 粉丝数 | 状态药丸
STRIP_WIDTH = 920
STRIP_ROW_H = 132
STRIP_ROW_H_OFFLINE = 176   # 下播款：标题 + 昵称/累计观看/粉丝 + 开播/下播/时长
STRIP_PAD_X = 28
STRIP_AVATAR = 80

# 在线款白底，下播款浅灰底（视觉上区分开播 / 下播）
STRIP_BG_ONLINE = (255, 255, 255)
STRIP_BG_OFFLINE = (238, 240, 243)

_STATUS_COLORS = {
    0: (150, 154, 163),   # 未开播 - 灰
    1: (251, 114, 153),   # 直播中 - 粉
    2: (108, 122, 158),   # 轮播中 - 蓝灰
}


def render_live_strip(
    *,
    anchor_name: str = "主播",
    bili_name: str = "",
    title: str = "",
    live_status: int = 0,
    follower: int = 0,
    watched_show: int = 0,
    avatar_bytes: bytes | None = None,
    room_id: str = "",
    variant: str = "online",
    live_time_text: str = "",
    end_time_text: str = "",
    duration_text: str = "",
) -> bytes:
    """渲染单行「长条形」主播信息图。

    显示：圆形头像、直播标题、B 站昵称、粉丝数、累计观看、状态。
    返回 PNG bytes；调用方把多行纵向拼接后一起发出。

    variant="offline" 时改用浅灰底，并把开播/下播时间合成一行、外加直播时长，
    用于下播通知。
    """
    if Image is None:
        raise RuntimeError("未安装 pillow，无法渲染卡片")

    offline = variant == "offline"
    row_h = STRIP_ROW_H_OFFLINE if offline else STRIP_ROW_H

    f_name = get_font(26)
    f_title = get_font(28)
    f_meta = get_font(21)

    d0 = ImageDraw.Draw(Image.new("RGB", (10, 10)))
    p_name = Painter(d0, f_name)
    p_title = Painter(d0, f_title)
    p_meta = Painter(d0, f_meta)

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
    title_line = (_wrap(title or "无标题", p_title, title_max_w, max_lines=1) or ["无标题"])[0]

    watched_text = f"累计观看 {human_count(watched_show)}" if watched_show else "累计观看 —"
    follower_text = f"粉丝 {human_count(follower)}" if follower else "粉丝 —"
    meta_text = f"{display_name}"
    meta_suffix = f"  ·  {watched_text}  ·  {follower_text}"

    bg = STRIP_BG_OFFLINE if offline else STRIP_BG_ONLINE
    canvas = Image.new("RGB", (STRIP_WIDTH, row_h), bg)
    draw = ImageDraw.Draw(canvas)

    p_name.set_target(draw)
    p_title.set_target(draw)
    p_meta.set_target(draw)

    if offline:
        # 三行：标题 / 昵称·累计观看·粉丝 / 开播+下播时间、时长
        title_y = row_h // 2 - 46
        meta_y = title_y + 36
        time_y = meta_y + 30
        dur_y = time_y + 27
    else:
        # 两行：标题 / 昵称·累计观看·粉丝
        title_y = row_h // 2 - 34
        meta_y = title_y + 40
        time_y = dur_y = None

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

    # 昵称（深色）+ 粉丝数（灰色），接在同一行
    end_x = p_meta.draw_text((tx, meta_y), meta_text, (92, 96, 106), anchor="lm")
    p_meta.draw_text((end_x, meta_y), meta_suffix, (156, 158, 165), anchor="lm")

    if offline and time_y is not None:
        start_t = live_time_text or "未知"
        end_t = end_time_text or "未知"
        dur_t = duration_text or "未知"
        # 开播 / 下播合成一行，时长单独一行
        for text_line, yy in (
            (f"开播：{start_t}    下播：{end_t}", time_y),
            (f"本次直播时长：{dur_t}", dur_y),
        ):
            fitted = (_wrap(text_line, p_meta, text_w, max_lines=1) or [text_line])[0]
            p_meta.draw_text((tx, yy), fitted, (150, 153, 161), anchor="lm")

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

    items: [{anchor_name, bili_name, title, live_status, follower, avatar_url, room_id}, ...]
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
                    follower=it.get("follower") or 0,
                    watched_show=it.get("watched_show") or 0,
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
