#!/usr/bin/env python3
"""Render a recorded terminal cast to PNG stills / video frames using pyte + Pillow.

Box-drawing glyphs are drawn geometrically so borders join seamlessly regardless
of the font's advance width; everything else comes from DejaVu Sans Mono, with
Noto Color Emoji for pictographs.
"""
import json, os, sys
import pyte
from PIL import Image, ImageDraw, ImageFont

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono.ttf"
FONT_B = "/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf"
EMOJI = "/usr/share/fonts/truetype/noto/NotoColorEmoji.ttf"
FS = 17
CW = 10          # cell width  (px)
CH = 24          # cell height (px)
PAD = 22
TITLEBAR = 34

BG = (23, 24, 28)
FG = (220, 224, 232)
WINDOW = (16, 17, 20)

ANSI = {
    "black": (60, 62, 70), "red": (233, 96, 100), "green": (126, 202, 128),
    "yellow": (231, 190, 108), "blue": (110, 165, 240), "magenta": (198, 141, 233),
    "cyan": (108, 205, 214), "white": (220, 224, 232),
    "brightblack": (124, 128, 140), "brightred": (255, 128, 130),
    "brightgreen": (160, 226, 160), "brightyellow": (247, 213, 140),
    "brightblue": (140, 190, 255), "brightmagenta": (219, 173, 245),
    "brightcyan": (150, 226, 232), "brightwhite": (255, 255, 255),
}

# glyph -> which half-edges are lit: (left, right, up, down)
BOX = {
    "─": (1, 1, 0, 0), "━": (1, 1, 0, 0),
    "│": (0, 0, 1, 1), "┃": (0, 0, 1, 1),
    "╭": (0, 1, 0, 1), "┌": (0, 1, 0, 1),
    "╮": (1, 0, 0, 1), "┐": (1, 0, 0, 1),
    "╰": (0, 1, 1, 0), "└": (0, 1, 1, 0),
    "╯": (1, 0, 1, 0), "┘": (1, 0, 1, 0),
    "├": (0, 1, 1, 1), "┤": (1, 0, 1, 1),
    "┬": (1, 1, 0, 1), "┴": (1, 1, 1, 0), "┼": (1, 1, 1, 1),
}
SHADE = {"░": 0.25, "▒": 0.5, "▓": 0.75, "█": 1.0}


def color(name, default):
    if name in ANSI:
        return ANSI[name]
    if isinstance(name, str) and len(name) == 6:
        try:
            return tuple(int(name[i:i + 2], 16) for i in (0, 2, 4))
        except ValueError:
            return default
    return default


def blend(fg, bg, a):
    return tuple(int(bg[i] + (fg[i] - bg[i]) * a) for i in range(3))


class Player:
    def __init__(self, cast):
        self.cols, self.rows = cast["cols"], cast["rows"]
        self.events = cast["events"]
        self.screen = pyte.Screen(self.cols, self.rows)
        self.stream = pyte.Stream(self.screen)
        self.i = 0

    def advance_to(self, t):
        while self.i < len(self.events) and self.events[self.i][0] <= t:
            ev = self.events[self.i]
            if ev[1] == "o":
                self.stream.feed(ev[2])
            self.i += 1

    @property
    def duration(self):
        return max([e[0] for e in self.events], default=0.0)


_fonts = {}


def fonts():
    if not _fonts:
        _fonts["r"] = ImageFont.truetype(FONT, FS)
        _fonts["b"] = ImageFont.truetype(FONT_B, FS)
        _fonts["t"] = ImageFont.truetype(FONT, 13)
        try:
            _fonts["e"] = ImageFont.truetype(EMOJI, 109)  # NotoColorEmoji: fixed 109px
        except OSError:
            _fonts["e"] = None
    return _fonts


def render(screen, cols, rows, title="g023 code", crop=False):
    f = fonts()
    w = CW * cols + PAD * 2
    used = rows
    if crop:
        used = 1
        for y in range(rows):
            if "".join(c.data for c in screen.buffer[y].values()).strip():
                used = y + 1
        used = min(rows, used + 1)
    h = CH * used + PAD * 2 + TITLEBAR
    img = Image.new("RGB", (w, h), WINDOW)
    d = ImageDraw.Draw(img)
    d.rectangle([0, TITLEBAR, w, h], fill=BG)
    for i, c in enumerate([(255, 95, 86), (255, 189, 46), (39, 201, 63)]):
        d.ellipse([16 + i * 20, 12, 26 + i * 20, 22], fill=c)
    d.text((w // 2, 17), title, font=f["t"], fill=(138, 142, 154), anchor="mm")

    for y in range(used):
        line = screen.buffer[y]
        for x in range(cols):
            cell = line[x]
            ch = cell.data
            if not ch or ch == " " and cell.bg == "default" and not cell.reverse:
                continue
            fgc = color(cell.fg, FG)
            bgc = color(cell.bg, BG) if cell.bg != "default" else BG
            if cell.bold and isinstance(cell.fg, str) and cell.fg in ANSI \
                    and not cell.fg.startswith("bright"):
                fgc = ANSI["bright" + cell.fg]
            if cell.reverse:
                fgc, bgc = bgc, fgc
            px, py = PAD + x * CW, TITLEBAR + PAD + y * CH
            if bgc != BG:
                d.rectangle([px, py, px + CW - 1, py + CH - 1], fill=bgc)
            if ch == " ":
                continue
            if ch in BOX:
                left, right, up, down = BOX[ch]
                cx, cy = px + CW // 2, py + CH // 2
                t = 1
                if left:
                    d.rectangle([px, cy - t, cx, cy + t - 1], fill=fgc)
                if right:
                    d.rectangle([cx, cy - t, px + CW, cy + t - 1], fill=fgc)
                if up:
                    d.rectangle([cx - t, py, cx + t - 1, cy], fill=fgc)
                if down:
                    d.rectangle([cx - t, cy, cx + t - 1, py + CH], fill=fgc)
                continue
            if ch in SHADE:
                d.rectangle([px, py + 2, px + CW - 1, py + CH - 3],
                            fill=blend(fgc, bgc, SHADE[ch]))
                continue
            if ord(ch) > 0x2500 and f["e"] and ord(ch) >= 0x1F000:
                try:
                    em = Image.new("RGBA", (109, 109), (0, 0, 0, 0))
                    ImageDraw.Draw(em).text((0, 0), ch, font=f["e"], embedded_color=True)
                    em = em.crop(em.getbbox() or (0, 0, 109, 109)).resize(
                        (CW * 2 - 2, CH - 6), Image.LANCZOS)
                    img.paste(em, (px, py + 3), em)
                    continue
                except Exception:
                    pass
            d.text((px, py + 3), ch, font=(f["b"] if cell.bold else f["r"]), fill=fgc)
    return img


def main():
    cast_path, outdir, mode = sys.argv[1], sys.argv[2], sys.argv[3]
    cast = json.load(open(cast_path))
    os.makedirs(outdir, exist_ok=True)
    title = sys.argv[4] if len(sys.argv) > 4 else "g023 code"
    if mode == "frames":
        fps = float(sys.argv[5]) if len(sys.argv) > 5 else 12.0
        p = Player(cast)
        n = int((p.duration + 1.5) * fps)
        for k in range(n):
            p.advance_to(k / fps)
            render(p.screen, p.cols, p.rows, title).save(f"{outdir}/f{k:05d}.png")
        print(n, p.duration)
    elif mode == "marks":
        p = Player(cast)
        prefix = sys.argv[5] if len(sys.argv) > 5 else ""
        for ev in cast["events"]:
            if ev[1] != "m":
                continue
            p.advance_to(ev[0])
            out = f"{outdir}/{prefix}{ev[2]}.png"
            render(p.screen, p.cols, p.rows, title, crop=True).save(out)
            print("wrote", out)


if __name__ == "__main__":
    main()
