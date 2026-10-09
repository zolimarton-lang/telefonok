import os
import json
import io
import math
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import pandas as pd
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet

app = FastAPI(title="Telefonok Inventory")

# Base directory path resolution for Render / Linux compatibility
BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

TEMPLATES_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

DATA_FILE = BASE_DIR / "inventory.json"

# --- HELPER FUNCTIONS ---

def load_data():
    if not DATA_FILE.exists():
        default_data = [
            {"id": 1, "name": "Sample Phone", "category": "General", "quantity": 10, "price": 199.99, "sold": 2}
        ]
        save_data(default_data)
        return default_data
    try:
        with open(DATA_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return []

def save_data(data):
    try:
        with open(DATA_FILE, "w") as f:
            json.dump(data, f, indent=4)
    except Exception:
        pass

def get_next_id(data):
    if not data:
        return 1
    return max(item["id"] for item in data) + 1

# --- ROUTES ---

@app.get("/", response_class=HTMLResponse)
def index(request: Request, search: Optional[str] = None):
    items = load_data()
    if search:
        items = [
            i for i in items 
            if search.lower() in i["name"].lower() or search.lower() in i["category"].lower()
        ]
    
    total_items = sum(i["quantity"] for i in items)
    total_val = sum(i["quantity"] * i["price"] for i in items)
    total_sold = sum(i["sold"] for i in items)
    
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "items": items, 
            "search": search or "",
            "total_items": total_items,
            "total_val": round(total_val, 2),
            "total_sold": total_sold
        }
    )

@app.post("/add")
def add_item(
    name: str = Form(...),
    category: str = Form(...),
    quantity: int = Form(...),
    price: float = Form(...)
):
    items = load_data()
    new_item = {
        "id": get_next_id(items),
        "name": name,
        "category": category,
        "quantity": quantity,
        "price": price,
        "sold": 0
    }
    items.append(new_item)
    save_data(items)
    return RedirectResponse(url="/", status_code=303)

@app.post("/update_stock/{item_id}")
def update_stock(
    item_id: int, 
    action: str = Form(...), 
    amount: int = Form(...)
):
    items = load_data()
    for item in items:
        if item["id"] == item_id:
            if action == "add":
                item["quantity"] += amount
            elif action == "sell":
                if amount > item["quantity"]:
                    raise HTTPException(status_code=400, detail="Not enough stock available")
                item["quantity"] -= amount
                item["sold"] += amount
            break
    save_data(items)
    return RedirectResponse(url="/", status_code=303)

@app.post("/delete/{item_id}")
def delete_item(item_id: int):
    items = load_data()
    items = [i for i in items if i["id"] != item_id]
    save_data(items)
    return RedirectResponse(url="/", status_code=303)

@app.get("/analytics", response_class=HTMLResponse)
def analytics(request: Request):
    items = load_data()
    
    for item in items:
        total_units = item["quantity"] + item["sold"]
        item["turnover_rate"] = round((item["sold"] / total_units) * 100, 1) if total_units > 0 else 0.0
        item["total_revenue"] = round(item["sold"] * item["price"], 2)

    return templates.TemplateResponse(
        request=request,
        name="analytics.html",
        context={"items": items}
    )

@app.get("/export/excel")
def export_excel():
    items = load_data()
    df = pd.DataFrame(items)
    
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Inventory")
    output.seek(0)

    headers = {"Content-Disposition": "attachment; filename=inventory_report.xlsx"}
    return StreamingResponse(
        output, 
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", 
        headers=headers
    )

@app.post("/import/excel")
async def import_excel(file: UploadFile = File(...)):
    contents = await file.read()
    try:
        # Read Excel with dtype=str to prevent Pandas from auto-inferring float NaN values
        df = pd.read_excel(io.BytesIO(contents), dtype=str)
        
        # Standardize column headers
        df.columns = [str(col).strip().lower() for col in df.columns]
        
        required_cols = {"name", "category", "quantity", "price"}
        if not required_cols.issubset(set(df.columns)):
            raise HTTPException(
                status_code=400, 
                detail=f"Excel missing required columns. Found: {list(df.columns)}. Required: name, category, quantity, price"
            )

        # Clean all empty/NaN entries at the DataFrame level
        df = df.fillna("")

        items = load_data()
        
        # String parsing helpers
        def parse_int(val, default=0):
            try:
                clean_str = str(val).split('.')[0].strip()
                return int(clean_str) if clean_str else default
            except (ValueError, TypeError):
                return default

        def parse_float(val, default=0.0):
            try:
                clean_str = str(val).strip()
                return float(clean_str) if clean_str else default
            except (ValueError, TypeError):
                return default

        for row in df.to_dict(orient="records"):
            name_val = str(row.get("name", "")).strip()
            category_val = str(row.get("category", "")).strip()
            
            if not name_val or name_val.lower() == "nan":
                name_val = "Unnamed Item"
            if not category_val or category_val.lower() == "nan":
                category_val = "General"

            new_item = {
                "id": get_next_id(items),
                "name": name_val,
                "category": category_val,
                "quantity": max(0, parse_int(row.get("quantity"), default=1)),
                "price": max(0.0, parse_float(row.get("price"), default=0.0)),
                "sold": max(0, parse_int(row.get("sold"), default=0))
            }
            items.append(new_item)
            
        save_data(items)
        return RedirectResponse(url="/", status_code=303)
        
    except HTTPException as he:
        raise he
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid file format: {str(e)}")

@app.get("/export/pdf")
def export_pdf():
    items = load_data()
    buffer = io.BytesIO()
    
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    elements = []
    styles = getSampleStyleSheet()

    elements.append(Paragraph("Inventory Management Summary Report", styles['Title']))
    elements.append(Spacer(1, 1