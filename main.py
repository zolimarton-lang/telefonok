import os
import io
from pathlib import Path
from typing import Optional
from fastapi import FastAPI, Request, Form, UploadFile, File, HTTPException, Depends
from fastapi.responses import HTMLResponse, StreamingResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from fastapi.staticfiles import StaticFiles
import pandas as pd
from reportlab.lib.pagesizes import letter
from reportlab.lib import colors
from reportlab.platypus import SimpleDocTemplate, Paragraph, Spacer, Table, TableStyle
from reportlab.lib.styles import getSampleStyleSheet

from sqlalchemy import create_engine, Column, Integer, String, Float
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import sessionmaker, Session

# --- DATABASE SETUP ---
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./inventory.db")

# Render provides 'postgres://' URLs, but SQLAlchemy requires 'postgresql://'
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
    price = Column(Float, default=0.0)
    sold = Column(Integer, default=0)

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

# --- ROUTES ---

@app.get("/", response_class=HTMLResponse)
def index(request: Request, search: Optional[str] = None, db: Session = Depends(get_db)):
    query = db.query(Item)
    if search:
        s = f"%{search.lower()}%"
        query = query.filter(Item.name.ilike(s) | Item.category.ilike(s))
    
    items = query.all()
    
    total_items = sum(i.quantity for i in items)
    total_val = sum(i.quantity * i.price for i in items)
    total_sold = sum(i.sold for i in items)
    
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
    price: float = Form(...),
    db: Session = Depends(get_db)
):
    new_item = Item(
        name=name.strip(),
        category=category.strip(),
        quantity=max(0, quantity),
        price=max(0.0, price),
        sold=0
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
            
            # If custom selling price is provided, update item unit price accordingly
            if selling_price is not None and selling_price >= 0:
                item.price = selling_price
                
            item.quantity -= amount
            item.sold += amount
        db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.post("/delete/{item_id}")
def delete_item(item_id: int, db: Session = Depends(get_db)):
    item = db.query(Item).filter(Item.id == item_id).first()
    if item:
        db.delete(item)
        db.commit()
    return RedirectResponse(url="/", status_code=303)

@app.get("/analytics", response_class=HTMLResponse)
def analytics(request: Request, db: Session = Depends(get_db)):
    items = db.query(Item).all()
    
    formatted_items = []
    for i in items:
        total_units = i.quantity + i.sold
        turnover = round((i.sold / total_units) * 100, 1) if total_units > 0 else 0.0
        revenue = round(i.sold * i.price, 2)
        formatted_items.append({
            "id": i.id,
            "name": i.name,
            "category": i.category,
            "quantity": i.quantity,
            "price": i.price,
            "sold": i.sold,
            "turnover_rate": turnover,
            "total_revenue": revenue
        })

    return templates.TemplateResponse(
        request=request,
        name="analytics.html",
        context={"items": formatted_items}
    )

@app.get("/export/excel")
def export_excel(db: Session = Depends(get_db)):
    items = db.query(Item).all()
    data = [{
        "id": i.id, "name": i.name, "category": i.category, 
        "quantity": i.quantity, "price": i.price, "sold": i.sold
    } for i in items]
    
    df = pd.DataFrame(data)
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
async def import_excel(file: UploadFile = File(...), db: Session = Depends(get_db)):
    contents = await file.read()
    try:
        # Read Excel completely as string dtypes to prevent Pandas NaN float conversion errors
        df = pd.read_excel(io.BytesIO(contents), dtype=str)
        df.columns = [str(col).strip().lower() for col in df.columns]
        
        required_cols = {"name", "category", "quantity", "price"}
        if not required_cols.issubset(set(df.columns)):
            raise HTTPException(
                status_code=400, 
                detail=f"Excel missing required columns. Found: {list(df.columns)}. Required: name, category, quantity, price"
            )

        df = df.fillna("")

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
            name_val = str(row.get("name", "")).strip() or "Unnamed Item"
            category_val = str(row.get("category", "")).strip() or "General"
            
            if name_val.lower() == "nan":
                name_val = "Unnamed Item"
            if category_val.lower() == "nan