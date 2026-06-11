# -*- coding: utf-8 -*-
import http.server, json, os, sys, socketserver, hashlib, hmac, base64, time
import urllib.request, urllib.parse, urllib.error, ssl, io, uuid, re, socketserver
from pathlib import Path

PORT = int(os.environ.get("PORT", 8093))
API_VERSION = "8.0.0"
ROOT = Path(__file__).parent
UPLOAD_DIR = ROOT / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

MEMORY_CACHE = {}
MAX_CACHE = 10
CACHE_LOCK = threading.Lock()

def cache_put(fid, data):
    with CACHE_LOCK:
        MEMORY_CACHE[fid] = data
        while len(MEMORY_CACHE) > MAX_CACHE:
            MEMORY_CACHE.pop(next(iter(MEMORY_CACHE)))

def cache_get(fid):
    with CACHE_LOCK:
        return MEMORY_CACHE.get(fid)

def safe_read(rfile, length):
    chunks = []
    remaining = length
    while remaining > 0:
        chunk = rfile.read(min(remaining, 65536))
        if not chunk: break
        chunks.append(chunk)
        remaining -= len(chunk)
    return b"".join(chunks)

# ===== OCR (科大讯飞) =====
def call_xfyun_ocr(file_bytes, filename, config):
    api_key = config.get("xf_apikey", "")
    api_secret = config.get("xf_apisecret", "")
    endpoint = config.get("xf_endpoint", "https://api.xfyun.cn/v1/service/v1/ocr/general")
    img_b64 = base64.b64encode(file_bytes).decode("utf-8")
    cur_time = str(int(time.time()))
    param = base64.b64encode(json.dumps({"language": "cn|en", "location": "false"}).encode("utf-8")).decode("utf-8")
    checksum = hashlib.md5((api_key + cur_time + param).encode("utf-8")).hexdigest()
    headers = {"X-Appid": config.get("xf_appid", ""), "X-CurTime": cur_time, "X-Param": param, "X-CheckSum": checksum, "Content-Type": "application/x-www-form-urlencoded; charset=utf-8"}
    req_data = urllib.parse.urlencode({"image": img_b64}).encode("utf-8")
    ctx = ssl.create_default_context()
    req = urllib.request.Request(endpoint, data=req_data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=30) as resp:
            result = json.loads(resp.read().decode("utf-8"))
        return parse_xfyun_result(result)
    except urllib.error.HTTPError as e:
        raise Exception(f"讯飞 OCR 失败 ({e.code}): {e.read().decode('utf-8', errors='replace')[:200]}")
    except Exception as e:
        raise Exception(f"讯飞 OCR 异常: {str(e)}")

def parse_xfyun_result(result):
    if "data" in result:
        data = result["data"]
        if isinstance(data, dict):
            if "block" in data:
                lines = []
                for block in data["block"]:
                    if isinstance(block, dict) and "line" in block:
                        for line in block["line"]:
                            if isinstance(line, dict) and "word" in line:
                                lines.append("".join(w.get("content", "") for w in line["word"]))
                return chr(10).join(lines)
            if "text" in data: return str(data["text"])
        elif isinstance(data, str): return data
    if "text" in result: return str(result["text"])
    return json.dumps(result, ensure_ascii=False)

# ===== PDF Text =====
def extract_pdf_text(file_bytes):
    from PyPDF2 import PdfReader
    reader = PdfReader(io.BytesIO(file_bytes))
    return chr(10).join([p.extract_text() or "" for p in reader.pages])

# ===== Safe Float =====
def safe_float(val, default=0.0):
    if val is None: return default
    if isinstance(val, (int, float)): return float(val)
    s = str(val).strip()
    for u in ["万元", "吨", "万kWh", "kWh", "t", "%", "万元/年", "吨/年"]:
        s = s.replace(u, "")
    s = s.replace(",", "").replace("，", "")
    try: return float(s)
    except: return default

# ===== Tag Parser =====
def parse_tagged_response(content):
    result = {"companyName": "", "projectName": "", "completionDate": "", "consultingAgency": "",
              "section3": {"projectSummary": "", "resultsSummary": ""},
              "section4": {"plans": [], "totals": {}}}
    num_fields = ["investment","economicBenefit","electricitySaving","coalSaving","co2Reduction",
                  "materialSaving","waterSaving","codcrReduction","ammoniaReduction","smokeReduction",
                  "dustReduction","so2Reduction","noxReduction","vocReduction","solidWasteReduction",
                  "liquidWasteReduction","heavyMetalReduction"]
    
    meta_m = re.search(r"---META---\s*([\s\S]*?)(?=---SEC|---\Z)", content, re.DOTALL)
    if meta_m:
        for line in meta_m.group(1).strip().split(chr(10)):
            for prefix, key in [("企业名称","companyName"),("项目名称","projectName"),("验收日期","completionDate"),("咨询机构","consultingAgency")]:
                if line.startswith(prefix+":") or line.startswith(prefix+"："):
                    result[key] = re.split(r"[:：]", line, 1)[1].strip()

    sec3_m = re.search(r"---SEC3---\s*([\s\S]*?)(?=---SEC4|---\Z)", content, re.DOTALL)
    if sec3_m:
        s3 = sec3_m.group(1)
        m = re.search(r"项目简介[:：]?\s*([\s\S]*?)(?=实施成效)", s3, re.DOTALL)
        if m: result["section3"]["projectSummary"] = m.group(1).strip()
        m = re.search(r"实施成效[:：]?\s*([\s\S]*)", s3, re.DOTALL)
        if m: result["section3"]["resultsSummary"] = m.group(1).strip()

    sec4_m = re.search(r"---SEC4---\s*([\s\S]*?)(?=\Z)", content, re.DOTALL)
    if sec4_m:
        in_data = False
        for line in sec4_m.group(1).split(chr(10)):
            line = line.strip()
            if not line: continue
            if any(s in line for s in ["表格列说明", "数据行", "方案序号", "seq"]):
                in_data = True; continue
            if not in_data: continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 5: continue
            seq = parts[0].strip()
            if not seq or seq == "0": continue
            plan = {"seq": str(seq), "name": parts[1] if len(parts)>1 else "",
                    "content": parts[2] if len(parts)>2 else "",
                    "other": parts[20] if len(parts)>20 else ""}
            field_order = num_fields
            for fi, fn in enumerate(field_order):
                idx = fi + 3
                plan[fn] = safe_float(parts[idx]) if idx < len(parts) else 0
            result["section4"]["plans"].append(plan)

    totals = {}
    for k in num_fields:
        totals[k] = sum(p.get(k, 0) for p in result["section4"]["plans"])
    result["section4"]["totals"] = totals
    # Parse CITE section
    cite_m = re.search(r"---CITE---\s*([\s\S]*?)(?=\Z)", content, re.DOTALL)
    if cite_m:
        for line in cite_m.group(1).strip().split(chr(10)):
            line = line.strip()
            if not line or "|" not in line: continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 3: continue
            seq = parts[0]
            field = parts[1]
            snippet = parts[2]
            for plan in result["section4"]["plans"]:
                if str(plan.get("seq", "")) == seq:
                    plan["citations"] = plan.get("citations", {})
                    plan["citations"][field] = snippet
                    break

    print(f"  [Parser] Extracted {len(result['section4']['plans'])} plans, company: {result['companyName']}")
    return result

# ===== DeepSeek AI =====
def call_deepseek(text, config):
    api_key = config.get("ds_apikey", "")
    endpoint = config.get("ds_endpoint", "") or "https://api.deepseek.com/v1/chat/completions"
    model = config.get("ds_model", "") or "deepseek-chat"
    # Auto-correct DeepSeek endpoint
    if "deepseek.com" in endpoint and "/chat/completions" not in endpoint:
        endpoint = endpoint.rstrip("/") + "/v1/chat/completions"
    if endpoint.endswith("/chat/completions") and "/v1/" not in endpoint:
        endpoint = endpoint.replace("/chat/completions", "/v1/chat/completions")

    system_prompt = chr(10).join([
    '是一个清洁生产验收报告数据提取助手。请从报告中提取所有清洁生产方案及绩效数据，按指定标记格式输出。IMPORTANT: 必须列出报告中EVERY方案，不要遗漏任何一个。',
    '',
    '【数据来源】：',
    '- 方案列表和详情 -> 查找"方案"相关章节，列出该章节中EVERY方案的名称和主要内容',
    '- 绩效数值 -> 查找"方案汇总表"或"绩效表"或"效益汇总"',
    '- 经济效益 -> 查找"经济效益"段落',
    '- 实施成效总结 -> 查找结论章节',
    '',
    '【提取强调】：',
    '- 必须列出ALL方案，一个不漏！',
    '- 表格中的数值直接复制，不要换算单位',
    '- 如原文"节约电量79.37万kWh"，electricitySaving填79.37',
    '',
    '【输出格式】：',
    '',
    '---META---',
    '企业名称: XXX',
    '项目名称: XXX',
    '验收日期: XXX',
    '咨询机构: XXX',
    '',
    '---SEC3---',
    '项目简介:',
    'WD01-方案名：方案主要内容（直接复制原文）',
    'ZG01-方案名：方案主要内容',
    '实施成效:',
    '复制结论章节的绩效总结段落',
    '',
    '---SEC4---',
    '表格列说明: seq | name | content | investment | economicBenefit | electricitySaving | coalSaving | co2Reduction | materialSaving | waterSaving | codcrReduction | ammoniaReduction | smokeReduction | dustReduction | so2Reduction | noxReduction | vocReduction | solidWasteReduction | liquidWasteReduction | heavyMetalReduction | other',
    '数据行（每行一个方案，用 | 分隔。数值只填数字不含单位。找不到填0。必须列出EVERY方案）:',
    'WD1 | 方案名称 | 方案内容 | 投资额 | 经济效益 | 节电量 | 折标煤 | CO2 | 原辅料 | 节水量 | COD | 氨氮 | 烟尘 | 粉尘 | SO2 | NOx | VOC | 固废 | 废液 | 重金属 | 其他  (序号请用报告原文的编号如WD1、ZG2等)',
    '',
    '---CITE---',
    '每个有数值的字段提供原文出处，格式：方案序号 | 字段名 | 原文片段',
    '示例：',
    'WD1 | investment | 项目投资320.50万元',
    'WD1 | electricitySaving | 年节电360万kWh',
    'ZG1 | codcrReduction | 减少COD排放45吨/年',
])
    body = {"model": model, "messages": [{"role": "system", "content": system_prompt},
            {"role": "user", "content": "请从以下清洁生产验收报告文本中提取信息：\n\n" + text[:120000]}],
            "temperature": 0.1, "max_tokens": 8192, "response_format": {"type": "text"}}
    req_data = json.dumps(body).encode("utf-8")
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    ctx = ssl.create_default_context()
    req = urllib.request.Request(endpoint, data=req_data, headers=headers, method="POST")
    try:
        with urllib.request.urlopen(req, context=ctx, timeout=90) as resp:
            result = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        err = e.read().decode("utf-8", errors="replace")
        if e.code == 401: raise Exception("DeepSeek 认证失败: API Key无效")
        elif e.code == 404: raise Exception(f"DeepSeek 端点不存在: {endpoint}")
        else: raise Exception(f"DeepSeek 请求失败 ({e.code}): {err[:200]}")
    except Exception as e:
        raise Exception(f"DeepSeek 请求异常: {str(e)}")

    content = result.get("choices", [{}])[0].get("message", {}).get("content", "")
    if not content: raise Exception("DeepSeek 返回内容为空")
    print(f"  [DeepSeek] Raw response length: {len(content)} chars")
    parsed = parse_tagged_response(content)
    return parsed

# ===== Word Export =====
def generate_word_html(data):
    plans = data.get("section4", {}).get("plans", [])
    totals = data.get("section4", {}).get("totals", {})
    sec3 = data.get("section3", {})
    company = data.get("companyName", "")
    t = time.localtime(); date_str = str(t.tm_year) + chr(0x5e74) + str(t.tm_mon).zfill(2) + chr(0x6708) + str(t.tm_mday).zfill(2) + chr(0x65e5)

    def fmt(v):
        try: return "{:.2f}".format(float(v or 0))
        except: return "0.00"

    lines = []
    lines.append('<html xmlns:o="urn:schemas-microsoft-com:office:office" xmlns:w="urn:schemas-microsoft-com:office:word" xmlns="http://www.w3.org/TR/REC-html40">')
    lines.append('<head><meta charset="UTF-8"><!--[if gte mso 9]><xml><w:WordDocument><w:View>Print</w:View></w:WordDocument></xml><![endif]--><style>')
    lines.append('@page{size:A4 landscape;margin:1cm}body{font-family:SimSun;font-size:9pt}')
    lines.append('h2{font-size:14pt;text-align:center;margin:8pt 0}h3{font-size:11pt;margin:6pt 0}')
    lines.append('table{border-collapse:collapse;width:100%}td,th{border:1px solid #000;padding:2pt 4pt;font-size:7pt}')
    lines.append('th{background:#f0f0f0;text-align:center}')
    lines.append('</style></head><body>')
    lines.append('<h2>清洁生产项目绩效表</h2>')
    lines.append('<p>企业名称：' + company + '</p><p>制表日期：' + date_str + '</p>')
    lines.append('<h3>三、清洁生产项目实施情况</h3>')
    lines.append('<p><b>项目简介</b></p><p style="text-indent:2em">' + sec3.get("projectSummary", "") + '</p>')
    lines.append('<p><b>实施成效</b></p><p style="text-indent:2em">' + sec3.get("resultsSummary", "") + '</p>')
    lines.append('<h3>四、清洁生产项目绩效表</h3><table>')
    hdrs = ['序号','项目名称','项目内容','投资额(万元)','经济效益(万元)','节电(万kWh)','折标煤(t)','CO2(t)','原辅料(t)','节水(t)','COD(t)','氨氮(t)','烟尘(t)','粉尘(t)','SO2(t)','NOx(t)','VOC(t)','固废(t)','废液(t)','重金属(t)','其他']
    lines.append('<tr>' + ''.join(['<th>' + h + '</th>' for h in hdrs]) + '</tr>')
    fields = ['investment','economicBenefit','electricitySaving','coalSaving','co2Reduction','materialSaving','waterSaving','codcrReduction','ammoniaReduction','smokeReduction','dustReduction','so2Reduction','noxReduction','vocReduction','solidWasteReduction','liquidWasteReduction','heavyMetalReduction']
    for i, p in enumerate(plans):
        cells = [str(p.get('seq', i+1)), p.get('name', ''), p.get('content', '')]
        cells.append(fmt(p.get('investment')))
        cells.append(fmt(p.get('economicBenefit')))
        for f in fields[2:]: cells.append(fmt(p.get(f)))
        cells.append(p.get('other', ''))
        lines.append('<tr>' + ''.join(['<td>' + c + '</td>' for c in cells]) + '</tr>')
    t_cells = ['', '', '合计', fmt(totals.get('investment')), fmt(totals.get('economicBenefit'))]
    for f in fields[2:]: t_cells.append(fmt(totals.get(f)))
    t_cells.append('')
    lines.append('<tr style="background:#e8e8e8;font-weight:bold">' + ''.join(['<td>' + c + '</td>' for c in t_cells]) + '</tr>')
    lines.append('</table></body></html>')
    return chr(10).join(lines)

# ===== Config =====
CONFIG_FILE = ROOT / "config.json"
DEFAULT_CONFIG = {
    "xf_appid": "6831d0af",
    "xf_apikey": "5863d8c31fdb0c73c036b04e13395b5e",
    "xf_apisecret": "YjExZTY4ZjIzYTExMmIyMzEzODk0ZGQ1",
    "xf_endpoint": "https://cbm01.cn-huabei-1.xf-yun.com/v1/private/se75ocrbm",
    "ds_apikey": "sk-e274f774261842f9b73700d581aeac13",
    "ds_endpoint": "https://api.deepseek.com/v1/chat/completions",
    "ds_model": "deepseek-chat"
}

def load_config():
    # Priority: environment variables > config.json > DEFAULT_CONFIG
    cfg = dict(DEFAULT_CONFIG)
    # Override from config.json
    if CONFIG_FILE.exists():
        try:
            saved = json.loads(open(CONFIG_FILE, "r", encoding="utf-8").read())
            cfg.update(saved)
        except: pass
    # Override from environment variables (highest priority)
    for key in ["xf_appid","xf_apikey","xf_apisecret","xf_endpoint","ds_apikey","ds_endpoint","ds_model"]:
        env_key = key.upper()
        if os.environ.get(env_key):
            cfg[key] = os.environ[env_key]
    return cfg

    if CONFIG_FILE.exists():
        try:
            saved = json.loads(open(CONFIG_FILE, "r", encoding="utf-8").read())
            # Merge with defaults in case of missing keys
            for k, v in DEFAULT_CONFIG.items():
                if k not in saved or not saved[k]:
                    saved[k] = v
            return saved
        except:
            return dict(DEFAULT_CONFIG)
    return dict(DEFAULT_CONFIG)
def save_config(cfg):
    merged = dict(DEFAULT_CONFIG)
    merged.update(cfg)
    for k in merged:
        if not merged[k] and k in DEFAULT_CONFIG:
            merged[k] = DEFAULT_CONFIG[k]
    open(CONFIG_FILE, "w", encoding="utf-8").write(json.dumps(merged, ensure_ascii=False, indent=2))

# ===== Multipart =====
def parse_multipart(data, boundary):
    b = boundary.encode("utf-8")
    parts = data.split(b"--" + b)
    filename = "upload.pdf"
    content = b""
    for part in parts:
        if b"Content-Disposition" in part:
            header_end = part.find(b"\\n\\n\\n\\n")
            if header_end == -1: header_end = part.find(b"\\n\\n")
            if header_end == -1: continue
            headers_raw = part[:header_end].decode("utf-8", errors="replace")
            body_start = header_end + 4
            for line in headers_raw.split("\\n\\n"):
                if "filename=" in line:
                    after = line.split("filename=")[1].strip()
                    if after.startswith('"'):
                        end_q = after.find('"', 1)
                        if end_q > 0: filename = after[1:end_q]
                    else:
                        end_s = after.find(";")
                        filename = after[:end_s].strip() if end_s >= 0 else after.strip()
                    break
            content = part[body_start:]
            if len(content) > 100: break
    if not content: content = data
    return content, filename

# ===== HTTP Server =====
class AppHandler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        if "/api/" in str(args) or "/uploads/" in str(args):
            print(f"[{time.strftime('%H:%M:%S')}] {args[0]}")

    def send_cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, Authorization")

    def serve_file(self, filepath, mime):
        try:
            with open(filepath, "rb") as f: data = f.read()
            if mime.startswith("text/html"):
                data = data.replace(b"</body>", f"<!-- v{API_VERSION} --></body>".encode())
            self.send_response(200)
            self.send_header("Content-Type", mime)
            self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
            self.send_header("Pragma", "no-cache"); self.send_header("Expires", "0")
            self.send_cors()
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except FileNotFoundError:
            self.send_error(404)

    def send_json(self, data, status=200):
        data["_v"] = API_VERSION
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_cors()
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        self.send_cors(); self.send_response(204); self.end_headers()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        if path in ("/", "/index.html"):
            self.serve_file("index.html", "text/html; charset=utf-8")
        elif path == "/api/version":
            self.send_json({"version": API_VERSION, "ts": int(time.time())})
        elif path == "/api/ocr-test":
            cfg = load_config()
            xf_ok = bool(cfg.get("xf_appid") and cfg.get("xf_apikey") and cfg.get("xf_apisecret"))
            ds_ok = bool(cfg.get("ds_apikey"))
            ds_ep = cfg.get("ds_endpoint","")
            if ds_ep and "deepseek.com" in ds_ep and "/chat/completions" not in ds_ep:
                ds_ep = ds_ep.rstrip("/") + "/v1/chat/completions"
            self.send_json({
                "ocr_configured": xf_ok,
                "ocr_appid": cfg.get("xf_appid","")[:4] + "****" if cfg.get("xf_appid") else "",
                "ocr_endpoint": cfg.get("xf_endpoint",""),
                "ds_configured": ds_ok,
                "ds_endpoint": ds_ep,
                "ds_model": cfg.get("ds_model","deepseek-chat"),
                "config_file_exists": CONFIG_FILE.exists()
            })
        elif path == "/api/config":
            cfg = load_config()
            self.send_json({"xf_appid": cfg.get("xf_appid",""), "xf_endpoint": cfg.get("xf_endpoint",""),
                           "ds_endpoint": cfg.get("ds_endpoint",""), "ds_model": cfg.get("ds_model","deepseek-chat"),
                           "has_xf_keys": bool(cfg.get("xf_apikey")), "has_ds_key": bool(cfg.get("ds_apikey"))})
        elif path.startswith("/uploads/"):
            fid = os.path.basename(path)
            data = cache_get(fid)
            if data:
                self.send_response(200)
                self.send_header("Content-Type","application/pdf")
                self.send_header("Cache-Control","max-age=3600")
                self.cors()
                self.send_header("Content-Length",str(len(data)))
                self.end_headers()
                self.wfile.write(data)
            else:
                fp = UPLOAD_DIR / os.path.basename(path)
                if fp.exists(): self.serve_file(str(fp), "application/pdf")
                else: self.send_error(404)
        else:
            fp = ROOT / path.lstrip("/")
            if fp.exists() and fp.is_file():
                mm = {".html":"text/html; charset=utf-8",".css":"text/css",".js":"application/javascript",
                      ".json":"application/json",".png":"image/png",".jpg":"image/jpeg",".svg":"image/svg+xml"}
                self.serve_file(str(fp), mm.get(fp.suffix.lower(), "application/octet-stream"))
            else: self.send_error(404)

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path
        handlers = {"/api/ocr": self.handle_ocr, "/api/ai": self.handle_ai,
                    "/api/config": self.handle_save_config, "/api/export-word": self.handle_export_word}
        if path in handlers: handlers[path]()
        else: self.send_error(404)

    def handle_ocr(self):
        try:
            ct = self.headers.get("Content-Type", "")
            if "multipart/form-data" in ct:
                boundary = ct.split("boundary=")[1].strip()
                length = int(self.headers.get("Content-Length", 0))
                raw = safe_read(self.rfile, length)
                if not raw or len(raw) < 10:
                    return self.send_json({"ok": False, "error": "未收到文件"}, 400)
                file_bytes, filename = parse_multipart(raw, boundary)
            else:
                length = int(self.headers.get("Content-Length", 0))
                file_bytes = safe_read(self.rfile, length)
                filename = "upload.pdf"
            if not file_bytes: return self.send_json({"ok": False, "error": "未收到文件"}, 400)
            ext = Path(filename).suffix.lower()
            saved = UPLOAD_DIR / f"{uuid.uuid4().hex}{ext}"
            open(saved, "wb").write(file_bytes)
            text, method = "", ""
            if ext == ".pdf":
                try: text = extract_pdf_text(file_bytes); method = "pdf_direct"
                except Exception as e: print(f"PDF error: {e}")
                if len(text.strip()) < 50:
                    cfg = load_config()
                    if cfg.get("xf_appid") and cfg.get("xf_apikey"):
                        try: text = call_xfyun_ocr(file_bytes, filename, cfg); method = "xfyun_ocr"
                        except Exception as e2: print(f"OCR error: {e2}")
            elif ext in [".jpg",".jpeg",".png",".bmp"]:
                cfg = load_config()
                if not cfg.get("xf_appid") or not cfg.get("xf_apikey"):
                    return self.send_json({"ok": False, "error": "请先配置OCR"}, 400)
                text = call_xfyun_ocr(file_bytes, filename, cfg); method = "xfyun_ocr"
            elif ext in [".doc",".docx"]:
                return self.send_json({"ok": False, "error": "Word请先导出为PDF"}, 400)
            else:
                return self.send_json({"ok": False, "error": f"不支持格式: {ext}"}, 400)
            if not text or len(text.strip()) < 10:
                return self.send_json({"ok": False, "error": "未能提取有效文本"}, 400)
            self.send_json({"ok": True, "text": text, "method": method, "fileId": saved.name})
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)}, 500)

    def handle_ai(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(safe_read(self.rfile, length))
            text = body.get("text", "")
            if not text or len(text.strip()) < 10: return self.send_json({"ok": False, "error": "文本过短"}, 400)
            cfg = load_config()
            if not cfg.get("ds_apikey"): return self.send_json({"ok": False, "error": "请先配置DeepSeek"}, 400)
            result = call_deepseek(text, cfg)
            self.send_json({"ok": True, "data": result})
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)}, 500)

    def handle_save_config(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(safe_read(self.rfile, length))
            cfg = {"xf_appid": body.get("xf_appid","").strip(), "xf_apikey": body.get("xf_apikey","").strip(),
                   "xf_apisecret": body.get("xf_apisecret","").strip(), "xf_endpoint": body.get("xf_endpoint","").strip(),
                   "ds_apikey": body.get("ds_apikey","").strip(), "ds_endpoint": body.get("ds_endpoint","").strip(),
                   "ds_model": body.get("ds_model","deepseek-chat").strip()}
            save_config(cfg)
            self.send_json({"ok": True})
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)}, 400)

    def handle_export_word(self):
        try:
            length = int(self.headers.get("Content-Length", 0))
            body = json.loads(safe_read(self.rfile, length))
            data = body.get("data", {})
            html = generate_word_html(data)
            word_bytes = html.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/msword; charset=utf-8")
            self.send_header("Content-Disposition", "attachment; filename=qingjieshengchan.doc")
            self.send_cors()
            self.send_header("Content-Length", str(len(word_bytes)))
            self.end_headers()
            self.wfile.write(word_bytes)
        except Exception as e:
            self.send_json({"ok": False, "error": str(e)}, 500)

# ===== Main =====
class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True

def main():
    os.chdir(str(ROOT))
    print("=" * 60)
    print("  清洁生产资金申报工具 v5.0")
    print(f"  http://localhost:{PORT}")
    print("=" * 60)
    ThreadedServer(("0.0.0.0", PORT), AppHandler).serve_forever()

if __name__ == "__main__":
    main()
