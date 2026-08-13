#!/usr/bin/env python3
"""desk UI 的两屏模板（搜索结果 / 商品详情；服务端渲染，移动端布局，640px 居中）。

私信页 0812 拍板下线（网页私信链路停用，与 Web 端同步）——本模块只剩「看」的两屏。

CSS 与页面 JS 住在 `assets/`（0.38.0 载荷闸开的口，仅该目录允许 .css/.js），
本模块只负责把数据渲成 HTML 片段。

🔴 **为什么是服务端渲染**（实验第一版写成了前端模板，改掉了）：
前端模板意味着「兜底文案」只是 JS 源码里的一个字符串常量 —— 测试断言它出现在页面里
**永远是绿的**，哪怕渲染逻辑整个是坏的。那是假绿，比没有测试更糟。
两屏都在 Python 里渲成 HTML 片段，页面只负责换 `innerHTML` 与转发点击：
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

# 移植自 frontend/src/pages/ListingDetail.tsx 的 CONTACT_TYPE_LABEL（防漂移闸对照 TS 源码）。
# 开放小写串，表外类型原样展示；邮箱在最前由服务端排序保证，模板照序渲染即可。
CONTACT_TYPE_LABEL = {"email": "邮箱", "wechat": "微信", "wechat_qr": "微信二维码",
                      "xiaohongshu": "小红书", "whatsapp": "WhatsApp",
                      "instagram": "Instagram", "phone": "电话"}


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


def card_badges(listing: dict) -> str:
    """帖型徽章 + （非在售时）状态徽章。**一律不压图**（2026-08 设计定稿），
    统一放标题上方，只剩 flat 一档底色。

    tone 两维取并（同 ListingCard 的 CardTag）：求购 ∨ accent=strong → wanted 档
    （紫，文字用 --yx-tag-strong 保对比度）；否则 plain 档。
    """
    meta = card_meta_of(listing)
    wanted = is_wanted(listing)
    label = meta["buy"] if wanted else meta["sell"]
    tone = "wanted" if (wanted or meta["accent"] == "strong") else "plain"
    badges = [f'<span class="badge badge-{tone}">{esc(label)}</span>']
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
}


def esc(value) -> str:
    """一切集市文本的唯一出口。属性和文本节点都走它（`quote=True`）。"""
    return html.escape("" if value is None else str(value), quote=True)


def act(action: dict) -> str:
    """把一个动作挂到元素上。服务端收到后仍会比对 `legal_actions`，这里只是搬运。"""
    return f"data-act='{esc(json.dumps(action, ensure_ascii=False))}'"


# 返回箭头 / 学士帽（2026-08 设计定稿：Web 同款 SVG，弃 `‹` 字符与 🎓 emoji——
# 前者基线对不齐是老问题，后者跨平台渲染不稳）。集市文本永远进不了这两段。
ARROW_SVG = ('<svg viewBox="0 0 24 24" width="16" height="16" fill="none" '
             'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
             'stroke-linejoin="round" aria-hidden="true">'
             '<path d="M19 12H5"/><path d="m12 19-7-7 7-7"/></svg>')
SCHOLAR_SVG = ('<svg viewBox="0 0 24 24" width="11" height="11" fill="none" '
               'stroke="currentColor" stroke-width="1.8" stroke-linecap="round" '
               'stroke-linejoin="round" aria-hidden="true">'
               '<path d="M22 10v6M2 10l10-5 10 5-10 5z"/>'
               '<path d="M6 12v5c3 3 9 3 12 0v-5"/></svg>')


def relative_time_note(iso: str | None) -> str:
    """「x 前」的近似口径（发帖人卡的确认在售副行用）。解析不了就空着不猜。"""
    if not iso:
        return ""
    try:
        then = datetime.fromisoformat(str(iso).replace(" ", "T").replace("Z", "+00:00"))
    except ValueError:
        return ""
    now = datetime.now(then.tzinfo) if then.tzinfo else datetime.now()
    seconds = (now - then).total_seconds()
    if seconds < 60:
        return "刚刚"
    if seconds < 3600:
        return f"{int(seconds // 60)} 分钟前"
    if seconds < 86400:
        return f"{int(seconds // 3600)} 小时前"
    return f"{int(seconds // 86400)} 天前"


# 确定性预设头像（对 frontend/src/lib/presetAvatar.ts 的简化）：id 哈希挑一对
# tokens 里的 avatar-* 渐变色 + 昵称首字符。同一个人永远同一个色。
_AVATAR_PAIRS = 6  # av-0 … av-5，色值在 deskui.css


def avatar(user_id, nickname, size_class: str) -> str:
    seed = str(user_id or nickname or "?")
    index = int(hashlib.md5(seed.encode("utf-8")).hexdigest(), 16) % _AVATAR_PAIRS
    initial = (str(nickname).strip()[:1] or "?") if nickname else "?"
    return (f'<span class="av av-{index} {size_class}" aria-hidden="true">'
            f'{esc(initial)}</span>')


# ---------------------------------------------------------------- 搜索结果页（2026-08 设计定稿 3a：单卡整合）


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


def _first_paragraph(text) -> str:
    """无图卡的描述补位取**首段**（第一个非空行）。"""
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


def search_view(payload: dict) -> str:
    items = payload.get("items") or []
    if not items:
        return '<div class="empty">这一屏还没有东西 —— 等 agent 摊出来</div>'
    parts = []
    query = payload.get("query")
    if query:
        parts.append(f'<h1 class="page-title">「{esc(query)}」</h1>')
    # AI 搜索摘要卡（设计定稿 3a）：agent 的呈现纪律四件套摊在这里；缺省整卡不渲染
    if payload.get("summary"):
        parts.append(f'<section class="sumcard"><b>AI 搜索摘要</b>'
                     f'<p>{esc(payload["summary"])}</p></section>')
    for item in items:
        meta = card_meta_of(item)
        open_act = act({"type": "open_listing", "listingId": item["listingId"]})
        # meta 行：✓校名 · 成色(按帖型显隐) · 位置 · 远近 —— 缺项写「未知/未写」，
        # 成色是唯一合法缺席（showCondition=false 的帖型，错位比缺席更误导）
        verified_part, seller_foot = _seller_line(item)
        meta_parts = [verified_part]
        if meta["show_condition"]:
            meta_parts.append(condition_label(item.get("itemCondition")) or FALLBACK["condition"])
        meta_parts.append(item.get("location") or FALLBACK["location"])
        meta_parts.append(item.get("distanceNote") or "")
        meta_line = " · ".join(esc(part) for part in meta_parts if part)
        foot = (f'<div class="scard-foot">'
                f'<span class="scard-seller">{avatar(None, seller_foot, "av-18")}'
                f'<span class="scard-seller-name">{esc(seller_foot)}</span></span>'
                f'<span class="scard-price">{esc(price_text(item))}</span></div>')
        title = f'<h3 class="scard-title">{esc(item.get("title") or FALLBACK["title"])}</h3>'
        # 七项①图：有图 = 左图右文；无图 = 去掉图位，描述首段补位（**禁止静默留白**——
        # 没描述也有 coverNote/兜底文案顶上，主人得知道这条为什么没图）
        if item.get("cover"):
            body = (f'<div class="scard-body">'
                    f'<div class="scard-media"><img src="{esc(item["cover"])}" alt="" '
                    f'data-ratio loading="lazy" decoding="async"></div>'
                    f'<div class="scard-main">{title}'
                    f'<p class="scard-meta">{meta_line}</p>{foot}</div></div>')
        else:
            desc = (_first_paragraph(item.get("description"))
                    or item.get("coverNote") or FALLBACK["cover"])
            body = (f'{title}<p class="scard-desc">{esc(desc)}</p>'
                    f'<p class="scard-meta">{meta_line}</p>{foot}')
        # 七项⑦链接：评语条尾缀给网页版入口（卡片整体是进详情页的动作，不是链接）
        web_link = (f'<a class="scard-ai-link" href="{esc(item["url"])}" target="_blank" '
                    f'rel="noreferrer">网页版 ↗</a>') if item.get("url") else ""
        ai_strip = (f'<div class="scard-ai"><span><b>AI：</b>'
                    f'{esc(item.get("aiNote") or FALLBACK["note"])} {web_link}</span></div>')
        parts.append(
            f'<article class="scard" role="button" tabindex="0" {open_act}>'
            f'<div class="scard-badges">{card_badges(item)}</div>'
            f'{body}{ai_strip}</article>')
    return "".join(parts)


# ---------------------------------------------------------------- 商品详情页


def listing_view(payload: dict) -> str:
    listing = payload.get("listing") or {}
    photos = [str(u) for u in (listing.get("photos") or []) if str(u).startswith("https://")]

    if photos:
        thumbs = "".join(
            f'<button type="button" class="gthumb{" on" if i == 0 else ""}" '
            f'data-thumb="{esc(u)}"><img src="{esc(u)}" alt="" '
            f'loading="lazy" decoding="async"></button>'
            for i, u in enumerate(photos))
        count = (f'<span class="gcount">1/{len(photos)}</span>' if len(photos) > 1 else "")
        # 主图容器钳制 4:3~1:1（设计定稿收紧线上 3:4 上限；data-ratio-min 给 JS 读）、
        # 点大图放大（deskui.js 的本地行为，不产生回传动作）
        hint = (f'左滑查看全部 {len(photos)} 张 · 点大图放大' if len(photos) > 1
                else '点大图放大')
        gallery = (f'<div class="gallery"><div class="gmain">'
                   f'<img id="gmain-img" src="{esc(photos[0])}" alt="" data-ratio '
                   f'data-ratio-min="1" data-lightbox decoding="async" '
                   f'fetchpriority="high">{count}</div>'
                   + (f'<div class="gthumbs">{thumbs}</div>' if len(photos) > 1 else "")
                   + f'<p class="ghint">{hint}</p></div>')
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
    chips.append(f'<span class="chip">{esc(listing.get("location") or FALLBACK["location"])}</span>')
    for method in (listing.get("deliveryMethods") or []):
        label = {"PICKUP": "自提", "SHIPPING": "邮寄", "LOCAL_DELIVERY": "同城送"}.get(method)
        if label:
            chips.append(f'<span class="chip">{esc(label)}</span>')

    # 主信息卡（设计定稿 3b 信息三段式之一）：徽章 → 标题 → 价格行（「可议价」从
    # chips 移进来同行）→ chips → 分隔线下并入瑕疵说明（无 flawNote 整段不渲染）
    negotiable = ('<span class="dnegotiable">可议价</span>'
                  if listing.get("negotiable") else "")
    flaw = (f'<div class="dflaw"><b>瑕疵说明</b><span>{esc(listing["flawNote"])}</span></div>'
            if listing.get("flawNote") else "")
    main_card = (f'<section class="dcard">'
                 f'<div class="scard-badges">{card_badges(listing)}</div>'
                 f'<h1 class="dtitle">{esc(listing.get("title") or FALLBACK["title"])}</h1>'
                 f'<div class="dprice-row"><span class="dprice">{esc(price_text(listing))}</span>'
                 f'{negotiable}</div>'
                 f'<div class="dchips">{"".join(chips)}</div>{flaw}</section>')

    # 转载 Alert 文案对齐 Web（ListingDetail.tsx）：有链接引导去原帖，没链接给搜索指引
    repost_alert = ""
    if payload.get("isRepost"):
        guide = ("请点下方按钮前往原帖联系卖家。" if payload.get("repostUrl")
                 else "请去小红书搜索标题里的原作者联系卖家。")
        repost_alert = (f'<div class="alert"><b>转载自小红书</b>'
                        f'这条帖子是从小红书转载的，发帖账号无法站内私信。{guide}</div>')

    # 发帖人卡：副行「x 前确认在售」只在卖家真确认过时出现（refreshedAt 明显晚于
    # createdAt 才是确认信号——Web 端 0807 两时间点模型的同款判定，别拿发布时间冒充）
    verified = listing.get("sellerVerifiedSchool")
    seller_name = listing.get("sellerNickname") or FALLBACK["seller"]
    confirmed = ""
    refreshed, created = listing.get("refreshedAt"), listing.get("createdAt")
    if refreshed and created and str(refreshed) > str(created):
        note = relative_time_note(refreshed)
        confirmed = f'<span class="seller-sub">{esc(note)}确认在售</span>' if note else ""
    school = (f'<span class="verify">{SCHOLAR_SVG}<span>{esc(verified)}</span></span>'
              if verified else "")
    seller_card = (f'<section class="dcard dseller">'
                   f'{avatar(listing.get("sellerUserId"), seller_name, "av-44")}'
                   f'<span class="seller-body"><span class="seller-name">{esc(seller_name)}</span>'
                   f'{confirmed}</span>{school}</section>')

    # CTA 分支**照抄 Web 端 ctaLabel**（ListingDetail.tsx，null = 这一态没有 CTA）：
    # ① 自己的帖子 → 无 CTA；② 转载有链接 → 外跳原帖；③ 转载没链接 → 无 CTA
    # （上方 Alert 已给「去小红书搜原作者」指引）；④ 正常 → 「查看联系方式」
    # （0812 拍板：替代私信入口，弹层展示、邮箱最前）。
    if payload.get("canContact"):
        cta = (f'<div class="cta-dock"><button type="button" class="cta" '
               f'{act({"type": "view_contacts", "listingId": listing.get("listingId")})}>'
               f'查看联系方式</button></div>')
    elif payload.get("isRepost") and payload.get("repostUrl"):
        cta = (f'<div class="cta-dock"><a class="cta cta-link" '
               f'href="{esc(payload["repostUrl"])}" target="_blank" rel="noreferrer">'
               f'去小红书原帖联系卖家</a></div>')
    else:
        cta = ""

    loading_state = ('<div class="load-state" role="status">'
                     '<span class="spinner" aria-hidden="true"></span>'
                     '<span>正在补全最新详情…</span></div>' if payload.get("loading") else "")
    load_error = (f'<div class="load-state load-error" role="alert">'
                  f'{esc(payload.get("loadError"))}</div>' if payload.get("loadError") else "")

    return (f'<div class="detail-bar"><button type="button" class="backbtn" '
            f'{act({"type": "back"})} aria-label="返回">{ARROW_SVG}</button>'
            f'<span class="detail-bar-title">帖子详情</span></div>'
            f'{loading_state}{load_error}{gallery}{photo_note}'
            f'{main_card}{repost_alert}'
            f'<div class="desc">{esc(listing.get("description") or FALLBACK["description"])}</div>'
            f'{seller_card}'
            f'{cta}<div id="overlay-root">{_contacts_modal(payload)}</div>')


def _contacts_modal(payload: dict) -> str:
    """联系方式弹层（对齐 ListingDetail.tsx 的弹层：邮箱最前提示、逐条复制、关闭）。

    服务端渲染：数据在 sidecar 取好放进载荷才出现这一段；「复制」按钮是页面上
    唯一的本地行为（clipboard，deskui.js 处理 data-copy），不产生回传动作。
    点背板关闭 = `close_contacts` 动作（卡片内的点击不冒泡到背板，JS 判 data-backdrop）。
    """
    if not payload.get("contactsOpen"):
        return ""
    contacts = payload.get("contacts") or []
    if payload.get("contactsLoading"):
        rows = ('<div class="modal-loading" role="status">'
                '<span class="spinner" aria-hidden="true"></span>'
                '<span>正在获取联系方式…</span></div>')
    elif payload.get("contactError"):
        rows = f'<p class="contact-error" role="alert">{esc(payload["contactError"])}</p>'
    elif contacts:
        rows = "".join(
            f'<div class="contact-row">'
            f'<span class="contact-type">{esc(CONTACT_TYPE_LABEL.get(c.get("type"), c.get("type")))}</span>'
            f'<span class="contact-value">{esc(c.get("value"))}</span>'
            f'<button type="button" class="copybtn" data-copy="{esc(c.get("value"))}">复制</button>'
            f'</div>'
            for c in contacts if isinstance(c, dict) and c.get("value"))
    else:
        rows = '<p class="contact-empty">发帖人没留下联系方式</p>'
    return (f'<div class="modal-backdrop" {act({"type": "close_contacts"})} data-backdrop>'
            f'<div class="modal-card" role="dialog" aria-modal="true" aria-label="发帖人联系方式">'
            f'<p class="modal-title">联系方式</p>'
            f'<p class="modal-hint">优先用邮箱联系——对方不常上站也能收到。</p>'
            f'{rows}'
            f'<button type="button" class="modal-close" {act({"type": "close_contacts"})}>'
            f'关闭</button></div></div>')


def render_overlay(state: dict) -> str:
    """同一详情页内只替换弹层，避免重新创建图片节点与重置图集状态。"""
    if state.get("view") != "listing":
        return ""
    return _contacts_modal(state.get("payload") or {})


VIEWS = {"search": search_view, "listing": listing_view}

HINTS = {"search": "点一张卡看详情", "listing": "想联系发帖人就点「查看联系方式」"}

TITLE = "A2H Market · 摊开看"


def render_fragment(state: dict) -> str:
    """一屏的 HTML 片段。页面拿它换 `#view` 的 innerHTML；首屏直接内联。"""
    view = VIEWS.get(state.get("view"))
    return view(state.get("payload") or {}) if view else ""


SHELL = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta name="referrer" content="no-referrer">
<title>__TITLE__</title>
<link rel="stylesheet" href="/assets/deskui.css?k=__TOKEN__"></head>
<body>
<span class="dot" id="live"></span>
<div class="shell">
  <div id="view">__VIEW__</div>
</div>
<div id="toast" class="toast" role="status" hidden></div>
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
            "hint": HINTS.get(state.get("view"), "")}
    boot_json = json.dumps(boot, ensure_ascii=False).replace("</", "<\\/")
    return (SHELL
            .replace("__TITLE__", TITLE)
            .replace("__VIEW__", render_fragment(state))
            .replace("__BOOT__", boot_json)
            .replace("__TOKEN__", html.escape(token, quote=True)))
