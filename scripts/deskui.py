#!/usr/bin/env python3
"""desk UI —— 本机图形界面。

起一个只听 127.0.0.1 的小服务，把搜索结果 / 商品详情摊成网页给主人看。
页面上的一切操作（进详情、返回、看联系方式）都由本服务自理，**没有任何会惊动
agent 的动作**——0812 拍板网页私信链路停用后，详情页 CTA 与 Web 端对齐
（查看联系方式 / 转载帖跳原帖），「让 AI 帮我聊聊」随之下线；想让 agent 出手，
主人在对话里说。使用剧本见 references/desk-ui.md。

🔴 本文件的四条不变量（kernel/tests/test-deskui.py 逐条钉着，改代码前先读）：

1. **只用标准库**。skill 的零依赖承诺不因为一个页面服务破掉。
2. **不直连后端**。所有集市数据都经 `scripts/a2hmarket.py` 子进程 —— 登录态、PAT、
   匿名降级全部沿用它。这不只是省事：打包器的端点闸与出站字段闸**只扫 a2hmarket.py**，
   本文件一旦自己发 HTTPS，那两道门就形同虚设。
3. **零落盘**。渲染载荷全在内存；进程一退什么都不剩（assets 静态文件只读不写）。
   主人的私有定价策略（定义见 references/pricing.md）只可能出现在
   agent 那侧，`human` 视图连字段都没有。
4. **回传只有封闭动作集**。页面能提交的动作由服务端逐屏算出来（`legal_actions`），
   不在表里的一律 422，且**没有任何自由文本字段**。集市文本因此在通道层
   就变不成 agent 的输入（红线 8）。

协议 `a2hmarket-deskui/v1`（长轮询 + 乐观并发，形状源自 qipai skill）。
事件流 / 互斥锁在 0.38.1 随最后一个 agent-bound 动作一起删除——
页面纯自理，agent 只管 render 和 stop。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

try:
    from deskui_pages import HINTS, render_fragment, render_overlay, render_page
except ModuleNotFoundError:  # 直接 import 本模块的测试没带 sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from deskui_pages import HINTS, render_fragment, render_overlay, render_page

PROTOCOL = "a2hmarket-deskui/v1"

# 服务端单次长轮询挂多久。客户端收到 204 会立刻重发，所以这个值只影响「多久一次空转」。
LONG_POLL_SECONDS = 25
# 页面多久没来请求就自动退出。浏览器的 /api/state 长轮询每 25s 至少来一次，
# 所以只要页面还开着就不会触发；关掉页面 30 分钟后进程自己收摊，不留常驻监听。
IDLE_EXIT_SECONDS = 30 * 60
# 子进程超时。逛集市那一发最慢（服务端要搜索），给足。
CLI_TIMEOUT = 60
# 搜索接口本身已经返回完整公开 DTO。刚 render 完的一屏直接把这份快照当详情用，
# 不要用户一点击又同步重取；超过这段时间仍先画快照，再在后台校准。
DETAIL_SNAPSHOT_TTL_SECONDS = 30

# AI 搜索摘要的硬上限（字符）。剧本口径是 150 字以内（软约束，见 desk-ui.md），
# 这里是结构性兜底：不守规矩的超长摘要会被截断加省略号——第一件商品不能被
# 摘要挤出首屏，页面质量不靠 agent 自觉（与「human 视图是载荷真子集」同一思路）。
SUMMARY_MAX = 220

# ListingDTO 的公开字段白名单。搜索载荷即便混进别的 agent 上下文字段，也只能有这些
# 进入详情快照；这与 normalize_search 只挑卡片字段是同一条结构性隐私边界。
PUBLIC_LISTING_FIELDS = (
    "listingId", "sellerUserId", "sellerNickname", "sellerVerifiedSchool",
    "tradeType", "card", "title", "description", "tags", "attributes",
    "category", "itemCondition", "flawNote", "currency", "price", "negotiable",
    "deliveryMethods", "meetupAreas", "location", "status", "photos",
    "commentCount", "viewCount", "refreshedAt", "availableUntil", "createdAt", "updatedAt",
)


class DeskError(Exception):
    """带 HTTP 状态码的错误。status 直接就是要回给调用方的那个码。"""

    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


# ---------------------------------------------------------------- 集市数据（只经 a2hmarket.py）


def _cli_path() -> Path:
    """`a2hmarket.py` 在哪：与本文件**同目录**（0.38.0 起源码树与产物布局一致，
    都是 scripts/ 下并排；实验期源码另住一个目录、要两个候选的年代结束了）。"""
    candidate = Path(__file__).resolve().parent / "a2hmarket.py"
    if candidate.is_file():
        return candidate
    raise DeskError(500, "找不到 scripts/a2hmarket.py —— desk UI 的所有数据都要经它取")


def _assets_dir() -> Path:
    """静态资源目录：scripts/ 与 assets/ 并排（产物与源码树同布局）。"""
    return Path(__file__).resolve().parents[1] / "assets"


def a2hmarket(*args: str) -> object:
    """跑一条 `a2hmarket.py` 子命令，返回它 `data` 字段里的东西。

    🔴 **这是本文件唯一的出站口**。别在别处发 HTTP —— 见文件头不变量 2。
    """
    command = [sys.executable, str(_cli_path()), *args]
    try:
        done = subprocess.run(command, capture_output=True, text=True, timeout=CLI_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise DeskError(504, f"集市响应超时：a2hmarket.py {' '.join(args)}") from None
    line = (done.stdout or "").strip().splitlines()
    if not line:
        detail = (done.stderr or "").strip()[:200]
        raise DeskError(502, f"a2hmarket.py 没有输出（{detail or '无 stderr'}）")
    try:
        payload = json.loads(line[-1])
    except json.JSONDecodeError:
        raise DeskError(502, f"a2hmarket.py 输出不是 JSON：{line[-1][:200]}") from None
    if not payload.get("ok"):
        message = (payload.get("error") or {}).get("message") or "集市调用失败"
        raise DeskError(502, message)
    return payload.get("data")


# ---------------------------------------------------------------- 转载帖判定
#
# 🔴 **已知的第二真相源**（实验期接受，正式方案要消掉）。
#    权威判定在 frontend/src/lib/repost.ts，那份文件开头写着「这是全站唯一一处」。
#    Python 侧 import 不了 TS，本函数是照它的判据**手抄**的：
#      主判据 attributes.source == 'xiaohongshu'；兜底扫描述里的小红书链接 / 标题 [转载] 前缀。
#    链接取法也照抄：优先描述里的完整 URL（带 xsec_token，attributes 里的短链被
#    128 字上限裁过只作兜底）。抄错的后果是**给一个没人守的帖子开了私信入口**
#    （0806 拍板「做硬」），所以本包的测试逐条对着 repost.ts 的判据钉了用例。

# 全角/半角冒号都认（同 repost.ts 的 SOURCE_LINE / XHS_URL）
_SOURCE_LINE = re.compile(r"原帖联系卖家[：:]\s*(https?://\S+)")
_XHS_URL = re.compile(r"https?://(?:www\.)?(?:xiaohongshu\.com|xhslink\.com)/\S+")


def repost_source(listing: dict) -> tuple[bool, str | None]:
    """(是不是转载帖, 原帖链接)。老数据只有 [转载] 前缀没链接时链接为 None。"""
    description = listing.get("description") or ""
    match = _SOURCE_LINE.search(description)
    desc_url = match.group(1) if match else None
    if desc_url is None:
        bare = _XHS_URL.search(description)
        desc_url = bare.group(0) if bare else None
    attributes = listing.get("attributes") or {}
    if attributes.get("source") == "xiaohongshu":
        # attributes 里的短链没经正则筛过，进 href 前把 scheme 钉死（红线 8：
        # 集市数据不许变成 javascript: 之类的可执行链接）
        attr_url = str(attributes.get("source_url") or "")
        if not attr_url.startswith(("https://", "http://")):
            attr_url = ""
        return True, desc_url or attr_url or None
    if desc_url:
        return True, desc_url
    if (listing.get("title") or "").startswith("[转载]"):
        return True, None
    return False, None


def is_repost(listing: dict) -> bool:
    return repost_source(listing)[0]


# ---------------------------------------------------------------- 会话状态（内存态）


class Session:
    """一次「摊开逛」的全部状态。**没有任何一行会落盘。**

    三个视图的分工（照 qipai：服务端分别产出 human / agent / public）：
      human  → 页面渲染用，**不含**任何私有策略字段
      agent  → 事件里给 agent 的那份，可以多带上下文
    本 MVP 里 human 视图是 agent 载荷的**真子集**：模板只读它认识的那些键，
    agent 就算把私有定价策略塞进载荷，页面也渲染不出来 —— 这是结构性的，不靠自觉。
    """

    def __init__(self) -> None:
        self.lock = threading.Condition()
        self.session_id = secrets.token_hex(8)
        self.revision = 0
        self.update_scope = "full"
        # 单窗口：view ∈ search | listing（私信页面 0812 拍板下线）。
        # revision 只在内容变化时 bump（事件/锁机制删除后没有别的写入源），
        # 所以页面收到新状态就换 HTML，不需要第二个游标。
        self.view = "search"
        self.payload: dict = {"query": None, "items": []}
        # 🔴 最近一次搜索结果**独立存**，视图切换不触碰 —— 「返回搜索页」永远
        #    从这里渲染，直到 agent 下一次 render search 才替换。实验版把它塞在
        #    payload["_search"] 里，open_listing 一整体替换 payload 它就没了，
        #    「← 返回搜索结果」回到的是一屏空白（0812 修）。
        self.search_payload: dict = {"query": None, "items": []}
        # listingId → {listing, complete, cached_at, is_mine}。只存公开 DTO 白名单，
        # 随 Session 一起消失；搜索换屏时整张表替换。
        self.detail_snapshots: dict[str, dict] = {}
        self.contact_cache: dict[str, list] = {}
        self.search_epoch = 0
        self.navigation_epoch = 0
        self.next_operation_id = 1
        self.last_touch = time.monotonic()
        self._mine: set[str] | None = None

    # -------- 只读派生

    def mine(self) -> set[str]:
        """主人自己在卖的帖子 ID。自己的帖子不出 CTA（与 Web 端 isOwner 判定对齐）。

        懒取一次并缓存：没登录 / 取不到就当空集合 —— 宁可多给一个入口，
        也不要因为一次网络抖动把正常商品的入口吞掉（服务端护栏仍在）。
        """
        if self._mine is None:
            try:
                data = a2hmarket("listing", "mine") or {}
                items = data.get("items") if isinstance(data, dict) else data
                self._mine = {str(item.get("listingId")) for item in (items or [])}
            except DeskError:
                self._mine = set()
        return self._mine

    def _legal_actions_locked(self) -> list[dict]:
        """当前页面允许提交的动作**全集**。

        🔴 页面能做什么由这里说了算，不由页面上画了什么按钮说了算。
        提交上来的动作必须与本表某一项**逐字段相等**，否则 422。
        """
        actions: list[dict] = []
        if self.view == "search":
            actions += [{"type": "open_listing", "listingId": item["listingId"]}
                        for item in self.payload.get("items", [])]
        else:  # listing
            listing = self.payload.get("listing") or {}
            actions.append({"type": "back"})
            if self.payload.get("contactsOpen"):
                actions.append({"type": "close_contacts"})
            elif self.payload.get("canContact"):
                actions.append({"type": "view_contacts",
                                "listingId": listing.get("listingId")})
        return actions

    def legal_actions(self) -> list[dict]:
        with self.lock:
            return self._legal_actions_locked()

    def _human_state_locked(self) -> dict:
        """给页面的那份。

        🔴 **只给渲染好的 HTML，不给 payload** —— 页面拿不到原始载荷，也就无从
        「自己再渲一遍」。这是「一套模板」这条纪律的结构性保证：想在页面上多显示
        一个字段，只能去改 Python 模板，不可能在 JS 里偷偷加一份。
        """
        state = {"view": self.view, "payload": self.payload}
        return {"protocol": PROTOCOL, "session_id": self.session_id,
                "revision": self.revision, "update_scope": self.update_scope,
                "view": self.view, "hint": HINTS.get(self.view, ""),
                "html": render_fragment(state),
                "overlay_html": render_overlay(state)}

    def human_state(self) -> dict:
        with self.lock:
            return self._human_state_locked()

    def render_state(self) -> dict:
        """整页渲染要的那份（带 payload，只在进程内用，不出网）。"""
        with self.lock:
            return {"protocol": PROTOCOL, "session_id": self.session_id,
                    "revision": self.revision,
                    "view": self.view, "payload": self.payload}

    # -------- 写（都要持锁）

    def _bump(self, update_scope: str = "full") -> None:
        self.revision += 1
        self.update_scope = update_scope
        self.last_touch = time.monotonic()
        self.lock.notify_all()

    def _set_view_locked(self, view: str, payload: dict,
                         update_scope: str = "full") -> None:
        self.view = view
        self.payload = payload
        self._bump(update_scope)

    def set_view(self, view: str, payload: dict, update_scope: str = "full") -> None:
        with self.lock:
            self._set_view_locked(view, payload, update_scope)

    def _validate_human_action_locked(self, body: dict) -> dict:
        action = body.get("action")
        if not isinstance(action, dict):
            raise DeskError(400, "缺 action")
        expected = body.get("expected_revision")
        if expected is not None and int(expected) != self.revision:
            raise DeskError(409, f"状态已经变了（现在 revision={self.revision}）")
        if action not in self._legal_actions_locked():
            raise DeskError(422, "这个动作不在当前页面的允许动作集里")
        return action

    def _new_navigation_locked(self) -> tuple[int, int]:
        self.navigation_epoch += 1
        operation_id = self.next_operation_id
        self.next_operation_id += 1
        return self.navigation_epoch, operation_id

    # -------- 长轮询

    def wait_state(self, after: int | None) -> dict | None:
        """页面用：revision 变了就返回新状态，25s 没变返回 None（客户端重发）。"""
        deadline = time.monotonic() + LONG_POLL_SECONDS
        with self.lock:
            self.last_touch = time.monotonic()
            while after is not None and self.revision <= after:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.lock.wait(remaining)
                self.last_touch = time.monotonic()
            return self._human_state_locked()


# ---------------------------------------------------------------- 视图装配


def listing_view_payload(listing: dict, *, is_mine: bool = False,
                         loading: bool = False, load_error: str | None = None) -> dict:
    """详情页载荷。CTA 分支**照抄 Web 端 ctaLabel**（ListingDetail.tsx）：

    ① 自己的帖子       → 没有 CTA（Web 端 isOwner → null）
    ② 转载帖有原帖链接 → 「去小红书原帖联系卖家」外跳
    ③ 转载帖没链接     → 没有 CTA（Alert 已给「去小红书搜原作者」指引，
                          再画一颗点不动的按钮只是制造虚假预期 —— Web 端原话）
    ④ 正常             → 「查看联系方式」（0812 拍板：替代私信入口，邮箱排最前）
    """
    repost, repost_url = repost_source(listing)
    return {"listing": listing, "isMine": is_mine, "isRepost": repost,
            "repostUrl": repost_url,
            "canContact": not loading and not is_mine and not repost,
            "contacts": None, "contactsOpen": False, "contactsLoading": False,
            "contactError": None, "loading": loading, "loadError": load_error}


def build_listing_view(session: Session, listing_id: str) -> dict:
    """兼容 agent 直接 render listing 的同步路径；网页点卡不走这里。"""
    listing = a2hmarket("market", "show", listing_id)
    if not isinstance(listing, dict):
        raise DeskError(502, "集市返回的商品详情不是一个对象")
    mine = str(listing.get("listingId")) in session.mine()
    return listing_view_payload(listing, is_mine=mine)


def _preview_listing(item: dict) -> dict:
    """精简卡也能立刻画一个详情骨架；远端补全在后台进行。"""
    cover = item.get("cover")
    seller = item.get("seller") or {}
    return {"listingId": item.get("listingId"), "title": item.get("title"),
            "price": item.get("price"), "currency": item.get("currency"),
            "card": item.get("card"), "tradeType": item.get("tradeType"),
            "status": item.get("status"), "itemCondition": item.get("itemCondition"),
            "location": item.get("location"), "photos": [cover] if cover else [],
            "sellerNickname": seller.get("nickname"),
            "sellerVerifiedSchool": seller.get("verifiedSchool")}


def _start_background(name: str, target) -> None:
    """页面动作先返回；慢 I/O 在 daemon 线程里补齐，进程退出时不拖住。"""
    threading.Thread(target=target, name=name, daemon=True).start()


def _refresh_listing(session: Session, listing_id: str, navigation_epoch: int,
                     search_epoch: int, is_mine: bool) -> None:
    try:
        listing = a2hmarket("market", "show", listing_id)
        if not isinstance(listing, dict):
            raise DeskError(502, "集市返回的商品详情不是一个对象")
    except Exception as error:  # noqa: BLE001 —— 后台线程不能静默死掉，把骨架永远留在加载态
        with session.lock:
            if (session.navigation_epoch != navigation_epoch or session.view != "listing"
                    or str((session.payload.get("listing") or {}).get("listingId")) != listing_id):
                return
            # 有完整快照时后台校准失败不降级已经可看的页面；只有骨架态才显式报错。
            if not session.payload.get("loading"):
                return
            payload = dict(session.payload)
            payload["loading"] = False
            payload["loadError"] = str(error)
            session._set_view_locked("listing", payload)
        return

    fresh = {"listing": listing, "complete": True, "cached_at": time.monotonic(),
             "is_mine": is_mine}
    with session.lock:
        if session.search_epoch == search_epoch:
            session.detail_snapshots[listing_id] = fresh
        # 较慢的旧点击回来时不能覆盖用户后来打开的商品或已经返回的搜索页。
        if (session.navigation_epoch != navigation_epoch or session.view != "listing"
                or str((session.payload.get("listing") or {}).get("listingId")) != listing_id):
            return
        payload = listing_view_payload(listing, is_mine=is_mine)
        # 详情校准与弹层数据请求可以同时在路上。校准只换商品正文，不能顺手关闭
        # 已经打开的弹层、清掉它的 loading/error，或让迟到结果改写用户刚做的操作。
        for key in ("contacts", "contactsOpen", "contactsLoading", "contactError"):
            if key in session.payload:
                payload[key] = session.payload[key]
        cached_contacts = session.contact_cache.get(listing_id)
        if cached_contacts is not None:
            payload["contacts"] = cached_contacts
        session._set_view_locked("listing", payload)


def _refresh_contacts(session: Session, listing_id: str, navigation_epoch: int) -> None:
    try:
        contacts = a2hmarket("market", "contacts", listing_id)
        contacts = contacts if isinstance(contacts, list) else []
        error_message = None
    except Exception as error:  # noqa: BLE001 —— 与详情刷新同理，不能把弹层永久卡在 loading
        contacts = []
        error_message = str(error)

    with session.lock:
        if error_message is None:
            session.contact_cache[listing_id] = contacts
        if (session.navigation_epoch != navigation_epoch or session.view != "listing"
                or str((session.payload.get("listing") or {}).get("listingId")) != listing_id):
            return
        payload = dict(session.payload)
        payload["contactsLoading"] = False
        payload["contactError"] = error_message
        payload["contacts"] = contacts
        session._set_view_locked("listing", payload, "overlay")


# ---------------------------------------------------------------- HTTP


class Handler(BaseHTTPRequestHandler):
    server_version = "a2hmarket-deskui"
    sys_version = ""

    # 每个请求都要过的三道门，见 README「安全边界」。
    def _guard(self, url) -> None:
        host = (self.headers.get("Host") or "").strip()
        if host not in self.server.allowed_hosts:            # DNS rebinding
            raise DeskError(403, "bad host")
        origin = self.headers.get("Origin")
        if origin and origin not in self.server.allowed_origins:   # 跨站 POST
            raise DeskError(403, "bad origin")
        given = (parse_qs(url.query).get("k") or [""])[0]
        if not secrets.compare_digest(given, self.server.token):   # 一次性令牌
            raise DeskError(403, "bad token")

    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        # 🔴 刻意不发任何 CORS 头：没有 Access-Control-Allow-* 就没有跨站读取。
        self.send_header("Referrer-Policy", "no-referrer")
        if content_type.startswith("text/html"):
            # CSS/JS 全部外置（assets/）之后页面不需要任何内联执行面 ——
            # 就算转义有漏，注入的 <script>/<style> 也会被 CSP 拒载。
            # img https: 是集市图片；connect 'self' 是长轮询与动作提交。
            self.send_header("Content-Security-Policy",
                             "default-src 'none'; style-src 'self'; script-src 'self'; "
                             "img-src https:; connect-src 'self'")
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, status: int, value: object) -> None:
        self._send(status, json.dumps(value, ensure_ascii=False).encode("utf-8"),
                   "application/json; charset=utf-8")

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 256 * 1024:
            raise DeskError(400, "空的或过大的请求体")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError):
            raise DeskError(400, "请求体不是 JSON") from None

    def do_GET(self) -> None:  # noqa: N802
        self._dispatch()

    def do_HEAD(self) -> None:  # noqa: N802
        self._dispatch()

    def do_POST(self) -> None:  # noqa: N802
        self._dispatch()

    def _dispatch(self) -> None:
        url = urlparse(self.path)
        session: Session = self.server.session
        try:
            self._guard(url)
            query = parse_qs(url.query)
            if self.command in ("GET", "HEAD") and url.path in ("/", "/search", "/listing"):
                page = render_page(session.render_state(), self.server.token)
                self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            elif self.command == "GET" and url.path in ("/assets/deskui.css", "/assets/deskui.js"):
                # 静态资源与页面同门禁（都带 ?k=）。read_bytes 只读，零落盘不变量不受影响。
                asset = _assets_dir() / url.path.rsplit("/", 1)[-1]
                content_type = ("text/css; charset=utf-8" if url.path.endswith(".css")
                                else "text/javascript; charset=utf-8")
                self._send(200, asset.read_bytes(), content_type)
            elif self.command == "GET" and url.path == "/api/state":
                after = query.get("after")
                state = session.wait_state(int(after[0]) if after else None)
                self._json(200, state) if state else self._send(204, b"", "application/json")
            elif self.command == "POST" and url.path == "/api/human-action":
                self._json(200, handle_human_action(session, self._body()))
            elif self.command == "POST" and url.path == "/api/agent-action":
                self._json(200, handle_agent_action(session, self._body(), self.server))
            elif self.command == "GET" and url.path == "/api/health":
                self._json(200, {"ok": True, "protocol": PROTOCOL,
                                 "session_id": session.session_id, "revision": session.revision})
            else:
                raise DeskError(404, "not found")
        except DeskError as error:
            self._json(error.status, {"ok": False, "error": str(error)})
        except Exception as error:  # noqa: BLE001 —— 单个请求崩掉不该带走整个会话
            self._json(500, {"ok": False, "error": f"{type(error).__name__}: {error}"})

    def log_message(self, *_args) -> None:
        """默认实现往 stderr 打访问日志，会把商品 ID 之类的东西刷进 agent 的终端。静音。"""


def handle_human_action(session: Session, body: dict) -> dict:
    """页面提交的动作。**全部由 sidecar 自理**（0.38.1 起没有 agent-bound 动作）。

    两层裁决：乐观并发（409）→ `legal_actions` 逐字段比对（不在表里 → 422）。
    两步必须与状态推进在同一个锁里；否则两个 revision=0 的并发点击会双双通过。
    """
    background = None
    operation_id = None
    with session.lock:
        action = session._validate_human_action_locked(body)
        kind = action.get("type")

        if kind == "open_listing":
            listing_id = str(action["listingId"])
            navigation_epoch, operation_id = session._new_navigation_locked()
            entry = session.detail_snapshots.get(listing_id)
            item = next((candidate for candidate in session.search_payload.get("items", [])
                         if str(candidate.get("listingId")) == listing_id), {})
            listing = dict(entry["listing"]) if entry else _preview_listing(item)
            is_mine = bool(entry and entry.get("is_mine"))
            complete = bool(entry and entry.get("complete"))
            payload = listing_view_payload(listing, is_mine=is_mine, loading=not complete)
            cached_contacts = session.contact_cache.get(listing_id)
            if cached_contacts is not None:
                payload["contacts"] = cached_contacts
            session._set_view_locked("listing", payload)

            age = time.monotonic() - float(entry.get("cached_at", 0)) if entry else float("inf")
            if not complete or age > DETAIL_SNAPSHOT_TTL_SECONDS:
                search_epoch = session.search_epoch
                background = (f"deskui-detail-{operation_id}",
                              lambda: _refresh_listing(session, listing_id, navigation_epoch,
                                                       search_epoch, is_mine))
        elif kind == "back":
            session._new_navigation_locked()  # 让仍在路上的详情结果自动失效
            session._set_view_locked("search", session.search_payload)
        elif kind == "view_contacts":
            listing_id = str(action["listingId"])
            payload = dict(session.payload)
            cached = session.contact_cache.get(listing_id)
            payload["contactsOpen"] = True
            if cached is not None:
                payload["contacts"] = cached
                payload["contactsLoading"] = False
                payload["contactError"] = None
            elif not payload.get("contactsLoading"):
                payload["contactsLoading"] = True
                payload["contactError"] = None
                operation_id = session.next_operation_id
                session.next_operation_id += 1
                navigation_epoch = session.navigation_epoch
                background = (f"deskui-contacts-{operation_id}",
                              lambda: _refresh_contacts(session, listing_id, navigation_epoch))
            session._set_view_locked("listing", payload, "overlay")
        elif kind == "close_contacts":
            payload = dict(session.payload)
            payload["contactsOpen"] = False
            session._set_view_locked("listing", payload, "overlay")
        else:
            raise DeskError(422, f"未知动作 {kind}")
        state = session._human_state_locked()

    if background is not None:
        _start_background(*background)
    result = {"ok": True, "handled_by": "sidecar", "revision": state["revision"],
              "state": state}
    if operation_id is not None:
        result["operation_id"] = operation_id
    return result


def handle_agent_action(session: Session, body: dict, server) -> dict:
    """agent 提交的动作：渲染某一屏、或收摊。"""
    action = body.get("action")
    if not isinstance(action, dict):
        raise DeskError(400, "缺 action")
    kind = action.get("type")
    if kind == "stop":
        with session.lock:
            expected = body.get("expected_revision")
            if expected is not None and int(expected) != session.revision:
                raise DeskError(409, f"状态已经变了（现在 revision={session.revision}）")
            server.should_stop.set()
        return {"ok": True, "stopping": True}
    if kind != "render":
        raise DeskError(422, f"agent 只能 render / stop（收到 {kind}）")

    view = action.get("view")
    result = {"ok": True}
    if view == "search":
        payload, snapshots = normalize_search_bundle(action.get("payload") or {})
        with session.lock:
            expected = body.get("expected_revision")
            if expected is not None and int(expected) != session.revision:
                raise DeskError(409, f"状态已经变了（现在 revision={session.revision}）")
            # 🔴 搜索结果只此一份、独立于当前视图存活：新搜索替换它，返回时复用它
            session.search_epoch += 1
            session._new_navigation_locked()
            session.detail_snapshots = snapshots
            session.search_payload = payload
            session._set_view_locked("search", payload)
            revision = session.revision
    elif view == "listing":
        # agent 直接点名一件时仍走兼容路径；慢请求结束后再原子校验 revision，
        # 期间若人已经操作过就回 409，不能用旧结果覆盖新页面。
        payload = build_listing_view(session, action["listingId"])
        with session.lock:
            expected = body.get("expected_revision")
            if expected is not None and int(expected) != session.revision:
                raise DeskError(409, f"状态已经变了（现在 revision={session.revision}）")
            session._new_navigation_locked()
            session._set_view_locked("listing", payload)
            revision = session.revision
    else:
        raise DeskError(422, f"未知视图 {view}")
    # agent 只收小回执；整页 HTML 若从 CLI stdout 回显，会白白进入模型上下文耗 token。
    # 浏览器自己的动作响应才直接带 state，后台变化仍由页面长轮询接收。
    result.update({"revision": revision, "view": view})
    return result


# 七项硬校验（marketplace.md，0811 业主拍板）。**缺项走兜底文案，绝不留白** ——
# 「缺数据写未知是诚实，省略这一栏是让主人猜」。这里做的是把缺失**显式化**，
# 真正的兜底文案在 deskui_pages.py 的模板里，两边由本包的测试一起钉住。
# 成色是唯一合法缺席：showCondition=false 的帖型（转租/帮带/跑腿…）不出成色 ——
# 「转租 · 全新」这类错位比缺席更误导，帖型显隐表在 deskui_pages.CARD_META。
SEVEN_FIELDS = ("cover", "title", "price", "itemCondition", "location", "seller", "aiNote")
_DETAIL_COMPLETENESS_FIELDS = {"description", "photos", "sellerUserId", "deliveryMethods"}


def _value(raw: dict, detail: dict, key: str):
    return raw.get(key) if key in raw else detail.get(key)


def _normalize_card(raw: dict, detail: dict) -> dict | None:
    listing_id = _value(raw, detail, "listingId")
    if not listing_id:
        return None
    seller = raw.get("seller") or {}
    photos = _value(raw, detail, "photos") or []
    cover = raw.get("cover") or (photos[0] if photos else None)
    listing_for_repost = dict(detail)
    listing_for_repost.update({key: raw[key] for key in
                               ("title", "description", "attributes") if key in raw})
    return {
        "listingId": str(listing_id),
        "cover": cover if str(cover or "").startswith("https://") else None,
        "coverNote": raw.get("coverNote"),
        "title": _value(raw, detail, "title"),
        "price": _value(raw, detail, "price"),
        "currency": _value(raw, detail, "currency"),
        "card": _value(raw, detail, "card"),
        "tradeType": _value(raw, detail, "tradeType"),
        "status": _value(raw, detail, "status"),
        "itemCondition": _value(raw, detail, "itemCondition"),
        "location": _value(raw, detail, "location"),
        "distanceNote": raw.get("distanceNote"),
        # 无图卡用描述首段补位（2026-08 设计定稿 3a）；有图卡模板不读这个键
        "description": _value(raw, detail, "description"),
        "seller": {
            "verifiedSchool": (seller.get("verifiedSchool")
                               or _value(raw, detail, "sellerVerifiedSchool")),
            "tag": seller.get("tag"),
            "nickname": seller.get("nickname") or _value(raw, detail, "sellerNickname"),
            "isRepost": bool(seller.get("isRepost") or is_repost(listing_for_repost)),
        },
        "aiNote": raw.get("aiNote"),
        "url": raw.get("url"),
    }


def _normalize_detail_snapshot(raw: dict, detail: dict, card: dict) -> dict:
    # 显式 detail 优先表达「这是 market list 的原始公开 DTO」；为兼容旧调用，原始字段
    # 直接铺在 card 同层也认。卡片上的补充值最后覆盖，避免两个视图标题/价格不一致。
    source = dict(detail)
    source.update({key: raw[key] for key in PUBLIC_LISTING_FIELDS if key in raw})
    listing = {key: source.get(key) for key in PUBLIC_LISTING_FIELDS if key in source}
    listing.update({key: card.get(key) for key in
                    ("listingId", "title", "price", "currency", "card", "tradeType",
                     "status", "itemCondition", "location") if card.get(key) is not None})
    if not listing.get("photos") and card.get("cover"):
        listing["photos"] = [card["cover"]]
    seller = card.get("seller") or {}
    if not listing.get("sellerNickname") and seller.get("nickname"):
        listing["sellerNickname"] = seller["nickname"]
    if not listing.get("sellerVerifiedSchool") and seller.get("verifiedSchool"):
        listing["sellerVerifiedSchool"] = seller["verifiedSchool"]
    complete_source = detail if detail else raw
    complete = _DETAIL_COMPLETENESS_FIELDS.issubset(complete_source.keys())
    return {"listing": listing, "complete": complete,
            "cached_at": time.monotonic() if complete else 0,
            "is_mine": bool(raw.get("isMine") or detail.get("isMine"))}


def normalize_search_bundle(payload: dict) -> tuple[dict, dict[str, dict]]:
    """返回页面卡片 + 同批公开详情快照；两份都经过显式字段白名单。"""
    items = []
    snapshots = {}
    for raw in payload.get("items") or []:
        if not isinstance(raw, dict):
            continue
        detail = raw.get("detail") if isinstance(raw.get("detail"), dict) else {}
        card = _normalize_card(raw, detail)
        if card is None:
            continue
        items.append(card)
        snapshots[card["listingId"]] = _normalize_detail_snapshot(raw, detail, card)
    summary = payload.get("summary")
    summary = summary.strip() if isinstance(summary, str) else ""
    if len(summary) > SUMMARY_MAX:
        summary = summary[:SUMMARY_MAX - 1] + "…"
    return {"query": payload.get("query"),
            # AI 搜索摘要（2026-08 设计定稿 3a）：agent 自由写的一段话，呈现纪律
            # 四件套摊在这里；缺省时页面整卡不渲染，超长被 SUMMARY_MAX 截断
            "summary": summary or None,
            "items": items}, snapshots


def normalize_search(payload: dict) -> dict:
    """把 agent 给的搜索载荷规整成模板认识的形状。

    🔴 只挑模板认识的键 —— 这就是「human 视图是 agent 载荷的真子集」的实现：
    agent 就算把私有定价策略塞进来，也进不了页面。
    card / tradeType / status 直接透传服务端原值：徽章文案、价格修饰、状态标签
    全部由模板按 CARD_META 计算，agent 不需要也不允许替页面翻译这些。
    """
    return normalize_search_bundle(payload)[0]


# ---------------------------------------------------------------- serve


def serve(args) -> int:
    session = Session()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    server.daemon_threads = True
    server.session = session
    server.token = secrets.token_urlsafe(24)
    server.should_stop = threading.Event()
    port = server.server_address[1]
    server.allowed_hosts = {f"{args.host}:{port}", f"localhost:{port}"}
    server.allowed_origins = {f"http://{args.host}:{port}", f"http://localhost:{port}"}
    url = f"http://{args.host}:{port}/?k={server.token}"

    # 🔴 一行 ready JSON 打到 stdout，agent 从这里读 url。**不写任何文件** ——
    #    后续的 render / wait / act 都靠 agent 把这个 url 带在 --url 上（同 qipai）。
    print(json.dumps({"ok": True, "protocol": PROTOCOL, "url": url,
                      "session_id": session.session_id, "pid": os.getpid()},
                     ensure_ascii=False), flush=True)

    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.5},
                              daemon=True)
    thread.start()
    try:
        while not server.should_stop.is_set():
            if time.monotonic() - session.last_touch > IDLE_EXIT_SECONDS:
                print(json.dumps({"ok": True, "stopped": "idle"}, ensure_ascii=False), flush=True)
                break
            time.sleep(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.shutdown()
        server.server_close()
    return 0


# ---------------------------------------------------------------- 客户端子命令


def _request(url: str, path: str, *, method: str = "GET", body: dict | None = None):
    """向本会话的 sidecar 发一发。url 里带着令牌，原样接上路径。"""
    parsed = urlparse(url)
    target = f"{parsed.scheme}://{parsed.netloc}{path}"
    target += ("&" if "?" in target else "?") + parsed.query
    data = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
    request = urllib.request.Request(target, data=data, method=method,
                                     headers={"Content-Type": "application/json",
                                              "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=LONG_POLL_SECONDS + 10) as response:
            raw = response.read().decode("utf-8")
            return response.status, (json.loads(raw) if raw else None)
    except urllib.error.HTTPError as error:
        raw = error.read().decode("utf-8", "replace")
        try:
            return error.code, json.loads(raw)
        except json.JSONDecodeError:
            return error.code, {"ok": False, "error": raw[:200]}
    except OSError as error:
        raise SystemExit(json.dumps({"ok": False, "error": f"连不上 desk UI：{error}"},
                                    ensure_ascii=False))


def cmd_render(args) -> int:
    action = {"type": "render", "view": args.view}
    if args.view == "search":
        raw = sys.stdin.read().strip()
        if not raw:
            raise SystemExit(json.dumps({"ok": False, "error": "search 载荷要从 stdin 喂进来"},
                                        ensure_ascii=False))
        action["payload"] = json.loads(raw)
    elif args.view == "listing":
        action["listingId"] = args.listing_id
    return _submit(args, action)


def cmd_stop(args) -> int:
    return _submit(args, {"type": "stop"})


def cmd_probe(_args) -> int:
    """环境探测：这台机器能不能开 desk UI。**只报机器属性**，宿主身份 agent 自查。

    v1 只支持 macOS（业主定的逐步支持范围）。输出单个 JSON，给 agent 读：
    supported=false 时按 marketplace.md 走纯对话呈现，别提这个功能。
    """
    import platform
    system = platform.system()
    reasons = []
    if system != "Darwin":
        reasons.append(f"目前只支持 macOS（这台机器是 {system}）")
    print(json.dumps({"supported": not reasons, "platform": system,
                      "reasons": reasons}, ensure_ascii=False))
    return 0 if not reasons else 1


def _submit(args, action: dict) -> int:
    body = {"action": action}
    if getattr(args, "revision", None) is not None:
        body["expected_revision"] = args.revision
    status, payload = _request(args.url, "/api/agent-action", method="POST", body=body)
    print(json.dumps(payload, ensure_ascii=False))
    return 0 if status == 200 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)

    serve_parser = sub.add_parser("serve", help="起服务，打一行 ready JSON 后常驻")
    serve_parser.add_argument("--host", default="127.0.0.1")
    serve_parser.add_argument("--port", type=int, default=0, help="0 = 由系统分配")
    serve_parser.set_defaults(fn=serve)

    sub.add_parser("probe", help="环境探测（只报机器属性）：supported=false 就别开页面"
                   ).set_defaults(fn=cmd_probe)

    for name, fn, extra in (("render", cmd_render, "渲染某一屏"),
                            ("stop", cmd_stop, "收摊")):
        child = sub.add_parser(name, help=extra)
        child.add_argument("--url", required=True, help="serve 打出来的那个带令牌的 URL")
        child.add_argument("--revision", type=int, help="乐观并发；不给就不校验")
        child.set_defaults(fn=fn)
        if name == "render":
            child.add_argument("--view", required=True, choices=("search", "listing"))
            child.add_argument("--listing-id")
    return parser


def main() -> int:
    args = build_parser().parse_args()
    try:
        return args.fn(args)
    except DeskError as error:
        print(json.dumps({"ok": False, "error": str(error)}, ensure_ascii=False))
        return 1


if __name__ == "__main__":
    sys.exit(main())
