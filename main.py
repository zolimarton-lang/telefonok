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
    quantity = Column(Integer, default=0)
    purchase_date = Column(DateTime, default=datetime.utcnow)
    price = Column(Float, default=0.0)      # Purchase Price
    sold = Column(Float, default=0.0)       # Selling Price
    sold_date = Column(DateTime, nullable=True)

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
        query = query.filter(Item.name.ilike(s))
    
    all_items = db.query(Item).all()
    
    total_items = sum(i.quantity for i in all_items)
    total_val = sum(i.quantity * i.price for i in all_items)
    total_sold = sum(i.sold for i in all_items if i.sold > 0)
    
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
    quantity: int = Form(...),
    price: float = Form(...),
    selling_price: float = Form(0.0),
    db: Session = Depends(get_db)
):
    sp = max(0.0, selling_price)
    qty = adjust_quantity(quantity, sp)
    s_date = datetime.utcnow() if sp > 0 else None

    new_item = Item(
        name=name.strip(),
        quantity=qty,
        purchase_date=datetime.utcnow(),
        price=max(0.0, price),
        sold=sp,
        sold_date=s_date
    )
    db.add(new_item)
    db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/edit/{item_id}")
def edit_item(
    item_id: int,
    name: str = Form(...),
    quantity: int = Form(...),
    price: float = Form(...),
    selling_price: float = Form(...),
    purchase_date: Optional[str] = Form(None),
    sold_date: Optional[str] = Form(None),
    db: Session = Depends(get_db)
):
    item = db.query(Item).filter(Item.id == item_id).first()
    if item:
        item.name = name.strip()
        item.price = max(0.0, price)
        sp = max(0.0, selling_price)
        item.sold = sp
        item.quantity = adjust_quantity(quantity, sp)

        if purchase_date:
            try:
                item.purchase_date = datetime.strptime(purchase_date, "%Y-%m-%d")
            except ValueError:
                pass

        if sold_date:
            try:
                item.sold_date = datetime.strptime(sold_date, "%Y-%m-%d")
            except ValueError:
                pass
        elif sp > 0 and not item.sold_date:
            item.sold_date = datetime.utcnow()
        elif sp == 0:
            item.sold_date = None

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
                if selling_price > 0:
                    item.sold_date = datetime.utcnow()
                
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

    # Sales timeframe filtering using sold_date
    sold_items = [i for i in all_items if i.sold > 0]
    if start_date:
        try:
            s_dt = datetime.strptime(start_date, "%Y-%m-%d")
            sold_items = [i for i in sold_items if i.sold_date and i.sold_date >= s_dt]
        except ValueError:
            pass
    if end_date:
        try:
            e_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
            sold_items = [i for i in sold_items if i.sold_date and i.sold_date <= e_dt]
        except ValueError:
            pass

    period_total_sales = sum(i.sold for i in sold_items)
    period_total_cost = sum(i.price for i in sold_items)
    period_profit = period_total_sales - period_total_cost

    # Aging report
    aging_items = []
    for i in all_items:
        age_days = (today - (i.purchase_date or today)).days
        aging_items.append({
            "id": i.id,
            "name": i.name,
            "quantity": i.quantity,
            "purchase_date": i.purchase_date.strftime("%Y-%m-%d") if i.purchase_date else "N/A",
            "age_days": age_days
        })
    aging_items.sort(key=lambda x: x["age_days"], reverse=True)

    # Low stock
    low_stock_items = [i for i in all_items if i.quantity <= 2]

    # Margin Ranking
    margin_items = []
    for i in all_items:
        profit = round(i.sold - i.price, 2)
        margin_pct = round((profit / i.price * 100), 1) if i.price > 0 else (100.0 if i.sold > 0 else 0.0)
        margin_items.append({
            "id": i.id,
            "name": i.name,
            "purchase_price": i.price,
            "selling_price": i.sold,
            "profit": profit,
            "margin_pct": margin_pct
        })
    margin_items.sort(key=lambda x: x["profit"], reverse=True)

    return templates.TemplateResponse(
        request=request,
        name="reports.html",
        context={
            "start_date": start_date or "",
            "end_date": end_date or "",
            "sold_items": sold_items,
            "period_total_sales": round(period_total_sales, 2),
            "period_total_cost": round(period_total_cost, 2),
            "period_profit": round(period_profit, 2),
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
            "quantity": i.quantity,
            "purchase_date": i.purchase_date.strftime("%Y-%m-%d") if i.purchase_date else "N/A",
            "purchase_price": i.price,
            "selling_price": i.sold,
            "sold_date": i.sold_date.strftime("%Y-%m-%d") if i.sold_date else "N/A",
            "profit_margin": round(i.sold - i.price, 2)
        })

    return templates.TemplateResponse(
        request=request,
        name="analytics.html",
        context={"items": formatted_items}
    )

@app.get("/export/excel")
def export_excel(
    start_date: Optional[str] = Query(None),
    end_date: Optional[str] = Query(None),
    db: Session = Depends(get_db)
):
    items = db.query(Item).all()

    if start_date or end_date:
        items = [i for i in items if i.sold_date]
        if start_date:
            try:
                s_dt = datetime.strptime(start_date, "%Y-%m-%d")
                items = [i for i in items if i.sold_date >= s_dt]
            except ValueError:
                pass
        if end_date:
            try:
                e_dt = datetime.strptime(end_date, "%Y-%m-%d").replace(hour=23, minute=59, second=59)
                items = [i for i in items if i.sold_date <= e_dt]
            except ValueError:
                pass

    data = [{
        "ID": i.id,
        "Name": i.name,
        "Quantity": i.quantity,
        "Purchase Date": i.purchase_date.strftime("%Y-%m-%d") if i.purchase_date else "",
        "Purchase Price": i.price,
        "Selling Price": i.sold,
        "Selling Date": i.sold_date.strftime("%Y-%m-%d") if i.sold_date else ""
    } for i in items]
    
    df = pd.DataFrame(data)
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="Inventory Report")
    output.seek(0)

    headers = {"Content-Disposition": "attachment; filename=inventory_report.xlsx"}
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
            if name_val.lower() == "nan": 
                name_val = "Unnamed Item"

            purchase_price = parse_float(row.get("purchase_price") or row.get("price") or row.get("unit_price"))
            selling_price = parse_float(row.get("selling_price") or row.get("sold") or row.get("units_sold"))
            raw_quantity = parse_int(row.get("quantity"), default=1)

            final_quantity = adjust_quantity(raw_quantity, selling_price)
            s_date = datetime.utcnow() if selling_price > 0 else None

            new_item = Item(
                name=name_val,
                quantity=final_quantity,
                purchase_date=datetime.utcnow(),
                price=purchase_price,
                sold=selling_price,
                sold_date=s_date
            )
            db.add(new_item)
            
        db.commit()
        return RedirectResponse(url="/", status_code=303)
        
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid file format: {str(e)}")

@app.get("/export/pdf")
def export_pdf(db: Session = Depends(get_db)):
    items = db.query(Item).all()
    buffer = io.BytesIO()
    
    doc = SimpleDocTemplate(buffer, pagesize=letter)
    elements = []
    styles = getSampleStyleSheet()

    elements.append(Paragraph("Inventory Management Comprehensive Report", styles["Title"]))
    elements.append(Spacer(1, 18))

    data = [["ID", "Name", "Qty", "Pur. Date", "Pur. ($)", "Sell ($)", "Sell Date"]]
    for item in items:
        p_date = item.purchase_date.strftime("%Y-%m-%d") if item.purchase_date else "N/A"
        s_date = item.sold_date.strftime("%Y-%m-%d") if item.sold_date else "N/A"
        data.append([
            str(item.id),
            item.name,
            str(item.quantity),
            p_date,
            f"{item.price:.2f}",
            f"{item.sold:.2f}",
            s_date
        ])

    table = Table(data, colWidths=[30, 150, 40, 75, 75, 75, 75])
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#2563EB")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.whitesmoke),
        ("ALIGN", (0, 0), (-1, -1), "CENTER"),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, 0), 9),
        ("BOTTOMPADDING", (0, 0), (-1, 0), 8),
        ("BACKGROUND", (0, 1), (-1, -1), colors.HexColor("#F8FAFC")),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#E2E8F0")),
    ]))

    elements.append(table)
    doc.build(elements)
    
    buffer.seek(0)
    headers = {"Content-Disposition": "attachment; filename=inventory_report.pdf"}
    return StreamingResponse(buffer, media_type="application/pdf", headers=headers)