import asyncio
import json
import random
import re
from datetime import datetime

import aiohttp
from astrbot.api.event import filter, AstrMessageEvent, MessageChain
from astrbot.api.message_components import Plain, Json, Image
from astrbot.api.star import Context, Star, register
from astrbot.api import logger

try:
    # 同目录模块（AstrBot 以插件目录为包导入）
    from . import card_renderer
except ImportError:  # pragma: no cover - 兼容直接以脚本方式加载
    import card_renderer

BILI_LIVE_INFO_API = "https://api.live.bilibili.com/room/v1/Room/get_info"
BILI_MASTER_INFO_API = "https://api.live.bilibili.com/live_user/v1/Master/info"
BILI_RELATION_API = "https://api.bilibili.com/x/relation/stat"
BILI_NAV_API = "https://api.bilibili.com/x/web-interface/nav"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/131.0.0.0 Safari/537.36"
    ),
    "Referer": "https://live.bilibili.com/",
    "Origin": "https://live.bilibili.com",
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
}


def parse_live_time(live_time) -> str:
    """安全解析 live_time 字段，兼容字符串和时间戳整数。

    返回值统一为 "%Y-%m-%d %H:%M:%S"（秒级），
    因为轮询状态机还要拿它再 strptime 一次；
    展示到分钟由调用方用 fmt_minute() 处理。
    """
    if live_time is None:
        return "未知"
    if isinstance(live_time, str):
        try:
            dt = datetime.strptime(live_time, "%Y-%m-%d %H:%M:%S")
            return dt.strftime("%Y-%m-%d %H:%M:%S")
        except ValueError:
            return live_time
    if isinstance(live_time, (int, float)):
        try:
            return datetime.fromtimestamp(live_time).strftime("%Y-%m-%d %H:%M:%S")
        except (OSError, ValueError):
            return str(live_time)
    return str(live_time)


def fmt_minute(dt) -> str:
    """时间展示到分钟：2026-09-23 16:45:56 -> 2026-09-23 16:45"""
    if dt is None:
        return "未知"
    if isinstance(dt, str):
        try:
            dt = datetime.strptime(dt, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            return dt if dt else "未知"
    try:
        return dt.strftime("%Y-%m-%d %H:%M")
    except (AttributeError, ValueError):
        return "未知"


def format_duration(seconds: int) -> str:
    """把秒数格式化成中文时长。"""
    if seconds < 0:
        seconds = 0
    if seconds < 60:
        return f"{seconds}秒"
    hours, remainder = divmod(seconds, 3600)
    minutes, _ = divmod(remainder, 60)
    if hours > 0:
        return f"{hours}小时{minutes}分钟" if minutes else f"{hours}小时"
    return f"{minutes}分钟"


@register("bililive_qc", "qcdg", "bilibili上下播通知", "1.0.0")
class BiliLiveMonitor(Star):
    def __init__(self, context: Context, config: dict):
        super().__init__(context, config)
        self.config = config or {}
        self.session: aiohttp.ClientSession | None = None
        self.monitor_task: asyncio.Task | None = None
        self.last_status: dict[int, int] = {}
        self.live_start_time: dict[int, datetime] = {}
        # 开播时缓存的信息（昵称/标题/粉丝/头像），供下播渲染长条图复用
        self.live_on_info: dict[int, dict] = {}
        self._buvid_cookies: str | None = None
        self._platform_id: str | None = None

    # ---------- 生命周期 ----------
    async def initialize(self):
        logger.info("[BiliLiveMonitor] initialize() 进入")
        try:
            # 允许通过配置覆盖字体路径
            font_path = (self.config.get("font_path") or "").strip()
            emoji_font_path = (self.config.get("emoji_font_path") or "").strip()
            if font_path or emoji_font_path:
                card_renderer.set_font_path(
                    font_path or None, emoji_font_path or None
                )
            detected = card_renderer.resolve_font_path()
            if detected:
                logger.info(f"[BiliLiveMonitor] 卡片字体: {detected}")
            else:
                logger.warning(
                    "[BiliLiveMonitor] 未找到中文字体，卡片中文可能显示为方块，"
                    "请在插件配置 font_path 中指定字体文件路径"
                )

            self.session = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=15)
            )
            self.monitor_task = asyncio.create_task(self._monitor_loop())
            logger.info("[BiliLiveMonitor] 监控任务已启动")
        except Exception as e:
            logger.error(
                f"[BiliLiveMonitor] initialize 失败: {e}", exc_info=True
            )

    async def terminate(self):
        try:
            if self.monitor_task:
                self.monitor_task.cancel()
                try:
                    await self.monitor_task
                except asyncio.CancelledError:
                    pass
            if self.session:
                await self.session.close()
            logger.info("[BiliLiveMonitor] 监控任务已停止")
        except Exception as e:
            logger.error(f"[BiliLiveMonitor] terminate 出错: {e}")

    # ---------- 平台 ID 获取 ----------
    def _get_platform_id(self) -> str | None:
        if self._platform_id:
            return self._platform_id
        try:
            platforms = self.context.platform_manager.get_insts()
            for p in platforms:
                try:
                    meta = p.meta()
                except Exception:
                    continue
                name = (getattr(meta, "name", "") or "").lower()
                pid = getattr(meta, "id", "") or ""
                if "aiocqhttp" in name or "aiocqhttp" in pid.lower():
                    self._platform_id = pid
                    logger.info(
                        f"[BiliLiveMonitor] 找到平台适配器: name={name}, id={pid}"
                    )
                    return pid
        except Exception as e:
            logger.error(f"[BiliLiveMonitor] 获取平台 ID 失败: {e}")
        return None

    # ---------- Cookie 准备 ----------
    async def _ensure_cookies(self):
        if self._buvid_cookies:
            return
        if self.session is None:
            return
        try:
            async with self.session.get(
                BILI_NAV_API, headers=DEFAULT_HEADERS
            ) as resp:
                raw = resp.headers.getall("Set-Cookie", [])
            parts = []
            for c in raw:
                m = re.match(r"(buvid[34]=[^;]+)", c)
                if m:
                    parts.append(m.group(1))
            if parts:
                self._buvid_cookies = "; ".join(parts)
                logger.info("[BiliLiveMonitor] 已获取 buvid Cookie")
        except Exception as e:
            logger.warning(f"[BiliLiveMonitor] 获取 buvid Cookie 失败: {e}")

    # ---------- 配置解析 ----------
    def _parse_room_line(self, line) -> tuple[str, str, list[str]]:
        """解析一行直播间配置：房间号 | 昵称 | 群号1|群号2|群号3

        群号同时兼容 | 和 , / ， / 空格 分隔，方便直接粘贴多个群。
        """
        if not isinstance(line, str):
            return "", "", []
        parts = [p for p in line.split("|") if p.strip() != ""] or line.split("|")
        if len(parts) < 3:
            # 兼容旧格式：房间号|昵称  → 没有群
            return "", "", []
        room_id = parts[0].strip()
        anchor_name = parts[1].strip() or "主播"
        raw_groups = "|".join(parts[2:])
        group_ids = [
            g
            for g in re.split(r"[|,，、\s]+", raw_groups)
            if g.strip()
        ]
        return room_id, anchor_name, [g.strip() for g in group_ids]

    # ---------- 消息发送 ----------
    async def _send_group_text(self, gid_int: int, text: str) -> bool:
        platform_id = self._get_platform_id()
        if not platform_id:
            logger.error("[BiliLiveMonitor] 未找到 aiocqhttp 平台")
            return False
        umo = f"{platform_id}:GroupMessage:{gid_int}"
        try:
            await self.context.send_message(umo, MessageChain([Plain(text)]))
            return True
        except Exception as e:
            logger.error(f"[BiliLiveMonitor] 向群 {gid_int} 发文本失败: {e}")
            return False

    async def _send_group_text_with_image(
        self, gid_int: int, text: str, image_url: str = "", image_bytes: bytes | None = None
    ) -> bool:
        """发送「文本 + 图片」组合消息，同一条消息框内。

        优先使用渲染好的 image_bytes，其次退回 image_url。
        """
        platform_id = self._get_platform_id()
        if not platform_id:
            logger.error("[BiliLiveMonitor] 未找到 aiocqhttp 平台")
            return False
        umo = f"{platform_id}:GroupMessage:{gid_int}"

        # 按顺序组合：文本 → 图片
        chain = [Plain(text)]
        try:
            if image_bytes:
                try:
                    chain.append(Image.fromBytes(image_bytes))
                except Exception:
                    if image_url:
                        chain.append(Image.fromURL(image_url))
            elif image_url:
                chain.append(Image.fromURL(image_url))
        except Exception as e:
            logger.warning(f"[BiliLiveMonitor] 构造图片组件失败，仅发文本: {e}")

        try:
            await self.context.send_message(umo, MessageChain(chain))
            return True
        except Exception as e:
            logger.error(
                f"[BiliLiveMonitor] 向群 {gid_int} 发组合消息失败: {e}"
            )
            # 降级：至少把文本发出去
            if len(chain) > 1:
                try:
                    await self.context.send_message(
                        umo, MessageChain([Plain(text)])
                    )
                    logger.info(
                        f"[BiliLiveMonitor] 已降级为纯文本发送到群 {gid_int}"
                    )
                    return True
                except Exception as e2:
                    logger.error(
                        f"[BiliLiveMonitor] 降级发送文本也失败: {e2}"
                    )
            return False

    # ---------- 监控循环 ----------
    async def _monitor_loop(self):
        logger.info("[BiliLiveMonitor] 监控循环开始")
        while True:
            try:
                rooms = self.config.get("rooms", [])
                interval = self.config.get("check_interval", 30)
                logger.info(
                    f"[BiliLiveMonitor] 本轮检查 {len(rooms)} 个直播间"
                )

                for line in rooms:
                    room_id, anchor_name, group_ids = self._parse_room_line(line)
                    if not room_id or not group_ids:
                        continue

                    try:
                        info = await self._fetch_live_status(room_id)
                    except Exception as e:
                        logger.error(
                            f"[BiliLiveMonitor] 获取直播间 {room_id} 信息失败: {e}"
                        )
                        continue

                    live_status = info["live_status"]
                    title = info["title"]
                    live_time_str = info["live_time"]
                    cover_url = info["cover"]

                    try:
                        room_key = int(room_id)
                    except ValueError:
                        continue

                    old_status = self.last_status.get(room_key)
                    self.last_status[room_key] = live_status
                    logger.info(
                        f"[BiliLiveMonitor] 房间 {room_id} 状态 {old_status} -> {live_status}"
                    )

                    if old_status is None:
                        continue

                    if old_status == 0 and live_status == 1:
                        start_dt = None
                        if live_time_str and live_time_str != "未知":
                            try:
                                start_dt = datetime.strptime(
                                    live_time_str, "%Y-%m-%d %H:%M:%S"
                                )
                            except ValueError:
                                start_dt = None
                        if start_dt is None:
                            start_dt = datetime.now()
                        self.live_start_time[room_key] = start_dt
                        # 缓存开播时的信息，下播渲染长条图时复用（下播后接口已无头像/标题）
                        self.live_on_info[room_key] = {
                            "bili_name": info.get("bili_name") or "",
                            "title": info.get("title") or "",
                            "follower": info.get("follower") or 0,
                            "watched_show": info.get("watched_show") or 0,
                            "face": info.get("face") or "",
                        }
                        await self._notify_live_on(
                            room_id, anchor_name, group_ids, info
                        )
                    elif old_status == 1 and live_status == 0:
                        start_dt = self.live_start_time.pop(room_key, None)
                        end_dt = datetime.now()
                        cached = self.live_on_info.pop(room_key, {}) or {}
                        await self._notify_live_off(
                            room_id,
                            anchor_name,
                            group_ids,
                            start_dt,
                            end_dt,
                            info=info,
                            cached=cached,
                        )

                await asyncio.sleep(interval)

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(
                    f"[BiliLiveMonitor] 监控循环异常: {e}", exc_info=True
                )
                await asyncio.sleep(10)

    # ---------- B站 API ----------
    def _headers(self) -> dict:
        headers = dict(DEFAULT_HEADERS)
        if self._buvid_cookies:
            headers["Cookie"] = self._buvid_cookies
        return headers

    async def _get_json(self, url: str, params: dict, retry_on_412: bool = True) -> dict:
        """带 buvid 重试的 JSON GET。"""
        if self.session is None:
            raise RuntimeError("session 未初始化")

        await self._ensure_cookies()
        headers = self._headers()

        async with self.session.get(url, params=params, headers=headers) as resp:
            if resp.status == 412 and retry_on_412:
                self._buvid_cookies = None
                await self._ensure_cookies()
                await asyncio.sleep(random.uniform(2.0, 4.0))
                async with self.session.get(
                    url, params=params, headers=self._headers()
                ) as resp2:
                    resp2.raise_for_status()
                    return await resp2.json()
            resp.raise_for_status()
            return await resp.json()

    async def _fetch_live_status(self, room_id: str) -> dict:
        """拉取直播间信息。返回 dict，包含卡片渲染需要的全部字段。"""
        params = {"room_id": room_id}
        await asyncio.sleep(random.uniform(0.5, 1.5))
        data = await self._get_json(BILI_LIVE_INFO_API, params)

        if data.get("code") != 0:
            raise RuntimeError(
                f"B站接口返回错误: code={data.get('code')}, msg={data.get('message')}"
            )

        room_info = data.get("data") or {}
        uid = room_info.get("uid")

        info = {
            "room_id": str(room_info.get("room_id") or room_id),
            "short_id": str(room_info.get("short_id") or "0"),
            "uid": str(uid or ""),
            "title": room_info.get("title") or "无标题",
            "live_status": room_info.get("live_status", 0),
            "live_time": parse_live_time(room_info.get("live_time")),
            "cover": room_info.get("user_cover") or room_info.get("cover") or "",
            "keyframe": room_info.get("keyframe") or "",
            "area_name": room_info.get("area_name") or "",
            "parent_area_name": room_info.get("parent_area_name") or "",
            "online": room_info.get("online") or 0,
            "watched_show": room_info.get("watched_show") or 0,
            "description": room_info.get("description") or "",
            "tags": room_info.get("tags") or "",
            "follower": 0,
            # 主播头像（get_info 的 face 字段）与 B 站真实昵称
            "face": room_info.get("face") or "",
            "bili_name": room_info.get("uname") or "",
        }

        # 主播粉丝数 + 昵称/头像兜底（免登录接口，失败不影响主流程）
        if uid:
            try:
                rel = await self._get_json(
                    BILI_RELATION_API, {"vmid": uid}, retry_on_412=False
                )
                if rel.get("code") == 0:
                    info["follower"] = (rel.get("data") or {}).get("follower") or 0
            except Exception as e:
                logger.debug(f"[BiliLiveMonitor] 获取主播粉丝数失败: {e}")

            # get_info 没给全时，用 Master/info 补齐头像和昵称
            if not info["face"] or not info["bili_name"]:
                try:
                    master = await self._get_json(
                        BILI_MASTER_INFO_API, {"uid": uid}, retry_on_412=False
                    )
                    if master.get("code") == 0:
                        minfo = (master.get("data") or {}).get("info") or {}
                        info["face"] = info["face"] or (minfo.get("face") or "")
                        info["bili_name"] = info["bili_name"] or (
                            minfo.get("uname") or ""
                        )
                except Exception as e:
                    logger.debug(f"[BiliLiveMonitor] 获取主播信息失败: {e}")

        return info

    # ---------- 通知 ----------
    async def _notify_live_on(self, room_id, anchor_name, group_ids, info: dict):
        try:
            room_id_int = int(room_id)
        except ValueError:
            return

        live_url = f"https://live.bilibili.com/{room_id_int}"
        title = info.get("title") or "无标题"

        # 文本 + 图片合并成一条消息
        text_msg = (
            f"{anchor_name} 😋 开播啦！\n"
            f"{live_url}"
        )

        # 渲染卡片（失败则退回原始封面 URL）
        image_bytes = None
        render_card = self.config.get("render_card", True)
        if render_card is not False:
            try:
                image_bytes = await card_renderer.render_live_card_async(
                    session=self.session,
                    cover_url=info.get("cover") or "",
                    avatar_url=info.get("face") or "",
                    headers=self._headers(),
                    anchor_name=anchor_name,
                    bili_name=info.get("bili_name") or "",
                    title=title,
                    area_name=info.get("area_name") or "",
                    parent_area_name=info.get("parent_area_name") or "",
                    online=info.get("online") or 0,
                    watched_show=info.get("watched_show") or 0,
                    follower=info.get("follower") or 0,
                    live_time_text=fmt_minute(info.get("live_time") or ""),
                    room_id=str(room_id_int),
                    live_url=live_url,
                )
                logger.info(
                    f"[BiliLiveMonitor] 开播卡片渲染成功（{len(image_bytes)} bytes）"
                )
            except Exception as e:
                logger.error(
                    f"[BiliLiveMonitor] 开播卡片渲染失败，降级为封面直链: {e}",
                    exc_info=True,
                )

        for gid in group_ids:
            try:
                gid_int = int(gid)
            except ValueError:
                continue
            ok = await self._send_group_text_with_image(
                gid_int,
                text_msg,
                image_url=info.get("cover") or "",
                image_bytes=image_bytes,
            )
            if ok:
                logger.info(f"[BiliLiveMonitor] 开播通知已发送到群 {gid}")

    async def _notify_live_off(
        self,
        room_id,
        anchor_name,
        group_ids,
        start_dt,
        end_dt,
        info: dict | None = None,
        cached: dict | None = None,
    ):
        info = info or {}
        cached = cached or {}

        if start_dt is not None:
            start_text = fmt_minute(start_dt)
            duration_seconds = max(0, int((end_dt - start_dt).total_seconds()))
            duration_text = format_duration(duration_seconds)
        else:
            start_text = "未知"
            duration_text = "未知"
        end_text = fmt_minute(end_dt)

        text_msg = f"{anchor_name} 💤 下播啦！"

        # 渲染长条图：头像 / 标题 / B站昵称 / 粉丝数 / 开播时间 / 下播时间 / 直播时长
        image_bytes = None
        if self.config.get("render_card", True) is not False:
            try:
                # 优先用开播时缓存的信息（下播后 get_info 的标题/头像可能已变或为空）
                bili_name = cached.get("bili_name") or info.get("bili_name") or ""
                title = cached.get("title") or info.get("title") or ""
                follower = cached.get("follower") or info.get("follower") or 0
                watched_show = (
                    cached.get("watched_show") or info.get("watched_show") or 0
                )
                avatar_url = cached.get("face") or info.get("face") or ""

                image_bytes = await card_renderer.render_strip_row_async(
                    session=self.session,
                    avatar_url=avatar_url,
                    headers=self._headers(),
                    anchor_name=anchor_name,
                    bili_name=bili_name,
                    title=title,
                    live_status=0,
                    follower=follower,
                    watched_show=watched_show,
                    live_time_text=start_text,
                    end_time_text=end_text,
                    duration_text=duration_text,
                    room_id=str(room_id),
                    variant="offline",
                )
                if image_bytes:
                    logger.info(
                        f"[BiliLiveMonitor] 下播长条图渲染成功（{len(image_bytes)} bytes）"
                    )
            except Exception as e:
                logger.error(
                    f"[BiliLiveMonitor] 下播长条图渲染失败，降级为纯文字: {e}",
                    exc_info=True,
                )

        for gid in group_ids:
            try:
                gid_int = int(gid)
            except ValueError:
                continue
            if image_bytes:
                ok = await self._send_group_text_with_image(
                    gid_int, text_msg, image_bytes=image_bytes
                )
            else:
                ok = await self._send_group_text(gid_int, text_msg)
            if ok:
                logger.info(f"[BiliLiveMonitor] 下播通知已发送到群 {gid}")

    # ---------- 事件辅助 ----------
    @staticmethod
    def _get_event_group_id(event: AstrMessageEvent) -> str:
        """尽可能拿到当前消息所在群的群号，拿不到返回空串。

        优先用 event.get_group_id()；不同 AstrBot 版本/平台事件对象差异较大，
        所以再退回解析 unified_msg_origin（形如 `pid:GroupMessage:123456`）。
        """
        try:
            fn = getattr(event, "get_group_id", None)
            if callable(fn):
                gid = fn()
                if gid:
                    return str(gid).strip()
        except Exception:
            pass
        try:
            umo = getattr(event, "unified_msg_origin", "") or ""
            if isinstance(umo, str) and ":" in umo:
                parts = umo.split(":")
                # pid:GroupMessage:gid[:sub]
                if len(parts) >= 3 and parts[1].lower().endswith("groupmessage"):
                    return parts[2].strip()
        except Exception:
            pass
        return ""

    @staticmethod
    def _get_message_text(event: AstrMessageEvent) -> str:
        """取当前消息的纯文本（备用，个别版本判断子参数时用得上）。

        依次尝试 get_message_str() / get_message_outline() / message_str，
        都取不到就拼一遍 message chain 里的 Plain 组件。
        """
        for attr in ("get_message_str", "get_message_outline"):
            fn = getattr(event, attr, None)
            if callable(fn):
                try:
                    s = fn()
                    if s:
                        return str(s)
                except Exception:
                    pass
        s = getattr(event, "message_str", None)
        if isinstance(s, str) and s:
            return s
        try:
            chain = getattr(event, "message", None) or []
            texts = []
            for comp in chain:
                t = getattr(comp, "text", None)
                if isinstance(t, str):
                    texts.append(t)
            return "".join(texts)
        except Exception:
            return ""

    def _is_admin(self, event: AstrMessageEvent) -> bool:
        """判断是否管理员：优先用 AstrBot 自带的 role 判定，再退回超管/配置管理员列表。"""
        # AstrBot 新版事件对象自带权限判定
        for attr in ("is_admin", "is_super_user"):
            fn = getattr(event, attr, None)
            if callable(fn):
                try:
                    if fn():
                        return True
                except Exception:
                    pass

        # 退回超管列表 / 配置里的管理员列表
        admins = self.config.get("admins_id") or []
        if isinstance(admins, str):
            admins = [admins]
        admins = {str(a).strip() for a in admins if str(a).strip()}
        for attr in ("get_super_user_ids", "super_user_ids"):
            v = getattr(self.context, attr, None)
            v = v() if callable(v) else v
            if v:
                admins |= {str(x).strip() for x in v if str(x).strip()}
        if not admins:
            return False

        sender = ""
        for attr in ("get_sender_id", "sender_id", "get_user_id"):
            try:
                v = getattr(event, attr, None)
                v = v() if callable(v) else v
                if v:
                    sender = str(v).strip()
                    break
            except Exception:
                continue
        return bool(sender) and sender in admins

    # ---------- 指令：查询状态 ----------
    @filter.command("liveinfo")
    async def liveinfo(self, event: AstrMessageEvent):
        """群内查询：只显示「推送目标包含本群」的主播。"""
        async for r in self._render_liveinfo(event, show_all=False):
            yield r

    @filter.command("liveinfoall")
    async def liveinfoall(self, event: AstrMessageEvent):
        """管理员指令：查看全部主播，忽略当前群限制。"""
        if not self._is_admin(event):
            yield event.plain_result("⚠️ 该指令仅管理员可用。")
            return
        async for r in self._render_liveinfo(event, show_all=True):
            yield r

    async def _render_liveinfo(self, event: AstrMessageEvent, show_all: bool = False):
        rooms = self.config.get("rooms", [])
        if not rooms:
            yield event.plain_result("当前没有配置任何直播间。")
            return

        # 群内查询只显示「推送目标包含本群」的主播；私聊不限制，显示全部
        cur_gid = self._get_event_group_id(event)

        # 记一下 UMO，方便排查"渲染成功但没发出去"
        try:
            logger.info(
                f"[BiliLiveMonitor] liveinfo 触发，umo={getattr(event, 'unified_msg_origin', '')!r}, "
                f"group={cur_gid!r}, all={show_all}"
            )
        except Exception:
            pass

        blocks = []
        strip_items = []
        for line in rooms:
            room_id, anchor_name, group_ids = self._parse_room_line(line)
            if not room_id:
                continue
            # 群聊场景：该主播没配到本群则跳过（管理员 all 时跳过这段限制）
            if cur_gid and not show_all and cur_gid not in group_ids:
                continue
            live_url = f"https://live.bilibili.com/{room_id}"
            try:
                info = await self._fetch_live_status(room_id)
                status_text = {
                    0: "💤 未开播",
                    1: "😋 直播中",
                    2: "🔁 轮播中",
                }.get(info["live_status"], "❓ 未知")
                # 文本格式：第一行「昵称 | 状态」，第二行直播间链接
                blocks.append(f"{anchor_name} | {status_text}\n{live_url}")
                # 收集渲染长条图所需字段（沿用同一份 info，不重复请求）
                strip_items.append(
                    {
                        "anchor_name": anchor_name,
                        "bili_name": info.get("bili_name") or "",
                        "title": info.get("title") or "",
                        "live_status": info.get("live_status", 0),
                        "follower": info.get("follower") or 0,
                        "watched_show": info.get("watched_show") or 0,
                        "avatar_url": info.get("face") or "",
                        "room_id": room_id,
                    }
                )
            except Exception as e:
                blocks.append(
                    f"{anchor_name} | ⚠️ 查询失败\n{live_url}\n{e}"
                )

        if not blocks:
            if cur_gid:
                yield event.plain_result(
                    f"本群（{cur_gid}）没有配置任何主播，请先在插件配置的 rooms 里"
                    f"把主播的推送群加上 {cur_gid}。\n"
                    f"（管理员可发送 /liveinfoall 查看全部主播）"
                )
            else:
                yield event.plain_result("没有有效的直播间配置。")
            return

        text_all = "\n\n".join(blocks)

        # 渲染长条信息图（失败则只发文字，不影响指令可用性）
        strip_bytes = None
        if strip_items and self.config.get("render_card", True) is not False:
            try:
                strip_bytes = await card_renderer.render_live_strip_async(
                    items=strip_items,
                    session=self.session,
                    headers=self._headers(),
                    gap=6,
                )
                logger.info(
                    f"[BiliLiveMonitor] liveinfo 长条图渲染成功"
                    f"（{len(strip_items)} 行，{len(strip_bytes)} bytes）"
                )
            except Exception as e:
                logger.error(
                    f"[BiliLiveMonitor] liveinfo 长条图渲染失败，仅发文字: {e}",
                    exc_info=True,
                )

        if strip_bytes:
            # 指令返回值就支持「文字 + 图片」：chain 必须是普通 list，
            # 绝不能传 MessageChain（result_decorate 会对 result.chain 做 len()）
            comps = [Plain(text_all), Image.fromBytes(strip_bytes)]
            result = self._build_image_result(event, comps)
            if result is not None:
                yield result
                return

        yield event.plain_result(text_all)

    @staticmethod
    def _build_image_result(event: AstrMessageEvent, components: list):
        """把组件 list 包装成指令返回值。

        关键：AstrBot 的 result_decorate 阶段会执行 `len(result.chain)`，
        所以 chain 必须是**普通 list**；传 MessageChain 会直接
        `TypeError: object of type 'MessageChain' has no len()`。

        优先 chain_result([Plain, Image])；个别版本只支持 image_result，
        那就退化成只发图（传单个组件，不传 list）。
        """
        fn = getattr(event, "chain_result", None)
        if callable(fn):
            try:
                return fn(components)
            except Exception as e:
                logger.warning(f"[BiliLiveMonitor] chain_result 构造失败: {e}")

        # 退而求其次：只发图片（image_result 收单个组件）
        fn = getattr(event, "image_result", None)
        if callable(fn):
            img = components[-1] if components else None
            if img is not None:
                try:
                    return fn(img)
                except Exception as e:
                    logger.warning(f"[BiliLiveMonitor] image_result 构造失败: {e}")

        logger.warning(
            "[BiliLiveMonitor] 当前 AstrBot 版本不支持图片结果，降级为纯文字"
        )
        return None

    # ---------- 指令：清空状态缓存 ----------
    @filter.command("livers")
    async def livers(self, event: AstrMessageEvent):
        old_status_count = len(self.last_status)
        old_start_count = len(self.live_start_time)
        self.last_status.clear()
        self.live_start_time.clear()
        self.live_on_info.clear()
        self._buvid_cookies = None
        msg = (
            f"✅ 已清空直播状态缓存\n"
            f"清空了 {old_status_count} 条状态记录、{old_start_count} 条开播时间记录\n"
            f"buvid Cookie 也已重置，下次请求会重新获取\n"
            f"下一轮轮询将重新记录状态，首次记录不会触发通知。"
        )
        logger.info(
            f"[BiliLiveMonitor] 缓存已清空（状态 {old_status_count} 条，开播时间 {old_start_count} 条）"
        )
        yield event.plain_result(msg)