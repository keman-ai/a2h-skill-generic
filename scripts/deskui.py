#!/usr/bin/env python3
"""desk UI —— 本机图形界面双向通道。

起一个只听 127.0.0.1 的小服务，把搜索结果 / 商品详情 / 私信摊成网页给主人看，
主人在页面上操作，agent 从长轮询里收到。使用剧本见 references/desk-ui.md。

🔴 本文件的四条不变量（kernel/tests/test-deskui.py 逐条钉着，改代码前先读）：

1. **只用标准库**。skill 的零依赖承诺不因为一个页面服务破掉。
2. **不直连后端**。所有集市数据都经 `scripts/a2hmarket.py` 子进程 —— 登录态、PAT、
   匿名降级全部沿用它。这不只是省事：打包器的端点闸与出站字段闸**只扫 a2hmarket.py**，
   本文件一旦自己发 HTTPS，那两道门就形同虚设。
3. **零落盘**。渲染载荷、事件、游标全在内存；进程一退什么都不剩（assets 静态文件
   只读不写）。主人的私有定价策略（定义见 references/pricing.md）只可能出现在
   agent 那侧，`human` 视图连字段都没有。
4. **回传只有封闭动作集**。页面能提交的动作由服务端逐屏算出来（`legal_actions`），
   不在表里的一律 422。集市文本因此在通道层就变不成 agent 的输入（红线 8）。

协议 `a2hmarket-deskui/v1`，形状照搬 qipai skill（游标长轮询 + 乐观并发），
按 A2H Market 的需要改了两处：事件分级（导航不惊动 agent）、退出条件（逛街没有终局）。
"""

from __future__ import annotations

import argparse
import json
import os
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
    from _site_config import FRONT_BASE
except ModuleNotFoundError:  # 直接 import 本模块的测试没带 sys.path；两个兄弟模块一起兜
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    from deskui_pages import HINTS, render_fragment, render_page
    from _site_config import FRONT_BASE

PROTOCOL = "a2hmarket-deskui/v1"

# 服务端单次长轮询挂多久。客户端收到 204 会立刻重发，所以这个值只影响「多久一次空转」，
# 不影响 agent 视角的等待时长 —— agent 那边 `wait` 是不会超时的。
LONG_POLL_SECONDS = 25
# 页面多久没来请求就自动退出。浏览器的 /api/state 长轮询每 25s 至少来一次，
# 所以只要页面还开着就不会触发；关掉页面 30 分钟后进程自己收摊，不留常驻监听。
IDLE_EXIT_SECONDS = 30 * 60
# 事件环形缓冲上限。逛一次街产生的事件是个位数，300 是给「主人狂点」留的余量。
MAX_EVENTS = 300
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
#    Python 侧 import 不了 TS，本函数是照它的两层判据**手抄**的：
#      主判据 attributes.source == 'xiaohongshu'；兜底扫描述里的小红书链接 / 标题 [转载] 前缀。
#    抄错的后果是**给一个没人守的帖子开了私信入口**（0806 拍板「做硬」不给入口），
#    所以本包的测试逐条对着 repost.ts 的判据钉了用例。

_XHS_HOSTS = ("xiaohongshu.com", "xhslink.com")


def is_repost(listing: dict) -> bool:
    attributes = listing.get("attributes") or {}
    if attributes.get("source") == "xiaohongshu":
        return True
    description = listing.get("description") or ""
    if any(host in description for host in _XHS_HOSTS):
        return True
    return (listing.get("title") or "").startswith("[转载]")


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
        self.view = "search"
        self.payload: dict = {"query": None, "items": []}
        # 🔴 最近一次搜索结果**独立存**，视图切换不触碰 —— 「返回/导航回搜索页」永远
        #    从这里渲染，直到 agent 下一次 render search 才替换。实验版把它塞在
        #    payload["_search"] 里，open_listing 一整体替换 payload 它就没了，
        #    「← 返回搜索结果」回到的是一屏空白（0812 修）。
        self.search_payload: dict = {"query": None, "items": []}
        # 🔴 人机互斥锁：人点了 agent-bound 动作（ai_negotiate / ai_reply）后置位，
        #    agent 的下一次成功动作（render / ack / stop）或主人手动 unlock 清掉。
        #    busy 期间页面锁定（除 unlock 外的人类动作一律 423），锁定横幅展示
        #    hint —— 「AI 接下来会做什么」要在页面上讲清，不让主人对着转圈猜。
        self.busy: dict | None = None
        self.events: list[dict] = []
        self.next_event_id = 1
        self.last_touch = time.monotonic()
        self._mine: set[str] | None = None

    # -------- 只读派生

    def mine(self) -> set[str]:
        """主人自己在卖的帖子 ID。用来决定详情页给不给「让 AI 帮我聊聊」。

        懒取一次并缓存：没登录 / 取不到就当空集合 —— 宁可多给一个入口，
        也不要因为一次网络抖动把正常商品的私信入口吞掉（服务端本来也会拒绝给自己发）。
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
        """当前这一屏允许提交的动作**全集**。

        🔴 页面能做什么由这里说了算，不由页面上画了什么按钮说了算。
        提交上来的动作必须与本表某一项**逐字段相等**，否则 422。
        唯一例外是 `send_message`：表里存的是**模板**（不含 content），
        校验时把 content 剥掉再比对 —— 自由文本单独走长度闸（见 handle_human_action）。
        """
        # 顶部胶囊导航：两格（搜索结果 | 私信），当前页那格不算动作
        actions: list[dict] = [{"type": "nav", "view": view}
                               for view in ("search", "messages") if view != self.view]
        if self.view == "search":
            actions += [{"type": "open_listing", "listingId": item["listingId"]}
                        for item in self.payload.get("items", [])]
        elif self.view == "listing":
            listing = self.payload.get("listing") or {}
            actions.append({"type": "back"})
            if self.payload.get("canNegotiate"):
                actions.append({"type": "ai_negotiate", "listingId": listing.get("listingId")})
        elif self.view == "messages":
            # 与 Web 移动端同构的两页态：列表页只能开串，串详情页只能发/代回/返回列表
            active = self.payload.get("activeThreadId")
            if active:
                actions.append({"type": "back_to_threads"})
                actions.append({"type": "send_message", "threadId": active})
                actions.append({"type": "ai_reply", "threadId": active})
            else:
                actions += [{"type": "open_thread", "threadId": thread["threadId"]}
                            for thread in self.payload.get("threads", [])]
        return actions

    def human_state(self) -> dict:
        """给页面的那份。

        🔴 **只给渲染好的 HTML，不给 payload** —— 页面拿不到原始载荷，也就无从
        「自己再渲一遍」。这是「一套模板」这条纪律的结构性保证：想在页面上多显示
        一个字段，只能去改 Python 模板，不可能在 JS 里偷偷加一份。
        """
        state = {"protocol": PROTOCOL, "session_id": self.session_id,
                 "revision": self.revision, "view": self.view,
                 "hint": HINTS.get(self.view, ""),
                 "html": render_fragment(self.render_state())}
        if self.busy:
            # 页面据此渲染锁定遮罩：hint 告诉主人「AI 接下来会做什么」，
            # sinceSeconds 给超时兜底（页面在 90s 后追加手动解锁按钮）。
            state["busy"] = {"hint": self.busy["hint"],
                            "sinceSeconds": int(time.monotonic() - self.busy["since"])}
        return state

    def render_state(self) -> dict:
        """整页渲染要的那份（带 payload，只在进程内用，不出网）。"""
        return {"protocol": PROTOCOL, "session_id": self.session_id,
                "revision": self.revision, "view": self.view, "payload": self.payload}

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

    def clear_busy(self) -> None:
        """解锁。幂等 —— 没锁时调用不 bump（别为无事发生刷新页面）。"""
        with self.lock:
            if self.busy is not None:
                self.busy = None
                self._bump()

    def emit(self, event_type: str, extra: dict, hint: str) -> dict:
        """产生一个**要惊动 agent** 的事件，并**同一把锁内**置互斥锁。导航不走这里。

        🔴 事件创建与 busy 置位必须原子：分两步的话 set_busy 会在事件之后再 bump 一次
        revision，agent 拿着事件里的 revision 来 render 就会平白吃 409。
        这里先置 busy、后算 revision、事件带**bump 后**的 revision，一次通知全下发。
        """
        with self.lock:
            self.busy = {"action": event_type, "hint": hint,
                         "event_id": self.next_event_id, "since": time.monotonic()}
            self.revision += 1
            event = {"protocol": PROTOCOL, "session_id": self.session_id,
                     "event_id": self.next_event_id, "revision": self.revision,
                     "type": event_type,
                     "state": {"view": self.view, **extra,
                               "legal_actions": self.legal_actions()}}
            self.next_event_id += 1
            self.events.append(event)
            del self.events[:-MAX_EVENTS]
            self.last_touch = time.monotonic()
            self.lock.notify_all()
            return event

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

    def wait_event(self, after: int) -> dict | None:
        """agent 用：游标之后的第一个事件；25s 没有返回 None（客户端重发）。"""
        deadline = time.monotonic() + LONG_POLL_SECONDS
        with self.lock:
            while True:
                for event in self.events:
                    if event["event_id"] > after:
                        return event
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self.lock.wait(remaining)


# ---------------------------------------------------------------- 视图装配


def build_listing_view(session: Session, listing_id: str) -> dict:
    """详情页载荷。**三个分支一个都不能少**（对齐 frontend/src/pages/ListingDetail.tsx）：

    ① 自己的帖子 → 不给私信入口（服务端也会拒："不能给自己的商品留言"）
    ② 转载帖     → 不给私信入口，明确告知走小红书原帖（0806 拍板「做硬」）
    ③ 正常       → 给「让 AI 帮我聊聊」
    """
    listing = a2hmarket("market", "show", listing_id)
    if not isinstance(listing, dict):
        raise DeskError(502, "集市返回的商品详情不是一个对象")
    mine = str(listing.get("listingId")) in session.mine()
    repost = is_repost(listing)
    return {"listing": listing, "isMine": mine, "isRepost": repost,
            "canNegotiate": not mine and not repost,
            "blockedReason": ("这是你自己的帖子" if mine else
                              "这条帖子从小红书转载，发帖账号无人在守站内私信 —— 要联系得去原帖"
                              if repost else None)}


def build_messages_view(thread_id: str | None = None) -> dict:
    """私信页载荷。查看 + 主人手动发送（发送走 handle_human_action 的 send_message）。

    🔴 列表**必须**用 `message conversations`，不是 `message inbox`（0811 首测踩的坑）。
       inbox 的服务端语义是「我参与的、**非我发的**、晚于 since 的留言」——
       刚 `message send` 开完一条串去查 inbox 一定是空的，因为那条串里唯一一句话是我发的。
       第一版拿 inbox 建列表，于是刚发完的私信在页面上显示「还没有私信」，
       被一路误判成服务端故障。conversations 才是「我有哪些串」，后端注释里原话就是
       「给『私信』页左栏的统一会话列表」。

    🔴 字段名全部对着 ConversationDTO / MessageDTO 取，不许凭印象猜 ——
       第一版三个名字（counterpartNickname / lastMessage / thread.messages）**全是猜的、
       全都不存在**，而单测打了桩所以一路全绿。`test_deskui.py` 现在有一条闸直接读
       那两个 Java DTO 核对字段名，别绕过它。
    """
    page = a2hmarket("message", "conversations", "--size", "50") or {}
    threads = page.get("items") if isinstance(page, dict) else page
    threads = [t for t in (threads or []) if t.get("threadId")]
    # 🔴 不自动选中第一条（0.38.0 起）：与 Web 移动端同构 —— 无 thread_id = 会话列表页，
    #    有 = 串详情页，两页切换。实验版自动选第一条是桌面式单视图的遗留。
    active = next((t for t in threads if t.get("threadId") == thread_id), None)
    # message thread 返回的是 **List[MessageDTO] 本身**，不是 {"messages": [...]}。
    messages = a2hmarket("message", "thread", thread_id) if thread_id else []
    if not isinstance(messages, list):
        messages = []
    listing_id = (active or {}).get("listingId")
    return {"threads": threads, "activeThreadId": thread_id, "messages": messages,
            # 「哪条是我发的」：senderRole 与 myRole 都是**结构**角色，结构对结构比就对了
            # （同 cmd_message_pending 的做法），不要在这里做业务角色换算。
            "myRole": (active or {}).get("myRole"),
            "tradeType": (active or {}).get("tradeType"),
            "peerUserId": (active or {}).get("peerUserId"),
            "peerNickname": (active or {}).get("peerNickname"),
            "listingTitle": (active or {}).get("listingTitle"),
            "listingUrl": f"{FRONT_BASE}/listings/{listing_id}" if listing_id else None}


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
            if self.command in ("GET", "HEAD") and url.path in ("/", "/search", "/listing", "/messages"):
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
            elif self.command == "GET" and url.path == "/api/agent-events":
                event = session.wait_event(int((query.get("after") or ["0"])[0]))
                self._json(200, event) if event else self._send(204, b"", "application/json")
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


# agent-bound 动作的锁定横幅文案：人点完之后「AI 接下来会做什么」要在页面上讲清，
# 不让主人对着转圈猜（业主 spec：人操作之后 AI 会去做什么，在 Web 端就提示）。
BUSY_HINTS = {
    "ai_negotiate": "AI 已收到：正在起草给卖家的私信。草稿会在左侧对话里跟你确认后才发出"
                    "——去看一眼对话框",
    "ai_reply": "AI 正在读这条串并处理回复。需要你拿主意的（价格低于你的线、或它不知道的"
                "信息）它会在对话里先问你",
}

# 主人亲手打的私信正文的长度闸。服务端对 content 没有长度校验，这里兜一道，
# 防止误粘一整篇文档进输入框。
MESSAGE_CONTENT_MAX = 2000


def handle_human_action(session: Session, body: dict) -> dict:
    """页面提交的动作。

    🔴 **三层裁决**，顺序不能换：
    ① busy 锁 —— 人机动作互斥：agent 正在处理事件时，除 `unlock` 外一律 423；
    ② `legal_actions` 比对（不在表里 → 422）—— `send_message` 剥掉 content 后按模板比对，
       content 是全协议唯一的自由文本字段，只流向 CLI 参数，永不进 agent 事件；
    ③ 事件分级 —— 导航类 sidecar 自理，`ai_negotiate` / `ai_reply` 惊动 agent 并上锁。
    """
    action = body.get("action")
    if not isinstance(action, dict):
        raise DeskError(400, "缺 action")

    # ① 互斥锁。unlock 是锁定期间唯一放行的动作（超时兜底：agent 可能死了）。
    if session.busy is not None:
        if action == {"type": "unlock"}:
            session.clear_busy()
            return {"ok": True, "handled_by": "sidecar", "unlocked": True,
                    "revision": session.revision}
        raise DeskError(423, "AI 正在处理上一个动作，页面已锁定 —— 等它完成，"
                             "或用锁定横幅上的解锁按钮")

    expected = body.get("expected_revision")
    if expected is not None and int(expected) != session.revision:
        raise DeskError(409, f"状态已经变了（现在 revision={session.revision}）")

    # ② 动作集比对。send_message 特殊：模板不含 content，剥掉再比。
    kind = action.get("type")
    comparable = {key: value for key, value in action.items() if key != "content"} \
        if kind == "send_message" else action
    if comparable not in session.legal_actions():
        raise DeskError(422, "这个动作不在当前页面的允许动作集里")

    # ③ 分级执行。
    if kind == "nav":
        if action["view"] == "search":
            session.set_view("search", session.search_payload)
        else:
            session.set_view("messages", build_messages_view())
        return {"ok": True, "handled_by": "sidecar", "revision": session.revision}
    if kind == "open_listing":
        session.set_view("listing", build_listing_view(session, action["listingId"]))
        return {"ok": True, "handled_by": "sidecar", "revision": session.revision}
    if kind == "back":
        session.set_view("search", session.search_payload)
        return {"ok": True, "handled_by": "sidecar", "revision": session.revision}
    if kind == "open_thread":
        # 读串是导航，不惊动 agent（0.38.0 起；实验版这里还发事件，改掉的理由：
        # agent 介入只留 ai_negotiate / ai_reply 两个显式入口，互斥锁状态机才收得住）。
        session.set_view("messages", build_messages_view(action["threadId"]))
        return {"ok": True, "handled_by": "sidecar", "revision": session.revision}
    if kind == "back_to_threads":
        session.set_view("messages", build_messages_view())
        return {"ok": True, "handled_by": "sidecar", "revision": session.revision}
    if kind == "send_message":
        content = action.get("content")
        if not isinstance(content, str) or not content.strip():
            raise DeskError(400, "消息内容是空的")
        if len(content) > MESSAGE_CONTENT_MAX:
            raise DeskError(400, f"消息太长（上限 {MESSAGE_CONTENT_MAX} 字）")
        # 主人亲手发的：sidecar 直接经 CLI 发出，不入 agent 事件流 ——
        # agent 不自动跟进主人的手动消息（要它帮忙，主人点「让 AI 代回」或在对话里说）。
        a2hmarket("message", "send", "--thread", action["threadId"],
                  "--content", content.strip())
        session.set_view("messages", build_messages_view(action["threadId"]))
        return {"ok": True, "handled_by": "sidecar", "revision": session.revision}
    if kind == "ai_negotiate":
        listing = (session.payload.get("listing") or {})
        event = session.emit("ai_negotiate", {
            "listingId": action["listingId"],
            "title": listing.get("title"),
            "price": listing.get("price"),
            "currency": listing.get("currency"),
        }, BUSY_HINTS[kind])
        return {"ok": True, "handled_by": "agent", "event_id": event["event_id"]}
    if kind == "ai_reply":
        # 🔴 事件只带 threadId，不带任何串内容 —— agent 自己经 CLI 读串，
        #    untrusted 标记走既有通道；页面永远回传不了自由文本给 agent。
        event = session.emit("ai_reply", {"threadId": action["threadId"]}, BUSY_HINTS[kind])
        return {"ok": True, "handled_by": "agent", "event_id": event["event_id"]}
    raise DeskError(422, f"未知动作 {kind}")


def handle_agent_action(session: Session, body: dict, server) -> dict:
    """agent 提交的动作：渲染某一屏、确认收到（ack）、或收摊。

    🔴 **任何一次成功的 agent 动作都会解互斥锁** —— 这是锁协议的另一半：
    agent 处理完事件（哪怕主人在对话里说不发了）必须 render 或 ack，
    否则页面永远锁着。ack 给「无需重渲染」的场景专用。
    """
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
    if kind == "ack":
        session.clear_busy()
        return {"ok": True, "revision": session.revision, "acked": True}
    if kind != "render":
        raise DeskError(422, f"agent 只能 render / ack / stop（收到 {kind}）")

    view = action.get("view")
    if view == "search":
        payload = normalize_search(action.get("payload") or {})
        # 🔴 搜索结果只此一份、独立于当前视图存活：新搜索替换它，导航/返回复用它
        session.search_payload = payload
        session.set_view("search", payload)
    elif view == "listing":
        session.set_view("listing", build_listing_view(session, action["listingId"]))
    elif view == "messages":
        session.set_view("messages", build_messages_view(action.get("threadId")))
    else:
        raise DeskError(422, f"未知视图 {view}")
    session.clear_busy()
    return {"ok": True, "revision": session.revision, "view": session.view}


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


def cmd_wait(args) -> int:
    """游标长轮询。**这个命令不会超时** —— 收到 204 就立刻重发，真有事件才返回。"""
    after = args.after
    while True:
        status, payload = _request(args.url, f"/api/agent-events?after={after}")
        if status == 204:
            continue
        print(json.dumps(payload, ensure_ascii=False))
        return 0 if status == 200 else 1


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
    elif args.view == "messages" and args.thread_id:
        action["threadId"] = args.thread_id
    return _submit(args, action)


def cmd_act(args) -> int:
    return _submit(args, json.loads(args.action))


def cmd_stop(args) -> int:
    return _submit(args, {"type": "stop"})


def cmd_ack(args) -> int:
    """确认已处理事件但无需重渲染（如主人在对话里说不发了）——只为解互斥锁。"""
    return _submit(args, {"type": "ack"})


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
    if getattr(args, "event", None) is not None:
        body["event_id"] = args.event
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

    for name, fn, extra in (("wait", cmd_wait, "等下一个用户意图"),
                            ("render", cmd_render, "渲染某一屏"),
                            ("act", cmd_act, "提交任意动作"),
                            ("ack", cmd_ack, "确认已处理事件但不重渲染（解互斥锁）"),
                            ("stop", cmd_stop, "收摊")):
        child = sub.add_parser(name, help=extra)
        child.add_argument("--url", required=True, help="serve 打出来的那个带令牌的 URL")
        child.set_defaults(fn=fn)
        if name == "wait":
            child.add_argument("--after", type=int, default=0, help="已处理到的 event_id")
        else:
            child.add_argument("--revision", type=int, help="乐观并发；不给就不校验")
            child.add_argument("--event", type=int, help="正在处理的 event_id")
        if name == "render":
            child.add_argument("--view", required=True, choices=("search", "listing", "messages"))
            child.add_argument("--listing-id")
            child.add_argument("--thread-id")
        if name == "act":
            child.add_argument("--action", required=True, help="动作 JSON")
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
