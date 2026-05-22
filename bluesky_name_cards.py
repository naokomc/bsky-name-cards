#!/usr/bin/env python3
"""
Bluesky Name Card Generator
----------------------------
Card size : 89 × 53 mm landscape (default) — configurable via --card
Layout    : @handle top-centre / avatar left / name field right /
            butterfly + event name footer
Sheet     : A4 portrait, 2 cols × 5 rows = 10 cards per page (default)

Usage:
    python3 bluesky_name_cards.py --file handles.txt --output cards.pdf
"""

import sys, os, io, re, argparse, tempfile, textwrap
from pathlib import Path
from typing import Optional, Tuple

import requests
try:
    import cairosvg as _cairosvg
    _HAS_CAIROSVG = True
except ImportError:
    _HAS_CAIROSVG = False
    print("  ⚠ cairosvg not found — butterfly logo will be omitted. Run: pip install cairosvg")
from PIL import Image, ImageDraw, ImageFont
from reportlab.lib.pagesizes import A4, letter as LETTER
from reportlab.lib.units import mm
from reportlab.pdfgen import canvas as rl_canvas
from reportlab.lib import colors
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfbase.pdfmetrics import stringWidth


# ── Fonts ─────────────────────────────────────────────────────────────────────
pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
FONT_JP = "HeiseiKakuGo-W5"   # Japanese ("お名前")

def _find_inter() -> "Tuple[str, str]":
    """Return ('Inter', 'Inter-Bold') if found and registered, else Helvetica variants."""
    regular_candidates = [
        "/Library/Fonts/Inter-Regular.ttf",
        "/Library/Fonts/Inter/Inter-Regular.ttf",
        os.path.expanduser("~/Library/Fonts/Inter-Regular.ttf"),
        os.path.expanduser("~/Library/Fonts/Inter/Inter-Regular.ttf"),
        "/opt/homebrew/share/fonts/inter/Inter-Regular.otf",
        "/usr/share/fonts/truetype/inter/Inter-Regular.ttf",
    ]
    bold_candidates = [
        "/Library/Fonts/Inter-Bold.ttf",
        "/Library/Fonts/Inter/Inter-Bold.ttf",
        os.path.expanduser("~/Library/Fonts/Inter-Bold.ttf"),
        os.path.expanduser("~/Library/Fonts/Inter/Inter-Bold.ttf"),
        "/opt/homebrew/share/fonts/inter/Inter-Bold.otf",
        "/usr/share/fonts/truetype/inter/Inter-Bold.ttf",
    ]
    reg_name = "Helvetica"
    for path in regular_candidates:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont("Inter", path))
                reg_name = "Inter"
                print(f"  ✓ Inter Regular: {path}")
                break
            except Exception:
                continue

    bold_name = "Helvetica-Bold"
    for path in bold_candidates:
        if os.path.exists(path):
            try:
                pdfmetrics.registerFont(TTFont("Inter-Bold", path))
                bold_name = "Inter-Bold"
                print(f"  ✓ Inter Bold: {path}")
                break
            except Exception:
                continue

    if reg_name == "Helvetica":
        print("  ⚠ Inter not found — using Helvetica. Install Inter from https://rsms.me/inter/")
    return reg_name, bold_name

FONT_EN, FONT_EN_BOLD = _find_inter()   # Latin (@handle, footer text)


# ── Card / paper presets ─────────────────────────────────────────────────────
CARD_PRESETS = {
    "meishi": (89.0,  53.0),    # Japanese business card
    "4x3":    (101.6, 76.2),    # 4 × 3 inches
}
PAPER_SIZES = {
    "a4":     A4,
    "letter": LETTER,
}

def parse_card_spec(spec: str) -> "Tuple[float, float]":
    """Return (width_mm, height_mm) from a preset name or 'WxH' string."""
    key = spec.strip().lower()
    if key in CARD_PRESETS:
        return CARD_PRESETS[key]
    m = re.match(r'^(\d+(?:\.\d+)?)[x×](\d+(?:\.\d+)?)$', key)
    if m:
        w, h = float(m.group(1)), float(m.group(2))
        if w < 20 or h < 20:
            raise ValueError(f"Card dimensions must be ≥ 20 mm (got {w}×{h})")
        return w, h
    raise ValueError(
        f"Unknown card spec '{spec}'. Use 'meishi', '4x3', or WxH in mm (e.g. '100x60')"
    )


# ── Asset paths ───────────────────────────────────────────────────────────────
SCRIPT_DIR     = os.path.dirname(os.path.abspath(__file__))
BUTTERFLY_SVG  = os.path.join(SCRIPT_DIR, "assets", "bluesky_media_kit_logo_transparent_1.svg")
BUTTERFLY_PNG  = os.path.join(SCRIPT_DIR, "assets", "bluesky_butterfly.png")


# ── Page layout ───────────────────────────────────────────────────────────────
PAGE_W, PAGE_H       = A4
CARD_W_MM, CARD_H_MM = 89.0, 53.0
CARD_W, CARD_H       = CARD_W_MM * mm, CARD_H_MM * mm
COLS, ROWS           = 2, 5
PER_PAGE             = COLS * ROWS
MARGIN_X             = (PAGE_W - COLS * CARD_W) / 2
MARGIN_Y             = (PAGE_H - ROWS * CARD_H) / 2


# ── Card internal layout (mm, y from card bottom) ─────────────────────────────
PAD       = 3.5
FOOTER_H  = 12.0
HANDLE_H  = 11.0
# Middle zone: [FOOTER_H … CARD_H_MM − HANDLE_H]
MIDDLE_H  = CARD_H_MM - FOOTER_H - HANDLE_H   # = 30.0 mm

AVATAR_D  = 28.0                                 # circle diameter
AVATAR_CX = PAD + AVATAR_D / 2
AVATAR_CY = FOOTER_H + MIDDLE_H / 2             # vertically centred in middle zone

# Underline: same y as avatar bottom edge
NAME_LINE_Y  = AVATAR_CY - AVATAR_D / 2         # = 13.0 mm
NAME_LABEL_Y = CARD_H_MM - HANDLE_H - PAD - 3.0 # "お名前" baseline

TEXT_LEFT  = PAD + AVATAR_D + 4.5
TEXT_RIGHT = CARD_W_MM - PAD


# ── Colours ───────────────────────────────────────────────────────────────────
BSKY_BLUE = colors.HexColor("#0085ff")
GREY      = colors.HexColor("#999999")
LINE_GREY = colors.HexColor("#cccccc")


# ── Footer ────────────────────────────────────────────────────────────────────
FOOTER_TEXT = "Bluesky Meetup"
FOOTER_FS   = 8.0    # font size (pt)
LOGO_H_MM   = 4.2    # butterfly rendered height

# ── Scalable font size defaults (recalculated by configure_layout) ────────────
HANDLE_FS_DEFAULT = 11.5   # @handle in top zone
PRESS_FS_DEFAULT  = 50.0   # PRESS label

# ── Name field label (overridable via CLI) ────────────────────────────────────
NAME_LABEL = "Name"

# ── PRESS / BLANK cards — defaults (overridable via CLI) ─────────────────────
PRESS_COUNT_DEFAULT = 4
BLANK_COUNT_DEFAULT = 8


# ── Layout configurator ───────────────────────────────────────────────────────

def configure_layout(card_w_mm: float, card_h_mm: float,
                     page_size: "Tuple[float, float]") -> None:
    """
    Recalculate all layout globals for the given card and page dimensions.
    All internal proportions scale relative to the baseline 89 × 53 mm card.
    Call this once, before any draw_* functions are invoked.
    """
    global PAGE_W, PAGE_H
    global CARD_W_MM, CARD_H_MM, CARD_W, CARD_H
    global COLS, ROWS, PER_PAGE, MARGIN_X, MARGIN_Y
    global PAD, FOOTER_H, HANDLE_H, MIDDLE_H
    global AVATAR_D, AVATAR_CX, AVATAR_CY
    global NAME_LINE_Y, NAME_LABEL_Y, TEXT_LEFT, TEXT_RIGHT
    global FOOTER_FS, LOGO_H_MM
    global HANDLE_FS_DEFAULT, PRESS_FS_DEFAULT

    sh = card_h_mm / 53.0   # height scale vs. meishi baseline
    sw = card_w_mm / 89.0   # width  scale vs. meishi baseline

    PAGE_W, PAGE_H = page_size

    CARD_W_MM, CARD_H_MM = card_w_mm, card_h_mm
    CARD_W  = card_w_mm * mm
    CARD_H  = card_h_mm * mm

    COLS     = max(1, int(PAGE_W / CARD_W))
    ROWS     = max(1, int(PAGE_H / CARD_H))
    PER_PAGE = COLS * ROWS
    MARGIN_X = (PAGE_W - COLS * CARD_W) / 2
    MARGIN_Y = (PAGE_H - ROWS * CARD_H) / 2

    PAD      = round(3.5  * sw, 2)
    FOOTER_H = round(12.0 * sh, 2)
    HANDLE_H = round(11.0 * sh, 2)
    MIDDLE_H = card_h_mm - FOOTER_H - HANDLE_H

    AVATAR_D     = round(28.0 * sh, 2)
    AVATAR_CX    = PAD + AVATAR_D / 2
    AVATAR_CY    = FOOTER_H + MIDDLE_H / 2
    NAME_LINE_Y  = AVATAR_CY - AVATAR_D / 2
    NAME_LABEL_Y = card_h_mm - HANDLE_H - PAD - round(3.0 * sh, 2)
    TEXT_LEFT    = PAD + AVATAR_D + round(4.5 * sw, 2)
    TEXT_RIGHT   = card_w_mm - PAD

    FOOTER_FS  = round(8.0  * sh, 1)
    LOGO_H_MM  = round(4.2  * sh, 2)

    HANDLE_FS_DEFAULT = round(11.5 * min(sh, sw), 1)
    PRESS_FS_DEFAULT  = round(50.0 * sh, 1)


# ── Raster settings ───────────────────────────────────────────────────────────
BSKY_API = "https://public.api.bsky.app/xrpc/app.bsky.actor.getProfile"
DPI      = 150


# ── Helpers ───────────────────────────────────────────────────────────────────

def fetch_profile(handle: str) -> dict:
    handle = handle.lstrip("@").strip()
    try:
        r = requests.get(BSKY_API, params={"actor": handle}, timeout=15)
        r.raise_for_status()
        data = r.json()
        return {"handle": handle, "avatar_url": data.get("avatar")}
    except Exception as exc:
        print(f"  ⚠ @{handle}: {exc}", file=sys.stderr)
        return {"handle": handle, "avatar_url": None}


def download_image(url: str) -> "Optional[Image.Image]":
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        return Image.open(io.BytesIO(r.content)).convert("RGBA")
    except Exception:
        return None


def make_circular_avatar(img: Image.Image, px: int) -> Image.Image:
    """Resize to px×px with a smooth circular mask (2× supersampling)."""
    px2  = px * 2
    img  = img.resize((px2, px2), Image.LANCZOS)
    mask = Image.new("L", (px2, px2), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, px2 - 1, px2 - 1), fill=255)
    out  = Image.new("RGBA", (px2, px2), (255, 255, 255, 0))
    out.paste(img, mask=mask)
    return out.resize((px, px), Image.LANCZOS)


def make_placeholder(px: int, handle: str) -> Image.Image:
    import colorsys
    px2     = px * 2
    hue     = sum(ord(c) for c in handle) % 360
    r, g, b = colorsys.hsv_to_rgb(hue / 360, 0.35, 0.92)
    img     = Image.new("RGBA", (px2, px2))
    draw    = ImageDraw.Draw(img)
    draw.ellipse((0, 0, px2 - 1, px2 - 1), fill=(int(r*255), int(g*255), int(b*255), 255))
    initials = handle[0].upper()
    try:
        font = ImageFont.truetype(
            "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", int(px2 * 0.42))
    except OSError:
        font = ImageFont.load_default()
    bbox = draw.textbbox((0, 0), initials, font=font)
    tw, th = bbox[2]-bbox[0], bbox[3]-bbox[1]
    draw.text(((px2-tw)/2, (px2-th)/2 - bbox[1]), initials,
              font=font, fill=(255, 255, 255, 220))
    mask = Image.new("L", (px2, px2), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, px2 - 1, px2 - 1), fill=255)
    out  = Image.new("RGBA", (px2, px2), (255, 255, 255, 0))
    out.paste(img, mask=mask)
    return out.resize((px, px), Image.LANCZOS)


def pil_to_temp_png(img: Image.Image) -> str:
    tmp = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
    img.save(tmp.name, "PNG")
    tmp.close()
    return tmp.name


def prepare_butterfly() -> "Tuple[Optional[str], float, float]":
    """
    Render the butterfly logo at LOGO_H_MM height.

    Tries cairosvg (SVG → PNG at exact target height) first.
    Falls back to the pre-rendered PNG in assets/ if cairosvg is unavailable
    or the SVG cannot be found — so the logo works without extra system libs.
    Returns (tmp_path, width_mm, height_mm) or (None, 0, 0).
    """
    h_target_px = int(LOGO_H_MM / 25.4 * DPI)

    # --- attempt SVG render via cairosvg ---
    if _HAS_CAIROSVG and os.path.exists(BUTTERFLY_SVG):
        try:
            png_data = _cairosvg.svg2png(url=BUTTERFLY_SVG, output_height=h_target_px)
            img = Image.open(io.BytesIO(png_data)).convert("RGBA")
            w_px, h_px = img.size
            return pil_to_temp_png(img), w_px / DPI * 25.4, h_px / DPI * 25.4
        except Exception as e:
            print(f"  ⚠ cairosvg render failed ({e}), falling back to PNG", file=sys.stderr)

    # --- fallback: bundled pre-rendered PNG ---
    if os.path.exists(BUTTERFLY_PNG):
        img = Image.open(BUTTERFLY_PNG).convert("RGBA")
        # Scale to target height, preserving aspect ratio
        orig_w, orig_h = img.size
        scale = h_target_px / orig_h
        new_w = max(1, int(orig_w * scale))
        img = img.resize((new_w, h_target_px), Image.LANCZOS)
        w_px, h_px = img.size
        return pil_to_temp_png(img), w_px / DPI * 25.4, h_px / DPI * 25.4

    print("  ⚠ butterfly logo not found (checked SVG and PNG fallback)", file=sys.stderr)
    return None, 0.0, 0.0


# ── Shared footer renderer ────────────────────────────────────────────────────

def _draw_footer(c, ox: float, oy: float,
                 butterfly_tmp: "Optional[str]",
                 logo_w_mm: float, logo_h_mm: float) -> None:
    """
    Draw the footer strip (butterfly logo + event name) centred on the card.

    Width is calculated including char spacing so the content never overflows
    the card boundary. Font size is scaled down automatically if needed.
    """
    CHAR_SP   = 0.5
    PAD_PT    = PAD * mm
    max_text_w = CARD_W - 2 * PAD_PT   # absolute maximum text zone

    logo_w_pt = logo_w_mm * mm
    logo_h_pt = logo_h_mm * mm
    gap_pt    = 2.0 * mm
    reserved  = (logo_w_pt + gap_pt) if butterfly_tmp else 0.0

    # Auto-scale font so text fits within the card
    fs = FOOTER_FS
    while fs > 5.0:
        tw = stringWidth(FOOTER_TEXT, FONT_EN_BOLD, fs) \
             + (len(FOOTER_TEXT) - 1) * CHAR_SP
        if reserved + tw <= max_text_w:
            break
        fs -= 0.25

    tw      = stringWidth(FOOTER_TEXT, FONT_EN_BOLD, fs) \
              + (len(FOOTER_TEXT) - 1) * CHAR_SP
    unit_w  = reserved + tw
    start_x = ox + (CARD_W - unit_w) / 2

    footer_ctr = oy + (FOOTER_H / 2) * mm

    if butterfly_tmp:
        logo_y = footer_ctr - logo_h_pt / 2
        c.drawImage(butterfly_tmp, start_x, logo_y,
                    logo_w_pt, logo_h_pt, mask="auto")
        text_x = start_x + logo_w_pt + gap_pt
    else:
        text_x = start_x

    text_y = footer_ctr - fs * 0.36
    t = c.beginText(text_x, text_y)
    t.setFont(FONT_EN_BOLD, fs)
    t.setFillColor(BSKY_BLUE)
    t.setCharSpace(CHAR_SP)
    t.textOut(FOOTER_TEXT)
    c.drawText(t)


# ── Card renderer ─────────────────────────────────────────────────────────────

def draw_card(c,
              ox: float, oy: float,
              handle: str,
              avatar_tmp: str,
              butterfly_tmp: "Optional[str]",
              logo_w_mm: float,
              logo_h_mm: float) -> None:
    """Draw one card. (ox, oy) = bottom-left corner in points."""

    # ── cut guide ──────────────────────────────────────────────────────────
    c.saveState()
    c.setStrokeColor(LINE_GREY)
    c.setLineWidth(0.3)
    c.setDash([2, 3], 0)
    c.rect(ox, oy, CARD_W, CARD_H, stroke=1, fill=0)
    c.restoreState()

    # ── @handle — top centre, Inter, auto-scaled, letter-spaced ───────────
    label     = f"@{handle}"
    fs        = HANDLE_FS_DEFAULT
    fs_min    = max(5.0, HANDLE_FS_DEFAULT * 0.55)
    max_w     = (CARD_W_MM - 2 * PAD) * mm
    char_sp   = 1.0
    while fs > fs_min and (stringWidth(label, FONT_EN_BOLD, fs) + (len(label)-1)*char_sp) > max_w:
        fs -= 0.5

    lw   = stringWidth(label, FONT_EN_BOLD, fs) + (len(label) - 1) * char_sp
    tx   = ox + (CARD_W - lw) / 2
    ty   = oy + (CARD_H_MM - HANDLE_H / 2 - fs * 0.35) * mm
    t    = c.beginText(tx, ty)
    t.setFont(FONT_EN_BOLD, fs)
    t.setFillColor(BSKY_BLUE)
    t.setCharSpace(char_sp)
    t.textOut(label)
    c.drawText(t)

    # ── avatar — perfect circle via 2× supersampled PNG ───────────────────
    avatar_pt = AVATAR_D * mm     # same value for w and h → perfect circle
    ax = ox + (AVATAR_CX - AVATAR_D / 2) * mm
    ay = oy + (AVATAR_CY - AVATAR_D / 2) * mm
    c.drawImage(avatar_tmp, ax, ay, avatar_pt, avatar_pt, mask="auto")

    # ── name-field label (locale-aware font) ──────────────────────────────
    _lbl_font = FONT_JP if any('\u3040' <= ch <= '\u9fff' for ch in NAME_LABEL) else FONT_EN
    c.setFont(_lbl_font, 7)
    c.setFillColor(GREY)
    c.drawString(ox + TEXT_LEFT * mm, oy + NAME_LABEL_Y * mm, NAME_LABEL)

    # ── name underline — y aligns with avatar bottom ───────────────────────
    c.setStrokeColor(LINE_GREY)
    c.setLineWidth(0.5)
    c.line(ox + TEXT_LEFT * mm, oy + NAME_LINE_Y * mm,
           ox + TEXT_RIGHT * mm, oy + NAME_LINE_Y * mm)

    _draw_footer(c, ox, oy, butterfly_tmp, logo_w_mm, logo_h_mm)


# ── PRESS card renderer ───────────────────────────────────────────────────────

def draw_press_card(c,
                   ox: float, oy: float,
                   butterfly_tmp: "Optional[str]",
                   logo_w_mm: float,
                   logo_h_mm: float) -> None:
    """Draw a PRESS card — footer only + large centred PRESS text."""

    # ── cut guide ──────────────────────────────────────────────────────────
    c.saveState()
    c.setStrokeColor(LINE_GREY)
    c.setLineWidth(0.3)
    c.setDash([2, 3], 0)
    c.rect(ox, oy, CARD_W, CARD_H, stroke=1, fill=0)
    c.restoreState()

    # ── large PRESS label — centred in the full card ────────────────────────
    press_fs  = PRESS_FS_DEFAULT
    press_txt = "PRESS"
    # centre horizontally
    pw = stringWidth(press_txt, FONT_EN_BOLD, press_fs)
    px = ox + (CARD_W - pw) / 2
    # vertically: equal-spacing centre, then shift down proportionally
    cap_h   = press_fs * 0.72
    body_h  = (CARD_H_MM - FOOTER_H) * mm
    body_cy = oy + FOOTER_H * mm + body_h / 2
    py = body_cy - cap_h / 2 - 2.0 * (CARD_H_MM / 53.0) * mm

    c.setFont(FONT_EN_BOLD, press_fs)
    c.setFillColor(BSKY_BLUE)
    c.drawString(px, py, press_txt)

    # ── footer: butterfly + event name (same as regular card) ──────────────
    footer_ctr = oy + (FOOTER_H / 2) * mm

    logo_w_pt = logo_w_mm * mm
    logo_h_pt = logo_h_mm * mm
    gap_pt    = 2.0 * mm
    tw        = stringWidth(FOOTER_TEXT, FONT_EN_BOLD, FOOTER_FS)
    unit_w    = (logo_w_pt + gap_pt + tw) if butterfly_tmp else tw
    start_x   = ox + (CARD_W - unit_w) / 2

    if butterfly_tmp:
        logo_y = footer_ctr - logo_h_pt / 2
        c.drawImage(butterfly_tmp, start_x, logo_y,
                    logo_w_pt, logo_h_pt, mask="auto")
        text_x = start_x + logo_w_pt + gap_pt
    else:
        text_x = start_x

    text_y = footer_ctr - FOOTER_FS * 0.36
    t = c.beginText(text_x, text_y)
    t.setFont(FONT_EN_BOLD, FOOTER_FS)
    t.setFillColor(BSKY_BLUE)
    t.setCharSpace(0.5)
    t.textOut(FOOTER_TEXT)
    c.drawText(t)


# ── Blank card renderer ───────────────────────────────────────────────────────

def draw_blank_card(c,
                   ox: float, oy: float,
                   butterfly_tmp: "Optional[str]",
                   logo_w_mm: float,
                   logo_h_mm: float) -> None:
    """
    Blank fallback card — light-grey circle avatar, name field, no handle.
    Footer (butterfly + event name) identical to regular card.
    """

    # ── cut guide ──────────────────────────────────────────────────────────
    c.saveState()
    c.setStrokeColor(LINE_GREY)
    c.setLineWidth(0.3)
    c.setDash([2, 3], 0)
    c.rect(ox, oy, CARD_W, CARD_H, stroke=1, fill=0)
    c.restoreState()

    # ── light-grey circle (avatar placeholder, no initials) ────────────────
    LIGHT_GREY = colors.HexColor("#e0e0e0")
    ax  = ox + AVATAR_CX * mm
    ay  = oy + AVATAR_CY * mm
    r   = (AVATAR_D / 2) * mm
    c.saveState()
    c.setFillColor(LIGHT_GREY)
    c.setStrokeColor(LIGHT_GREY)
    c.circle(ax, ay, r, stroke=0, fill=1)
    c.restoreState()

    # ── name-field label (locale-aware font) ──────────────────────────────
    _lbl_font = FONT_JP if any('\u3040' <= ch <= '\u9fff' for ch in NAME_LABEL) else FONT_EN
    c.setFont(_lbl_font, 7)
    c.setFillColor(GREY)
    c.drawString(ox + TEXT_LEFT * mm, oy + NAME_LABEL_Y * mm, NAME_LABEL)

    # ── name underline — y aligns with avatar bottom ───────────────────────
    c.setStrokeColor(LINE_GREY)
    c.setLineWidth(0.5)
    c.line(ox + TEXT_LEFT * mm, oy + NAME_LINE_Y * mm,
           ox + TEXT_RIGHT * mm, oy + NAME_LINE_Y * mm)

    # ── footer: butterfly + event name ────────────────────────────────────
    footer_ctr = oy + (FOOTER_H / 2) * mm

    logo_w_pt = logo_w_mm * mm
    logo_h_pt = logo_h_mm * mm
    gap_pt    = 2.0 * mm
    tw        = stringWidth(FOOTER_TEXT, FONT_EN_BOLD, FOOTER_FS)
    unit_w    = (logo_w_pt + gap_pt + tw) if butterfly_tmp else tw
    start_x   = ox + (CARD_W - unit_w) / 2

    if butterfly_tmp:
        logo_y = footer_ctr - logo_h_pt / 2
        c.drawImage(butterfly_tmp, start_x, logo_y,
                    logo_w_pt, logo_h_pt, mask="auto")
        text_x = start_x + logo_w_pt + gap_pt
    else:
        text_x = start_x

    text_y = footer_ctr - FOOTER_FS * 0.36
    t = c.beginText(text_x, text_y)
    t.setFont(FONT_EN_BOLD, FOOTER_FS)
    t.setFillColor(BSKY_BLUE)
    t.setCharSpace(0.5)
    t.textOut(FOOTER_TEXT)
    c.drawText(t)


# ── PDF builder ───────────────────────────────────────────────────────────────

def build_pdf(profiles: list, output_path: str,
              press_count: int = PRESS_COUNT_DEFAULT,
              blank_count: int = BLANK_COUNT_DEFAULT,
              event_name: str  = FOOTER_TEXT,
              show_logo: bool  = True,
              card_spec: str   = "meishi",
              paper: str       = "a4",
              name_label: str  = NAME_LABEL) -> None:
    global FOOTER_TEXT, NAME_LABEL

    # Apply card / page layout first (updates all dimension globals)
    cw, ch = parse_card_spec(card_spec)
    configure_layout(cw, ch, PAPER_SIZES[paper])

    FOOTER_TEXT = event_name
    NAME_LABEL  = name_label

    avatar_px                       = int(AVATAR_D / 25.4 * DPI)
    butterfly_tmp, logo_w, logo_h   = prepare_butterfly() if show_logo \
                                      else (None, 0.0, 0.0)

    print("\nPreparing avatar images…")
    cards = []
    for p in profiles:
        handle = p["handle"]
        raw    = download_image(p["avatar_url"]) if p["avatar_url"] else None
        circ   = make_circular_avatar(raw, avatar_px) if raw \
                 else make_placeholder(avatar_px, handle)
        cards.append((handle, pil_to_temp_png(circ)))
        print(f"  ✓ @{handle}")

    c     = rl_canvas.Canvas(output_path, pagesize=(PAGE_W, PAGE_H))
    pages = max(1, (len(cards) + PER_PAGE - 1) // PER_PAGE)

    for page in range(pages):
        if page > 0:
            c.showPage()
        batch = cards[page * PER_PAGE : (page + 1) * PER_PAGE]
        for idx, (handle, avatar_tmp) in enumerate(batch):
            col = idx % COLS
            row = idx // COLS
            ox  = MARGIN_X + col * CARD_W
            oy  = PAGE_H - MARGIN_Y - (row + 1) * CARD_H
            draw_card(c, ox, oy, handle, avatar_tmp,
                      butterfly_tmp, logo_w, logo_h)

    # ── PRESS cards appended after participant cards ──────────────────────
    offset = len(cards)
    if press_count > 0:
        for i in range(press_count):
            abs_idx     = offset + i
            pos_on_page = abs_idx % PER_PAGE
            if pos_on_page == 0:
                c.showPage()
            col = pos_on_page % COLS
            row = pos_on_page // COLS
            ox  = MARGIN_X + col * CARD_W
            oy  = PAGE_H - MARGIN_Y - (row + 1) * CARD_H
            draw_press_card(c, ox, oy, butterfly_tmp, logo_w, logo_h)

    # ── BLANK cards appended after PRESS cards ────────────────────────────
    blank_offset = offset + press_count
    if blank_count > 0:
        for i in range(blank_count):
            abs_idx     = blank_offset + i
            pos_on_page = abs_idx % PER_PAGE
            if pos_on_page == 0:
                c.showPage()
            col = pos_on_page % COLS
            row = pos_on_page // COLS
            ox  = MARGIN_X + col * CARD_W
            oy  = PAGE_H - MARGIN_Y - (row + 1) * CARD_H
            draw_blank_card(c, ox, oy, butterfly_tmp, logo_w, logo_h)

    total_cards = offset + press_count + blank_count
    pages = (total_cards + PER_PAGE - 1) // PER_PAGE

    c.save()

    for _, tmp in cards:
        try: os.unlink(tmp)
        except OSError: pass
    if butterfly_tmp:
        try: os.unlink(butterfly_tmp)
        except OSError: pass

    print(f"\n✅ PDF saved → {output_path}")
    print(f"   {len(cards)} participant + {press_count} PRESS + {blank_count} blank = {total_cards} cards, {pages} page(s)")


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_args():
    p = argparse.ArgumentParser(
        description="Bluesky event card sheet (89×53mm landscape, 10 per A4).",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(f"""
        Examples:
          # Full run with defaults (meishi size, A4, 4 PRESS + 8 blank)
          python3 bluesky_name_cards.py --file handles.txt --output cards.pdf

          # 4×3 inch cards on Letter paper
          python3 bluesky_name_cards.py --file handles.txt --card 4x3 --paper letter

          # Custom card size (100 × 60 mm)
          python3 bluesky_name_cards.py --file handles.txt --card 100x60

          # Custom event name and Japanese name label
          python3 bluesky_name_cards.py --file handles.txt \\
            --event "Bluesky Meetup Vol. 5" --name-label "お名前"

          # Participant cards only — no PRESS, no blank, no logo
          python3 bluesky_name_cards.py --file handles.txt \\
            --press 0 --blank 0 --no-logo

          # Single test card
          python3 bluesky_name_cards.py jay.bsky.social --press 0 --blank 0
        """),
    )
    p.add_argument("handles", nargs="*",
                   help="Bluesky handles (inline, without @)")
    p.add_argument("--file",   "-f",
                   help="Text file with one handle per line")
    p.add_argument("--output", "-o", default="bluesky_cards.pdf",
                   help="Output PDF path (default: bluesky_cards.pdf)")
    # ── Card / paper size ─────────────────────────────────────────────────
    p.add_argument("--card", default="meishi", metavar="PRESET|WxH",
                   help=(
                       'Card size. Presets: "meishi" (89×53 mm), "4x3" (101.6×76.2 mm). '
                       'Custom: WxH in mm, e.g. "100x60". (default: meishi)'
                   ))
    p.add_argument("--paper", default="a4", choices=["a4", "letter"],
                   help="Paper size: a4 (210×297 mm) or letter (215.9×279.4 mm) (default: a4)")
    # ── Extra cards ───────────────────────────────────────────────────────
    p.add_argument("--press",  type=int, default=PRESS_COUNT_DEFAULT,
                   metavar="N",
                   help=f"Number of PRESS cards to append (0 = none, default: {PRESS_COUNT_DEFAULT})")
    p.add_argument("--blank",  type=int, default=BLANK_COUNT_DEFAULT,
                   metavar="N",
                   help=f"Number of blank cards to append (0 = none, default: {BLANK_COUNT_DEFAULT})")
    # ── Content ───────────────────────────────────────────────────────────
    p.add_argument("--event",  default=FOOTER_TEXT, metavar="TEXT",
                   help=f'Event name in the card footer (default: "{FOOTER_TEXT}")')
    p.add_argument("--name-label", default=NAME_LABEL, metavar="TEXT",
                   help=f'Label above the name field (default: "{NAME_LABEL}")')
    p.add_argument("--no-logo", action="store_true",
                   help="Omit the Bluesky butterfly logo from the footer")
    return p.parse_args()


def main():
    args    = parse_args()
    handles = list(args.handles)

    if args.file:
        path = Path(args.file)
        if not path.exists():
            sys.exit(f"Error: file not found: {args.file}")
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                handles.append(line)

    handles = [h.lstrip("@").strip() for h in handles if h.strip()]
    if not handles:
        sys.exit("No handles provided.")

    print(f"Fetching profiles for {len(handles)} handle(s)…")
    profiles = []
    for h in handles:
        print(f"  → @{h}", end=" ", flush=True)
        prof = fetch_profile(h)
        print("(ok)" if prof["avatar_url"] else "(no avatar)")
        profiles.append(prof)

    # Validate card spec early so errors appear before network calls
    try:
        parse_card_spec(args.card)
    except ValueError as e:
        sys.exit(f"Error: {e}")

    build_pdf(profiles, args.output,
              press_count=max(0, args.press),
              blank_count=max(0, args.blank),
              event_name=args.event,
              show_logo=not args.no_logo,
              card_spec=args.card,
              paper=args.paper,
              name_label=args.name_label)


if __name__ == "__main__":
    main()
