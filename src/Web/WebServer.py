import base64
import json
import socket
import sys
import threading
from http.server import HTTPServer, ThreadingHTTPServer, BaseHTTPRequestHandler
from typing import Optional
from Web.WebBridge import WebBridge


INDEX_HTML_BYTES: bytes | None = None
FAVICON_BYTES: bytes | None = None


def _get_index_html_bytes() -> bytes:
    global INDEX_HTML_BYTES
    if INDEX_HTML_BYTES is None:
        INDEX_HTML_BYTES = _build_index_html().encode("utf-8")
    return INDEX_HTML_BYTES


def _get_favicon_bytes() -> bytes:
    global FAVICON_BYTES
    if FAVICON_BYTES is None:
        import pathlib
        if hasattr(sys, '_MEIPASS'):
            ico_path = pathlib.Path(sys._MEIPASS) / "icons" / "gaugeIcon.ico"
        else:
            ico_path = pathlib.Path(__file__).resolve().parent.parent / "icons" / "gaugeIcon.ico"
        try:
            FAVICON_BYTES = ico_path.read_bytes()
        except OSError:
            FAVICON_BYTES = b""
    return FAVICON_BYTES


_RESP_OK = b'{"ok":true}'
_RESP_400_INVALID_JSON = b'{"ok":false,"error":"Invalid JSON"}'
_RESP_400_INVALID_MODE = b'{"ok":false,"error":"Invalid mode"}'
_RESP_400_MISSING_SPEED = b'{"ok":false,"error":"Missing gpu_speed or cpu_speed"}'
_RESP_400_INVALID_SPEED = b'{"ok":false,"error":"Invalid speed values"}'
_RESP_404 = b'{"ok":false,"error":"Not found"}'
_RESP_401 = b'{"ok":false,"error":"Unauthorized"}'


class WebRequestHandler(BaseHTTPRequestHandler):
    bridge: WebBridge = None  # Set by server before start
    auth_enabled: bool = False
    auth_user: str = ""
    auth_pass: str = ""

    def log_message(self, format, *args):
        pass  # Suppress default HTTP logging

    def _check_access(self) -> bool:
        """Check Basic Auth. Returns False if request was rejected (response already sent)."""
        if self.auth_enabled:
            auth_header = self.headers.get("Authorization", "")
            if not auth_header.startswith("Basic "):
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="TCC-G15"')
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(_RESP_401)
                return False
            try:
                decoded = base64.b64decode(auth_header[6:]).decode("utf-8")
                user, passwd = decoded.split(":", 1)
            except Exception:
                self._send_json(401, _RESP_401)
                return False
            if user != self.auth_user or passwd != self.auth_pass:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="TCC-G15"')
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(_RESP_401)
                return False
        return True

    def _send_json(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        return json.loads(self.rfile.read(length))

    def do_OPTIONS(self) -> None:
        self._send_json(204, b"")

    def do_GET(self) -> None:
        try:
            if not self._check_access():
                return
            if self.path == "/" or self.path == "/index.html":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.end_headers()
                self.wfile.write(_get_index_html_bytes())
            elif self.path == "/api/status":
                status = self.bridge.get_status()
                self._send_json(200, json.dumps(status).encode())
            elif self.path == "/favicon.ico":
                favicon = _get_favicon_bytes()
                if favicon:
                    self.send_response(200)
                    self.send_header("Content-Type", "image/x-icon")
                    self.end_headers()
                    self.wfile.write(favicon)
                else:
                    self.send_response(404)
                    self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()
        except (ConnectionAbortedError, BrokenPipeError, OSError):
            pass

    def do_POST(self) -> None:
        print(f"[WebServer] POST {self.path} from {self.client_address}", flush=True)
        if not self._check_access():
            return
        try:
            body = self._read_body()
        except Exception as ex:
            print(f"[WebServer] _read_body failed: {ex}", flush=True)
            try:
                self._send_json(400, _RESP_400_INVALID_JSON)
            except Exception:
                pass
            return

        try:
            if self.path == "/api/mode":
                mode = body.get("mode")
                if mode not in ("Balanced", "G_Mode", "Custom"):
                    self._send_json(400, _RESP_400_INVALID_MODE)
                    return
                print(f"[WebServer] POST /api/mode -> {mode}", flush=True)
                self.bridge.set_mode(mode)
                self._send_json(200, _RESP_OK)

            elif self.path == "/api/fan":
                gpu = body.get("gpu_speed")
                cpu = body.get("cpu_speed")
                if gpu is None or cpu is None:
                    self._send_json(400, _RESP_400_MISSING_SPEED)
                    return
                try:
                    gpu = int(gpu)
                    cpu = int(cpu)
                except (ValueError, TypeError):
                    self._send_json(400, _RESP_400_INVALID_SPEED)
                    return
                print(f"[WebServer] POST /api/fan -> gpu={gpu} cpu={cpu}", flush=True)
                self.bridge.set_fan_speeds(max(0, min(120, gpu)), max(0, min(120, cpu)))
                self._send_json(200, _RESP_OK)

            else:
                self._send_json(404, _RESP_404)
        except (ConnectionAbortedError, BrokenPipeError, OSError) as ex:
            print(f"[WebServer] write failed: {ex}", flush=True)
        except Exception as ex:
            print(f"[WebServer] unexpected error: {type(ex).__name__}: {ex}", flush=True)


class QuietHTTPServer(ThreadingHTTPServer):
    def handle_error(self, request, client_address):
        exc_type = sys.exc_info()[0]
        exc_val = sys.exc_info()[1]
        if exc_type in (ConnectionAbortedError, BrokenPipeError, ConnectionResetError):
            return  # silently ignore client disconnects
        print(f"[WebServer] handle_error: {exc_type.__name__}: {exc_val}")
        super().handle_error(request, client_address)


_cached_lan_ip: Optional[str] = None


class ThreadedHTTPServer(threading.Thread):
    def __init__(self, bridge: WebBridge, port: int = 8080,
                 bind_addr: str = "0.0.0.0",
                 auth_enabled: bool = False,
                 auth_user: str = "",
                 auth_pass: str = "") -> None:
        super().__init__(daemon=True)
        self.bridge = bridge
        self.port = port
        self.bind_addr = bind_addr
        self.auth_enabled = auth_enabled
        self.auth_user = auth_user
        self.auth_pass = auth_pass
        self._server: Optional[HTTPServer] = None
        self._started_event = threading.Event()
        self._stop_event = threading.Event()

    def wait_until_ready(self, timeout: float = 5.0) -> bool:
        return self._started_event.wait(timeout)

    def stop(self) -> None:
        self._stop_event.set()
        if self._server:
            self._server.shutdown()

    def run(self) -> None:
        handler_class = type("Handler", (WebRequestHandler,), {
            "bridge": self.bridge,
            "auth_enabled": self.auth_enabled,
            "auth_user": self.auth_user,
            "auth_pass": self.auth_pass,
        })
        try:
            self._server = QuietHTTPServer((self.bind_addr, self.port), handler_class)
        except OSError as e:
            print(f"WebServer: failed to start on {self.bind_addr}:{self.port}: {e}")
            return
        self._started_event.set()
        print(f"WebServer: listening on http://{self.bind_addr}:{self.port}")
        self._server.serve_forever()
        print("WebServer: stopped")

    @staticmethod
    def get_lan_ip() -> str:
        global _cached_lan_ip
        if _cached_lan_ip is not None:
            return _cached_lan_ip
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
                s.connect(("8.8.8.8", 80))
                _cached_lan_ip = s.getsockname()[0]
            return _cached_lan_ip
        except Exception:
            return "127.0.0.1"


def _build_index_html() -> str:
    return r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>TCC-G15 Web Monitor</title>
<link rel="icon" href="/favicon.ico" type="image/x-icon">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4/dist/chart.umd.min.js"></script>
<style>
*{margin:0;padding:0;box-sizing:border-box}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Roboto,sans-serif;background:#1a1a1a;color:#ddd;min-height:100vh}
.header{display:flex;align-items:center;justify-content:space-between;padding:12px 20px;background:#252525;border-bottom:1px solid #333}
.header h1{font-size:16px;font-weight:600}
.status-dot{width:10px;height:10px;border-radius:50%;display:inline-block;margin-right:8px}
.status-dot.on{background:#34a853}
.status-dot.off{background:#f44336}
.container{max-width:900px;margin:0 auto;padding:16px}
.cards{display:grid;grid-template-columns:1fr 1fr;gap:16px;margin-bottom:16px}
@media(max-width:600px){.cards{grid-template-columns:1fr}}
.card{background:#252525;border-radius:8px;padding:16px;border:1px solid #333}
.card h2{font-size:14px;color:#aaa;margin-bottom:12px;text-transform:uppercase;letter-spacing:1px}
.metric{margin-bottom:10px}
.metric-label{font-size:12px;color:#888;margin-bottom:4px}
.metric-bar{height:24px;background:#333;border-radius:4px;overflow:hidden;position:relative}
.metric-fill{height:100%;border-radius:4px;transition:width .3s}
.metric-value{position:absolute;right:8px;top:50%;transform:translateY(-50%);font-size:13px;font-weight:600;color:#fff;text-shadow:0 1px 2px rgba(0,0,0,.5)}
.color-green{background:linear-gradient(90deg,#34a853,#4caf50)}
.color-yellow{background:linear-gradient(90deg,#f1c232,#ffc107)}
.color-red{background:linear-gradient(90deg,#f44336,#e53935)}
.controls{background:#252525;border-radius:8px;padding:16px;border:1px solid #333;margin-bottom:16px}
.controls h2{font-size:14px;color:#aaa;margin-bottom:12px;text-transform:uppercase;letter-spacing:1px}
.mode-btns{display:flex;gap:8px;margin-bottom:16px;flex-wrap:wrap}
.mode-btn{padding:8px 20px;border:1px solid #555;border-radius:6px;background:#333;color:#ddd;cursor:pointer;font-size:14px;transition:all .2s}
.mode-btn:hover{background:#444}
.mode-btn.active{background:#1A6497;border-color:#1A6497;color:#fff}
.fan-control{margin-top:12px}
.fan-row{display:flex;align-items:center;gap:12px;margin-bottom:8px}
.fan-row label{min-width:90px;font-size:13px;color:#aaa}
.fan-row input[type=range]{flex:1;accent-color:#1A6497}
.fan-row .fan-val{min-width:50px;text-align:right;font-size:13px;font-weight:600}
.fan-row.disabled{opacity:.4;pointer-events:none}
.chart-card{background:#252525;border-radius:8px;padding:16px;border:1px solid #333;margin-bottom:16px}
.chart-card h2{font-size:14px;color:#aaa;margin-bottom:8px;text-transform:uppercase;letter-spacing:1px;display:flex;justify-content:space-between;align-items:center}
.chart-btns{display:flex;gap:4px}
.chart-btn{padding:4px 10px;border:1px solid #444;border-radius:4px;background:#333;color:#aaa;cursor:pointer;font-size:11px}
.chart-btn.active{background:#1A6497;border-color:#1A6497;color:#fff}
.chart-wrap{height:200px;position:relative}
.footer{text-align:center;padding:12px;color:#555;font-size:12px}
</style>
</head>
<body>
<div class="header">
  <h1>TCC-G15 Web Monitor</h1>
  <div><span class="status-dot off" id="statusDot"></span><span id="statusText" style="font-size:13px;color:#888">Connecting...</span></div>
</div>
<div class="container">
  <div class="cards">
    <div class="card">
      <h2>GPU</h2>
      <div class="metric">
        <div class="metric-label">Temperature</div>
        <div class="metric-bar"><div class="metric-fill color-green" id="gpuTempBar" style="width:0%"></div><div class="metric-value" id="gpuTempVal">-- °C</div></div>
      </div>
      <div class="metric">
        <div class="metric-label">Fan Speed</div>
        <div class="metric-bar"><div class="metric-fill color-green" id="gpuRpmBar" style="width:0%"></div><div class="metric-value" id="gpuRpmVal">-- RPM</div></div>
      </div>
    </div>
    <div class="card">
      <h2>CPU</h2>
      <div class="metric">
        <div class="metric-label">Temperature</div>
        <div class="metric-bar"><div class="metric-fill color-green" id="cpuTempBar" style="width:0%"></div><div class="metric-value" id="cpuTempVal">-- °C</div></div>
      </div>
      <div class="metric">
        <div class="metric-label">Fan Speed</div>
        <div class="metric-bar"><div class="metric-fill color-green" id="cpuRpmBar" style="width:0%"></div><div class="metric-value" id="cpuRpmVal">-- RPM</div></div>
      </div>
    </div>
  </div>

  <div class="controls">
    <h2>Controls</h2>
    <div class="mode-btns">
      <button class="mode-btn" data-mode="Balanced" onclick="setMode('Balanced')">Balanced</button>
      <button class="mode-btn" data-mode="G_Mode" onclick="setMode('G_Mode')">G-Mode</button>
      <button class="mode-btn" data-mode="Custom" onclick="setMode('Custom')">Custom</button>
    </div>
    <div class="fan-control">
      <div class="fan-row disabled" id="gpuFanRow">
        <label>GPU Fan</label>
        <input type="range" min="0" max="120" value="0" id="gpuFanSlider" oninput="onFanSlider()">
        <span class="fan-val" id="gpuFanVal">0</span>
      </div>
      <div class="fan-row disabled" id="cpuFanRow">
        <label>CPU Fan</label>
        <input type="range" min="0" max="120" value="0" id="cpuFanSlider" oninput="onFanSlider()">
        <span class="fan-val" id="cpuFanVal">0</span>
      </div>
    </div>
  </div>

  <div class="chart-card">
    <h2>Temperature History <div class="chart-btns"><button class="chart-btn active" data-range="3600" onclick="setChartRange(3600,this)">1H</button><button class="chart-btn" data-range="21600" onclick="setChartRange(21600,this)">6H</button><button class="chart-btn" data-range="86400" onclick="setChartRange(86400,this)">24H</button></div></h2>
    <div class="chart-wrap"><canvas id="tempChart"></canvas></div>
  </div>

  <div class="footer">TCC-G15 Web Monitor &mdash; <a href="https://github.com/AlexIII/tcc-g15" style="color:#1A6497">github.com/AlexIII/tcc-g15</a></div>
</div>

<script>
// --- IndexedDB ---
const DB_NAME='TCC_G15_History',DB_STORE='temps',DB_VER=1;
let db=null;
function openDB(){return new Promise(r=>{const rq=indexedDB.open(DB_NAME,DB_VER);rq.onupgradeneeded=e=>{const d=e.target.result;if(!d.objectStoreNames.contains(DB_STORE))d.createObjectStore(DB_STORE,{keyPath:'ts'})};rq.onsuccess=e=>{db=e.target.result;r(db)};rq.onerror=()=>r(null)})}
async function dbAdd(ts,gpu,cpu){if(!db)return;const tx=db.transaction(DB_STORE,'readwrite');tx.objectStore(DB_STORE).put({ts,gpu,cpu})}
async function dbGetRange(since){if(!db)return[];return new Promise(r=>{const tx=db.transaction(DB_STORE,'readonly');const rq=tx.objectStore(DB_STORE).getAll(IDBKeyRange.lowerBound(since));rq.onsuccess=()=>r(rq.result);rq.onerror=()=>r([])})}
async function dbClearOld(maxAgeMs){if(!db)return;const cutoff=Date.now()-maxAgeMs;const tx=db.transaction(DB_STORE,'readwrite');const store=tx.objectStore(DB_STORE);const rq=store.openCursor();rq.onsuccess=e=>{const c=e.target.result;if(!c)return;if(c.key<cutoff)c.delete().onsuccess=()=>c.continue();else c.continue()}}
openDB();

// --- Chart ---
const ctx=document.getElementById('tempChart').getContext('2d');
const chartData={labels:[],datasets:[{label:'GPU',data:[],borderColor:'#f44336',backgroundColor:'rgba(244,67,54,.1)',pointRadius:0,borderWidth:2,tension:.3},{label:'CPU',data:[],borderColor:'#ffc107',backgroundColor:'rgba(255,193,7,.1)',pointRadius:0,borderWidth:2,tension:.3}]};
const chart=new Chart(ctx,{type:'line',data:chartData,options:{responsive:true,maintainAspectRatio:false,animation:{duration:0},scales:{x:{display:true,ticks:{maxTicksLimit:8,color:'#666',font:{size:10}},grid:{color:'#333'}},y:{min:0,max:110,ticks:{color:'#666',font:{size:10}},grid:{color:'#333'}}},plugins:{legend:{labels:{color:'#aaa',font:{size:11}}}}}});
let chartRange=3600;
function setChartRange(s,btn){chartRange=s;document.querySelectorAll('.chart-btn').forEach(b=>b.classList.remove('active'));btn.classList.add('active');updateChart()}

async function updateChart(){
  const since=Date.now()-chartRange*1000;
  const data=await dbGetRange(since);
  chart.data.labels=data.map(d=>{const dt=new Date(d.ts);return dt.getHours().toString().padStart(2,'0')+':'+dt.getMinutes().toString().padStart(2,'0')});
  chart.data.datasets[0].data=data.map(d=>d.gpu);
  chart.data.datasets[1].data=data.map(d=>d.cpu);
  chart.update();
}

// --- Polling ---
let pollCount=0;
setInterval(async()=>{
  try{
    const res=await fetch('/api/status');
    if(!res.ok)throw new Error();
    const d=await res.json();
    document.getElementById('statusDot').className='status-dot on';
    document.getElementById('statusText').textContent='Connected';
    updateUI(d);
    dbAdd(Date.now(),d.gpu_temp,d.cpu_temp);
    if(++pollCount%5===0){dbClearOld(86400000*2);updateChart()}
  }catch{
    document.getElementById('statusDot').className='status-dot off';
    document.getElementById('statusText').textContent='Disconnected';
  }
},1000);

function updateUI(d){
  const gpuTemp=d.gpu_temp,cpuTemp=d.cpu_temp,gpuRpm=d.gpu_rpm,cpuRpm=d.cpu_rpm;
  setBar('gpuTempBar','gpuTempVal',gpuTemp,100,'°C',[72,85]);
  setBar('cpuTempBar','cpuTempVal',cpuTemp,110,'°C',[85,95]);
  setBar('gpuRpmBar','gpuRpmVal',gpuRpm,5500,' RPM',null);
  setBar('cpuRpmBar','cpuRpmVal',cpuRpm,5500,' RPM',null);
  document.querySelectorAll('.mode-btn').forEach(b=>{b.classList.toggle('active',b.dataset.mode===d.mode)});
  const isCustom=d.mode==='Custom';
  document.getElementById('gpuFanRow').className='fan-row'+(isCustom?'':' disabled');
  document.getElementById('cpuFanRow').className='fan-row'+(isCustom?'':' disabled');
  if(isCustom&&!fanTimer){
    if(d.gpu_fan_speed!==null){document.getElementById('gpuFanSlider').value=d.gpu_fan_speed;document.getElementById('gpuFanVal').textContent=d.gpu_fan_speed}
    if(d.cpu_fan_speed!==null){document.getElementById('cpuFanSlider').value=d.cpu_fan_speed;document.getElementById('cpuFanVal').textContent=d.cpu_fan_speed}
  }
}

function setBar(barId,valId,value,max,unit,limits){
  const pct=Math.max(0,Math.min(100,value/max*100));
  document.getElementById(barId).style.width=pct+'%';
  const el=document.getElementById(barId);
  if(limits){
    if(value>=limits[1])el.className='metric-fill color-red';
    else if(value>=limits[0])el.className='metric-fill color-yellow';
    else el.className='metric-fill color-green';
  }
  document.getElementById(valId).textContent=(value!==null?value:'--')+unit;
}

function setMode(mode){
  document.querySelectorAll('.mode-btn').forEach(b=>{b.classList.toggle('active',b.dataset.mode===mode)});
  const isCustom=mode==='Custom';
  document.getElementById('gpuFanRow').className='fan-row'+(isCustom?'':' disabled');
  document.getElementById('cpuFanRow').className='fan-row'+(isCustom?'':' disabled');
  fetch('/api/mode',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({mode})}).catch(()=>{});
}
let fanTimer=null;
function onFanSlider(){
  document.getElementById('gpuFanVal').textContent=document.getElementById('gpuFanSlider').value;
  document.getElementById('cpuFanVal').textContent=document.getElementById('cpuFanSlider').value;
  clearTimeout(fanTimer);
  fanTimer=setTimeout(()=>{
    fetch('/api/fan',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({gpu_speed:+document.getElementById('gpuFanSlider').value,cpu_speed:+document.getElementById('cpuFanSlider').value})}).catch(()=>{})
  },500);
}

</script>
</body>
</html>"""
