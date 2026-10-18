import os, json, base64, uuid, time, datetime
import xml.etree.ElementTree as ET
from email.utils import parsedate_to_datetime
import requests
from flask import Flask, request, jsonify
from dotenv import load_dotenv

load_dotenv()
KEY = os.getenv("GEMINI_API_KEY")
MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
GOV = os.getenv("DATA_GOV_KEY")
HERE = os.path.dirname(os.path.abspath(__file__))
D = os.path.join(HERE, "data")
LANG = {"en": "English", "hi": "Hindi (Devanagari script)"}

app = Flask(__name__, static_folder="static", static_url_path="")


def rd(name):
    with open(os.path.join(D, name), encoding="utf-8") as f:
        return json.load(f)


def wr(name, value):
    with open(os.path.join(D, name), "w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=1)


def gemini(contents, system, as_json=False):
    if not KEY:
        raise RuntimeError("GEMINI_API_KEY is missing in the .env file")
    body = {"system_instruction": {"parts": [{"text": system}]}, "contents": contents}
    if as_json:
        body["generationConfig"] = {"responseMimeType": "application/json"}
    models = [MODEL] + [m.strip() for m in os.getenv("GEMINI_FALLBACKS", "").split(",") if m.strip()]
    last = ""
    for model in models:
        url = "https://generativelanguage.googleapis.com/v1beta/models/%s:generateContent" % model
        for attempt in range(3):
            try:
                r = requests.post(url, headers={"x-goog-api-key": KEY}, json=body, timeout=60)
            except requests.RequestException as e:
                last = str(e)
                time.sleep(2)
                continue
            if r.status_code == 200:
                return r.json()["candidates"][0]["content"]["parts"][0]["text"]
            last = "AI error %s: %s" % (r.status_code, r.text[:200])
            if r.status_code in (429, 500, 503):
                time.sleep(2 * (attempt + 1))
                continue
            break
    raise RuntimeError("The AI is busy right now. Please try again in a few seconds. (%s)" % last)


@app.get("/")
def home():
    return app.send_static_file("index.html")


@app.post("/api/diagnose")
def diagnose():
    f = request.files.get("image")
    if not f:
        return jsonify(error="No photo received"), 400
    lang = LANG.get(request.form.get("lang"), "English")
    system = (
        "You are a careful plant disease assistant for small Indian farmers. "
        "Look at the leaf photo and the farmer's note. Reply ONLY with JSON: "
        '{"crop":str,"crop_en":str,"disease":str,"disease_en":str,"confidence":"low|medium|high","summary":str,"steps":[str,str,str],"warning":str}. '
        'If the image is not a plant or is too unclear, set disease to "Unclear" and ask for a better photo. '
        "Never give pesticide doses; tell the farmer to follow the product label and ask a local KVK or agriculture officer. "
        "crop_en and disease_en must always be short English names (e.g. Tomato, Early blight), or Healthy / Unclear. Write every other text value in %s using simple words." % lang
    )
    img = base64.b64encode(f.read()).decode()
    parts = [
        {"text": "Farmer note: " + request.form.get("note", "")},
        {"inline_data": {"mime_type": f.mimetype or "image/jpeg", "data": img}},
    ]
    try:
        text = gemini([{"role": "user", "parts": parts}], system, True)
        result = json.loads(text)
        save_report(result, request.form.get("village"), request.form.get("device"))
        return jsonify(result)
    except Exception as e:
        return jsonify(error=str(e)), 500


@app.post("/api/chat")
def chat():
    data = request.get_json(force=True)
    lang = LANG.get(data.get("lang"), "English")
    system = (
        "You are AgriExpert, a friendly farming guide for Indian farmers. Answer only farming, crop, weather, "
        "market and machinery questions. Keep answers short and simple. Do not give pesticide doses; refer to the "
        "product label and a local KVK. If unsure, say so. Reply in %s." % lang
    )
    contents = []
    for m in data.get("history", [])[-10:]:
        contents.append({"role": "user" if m["role"] == "user" else "model", "parts": [{"text": m["text"]}]})
    contents.append({"role": "user", "parts": [{"text": data.get("message", "")}]})
    try:
        return jsonify(reply=gemini(contents, system))
    except Exception as e:
        return jsonify(error=str(e)), 500



def norm(x):
    return " ".join((x or "").lower().split())


def reports():
    try:
        return rd("reports.json")
    except Exception:
        return []


def add_report(village, crop, disease, device, steps, sample=False):
    items = reports()
    items.append({"village": norm(village), "crop": norm(crop), "disease": disease, "device": device or "anon",
                  "time": datetime.datetime.utcnow().isoformat(), "steps": steps or [], "sample": sample})
    wr("reports.json", items)


def save_report(result, village, device):
    d = norm(result.get("disease_en") or result.get("disease"))
    if not norm(village) or not d or any(w in d for w in ("unclear", "healthy", "no disease")):
        return
    add_report(village, result.get("crop_en") or result.get("crop"), result.get("disease_en") or result.get("disease"),
               device, result.get("steps"))


@app.get("/api/alerts")
def alerts():
    v = norm(request.args.get("village"))
    want = norm(request.args.get("crop"))
    if not v:
        return jsonify(alerts=[], need_village=True)
    cutoff = datetime.datetime.utcnow() - datetime.timedelta(days=7)
    groups = {}
    for r in reports():
        if r["village"] != v or datetime.datetime.fromisoformat(r["time"]) < cutoff:
            continue
        g = groups.setdefault((r["crop"], norm(r["disease"])), {"devices": set(), "last": r, "sample": False})
        g["devices"].add(r["device"])
        g["sample"] = g["sample"] or r.get("sample", False)
        if r["time"] > g["last"]["time"]:
            g["last"] = r
    out = []
    for (crop, _), g in groups.items():
        n = len(g["devices"])
        if n < 2 or (want not in ("", "all") and want != crop):
            continue
        out.append({"crop": crop.title(), "disease": g["last"]["disease"], "farmers": n,
                    "level": "alert" if n >= 3 else "watch", "steps": g["last"].get("steps", []), "sample": g["sample"]})
    out.sort(key=lambda a: -a["farmers"])
    return jsonify(alerts=out)


@app.post("/api/alerts/demo")
def alerts_demo():
    d = request.get_json(force=True)
    for i in range(3):
        add_report(d.get("village", ""), "tomato", "Early blight", "demo%d" % i,
                   ["Remove infected leaves", "Avoid wetting the leaves", "Ask your KVK about a suitable fungicide"], True)
    return jsonify(ok=True)



NEWS_CACHE = {}


def parse_news(xml_text, limit=8):
    out = []
    for it in ET.fromstring(xml_text).iter("item"):
        title = (it.findtext("title") or "").strip()
        src = (it.findtext("source") or "").strip()
        if src and title.endswith(" - " + src):
            title = title[: -len(src) - 3]
        try:
            date = parsedate_to_datetime(it.findtext("pubDate")).strftime("%d %b")
        except Exception:
            date = ""
        out.append({"title": title, "link": (it.findtext("link") or "").strip(), "source": src, "date": date})
        if len(out) >= limit:
            break
    return out


@app.get("/api/news")
def news():
    hi = request.args.get("lang") == "hi"
    hit = NEWS_CACHE.get(hi)
    if hit and time.time() - hit[0] < 600:
        return jsonify(items=hit[1])
    q = "किसान OR खेती OR मंडी OR MSP when:7d" if hi else "farmers OR agriculture OR mandi OR MSP OR kisan when:7d"
    params = {"q": q, "hl": "hi" if hi else "en-IN", "gl": "IN", "ceid": "IN:hi" if hi else "IN:en"}
    try:
        r = requests.get("https://news.google.com/rss/search", params=params, timeout=15,
                         headers={"User-Agent": "Mozilla/5.0"})
        items = parse_news(r.text)
        NEWS_CACHE[hi] = (time.time(), items)
        return jsonify(items=items)
    except Exception as e:
        return jsonify(items=[], error="Could not load news: %s" % str(e)[:120])


@app.get("/api/reports")
def list_reports():
    return jsonify(reports())


@app.get("/api/crops")
def crops():
    return jsonify(rd("crops.json"))


@app.get("/api/prices")
def prices():
    crop = request.args.get("commodity", "Tomato")
    state = request.args.get("state", "Delhi")
    if GOV:
        try:
            r = requests.get(
                "https://api.data.gov.in/resource/9ef84268-d588-465a-a308-a864a43d0070",
                params={"api-key": GOV, "format": "json", "limit": 10,
                        "filters[state.keyword]": state, "filters[commodity]": crop},
                timeout=20,
            )
            rows = r.json().get("records", [])
            if rows:
                return jsonify(source="Live: Agmarknet via data.gov.in", live=True, rows=[
                    {"market": x.get("market"), "min": x.get("min_price"), "max": x.get("max_price"),
                     "modal": x.get("modal_price"), "date": x.get("arrival_date")} for x in rows])
        except Exception:
            pass
    rows = [x for x in rd("prices_sample.json") if x["commodity"].lower() == crop.lower()]
    return jsonify(source="Sample data (no live record found)", live=False, rows=rows)


@app.get("/api/machines")
def machines():
    return jsonify(rd("machines.json"))


@app.post("/api/machines")
def add_machine():
    d = request.get_json(force=True)
    items = rd("machines.json")
    items.insert(0, {"id": uuid.uuid4().hex[:8], "name": d.get("name", "Machine")[:40],
                     "price": d.get("price", 0), "place": d.get("place", "")[:40],
                     "phone": d.get("phone", "")[:15], "requests": []})
    wr("machines.json", items)
    return jsonify(ok=True)


@app.post("/api/machines/<mid>/book")
def book(mid):
    d = request.get_json(force=True)
    items = rd("machines.json")
    for m in items:
        if m["id"] == mid:
            m["requests"].append({"name": d.get("name", "")[:40], "phone": d.get("phone", "")[:15]})
    wr("machines.json", items)
    return jsonify(ok=True)


if __name__ == "__main__":
    app.run(port=5050, debug=True)