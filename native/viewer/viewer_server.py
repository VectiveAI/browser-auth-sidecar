#!/usr/bin/env python3
"""Minimal login viewer over CDP — stdlib only.

/       HTML client (screenshot poll + keyboard/mouse capture)
/shot   jpeg via Page.captureScreenshot
/event  input dispatch (key/mouse)

Auth: ?bas_token=$BAS_CDP_TOKEN required on all routes.
"""
import base64
import json
import os
import socket
import struct
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CDP_LOCAL = int(os.environ.get("BAS_CHROME_CDP_PORT", "9222"))
HOST = os.environ.get("BAS_TAILNET_IP", "127.0.0.1")
PORT = int(os.environ.get("BAS_VIEWER_PORT", "6901"))
TOKEN = os.environ.get("BAS_CDP_TOKEN", "")


class CDPError(RuntimeError):
    pass


def _pick_page_ws():
    targets = json.load(urllib.request.urlopen(
        f"http://127.0.0.1:{CDP_LOCAL}/json/list", timeout=5))
    pages = [t for t in targets if t.get("type") == "page"]
    if not pages:
        req = urllib.request.Request(
            f"http://127.0.0.1:{CDP_LOCAL}/json/new?about:blank", method="PUT")
        pages = [json.load(urllib.request.urlopen(req, timeout=5))]
    return pages[0]["webSocketDebuggerUrl"]


class CDP:
    """Hand-rolled RFC6455 client; reconnects transparently on drop."""

    def __init__(self):
        self._next_id = 0
        self.lock = threading.Lock()
        self.s = None
        self._tail = b""
        self.connect()

    def connect(self):
        url = _pick_page_ws().replace("ws://", "")
        host, _, path = url.partition("/")
        h, _, p = host.partition(":")
        s = socket.create_connection((h, int(p or 9222)), timeout=15)
        key = base64.b64encode(os.urandom(16)).decode()
        s.sendall((f"GET /{path} HTTP/1.1\r\nHost: {h}:{p}\r\n"
                   f"Upgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Key: {key}\r\n"
                   f"Sec-WebSocket-Version: 13\r\n\r\n").encode())
        buf = b""
        while b"\r\n\r\n" not in buf:
            c = s.recv(4096)
            if not c:
                raise CDPError("handshake closed")
            buf += c
        head, _, tail = buf.partition(b"\r\n\r\n")
        if b" 101 " not in head.split(b"\r\n", 1)[0]:
            raise CDPError("handshake: " + head[:60].decode(errors="replace"))
        self.s, self._tail = s, tail

    def _rd(self, n):
        b = self._tail[:n]
        while len(b) < n:
            c = self.s.recv(n - len(b))
            if not c:
                raise CDPError("ws closed")
            b += c
        self._tail = self._tail[n:]
        return b

    def _frame(self):
        b1, b2 = self._rd(2)
        ln = b2 & 0x7F
        if ln == 126:
            ln = struct.unpack(">H", self._rd(2))[0]
        elif ln == 127:
            ln = struct.unpack(">Q", self._rd(8))[0]
        data = self._rd(ln)
        while not (b1 & 0x80):
            b1, b2 = self._rd(2)
            ln2 = b2 & 0x7F
            if ln2 == 126:
                ln2 = struct.unpack(">H", self._rd(2))[0]
            elif ln2 == 127:
                ln2 = struct.unpack(">Q", self._rd(8))[0]
            data += self._rd(ln2)
        return data

    def _tx(self, obj):
        payload = json.dumps(obj).encode()
        mask = os.urandom(4)
        n = len(payload)
        if n < 126:
            hdr = b"\x81" + bytes([0x80 | n])
        elif n < 65536:
            hdr = b"\x81\xfe" + struct.pack(">H", n)
        else:
            hdr = b"\x81\xff" + struct.pack(">Q", n)
        self.s.sendall(hdr + mask + bytes(b ^ mask[i % 4]
                                          for i, b in enumerate(payload)))

    def call(self, method, params=None, timeout=15):
        with self.lock:
            for attempt in range(2):
                try:
                    self._next_id += 1
                    mid = self._next_id
                    self._tx({"id": mid, "method": method,
                              "params": params or {}})
                    deadline = time.time() + timeout
                    while True:
                        if time.time() > deadline:
                            raise CDPError("timeout: " + method)
                        msg = json.loads(self._frame().decode())
                        if msg.get("id") == mid:
                            if "error" in msg:
                                raise CDPError(json.dumps(msg["error"]))
                            return msg.get("result", {})
                except (CDPError, OSError):
                    if attempt:
                        raise
                    time.sleep(0.3)
                    self.connect()


_cdp = None
_cdp_lock = threading.Lock()


def cdp_call(method, params=None):
    global _cdp
    with _cdp_lock:
        if _cdp is None:
            _cdp = CDP()
        return _cdp.call(method, params)


def ok_tok(query):
    return TOKEN and "bas_token=" + TOKEN in (query or "")


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _send(self, code, body, ctype):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path, _, query = self.path.partition("?")
        if path == "/":
            if not ok_tok(query):
                return self._send(401, b"missing bas_token", "text/plain")
            return self._send(200, PAGE.encode(), "text/html")
        if path == "/shot":
            if not ok_tok(query):
                return self._send(401, b"missing bas_token", "text/plain")
            try:
                r = cdp_call("Page.captureScreenshot",
                             {"format": "jpeg", "quality": 55})
                return self._send(200,
                                  base64.b64decode(r["data"]), "image/jpeg")
            except CDPError as e:
                return self._send(502, str(e).encode(), "text/plain")
        self._send(404, b"not found", "text/plain")

    def do_POST(self):
        path, _, query = self.path.partition("?")
        if path != "/event" or not ok_tok(query):
            return self._send(401, b"missing bas_token", "text/plain")
        try:
            o = json.loads(self.rfile.read(int(self.headers.get(
                "Content-Length", 0))) or b"{}")
            if o.get("t") == "key":
                p = {"type": o.get("k", "keyDown"),
                     "modifiers": int(o.get("m", 0))}
                for f in ("key", "code", "text"):
                    if o.get(f):
                        p[f] = o[f]
                cdp_call("Input.dispatchKeyEvent", p)
            elif o.get("t") == "mouse":
                p = {"type": o.get("k"), "x": o["x"], "y": o["y"]}
                if o.get("b"):
                    p["button"] = o["b"]
                if p["type"] == "mousePressed":
                    p["clickCount"] = 1
                if p["type"] == "mouseWheel":
                    p["deltaX"] = o.get("dx", 0)
                    p["deltaY"] = o.get("dy", 0)
                cdp_call("Input.dispatchMouseEvent", p)
            return self._send(200, b"ok", "text/plain")
        except Exception as e:
            return self._send(500, repr(e).encode(), "text/plain")


PAGE = """<!doctype html><meta charset=utf-8><title>browser-auth login</title>
<style>body{margin:0;background:#111;color:#eee;font:14px monospace;
display:flex;flex-direction:column;align-items:center}
h1{margin:8px;font-size:14px}
img{max-width:100vw;max-height:calc(100vh - 40px);cursor:crosshair}</style>
<h1>browser-auth login viewer — click + type here; right-drag = wheel</h1>
<img id=s alt=""><script>
const tok=new URLSearchParams(location.search).get('bas_token')||'';
const img=document.getElementById('s');
(async function loop(){try{const r=await fetch('/shot?bas_token='+tok);
if(r.ok)img.src=URL.createObjectURL(await r.blob());}catch(e){}
setTimeout(loop,400);})();
function mod(e){let m=0;if(e.shiftKey)m|=8;if(e.ctrlKey)m|=2;
if(e.altKey)m|=1;if(e.metaKey)m|=4;return m}
function send(o){fetch('/event?bas_token='+tok,
{method:'POST',body:JSON.stringify(o)}).catch(()=>{})}
addEventListener('keydown',e=>{e.preventDefault();
const k=e.key.length===1&&!e.ctrlKey&&!e.metaKey?'char':'rawKeyDown';
send({t:'key',k,key:e.key,code:e.code,text:e.key.length===1?e.key:'',
m:mod(e)});});
addEventListener('keyup',e=>{e.preventDefault();
send({t:'key',k:'keyUp',key:e.key,code:e.code,m:mod(e)});});
const pos=e=>{const r=img.getBoundingClientRect();
return{x:Math.round((e.clientX-r.left)*img.naturalWidth/r.width),
y:Math.round((e.clientY-r.top)*img.naturalHeight/r.height)}};
img.addEventListener('contextmenu',e=>e.preventDefault());
img.addEventListener('mousedown',e=>{e.preventDefault();
const p=pos(e),b=e.button===2?'right':'left';
send({t:'mouse',k:'mousePressed',x:p.x,y:p.y,b});
const mv=ev=>{const q=pos(ev);send({t:'mouse',k:'mouseMoved',x:q.x,y:q.y});};
const up=ev=>{removeEventListener('mousemove',mv);
removeEventListener('mouseup',up);const q=pos(ev);
send({t:'mouse',k:'mouseReleased',x:q.x,y:q.y,b:ev.button===2?'right':'left'});};
addEventListener('mousemove',mv);addEventListener('mouseup',up);});
img.addEventListener('wheel',e=>{e.preventDefault();const p=pos(e);
send({t:'mouse',k:'mouseWheel',x:p.x,y:p.y,dx:e.deltaX,dy:e.deltaY});},
{passive:false});
</script>"""


def main():
    if not TOKEN:
        raise SystemExit("BAS_CDP_TOKEN not set")
    srv = ThreadingHTTPServer((HOST, PORT), H)
    print(f"viewer up: http://{HOST}:{PORT}/?bas_token={TOKEN}", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
