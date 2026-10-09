import os
import io
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException, Depends, Query
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles

import pandas as pd
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet

from sqlalchemy import create_engine, Column, Integer, String, Float, DateTime
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

# --- DATABASE SETUP ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./inventory.db")

# Render compatibility for PostgreSQL connection string
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = DATABASE_URL.replace("postgres://", "postgresql://", 1)

connect_args = {"check_same_thread": False} if "sqlite" in DATABASE_URL else {}
engine = create_engine(DATABASE_URL, connect_args=connect_args)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

class Item(Base):
    __tablename__ = "items"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True)
    category = Column(String, index=True)
    quantity = Column(Integer, default=0)
    price = Column(Float, default=0.0)      # Purchase Price
    sold = Column(Float, default=0.0)       # Selling Price
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

Base.metadata.create_all(bind=engine)

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --- APP SETUP ---
app = FastAPI(title="Telefonok Inventory")

BASE_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = BASE_DIR / "templates"
STATIC_DIR = BASE_DIR / "static"

TEMPLATES_DIR.mkdir(exist_ok=True)
STATIC_DIR.mkdir(exist_ok=True)

app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
templates = Jinja2Templates(directory=str(TEMPLATES_DIR))

# --- HELPER LOGIC ---
def adjust_quantity(qty: int, selling_price: float) -> int:
    """If quantity is 1 and selling price is present (>0), auto-set quantity to 0."""
    if qty == 1 and selling_price > 0:
        return 0
    return max(0, qty)

# --- ROUTES ---

@app.get("/", response_class=HTMLResponse)
def index(
    request: Request, 
    search: Optional[str] = None, 
    show_out_of_stock: bool = False,
    db: Session = Depends(get_db)
):
    query = db.query(Item)
    if search:
        s = f"%{search.lower()}%"
        query = query.filter(Item.name.ilike(s) | Item.category.ilike(s))
    
    all_items = db.query(Item).all()
    
    # Calculate KPIs across all items in DB
    total_items = sum(i.quantity for i in all_items)
    total_val = sum(i.quantity * i.price for i in all_items)
    total_sold = sum(i.sold for i in all_items)
    
    # Hide stock == 0 by default on the main inventory screen
    if not show_out_of_stock:
        query = query.filter(Item.quantity > 0)
        
    items = query.all()
    
    return templates.TemplateResponse(
        request=request,
        name="index.html",
        context={
            "items": items, 
            "search": search or "",
            "show_out_of_stock": show_out_of_stock,
            "total_items": total_items,
            "total_val": round(total_val, 2),
            "total_sold": round(total_sold, 2)
        }
    )

@app.post("/add")
def add_item(
    name: str = Form(...),
    category: str = Form(...),
    quantity: int = Form(...),
    price: float = Form(...),            # Purchase Price
    selling_price: float = Form(0.0),    # Selling Price
    db: Session = Depends(get_db)
):
    sp = max(0.0, selling_price)
    qty = adjust_quantity(quantity, sp)

    new_item = Item(
        name=name.strip(),
        category=category.strip(),
        quantity=qty,
        price=max(0.0, price),
        sold=sp
    )
    db.add(new_item)
    db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/update_stock/{item_id}")
def update_stock(
    item_id: int, 
    action: str = Form(...), 
    amount: int = Form(...),
    selling_price: Optional[float] = Form(None),
    db: Session = Depends(get_db)
):
    item = db.query(Item).filter(Item.id == item_id).first()
    if item:
        if action == "add":
            item.quantity += amount
        elif action == "sell":
            if amount > item.quantity:
                raise HTTPException(status_code=400, detail="Not enough stock available")
            
            if selling_price is not None and selling_price >= 0:
                item.sold = selling_price
                
            item.quantity -= amount
            item.quantity = adjust_quantity(item.quantity, item.sold)

        db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/delete/{item_id}")
def delete_item(item_id: int, db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.id == item_id).first()
    if item:
        db.delete(item)
        db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.get("/reports", response_class=HTMLResponse)
def reports_hub(
    request: Request,
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    all_items = db.query(Item).all()
    today = datetime.utcnow()

    # 1. Date Range Sales & Activity Filtering
    date_filtered_items = all_items
    if start_date:
        try:
            s_dt = datetime.strptime(start_date, "%Y-%m-%d")
            date_filtered_items = [i for i in date_filtered_items if i.updated_at >= s_dt]
        except ValueError:
            pass
    if end_date:
        try:
            e_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            date_filtered_items = [i for i in date_filtered_items if i.updated_at <= e_dt]
        except ValueError:
            pass

    # 2. Category Performance Report
    categories = {}
    for i in all_items:
        cat = i.category or "Uncategorized"
        if cat not in categories:
            categories[cat] = {"count": 0, "total_qty": 0, "purchase_val": 0.0, "sales_val": 0.0}
        categories[cat]["count"] += 1
        categories[cat]["total_qty"] += i.quantity
        categories[cat]["purchase_val"] += (i.quantity * i.price)
        categories[cat]["sales_val"] += (i.quantity * i.sold)

    cat_report = [
        {
            "category": k,
            "item_count": v["count"],
            "total_qty": v["total_qty"],
            "purchase_val": round(v["purchase_val"], 2),
            "potential_sales": round(v["sales_val"], 2),
            "potential_profit": round(v["sales_val"] - v["purchase_val"], 2)
        } for k, v in categories.items()
    ]

    # 3. Oldest Items & Aging Report
    aging_items = []
    for i in all_items:
        age_days = (today - (i.created_at or today)).days
        aging_items.append({
            "id": i.id, "name": i.name, "category": i.category,
            "quantity": i.quantity, "created_at": i.created_at.strftime("%Y-%m-%d") if i.created_at else "N/A",
            "age_days": age_days
        })
    aging_items.sort(key=lambda x: x["age_days"], reverse=True)

    # 4. Low & Out of Stock Report
    low_stock_items = [i for i in all_items if i.quantity <= 2]

    # 5. Profitability & Margin Ranking Report
    margin_items = []
    for i in all_items:
        profit = round(i.sold - i.price, 2)
        margin_pct = round((profit / i.price * 100), 1) if i.price > 0 else (100.0 if i.sold > 0 else 0.0)
        margin_items.append({
            "id": i.id, "name": i.name, "category": i.category,
            "purchase_price": i.price, "selling_price": i.sold,
            "profit": profit, "margin_pct": margin_pct
        })
    margin_items.sort(key=lambda x: x["profit"], reverse=True)

    return templates.TemplateResponse(
        request=request,
        name="reports.html",
        context={
            "start_date": start_date or "",
            "end_date": end_date or "",
            "date_items": date_filtered_items,
            "cat_report": cat_report,
            "aging_items": aging_items[:10],
            "low_stock": low_stock_items,
            "margin_items": margin_items
        }
    )

@app.get("/analytics", response_class=HTMLResponse)
def analytics(request: Request, db: Session = Depends(get_db)):
    items = db.query(Item).all()
    formatted_items = []
    for i in items:
        formatted_items.append({
            "id": i.id,
            "name": i.name,
            "category": i.category,
            "quantity": i.quantity,
            "purchase_price": i.price,
            "selling_price": i.sold,
            "profit_margin": round(i.sold - i.price, 2)
        })

    return templates.TemplateResponse(
        request=request,
        name="analytics.html",
        context={"items": formatted_items}
    )

@app.get("/export/excel")
def export_excel(
    report_type: str = Query("all"),
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    items = db.query(Item).all()

    if start_date:
        try:
            s_dt = datetime.strptime(start_date, "%Y-%m-%d")
            items = [i for i in items if i.updated_at >= s_dt]
        except ValueError:
            pass
    if end_date:
        try:
            e_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            items = [i for i in items if i.updated_at <= e_dt]
        except ValueError:
            pass

    data = [{
        "ID": i.id, "Name": i.name, "Category": i.category, 
        "Quantity": i.quantity, "Purchase Price": i.price, "Selling Price": i.sold,
        "Created Date": i.created_at.strftime("%Y-%m-%d") if i.created_at else ""
    } for i in items]
    
    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Report")
    output.seek(0)

    headers = {"Content-Disposition": f"attachment; filename=inventory_report_{report_type}.xlsx"}
    return StreamingResponse(
        output, 
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet", 
        headers=headers
    )

@app.post("/import/excel")
async def import_excel(file: UploadFile = File(...), db: Session = Depends(get_db)):
    contents = await file.read()
    try:
        df = pd.read_excel(io.BytesIO(contents), dtype=str)
        df.columns = [str(col).strip().lower().replace(" ", "_") for col in df.columns]
        df = df.fillna("")

        def parse_int(val, default=0):
            try:
                clean_str = str(val).split('.')[0].strip()
                return int(clean_str) if clean_str else default
            except (ValueError, TypeError):
                return default

        def parse_float(val, default=0.0):
            try:
                clean_str = str(val).replace("$", "").replace(",", "").strip()
                return float(clean_str) if clean_str else default
            except (ValueError, TypeError):
                return default

        for row in df.to_dict(orient="records"):
            name_val = str(row.get("name", "")).strip() or "Unnamed Item"
            category_val = str(row.get("category", "")).strip() or "General"
            
            if name_val.lower() == "nan": 
                name_val = "Unnamed Item"
            if category_val.lower() == "nan": 
                category_val = "General"

            purchase_price = parse_float(row.get("purchase_price") or row.get("price") or row.get("unit_price"))
            selling_price = parse_float(row.get("selling_price") or row.get("sold") or row.get("units_sold"))
            raw_quantity = parse_int(row.get("quantity"), default=1)

            # Rule: if qty == 1 and selling_price > 0, set stock to 0
            final_quantity = adjust_quantity(raw_quantity, selling_price)

            new_item = Item(
                name=name_val,
                category=category_val,
                quantity=final_quantity,
                price=purchase_price,
                sold=selling_price
            )
            db.add(new_item)
            
        db.commit()
        return RedirectResponse(url="/", status_code=303)
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid file format: {str(e)}")

@app.get("/export/pdf")
def export_pdf(db: Session = Depends(get_db)):
    # Export EVERY item (including stock 0)
    items = db.query(Item).all()
    buffer = io.BytesIO()
    
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    elements = []
    styles = getSampleStyleSheet()

    elements.append(