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
    from deskui_pages import HINTS, render_fragment, render_page
except ModuleNotFoundError:  # 直接 import 本模块的测试没带 sys.path
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from deskui_pages import HINTS, render_fragment, render_page

PROTOCOL = "a2hmarket-deskui/v1"

# 服务端单次长轮询挂多久。客户端收到 204 会立刻重发，所以这个值只影响「多久一次空转」。
LONG_POLL_SECONDS = 25
# 页面多久没来请求就自动退出。浏览器的 /api/state 长轮询每 25s 至少来一次，
# 所以只要页面还开着就不会触发；关掉页面 30 分钟后进程自己收摊，不留常驻监听。
IDLE_EXIT_SECONDS = 30 * 60
# 子进程超时。逛集市那一发最慢（服务端要搜索），给足。
CLI_TIMEOUT = 60


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

    def legal_actions(self) -> list[dict]:
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

    def human_state(self) -> dict:
        """给页面的那份。

        🔴 **只给渲染好的 HTML，不给 payload** —— 页面拿不到原始载荷，也就无从
        「自己再渲一遍」。这是「一套模板」这条纪律的结构性保证：想在页面上多显示
        一个字段，只能去改 Python 模板，不可能在 JS 里偷偷加一份。
        """
        return {"protocol": PROTOCOL, "session_id": self.session_id,
                "revision": self.revision,
                "view": self.view, "hint": HINTS.get(self.view, ""),
                "html": render_fragment({"view": self.view, "payload": self.payload})}

    def render_state(self) -> dict:
        """整页渲染要的那份（带 payload，只在进程内用，不出网）。"""
        return {"protocol": PROTOCOL, "session_id": self.session_id,
                "revision": self.revision,
                "view": self.view, "payload": self.payload}

    # -------- 写（都要持锁）

    def _bump(self) -> None:
        self.revision += 1
        self.last_touch = time.monotonic()
        self.lock.notify_all()

    def set_view(self, view: str, payload: dict) -> None:
        with self.lock:
            self.view = view
            self.payload = payload
            self._bump()

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
            return self.human_state()


# ---------------------------------------------------------------- 视图装配


def build_listing_view(session: Session, listing_id: str) -> dict:
    """详情页载荷。CTA 分支**照抄 Web 端 ctaLabel**（ListingDetail.tsx）：

    ① 自己的帖子       → 没有 CTA（Web 端 isOwner → null）
    ② 转载帖有原帖链接 → 「去小红书原帖联系卖家」外跳
    ③ 转载帖没链接     → 没有 CTA（Alert 已给「去小红书搜原作者」指引，
                          再画一颗点不动的按钮只是制造虚假预期 —— Web 端原话）
    ④ 正常             → 「查看联系方式」（0812 拍板：替代私信入口，邮箱排最前）
    """
    listing = a2hmarket("market", "show", listing_id)
    if not isinstance(listing, dict):
        raise DeskError(502, "集市返回的商品详情不是一个对象")
    mine = str(listing.get("listingId")) in session.mine()
    repost, repost_url = repost_source(listing)
    return {"listing": listing, "isMine": mine, "isRepost": repost,
            "repostUrl": repost_url,
            "canContact": not mine and not repost,
            "contacts": None, "contactsOpen": False}


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
    """
    action = body.get("action")
    if not isinstance(action, dict):
        raise DeskError(400, "缺 action")
    kind = action.get("type")

    expected = body.get("expected_revision")
    if expected is not None and int(expected) != session.revision:
        raise DeskError(409, f"状态已经变了（现在 revision={session.revision}）")

    if action not in session.legal_actions():
        raise DeskError(422, "这个动作不在当前页面的允许动作集里")

    if kind == "open_listing":
        session.set_view("listing", build_listing_view(session, action["listingId"]))
    elif kind == "back":
        session.set_view("search", session.search_payload)
    elif kind == "view_contacts":
        # 0812 拍板：CTA 直接展示发帖人联系方式。取一次缓存在载荷里（同 Web 端行为），
        # 关掉再开不重复打服务端 —— 配额是按查看者计次的，别替主人浪费。
        payload = dict(session.payload)
        if payload.get("contacts") is None:
            contacts = a2hmarket("market", "contacts", action["listingId"])
            payload["contacts"] = contacts if isinstance(contacts, list) else []
        payload["contactsOpen"] = True
        session.set_view("listing", payload)
    elif kind == "close_contacts":
        payload = dict(session.payload)
        payload["contactsOpen"] = False
        session.set_view("listing", payload)
    else:
        raise DeskError(422, f"未知动作 {kind}")
    return {"ok": True, "handled_by": "sidecar", "revision": session.revision}


def handle_agent_action(session: Session, body: dict, server) -> dict:
    """agent 提交的动作：渲染某一屏、或收摊。"""
    action = body.get("action")
    if not isinstance(action, dict):
        raise DeskError(400, "缺 action")
    expected = body.get("expected_revision")
    if expected is not None and int(expected) != session.revision:
        raise DeskError(409, f"状态已经变了（现在 revision={session.revision}）")

    kind = action.get("type")
    if kind == "stop":
        server.should_stop.set()
        return {"ok": True, "stopping": True}
    if kind != "render":
        raise DeskError(422, f"agent 只能 render / stop（收到 {kind}）")

    view = action.get("view")
    result = {"ok": True}
    if view == "search":
        payload = normalize_search(action.get("payload") or {})
        # 🔴 搜索结果只此一份、独立于当前视图存活：新搜索替换它，返回时复用它
        session.search_payload = payload
        session.set_view("search", payload)
    elif view == "listing":
        session.set_view("listing", build_listing_view(session, action["listingId"]))
    else:
        raise DeskError(422, f"未知视图 {view}")
    result.update({"revision": session.revision, "view": view})
    return result


# 七项硬校验（marketplace.md，0811 业主拍板）。**缺项走兜底文案，绝不留白** ——
# 「缺数据写未知是诚实，省略这一栏是让主人猜」。这里做的是把缺失**显式化**，
# 真正的兜底文案在 deskui_pages.py 的模板里，两边由本包的测试一起钉住。
# 成色是唯一合法缺席：showCondition=false 的帖型（转租/帮带/跑腿…）不出成色 ——
# 「转租 · 全新」这类错位比缺席更误导，帖型显隐表在 deskui_pages.CARD_META。
SEVEN_FIELDS = ("cover", "title", "price", "itemCondition", "location", "seller", "aiNote")


def normalize_search(payload: dict) -> dict:
    """把 agent 给的搜索载荷规整成模板认识的形状。

    🔴 只挑模板认识的键 —— 这就是「human 视图是 agent 载荷的真子集」的实现：
    agent 就算把私有定价策略塞进来，也进不了页面。
    card / tradeType / status 直接透传服务端原值：徽章文案、价格修饰、状态标签
    全部由模板按 CARD_META 计算，agent 不需要也不允许替页面翻译这些。
    """
    items = []
    for raw in payload.get("items") or []:
        listing_id = raw.get("listingId")
        if not listing_id:
            continue
        seller = raw.get("seller") or {}
        items.append({
            "listingId": str(listing_id),
            "cover": raw.get("cover") if str(raw.get("cover") or "").startswith("https://") else None,
            "coverNote": raw.get("coverNote"),
            "title": raw.get("title"),
            "price": raw.get("price"),
            "currency": raw.get("currency"),
            "card": raw.get("card"),
            "tradeType": raw.get("tradeType"),
            "status": raw.get("status"),
            "itemCondition": raw.get("itemCondition"),
            "location": raw.get("location"),
            "distanceNote": raw.get("distanceNote"),
            "seller": {"verifiedSchool": seller.get("verifiedSchool"),
                       "tag": seller.get("tag"),
                       "nickname": seller.get("nickname"),
                       "isRepost": bool(seller.get("isRepost"))},
            "aiNote": raw.get("aiNote"),
            "url": raw.get("url"),
        })
    return {"query": payload.get("query"), "items": items}


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
