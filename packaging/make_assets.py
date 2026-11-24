"""Generates what the build needs: the app icon, the installer artwork, the version resource and the
code statistics shown in Settings > About (the installed app ships no .py files to count).

    python packaging/make_assets.py          (run from the repository root; build.ps1 does it for you)

Everything is drawn here from the same logo as the tray icon, so there are no binary files to keep in sync."""
import json
import math
import os
import random
import sys

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
OUT = os.path.join(ROOT, "build", "assets")

from rshub import VERSION  # noqa: E402
from rshub.tray import make_icon_image, save_ico  # noqa: E402

SPACE_TOP, SPACE_BOTTOM = (6, 10, 20), (14, 22, 42)  # #060A14 → a slightly lifted navy, as in the app
CORAL, ICE = (0, 240, 208), (255, 255, 255)


def icon():
    """Every size in the .ico is drawn on its own: 16 to 40 px are the hand-tuned small drawings."""
    path = os.path.join(OUT, "RsHub.ico")
    save_ico(path)
    return path


def _space(w, h, seed):
    """Deep-space background: vertical gradient, a coral glow low on the left, an ice one high on the right, stars."""
    m = Image.linear_gradient("L").resize((w, h), Image.BICUBIC)
    img = Image.composite(Image.new("RGBA", (w, h), SPACE_BOTTOM + (255,)),
                          Image.new("RGBA", (w, h), SPACE_TOP + (255,)), m)
    for col, alpha, (gx, gy), gr in ((CORAL, 30, (.1, .95), .7), (ICE, 18, (.95, .05), .6), (CORAL, 22, (.5, .4), .45)):
        glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        r = w * gr
        ImageDraw.Draw(glow).ellipse((w * gx - r, h * gy - r, w * gx + r, h * gy + r), fill=col + (alpha,))
        img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(r * .5)))
    rnd = random.Random(seed)
    stars = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stars)
    for _ in range(int(w * h / 1500)):
        x, y = rnd.random() * w, rnd.random() * h
        s = rnd.choice((.6, .6, .8, 1, 1.3)) * w / 202
        a = rnd.randint(50, 210)
        col = ICE if rnd.random() < .25 else (255, 255, 255)
        sd.ellipse((x - s / 2, y - s / 2, x + s / 2, y + s / 2), fill=col + (a,))
    return Image.alpha_composite(img, stars)


def _font(size, weight):
    f = ImageFont.truetype(os.path.join(ROOT, "rshub", "ui", "fonts", "unbounded-latin.woff2"), size)
    try:
        f.set_variation_by_axes([weight])
    except (OSError, AttributeError):
        pass
    return f


def wizard_large(w, h):
    """Welcome and finish pages: the planet large, faint orbits around it, the name below."""
    k = 2  # drawn at twice the size, then scaled down: smooth orbit lines
    W, H = w * k, h * k
    img = _space(W, H, seed=7)
    cx, cy = W * .5, H * .38
    orbits = Image.new("L", (W, H), 0)
    od = ImageDraw.Draw(orbits)
    lw = max(1, round(W / 202 * 1.1))
    for rx, ry, a in ((.62, .19, 60), (.86, .27, 40), (1.18, .37, 26)):
        od.ellipse((cx - W * rx, cy - W * ry, cx + W * rx, cy + W * ry), outline=a, width=lw)
    orbits = orbits.rotate(20, resample=Image.BICUBIC, center=(cx, cy))
    layer = Image.new("RGBA", (W, H), ICE + (0,))
    layer.putalpha(orbits)
    img = Image.alpha_composite(img, layer)
    # a small coral moon on the inner orbit, lower left
    md = ImageDraw.Draw(img)
    t, a = math.radians(130), math.radians(20)
    ox, oy = W * (.62 - .0055) * math.cos(t), W * (.19 - .0055) * math.sin(t)
    mx, my, mr = cx + ox * math.cos(a) + oy * math.sin(a), cy - ox * math.sin(a) + oy * math.cos(a), W * .018
    md.ellipse((mx - mr, my - mr, mx + mr, my + mr), fill=(255, 196, 182, 230))
    halo = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    r = W * .3
    ImageDraw.Draw(halo).ellipse((cx - r, cy - r, cx + r, cy + r), fill=CORAL + (50,))
    img = Image.alpha_composite(img, halo.filter(ImageFilter.GaussianBlur(W * .12)))
    g = make_icon_image(int(W * 1.1), tile=False)
    img.alpha_composite(g, (int(cx - g.width * .5), int(cy - g.height * .53)))
    # the name, in the app's display font
    f = _font(int(W * .1), 560)
    text = "RsHub"
    tw = f.getlength(text)
    ty = H * .79
    ImageDraw.Draw(img).text((cx - tw / 2, ty), text, font=f, fill=(238, 242, 249, 255))
    # a short coral-to-ice line under it
    lw2, lh = W * .16, max(2, round(W * .011))
    bar = Image.linear_gradient("L").rotate(90).resize((int(lw2), lh))
    line = Image.composite(Image.new("RGBA", bar.size, ICE + (255,)), Image.new("RGBA", bar.size, CORAL + (255,)), bar)
    img.alpha_composite(line, (int(cx - lw2 / 2), int(ty + W * .175)))
    return img.resize((w, h), Image.LANCZOS).convert("RGB")


def wizard_small(size):
    return make_icon_image(size)


def wizard_back(w, h):
    """Background of every wizard page: the app's navy, barely-there stars and glows, one faint orbit. Kept dark and
    quiet so text and checkboxes stay easy to read on top of it."""
    m = Image.linear_gradient("L").resize((w, h), Image.BICUBIC)
    img = Image.composite(Image.new("RGBA", (w, h), (6, 10, 20, 255)), Image.new("RGBA", (w, h), (11, 18, 36, 255)), m)
    for col, alpha, (gx, gy), gr in ((CORAL, 16, (.02, 1.0), .42), (ICE, 10, (1.0, .0), .4)):
        glow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        r = w * gr
        ImageDraw.Draw(glow).ellipse((w * gx - r, h * gy - r, w * gx + r, h * gy + r), fill=col + (alpha,))
        img = Image.alpha_composite(img, glow.filter(ImageFilter.GaussianBlur(r * .5)))
    k = 2  # the orbit is drawn at twice the size for a smooth line
    orbit = Image.new("L", (w * k, h * k), 0)
    ox, oy, rx, ry, a = w * k * .98, h * k * 1.02, w * k * .62, h * k * .3, math.radians(14)
    pts = [(ox + rx * math.cos(t) * math.cos(a) + ry * math.sin(t) * math.sin(a),
            oy - rx * math.cos(t) * math.sin(a) + ry * math.sin(t) * math.cos(a))
           for t in (i * math.pi / 180 for i in range(361))]
    ImageDraw.Draw(orbit).line(pts, fill=24, width=max(1, round(k * w / 596)))
    orbit = orbit.resize((w, h), Image.LANCZOS)
    layer = Image.new("RGBA", (w, h), ICE + (0,))
    layer.putalpha(orbit)
    img = Image.alpha_composite(img, layer)
    rnd = random.Random(5)
    stars = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    sd = ImageDraw.Draw(stars)
    for _ in range(int(w * h / 5200)):
        x, y = rnd.random() * w, rnd.random() * h
        r = rnd.choice((.5, .5, .7, .9)) * w / 596
        col = ICE if rnd.random() < .3 else (255, 255, 255)
        sd.ellipse((x - r, y - r, x + r, y + r), fill=col + (rnd.randint(30, 110),))
    return Image.alpha_composite(img, stars).convert("RGB")


def version_file():
    nums = [int(x) for x in VERSION.split(".")] + [0] * 4
    v = tuple(nums[:4])
    text = f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers={v}, prodvers={v}, mask=0x3f, flags=0x0, OS=0x40004, fileType=0x1, subtype=0x0,
                    date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('040904B0', [
      StringStruct('CompanyName', 'Esteban Arosemena'),
      StringStruct('FileDescription', 'RsHub'),
      StringStruct('FileVersion', '{VERSION}'),
      StringStruct('InternalName', 'RsHub'),
      StringStruct('LegalCopyright', 'Copyright (C) Esteban Arosemena. Licensed under the GNU GPL v3.'),
      StringStruct('OriginalFilename', 'RsHub.exe'),
      StringStruct('ProductName', 'RsHub'),
      StringStruct('ProductVersion', '{VERSION}')])]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
"""
    path = os.path.join(OUT, "version_info.txt")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)


def project_stats():
    langs = {".py": "Python", ".js": "JavaScript", ".css": "CSS", ".html": "HTML"}
    out = {}
    for dirpath, dirs, files in os.walk(os.path.join(ROOT, "rshub")):
        dirs[:] = [d for d in dirs if d not in ("vendor", "fonts", "__pycache__")]
        for f in files:
            lang = langs.get(os.path.splitext(f)[1].lower())
            if lang:
                out[lang] = out.get(lang, 0) + os.path.getsize(os.path.join(dirpath, f))
    with open(os.path.join(OUT, "project_stats.json"), "w", encoding="utf-8") as fh:
        json.dump(out, fh)


def main():
    os.makedirs(OUT, exist_ok=True)
    icon()
    # sizes from the Inno Setup help: the image area at 100, 125, 150, 175 and 250 % scaling
    for w, h in ((202, 386), (269, 515), (336, 643), (403, 772), (534, 1022)):
        wizard_large(w, h).save(os.path.join(OUT, f"wizard-{w}.png"), optimize=True)
    for s in (58, 77, 97, 116, 159):
        wizard_small(s).save(os.path.join(OUT, f"wizard-small-{s}.png"), optimize=True)
    # the page background, at the sizes the Inno Setup help lists for 100, 125, 150, 200 and 250 % scaling
    for w, h in ((596, 432), (796, 576), (994, 720), (1272, 922), (1630, 1148)):
        wizard_back(w, h).save(os.path.join(OUT, f"wizard-back-{w}.png"), optimize=True)
    version_file()
    project_stats()
    print("assets ->", OUT, f"(version {VERSION})")


if __name__ == "__main__":
    main()
