#!/usr/bin/env python3
"""
lume-web: tiny dashboard for local lume VMs.

Reads from the lume daemon at http://127.0.0.1:7777/lume/vms and renders a
single HTML page with start/stop, VNC, SSH-into-Terminal, edit settings,
clone, delete, and create/pull actions.

Mutations go through the `lume` CLI (subprocess) rather than the daemon's
HTTP API, because the CLI is the canonical, fully-documented interface.
Fast ops (set, clone, delete) run synchronously and the page redirects
back. Long ops (create, pull) are spawned in Terminal.app so you can
watch progress.

Stdlib only. Run:  python3 server.py [--port 8080]
"""

from __future__ import annotations

import argparse
import html
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LUME = os.environ.get("LUME_DAEMON_URL", "http://127.0.0.1:7777")
# Resolve `lume` from $LUME_BIN, then $PATH, then a few common install
# locations as a last resort. Falls back to the bare name (which lets the
# subprocess raise a clear "lume binary not found" error downstream).
LUME_BIN = (
    os.environ.get("LUME_BIN")
    or shutil.which("lume")
    or next(
        (p for p in (
            os.path.expanduser("~/.local/bin/lume"),
            "/usr/local/bin/lume",
            "/opt/homebrew/bin/lume",
        ) if os.path.isfile(p) and os.access(p, os.X_OK)),
        "lume",
    )
)
REFRESH_SECONDS = 5

# Tight character set for VM names / image refs. Mirrors what `lume create`
# / `lume pull` accept in practice; rejects anything that could surprise
# argv parsing or the filesystem.
NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")
IMAGE_RE = re.compile(r"^[A-Za-z0-9._/-]+:[A-Za-z0-9._-]+$")


# ─── helpers ────────────────────────────────────────────────────────────────

def lume_request(path: str, method: str = "GET", timeout: float = 10.0) -> tuple[int, bytes]:
    req = urllib.request.Request(f"{LUME}{path}", method=method)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read() or b""
    except urllib.error.URLError as e:
        return 0, str(e).encode()


def run_lume_cli(args: list[str], timeout: float = 120.0) -> tuple[int, str]:
    """Synchronously run `lume <args>`. Returns (returncode, message)."""
    try:
        r = subprocess.run(
            [LUME_BIN, *args],
            capture_output=True,
            text=True,
            timeout=timeout,
            env={**os.environ, "TERM": "dumb"},  # suppress lume's tput
        )
        msg = (r.stderr or r.stdout or "").strip()
        # lume prints a banner + colored INFO lines we don't want surfaced.
        # Keep the last non-banner line as the user-facing message.
        last = ""
        for line in msg.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("\033"):
                continue
            last = stripped
        return r.returncode, last or msg
    except subprocess.TimeoutExpired:
        return -1, "lume command timed out"
    except FileNotFoundError:
        return -1, f"lume binary not found at {LUME_BIN}"


def spawn_in_terminal(shell_cmd: str) -> tuple[bool, str]:
    """Open Terminal.app in a new window and run shell_cmd. Returns (ok, msg)."""
    # json.dumps gives a "..."-quoted string with \" escaping — same convention
    # as AppleScript string literals, so this is injection-safe.
    script = (
        'tell application "Terminal"\n'
        "  activate\n"
        f"  do script {json.dumps(shell_cmd)}\n"
        "end tell"
    )
    try:
        subprocess.run(
            ["/usr/bin/osascript", "-e", script],
            check=False, timeout=5, capture_output=True,
        )
        return True, ""
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        return False, str(e)


def human_bytes(n: int) -> str:
    f = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if f < 1024:
            return f"{int(f)}{unit}" if unit == "B" else f"{f:.1f}{unit}"
        f /= 1024
    return f"{f:.1f}PB"


def parse_form(rfile, headers) -> dict[str, str]:
    length = int(headers.get("Content-Length", "0") or "0")
    if not length:
        return {}
    raw = rfile.read(length).decode("utf-8", errors="replace")
    parsed = urllib.parse.parse_qs(raw, keep_blank_values=False)
    return {k: v[0] for k, v in parsed.items()}


# ─── HTML rendering ─────────────────────────────────────────────────────────

CSS = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font: 15px/1.4 -apple-system, BlinkMacSystemFont, "SF Pro Text", system-ui, sans-serif;
       max-width: 1100px; margin: 0 auto; padding: 24px; background: Canvas; color: CanvasText; }
.toolbar { display: flex; justify-content: space-between; align-items: end; margin-bottom: 24px; gap: 16px; }
h1 { margin: 0 0 4px; font-size: 22px; font-weight: 600; }
.sub { color: GrayText; font-size: 13px; margin: 0; }
.grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(320px, 1fr)); gap: 16px; }
.vm { border: 1px solid color-mix(in srgb, CanvasText 20%, transparent);
       border-radius: 10px; padding: 16px; background: color-mix(in srgb, Canvas 96%, CanvasText 4%); }
.vm.running { border-color: color-mix(in srgb, #2fa84f 60%, transparent); }
.vm header { display: flex; justify-content: space-between; align-items: center; margin-bottom: 12px; }
.vm h2 { margin: 0; font-size: 16px; font-weight: 600; word-break: break-all; }
.badge { font-size: 11px; padding: 3px 8px; border-radius: 999px; text-transform: uppercase;
         letter-spacing: 0.04em; background: color-mix(in srgb, CanvasText 12%, transparent); }
.badge.running { background: #2fa84f; color: white; }
dl { display: grid; grid-template-columns: 80px 1fr; gap: 4px 12px; margin: 0 0 14px; font-size: 13px; }
dt { color: GrayText; }
dd { margin: 0; }
.bar { height: 6px; border-radius: 3px; background: color-mix(in srgb, CanvasText 12%, transparent); overflow: hidden; }
.bar > span { display: block; height: 100%; background: #2fa84f; transition: width .3s; }
small { color: GrayText; font-size: 11px; }
footer { display: flex; gap: 6px; flex-wrap: wrap; }
.btn { font: inherit; font-size: 13px; padding: 6px 12px; border-radius: 6px; cursor: pointer;
       border: 1px solid color-mix(in srgb, CanvasText 30%, transparent); background: Canvas;
       color: CanvasText; text-decoration: none; display: inline-block; }
.btn:hover { background: color-mix(in srgb, CanvasText 8%, Canvas); }
.btn.primary { background: #007aff; border-color: #007aff; color: white; }
.btn.primary:hover { background: #0066d6; }
.btn.danger { color: #c0392b; border-color: color-mix(in srgb, #c0392b 50%, transparent); }
.btn.danger:hover { background: color-mix(in srgb, #c0392b 10%, Canvas); }
.empty { color: GrayText; text-align: center; padding: 48px;
         border: 1px dashed color-mix(in srgb, CanvasText 20%, transparent); border-radius: 10px; }
.flash { padding: 10px 14px; border-radius: 6px; margin-bottom: 16px; font-size: 13px; }
.flash.err { background: #ffe5e5; color: #8b0000; }
.flash.ok { background: #e1f7e6; color: #1e6b34; }
code { font-family: ui-monospace, SFMono-Regular, monospace; font-size: 12px;
       padding: 1px 5px; background: color-mix(in srgb, CanvasText 10%, transparent); border-radius: 3px; }
dialog { border: 1px solid color-mix(in srgb, CanvasText 20%, transparent); border-radius: 12px;
         background: Canvas; color: CanvasText; padding: 0; max-width: 480px; width: 92vw; }
dialog::backdrop { background: rgba(0,0,0,.45); backdrop-filter: blur(2px); }
dialog .dlg-head { padding: 16px 20px; border-bottom: 1px solid color-mix(in srgb, CanvasText 12%, transparent); }
dialog .dlg-head h3 { margin: 0; font-size: 16px; font-weight: 600; }
dialog .dlg-head p { margin: 4px 0 0; font-size: 12px; color: GrayText; }
dialog .dlg-body { padding: 16px 20px; }
dialog .dlg-foot { padding: 12px 20px; display: flex; justify-content: flex-end; gap: 8px;
                   border-top: 1px solid color-mix(in srgb, CanvasText 12%, transparent); }
dialog label { display: block; font-size: 13px; margin-bottom: 14px; }
dialog label span { display: block; color: GrayText; font-size: 12px; margin-bottom: 4px; }
dialog input[type=text], dialog input[type=number] {
  font: inherit; font-size: 14px; padding: 7px 10px; width: 100%; border-radius: 6px;
  border: 1px solid color-mix(in srgb, CanvasText 25%, transparent);
  background: Canvas; color: CanvasText;
}
dialog .row { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; }
.tabs { display: flex; gap: 0; border-bottom: 1px solid color-mix(in srgb, CanvasText 12%, transparent); margin-bottom: 16px; }
.tabs button { font: inherit; font-size: 13px; padding: 8px 14px; border: none; background: transparent;
               color: CanvasText; cursor: pointer; border-bottom: 2px solid transparent; margin-bottom: -1px; }
.tabs button.active { border-bottom-color: #007aff; color: #007aff; }
.tab-pane { display: none; }
.tab-pane.active { display: block; }
.help { font-size: 12px; color: GrayText; margin-top: 8px; }
.help code { background: color-mix(in srgb, CanvasText 8%, transparent); }
"""

JS = """
// Auto-refresh that pauses while a dialog is open, so creating/editing
// forms aren't wiped mid-input. Also strips any flash query params from
// the URL on first load so they don't reappear after the next reload.
(function() {
  if (location.search) {
    history.replaceState({}, '', location.pathname);
  }
  setInterval(function() {
    if (document.querySelector('dialog[open]')) return;  // user is mid-form
    if (document.activeElement && document.activeElement.matches('input, textarea, select')) return;
    location.reload();
  }, REFRESH_MS);
})();

function openDialog(id) { document.getElementById(id).showModal(); }
function closeDialog(id) { document.getElementById(id).close(); }

function openEdit(name) {
  const data = window.__VM_DATA__[name];
  if (!data) return;
  const dlg = document.getElementById('dlg-edit');
  dlg.querySelector('[name=_name]').value = name;
  dlg.querySelector('[name=cpu]').value = data.cpuCount;
  dlg.querySelector('[name=memory]').value = Math.round(data.memorySize / (1024*1024*1024));
  dlg.querySelector('[name=disk_size]').value = Math.round((data.diskSize?.total || 0) / (1024*1024*1024));
  dlg.querySelector('[name=display]').value = data.display || '1024x768';
  dlg.querySelector('form').action = '/edit/' + encodeURIComponent(name);
  dlg.querySelector('.dlg-head h3').textContent = 'Edit ' + name;
  const isRunning = data.status === 'running';
  dlg.querySelector('.warn').style.display = isRunning ? 'block' : 'none';
  dlg.showModal();
}

function openClone(name) {
  const dlg = document.getElementById('dlg-clone');
  dlg.querySelector('[name=source]').value = name;
  dlg.querySelector('.dlg-head h3').textContent = 'Clone ' + name;
  dlg.showModal();
}

function confirmDelete(name) {
  return confirm("Delete VM \\"" + name + "\\"?\\n\\nThis is irreversible. The disk image and config will be removed.");
}

function switchTab(group, tab) {
  document.querySelectorAll('[data-tab-group="'+group+'"]').forEach(el => {
    const isActive = el.dataset.tab === tab;
    if (el.tagName === 'BUTTON') el.classList.toggle('active', isActive);
    else el.classList.toggle('active', isActive);
  });
}
"""


def render_create_dialog() -> str:
    return """
<dialog id="dlg-create">
<form method="POST" action="/create" id="create-form">
  <div class="dlg-head">
    <h3>Create VM</h3>
    <p>Pick the source. Empty Linux is instant; Pull and macOS require a multi-GB download.</p>
  </div>
  <div class="dlg-body">
    <div class="tabs">
      <button type="button" data-tab-group="create" data-tab="linux" class="active" onclick="switchTab('create','linux'); document.getElementById('create-mode').value='linux'">Empty Linux</button>
      <button type="button" data-tab-group="create" data-tab="pull" onclick="switchTab('create','pull'); document.getElementById('create-mode').value='pull'">Pull image</button>
      <button type="button" data-tab-group="create" data-tab="macos" onclick="switchTab('create','macos'); document.getElementById('create-mode').value='macos'">macOS (IPSW)</button>
    </div>
    <input type="hidden" name="mode" id="create-mode" value="linux">

    <div class="tab-pane active" data-tab-group="create" data-tab="linux">
      <label><span>Name</span><input type="text" name="name" pattern="[A-Za-z0-9._-]+" required placeholder="e.g. dev-sandbox"></label>
      <div class="row">
        <label><span>CPU cores</span><input type="number" name="cpu" min="1" max="32" value="4"></label>
        <label><span>RAM (GB)</span><input type="number" name="memory" min="1" max="128" value="4"></label>
      </div>
      <label><span>Disk (GB)</span><input type="number" name="disk_size" min="1" max="2000" value="20"></label>
      <p class="help">Allocates an empty disk only. To install Linux you'd boot from an ISO via VNC. For a working out-of-the-box VM, use the <strong>Pull image</strong> tab.</p>
    </div>

    <div class="tab-pane" data-tab-group="create" data-tab="pull">
      <label><span>Image</span>
        <select name="image" onchange="document.getElementById('pull-other-row').style.display = this.value === '__other__' ? 'block' : 'none'">
          <optgroup label="Linux — known working">
            <option value="ubuntu-noble-vanilla-sparse:latest" selected>Ubuntu 24.04 (sparse) — smaller, recommended</option>
            <option value="ubuntu-noble-vanilla:latest">Ubuntu 24.04 (full)</option>
          </optgroup>
          <optgroup label="macOS — broken on lume v0.3.9">
            <option value="macos-sequoia-vanilla:latest" disabled>macOS 15 Sequoia (vanilla) — blocked by PR #1395</option>
            <option value="macos-sequoia-cua:latest" disabled>macOS 15 Sequoia + cua tools — blocked by PR #1395</option>
            <option value="macos-tahoe-vanilla:latest" disabled>macOS 26 Tahoe (vanilla) — blocked by PR #1395</option>
          </optgroup>
          <option value="__other__">Other (paste registry path)…</option>
        </select>
      </label>
      <label id="pull-other-row" style="display:none"><span>Custom image (name:tag)</span>
        <input type="text" name="image_other" pattern="[A-Za-z0-9._/-]+:[A-Za-z0-9._-]+" placeholder="my-image:latest">
      </label>
      <label><span>VM name (optional)</span>
        <input type="text" name="pull_name" pattern="[A-Za-z0-9._-]*" placeholder="(auto-derived from image)">
      </label>
      <p class="help">
        Pull runs in a Terminal window so you can watch progress. Linux pulls take ~5–15 min on typical home bandwidth. macOS pulls are listed but disabled —
        lume v0.3.9 rejects the published OCI tar-part layers (<a href="https://github.com/trycua/cua/pull/1395" target="_blank">PR #1395</a> open, unmerged). For macOS today, use the <strong>macOS (IPSW)</strong> tab.
      </p>
    </div>

    <div class="tab-pane" data-tab-group="create" data-tab="macos">
      <label><span>Name</span><input type="text" name="macos_name" pattern="[A-Za-z0-9._-]+" placeholder="e.g. mac-sandbox"></label>
      <div class="row">
        <label><span>CPU cores</span><input type="number" name="macos_cpu" min="1" max="32" value="4"></label>
        <label><span>RAM (GB)</span><input type="number" name="macos_memory" min="2" max="128" value="8"></label>
      </div>
      <label><span>Disk (GB)</span><input type="number" name="macos_disk_size" min="20" max="2000" value="60"></label>
      <label><span>IPSW</span>
        <select name="macos_ipsw_mode" onchange="document.getElementById('macos-ipsw-url-row').style.display = this.value === 'custom' ? 'block' : 'none'">
          <option value="latest" selected>Latest (auto-fetched, ~15 GB)</option>
          <option value="custom">Custom URL…</option>
        </select>
      </label>
      <label id="macos-ipsw-url-row" style="display:none"><span>IPSW URL</span>
        <input type="text" name="macos_ipsw_url" placeholder="https://updates.cdn-apple.com/.../UniversalMac_*.ipsw">
      </label>
      <label><span>Setup Assistant</span>
        <select name="macos_unattended">
          <option value="none" selected>Manual (use VNC to click through Setup Assistant)</option>
          <option value="sequoia">Unattended: Sequoia preset (only matches Sequoia IPSWs)</option>
          <option value="tahoe">Unattended: Tahoe preset — currently broken on macOS 26.4.1</option>
        </select>
      </label>
      <p class="help">
        Runs in a Terminal window so you can watch the IPSW download + macOS installer (~30 min total). After it boots, use the <strong>VNC</strong> button on the VM card to click through Setup Assistant. Default username/password for unattended images: <code>lume</code> / <code>lume</code>.
      </p>
    </div>
  </div>
  <div class="dlg-foot">
    <button type="button" class="btn" onclick="closeDialog('dlg-create')">Cancel</button>
    <button type="submit" class="btn primary">Create</button>
  </div>
</form>
</dialog>
"""


def render_edit_dialog() -> str:
    return """
<dialog id="dlg-edit">
<form method="POST" action="/edit/">
  <div class="dlg-head">
    <h3>Edit</h3>
    <p>Disk size can only increase, not shrink. Other fields can change in either direction.</p>
  </div>
  <div class="dlg-body">
    <input type="hidden" name="_name">
    <p class="warn flash err" style="display:none">VM is running — settings changes typically require it to be stopped first. Lume may reject this.</p>
    <div class="row">
      <label><span>CPU cores</span><input type="number" name="cpu" min="1" max="32"></label>
      <label><span>RAM (GB)</span><input type="number" name="memory" min="1" max="128"></label>
    </div>
    <label><span>Disk (GB) — only growable</span><input type="number" name="disk_size" min="1" max="2000"></label>
    <label><span>Display (e.g. 1920x1080)</span><input type="text" name="display" pattern="\\d+x\\d+"></label>
  </div>
  <div class="dlg-foot">
    <button type="button" class="btn" onclick="closeDialog('dlg-edit')">Cancel</button>
    <button type="submit" class="btn primary">Save</button>
  </div>
</form>
</dialog>
"""


def render_clone_dialog() -> str:
    return """
<dialog id="dlg-clone">
<form method="POST" action="/clone">
  <div class="dlg-head">
    <h3>Clone</h3>
    <p>Full disk copy. May take a while for large VMs (the page will redirect when done).</p>
  </div>
  <div class="dlg-body">
    <input type="hidden" name="source">
    <label><span>New VM name</span><input type="text" name="new_name" pattern="[A-Za-z0-9._-]+" required placeholder="e.g. suparbot-copy"></label>
  </div>
  <div class="dlg-foot">
    <button type="button" class="btn" onclick="closeDialog('dlg-clone')">Cancel</button>
    <button type="submit" class="btn primary">Clone</button>
  </div>
</form>
</dialog>
"""


def render_page(vms: list[dict], flash: tuple[str, str] | None = None, err: str | None = None) -> str:
    rows = []
    vm_data_for_js: dict[str, dict] = {}

    for vm in sorted(vms, key=lambda v: (v.get("status") != "running", v.get("name", ""))):
        name = vm.get("name", "?")
        name_q = urllib.parse.quote(name, safe="")
        status = vm.get("status", "?")
        os_name = vm.get("os", "?")
        ip = vm.get("ipAddress") or "—"
        vnc = vm.get("vncUrl")
        disk = vm.get("diskSize") or {}
        used = disk.get("allocated", 0)
        total = disk.get("total", 0)
        pct = (used / total * 100) if total else 0
        cpu = vm.get("cpuCount", "?")
        mem = vm.get("memorySize", 0)
        ssh_ok = vm.get("sshAvailable") is True
        is_running = status == "running"
        running_class = "running" if is_running else "stopped"
        name_js = json.dumps(name)  # for onclick handlers

        vm_data_for_js[name] = {
            "cpuCount": cpu, "memorySize": mem,
            "diskSize": {"total": total},
            "display": vm.get("display"), "status": status,
        }

        actions: list[str] = []
        if is_running:
            if vnc:
                actions.append(f'<a class="btn primary" href="{html.escape(vnc)}">VNC</a>')
            if ssh_ok:
                actions.append(
                    f'<form method="POST" action="/ssh/{name_q}" style="display:inline">'
                    f'<button class="btn">SSH</button></form>'
                )
            actions.append(
                f'<form method="POST" action="/stop/{name_q}" style="display:inline">'
                f'<button class="btn">Stop</button></form>'
            )
        else:
            actions.append(
                f'<form method="POST" action="/start/{name_q}" style="display:inline">'
                f'<button class="btn primary">Start</button></form>'
            )
        actions.append(f'<button class="btn" onclick="openEdit({name_js})">Edit</button>')
        actions.append(f'<button class="btn" onclick="openClone({name_js})">Clone</button>')
        actions.append(
            f'<form method="POST" action="/delete/{name_q}" style="display:inline" '
            f'onsubmit="return confirmDelete({name_js})">'
            f'<button class="btn danger">Delete</button></form>'
        )

        rows.append(f"""
<article class="vm {running_class}">
  <header>
    <h2>{html.escape(name)}</h2>
    <span class="badge {running_class}">{html.escape(status)}</span>
  </header>
  <dl>
    <dt>OS</dt><dd>{html.escape(os_name)}</dd>
    <dt>IP</dt><dd>{html.escape(ip)}</dd>
    <dt>SSH</dt><dd>{'yes' if ssh_ok else 'no'}</dd>
    <dt>CPU / RAM</dt><dd>{cpu} cores · {human_bytes(mem)}</dd>
    <dt>Disk</dt><dd>
      <div class="bar"><span style="width:{pct:.1f}%"></span></div>
      <small>{human_bytes(used)} / {human_bytes(total)} ({pct:.0f}%)</small>
    </dd>
  </dl>
  <footer>{' '.join(actions)}</footer>
</article>""")

    body = "\n".join(rows) if rows else (
        '<p class="empty">No VMs found. Click <strong>+ Create VM</strong> to make one.</p>'
    )

    flash_html = ""
    if flash:
        kind, msg = flash
        flash_html = f'<div class="flash {kind}">{html.escape(msg)}</div>'
    elif err:
        flash_html = f'<div class="flash err">{html.escape(err)}</div>'

    vm_data_json = json.dumps(vm_data_for_js)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>lume VMs</title>
<style>{CSS}</style>
</head>
<body>
<div class="toolbar">
  <div>
    <h1>lume VMs</h1>
    <p class="sub">Auto-refreshing every {REFRESH_SECONDS}s · {len(vms)} VM{'s' if len(vms) != 1 else ''} · daemon at <code>{LUME}</code></p>
  </div>
  <button class="btn primary" onclick="openDialog('dlg-create')">+ Create VM</button>
</div>
{flash_html}
<section class="grid">
{body}
</section>

{render_create_dialog()}
{render_edit_dialog()}
{render_clone_dialog()}

<script>
window.__VM_DATA__ = {vm_data_json};
const REFRESH_MS = {REFRESH_SECONDS * 1000};
</script>
<script>{JS}</script>
</body>
</html>
"""


# ─── HTTP handler ───────────────────────────────────────────────────────────

def _flash_redirect(handler: BaseHTTPRequestHandler, kind: str, msg: str) -> None:
    qs = urllib.parse.urlencode({"f": kind, "m": msg[:300]})
    handler.send_response(303)
    handler.send_header("Location", f"/?{qs}")
    handler.send_header("Content-Length", "0")
    handler.end_headers()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write(f"[lume-web] {self.address_string()} {fmt % args}\n")

    def _send(self, code: int, body: bytes, content_type: str = "text/html; charset=utf-8"):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    # ---- GET ----
    def do_GET(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path

        if path in ("/", "/index.html"):
            qs = urllib.parse.parse_qs(u.query)
            flash = None
            if "f" in qs and "m" in qs:
                kind = qs["f"][0]
                if kind in ("ok", "err"):
                    flash = (kind, qs["m"][0])

            status, body = lume_request("/lume/vms")
            err = None
            vms: list[dict] = []
            if status == 200:
                try:
                    vms = json.loads(body)
                except json.JSONDecodeError as e:
                    err = f"Invalid JSON from lume daemon: {e}"
            else:
                err = f"lume daemon returned status {status}: {body[:200].decode(errors='replace')}"
            self._send(200, render_page(vms, flash, err).encode("utf-8"))
            return

        if path == "/api/vms":
            status, body = lume_request("/lume/vms")
            self._send(status or 502, body, "application/json")
            return

        self._send(404, b"Not found", "text/plain")

    # ---- POST ----
    def do_POST(self):
        u = urllib.parse.urlparse(self.path)
        path = u.path
        parts = path.strip("/").split("/")

        # /start/{name}, /stop/{name} — go via daemon API (instant 202 response)
        if len(parts) == 2 and parts[0] in ("start", "stop"):
            action = "run" if parts[0] == "start" else "stop"
            name = urllib.parse.unquote(parts[1])
            if not NAME_RE.match(name):
                _flash_redirect(self, "err", f"Invalid VM name: {name}")
                return
            name_q = urllib.parse.quote(name, safe="")
            status, _ = lume_request(f"/lume/vms/{name_q}/{action}", method="POST")
            kind = "ok" if status in (200, 202, 204) else "err"
            _flash_redirect(self, kind, f"{action} {name} → HTTP {status}")
            return

        # /ssh/{name} — open Terminal with `lume ssh <name>`
        if len(parts) == 2 and parts[0] == "ssh":
            name = urllib.parse.unquote(parts[1])
            if not NAME_RE.match(name):
                _flash_redirect(self, "err", f"Invalid VM name: {name}")
                return
            shell_cmd = f"{shlex.quote(LUME_BIN)} ssh {shlex.quote(name)}"
            ok, msg = spawn_in_terminal(shell_cmd)
            _flash_redirect(self, "ok" if ok else "err",
                            f"Opened Terminal: lume ssh {name}" if ok else f"Failed to open Terminal: {msg}")
            return

        # /delete/{name} — `lume delete <name> --force`
        if len(parts) == 2 and parts[0] == "delete":
            name = urllib.parse.unquote(parts[1])
            if not NAME_RE.match(name):
                _flash_redirect(self, "err", f"Invalid VM name: {name}")
                return
            # Drain body even if empty
            parse_form(self.rfile, self.headers)
            rc, msg = run_lume_cli(["delete", name, "--force"], timeout=120)
            _flash_redirect(self, "ok" if rc == 0 else "err",
                            f"Deleted {name}" if rc == 0 else f"Delete failed: {msg or 'rc='+str(rc)}")
            return

        # /edit/{name} — `lume set <name> --cpu N --memory NG --disk-size NG --display WxH`
        if len(parts) == 2 and parts[0] == "edit":
            name = urllib.parse.unquote(parts[1])
            if not NAME_RE.match(name):
                _flash_redirect(self, "err", f"Invalid VM name: {name}")
                return
            form = parse_form(self.rfile, self.headers)
            args = ["set", name]
            if form.get("cpu"):
                args += ["--cpu", form["cpu"]]
            if form.get("memory"):
                args += ["--memory", f"{form['memory']}GB"]
            if form.get("disk_size"):
                args += ["--disk-size", f"{form['disk_size']}GB"]
            if form.get("display"):
                if not re.match(r"^\d+x\d+$", form["display"]):
                    _flash_redirect(self, "err", "Display must be like 1920x1080")
                    return
                args += ["--display", form["display"]]
            rc, msg = run_lume_cli(args, timeout=30)
            _flash_redirect(self, "ok" if rc == 0 else "err",
                            f"Updated {name}" if rc == 0 else f"Update failed: {msg or 'rc='+str(rc)}")
            return

        # /clone — `lume clone <source> <new_name>` (sync; can be slow for big VMs)
        if path == "/clone":
            form = parse_form(self.rfile, self.headers)
            src = form.get("source", "")
            dst = form.get("new_name", "")
            if not NAME_RE.match(src) or not NAME_RE.match(dst):
                _flash_redirect(self, "err", "Invalid VM name(s)")
                return
            # Long timeout — clone copies the full disk image.
            rc, msg = run_lume_cli(["clone", src, dst], timeout=1800)
            _flash_redirect(self, "ok" if rc == 0 else "err",
                            f"Cloned {src} → {dst}" if rc == 0 else f"Clone failed: {msg or 'rc='+str(rc)}")
            return

        # /create — either Empty Linux (sync) or Pull image (Terminal)
        if path == "/create":
            form = parse_form(self.rfile, self.headers)
            mode = form.get("mode", "linux")

            if mode == "linux":
                name = form.get("name", "")
                if not NAME_RE.match(name):
                    _flash_redirect(self, "err", "Invalid VM name")
                    return
                args = ["create", name, "--os", "linux"]
                if form.get("cpu"):
                    args += ["--cpu", form["cpu"]]
                if form.get("memory"):
                    args += ["--memory", f"{form['memory']}GB"]
                if form.get("disk_size"):
                    args += ["--disk-size", f"{form['disk_size']}GB"]
                rc, msg = run_lume_cli(args, timeout=120)
                _flash_redirect(self, "ok" if rc == 0 else "err",
                                f"Created empty Linux VM: {name}" if rc == 0
                                else f"Create failed: {msg or 'rc='+str(rc)}")
                return

            if mode == "pull":
                # If user picked "Other..." use the typed value; otherwise use dropdown.
                image = form.get("image", "").strip()
                if image == "__other__":
                    image = form.get("image_other", "").strip()
                pull_name = form.get("pull_name", "").strip()
                if not IMAGE_RE.match(image):
                    _flash_redirect(self, "err", "Image must be like name:tag")
                    return
                if pull_name and not NAME_RE.match(pull_name):
                    _flash_redirect(self, "err", "Invalid VM name")
                    return
                cmd = f"{shlex.quote(LUME_BIN)} pull {shlex.quote(image)}"
                if pull_name:
                    cmd += f" {shlex.quote(pull_name)}"
                ok, msg = spawn_in_terminal(cmd)
                _flash_redirect(self, "ok" if ok else "err",
                                f"Pulling {image} in Terminal" if ok else f"Failed: {msg}")
                return

            if mode == "macos":
                name = form.get("macos_name", "")
                if not NAME_RE.match(name):
                    _flash_redirect(self, "err", "Invalid VM name")
                    return
                ipsw_mode = form.get("macos_ipsw_mode", "latest")
                if ipsw_mode == "custom":
                    ipsw = form.get("macos_ipsw_url", "").strip()
                    # Allow only http(s) URLs to a .ipsw to keep this safe.
                    if not re.match(r"^https?://[A-Za-z0-9._/+\-?=&%:]+\.ipsw$", ipsw):
                        _flash_redirect(self, "err", "IPSW URL must be an http(s) URL ending in .ipsw")
                        return
                else:
                    ipsw = "latest"
                args = ["create", name, "--os", "macOS", "--ipsw", ipsw]
                if form.get("macos_cpu"):
                    args += ["--cpu", form["macos_cpu"]]
                if form.get("macos_memory"):
                    args += ["--memory", f"{form['macos_memory']}GB"]
                if form.get("macos_disk_size"):
                    args += ["--disk-size", f"{form['macos_disk_size']}GB"]
                unattended = form.get("macos_unattended", "none")
                if unattended in ("sequoia", "tahoe"):
                    args += ["--unattended", unattended]
                # Build a shell-safe command and run in Terminal so the user
                # sees the IPSW download progress + macOS installer output.
                cmd = " ".join(shlex.quote(a) for a in [LUME_BIN, *args])
                ok, msg = spawn_in_terminal(cmd)
                _flash_redirect(self, "ok" if ok else "err",
                                f"Creating macOS VM {name} in Terminal" if ok else f"Failed: {msg}")
                return

            _flash_redirect(self, "err", f"Unknown create mode: {mode}")
            return

        self._send(404, b"Not found", "text/plain")


# ─── main ───────────────────────────────────────────────────────────────────

def main():
    p = argparse.ArgumentParser(description="Tiny local web UI for lume VMs.")
    p.add_argument("--host", default="127.0.0.1", help="Bind address (default: 127.0.0.1)")
    p.add_argument("--port", type=int, default=8080, help="Bind port (default: 8080)")
    args = p.parse_args()

    status, _ = lume_request("/lume/vms")
    if status != 200:
        sys.stderr.write(
            f"[lume-web] WARNING: lume daemon at {LUME} returned {status}. "
            "Is `lume serve` running? (`launchctl list | grep com.trycua.lume`)\n"
        )

    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    sys.stderr.write(f"[lume-web] Listening on http://{args.host}:{args.port}/\n")
    sys.stderr.write("[lume-web] Open it in a browser; Ctrl-C to stop.\n")
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        sys.stderr.write("\n[lume-web] Shutting down.\n")
        srv.server_close()


if __name__ == "__main__":
    main()
