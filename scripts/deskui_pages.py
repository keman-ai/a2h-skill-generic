#!/usr/bin/env python3
"""desk UI 的三屏模板（服务端渲染，移动端布局，内容容器 640px 居中）。

CSS 与页面 JS 住在 `assets/`（0.38.0 载荷闸开的口，仅该目录允许 .css/.js），
本模块只负责把数据渲成 HTML 片段。

🔴 **为什么是服务端渲染**（实验第一版写成了前端模板，改掉了）：
前端模板意味着「兜底文案」只是 JS 源码里的一个字符串常量 —— 测试断言它出现在页面里
**永远是绿的**，哪怕渲染逻辑整个是坏的。那是假绿，比没有测试更糟。
三屏都在 Python 里渲成 HTML 片段，页面只负责换 `innerHTML` 与转发点击：
**一套模板、一处转义、测试断言的是真正渲出来的东西。**

两条渲染纪律（不是风格，是合规）：

1. **一切来自集市的文本都当数据**（红线 8）：只经 `esc()` 进文本节点或属性，
   永不拼进 `<script>`、永不当 HTML。图片只接受 `https://`。
2. **七项硬校验缺项走兜底文案，绝不留白**（marketplace.md）：「缺数据写未知是诚实，
   省略这一栏是让主人猜」。兜底在模板里，不指望 agent 记得填。
   唯一的合法缺席：`showCondition=false` 的帖型不出成色（「转租 · 全新」这类错位
   正是帖型体系要治的，见 CARD_META）。

样式对齐 Web 端移动档（tokens 快照 + 结构对照见 assets/deskui.css 头注）。
CARD_META / 价格呈现 / 状态文案是对 frontend/src/lib/{cardMeta,listingPrice,listingCopy}.ts
的移植 —— **防漂移闸**（kernel/tests/test-deskui.py::CardMetaMirrorsFrontend）直接解析
TS 源码逐值比对，改任何一边另一边必须跟。
"""

from __future__ import annotations

import hashlib
import html
import json
from datetime import datetime
from pathlib import Path

# 静态资源在 assets/：产物里 scripts/ 与 assets/ 并排，源码树里 kernel/scripts/ 与
# kernel/assets/ 并排 —— 同一个表达式两种布局都对。只读不写（零落盘不变量）。
_ASSETS = Path(__file__).resolve().parents[1] / "assets"

# ---------------------------------------------------------------- 帖型呈现（移植自前端）

# 🔴 移植自 frontend/src/lib/cardMeta.ts 的 CARD_META（全站唯一真相源在那边）。
#    16 帖型 × (徽章 sell/buy、成色显隐、价格修饰、浓淡)。防漂移闸逐值对照 TS 源码。
#    翻转判定纪律沿用：只写 == "BUY"，不写 != "SELL"（tradeType 可选，缺列按 SELL）。
CARD_META = {
    "GOODS":       {"sell": "出闲置", "buy": "求购",   "show_condition": True,  "price": "budget-aware", "accent": "quiet"},
    "TICKET":      {"sell": "转票",   "buy": "收票",   "show_condition": False, "price": "fixed",        "accent": "strong"},
    "LEND":        {"sell": "出借",   "buy": "求租借", "show_condition": True,  "price": "from",         "accent": "strong"},
    "RENTAL":      {"sell": "转租",   "buy": "找房",   "show_condition": False, "price": "periodic",     "accent": "strong"},
    "STORAGE":     {"sell": "寄存",   "buy": "求寄存", "show_condition": False, "price": "from",         "accent": "strong"},
    "ERRAND":      {"sell": "帮带",   "buy": "求帮带", "show_condition": False, "price": "from",         "accent": "strong"},
    "LOCALRUN":    {"sell": "跑腿",   "buy": "求代办", "show_condition": False, "price": "from",         "accent": "strong"},
    "HOMESERVICE": {"sell": "上门服务", "buy": "求上门", "show_condition": False, "price": "from",       "accent": "strong"},
    "PHOTOSHOOT":  {"sell": "约拍",   "buy": "求约拍", "show_condition": False, "price": "from",         "accent": "strong"},
    "CONSULTING":  {"sell": "辅导",   "buy": "求辅导", "show_condition": False, "price": "fixed",        "accent": "strong"},
    "PETCARE":     {"sell": "宠物照看", "buy": "求代喂", "show_condition": False, "price": "from",       "accent": "strong"},
    "COMPANION":   {"sell": "找搭子", "buy": "找搭子", "show_condition": False, "price": "fixed",        "accent": "strong"},
    "CARPOOL":     {"sell": "拼车",   "buy": "求拼车", "show_condition": False, "price": "fixed",        "accent": "strong"},
    "GROUPBUY":    {"sell": "拼团",   "buy": "求拼",   "show_condition": False, "price": "fixed",        "accent": "strong"},
    "JOB":         {"sell": "招人",   "buy": "求职",   "show_condition": False, "price": "fixed",        "accent": "strong"},
    "OTHER":       {"sell": "其他",   "buy": "其他",   "show_condition": True,  "price": "budget-aware", "accent": "quiet"},
}


def card_meta_of(listing: dict) -> dict:
    """白名单式回落 GOODS（同 cardTypeOf）：老数据/未知值按改版前现状呈现。"""
    return CARD_META.get(listing.get("card") or "", CARD_META["GOODS"])


def is_wanted(listing: dict) -> bool:
    return listing.get("tradeType") == "BUY"


# 移植自 frontend/src/api/types.ts 的 LISTING_STATUS_LABEL + listingCopy 的 BUY 翻转表。
# 只有非 ON_SALE 才挂状态徽章（黑底），与 Web 卡片一致。
STATUS_LABEL = {"RESERVED": "已预定", "SOLD": "已成交", "GIFTED": "已送出", "OFFLINE": "已下架"}
BUY_STATUS_LABEL = {"RESERVED": "已锁定", "SOLD": "已收到", "GIFTED": "已收到", "OFFLINE": "已停止"}

# 移植自 frontend/src/lib/listingPrice.ts 的 CURRENCY_SYMBOL。表外币种显示「代码+空格」。
CURRENCY_SYMBOL = {"CNY": "¥", "GBP": "£", "USD": "$", "EUR": "€", "HKD": "HK$", "SGD": "S$"}

# 移植自 frontend/src/api/types.ts 的 CONDITION_LABEL。表外值原样显示（老数据兜底）。
CONDITION_LABEL = {"NEW": "全新", "LIKE_NEW": "几乎全新", "LIGHT_WEAR": "轻微使用痕迹",
                   "VISIBLE_WEAR": "明显使用痕迹", "FLAWED": "能用有瑕疵"}


def condition_label(value) -> str:
    return CONDITION_LABEL.get(value, str(value)) if value else ""


def _amount(price) -> str:
    """120.0 显示成 120；120.5 保留小数。"""
    if isinstance(price, float) and price.is_integer():
        return str(int(price))
    return str(price)


def price_text(listing: dict) -> str:
    """移植自 listingPrice.ts::listingPriceText。

    🔴 方向翻转（预算/面议/免费送）与卡型修饰（/月、起）两层，判定原样照搬：
    - price 空 = 面议 / 预算面议（别和 0 混：0 在卖帖是免费送）
    - BUY 的 0 绝不显示成免费送（预算 0 只能是「预算面议」）
    - from 修饰只加在供给侧（求购预算是上限，「预算 £5 起」自相矛盾）
    """
    wanted = is_wanted(listing)
    price = listing.get("price")
    if price is None:
        return "预算面议" if wanted else "面议"
    if price == 0:
        return "预算面议" if wanted else "免费送"
    currency = listing.get("currency")
    symbol = CURRENCY_SYMBOL.get(currency, f"{currency} ") if currency else "¥"
    prefix = f"预算 {symbol}" if wanted else symbol
    presentation = card_meta_of(listing)["price"]
    if presentation == "periodic":
        return f"{prefix}{_amount(price)}/月"
    if presentation == "from":
        return f"{prefix}{_amount(price)}" if wanted else f"{prefix}{_amount(price)}起"
    return f"{prefix}{_amount(price)}"


def card_badges(listing: dict, *, on_cover: bool) -> str:
    """帖型徽章 + （非在售时）状态徽章。

    tone 两维取并（同 ListingCard 的 CardTag）：求购 ∨ accent=strong → wanted 档
    （紫，文字用 --yx-tag-strong 保对比度）；否则 plain 档。
    """
    meta = card_meta_of(listing)
    wanted = is_wanted(listing)
    label = meta["buy"] if wanted else meta["sell"]
    tone = "wanted" if (wanted or meta["accent"] == "strong") else "plain"
    place = "cover" if on_cover else "flat"
    badges = [f'<span class="badge badge-{tone} badge-{place}">{esc(label)}</span>']
    status = listing.get("status")
    if status and status != "ON_SALE":
        status_label = (BUY_STATUS_LABEL if wanted else STATUS_LABEL).get(status)
        if status_label:
            badges.append(f'<span class="badge badge-status">{esc(status_label)}</span>')
    return "".join(badges)


# ---------------------------------------------------------------- 兜底文案与小件

# 七项硬校验的兜底文案。**集中在这里**，模板与测试都从这引，不让两边各写一份字面量。
FALLBACK = {
    "cover": "这帖没配图，点开看详情",
    "cover_failed": "图没拿到，点链接看",
    "title": "（无标题）",
    "price": "面议",
    "condition": "成色未知",
    "location": "未写位置",
    "seller": "卖家未留背景",
    "note": "（这件我还没来得及看）",
    "description": "（这帖没写描述）",
    # 私信页
    "peer": "对方",
    "no_message": "（还没有内容）",
    "no_thread": "还没有私信",
    "pick_thread": "选一条串看看",
}


def esc(value) -> str:
    """一切集市文本的唯一出口。属性和文本节点都走它（`quote=True`）。"""
    return html.escape("" if value is None else str(value), quote=True)


def act(action: dict) -> str:
    """把一个动作挂到元素上。服务端收到后仍会比对 `legal_actions`，这里只是搬运。"""
    return f"data-act='{esc(json.dumps(action, ensure_ascii=False))}'"


# 确定性预设头像（对 frontend/src/lib/presetAvatar.ts 的简化）：id 哈希挑一对
# tokens 里的 avatar-* 渐变色 + 昵称首字符。同一个人永远同一个色。
_AVATAR_PAIRS = 6  # av-0 … av-5，色值在 deskui.css


def avatar(user_id, nickname, size_class: str) -> str:
    seed = str(user_id or nickname or "?")
    index = int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16) % _AVATAR_PAIRS
    initial = (str(nickname).strip()[:1] or "?") if nickname else "?"
    return (f'<span class="av av-{index} {size_class}" aria-hidden="true">'
            f'{esc(initial)}</span>')


def relative_time(iso: str | None) -> str:
    """会话列表的相对时间（对 pages/messages/time.ts 的近似移植）。"""
    if not iso:
        return ""
    try:
        then = datetime.fromisoformat(str(iso).replace(" ", "T"))
    except ValueError:
        return ""
    now = datetime.now()
    seconds = (now - then).total_seconds()
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)}分钟前"
    if then.date() == now.date():
        return then.strftime("%H:%M")
    days = (now.date() - then.date()).days
    if days == 1:
        return "昨天"
    if days < 7:
        return f"{days}天前"
    return f"{then.month}月{then.day}日"


# 结构角色（SELLER=帖主/BUYER=访客）→ 业务角色标签。镜像 a2hmarket.py::_trade_role
# 的真值表（那边是唯一实现；防漂移闸对照源码）。求购帖上两者相反。
def trade_role_label(structural_role: str | None, trade_type: str | None,
                     *, poster_role: str = "SELLER") -> str:
    i_am_poster = structural_role == poster_role
    wanted = trade_type == "BUY"
    if i_am_poster:
        return "买家" if wanted else "卖家"
    return "卖家" if wanted else "买家"


# ---------------------------------------------------------------- 搜索结果页（单列 Feed 卡 + 评语条）


def _seller_line(item: dict) -> tuple[str, str]:
    """七项⑥卖家信息：meta 行的认证段 + 底排的昵称段。转载帖标明，都没有也要写一句。

    底排回落链：昵称 → 身份标签 → 认证校名（有认证就不该写「未留背景」）→ 兜底文案。
    """
    seller = item.get("seller") or {}
    if seller.get("isRepost"):
        return "", "小红书转载帖"
    verified = seller.get("verifiedSchool")
    meta_part = f"✓ {verified} 认证" if verified else ""
    foot = (seller.get("nickname") or seller.get("tag")
            or (f"{verified} 同学" if verified else FALLBACK["seller"]))
    return meta_part, foot


def search_view(payload: dict) -> str:
    items = payload.get("items") or []
    if not items:
        return '<div class="empty">这一屏还没有东西 —— 等 agent 摊出来</div>'
    cards = []
    for item in items:
        meta = card_meta_of(item)
        open_act = act({"type": "open_listing", "listingId": item["listingId"]})
        # 七项①图：没有就出兜底占位块（徽章降级进正文行），**禁止静默变纯文字**
        if item.get("cover"):
            media = (f'<div class="fcard-media">'
                     f'<img src="{esc(item["cover"])}" alt="" data-ratio>'
                     f'<div class="fcard-badges">{card_badges(item, on_cover=True)}</div></div>')
            flat_badges = ""
        else:
            media = (f'<div class="fcard-nomedia">'
                     f'{esc(item.get("coverNote") or FALLBACK["cover"])}</div>')
            flat_badges = f'<div class="fcard-badges-flat">{card_badges(item, on_cover=False)}</div>'
        # meta 行：✓校名 · 成色(按帖型显隐) · 位置 · 远近 —— 缺项写「未知/未写」，
        # 成色是唯一合法缺席（showCondition=false 的帖型，错位比缺席更误导）
        verified_part, seller_foot = _seller_line(item)
        meta_parts = [verified_part]
        if meta["show_condition"]:
            meta_parts.append(condition_label(item.get("itemCondition")) or FALLBACK["condition"])
        meta_parts.append(item.get("location") or FALLBACK["location"])
        meta_parts.append(item.get("distanceNote") or "")
        meta_line = " · ".join(esc(part) for part in meta_parts if part)
        # 七项⑦链接：评语条尾部给网页版入口（卡片整体是进详情页的动作，不是链接）
        web_link = (f'<a class="fnote-link" href="{esc(item["url"])}" target="_blank" '
                    f'rel="noreferrer">网页版打开 ↗</a>') if item.get("url") else ""
        cards.append(
            f'<article class="fcard">'
            f'<div class="fcard-tap" role="button" tabindex="0" {open_act}>'
            f'{media}<div class="fcard-body">{flat_badges}'
            f'<h3 class="fcard-title">{esc(item.get("title") or FALLBACK["title"])}</h3>'
            f'<p class="fcard-meta">{meta_line}</p>'
            f'<div class="fcard-foot">'
            f'<span class="fcard-seller">{avatar(None, seller_foot, "av-18")}'
            f'<span class="fcard-seller-name">{esc(seller_foot)}</span></span>'
            f'<span class="fcard-price">{esc(price_text(item))}</span>'
            f'</div></div></div>'
            f'<div class="fnote"><b>AI 的看法</b>'
            f'<span>{esc(item.get("aiNote") or FALLBACK["note"])}</span>{web_link}</div>'
            f'</article>')
    query = payload.get("query")
    head = (f'<p class="search-head">「{esc(query)}」的结果</p>' if query else "")
    return head + "".join(cards)


# ---------------------------------------------------------------- 商品详情页


def listing_view(payload: dict) -> str:
    listing = payload.get("listing") or {}
    photos = [str(u) for u in (listing.get("photos") or []) if str(u).startswith("https://")]

    if photos:
        thumbs = "".join(
            f'<button type="button" class="gthumb{" on" if i == 0 else ""}" '
            f'data-thumb="{esc(u)}"><img src="{esc(u)}" alt=""></button>'
            for i, u in enumerate(photos))
        count = (f'<span class="gcount">1/{len(photos)}</span>' if len(photos) > 1 else "")
        gallery = (f'<div class="gallery"><div class="gmain">'
                   f'<img id="gmain-img" src="{esc(photos[0])}" alt="" data-ratio>{count}</div>'
                   + (f'<div class="gthumbs">{thumbs}</div>' if len(photos) > 1 else "")
                   + "</div>")
    else:
        gallery = f'<div class="fcard-nomedia gallery-empty">{esc(FALLBACK["cover"])}</div>'
    photo_note = ('<p class="photo-note">参考图 · TA 想要的大概是这样，不是实物</p>'
                  if is_wanted(listing) and photos else "")

    meta = card_meta_of(listing)
    chips = []
    if listing.get("category"):
        chips.append(f'<span class="chip">{esc(listing["category"])}</span>')
    if meta["show_condition"]:
        prefix = "最低接受 " if is_wanted(listing) else ""
        chips.append(f'<span class="chip">{prefix}{esc(condition_label(listing.get("itemCondition")) or FALLBACK["condition"])}</span>')
    if listing.get("negotiable"):
        chips.append('<span class="chip">可议价</span>')
    chips.append(f'<span class="chip">{esc(listing.get("location") or FALLBACK["location"])}</span>')
    for method in (listing.get("deliveryMethods") or []):
        label = {"PICKUP": "自提", "SHIPPING": "邮寄", "LOCAL_DELIVERY": "同城送"}.get(method)
        if label:
            chips.append(f'<span class="chip">{esc(label)}</span>')

    flaw = (f'<div class="flaw"><b>瑕疵说明</b><span>{esc(listing["flawNote"])}</span></div>'
            if listing.get("flawNote") else "")
    repost_alert = ('<div class="alert">这条帖子从小红书转载，站内私信无人在守 —— '
                    '要联系得去原帖。</div>' if payload.get("isRepost") else "")

    verified = listing.get("sellerVerifiedSchool")
    seller_name = listing.get("sellerNickname") or FALLBACK["seller"]
    seller_card = (f'<div class="seller">{avatar(listing.get("sellerUserId"), seller_name, "av-44")}'
                   f'<span class="seller-name">{esc(seller_name)}</span>'
                   + (f'<span class="verify">🎓 {esc(verified)}</span>' if verified else "")
                   + "</div>")

    if payload.get("canNegotiate"):
        cta = (f'<div class="cta-dock"><button type="button" class="cta" '
               f'{act({"type": "ai_negotiate", "listingId": listing.get("listingId")})}>'
               f'让 AI 帮我聊聊</button></div>')
    else:
        # 🔴 不给入口时也要说清为什么（自己的帖子 / 转载帖），不是画个灰按钮了事
        cta = (f'<div class="cta-dock"><button type="button" class="cta" disabled>让 AI 帮我聊聊</button>'
               f'<p class="cta-blocked">{esc(payload.get("blockedReason") or "")}</p></div>')

    return (f'<div class="detail-bar"><button type="button" class="backbtn" '
            f'{act({"type": "back"})} aria-label="返回">‹</button>'
            f'<span class="detail-bar-title">帖子详情</span></div>'
            f'{gallery}{photo_note}'
            f'<h1 class="dtitle">{esc(listing.get("title") or FALLBACK["title"])}</h1>'
            f'<div class="dprice-row"><span class="dprice">{esc(price_text(listing))}</span>'
            f'{card_badges(listing, on_cover=False)}</div>'
            f'<div class="dchips">{"".join(chips)}</div>'
            f'{flaw}{repost_alert}{seller_card}'
            f'<div class="desc">{esc(listing.get("description") or FALLBACK["description"])}</div>'
            f'{cta}')


# ---------------------------------------------------------------- 私信页（列表 ↔ 串详情两页切换）


def messages_view(payload: dict) -> str:
    """与 Web 移动端同构：列表与串详情是两个页面态，靠 activeThreadId 切。

    字段名取自 ConversationDTO / MessageDTO（DtoFieldNames 闸钉着，别凭印象改）。
    串状态 NEW/CONTACTED/DEALT/CLOSED **不显示** —— Web 端就不显示，对齐。
    """
    if payload.get("activeThreadId"):
        return _thread_page(payload)
    return _thread_list(payload)


def _thread_list(payload: dict) -> str:
    threads = payload.get("threads") or []
    if not threads:
        return f'<div class="empty">{esc(FALLBACK["no_thread"])}</div>'
    rows = []
    for thread in threads:
        role = trade_role_label(
            # peer 的结构角色 = 我的对侧：myRole 是 SELLER 则对方是访客（BUYER 结构位）
            "BUYER" if thread.get("myRole") == "SELLER" else "SELLER",
            thread.get("tradeType"))
        rows.append(
            f'<div class="mrow" role="button" tabindex="0" '
            f'{act({"type": "open_thread", "threadId": thread["threadId"]})}>'
            f'{avatar(thread.get("peerUserId"), thread.get("peerNickname"), "av-48")}'
            f'<span class="mrow-body">'
            f'<span class="mrow-line1"><span class="mrow-title">'
            f'{esc(thread.get("listingTitle") or "这个帖子")}</span>'
            f'<span class="mrow-time">{esc(relative_time(thread.get("lastCreatedAt")))}</span></span>'
            f'<span class="mrow-line2"><span class="mrow-peer">'
            f'{esc(role)} · {esc(thread.get("peerNickname") or FALLBACK["peer"])}</span>'
            f'<span class="mrow-last">{esc(thread.get("lastContent") or FALLBACK["no_message"])}</span>'
            f'</span></span></div>')
    return '<p class="mtitle">私信</p>' + "".join(rows)


def _bubble_groups(messages: list, my_role: str | None) -> str:
    """气泡分组（对 MessageThread 的移植）：同侧 + 同天 + 间隔 ≤5 分钟合一组，
    头像挂组首、时间挂组尾、跨天插日期条。**不重排序**，沿用服务端顺序。
    我方判定用结构角色对比（求购帖上业务角色相反，比业务角色会画反）。"""
    if not messages:
        return f'<div class="empty">{esc(FALLBACK["pick_thread"])}</div>'

    def parse(iso):
        try:
            return datetime.fromisoformat(str(iso).replace(" ", "T"))
        except (ValueError, TypeError):
            return None

    parts, prev_time, prev_mine = [], None, None
    for i, message in enumerate(messages):
        mine = message.get("senderRole") == my_role
        this_time = parse(message.get("createdAt"))
        new_day = (prev_time is None or (this_time and prev_time
                                         and this_time.date() != prev_time.date()))
        if new_day and this_time:
            parts.append(f'<p class="daynote">{this_time.month}月{this_time.day}日</p>')
        gap = ((this_time - prev_time).total_seconds()
               if this_time and prev_time else None)
        new_group = new_day or mine != prev_mine or gap is None or gap > 300
        if new_group and parts and not new_day and prev_time is not None:
            pass  # 组间距交给 CSS margin
        side = "me" if mine else "peer"
        head = ""
        if new_group and not mine:
            head = avatar(None, "对", "av-32 bub-av")
        rail = head or ('<span class="bub-spacer"></span>' if not mine else "")
        parts.append(f'<div class="brow {side}">{rail}'
                     f'<span class="bub">{esc(message.get("content"))}</span></div>')
        next_msg = messages[i + 1] if i + 1 < len(messages) else None
        next_time = parse(next_msg.get("createdAt")) if next_msg else None
        group_ends = (next_msg is None
                      or (next_msg.get("senderRole") == my_role) != mine
                      or (next_time and this_time and
                          (next_time.date() != this_time.date()
                           or (next_time - this_time).total_seconds() > 300)))
        if group_ends and this_time:
            parts.append(f'<p class="btime {side}">{this_time.strftime("%H:%M")}</p>')
        prev_time, prev_mine = this_time or prev_time, mine
    return "".join(parts)


def _thread_page(payload: dict) -> str:
    peer_role = trade_role_label(
        "BUYER" if payload.get("myRole") == "SELLER" else "SELLER",
        payload.get("tradeType"))
    peer = payload.get("peerNickname") or FALLBACK["peer"]
    listing_link = ""
    if payload.get("listingTitle"):
        if payload.get("listingUrl"):
            listing_link = (f'<a class="thead-listing" href="{esc(payload["listingUrl"])}" '
                            f'target="_blank" rel="noreferrer">{esc(payload["listingTitle"])}</a>')
        else:
            listing_link = f'<span class="thead-listing">{esc(payload["listingTitle"])}</span>'
    header = (f'<div class="thead">'
              f'<button type="button" class="backbtn" {act({"type": "back_to_threads"})} '
              f'aria-label="返回列表">‹</button>'
              f'{avatar(payload.get("peerUserId"), peer, "av-36")}'
              f'<span class="thead-body"><span class="thead-name">{esc(peer)}'
              f'<span class="thead-role">{esc(peer_role)}</span></span>{listing_link}</span>'
              f'<button type="button" class="aireply" '
              f'{act({"type": "ai_reply", "threadId": payload.get("activeThreadId")})}>'
              f'让 AI 代回</button></div>')
    bubbles = _bubble_groups(payload.get("messages") or [], payload.get("myRole"))
    composer = (f'<form class="composer" data-send-thread="{esc(payload.get("activeThreadId"))}">'
                f'<div class="composer-error" hidden></div>'
                f'<div class="composer-row">'
                f'<textarea class="composer-input" rows="1" aria-label="私信内容" '
                f'placeholder="发消息给{esc(peer_role)}…"></textarea>'
                f'<button type="submit" class="composer-send" disabled aria-label="发送">↑</button>'
                f'</div></form>')
    return header + f'<div class="bubbles">{bubbles}</div>' + composer


VIEWS = {"search": search_view, "listing": listing_view, "messages": messages_view}

HINTS = {"search": "点一张卡看详情", "listing": "想聊就点「让 AI 帮我聊聊」",
         "messages": "可以直接打字回，也可以让 AI 代回"}


def render_fragment(state: dict) -> str:
    """一屏的 HTML 片段。页面拿它换 `#view` 的 innerHTML；首屏直接内联。"""
    view = VIEWS.get(state.get("view"))
    return view(state.get("payload") or {}) if view else ""


def nav_html(active_view: str) -> str:
    """顶部两格胶囊导航。详情页归在「搜索结果」一侧（它从搜索点进来）。"""
    search_on = " on" if active_view in ("search", "listing") else ""
    messages_on = " on" if active_view == "messages" else ""
    return (f'<nav class="topnav">'
            f'<button type="button" class="topnav-tab{search_on}" '
            f'{act({"type": "nav", "view": "search"})}>搜索结果</button>'
            f'<button type="button" class="topnav-tab{messages_on}" '
            f'{act({"type": "nav", "view": "messages"})}>私信</button>'
            f'<span class="dot" id="live"></span></nav>')


SHELL = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>A2H Market · 摊开看</title>
<link rel="stylesheet" href="/assets/deskui.css?k=__TOKEN__"></head>
<body>
<div class="shell">
  __NAV__
  <div id="view">__VIEW__</div>
</div>
<div id="lock" class="lock" hidden>
  <div class="lock-card">
    <span class="spinner" aria-hidden="true"></span>
    <p class="lock-hint" id="lock-hint"></p>
    <p class="lock-since" id="lock-since"></p>
    <button type="button" class="lock-unlock" id="lock-unlock" hidden>解除锁定</button>
  </div>
</div>
<script type="application/json" id="boot">__BOOT__</script>
<script src="/assets/deskui.js?k=__TOKEN__"></script>
</body></html>
"""


def render_page(state: dict, token: str) -> str:
    """整页壳。首屏片段直接内联（省一次白屏）；动态状态经 JSON 数据岛给 JS
    （CSP 下不执行、不需要内联脚本）。集市文本以 JSON 字符串身份进数据岛，
    `</` 拆开防止商品描述里的 `</script>` 提前关掉标签。"""
    boot = {"revision": int(state.get("revision", 0)),
            "view": state.get("view"),
            "hint": HINTS.get(state.get("view"), ""),
            "busy": state.get("busy")}
    boot_json = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
    return (SHELL
            .replace("__NAV__", nav_html(state.get("view") or "search"))
            .replace("__VIEW__", render_fragment(state))
            .replace("__BOOT__", boot_json)
            .replace("__TOKEN__", html.escape(token, quote=True)))
