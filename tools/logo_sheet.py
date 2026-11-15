"""Contact sheet of the cached company logos the GUI shows, to spot wrong ones by eye.
    python tools/logo_sheet.py <out.png> [max]"""
import io, os, sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent"))
from PIL import Image, ImageDraw, ImageFont
import common, logos

out, mx = sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 160
c = common.db()
cos = [r[0] for r in c.execute("""SELECT company, MAX(COALESCE(score,0)) s FROM jobs WHERE active=1 AND (status!='new' OR score>=65)
                                  GROUP BY company ORDER BY (status!='new') DESC, s DESC LIMIT ?""", (mx,))]
cols, cw, ch = 10, 150, 112
rows = (len(cos) + cols - 1) // cols
sheet = Image.new("RGB", (cols * cw, rows * ch), "white")
dr = ImageDraw.Draw(sheet)
try:
    font = ImageFont.truetype("arial.ttf", 11)
except OSError:
    font = ImageFont.load_default()
for i, co in enumerate(cos):
    x, y = (i % cols) * cw, (i // cols) * ch
    p = os.path.join(logos.CACHE, logos._key(co) + ".img")
    if os.path.exists(p):
        try:
            b = open(p, "rb").read()
            if logos.sniff(b) == "image/svg+xml":
                raise ValueError("svg")
            im = Image.open(io.BytesIO(b)).convert("RGBA")
            im.thumbnail((64, 64))
            bg = Image.new("RGBA", im.size, "white")
            sheet.paste(Image.alpha_composite(bg, im).convert("RGB"), (x + (cw - im.width) // 2, y + 8))
        except Exception:
            dr.text((x + 50, y + 30), "(svg/err)", fill="gray", font=font)
    else:
        dr.text((x + 50, y + 30), "none", fill="gray", font=font)
    dr.text((x + 4, y + 80), f"{i}: {co[:22]}", fill="black", font=font)
sheet.save(out)
print(len(cos), "companies ->", out)
