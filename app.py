"""
backend/app.py
--------------
Flask REST API for scaling recipe ingredients + cooking time.

• Reads an Excel workbook containing all recipes (default: recipe_data.xlsx)
• Accepts POST /adjust_ingredients with JSON:
      {
        "recipe_name": "<string>",
        "servings"   : <int>
      }
• Returns JSON with adjusted ingredient list and cooking time
• Secured with a simple Bearer‐token API key

ENV VARS expected on Render (or any host)
-----------------------------------------
API_KEY        : Secret token used in the Authorization header
RECIPE_XLSX    : Optional path/URL for the Excel file (default recipe_data.xlsx)
PORT           : Injected by Render automatically
"""
import os, re, json
from fractions import Fraction
from math import log
# from flask_cors import CORS

import pandas as pd
from flask import Flask, request, jsonify, abort, send_from_directory
from flask_cors import CORS

# ─────────────────────────────────────────────────────────────
#  Config & Data Load
# ─────────────────────────────────────────────────────────────
API_KEY       = os.getenv("API_KEY") or "abc123securetoken"   # fallback for local test
BASE_SERVINGS = 2
EXCEL_PATH = os.getenv("RECIPE_XLSX", "Recipe App Dataset.xlsx")# Excel bundled with repo

try:
    # Load every sheet into a dict of DataFrames
    xls = pd.read_excel(EXCEL_PATH, sheet_name=None)
except FileNotFoundError as e:
    raise RuntimeError(f"❌ Could not find Excel file at {EXCEL_PATH}") from e

# Ingredient scaling rules
LINEAR_INGREDIENTS = ["rice","flour","water","milk","oil","ghee","sugar",
                      "jaggery","coconut","curd"]
LOG_INGREDIENTS    = ["salt","spice","turmeric","chilli","pepper","masala"]
FIXED_INGREDIENTS  = ["cardamom","cloves","cinnamon","bay leaf",
                      "mustard","curry leaves"]

LANGUAGE_SUFFIX = {
    "TamilName":"ta", "tamilname":"ta",
    "hindiName":"hn", "malayalamName":"kl",
    "kannadaName":"kn", "teluguName":"te",
    "frenchName":"french", "spanishName":"spanish", "germanName":"german"
}

# ─────────────────────────────────────────────────────────────
#  Helper functions
# ─────────────────────────────────────────────────────────────
def to_mixed_fraction(val: float, precision: float = 1/8) -> str:
    frac = Fraction(val).limit_denominator(int(1/precision))
    whole, remainder = divmod(frac.numerator, frac.denominator)
    rem = Fraction(remainder, frac.denominator)
    if rem == 0:
        return str(whole)
    if whole == 0:
        return str(rem)
    return f"{whole} and {rem}"

def format_time(minutes) -> str:
    try:
        m = int(round(float(minutes)))
        hrs, mins = divmod(m, 60)
        return (f"{hrs} hr{'s' if hrs>1 else ''} " if hrs else "") + \
               (f"{mins} min{'s' if mins>1 else ''}" if mins else "")
    except Exception:
        return str(minutes)

def detect_row(recipe_name: str):
    """Return (sheet_name, lang_col, lang_code, row_df) or (None,...)."""
    lower = recipe_name.lower()
    for sheet, df in xls.items():
        # Multilingual columns
        for lang_col in LANGUAGE_SUFFIX:
            if lang_col in df.columns:
                hit = df[df[lang_col].astype(str).str.lower().str.strip() == lower]
                if not hit.empty:
                    return sheet, lang_col, LANGUAGE_SUFFIX[lang_col], hit
        # Fallback to 'name' column
        for col in ("name", "Name"):
            if col in df.columns:
                hit = df[df[col].astype(str).str.lower().str.strip() == lower]
                if not hit.empty:
                    return sheet, col, "en", hit
    return None, None, None, None

def parse_ingredient_line(line: str):
    """Split a CSV / newline list into dicts with qty, unit, name."""
    items = [i.strip() for i in re.split(r",|\n", str(line)) if i.strip()]
    out   = []
    for it in items:
        m = re.match(r"(?P<qty>[\d\.\/]+)?\s*(?P<unit>[a-zA-Z])\s(?P<name>.+)", it)
        if m:
            try:
                amt = eval(m.group("qty")) if m.group("qty") else 1
            except Exception:
                amt = 1
            out.append({
                "amount"         : amt,
                "unit"           : m.group("unit").strip(),
                "name"           : m.group("name").strip(),
                "formattedAmount": f"{round(amt,2)}"
            })
    return out

def scale_ingredient(item: dict, servings: int, base: int = BASE_SERVINGS) -> dict:
    name = item["name"].lower()
    qty  = item["amount"]
    if any(k in name for k in FIXED_INGREDIENTS):
        scaled = qty
    elif any(k in name for k in LOG_INGREDIENTS):
        scaled = qty * (log(servings) / log(base))
    else:
        scaled = qty * (servings / base)
    return {**item,
            "amount"         : round(scaled, 2),
            "formattedAmount": to_mixed_fraction(scaled)}

def scale_cooking_time(t, servings, base=BASE_SERVINGS):
    try:
        return round(float(t) * (servings / base))
    except Exception:
        return "N/A"

# ─────────────────────────────────────────────────────────────
#  Flask API
# ─────────────────────────────────────────────────────────────
app = Flask(__name__)
CORS(app)

def require_key(fn):
    """Decorator to enforce Bearer token auth."""
    def _wrapper(*args, **kwargs):
        token = request.headers.get("Authorization", "").replace("Bearer ", "")
        if token != API_KEY:
            abort(401, description="Unauthorized")
        return fn(*args, **kwargs)
    _wrapper.__name__ = fn.__name__
    return _wrapper

@app.route("/adjust_ingredients", methods=["POST"])
@require_key
def adjust_ingredients():
    payload = request.get_json(force=True)
    recipe  = payload.get("recipe_name", "").strip()
    servings= int(payload.get("servings", BASE_SERVINGS))

    sheet, lang_col, lang_code, df_row = detect_row(recipe)
    if df_row is None:
        return jsonify({"error": "Recipe not found"}), 404

    row = df_row.iloc[0]

    # Ingredient column discovery
    ing_col = next((c for c in row.index
                    if f"ingredients_{lang_code}" in c.lower()), None) \
              or next((c for c in row.index if "ingredients_en" in c.lower()), None)
    if ing_col is None:
        return jsonify({"error": "Ingredient column not found"}), 500

    cook_col = next((c for c in row.index
                     if c.lower() in ("cooking", "cookingtime")), None)
    original_time = row[cook_col] if cook_col else "N/A"
    new_time      = scale_cooking_time(original_time, servings)

    base_ing      = parse_ingredient_line(row[ing_col])
    adjusted_ing  = [scale_ingredient(i, servings) for i in base_ing]

    return jsonify({
        "recipe"       : recipe,
        "base_servings": BASE_SERVINGS,
        "new_servings" : servings,
        "original_time": format_time(original_time),
        "adjusted_time": format_time(new_time),
        "ingredients"  : adjusted_ing
    })

@app.route("/")
def root():
    # return "🥘 Recipe Adjuster API is live", 200
      return send_from_directory("static", "index.html")

# ─────────────────────────────────────────────────────────────
#  Entry point for Render / Gunicorn
# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
