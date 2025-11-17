from flask import Flask, render_template_string
import requests
import datetime

# ================== CONFIG ==================
LIGHTER_URL = "https://mainnet.zklighter.elliot.ai/api/v1/funding-rates"
PACIFICA_INFO_URL = "https://api.pacifica.fi/api/v1/info"
PACIFICA_FUNDING_URL = "https://api.pacifica.fi/api/v1/funding_rate/history"

MIN_ABS_DIFF = 0.0005  # lọc kèo có chênh lệch funding đủ lớn (theo decimal, trước khi *100)


app = Flask(__name__)


# ================== HÀM DÙNG CHUNG ==================

def fetch_json(url, **kwargs):
    resp = requests.get(url, timeout=15, **kwargs)
    resp.raise_for_status()
    return resp.json()


def normalize_symbol(raw):
    s = str(raw).upper()
    for ch in ["/", "_", ":"]:
        s = s.replace(ch, "-")
    base = s.split("-")[0]
    import re
    base = re.sub(r"[^A-Z0-9]", "", base)
    return base


# ---------- Lighter ----------

def extract_lighter_funding():
    """
    Parse funding Lighter theo format:
    {
        "code":200,
        "funding_rates":[
            {"market_id":78,"exchange":"binance","symbol":"PYTH","rate":-7.26e-05},
            ...
        ]
    }
    'rate' là funding cho chu kỳ 8h => chia 8 để ra funding /1h.
    """
    try:
        data = fetch_json(LIGHTER_URL)
    except Exception as e:
        print("Lỗi gọi API Lighter:", e)
        return {}

    items = data.get("funding_rates")
    if not isinstance(items, list):
        print("⚠️ API Lighter không có 'funding_rates'. Dump JSON:")
        print(str(data)[:500])
        return {}

    out = {}
    for item in items:
        try:
            sym = item.get("symbol")
            fr = item.get("rate")
            if sym is None or fr is None:
                continue
            base = normalize_symbol(sym)
            # funding /1h
            out[base] = float(fr) / 8.0
        except Exception:
            continue

    return out


# ---------- Pacifica ----------

def get_pacifica_symbols():
    try:
        data = fetch_json(PACIFICA_INFO_URL)
    except Exception as e:
        print("Lỗi gọi API Pacifica /info:", e)
        return []

    items = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        for key in ("data", "markets", "symbols"):
            v = data.get(key)
            if isinstance(v, list):
                items = v
                break
        if not items:
            for k, v in data.items():
                if isinstance(v, dict):
                    obj = dict(v)
                    obj.setdefault("symbol", k)
                    items.append(obj)

    symbols = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        sym = None
        for key in ("symbol", "name", "market", "ticker"):
            if key in item:
                sym = item[key]
                break
        if sym:
            symbols.add(str(sym))
    return sorted(symbols)


def extract_pacifica_funding():
    symbols = get_pacifica_symbols()
    if not symbols:
        return {}

    out = {}
    for sym in symbols:
        params = {"symbol": sym, "limit": 1}
        try:
            data = fetch_json(PACIFICA_FUNDING_URL, params=params)
        except Exception as e:
            print(f"Lỗi gọi funding Pacifica cho {sym}: {e}")
            continue

        rows = []
        if isinstance(data, list):
            rows = data
        elif isinstance(data, dict):
            for key in ("data", "rows", "history"):
                v = data.get(key)
                if isinstance(v, list):
                    rows = v
                    break

        if not rows:
            continue

        last = rows[0]

        # funding_rate = 1hr funding đã trả xong (khớp ô "1hr Funding" trên UI)
        fr = None
        if last.get("funding_rate") is not None:
            try:
                fr = float(last["funding_rate"])
            except Exception:
                fr = None

        # fallback: nếu không có funding_rate thì dùng next_funding_rate
        if fr is None and last.get("next_funding_rate") is not None:
            try:
                fr = float(last["next_funding_rate"])
            except Exception:
                fr = None

        if fr is None:
            continue

        base = normalize_symbol(sym)
        out[base] = fr

    return out


# ================== TEMPLATE HTML ==================

HTML_TEMPLATE = """
<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <title>Funding Arbitrage — Lighter x Pacifica</title>
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <link
    href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css"
    rel="stylesheet">
  <style>
    body { background-color:#020617; color:#e5e7eb; }
    .table thead th { position: sticky; top: 0; background:#020617; }
    .apr-high { color:#22c55e; font-weight:bold; }
    .side-short { color:#f97316; font-weight:bold; }
    .side-long { color:#38bdf8; font-weight:bold; }
    .badge-small { font-size:0.7rem; }
  </style>
</head>
<body>
<div class="container py-4">
  <h1 class="mb-2">Funding Arbitrage — Lighter x Pacifica</h1>
  <p class="text-secondary mb-1">
    Data: 1h funding hiện tại, lấy từ public API của Lighter &amp; Pacifica (không dùng API key).
  </p>
  <p class="text-secondary mb-3" style="font-size:0.9rem;">
    Thời gian quét: <b>{{ scanned_at }}</b>
  </p>

  {% if error %}
    <div class="alert alert-danger">{{ error }}</div>
  {% endif %}

  {% if rows %}
  <div class="table-responsive" style="max-height: 70vh;">
    <table class="table table-sm table-dark table-hover align-middle">
      <thead>
        <tr>
          <th>Token</th>
          <th>Funding Lighter (&#37;/1h)</th>
          <th>Funding Pacifica (&#37;/1h)</th>
          <th>Chênh lệch (&#37;/1h)</th>
          <th>APR xấp xỉ (%/năm)</th>
          <th>Lighter nên</th>
          <th>Pacifica nên</th>
        </tr>
      </thead>
      <tbody>
      {% for row in rows %}
        <tr>
          <td>{{ row.token }}</td>
          <td>{{ "%.4f"|format(row.fr_l * 100) }}</td>
          <td>{{ "%.4f"|format(row.fr_p * 100) }}</td>
          <td>{{ "%.4f"|format(row.edge * 100) }}</td>
          <td class="{% if row.apr > 80 %}apr-high{% endif %}">
            {{ "%.2f"|format(row.apr) }}
          </td>
          <td class="side-{{ row.lighter_side|lower }}">{{ row.lighter_side }}</td>
          <td class="side-{{ row.pacifica_side|lower }}">{{ row.pacifica_side }}</td>
        </tr>
      {% endfor %}
      </tbody>
    </table>
  </div>
  {% else %}
    <p>Hiện tại không có kèo nào vượt ngưỡng chênh lệch MIN_ABS_DIFF.</p>
  {% endif %}

  <hr class="border-secondary mt-4">
  <p class="text-secondary" style="font-size:0.85rem;">
    Ghi chú:<br>
    – Funding &gt; 0 thường là LONG trả funding cho SHORT (hãy xác nhận lại với UI từng sàn trước khi trade).<br>
    – APR xấp xỉ chỉ là ước lượng dựa trên funding hiện tại, dùng để so sánh tương đối giữa các kèo.<br>
    – Tool này chỉ mang tính tham khảo, bạn tự chịu trách nhiệm với mọi quyết định trade.
  </p>
</div>
</body>
</html>
"""


@app.route("/")
def index():
    lighter = extract_lighter_funding()
    pacifica = extract_pacifica_funding()

    scanned_at = datetime.datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S UTC")

    if not lighter or not pacifica:
        return render_template_string(
            HTML_TEMPLATE,
            rows=[],
            error="Không lấy được đủ dữ liệu từ cả 2 sàn (Lighter hoặc Pacifica). Thử F5 lại sau vài phút.",
            scanned_at=scanned_at,
        )

    tokens = sorted(set(lighter.keys()) & set(pacifica.keys()))
    rows = []

    for base in tokens:
        fr_l = lighter[base]
        fr_p = pacifica[base]
        diff = fr_l - fr_p
        edge = abs(diff)
        if edge < MIN_ABS_DIFF:
            continue

        if diff > 0:
            lighter_side = "SHORT"
            pacifica_side = "LONG"
        else:
            lighter_side = "LONG"
            pacifica_side = "SHORT"

        approx_apr = edge * 24 * 365 * 100  # funding /1h -> APR

        rows.append({
            "token": base,
            "fr_l": fr_l,
            "fr_p": fr_p,
            "edge": edge,
            "apr": approx_apr,
            "lighter_side": lighter_side,
            "pacifica_side": pacifica_side,
        })

    rows.sort(key=lambda r: r["edge"], reverse=True)

    return render_template_string(HTML_TEMPLATE, rows=rows, error=None, scanned_at=scanned_at)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
