#!/usr/bin/env python3
"""一图多物的图像加工器：把「模型看图报出的框」画成主人能看、能发的四种图。

    A 确认图    原图 + 每件一个彩框 + 同色标签（序号 + 名称）——给主人过目用
    B 聚焦封面  其余压暗、本帖商品保持原亮度 + 框 + 名——零裁剪、零信息丢失
    C 裁剪图    按框裁出单件——主人自己下载了发群用
    D 价签总览  每件旁贴「序号 + 名称 + 挂价」价签；清单里标着已出的画叉、保留原号

🔴 三条设计前提，改这个文件之前先读：

1. **本脚本没有记忆，也不许有。** 框、名称、序号全部由调用方当轮传进来，
   出图之后除了临时目录里那几张图，磁盘上不留任何东西。不要在这里加缓存、
   加状态文件、加「下次记住这个框」—— 那是在给一个刻意不持久化的设计开后门。

2. **序号不是本脚本发明的。** 帖子发布之后，序号的唯一真相源是帖子描述里的
   分件清单（一件一行、行首编号）。所以 `--numbering listing` 这条路要求调用方
   **先把描述读回来**，本脚本从描述里解析出号、名、价、是否已出，照着画；
   调用方给的名字只用来**核对**，对不上就拒绝出图。
   `--numbering fresh` 只服务「还没发布」的那一刻，且强制**从 `--first-number` 起连续**
   （默认 1，即 1..N）—— 已发布的批次几乎必然有跳号或已出行，冒充 fresh 会当场被拒。
   多图分批时第二张图的件要接着上一张往下编（`--first-number 4`），那是调用方
   **当轮说出来的一句声明**：本脚本不记也不猜上一批用到几号，声明与手里的号对不上
   就当场拒。「让它自己记住上次编到哪」= 前提 1 说的那种后门。
   为什么要拒到这个程度：图上 3 号 = 清单 4 号是一种**静默**错位，
   没有任何地方会报错，而买家是点着号来问的。

3. **零依赖承诺只覆盖 a2hmarket.py，不覆盖本脚本。** 本脚本要 Pillow；
   `a2hmarket.py` 不 import 它，也不调用它，装不上 Pillow 的机器上既有功能一条不少。
   本脚本自己在 Pillow 缺席时返回结构化的「不可用」+ 安装引导，不吐栈、不假装出了图。

输出一律是**一行 JSON 到 stdout**（成功 `{"ok": true, ...}`，失败 `{"ok": false, "error": ...}`），
调用方按 `ok` 判，不要去解析人话。exit code：0 成功 / 2 入参不对 / 3 编号闸拒绝 / 4 Pillow 缺席。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sys
import tempfile
from pathlib import Path

# ---------------------------------------------------------------- 常量（都注来源）

# 压暗系数：其余部分乘这个亮度。0810 POC 用 0.35 实测偏暗，主人看不清没被选中的那几件
# 长什么样；0.5 是那次目测调出来的值。它是**产品手感**，不是算法参数，可调。
DIM_FACTOR = 0.5

# 上传口硬拒 > 10MB（a2hmarket.py 的 photo upload）。存图前先把最长边缩到这个数以内，
# 免得加工完才发现传不上去。JPEG 质量 90 是 POC 的取值，再低会让瑕疵看不清 ——
# 而瑕疵看不看得清正是买家判断成色的依据。
MAX_EDGE = 2560
JPEG_QUALITY = 90
MAX_BYTES = 10 * 1024 * 1024

# 一次画几件。超过这个数，标签必然互相压到没地方放，主人也过不了目 ——
# 那时该做的是拆图重拍，不是硬画。
MAX_ITEMS = 20

# 颜色是**唯一的锚定手段**：标签色 = 框色，框重叠时主人也能把标签和框对上。
# 取值取自 Material 基础色，彼此在缩略图尺寸下也分得开。
PALETTE = [
    (66, 133, 244), (234, 67, 53), (52, 168, 83), (251, 140, 0), (156, 39, 176),
    (0, 172, 193), (216, 27, 96), (124, 179, 66), (94, 53, 177), (109, 76, 65),
]

# 字体探测：**不按字体名字信任，逐字符实测渲染**。
# 0810 POC 踩过：Hiragino Sans GB 名字看着像全能中文字体，实际缺 `£` 的字形，
# 渲出来是一个方框，而那张图上正好每件都标着英镑价。
# 下面这张表只是**先试哪些**（多数机器上它们的覆盖面最好），不是白名单：
# 表里的全试不过就继续试目录里扫到的其它字体，全都不过才降级。
FONT_HINTS = (
    "arial unicode", "notosanscjk", "noto sans cjk", "sourcehansans",
    "source han sans", "pingfang", "songti", "stheiti", "heiti",
    "microsoft yahei", "msyh", "simsun", "simhei", "wqy", "droidsansfallback",
    "hiragino", "dejavusans", "liberationsans", "arial",
)
FONT_DIRS = (
    "/System/Library/Fonts", "/System/Library/Fonts/Supplemental",
    "/Library/Fonts", "~/Library/Fonts",
    "/usr/share/fonts", "/usr/local/share/fonts", "~/.fonts", "~/.local/share/fonts",
    "C:/Windows/Fonts",
)
FONT_SUFFIXES = (".ttf", ".otf", ".ttc", ".otc")
# 必须能渲的最小字符集：CJK + 四种货币符号 + 数字。加上当轮真要画的那些字，
# 两者都过才算这个字体可用。
BASE_GLYPHS = "0123456789£$¥€件号价已出中文"
# 平面 15 私用区，正常字体不会给它字形 —— 拿它渲出来的位图当 .notdef 的样子，
# 谁跟它长得一样就是缺字形。这是**实测**，不是查表，所以对任何字体都成立。
NOTDEF_PROBE = "\U000f0000"

TMP_PREFIX = "a2hmarket-annotate-"

# 分件清单的行：`3. 山地自行车（单车）——£60 · ❌已出`
LIST_ROW = re.compile(r"^\s*(\d{1,3})\s*[.、．]\s*(\S.*)$")
SOLD_MARK = re.compile(r"(❌\s*已出|已出售|\b已出\b|已出$|已出\s)")
NAME_SPLIT = re.compile(r"—{1,2}|--|――")
PAREN = re.compile(r"[（(][^）)]*[）)]")


class Refused(Exception):
    """带 error code 的拒绝。exit code 由 CODES 决定。"""

    def __init__(self, code: str, message: str, **extra):
        super().__init__(message)
        self.code, self.message, self.extra = code, message, extra


CODES = {
    "bad_input": 2,
    "numbering_unverifiable": 3,
    "numbering_mismatch": 3,
    "numbering_not_fresh": 3,
    "pil_missing": 4,
    "render_failed": 5,
}


# ---------------------------------------------------------------- Pillow 探测


def probe_pil():
    """探测 Pillow。缺席时**不吐栈**，返回结构化的不可用 + 安装引导。

    这里刻意不 `pip install` —— 往主人机器上装东西是主人的决定，我们只引导。
    """
    try:
        from PIL import Image, ImageDraw, ImageEnhance, ImageFont  # noqa: F401
    except Exception as error:  # ImportError 之外还可能是装坏了的 _imaging
        return None, {
            "available": False,
            "detail": f"{type(error).__name__}: {error}",
            "install": "pip install Pillow",
            "degrade": (
                "画不了标注图，但流程不断：逐件识别照做、物项表照出（改成文字版，"
                "序号 + 名称 + 大致位置），确认门用文字表照样过，封面用原图。"
            ),
        }
    import PIL

    return (Image, ImageDraw, ImageEnhance, ImageFont), {
        "available": True, "version": getattr(PIL, "__version__", "unknown"),
    }


def require_pil():
    mods, info = probe_pil()
    if mods is None:
        raise Refused(
            "pil_missing",
            "这台机器上没有 Pillow，画不了图。跟主人说一句：装的话一句 "
            "`pip install Pillow`，装完告诉我；不装也不耽误发布，我出文字版物项表。",
            pil=info,
        )
    return mods


# ---------------------------------------------------------------- 字体探测 + 回退链


def _font_candidates():
    """先按 FONT_HINTS 的顺序，再按目录里扫到的其余字体。"""
    seen, ordered, rest = set(), [], []
    for directory in FONT_DIRS:
        base = Path(os.path.expanduser(directory))
        if not base.is_dir():
            continue
        try:
            files = sorted(p for p in base.rglob("*") if p.suffix.lower() in FONT_SUFFIXES)
        except OSError:
            continue
        for path in files:
            key = str(path)
            if key in seen:
                continue
            seen.add(key)
            name = path.name.lower().replace(" ", "").replace("-", "")
            rank = next((i for i, hint in enumerate(FONT_HINTS)
                         if hint.replace(" ", "") in name), None)
            (ordered if rank is not None else rest).append((rank, str(path)))
    ordered.sort(key=lambda item: (item[0], item[1]))
    return [p for _r, p in ordered] + [p for _r, p in rest]


def _glyph_missing(font, char, reference):
    """这个字体有没有 `char` 的字形 —— 渲出来跟 .notdef 一样就是没有。"""
    try:
        mask = font.getmask(char, mode="L")
        return (mask.size, bytes(mask)) == reference
    except Exception:
        return True


def pick_font(ImageFont, text, size, candidates=None):
    """字体回退链：逐个候选**逐字符实测渲染**，缺字形就换下一个。

    返回 (font, info)。整条链都过不了必须画的字时，退回 Pillow 自带的点阵字体，
    并把 `ascii_only` 标成 True —— 那时调用方要改成「只画框 + 序号」，
    名称放到对话里说，别硬塞进图。
    """
    needed = sorted(set(BASE_GLYPHS + "".join(text)) - set(" \t\n"))
    tried = []
    for path in (candidates if candidates is not None else _font_candidates()):
        for index in (0, 1, 2):
            try:
                font = ImageFont.truetype(path, size, index=index)
            except Exception:
                break
            try:
                reference = font.getmask(NOTDEF_PROBE, mode="L")
                reference = (reference.size, bytes(reference))
            except Exception:
                break
            # 自检：探测法本身要是失效了（参考位图跟正常字长得一样），
            # 这个字体的判定结果不可信，跳过而不是信一个错的结论。
            if _glyph_missing(font, "A", reference):
                break
            missing = [c for c in needed if _glyph_missing(font, c, reference)]
            if not missing:
                return font, {"path": path, "index": index, "ascii_only": False,
                              "tried": tried[-6:]}
            tried.append({"path": path, "index": index, "missing": "".join(missing[:6])})
            break
    try:
        fallback = ImageFont.load_default(size=size)
    except TypeError:      # 老版本 Pillow 的 load_default 不收 size
        fallback = ImageFont.load_default()
    return fallback, {"path": None, "index": None, "ascii_only": True,
                      "tried": tried[-6:],
                      "note": "没有一个字体能把要画的字渲全 —— 只画框和序号，"
                              "名称在对话里说给主人听"}


# ---------------------------------------------------------------- 编号闸


def parse_list_rows(description):
    """从帖子描述里解析分件清单。解析不出行 = 读不回编号，调用方必须拒绝出图。"""
    rows = {}
    for line in (description or "").splitlines():
        match = LIST_ROW.match(line)
        if not match:
            continue
        number, tail = int(match.group(1)), match.group(2).strip()
        head = NAME_SPLIT.split(tail)[0].strip(" ·—-")
        name = PAREN.sub("", head).strip() or head
        price = ""
        parts = NAME_SPLIT.split(tail)
        if len(parts) > 1:
            # 价签上只放价那一小段：清单行的第一个 `·` 之前、第一个逗号之前。
            # 「送，随任一件附赠」这种整句塞进价签会把标签撑到没处放。
            price = re.split(r"[，,]", parts[1].split("·")[0])[0].strip()
        rows[number] = {
            "no": number, "name": name, "name_full": head,
            "price": price, "sold": bool(SOLD_MARK.search(tail)), "raw": line.strip(),
        }
    return rows


def _norm(text):
    return re.sub(r"[\s·・,，.。()（）\-—_/]+", "", (text or "")).lower()


def _looks_like(a, b):
    """两个名字像不像同一件东西。宽松，但**不宽松到放过错位**。"""
    x, y = _norm(a), _norm(b)
    if not x or not y:
        return True
    if x in y or y in x:
        return True
    grams = {x[i:i + 2] for i in range(len(x) - 1)}
    return any(y[i:i + 2] in grams for i in range(len(y) - 1))


def verify_numbering(items, source, description, first_number=1):
    """编号闸。返回**画图真正要用的那份物项表**（listing 模式下名/价/已出取自清单）。

    - `listing`：读不回描述、或描述里没有分件清单 → `numbering_unverifiable`，拒绝出图。
      调用方不许退而求其次「那就按当轮识别的号画」——那正是错位的来源。
    - `fresh`：只服务还没发布的那一刻，序号必须**从 `first_number` 起连续**、
      且不含已出的件。

    `first_number` 是调用方的**一句声明**：「我这批从 N 号起、共 M 件」。默认 1 =
    单图主路，行为与它出生时一模一样。多图分批（一屏两张不同的堆，托管清单合并成
    一张表）时，第二张图的件在合并表里接着上一张往下编，调用方就得把 4 说出来。

    🔴 声明不等于放松：给了 4 就要求手里的号正好是 4..4+M-1，跳号、重号照拒。
       闸的判别力全靠这句声明撑着 —— 若改成「连续即可、不必从 1 起」，`[4,5,6]`
       就分不出是「第二张图」还是「一个已发布批次的残片」，那才是判别力掉一截。
    🔴 本脚本不记上一批用到几号（记了就是持久化后门），所以这个数只能由调用方给。
    """
    # `type(...) is int` 而不是 isinstance：True 是 int 的子类，别让它当成 1 号混进来
    if type(first_number) is not int or first_number < 1:
        raise Refused("bad_input", f"起始序号得是 ≥1 的整数，给的是 {first_number!r}")
    if source != "fresh" and first_number != 1:
        raise Refused(
            "bad_input",
            f"listing 模式不接受 --first-number（给了 {first_number}）："
            "已发布的号只有一个真相源，就是帖子描述里的分件清单，"
            "从外面另立一个起点等于绕过那份清单。要续号请在 fresh 那一侧声明。",
        )
    if not items:
        raise Refused("bad_input", "一件都没有，没有可画的东西")
    if len(items) > MAX_ITEMS:
        raise Refused("bad_input", f"一次最多画 {MAX_ITEMS} 件，给了 {len(items)} 件")
    numbers = [item["no"] for item in items]
    if len(set(numbers)) != len(numbers):
        raise Refused("bad_input", f"序号有重复：{sorted(numbers)}")

    if source == "fresh":
        expected = list(range(first_number, first_number + len(numbers)))
        if sorted(numbers) != expected:
            if first_number == 1:
                # 没声明起点 = 单图主路，说法照旧：跳号在这里只可能是"其实已经发布过了"。
                complaint = (
                    f"fresh 模式的序号必须是 1..N 连续，给的是 {sorted(numbers)}。"
                    "有跳号说明这批东西已经发布过了 —— 先把帖子描述读回来，"
                    "改用 `--numbering listing` 按清单行的号画。"
                )
            else:
                # 声明了起点 = 多图续号。这时错的多半是声明本身，别反过来改声明去迁就号。
                complaint = (
                    f"你声明了这批从 {first_number} 号起、共 {len(numbers)} 件，"
                    f"那序号就得正好是 {expected[0]}..{expected[-1]} 连续，"
                    f"给的是 {sorted(numbers)}。"
                    "🔴 别调 --first-number 去迁就手里的号 —— 那是把闸拧松，"
                    "合并成一张托管清单之后号就有洞了。"
                    "先把这批的号按上一张图的末号往下顺排；"
                    "东西已经发布过了就改用 `--numbering listing` 按清单行的号画。"
                )
            raise Refused("numbering_not_fresh", complaint)
        if any(item.get("sold") for item in items):
            raise Refused(
                "numbering_not_fresh",
                "fresh 模式里不该有已出的件：已出是清单行上的状态，"
                "只能从读回来的描述里取，我们不自己判定谁卖了。",
            )
        info = {"source": "fresh", "verified": False}
        if first_number != 1:
            # 只有续号那一批才多这两项：不给起点的单图主路，返回体与 0.34.0 逐字相同。
            info["first_number"] = first_number
            info["count"] = len(numbers)
        return [dict(item) for item in items], info

    rows = parse_list_rows(description)
    if not rows:
        raise Refused(
            "numbering_unverifiable",
            "读不回这条帖子的分件清单，所以不出带号的图。"
            "跟主人说实话：清单读不回来我就没法保证图上的号和帖子里的号是同一套，"
            "买家是点着号来问的。先把帖子描述取到手（listing mine / market show）再来。",
        )
    resolved, mismatches = [], []
    for item in items:
        row = rows.get(item["no"])
        if row is None:
            mismatches.append(f"{item['no']} 号在清单里不存在（清单只有 {sorted(rows)}）")
            continue
        if item.get("name") and not _looks_like(item["name"], row["name_full"]):
            mismatches.append(
                f"{item['no']} 号对不上：你说是「{item['name']}」，"
                f"清单里这一行是「{row['name_full']}」"
            )
            continue
        merged = dict(item)
        # 🔴 名、价、已出一律以清单为准。调用方给的只用来核对 ——
        #    让图上的字和帖子里的字来自同一处，错位就无从发生。
        merged["name"] = row["name"]
        merged["price"] = row["price"]
        merged["sold"] = row["sold"]
        resolved.append(merged)
    if mismatches:
        raise Refused(
            "numbering_mismatch",
            "图上的号和清单里的号对不上，不画：\n  - " + "\n  - ".join(mismatches)
            + "\n这多半是当轮重新识别的号和清单的号错位了。按清单行的号重来一遍。",
        )
    return resolved, {"source": "listing", "verified": True, "rows": len(rows)}


# ---------------------------------------------------------------- 标签避让


def _intersects(a, b):
    return not (a[2] <= b[0] or b[2] <= a[0] or a[3] <= b[1] or b[3] <= a[1])


def _core(box, shrink=0.22):
    x0, y0, x1, y1 = box
    dx, dy = (x1 - x0) * shrink, (y1 - y0) * shrink
    return (x0 + dx, y0 + dy, x1 - dx, y1 - dy)


def place_labels(boxes, sizes, canvas, gap=8):
    """给每个框找一个不压别人的标签位。

    0810 POC 首轮踩过：三个标签互相压、还盖住了白叉 —— 是实际发生的，不是理论风险。
    所以这里按 上 → 下 → 左 → 右 → 框内四角 依次试，并且要求标签矩形
    ① 不出画面 ② 不压已放好的标签 ③ 不压**别的物项框内的主体区域**。
    全都放不下就整张图改用「序号 + 图下图例」，不硬塞。

    返回 (placements, ok)；ok=False 表示该走图例模式了。
    """
    width, height = canvas
    cores = [_core(box) for box in boxes]
    order = sorted(range(len(boxes)), key=lambda i: (boxes[i][1], boxes[i][0]))
    placed, result = [], [None] * len(boxes)
    for index in order:
        x0, y0, x1, y1 = boxes[index]
        tw, th = sizes[index]
        candidates = [
            (x0, y0 - th - gap),                 # 上
            (x0, y1 + gap),                      # 下
            (x0 - tw - gap, y0),                 # 左
            (x1 + gap, y0),                      # 右
            (x0 + gap, y0 + gap),                # 框内左上
            (x1 - tw - gap, y0 + gap),           # 框内右上
            (x0 + gap, y1 - th - gap),           # 框内左下
            (x1 - tw - gap, y1 - th - gap),      # 框内右下
        ]
        chosen = None
        for tier in (0, 1):
            for position, (lx, ly) in enumerate(candidates):
                lx = max(0, min(lx, width - tw))
                ly = max(0, min(ly, height - th))
                rect = (lx, ly, lx + tw, ly + th)
                if rect[2] > width or rect[3] > height:
                    continue
                if any(_intersects(rect, other) for other in placed):
                    continue
                foreign = [c for j, c in enumerate(cores) if j != index]
                if any(_intersects(rect, c) for c in foreign):
                    continue
                if tier == 0 and position >= 4 and _intersects(rect, cores[index]):
                    continue      # 第一轮不许压自己框里的主体，第二轮才让步
                # 贴边闸：候选位被"夹回画面内"之后可能已经离自己的框很远，
                # 那样的标签认不出是在说哪一件。第一轮要求它仍贴着自己的框。
                near = (x0 - tw - gap * 2, y0 - th - gap * 2,
                        x1 + tw + gap * 2, y1 + th + gap * 2)
                if tier == 0 and not _intersects(rect, near):
                    continue
                chosen = rect
                break
            if chosen:
                break
        if not chosen:
            return result, False
        placed.append(chosen)
        result[index] = chosen
    return result, True


# ---------------------------------------------------------------- 画


def _circled(no, font, reference_ok):
    if reference_ok and 1 <= no <= 20:
        return chr(0x2460 + no - 1)
    return f"{no}."


def _text_size(draw, text, font):
    box = draw.textbbox((0, 0), text, font=font)
    return box[2] - box[0], box[3] - box[1]


def _save(image, path):
    """存图：先按最长边缩到闸内，再存。**不写任何元数据**（不带 exif、不带注释）。"""
    from PIL import Image

    longest = max(image.size)
    if longest > MAX_EDGE:
        scale = MAX_EDGE / longest
        image = image.resize((max(1, int(image.width * scale)),
                              max(1, int(image.height * scale))), Image.LANCZOS)
    quality = JPEG_QUALITY
    while True:
        image.convert("RGB").save(path, "JPEG", quality=quality, optimize=True)
        if path.stat().st_size <= MAX_BYTES or quality <= 70:
            return path
        quality -= 8


class Canvas:
    """一次出图用得着的东西：原图、字体、调色、标签排布。"""

    def __init__(self, image_path, items):
        Image, ImageDraw, ImageEnhance, ImageFont = require_pil()
        self.Image, self.ImageDraw, self.ImageEnhance = Image, ImageDraw, ImageEnhance
        try:
            with Image.open(image_path) as opened:
                self.base = opened.convert("RGB")      # 只读原图，绝不改写原图文件
        except Exception as error:
            raise Refused("bad_input", f"这张图打不开：{image_path}（{error}）")
        self.items = items
        # 颜色按物项表里的位次固定分配：同一批东西出 A / B / D 三张图，
        # 某一件的颜色必须张张一样，否则颜色就锚不住了。
        self.slot = {item["no"]: index for index, item in enumerate(items)}
        self.width, self.height = self.base.size
        span = max(self.width, self.height)
        self.line = max(3, span // 320)
        self.font_size = max(18, span // 34)
        texts = [item.get("name", "") for item in items] + \
                [str(item.get("price", "")) for item in items] + ["已出"]
        self.font, self.font_info = pick_font(ImageFont, texts, self.font_size)
        self.ascii_only = self.font_info["ascii_only"]
        # 量字宽用的画布，1x1 就够（textbbox 不看底图内容），不必复制整张原图
        probe = self.ImageDraw.Draw(Image.new("RGB", (1, 1)))
        try:
            reference = self.font.getmask(NOTDEF_PROBE, mode="L")
            self.circled_ok = not self.ascii_only and (
                (reference.size, bytes(reference)) !=
                (self.font.getmask("\u2460", mode="L").size,
                 bytes(self.font.getmask("\u2460", mode="L")))
            )
        except Exception:
            self.circled_ok = False
        self._probe_draw = probe

    def color(self, index):
        return PALETTE[index % len(PALETTE)]

    def label_text(self, item, with_price=False):
        badge = _circled(item["no"], self.font, self.circled_ok)
        if self.ascii_only:
            return badge
        parts = [badge, item.get("name", "")]
        if with_price and item.get("price"):
            parts.append(str(item["price"]))
        if with_price and item.get("sold"):
            parts.append("已出")
        return " ".join(p for p in parts if p)

    def measure(self, texts):
        pad_x, pad_y = self.font_size // 2, self.font_size // 4
        sizes = []
        for text in texts:
            tw, th = _text_size(self._probe_draw, text, self.font)
            sizes.append((tw + pad_x * 2, th + pad_y * 2 + self.font_size // 3))
        return sizes, pad_x, pad_y

    def draw_chip(self, draw, rect, text, fill, text_fill="white"):
        pad_x, pad_y = self.font_size // 2, self.font_size // 4
        radius = max(6, self.font_size // 4)
        draw.rounded_rectangle(rect, radius=radius, fill=fill)
        draw.text((rect[0] + pad_x, rect[1] + pad_y), text, font=self.font, fill=text_fill)

    def draw_box(self, draw, box, color):
        for width in range(self.line):
            draw.rounded_rectangle(
                [box[0] - width, box[1] - width, box[2] + width, box[3] + width],
                radius=max(8, self.font_size // 2), outline=color,
            )

    def draw_cross(self, draw, box):
        """已出的件：原位打叉，**号照旧留着** —— 消失的是可售性，不是锚点。"""
        x0, y0, x1, y1 = box
        width = max(6, self.line * 2)
        for offset, color in ((width, (0, 0, 0)), (0, (255, 255, 255))):
            draw.line([x0 + offset, y0 + offset, x1 + offset, y1 + offset],
                      fill=color, width=width)
            draw.line([x0 + offset, y1 + offset, x1 + offset, y0 + offset],
                      fill=color, width=width)

    def legend_strip(self, image, items=None):
        """标签放不下时的退路：图下面接一条图例，图上只留序号徽章。

        🔴 只列**这张图上真的画了的那几件**。B 聚焦封面是某一条帖的封面，
        把没高亮的件也写进图例，等于让这条帖的封面公开叫卖别条帖的东西 ——
        而封面是要 `photo upload` 出去的，错了就是一个删不掉的公开 URL。
        """
        if self.ascii_only:
            return image
        items = self.items if items is None else items
        line_height = int(self.font_size * 1.6)
        rows = len(items)
        strip = self.Image.new("RGB", (image.width, line_height * rows + line_height // 2),
                               (17, 17, 17))
        draw = self.ImageDraw.Draw(strip)
        room = image.width - self.font_size * 2
        for index, item in enumerate(items):
            text = self.label_text(item, with_price=True)
            # 截断用 ASCII 的三个点，不用「…」—— 省得撞上某个字体没有那个字形
            while len(text) > 3 and _text_size(draw, text, self.font)[0] > room:
                text = text[:-4] + "..."
            y = line_height // 4 + index * line_height
            # 🔴 色块按**物项表里的位次**取，不按图例行序 —— 图例只列一部分时，
            #    行序与位次会错开，那样图例的颜色就对不上图上的框，颜色锚就断了。
            draw.rectangle([12, y + 6, 12 + self.font_size // 2, y + self.font_size],
                           fill=self.color(self.slot[item["no"]]))
            draw.text((12 + self.font_size, y), text, font=self.font, fill="white")
        out = self.Image.new("RGB", (image.width, image.height + strip.height))
        out.paste(image, (0, 0))
        out.paste(strip, (0, image.height))
        return out

    def annotate(self, targets, dim=False, with_price=False, chip_color=None):
        """A / B / D 共用的那一个原语：画框 + 放标签。四种产物的差别只在参数。"""
        image = self.base.copy()
        if dim:
            dark = self.ImageEnhance.Brightness(image).enhance(DIM_FACTOR)
            mask = self.Image.new("L", image.size, 0)
            mask_draw = self.ImageDraw.Draw(mask)
            for item in targets:
                mask_draw.rounded_rectangle(item["box"], radius=max(8, self.font_size // 2),
                                            fill=255)
            image = self.Image.composite(image, dark, mask)
        draw = self.ImageDraw.Draw(image)
        texts = [self.label_text(item, with_price=with_price) for item in targets]
        sizes, _pad_x, _pad_y = self.measure(texts)
        boxes = [tuple(item["box"]) for item in targets]
        placements, ok = place_labels(boxes, sizes, (self.width, self.height))
        for index, item in enumerate(targets):
            color = chip_color or self.color(self.slot[item["no"]])
            self.draw_box(draw, item["box"], self.color(self.slot[item["no"]]))
            if item.get("sold"):
                self.draw_cross(draw, item["box"])
            if ok:
                self.draw_chip(draw, placements[index], texts[index], color)
            else:
                # 图例模式：图上只留一个带序号的小徽章，名字挪到图下的图例里
                badge = _circled(item["no"], self.font, self.circled_ok)
                bw, bh = self.measure([badge])[0][0]
                x0, y0 = item["box"][0], item["box"][1]
                self.draw_chip(draw, (x0, y0, x0 + bw, y0 + bh), badge,
                               self.color(self.slot[item["no"]]))
        if not ok:
            image = self.legend_strip(image, targets)
        return image, ok


# ---------------------------------------------------------------- 出图


def _slug(text, limit=18):
    cleaned = re.sub(r"[^\w\u4e00-\u9fff]+", "-", (text or "").strip()).strip("-")
    return cleaned[:limit] or "item"


def run_dir():
    """每轮一个独立的临时目录。

    🔴 不进 `~/.a2hmarket/`（那是状态目录，放进去等于新建一处持久化），
    🔴 不进工作目录（会被 git 收走）。系统临时目录由系统回收，我们不做定时清理任务。
    """
    return Path(tempfile.mkdtemp(prefix=TMP_PREFIX))


def render(image_path, items, form, only, source, description, listing_id=None,
           first_number=1):
    resolved, numbering = verify_numbering(items, source, description, first_number)
    if listing_id:
        numbering["listing_id"] = listing_id
    canvas = Canvas(image_path, resolved)
    picked = [item for item in resolved if not only or item["no"] in only]
    if not picked:
        raise Refused("bad_input", f"--only {sorted(only)} 在物项表里一件都没命中")

    out = run_dir()
    outputs, warnings = [], []
    # 🔴 文件名里只有形态和名字，**没有序号** —— 序号进文件名就等于在磁盘上
    #    立了第二份编号真相源，它跟帖子描述里的清单迟早打架。
    if form == "A":
        image, ok = canvas.annotate(resolved)
        path = _save(image, out / "A-confirm.jpg")
        outputs.append({"kind": "confirm", "path": str(path),
                        "items": [item["no"] for item in resolved]})
        if not ok:
            warnings.append("标签放不下，已改成「序号 + 图下图例」")
    elif form == "B":
        image, ok = canvas.annotate(picked, dim=True)
        # 文件名里最多带两个名字，多了缀个「等」—— 名字是给主人认图用的，
        # 拼成一长串反而认不出，而且**一个序号都不许出现在这里**（见下面那条注释）。
        name = _slug("-".join(item["name"] for item in picked[:2]))
        if len(picked) > 2:
            name += "-等"
        path = _save(image, out / f"B-focus-{name}.jpg")
        outputs.append({"kind": "cover", "path": str(path),
                        "items": [item["no"] for item in picked]})
        if not ok:
            warnings.append("标签放不下，已改成「序号 + 图下图例」")
    elif form == "C":
        used = set()
        for item in picked:
            x0, y0, x1, y1 = item["box"]
            px, py = (x1 - x0) * 0.04, (y1 - y0) * 0.04
            crop = canvas.base.crop((
                int(max(0, x0 - px)), int(max(0, y0 - py)),
                int(min(canvas.width, x1 + px)), int(min(canvas.height, y1 + py)),
            ))
            stem = _slug(item["name"])
            while stem in used:
                stem += "x"
            used.add(stem)
            path = _save(crop, out / f"C-crop-{stem}.jpg")
            outputs.append({"kind": "crop", "path": str(path), "items": [item["no"]]})
    elif form == "D":
        image, ok = canvas.annotate(resolved, with_price=True, chip_color=(17, 17, 17))
        path = _save(image, out / "D-pricetags.jpg")
        outputs.append({"kind": "pricetags", "path": str(path),
                        "items": [item["no"] for item in resolved]})
        warnings.append(
            "这张是此刻的快照：价格或状态变了要跟我说一声重出一张 —— 这句话要说给主人听"
        )
        if not ok:
            warnings.append("标签放不下，已改成「序号 + 图下图例」")
    else:
        raise Refused("bad_input", f"没有这种形态：{form}")

    return {
        "ok": True,
        "form": form,
        "dir": str(out),
        "outputs": outputs,
        "numbering": numbering,
        "font": canvas.font_info,
        # 名称只在这里回给调用方一份（ascii_only 时图上画不了字，得由 agent 在对话里说）
        "legend": [{"no": item["no"], "name": item["name"],
                    "price": item.get("price", ""), "sold": bool(item.get("sold"))}
                   for item in resolved],
        "warnings": warnings,
        "lifecycle": "上传成功或主人说不要了 → 立刻 `annotate.py cleanup --dir <dir>`",
    }


def cleanup(directory):
    path = Path(directory).resolve()
    if not path.name.startswith(TMP_PREFIX):
        raise Refused("bad_input", f"只删本脚本自己建的临时目录，这个不是：{path}")
    if path.exists():
        shutil.rmtree(path)
    return {"ok": True, "removed": str(path)}


# ---------------------------------------------------------------- CLI


def _read_items(source):
    raw = sys.stdin.read() if source in (None, "-") else Path(source).read_text("utf-8")
    try:
        document = json.loads(raw)
    except json.JSONDecodeError as error:
        raise Refused("bad_input", f"物项表不是合法 JSON：{error}")
    items = document.get("items") if isinstance(document, dict) else document
    if not isinstance(items, list):
        raise Refused("bad_input", "物项表要么是 {\"items\": [...]}，要么直接是一个数组")
    cleaned = []
    for index, item in enumerate(items):
        if not isinstance(item, dict) or "no" not in item or "box" not in item:
            raise Refused("bad_input", f"第 {index + 1} 项缺 no 或 box")
        box = [int(v) for v in item["box"]]
        if len(box) != 4 or box[2] <= box[0] or box[3] <= box[1]:
            raise Refused("bad_input", f"{item['no']} 号的框不成立：{item['box']}")
        cleaned.append({
            "no": int(item["no"]), "box": box,
            "name": str(item.get("name", "")), "price": str(item.get("price", "")),
            "sold": bool(item.get("sold", False)),
        })
    return cleaned


def build_parser():
    parser = argparse.ArgumentParser(
        description="一图多物：把模型报的框画成 A 确认图 / B 聚焦封面 / C 裁剪图 / D 价签总览")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("probe", help="看看这台机器画不画得了图（Pillow + 字体）")

    render_parser = sub.add_parser("render", help="出图")
    render_parser.add_argument("--image", required=True, help="原图路径（只读，不改写）")
    render_parser.add_argument("--form", required=True, choices=["A", "B", "C", "D"])
    render_parser.add_argument("--items-file", default="-",
                               help="物项表 JSON 路径；默认 `-` 从 stdin 读")
    render_parser.add_argument("--numbering", required=True, choices=["fresh", "listing"],
                               help="fresh = 还没发布，序号必须从 --first-number 起连续"
                                    "（默认就是 1..N）；"
                                    "listing = 已发布，必须同时给 --listing-description-file")
    render_parser.add_argument("--first-number", type=int, default=1, metavar="N",
                               help="这批的序号从几号起，默认 1。只对 --numbering fresh 有效。"
                                    "多图分批、托管清单合并成一张表时，第二张图接着上一张"
                                    "往下编：上一张出到 3，这张就 --first-number 4。"
                                    "🔴 它是一句声明不是偏移量：给了 4 就要求这批的号"
                                    "正好是 4、5、6…，跳号照拒；脚本不记上一批编到几号")
    render_parser.add_argument("--listing-description-file",
                               help="读回来的帖子描述（分件清单在里面）")
    render_parser.add_argument("--listing-id", help="只回显进结果 JSON，不进文件名、不进图")
    render_parser.add_argument("--only", help="只画这几号（逗号分隔），B/C 形态用")

    clean_parser = sub.add_parser("cleanup", help="删掉某一轮的临时目录")
    clean_parser.add_argument("--dir", required=True)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    try:
        if args.cmd == "probe":
            _mods, info = probe_pil()
            payload = {"ok": bool(info["available"]), "pil": info}
            if info["available"]:
                from PIL import ImageFont
                _font, font_info = pick_font(ImageFont, ["价 £ 已出"], 32)
                payload["font"] = font_info
            print(json.dumps(payload, ensure_ascii=False))
            return 0 if info["available"] else CODES["pil_missing"]

        if args.cmd == "cleanup":
            print(json.dumps(cleanup(args.dir), ensure_ascii=False))
            return 0

        description = None
        if args.numbering == "listing":
            if not args.listing_description_file:
                raise Refused(
                    "numbering_unverifiable",
                    "listing 模式必须给 --listing-description-file：图上的号要按"
                    "帖子描述里的分件清单来。读不回描述就别出图。",
                )
            try:
                description = Path(args.listing_description_file).read_text("utf-8")
            except OSError as error:
                raise Refused("numbering_unverifiable",
                              f"帖子描述读不回来（{error}），不出带号的图")
        only = {int(v) for v in args.only.split(",") if v.strip()} if args.only else set()
        result = render(args.image, _read_items(args.items_file), args.form, only,
                        args.numbering, description, args.listing_id, args.first_number)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except Refused as refused:
        print(json.dumps({"ok": False, "error": refused.code,
                          "message": refused.message, **refused.extra},
                         ensure_ascii=False))
        return CODES.get(refused.code, 1)


if __name__ == "__main__":
    raise SystemExit(main())
