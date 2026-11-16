"""Render the app icons from agent/gui_static/favicon.svg (run once after changing the logo; the outputs are committed):
    agent/gui_static/icon-512.png, icon-192.png   (Linux .desktop, PWA manifest, macOS)
    agent/gui_static/icon.ico                     (Windows shortcuts: 16-256 px, PNG-compressed entries)
Needs Playwright plus a local Chrome/Edge/Chromium, and Pillow for the small sizes."""
import io, os, struct, sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "agent"))
STATIC = os.path.join(ROOT, "agent", "gui_static")


def render(size):
    from playwright.sync_api import sync_playwright
    import fill
    exe = next((p for p in fill.CHROME_CANDIDATES if os.path.exists(p)), None)
    svg = open(os.path.join(STATIC, "favicon.svg"), encoding="utf-8").read()
    sized = svg.replace("<svg ", '<svg width="%d" height="%d" ' % (size, size), 1)  # (no backslashes inside f-strings: Python 3.10)
    html = f"<html><body style='margin:0;background:transparent'><div style='width:{size}px;height:{size}px'>{sized}</div></body></html>"
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=exe) if exe else p.chromium.launch()
        pg = b.new_page(viewport={"width": size, "height": size})
        pg.set_content(html)
        png = pg.screenshot(omit_background=True, clip={"x": 0, "y": 0, "width": size, "height": size})
        b.close()
    return png


def ico(pngs):
    """ICO container with PNG entries (supported since Windows Vista)."""
    head = struct.pack("<HHH", 0, 1, len(pngs))
    off, dirs, data = 6 + 16 * len(pngs), b"", b""
    for size, png in pngs:
        dirs += struct.pack("<BBBBHHII", size % 256, size % 256, 0, 0, 1, 32, len(png), off + len(data))
        data += png
    return head + dirs + data


def main():
    from PIL import Image
    big = render(512)
    open(os.path.join(STATIC, "icon-512.png"), "wb").write(big)
    im = Image.open(io.BytesIO(big)).convert("RGBA")
    out = []
    for s in (16, 24, 32, 48, 64, 128, 192, 256):
        buf = io.BytesIO()
        im.resize((s, s), Image.LANCZOS).save(buf, "PNG", optimize=True)
        out.append((s, buf.getvalue()))
        if s == 192:
            open(os.path.join(STATIC, "icon-192.png"), "wb").write(buf.getvalue())
    open(os.path.join(STATIC, "icon.ico"), "wb").write(ico([o for o in out if o[0] != 192]))
    print("wrote icon-512.png, icon-192.png, icon.ico")


if __name__ == "__main__":
    main()
