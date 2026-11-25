# -*- coding: utf-8 -*-
"""
price_map.py — يرسم خريطة ملعب الأول بارك ويلوّن كل فئة ويكتب سعرها بالوسط.
يستخدمه x_alert.py مع رسالة الفئات. الخريطة محفوظة في awwal_template.b64
(كل فئة لها لون مفتاح ينبدّل بلونها الحقيقي)، والخط Cairo ينزل أول مرة من npm.
"""
import base64
import datetime
import io
import os
import re
import tarfile
import urllib.request

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATE = os.path.join(HERE, "awwal_template.b64")
FONT_DIR = os.path.join(HERE, "fonts")
FONT_URL = "https://registry.npmjs.org/@fontsource/cairo/-/cairo-5.3.0.tgz"

VENUE_WORDS = ("الأول بارك", "الاول بارك", "Al-Awwal Park", "AlAwwal Park")

OLIVE = (196, 198, 70)
GOLDC = (232, 172, 28)
PURP = (150, 95, 205)
BLUE = (70, 130, 225)
ROSE = (215, 70, 120)
GREY = (62, 60, 70)

# الفئة: (لون المفتاح في القالب، لونها، كتابة عمودية؟)
REGIONS = {
    "F": ((255, 0, 10), OLIVE, False),
    "G": ((255, 0, 20), GOLDC, False),
    "H": ((255, 0, 30), OLIVE, False),
    "E": ((255, 0, 40), PURP, False),
    "I": ((255, 0, 50), PURP, False),
    "D": ((255, 0, 60), BLUE, True),
    "C": ((255, 0, 70), ROSE, True),
    "K": ((255, 0, 80), ROSE, True),
    "J": ((255, 0, 90), BLUE, True),
    "B": ((255, 0, 100), PURP, False),
    "A": ((255, 0, 110), OLIVE, False),
}
CENTER_BY_BBOX = set("FGHDCKJA")
ALIASES = {"فانز لاونج": "A"}

RTL = dict(direction="rtl", language="ar")


def is_awwal(text):
    return any(w in (text or "") for w in VENUE_WORDS)


def ensure_fonts():
    """يجهز Cairo-700/900.ttf (عربي + لاتيني) مرة وحدة."""
    need = [os.path.join(FONT_DIR, f"Cairo-{w}.ttf") for w in (700, 900)]
    if all(os.path.exists(p) for p in need):
        return True
    from fontTools.ttLib import TTFont
    from fontTools.merge import Merger
    os.makedirs(FONT_DIR, exist_ok=True)
    data = urllib.request.urlopen(FONT_URL, timeout=30).read()
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for w in (700, 900):
            parts = []
            for sub in ("arabic", "latin"):
                raw = tf.extractfile(f"package/files/cairo-{sub}-{w}-normal.woff").read()
                f = TTFont(io.BytesIO(raw))
                f.flavor = None
                p = os.path.join(FONT_DIR, f"_c-{sub}-{w}.ttf")
                f.save(p)
                parts.append(p)
            Merger().merge(parts).save(os.path.join(FONT_DIR, f"Cairo-{w}.ttf"))
    return True


def _font(w, s):
    return ImageFont.truetype(os.path.join(FONT_DIR, f"Cairo-{w}.ttf"), s,
                              layout_engine=ImageFont.Layout.RAQM)


def _bright(c):
    return 0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]


def region_of(group):
    g = ALIASES.get(group, group)
    return g if g in REGIONS else None


def render(prices, title="", derby=None):
    """prices: {اسم المجموعة: أرخص سعر} من cat_group. يرجع PNG bytes."""
    ensure_fonts()
    tmpl = Image.open(io.BytesIO(base64.b64decode(open(TEMPLATE).read()))).convert("RGB")
    a = np.array(tmpl)
    priced = {}
    for g, p in prices.items():
        r = region_of(g)
        if r and (r not in priced or p < priced[r]):
            priced[r] = p
    labels = []
    for name, (key, col, vert) in REGIONS.items():
        m = np.all(a == key, axis=2)
        if not m.any():
            continue
        if name in priced:
            a[m] = col
            ys, xs = np.where(m)
            if name in CENTER_BY_BBOX:
                cx, cy = (xs.min() + xs.max()) / 2, (ys.min() + ys.max()) / 2
            else:
                cx, cy = xs.mean(), ys.mean()
            labels.append((cx, cy, priced[name], col, vert))
        else:
            a[m] = GREY

    S = 1.65
    m = Image.fromarray(a)
    m = m.resize((int(m.width * S), int(m.height * S)), Image.LANCZOS)

    W, TOP = 2200, 480
    MAPX = (W - m.width) // 2
    H = TOP + m.height + 160
    bg = np.zeros((H, W, 3), float)
    t = np.linspace(0, 1, H)[:, None]
    bg[:] = (np.array([38, 14, 58]) * (1 - t) + np.array([6, 4, 10]) * t)[:, None, :]
    yy, xx = np.mgrid[0:H, 0:W]
    bg += np.exp(-(((xx - W / 2) / 700) ** 2 + ((yy - 120) / 260) ** 2))[..., None] * np.array([120, 90, 20])
    img = Image.fromarray(np.clip(bg, 0, 255).astype(np.uint8))
    d = ImageDraw.Draw(img)

    def ctext(y, s, font, fill):
        w = d.textlength(s, font=font, **RTL)
        d.text(((W - w) / 2, y), s, font=font, fill=fill, **RTL)

    league, teams = "", title.strip()
    if "–" in title or " - " in title:
        parts = re.split(r"\s+[–-]\s+", title, maxsplit=1)
        if len(parts) == 2:
            league, teams = parts[0].strip(), parts[1].strip()
    teams = re.sub(r"\s+ضد\s+", " × ", teams)
    if league:
        ctext(20, league, _font(700, 44), (210, 200, 225))
    if derby:
        ctext(70, derby, _font(900, 120), (255, 214, 0))
        ctext(262, teams, _font(900, 70), (255, 255, 255))
    else:
        ctext(110, teams, _font(900, 96), (255, 214, 0))
    ctext(372, "أسعار الفئات  •  ملعب الأول بارك", _font(700, 46), (225, 215, 240))

    mask = Image.new("L", m.size, 0)
    ImageDraw.Draw(mask).rounded_rectangle((0, 0, m.width - 1, m.height - 1), 40, fill=255)
    img.paste(m, (MAPX, TOP), mask)
    d.rounded_rectangle((MAPX, TOP, MAPX + m.width - 1, TOP + m.height - 1), 40, outline=(120, 90, 30), width=4)

    for x, y, price, col, vert in labels:
        size = 40 if vert else 46
        f = _font(900, size)
        text = f"{price:,} ريال"
        tw = d.textlength(text, font=f, **RTL)
        pw, ph, pad = int(tw + 46), int(size * 1.55), 24
        layer = Image.new("RGBA", (pw + 2 * pad, ph + 2 * pad), (0, 0, 0, 0))
        sh = Image.new("L", layer.size, 0)
        ImageDraw.Draw(sh).rounded_rectangle((pad, pad + 4, pad + pw, pad + ph + 4), ph // 2, fill=190)
        layer.paste((0, 0, 0, 255), (0, 0), sh.filter(ImageFilter.GaussianBlur(9)))
        ld = ImageDraw.Draw(layer)
        ld.rounded_rectangle((pad, pad, pad + pw, pad + ph), ph // 2, fill=col + (255,),
                             outline=(255, 255, 255, 255), width=3)
        tc = (20, 16, 24) if _bright(col) > 150 else (255, 255, 255)
        ld.text((pad + (pw - tw) / 2, pad + ph / 2 - size * 0.98), text, font=f, fill=tc, **RTL)
        if vert:
            layer = layer.rotate(90, expand=True)
        cx, cy = MAPX + x * S, TOP + y * S
        img.paste(layer, (int(cx - layer.width / 2), int(cy - layer.height / 2)), layer)

    today = (datetime.datetime.utcnow() + datetime.timedelta(hours=3)).strftime("%Y/%m/%d")
    ctext(H - 105, f"الأسعار حسب ويبوك • تحديث {today}", _font(700, 32), (160, 150, 180))
    buf = io.BytesIO()
    img.save(buf, "PNG", optimize=True)
    return buf.getvalue()
