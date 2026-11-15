"""Record short GIFs of the GUI for the README, from a demo workspace (fictional data).
    python tools/make_demo.py <demo>; JOBAGENT_HOME=<demo> python agent/run.py gui --no-open   (port 8777, or pass --url)
    python tools/record_gifs.py [--url http://127.0.0.1:8777] [--out docs/img]
Needs Playwright (installed by the installer) and Pillow (dev only: python -m pip install pillow). Uses your installed Chrome."""
import argparse, io, os, time
from PIL import Image
from playwright.sync_api import sync_playwright

ap = argparse.ArgumentParser()
ap.add_argument("--url", default="http://127.0.0.1:8777")
ap.add_argument("--out", default=os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "docs", "img"))
args = ap.parse_args()
W, H = 1280, 800


class Rec:
    def __init__(self, page):
        self.page, self.frames = page, []

    def snap(self, ms=120):
        self.frames.append((Image.open(io.BytesIO(self.page.screenshot(type="png"))).convert("RGB"), ms))

    def hold(self, ms):
        self.snap(ms)

    def type(self, sel, text, per=70):
        for ch in text:
            self.page.type(sel, ch)
            self.snap(per)

    def save(self, name, width=1000):
        imgs = []
        for im, ms in self.frames:
            im = im.resize((width, int(im.height * width / im.width)), Image.LANCZOS)
            imgs.append((im.quantize(colors=96, method=Image.Quantize.MEDIANCUT, dither=Image.Dither.NONE), ms))
        path = os.path.join(args.out, name)
        imgs[0][0].save(path, save_all=True, append_images=[i for i, _ in imgs[1:]], duration=[ms for _, ms in imgs], loop=0, optimize=True, disposal=1)
        print(name, len(imgs), "frames", round(os.path.getsize(path) / 1e6, 2), "MB")
        self.frames = []


def fresh(page, view):
    page.goto(args.url)
    page.wait_for_timeout(1300)
    page.evaluate(f"() => go('{view}')")
    page.wait_for_timeout(1300)


with sync_playwright() as p:
    b = p.chromium.launch(channel="chrome", headless=True)
    page = b.new_page(viewport={"width": W, "height": H}, device_scale_factor=1)
    page.add_init_script("try { localStorage.setItem('ja-tour','1'); localStorage.setItem('ja-theme','light') } catch (e) {}")  # no tour overlay
    r = Rec(page)

    # 0. Hero: the whole story in ~12 seconds. Next action -> who you know -> the referral plan -> asking the agent
    fresh(page, "today")
    r.hold(2200)
    page.evaluate("() => go('picks')")
    page.wait_for_timeout(1200)
    r.hold(2200)
    page.locator("#picks-list button", has_text="Open").first.click()
    page.wait_for_timeout(800)
    page.locator("#dt button", has_text="Referral").first.click()
    page.wait_for_timeout(700)
    r.hold(2800)
    page.evaluate("() => closeDrawer()")
    page.evaluate("() => go('chat')")
    page.wait_for_timeout(1500)
    page.click("#chat-in")
    r.type("#chat-in", "/top 3", 90)
    page.keyboard.press("Enter")
    page.wait_for_timeout(1500)
    page.evaluate("() => { const b = document.getElementById('chatbox'); b.scrollTop = b.scrollHeight }")
    r.hold(2800)
    r.save("gif-hero.gif", width=1100)

    # 1. Ask the agent with slash commands (instant, no model needed)
    fresh(page, "chat")
    page.evaluate("() => { const b = document.getElementById('chatbox'); b.scrollTop = b.scrollHeight }")
    r.hold(900)
    page.click("#chat-in")
    r.type("#chat-in", "/li", 110)
    r.hold(1300)
    page.fill("#chat-in", "")
    r.type("#chat-in", "/top 3", 90)
    r.hold(500)
    page.keyboard.press("Enter")
    page.wait_for_timeout(1600)
    page.evaluate("() => { const b = document.getElementById('chatbox'); b.scrollTop = b.scrollHeight }")
    r.hold(2600)
    r.save("gif-ask-the-agent.gif")

    # 2. Command palette: jump to a job in two keystrokes
    fresh(page, "today")
    r.hold(1400)
    page.keyboard.press("Control+K")
    page.wait_for_timeout(250)
    r.hold(700)
    r.type("#pal-in", "kernel", 120)
    r.hold(900)
    page.keyboard.press("Enter")
    page.wait_for_timeout(900)
    r.hold(1200)
    for tab in ("Cover letter", "Referral"):
        btn = page.locator("#dt button", has_text=tab)
        if btn.count():
            btn.first.click()
            page.wait_for_timeout(600)
            r.hold(1600)
    r.save("gif-command-palette.gif")

    # 3. All jobs: filter like a job board, then reset
    fresh(page, "browse")
    r.hold(1200)
    for label in ("ML systems / inference / kernels", "Remote"):
        page.locator("label", has_text=label).first.click()
        page.wait_for_timeout(700)
        r.hold(900)
    page.fill(".fpanel input[type=search], .fpanel input[placeholder*='kernel']", "")
    r.type(".fpanel input[placeholder*='kernel']", "intern", 110)
    page.wait_for_timeout(800)
    r.hold(1400)
    page.locator("button", has_text="Reset all filters").first.click()
    page.wait_for_timeout(800)
    r.hold(1400)
    r.save("gif-all-jobs.gif")

    # 4. My picks: who you know at each company, then the referral plan
    fresh(page, "picks")
    r.hold(1600)
    page.mouse.wheel(0, 300)
    page.wait_for_timeout(400)
    r.hold(1200)
    page.locator("#picks-list button", has_text="Open").first.click()
    page.wait_for_timeout(900)
    r.hold(1000)
    page.locator("#dt button", has_text="Referral").first.click()
    page.wait_for_timeout(700)
    r.hold(2600)
    r.save("gif-my-picks.gif")
    b.close()
