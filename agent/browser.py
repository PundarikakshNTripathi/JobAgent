"""Shared helpers for Claude-driven browser sessions in the dedicated agent Chrome (CDP port, own profile, you are logged in to LinkedIn / YC there)."""
import json, os, shutil, time
from common import DATA, SETTINGS
import fill

READONLY_GUARD = r"""
(() => {
  // Read-only guard for LinkedIn-style sites: blocks every action button. Always on (no expiry).
  const ACT = /^(connect|message|follow|following|send|send now|send without a note|post|like|comment|repost|invite|endorse|accept|ignore|withdraw|pending|add a note|save to pdf|report|block|unfollow|remove)$/i;
  const txt = el => (el && (el.innerText || el.value || el.getAttribute('aria-label') || '')).trim().split('\n')[0];
  addEventListener('click', e => {
    const el = e.target && e.target.closest ? e.target.closest('button, [role=button], a[role=button], input[type=submit]') : null;
    if (!el) return;
    const t = txt(el);
    if (ACT.test(t) || /^(connect|message|follow|invite)\b/i.test(t) || /^(connect|message|follow|invite|send)\s/i.test(el.getAttribute('aria-label') || '')) {
      e.preventDefault(); e.stopImmediatePropagation(); console.warn('[jobagent] read-only guard blocked', t);
    }
  }, true);
  addEventListener('submit', e => { e.preventDefault(); e.stopImmediatePropagation(); }, true);
  const b = () => { if (!document.documentElement || document.getElementById('__ja_ro')) return; const d = document.createElement('div'); d.id = '__ja_ro';
    d.style.cssText = 'position:fixed;z-index:2147483647;right:8px;bottom:8px;background:#1e3a8a;color:#fff;font:600 11px system-ui;padding:4px 8px;border-radius:6px;pointer-events:none';
    d.textContent = 'JobAgent: read-only browsing'; document.documentElement.appendChild(d); };
  setInterval(b, 2000);
})();
"""


def npx_server(args):
    """MCP server entry that runs `npx -y <args>` on any OS (Windows needs cmd /c for the npx.cmd shim)."""
    if os.name == "nt":
        return {"command": "cmd", "args": ["/c", "npx", "-y"] + args}
    return {"command": shutil.which("npx") or "npx", "args": ["-y"] + args}


def mcp_config(name="readonly", guard_js=None):
    """Write an MCP config attaching Playwright to the agent Chrome. guard_js=None -> no init script."""
    fill.ensure_chrome()
    args = ["@playwright/mcp@0.0.84", "--cdp-endpoint", f"http://127.0.0.1:{SETTINGS['chrome_debug_port']}", "--allow-unrestricted-file-access"]
    if guard_js:
        gp = os.path.join(DATA, f"guard-{name}.js")
        open(gp, "w", encoding="utf-8").write(guard_js)
        args += ["--init-script", gp]
    path = os.path.join(DATA, f"mcp-{name}.json")
    json.dump({"mcpServers": {"fillbrowser": npx_server(args)}}, open(path, "w"), indent=1)
    return path
