"""Static README screenshots from a demo workspace (fictional data). Same setup as record_gifs.py.
    python tools/screenshots.py [--url http://127.0.0.1:8777] [--out docs/img]"""
import argparse, os
from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://127.0.0.1:8777")
ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "img"))
args = ap.parse_args()
MASK = """() => { const w = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT); let n;
  while ((n = w.nextNode())) n.nodeValue = n.nodeValue.replace(/[A-Z]:\\\\[^\\s]*?(demo-ws|JobAgent[-\\w]*)/g, '~/JobAgent').replace(/\\/(home|Users)\\/[^\\s/]+\\/[^\\s]*/g, '~/JobAgent') }"""
SHOTS = [("gui-today.png", "today", "light", None), ("gui-today-dark.png", "today", "dark", None), ("gui-usage.png", "usage", "light", None),
         ("gui-inbox.png", "inbox", "light", None), ("gui-settings.png", "settings", "light", "#s-times"), ("gui-chat.png", "chat", "light", None), ("gui-picks.png", "picks", "light", None)]
with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    for name, view, theme, anchor in SHOTS:
        page = b.new_page(viewport={"width": 1440, "height": 900})
        page.add_init_script(f"try {{ localStorage.setItem('ja-tour','1'); localStorage.setItem('ja-theme','{theme}') }} catch (e) {{}}")
        page.goto(args.url)
        page.wait_for_timeout(1400)
        page.evaluate(f"() => go('{view}')")
        page.wait_for_timeout(2200)
        if anchor:
            page.wait_for_selector(anchor, timeout=90000)
            page.wait_for_timeout(500)
        page.evaluate(MASK)
        if anchor:
            page.evaluate(f"() => {{ const e = document.querySelector('{anchor}'); if (e) {{ e.closest('.card, div').scrollIntoView({{block: 'start'}}); window.scrollBy(0, -130) }} }}")
            page.wait_for_timeout(300)
        page.screenshot(path=os.path.join(args.out, name))
        print(name)
        page.close()
    b.close()
