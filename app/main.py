import asyncio
import os
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import func, inspect, or_, text
from sqlalchemy.orm import Session, joinedload

from app.auth import admin_user, create_token, current_user, hash_password, verify_password
from app.database import Base, engine, get_db
from app.models import (
    Computer,
    DailyClosing,
    DailyReport,
    Debtor,
    DebtorTransaction,
    Expense,
    Income,
    Product,
    ProductSale,
    Session,
    SessionProduct,
    User,
)
from app.schemas import (
    AdminStatisticsOut,
    ComputerOut,
    DailyClosingOut,
    DailyReportCreate,
    DailyReportOut,
    DebtorCreate,
    DebtorOut,
    DebtorPayment,
    ExpenseCreate,      
    ExpenseOut,        
    IncomeCreate,
    IncomeOut,
    IncomeUpdate,
    LoginIn,
    ProductCreate,
    ProductOut,
    ProductSaleCreate,
    ProductSaleOut,
    ProductUpdate,
    ProfileUpdate,
    SessionActiveOut,
    SessionComplete,
    SessionStart,
    UploadOut,
    UserCreate,
    UserOut,
    UserStatisticsOut,
    UserUpdate,
    DebtorTransactionOut,
    DebtorUpdate,
)

try:
    from dotenv import load_dotenv

    load_dotenv()
except ModuleNotFoundError:
    pass

UPLOAD_DIR = Path("uploads")
UPLOAD_DIR.mkdir(exist_ok=True)

app = FastAPI(title="GameClub Finance API", version="1.0.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=[os.getenv("FRONTEND_URL", "*"), "*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
app.mount("/uploads", StaticFiles(directory=str(UPLOAD_DIR)), name="uploads")


main_loop = None

def ensure_column_exists(db, table_name: str, column_name: str, column_definition: str):
    inspector = inspect(db.get_bind())
    existing_columns = [col["name"] for col in inspector.get_columns(table_name)]
    if column_name not in existing_columns:
        db.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_definition}"))
        db.commit()


@app.on_event("startup")
async def startup():
    global main_loop
    main_loop = asyncio.get_running_loop()
    Base.metadata.create_all(bind=engine)
    db = next(get_db())
    try:
        ensure_column_exists(db, "debtors", "is_active", "is_active BOOLEAN NOT NULL DEFAULT 1")
        ensure_column_exists(db, "users", "is_active", "is_active BOOLEAN NOT NULL DEFAULT 1")
        ensure_column_exists(db, "computers", "is_active", "is_active BOOLEAN NOT NULL DEFAULT 0")

        if not db.query(User).filter(User.username == "admin").first():
            db.add(User(full_name="Administrator", username="admin", password_hash=hash_password("admin123"), role="admin"))
            db.commit()

        if not db.query(Computer).first():
            computers = [Computer(number=i, type="computer") for i in range(1, 13)]
            computers.append(Computer(number=13, type="playstation"))
            db.add_all(computers)
            db.commit()
    finally:
        db.close()


def day_bounds(day: date):
    start = datetime.combine(day, time(4, 0, 0))
    end = datetime.combine(day + timedelta(days=1), time(3, 59, 59, 999999))
    return start, end


def month_bounds(month: str):
    year, mon = [int(part) for part in month.split("-")]
    start = datetime(year, mon, 1)
    end = datetime(year + (mon == 12), 1 if mon == 12 else mon + 1, 1)
    return start, end


def money(value):
    return float(value or Decimal("0"))


active_connections: set[WebSocket] = set()


async def broadcast_event(event: str, payload: dict):
    if not active_connections:
        return
    message = {"event": event, "payload": payload}
    to_remove: list[WebSocket] = []
    for websocket in list(active_connections):
        try:
            await websocket.send_json(message)
        except Exception:
            to_remove.append(websocket)
    for websocket in to_remove:
        active_connections.discard(websocket)


def notify_update(event: str, payload: dict):
    if main_loop and main_loop.is_running():
        asyncio.run_coroutine_threadsafe(broadcast_event(event, payload), main_loop)


def stats_query(db: Session, start: datetime, end: datetime, user_id: int | None = None):
    income_filters = [Income.created_at >= start, Income.created_at < end]
    expense_filters = [Expense.created_at >= start, Expense.created_at < end]
    session_filters = [Session.completed_at >= start, Session.completed_at < end, Session.status == "completed"]
    sale_filters = [ProductSale.created_at >= start, ProductSale.created_at < end]
    if user_id:
        income_filters.append(Income.user_id == user_id)
        session_filters.append(Session.user_id == user_id)
        sale_filters.append(ProductSale.user_id == user_id)
    total_income = db.query(func.coalesce(func.sum(Income.amount), 0)).filter(*income_filters).scalar() or Decimal("0")
    total_session_income = db.query(func.coalesce(func.sum(Session.total_amount), 0)).filter(*session_filters).scalar() or Decimal("0")
    total_sale_income = db.query(func.coalesce(func.sum(ProductSale.total_amount), 0)).filter(*sale_filters).scalar() or Decimal("0")
    total_expense = db.query(func.coalesce(func.sum(Expense.amount), 0)).filter(*expense_filters).scalar() or Decimal("0")

    payment_totals = {
        "cash": Decimal("0"),
        "card": Decimal("0"),
        "debt": Decimal("0"),
    }
    for key, value in db.query(Income.payment_type, func.coalesce(func.sum(Income.amount), 0)).filter(*income_filters).group_by(Income.payment_type).all():
        payment_totals[key] += value
    session_payments = db.query(
        func.coalesce(func.sum(Session.payment_cash), 0),
        func.coalesce(func.sum(Session.payment_card), 0),
        func.coalesce(func.sum(Session.payment_debt), 0),
    ).filter(*session_filters).one()
    payment_totals["cash"] += session_payments[0]
    payment_totals["card"] += session_payments[1]
    payment_totals["debt"] += session_payments[2]
    sale_payments = db.query(
        func.coalesce(func.sum(ProductSale.payment_cash), 0),
        func.coalesce(func.sum(ProductSale.payment_card), 0),
        func.coalesce(func.sum(ProductSale.payment_debt), 0),
    ).filter(*sale_filters).one()
    payment_totals["cash"] += sale_payments[0]
    payment_totals["card"] += sale_payments[1]
    payment_totals["debt"] += sale_payments[2]

    category_totals = {}
    for key, value in db.query(Income.category, func.coalesce(func.sum(Income.amount), 0)).filter(*income_filters).group_by(Income.category).all():
        category_totals[key] = category_totals.get(key, Decimal("0")) + value
    for key, value in db.query(Session.category, func.coalesce(func.sum(Session.computer_amount), 0)).filter(*session_filters).group_by(Session.category).all():
        category_totals[key] = category_totals.get(key, Decimal("0")) + value
    products_total = db.query(func.coalesce(func.sum(ProductSale.total_amount), 0)).filter(*sale_filters).scalar() or Decimal("0")
    if products_total:
        category_totals["products"] = category_totals.get("products", Decimal("0")) + products_total

    income_count = db.query(func.count(Income.id)).filter(*income_filters).scalar() or 0
    session_count = db.query(func.count(Session.id)).filter(*session_filters).scalar() or 0
    sale_count = db.query(func.count(ProductSale.id)).filter(*sale_filters).scalar() or 0
    records_count = income_count + session_count + sale_count

    products_sold = db.query(func.coalesce(func.sum(ProductSale.quantity), 0)).filter(*sale_filters).scalar() or 0
    total_discount = db.query(func.coalesce(func.sum(Session.discount), 0)).filter(*session_filters).scalar() or Decimal("0")

    user_ids = set()
    user_ids.update([row[0] for row in db.query(func.distinct(Income.user_id)).filter(*income_filters).all()])
    user_ids.update([row[0] for row in db.query(func.distinct(Session.user_id)).filter(*session_filters).all()])
    user_ids.update([row[0] for row in db.query(func.distinct(ProductSale.user_id)).filter(*sale_filters).all()])
    users_count = len(user_ids)

    by_user = {}
    for name, value in db.query(User.full_name, func.coalesce(func.sum(Income.amount), 0)).join(Income, Income.user_id == User.id).filter(*income_filters).group_by(User.id).all():
        by_user[name] = by_user.get(name, Decimal("0")) + value
    for name, value in db.query(User.full_name, func.coalesce(func.sum(Session.total_amount), 0)).join(Session, Session.user_id == User.id).filter(*session_filters).group_by(User.id).all():
        by_user[name] = by_user.get(name, Decimal("0")) + value
    for name, value in db.query(User.full_name, func.coalesce(func.sum(ProductSale.total_amount), 0)).join(ProductSale, ProductSale.user_id == User.id).filter(*sale_filters).group_by(User.id).all():
        by_user[name] = by_user.get(name, Decimal("0")) + value

    return {
        "total_income": money(total_income + total_session_income + total_sale_income),
        "total_revenue": money(total_income + total_session_income + total_sale_income),
        "total_expense": money(total_expense),
        "net_profit": money(total_income + total_session_income + total_sale_income - total_expense),
        "total_cash": money(payment_totals["cash"]),
        "total_card": money(payment_totals["card"]),
        "total_debt": money(payment_totals["debt"]),
        "records_count": records_count,
        "users_count": users_count,
        "sessions_count": session_count,
        "products_sold": int(products_sold),
        "total_discount": money(total_discount),
        "payment_totals": {key: money(value) for key, value in payment_totals.items()},
        "category_totals": {key: money(value) for key, value in category_totals.items()},
        "user_totals": [{"full_name": name, "amount": money(value)} for name, value in by_user.items()],
    }


@app.websocket("/ws/updates")
async def websocket_updates(websocket: WebSocket):
    await websocket.accept()
    active_connections.add(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        active_connections.discard(websocket)


@app.get("/")
def root():
    return {"message": "GameClub backend API ishlayapti"}


@app.post("/api/auth/login")
def login(payload: LoginIn, db: Session = Depends(get_db)):
    user = db.query(User).filter(User.username == payload.username).first()
    if not user or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=401, detail="Login yoki parol xato")
    if not user.is_active:
        raise HTTPException(status_code=403, detail="User bloklangan")
    return {"access_token": create_token(user), "token_type": "bearer", "user": UserOut.model_validate(user)}


@app.get("/api/auth/me", response_model=UserOut)
def me(user: User = Depends(current_user)):
    return user


@app.get("/api/users", response_model=list[UserOut])
def users(_: User = Depends(admin_user), db: Session = Depends(get_db)):
    return db.query(User).order_by(User.id.desc()).all()


@app.post("/api/users", response_model=UserOut)
def create_user(payload: UserCreate, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    if db.query(User).filter(User.username == payload.username).first():
        raise HTTPException(status_code=400, detail="Username band")
    user = User(**payload.model_dump(exclude={"password"}), password_hash=hash_password(payload.password))
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@app.put("/api/users/{user_id}", response_model=UserOut)
def update_user(user_id: int, payload: UserUpdate, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User topilmadi")
    for key, value in payload.model_dump(exclude_unset=True).items():
        if key == "password":
            user.password_hash = hash_password(value)
        else:
            setattr(user, key, value)
    db.commit()
    db.refresh(user)
    return user


@app.patch("/api/users/me", response_model=UserOut)
def update_profile(payload: ProfileUpdate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if not verify_password(payload.current_password, user.password_hash):
        raise HTTPException(status_code=400, detail="Joriy parol noto'g'ri")
    if payload.new_username:
        if db.query(User).filter(User.username == payload.new_username, User.id != user.id).first():
            raise HTTPException(status_code=400, detail="Username allaqachon olingan")
        user.username = payload.new_username
    if payload.new_password:
        user.password_hash = hash_password(payload.new_password)
    db.commit()
    db.refresh(user)
    return user


@app.patch("/api/users/{user_id}/status", response_model=UserOut)
def toggle_user(user_id: int, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User topilmadi")
    user.is_active = not user.is_active
    db.commit()
    db.refresh(user)
    return user


@app.delete("/api/users/{user_id}")
def delete_user(user_id: int, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    user = db.get(User, user_id)
    if not user:
        raise HTTPException(status_code=404, detail="User topilmadi")
    db.delete(user)
    db.commit()
    notify_update('user_deleted', {'user_id': user_id})
    return {"detail": "O'chirildi"}


@app.post("/api/incomes", response_model=IncomeOut)
def create_income(payload: IncomeCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    income = Income(user_id=user.id, **payload.model_dump())
    db.add(income)
    db.commit()
    db.refresh(income)
    return db.query(Income).options(joinedload(Income.user)).filter(Income.id == income.id).first()


@app.get("/api/incomes/my", response_model=list[IncomeOut])
def my_incomes(month: str | None = Query(None), user: User = Depends(current_user), db: Session = Depends(get_db)):
    query = db.query(Income).options(joinedload(Income.user)).filter(Income.user_id == user.id)
    if month:
        start, end = month_bounds(month)
        query = query.filter(Income.created_at >= start, Income.created_at < end)
    return query.order_by(Income.created_at.desc()).all()


@app.get("/api/incomes", response_model=list[IncomeOut])
def all_incomes(
    date_filter: date | None = Query(None, alias="date"),
    user_id: int | None = None,
    category: str | None = None,
    payment_type: str | None = None,
    _: User = Depends(admin_user),
    db: Session = Depends(get_db),
):
    query = db.query(Income).options(joinedload(Income.user))
    if date_filter:
        start, end = day_bounds(date_filter)
        query = query.filter(Income.created_at >= start, Income.created_at <= end)
    if user_id:
        query = query.filter(Income.user_id == user_id)
    if category:
        query = query.filter(Income.category == category)
    if payment_type:
        query = query.filter(Income.payment_type == payment_type)
    return query.order_by(Income.created_at.desc()).limit(500).all()


@app.put("/api/incomes/{income_id}", response_model=IncomeOut)
def update_income(income_id: int, payload: IncomeUpdate, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    income = db.get(Income, income_id)
    if not income:
        raise HTTPException(status_code=404, detail="Daromad topilmadi")
    for key, value in payload.model_dump(exclude_unset=True).items():
        setattr(income, key, value)
    db.commit()
    return db.query(Income).options(joinedload(Income.user)).filter(Income.id == income.id).first()


@app.delete("/api/incomes/{income_id}")
def delete_income(income_id: int, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    income = db.get(Income, income_id)
    if not income:
        raise HTTPException(status_code=404, detail="Daromad topilmadi")
    db.delete(income)
    db.commit()
    return {"detail": "O'chirildi"}


@app.post("/api/daily-closings", response_model=DailyClosingOut)
def create_closing(
    total_amount: Decimal = Form(...),
    comment: str | None = Form(None),
    image: UploadFile = File(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if image.content_type not in {"image/jpeg", "image/png", "image/jpg"}:
        raise HTTPException(status_code=400, detail="Faqat jpg, jpeg, png ruxsat")
    content = image.file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Rasm 5MB dan katta")
    suffix = Path(image.filename or "image.png").suffix.lower() or ".png"
    filename = f"{uuid4().hex}{suffix}"
    (UPLOAD_DIR / filename).write_bytes(content)
    closing = DailyClosing(user_id=user.id, total_amount=total_amount, comment=comment, image_url=f"/uploads/{filename}")
    db.add(closing)
    db.commit()
    db.refresh(closing)
    return db.query(DailyClosing).options(joinedload(DailyClosing.user)).filter(DailyClosing.id == closing.id).first()


@app.get("/api/daily-closings/my", response_model=list[DailyClosingOut])
def my_closings(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.query(DailyClosing).options(joinedload(DailyClosing.user)).filter(DailyClosing.user_id == user.id).order_by(DailyClosing.created_at.desc()).all()


@app.get("/api/daily-closings", response_model=list[DailyClosingOut])
def closings(_: User = Depends(admin_user), db: Session = Depends(get_db)):
    return db.query(DailyClosing).options(joinedload(DailyClosing.user)).order_by(DailyClosing.created_at.desc()).limit(200).all()


@app.post("/api/expenses", response_model=ExpenseOut)
def create_expense(payload: ExpenseCreate, admin: User = Depends(admin_user), db: Session = Depends(get_db)):
    expense = Expense(admin_id=admin.id, **payload.model_dump())
    db.add(expense)
    db.commit()
    db.refresh(expense)
    notify_update('expense_created', {'expense_id': expense.id})
    return expense


@app.get("/api/expenses", response_model=list[ExpenseOut])
def expenses(_: User = Depends(admin_user), db: Session = Depends(get_db)):
    return db.query(Expense).order_by(Expense.created_at.desc()).all()


@app.delete("/api/expenses/{expense_id}")
def delete_expense(expense_id: int, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    expense = db.get(Expense, expense_id)
    if not expense:
        raise HTTPException(status_code=404, detail="Xarajat topilmadi")
    db.delete(expense)
    db.commit()
    notify_update('expense_deleted', {'expense_id': expense_id})
    return {"detail": "O'chirildi"}


@app.get("/api/statistics/daily")
def daily_statistics(date_filter: date = Query(default_factory=date.today, alias="date"), _: User = Depends(admin_user), db: Session = Depends(get_db)):
    start, end = day_bounds(date_filter)
    return {"date": date_filter.isoformat(), **stats_query(db, start, end)}


@app.get("/api/statistics/monthly")
def monthly_statistics(month: str = Query(...), _: User = Depends(admin_user), db: Session = Depends(get_db)):
    start, end = month_bounds(month)
    return {"month": month, **stats_query(db, start, end)}


@app.get("/api/statistics/user/{user_id}/monthly")
def user_monthly_statistics(user_id: int, month: str = Query(...), user: User = Depends(current_user), db: Session = Depends(get_db)):
    if user.role != "admin" and user.id != user_id:
        raise HTTPException(status_code=403, detail="Ruxsat yo'q")
    start, end = month_bounds(month)
    return {"month": month, "user_id": user_id, **stats_query(db, start, end, user_id=user_id)}


@app.get("/api/bot/daily-report")
def bot_daily_report(x_bot_api_key: str | None = Header(None), db: Session = Depends(get_db)):
    if x_bot_api_key != os.getenv("BOT_API_KEY", "change-bot-secret"):
        raise HTTPException(status_code=403, detail="Bot API key xato")
    today = date.today()
    start, end = day_bounds(today)
    stats = stats_query(db, start, end)
    return {
        "date": today.isoformat(),
        "cashTotal": stats["payment_totals"].get("cash", 0),
        "cardTotal": stats["payment_totals"].get("card", 0),
        "debtTotal": stats["payment_totals"].get("debt", 0),
        "productsTotal": stats["category_totals"].get("products", 0),
        "recordsCount": stats["records_count"],
        "usersCount": stats["users_count"],
        "totalIncome": stats["total_income"],
        "totalExpense": stats["total_expense"],
        "netProfit": stats["net_profit"],
    }


@app.get("/api/computers", response_model=list[ComputerOut])
def fetch_computers(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.query(Computer).order_by(Computer.number).all()


@app.get("/api/computers/{computer_id}/active-session", response_model=SessionActiveOut)
def fetch_active_session(computer_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = (
        db.query(Session)
        .filter(Session.computer_id == computer_id, Session.status == "active")
        .order_by(Session.started_at.desc())
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Faol sessiya topilmadi")
    return SessionActiveOut(
        session_id=session.id,
        computer_id=session.computer_id,
        started_at=session.started_at,
        computer_price=session.computer_price,
        products_amount=session.products_amount,
        discount=session.discount,
        total_amount=session.total_amount,
        status=session.status,
        payment_cash=session.payment_cash,
        payment_card=session.payment_card,
        payment_debt=session.payment_debt,
        debtor_id=session.debtor_id,
    )


@app.post("/api/sessions")
def start_session(payload: SessionStart, user: User = Depends(current_user), db: Session = Depends(get_db)):
    computer = db.get(Computer, payload.computer_id)
    if not computer:
        raise HTTPException(status_code=404, detail="Kompyuter topilmadi")
    if computer.is_active:
        raise HTTPException(status_code=400, detail="Kompyuter allaqachon band")
    products_amount = sum(item.quantity * item.price for item in payload.products)
    total_amount = max(Decimal("0"), payload.computer_price + products_amount - payload.discount)
    category = "playstation" if computer.type == "playstation" else "computer"
    session = Session(
        user_id=user.id,
        computer_id=computer.id,
        computer_price=payload.computer_price,
        computer_amount=payload.computer_price,
        products_amount=products_amount,
        discount=payload.discount,
        total_amount=total_amount,
        payment_cash=Decimal("0"),
        payment_card=Decimal("0"),
        payment_debt=Decimal("0"),
        status="active",
        category=category,
    )
    computer.is_active = True
    db.add(session)
    db.flush()
    for item in payload.products:
        db.add(
            SessionProduct(
                session_id=session.id,
                product_id=item.product_id,
                quantity=item.quantity,
                price=item.price,
            )
        )
    db.commit()
    notify_update('session_started', {'session_id': session.id, 'computer_id': computer.id})
    return {"detail": "Sessiya boshlandi"}


@app.post("/api/sessions/{session_id}/save")
def save_session(session_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = db.get(Session, session_id)
    if not session or session.status != "active":
        raise HTTPException(status_code=404, detail="Faol sessiya topilmadi")
    if session.user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Ruxsat yo'q")
    session.updated_at = datetime.utcnow()
    db.commit()
    notify_update('session_saved', {'session_id': session.id, 'computer_id': session.computer_id})
    return {"detail": "Sessiya saqlandi"}


@app.post("/api/sessions/{session_id}/complete")
def complete_session(session_id: int, payload: SessionComplete, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = db.get(Session, session_id)
    if not session or session.status != "active":
        raise HTTPException(status_code=404, detail="Faol sessiya topilmadi")
    if payload.computer_price is not None:
        session.computer_price = payload.computer_price
    if payload.discount is not None:
        session.discount = payload.discount
    session.total_amount = max(Decimal("0"), session.computer_price + session.products_amount - session.discount)
    if payload.payment_cash + payload.payment_card + payload.payment_debt != session.total_amount:
        raise HTTPException(status_code=400, detail="Toʻlov jami sessiya summasiga teng boʻlishi kerak")
    if payload.payment_debt > 0 and not payload.debtor_id:
        raise HTTPException(status_code=400, detail="Qarzdor tanlanishi kerak")
    session.payment_cash = payload.payment_cash
    session.payment_card = payload.payment_card
    session.payment_debt = payload.payment_debt
    session.debtor_id = payload.debtor_id
    session.status = "completed"
    session.completed_at = datetime.utcnow()
    session.updated_at = datetime.utcnow()
    session.computer.is_active = False
    if payload.payment_debt > 0 and payload.debtor_id:
        debtor = db.get(Debtor, payload.debtor_id)
        if not debtor:
            raise HTTPException(status_code=404, detail="Qarzdor topilmadi")
        debtor.total_debt += payload.payment_debt
        debtor.last_payment_at = datetime.utcnow()
        db.add(
            DebtorTransaction(
                debtor_id=debtor.id,
                session_id=session.id,
                amount=payload.payment_debt,
                payment_cash=Decimal("0"),
                payment_card=Decimal("0"),
            )
        )
    db.commit()
    notify_update('session_completed', {'session_id': session.id, 'computer_id': session.computer_id})
    return {"detail": "Sessiya yakunlandi"}


@app.get("/api/products", response_model=list[ProductOut])
def fetch_products(db: Session = Depends(get_db)):
    # Only return products with available stock.
    return db.query(Product).filter(Product.quantity > 0).order_by(Product.name).all()


@app.post("/api/products", response_model=ProductOut)
def create_product(payload: ProductCreate, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    data = payload.model_dump()
    product = Product(name=data['name'], price=data['price'], quantity=data.get('quantity', 0), created_by_id=_.id if _ else None)
    db.add(product)
    db.commit()
    db.refresh(product)
    notify_update('product_created', {'product_id': product.id})
    return product


@app.put("/api/products/{product_id}", response_model=ProductOut)
def update_product(product_id: int, payload: ProductUpdate, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Mahsulot topilmadi")
    allowed = {"name", "price", "quantity"}
    for key, value in payload.model_dump(exclude_unset=True).items():
        if key in allowed:
            setattr(product, key, value)
    db.commit()
    db.refresh(product)
    notify_update('product_updated', {'product_id': product.id})
    return product


@app.delete("/api/products/{product_id}")
def delete_product(product_id: int, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    product = db.get(Product, product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Mahsulot topilmadi")
    # Avoid hard-deleting products that may be referenced by sessions/sales.
    # Instead mark as out-of-stock by setting quantity to 0.
    product.quantity = 0
    db.commit()
    return {"detail": "Mahsulot o'chirildi (soni 0 ga o'zgartirildi)"}


@app.get("/api/debtors", response_model=list[DebtorOut])
def list_debtors(search: str | None = Query(None), db: Session = Depends(get_db)):
    query = db.query(Debtor).filter(Debtor.is_active == True)
    if search:
        term = f"%{search.lower()}%"
        query = query.filter(
            or_(
                func.lower(Debtor.first_name).like(term),
                func.lower(func.coalesce(Debtor.last_name, "")).like(term),
                func.lower(Debtor.phone).like(term),
            )
        )
    return query.order_by(Debtor.created_at.desc()).limit(200).all()


@app.get("/api/debtors/{debtor_id}", response_model=DebtorOut)
def get_debtor(debtor_id: int, db: Session = Depends(get_db)):
    debtor = db.get(Debtor, debtor_id)
    if not debtor:
        raise HTTPException(status_code=404, detail="Qarzdor topilmadi")
    return debtor


@app.get("/api/debtors/{debtor_id}/history", response_model=list[DebtorTransactionOut])
def debtor_history(debtor_id: int, db: Session = Depends(get_db)):
    debtor = db.get(Debtor, debtor_id)
    if not debtor:
        raise HTTPException(status_code=404, detail="Qarzdor topilmadi")
    return db.query(DebtorTransaction).filter(DebtorTransaction.debtor_id == debtor_id).order_by(DebtorTransaction.created_at.desc()).all()


@app.get("/api/admin/debtors", response_model=list[DebtorOut])
def fetch_all_debtors(_: User = Depends(admin_user), db: Session = Depends(get_db)):
    return db.query(Debtor).order_by(Debtor.created_at.desc()).limit(500).all()


@app.post("/api/debtors", response_model=DebtorOut)
def create_debtor(payload: DebtorCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    existing = db.query(Debtor).filter(
        func.lower(Debtor.first_name) == payload.first_name.lower(),
        func.lower(func.coalesce(Debtor.last_name, "")) == (payload.last_name or "").lower(),
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="This debtor name already exists")
    if db.query(Debtor).filter(Debtor.phone == payload.phone).first():
        raise HTTPException(status_code=400, detail="This debtor phone already exists")
    debtor = Debtor(**payload.model_dump())
    db.add(debtor)
    db.commit()
    db.refresh(debtor)
    notify_update('debtor_updated', {'debtor_id': debtor.id})
    return debtor


@app.post("/api/admin/debtors", response_model=DebtorOut)
def create_admin_debtor(payload: DebtorCreate, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    existing = db.query(Debtor).filter(
        func.lower(Debtor.first_name) == payload.first_name.lower(),
        func.lower(func.coalesce(Debtor.last_name, "")) == (payload.last_name or "").lower(),
    ).first()
    if existing:
        raise HTTPException(status_code=400, detail="This debtor name already exists")
    if db.query(Debtor).filter(Debtor.phone == payload.phone).first():
        raise HTTPException(status_code=400, detail="This debtor phone already exists")
    debtor = Debtor(**payload.model_dump())
    db.add(debtor)
    db.commit()
    db.refresh(debtor)
    notify_update('debtor_updated', {'debtor_id': debtor.id})
    return debtor


@app.put("/api/admin/debtors/{debtor_id}", response_model=DebtorOut)
def update_admin_debtor(debtor_id: int, payload: DebtorUpdate, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    debtor = db.get(Debtor, debtor_id)
    if not debtor:
        raise HTTPException(status_code=404, detail="Qarzdor topilmadi")
    data = payload.model_dump(exclude_unset=True)
    if data.get('first_name') or data.get('last_name'):
        full_first = data.get('first_name', debtor.first_name)
        full_last = data.get('last_name', debtor.last_name or '')
        existing = db.query(Debtor).filter(
            Debtor.id != debtor.id,
            func.lower(Debtor.first_name) == full_first.lower(),
            func.lower(func.coalesce(Debtor.last_name, "")) == full_last.lower(),
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="This debtor name already exists")
    if data.get('phone') and db.query(Debtor).filter(Debtor.id != debtor.id, Debtor.phone == data['phone']).first():
        raise HTTPException(status_code=400, detail="This debtor phone already exists")
    for key, value in data.items():
        setattr(debtor, key, value)
    db.commit()
    db.refresh(debtor)
    notify_update('debtor_updated', {'debtor_id': debtor.id})
    return debtor


@app.delete("/api/admin/debtors/{debtor_id}")
def delete_admin_debtor(debtor_id: int, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    debtor = db.get(Debtor, debtor_id)
    if not debtor:
        raise HTTPException(status_code=404, detail="Qarzdor topilmadi")
    # Archive instead of delete
    debtor.is_active = False
    db.commit()
    notify_update('debtor_updated', {'debtor_id': debtor.id})
    return {"detail": "Qarzdor arxivga qo'yildi"}


@app.post("/api/debtors/{debtor_id}/pay")
def pay_debtor(debtor_id: int, payload: DebtorPayment, user: User = Depends(current_user), db: Session = Depends(get_db)):
    debtor = db.get(Debtor, debtor_id)
    if not debtor:
        raise HTTPException(status_code=404, detail="Qarzdor topilmadi")
    amount = payload.payment_cash + payload.payment_card
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Toʻlov summasi 0 dan katta boʻlishi kerak")
    debtor.total_debt = max(Decimal("0"), debtor.total_debt - amount)
    debtor.last_payment_at = datetime.utcnow()
    if debtor.total_debt <= 0:
        debtor.is_active = False
    db.add(
        DebtorTransaction(
            debtor_id=debtor.id,
            amount=-amount,
            payment_cash=payload.payment_cash,
            payment_card=payload.payment_card,
        )
    )
    db.commit()
    db.refresh(debtor)
    notify_update('debtor_updated', {'debtor_id': debtor.id})
    return debtor


@app.post("/api/productsales", response_model=ProductSaleOut)
def product_sale(payload: ProductSaleCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    product = db.get(Product, payload.product_id)
    if not product:
        raise HTTPException(status_code=404, detail="Mahsulot topilmadi")
    if payload.quantity <= 0:
        raise HTTPException(status_code=400, detail="Quantity must be greater than zero")
    if product.quantity is not None and payload.quantity > product.quantity:
        raise HTTPException(status_code=400, detail="Not enough product quantity available")
    total_amount = product.price * payload.quantity
    if payload.payment_cash + payload.payment_card + payload.payment_debt != total_amount:
        raise HTTPException(status_code=400, detail="Toʻlov jami mahsulot narxiga teng boʻlishi kerak")
    if payload.payment_debt > 0 and not payload.debtor_id:
        raise HTTPException(status_code=400, detail="Qarzdor tanlanishi kerak")
    sale = ProductSale(
        user_id=user.id,
        product_id=product.id,
        quantity=payload.quantity,
        total_amount=total_amount,
        payment_cash=payload.payment_cash,
        payment_card=payload.payment_card,
        payment_debt=payload.payment_debt,
        debtor_id=payload.debtor_id,
    )
    db.add(sale)
    if product.quantity is not None:
        product.quantity = max(0, product.quantity - payload.quantity)
    if payload.payment_debt > 0:
        debtor = db.get(Debtor, payload.debtor_id)
        if not debtor:
            raise HTTPException(status_code=404, detail="Qarzdor topilmadi")
        debtor.total_debt += payload.payment_debt
        debtor.last_payment_at = datetime.utcnow()
        db.add(
            DebtorTransaction(
                debtor_id=debtor.id,
                amount=payload.payment_debt,
                payment_cash=Decimal("0"),
                payment_card=Decimal("0"),
            )
        )
    db.commit()
    db.refresh(sale)
    notify_update('product_sold', {'sale_id': sale.id, 'product_id': product.id})
    notify_update('product_updated', {'product_id': product.id})
    return sale


@app.get("/api/productsales", response_model=list[ProductSaleOut])
def list_product_sales(limit: int = 50, _: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.query(ProductSale).order_by(ProductSale.created_at.desc()).limit(limit).all()


@app.post("/api/upload-image", response_model=UploadOut)
def upload_image(image: UploadFile = File(...), user: User = Depends(current_user)):
    if image.content_type not in {"image/jpeg", "image/png", "image/jpg", "image/gif"}:
        raise HTTPException(status_code=400, detail="Faqat jpg, jpeg, png, gif ruxsat")
    content = image.file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Rasm 5MB dan katta")
    suffix = Path(image.filename or "image.png").suffix.lower() or ".png"
    filename = f"{uuid4().hex}{suffix}"
    (UPLOAD_DIR / filename).write_bytes(content)
    return UploadOut(image_url=f"/uploads/{filename}")


@app.post("/api/daily-reports", response_model=DailyReportOut)
def create_daily_report(payload: DailyReportCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    today = date.today()
    start, end = day_bounds(today)
    stats = stats_query(db, start, end)
    total_discount = db.query(func.coalesce(func.sum(Session.discount), 0)).filter(Session.completed_at >= start, Session.completed_at < end).scalar() or Decimal("0")
    report = DailyReport(
        user_id=user.id,
        total_revenue=Decimal(str(stats["total_income"])),
        total_cash=Decimal(str(stats["payment_totals"]["cash"])),
        total_card=Decimal(str(stats["payment_totals"]["card"])),
        total_debt=Decimal(str(stats["payment_totals"]["debt"])),
        total_expenses=payload.expenses,
        total_discount=total_discount,
        cash_difference=payload.cash_difference,
        image_url=payload.image_url,
        comment=payload.comment,
    )
    db.add(report)
    db.commit()
    db.refresh(report)
    notify_update('daily_report_saved', {'report_id': report.id})
    return report


@app.get("/api/daily-reports", response_model=list[DailyReportOut])
def list_daily_reports(_: User = Depends(admin_user), db: Session = Depends(get_db)):
    return db.query(DailyReport).order_by(DailyReport.created_at.desc()).limit(200).all()


@app.get("/api/daily-reports/{report_id}", response_model=DailyReportOut)
def get_daily_report(report_id: int, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    report = db.get(DailyReport, report_id)
    if not report:
        raise HTTPException(status_code=404, detail="Hisobot topilmadi")
    return report


@app.get("/api/statistics/user/me")
def fetch_user_statistics(user: User = Depends(current_user), db: Session = Depends(get_db)):
    start, end = day_bounds(date.today())
    return stats_query(db, start, end, user_id=user.id)


@app.get("/api/statistics/yearly")
def yearly_statistics(year: int = Query(...), _: User = Depends(admin_user), db: Session = Depends(get_db)):
    start = datetime(year, 1, 1)
    end = datetime(year + 1, 1, 1)
    return {"year": year, **stats_query(db, start, end)}
