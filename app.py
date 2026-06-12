# -*- coding: utf-8 -*-
import http.server, json, os, sys, socketserver, threading, hashlib, hmac, base64, time
import urllib.request, urllib.parse, urllib.error, ssl, io, uuid, re, socketserver
from pathlib import Path

PORT = int(os.environ.get("PORT", 8093))
API_VERSION = "8.0.0"
ROOT = Path(__file__).parent
UPLOAD_DIR = ROOT / "uploads"
UPLOAD_DIR.mkdir(exist_ok=True)

MEMORY_CACHE = {}
AI_TASKS = {}
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
    try:
        ctx = ssl.create_default_context()
    except:
        ctx = ssl._create_unverified_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
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

    sec4_m = re.search(r"---SEC4---\s*([\s\S]*?)(?=---CITE|---\Z)", content, re.DOTALL)
    if sec4_m:
        s4 = sec4_m.group(1)
        # Strategy A: find data rows after header marker
        in_data = False
        for line in s4.split(chr(10)):
            line = line.strip()
            if not line: continue
            if any(s in line for s in ["表格列说明", "数据行", "方案序号", "seq", "方案汇总", "汇总表", "绩效表", "列出每个", "列顺序", "以下"]):
                in_data = True; continue
            if not in_data: continue
            parts = [p.strip() for p in line.split("|")]
            if len(parts) < 3: continue
            seq = parts[0].strip()
            if not seq: continue
            if seq.startswith("表格") or seq.startswith("列") or seq.startswith("数据"):
                continue
            plan = {"seq": str(seq), "name": parts[1] if len(parts)>1 else "",
                    "content": "", "other": parts[19] if len(parts)>19 else ""}
            for fi, fn in enumerate(num_fields):
                pidx = fi + 2
                plan[fn] = safe_float(parts[pidx]) if pidx < len(parts) else 0
            result["section4"]["plans"].append(plan)
        # Strategy B: if Strategy A found nothing, try any pipe-separated line with numbers
        if not result["section4"]["plans"]:
            print("  [Parser] Strategy A found 0 plans, trying Strategy B...")
            for line in s4.split(chr(10)):
                line = line.strip()
                if not line or "|" not in line: continue
                parts = [p.strip() for p in line.split("|")]
                if len(parts) < 3: continue
                seq = parts[0]
                if not seq or len(seq) > 8: continue
                plan = {"seq": str(seq), "name": parts[1] if len(parts)>1 else "",
                        "content": "", "other": parts[19] if len(parts)>19 else ""}
                for fi, fn in enumerate(num_fields):
                    pidx = fi + 2
                    plan[fn] = safe_float(parts[pidx]) if pidx < len(parts) else 0
                result["section4"]["plans"].append(plan)
