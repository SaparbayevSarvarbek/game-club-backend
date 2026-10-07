import asyncio
import json
import os
from datetime import date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path
from urllib import request
from urllib.error import URLError
from uuid import uuid4

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import and_, func, inspect, or_, text
from sqlalchemy.orm import Session, joinedload

try:
    from supabase import create_client
except ImportError:  # pragma: no cover - optional dependency guard
    create_client = None

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
    Session as SessionModel,
    SessionProduct,
    User,
)
from app.timezone import now_uz, today_uz
from app.schemas import (
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
    SessionProductIn,
    SessionProductOut,
    UploadOut,
    UserCreate,
    UserOut,
    UserUpdate,
    DebtorTransactionOut,
    DebtorUpdate,
)

from app.config import (
    APP_ENV,
    BOT_API_KEY,
    FRONTEND_ORIGIN_REGEX,
    FRONTEND_ORIGINS,
    SUPABASE_BUCKET,
    SUPABASE_KEY,
    SUPABASE_URL,
)

app = FastAPI(title="GameClub Finance API", version="1.0.0")

supabase = None
try:
    if create_client is not None:
        supabase = create_client(SUPABASE_URL or "", SUPABASE_KEY or "")
except Exception:
    supabase = None


def upload_to_supabase(file_bytes: bytes, filename: str, content_type: str) -> str:
    if not supabase:
        raise HTTPException(status_code=400, detail="Supabase client is not configured")

    bucket_name = SUPABASE_BUCKET or "uploads"
    storage = supabase.storage.from_(bucket_name)

    try:
        storage.upload(file=file_bytes, path=filename, file_options={"content-type": content_type})
    except Exception as exc:
        raise HTTPException(status_code=400, detail=f"Supabase yuklashda xatolik: {exc}") from exc

    return storage.get_public_url(filename)

# CORS configuration: use explicit origin list from environment
frontend_origins = FRONTEND_ORIGINS or []

# Fail-fast for insecure wildcard origins in production
if APP_ENV == "production" and (not frontend_origins or any(o == "*" for o in frontend_origins)):
    raise RuntimeError("FRONTEND_ORIGINS must be set to a comma-separated list of allowed origins in production")

app.add_middleware(
    CORSMiddleware,
    allow_origins=frontend_origins or ["http://localhost:5173"],
    allow_origin_regex=FRONTEND_ORIGIN_REGEX or r"https://.*\.vercel\.app",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


main_loop = None

def ensure_column_exists(db: Session, table_name: str, column_name: str, column_definition: str):
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
        ensure_column_exists(db, "debtors", "note", "note TEXT")
        ensure_column_exists(db, "products", "cost_price", "cost_price NUMERIC(14, 2) NOT NULL DEFAULT 0")
        ensure_column_exists(db, "products", "purchase_total", "purchase_total NUMERIC(14, 2) NOT NULL DEFAULT 0")
        ensure_column_exists(db, "users", "is_active", "is_active BOOLEAN NOT NULL DEFAULT 1")
        ensure_column_exists(db, "computers", "is_active", "is_active BOOLEAN NOT NULL DEFAULT 0")
        ensure_column_exists(db, "debtor_transactions", "user_id", "user_id INTEGER REFERENCES users(id)")

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
    """
    Oylik oraliqni klub grafigi bo'yicha hisoblaydi (04:00 - 04:00).

    Masalan, 2026-09 oy uchun:
    - start: 2026-09-01 04:00:00
    - end:   2026-10-01 03:59:59

    Bu daily range (01.09 → 30.09) bilan bir xil natija beradi.
    """
    year, mon = [int(part) for part in month.split("-")]
    # Oyning birinchi kuni 04:00 da boshlanadi
    start = datetime(year, mon, 1, 4, 0, 0)
    # Keyingi oyning birinchi kuni 03:59:59 da tugaydi
    next_month = mon + 1 if mon < 12 else 1
    next_year = year if mon < 12 else year + 1
    end = datetime(next_year, next_month, 1, 3, 59, 59, 999999)
    return start, end


def year_bounds(year: int):
    """
    Yillik oraliqni klub grafigi bo'yicha hisoblaydi (04:00 - 04:00).

    Masalan, 2026 yil uchun:
    - start: 2026-01-01 04:00:00
    - end:   2027-01-01 03:59:59

    Bu daily range (01.01.2026 → 31.12.2026) bilan bir xil natija beradi.
    """
    # Yilning birinchi kuni 04:00 da boshlanadi
    start = datetime(year, 1, 1, 4, 0, 0)
    # Keyingi yilning birinchi kuni 03:59:59 da tugaydi
    end = datetime(year + 1, 1, 1, 3, 59, 59, 999999)
    return start, end


def money(value):
    return float(value or Decimal("0"))


def normalize_phone(phone: str | None) -> str:
    """Normalize any Uzbek phone variant to the 12-digit international form (998XXXXXXXXX)."""
    if not phone:
        return ""
    digits = "".join(ch for ch in str(phone) if ch.isdigit())
    if digits.startswith("8") and len(digits) == 10:
        digits = "998" + digits[1:]
    elif len(digits) == 9:
        digits = "998" + digits
    return digits


def phone_exists(db: Session, phone_digits: str, exclude_id: int | None = None) -> bool:
    """Check for a phone duplicate across any stored format."""
    query = db.query(Debtor.phone)
    if exclude_id is not None:
        query = query.filter(Debtor.id != exclude_id)
    return any(normalize_phone(p) == phone_digits for (p,) in query.all())


def format_money(value) -> str:
    return f"{money(value):,.0f}".replace(",", " ") + " so'm"


def format_bot_report(title: str, period: str, stats: dict) -> str:
    return "\n".join(
        [
            f"<b>{title}</b>",
            f"<b>Davr:</b> {period}",
            "",
            f"<b>Umumiy summa:</b> {format_money(stats.get('total_revenue'))}",
            f"<b>Naqd:</b> {format_money(stats.get('total_cash'))}",
            f"<b>Karta:</b> {format_money(stats.get('total_card'))}",
            f"<b>Qarz:</b> {format_money(stats.get('total_debt'))}",
            f"<b>Chegirma:</b> {format_money(stats.get('total_discount'))}",
            f"<b>Xarajat:</b> {format_money(stats.get('total_expenses', stats.get('total_expense')))}",
            f"<b>Sof foyda:</b> {format_money(stats.get('net_profit'))}",
            "",
            f"<b>Sessiyalar:</b> {stats.get('sessions_count', 0)} ta",
            f"<b>Sotilgan mahsulotlar:</b> {stats.get('products_sold', 0)} ta",
            f"<b>Yozuvlar:</b> {stats.get('records_count', 0)} ta",
            f"<b>Userlar:</b> {stats.get('users_count', 0)} ta",
        ]
    )


def send_telegram_report(text: str):
    token = os.getenv("BOT_TOKEN")
    chat_id = os.getenv("REPORT_CHAT_ID") or os.getenv("ADMIN_CHAT_ID")
    if not token or not chat_id:
        return
    payload = json.dumps({"chat_id": chat_id, "text": text, "parse_mode": "HTML"}).encode("utf-8")
    req = request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        request.urlopen(req, timeout=10).read()
    except URLError as exc:
        print(f"Telegram hisobot yuborilmadi: {exc}")


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

    # ✅ FIX: completed_at o'rniga created_at ishlatamiz + completed_at NULL bo'lmagan holatlarni ham hisobga olamiz
    # Completed sessions: completed_at mavjud va davrda
    # Active/old sessions: created_at davrda va status='completed' (eski ma'lumotlar uchun)
    session_filters = [
        SessionModel.status == "completed",
        or_(
            and_(
                SessionModel.completed_at.isnot(None),
                SessionModel.completed_at >= start,
                SessionModel.completed_at < end
            ),
            and_(
                SessionModel.completed_at.is_(None),
                SessionModel.created_at >= start,
                SessionModel.created_at < end
            )
        )
    ]

    sale_filters = [ProductSale.created_at >= start, ProductSale.created_at < end]
    if user_id:
        income_filters.append(Income.user_id == user_id)
        session_filters.append(SessionModel.user_id == user_id)
        sale_filters.append(ProductSale.user_id == user_id)
    total_income = db.query(func.coalesce(func.sum(Income.amount), 0)).filter(*income_filters).scalar() or Decimal("0")
    total_session_income = db.query(func.coalesce(func.sum(SessionModel.total_amount), 0)).filter(*session_filters).scalar() or Decimal("0")
    total_sale_income = db.query(func.coalesce(func.sum(ProductSale.total_amount), 0)).filter(*sale_filters).scalar() or Decimal("0")
    total_expense = db.query(func.coalesce(func.sum(Expense.amount), 0)).filter(*expense_filters).scalar() or Decimal("0")

    payment_totals = {
        "cash": Decimal("0"),
        "card": Decimal("0"),
        "debt": Decimal("0"),
    }
    for key, value in db.query(Income.payment_type, func.coalesce(func.sum(Income.amount), 0)).filter(*income_filters).group_by(Income.payment_type).all():
        if key == "debt":
            continue  # debt is derived from DebtorTransaction, not manual Income entries
        payment_totals[key] += value
    session_payments = db.query(
        func.coalesce(func.sum(SessionModel.payment_cash), 0),
        func.coalesce(func.sum(SessionModel.payment_card), 0),
    ).filter(*session_filters).one()
    payment_totals["cash"] += session_payments[0]
    payment_totals["card"] += session_payments[1]
    sale_payments = db.query(
        func.coalesce(func.sum(ProductSale.payment_cash), 0),
        func.coalesce(func.sum(ProductSale.payment_card), 0),
    ).filter(*sale_filters).one()
    payment_totals["cash"] += sale_payments[0]
    payment_totals["card"] += sale_payments[1]

    debt_tx_filters = [DebtorTransaction.created_at >= start, DebtorTransaction.created_at < end]
    if user_id:
        debt_tx_filters.append(DebtorTransaction.user_id == user_id)
    debt_payments = db.query(
        func.coalesce(func.sum(DebtorTransaction.payment_cash), 0),
        func.coalesce(func.sum(DebtorTransaction.payment_card), 0),
    ).filter(*debt_tx_filters).one()
    payment_totals["cash"] += debt_payments[0]
    payment_totals["card"] += debt_payments[1]
    # New debt created this period = sum of positive DebtorTransaction amounts.
    # DebtorTransaction is the single source of truth for debt (sessions and
    # product sales each write a positive row on credit), so this matches the
    # Qarz drawer exactly and never counts manual Income("debt") entries.
    debt_created = db.query(func.coalesce(func.sum(DebtorTransaction.amount), 0)).filter(
        *debt_tx_filters, DebtorTransaction.amount > 0
    ).scalar() or Decimal("0")
    payment_totals["debt"] += debt_created

    category_totals = {}
    for key, value in db.query(Income.category, func.coalesce(func.sum(Income.amount), 0)).filter(*income_filters).group_by(Income.category).all():
        category_totals[key] = category_totals.get(key, Decimal("0")) + value
    for key, value in db.query(SessionModel.category, func.coalesce(func.sum(SessionModel.computer_amount), 0)).filter(*session_filters).group_by(SessionModel.category).all():
        category_totals[key] = category_totals.get(key, Decimal("0")) + value
    products_total = db.query(func.coalesce(func.sum(ProductSale.total_amount), 0)).filter(*sale_filters).scalar() or Decimal("0")
    if products_total:
        category_totals["products"] = category_totals.get("products", Decimal("0")) + products_total

    # Include products sold inside sessions (SessionProduct linked to completed sessions)
    session_products_qty = db.query(func.coalesce(func.sum(SessionProduct.quantity), 0)).join(SessionModel, SessionModel.id == SessionProduct.session_id).filter(*session_filters).scalar() or 0
    session_products_amount = db.query(func.coalesce(func.sum(SessionProduct.price * SessionProduct.quantity), 0)).join(SessionModel, SessionModel.id == SessionProduct.session_id).filter(*session_filters).scalar() or Decimal("0")
    if session_products_amount:
        category_totals["products"] = category_totals.get("products", Decimal("0")) + session_products_amount

    income_count = db.query(func.count(Income.id)).filter(*income_filters).scalar() or 0
    session_count = db.query(func.count(SessionModel.id)).filter(*session_filters).scalar() or 0
    sale_count = db.query(func.count(ProductSale.id)).filter(*sale_filters).scalar() or 0
    records_count = income_count + session_count + sale_count

    sale_products_qty = db.query(func.coalesce(func.sum(ProductSale.quantity), 0)).filter(*sale_filters).scalar() or 0
    products_sold = (sale_products_qty or 0) + (session_products_qty or 0)
    direct_product_cost = db.query(func.coalesce(func.sum(Product.cost_price * ProductSale.quantity), 0)).join(ProductSale, ProductSale.product_id == Product.id).filter(*sale_filters).scalar() or Decimal("0")
    session_product_cost = db.query(func.coalesce(func.sum(Product.cost_price * SessionProduct.quantity), 0)).join(SessionProduct, SessionProduct.product_id == Product.id).join(SessionModel, SessionModel.id == SessionProduct.session_id).filter(*session_filters).scalar() or Decimal("0")
    products_revenue = products_total + session_products_amount
    products_cost = direct_product_cost + session_product_cost
    products_profit = products_revenue - products_cost
    total_discount = db.query(func.coalesce(func.sum(SessionModel.discount), 0)).filter(*session_filters).scalar() or Decimal("0")

    user_ids = set()
    user_ids.update([row[0] for row in db.query(func.distinct(Income.user_id)).filter(*income_filters).all()])
    user_ids.update([row[0] for row in db.query(func.distinct(SessionModel.user_id)).filter(*session_filters).all()])
    user_ids.update([row[0] for row in db.query(func.distinct(ProductSale.user_id)).filter(*sale_filters).all()])
    users_count = len(user_ids)

    by_user = {}
    for name, value in db.query(User.full_name, func.coalesce(func.sum(Income.amount), 0)).join(Income, Income.user_id == User.id).filter(*income_filters).group_by(User.id).all():
        by_user[name] = by_user.get(name, Decimal("0")) + value
    for name, value in db.query(User.full_name, func.coalesce(func.sum(SessionModel.total_amount), 0)).join(SessionModel, SessionModel.user_id == User.id).filter(*session_filters).group_by(User.id).all():
        by_user[name] = by_user.get(name, Decimal("0")) + value
    for name, value in db.query(User.full_name, func.coalesce(func.sum(ProductSale.total_amount), 0)).join(ProductSale, ProductSale.user_id == User.id).filter(*sale_filters).group_by(User.id).all():
        by_user[name] = by_user.get(name, Decimal("0")) + value

    total_expense_value = money(total_expense)
    return {
        "total_income": money(total_income + total_session_income + total_sale_income),
        "total_revenue": money(total_income + total_session_income + total_sale_income),
        "total_expense": total_expense_value,
        "total_expenses": total_expense_value,
        "net_profit": money(total_income + total_session_income + total_sale_income - total_expense),
        "total_cash": money(payment_totals["cash"]),
        "total_card": money(payment_totals["card"]),
        "total_debt": money(payment_totals["debt"]),
        "records_count": records_count,
        "users_count": users_count,
        "sessions_count": session_count,
        "products_sold": int(products_sold),
        "products_revenue": money(products_revenue),
        "products_cost": money(products_cost),
        "products_profit": money(products_profit),
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
def root(db: Session = Depends(get_db)):
    try:
        # Lightweight database health check to keep the connection responsive
        db.execute(text("SELECT 1"))
        return {
            "status": "success",
            "message": "GameClub backend API va Database to'liq ishlayapti"
        }
    except Exception as e:
        return {
            "status": "error",
            "message": "Bazaga ulanishda xatolik yuz berdi",
            "details": str(e)
        }

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
    # Sessions and their related SessionProduct and DebtorTransaction
    session_ids = db.query(SessionModel.id).filter(SessionModel.user_id == user_id).all()
    session_ids = [sid for (sid,) in session_ids]
    if session_ids:
        db.query(SessionProduct).filter(SessionProduct.session_id.in_(session_ids)).delete(synchronize_session=False)
        db.query(DebtorTransaction).filter(DebtorTransaction.session_id.in_(session_ids)).delete(synchronize_session=False)
    # DebtorTransactions created by this user without a session (debtor payments)
    db.query(DebtorTransaction).filter(DebtorTransaction.user_id == user_id).delete(synchronize_session=False)
    # Daily reports / closings linked to the user
    db.query(DailyReport).filter(DailyReport.user_id == user_id).delete(synchronize_session=False)
    db.query(DailyClosing).filter(DailyClosing.user_id == user_id).delete(synchronize_session=False)
    # Expenses created by this user (admin_id references users.id without a cascade)
    db.query(Expense).filter(Expense.admin_id == user_id).delete(synchronize_session=False)
    # Now delete the user (sessions will be cascade-deleted)
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
    image_url = upload_to_supabase(content, filename, image.content_type or "image/png")
    closing = DailyClosing(user_id=user.id, total_amount=total_amount, comment=comment, image_url=image_url)
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
def daily_statistics(
    start_date: date | None = Query(None),
    end_date: date | None = Query(None),
    date_filter: date | None = Query(None, alias="date"),
    _: User = Depends(admin_user),
    db: Session = Depends(get_db),
):
    if start_date and end_date:
        start, _ = day_bounds(start_date)
        _, end = day_bounds(end_date)
    else:
        # Fallback to single date parameter or today
        if date_filter:
            start, end = day_bounds(date_filter)
        else:
            start, end = day_bounds(today_uz())
    return {"date": start.date().isoformat(), **stats_query(db, start, end)}


@app.get("/api/dashboard/summary")
def dashboard_summary(_: User = Depends(current_user), db: Session = Depends(get_db)):
    # Return quick dashboard widgets for today
    start, end = day_bounds(today_uz())
    stats = stats_query(db, start, end)
    # PlayStation: category totals for playstation
    playstation_total = 0
    try:
        playstation_total = stats.get("category_totals", {}).get("playstation", 0)
    except Exception:
        playstation_total = 0
    return {
        "date": start.date().isoformat(),
        "cash": stats.get("payment_totals", {}).get("cash", 0),
        "card": stats.get("payment_totals", {}).get("card", 0),
        "debt": stats.get("payment_totals", {}).get("debt", 0),
        "playstation": playstation_total,
        "products_sold": stats.get("products_sold", 0),
        "total_discount": stats.get("total_discount", 0),
    }


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


def parse_flexible_date(date_str: str | None) -> date | None:
    if not date_str:
        return None
    cleaned = date_str.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%d-%m-%Y", "%Y.%m.%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(cleaned, fmt).date()
        except ValueError:
            continue
    return None


def parse_flexible_month(month_str: str | None) -> str | None:
    if not month_str:
        return None
    cleaned = month_str.strip()
    for fmt in ("%Y-%m", "%m.%Y", "%m/%Y", "%m-%Y", "%Y.%m", "%Y/%m"):
        try:
            return datetime.strptime(cleaned, fmt).strftime("%Y-%m")
        except ValueError:
            continue
    return None


@app.get("/api/bot/daily-report")
def bot_daily_report(date: str | None = Query(None), x_bot_api_key: str | None = Header(None), db: Session = Depends(get_db)):
    if x_bot_api_key != (BOT_API_KEY or "change-bot-secret"):
        raise HTTPException(status_code=403, detail="Bot API key xato")
    if date:
        parsed_date = parse_flexible_date(date)
        if not parsed_date:
            raise HTTPException(status_code=400, detail="Sana formati noto'g'ri. Namuna: 06.10.2026 yoki 2026-10-06")
        selected_day = parsed_date
    else:
        selected_day = today_uz()
    start, end = day_bounds(selected_day)
    stats = stats_query(db, start, end)
    return {
        "date": selected_day.isoformat(),
        "title": "Kunlik hisobot",
        "message": format_bot_report("Kunlik hisobot", selected_day.isoformat(), stats),
        "cashTotal": stats["payment_totals"].get("cash", 0),
        "cardTotal": stats["payment_totals"].get("card", 0),
        "debtTotal": stats["payment_totals"].get("debt", 0),
        "productsTotal": stats["category_totals"].get("products", 0),
        "discountTotal": stats["total_discount"],
        "recordsCount": stats["records_count"],
        "usersCount": stats["users_count"],
        "totalIncome": stats["total_income"],
        "totalExpense": stats["total_expense"],
        "netProfit": stats["net_profit"],
        **stats,
    }


@app.get("/api/bot/monthly-report")
def bot_monthly_report(month: str | None = Query(None), x_bot_api_key: str | None = Header(None), db: Session = Depends(get_db)):
    if x_bot_api_key != (BOT_API_KEY or "change-bot-secret"):
        raise HTTPException(status_code=403, detail="Bot API key xato")
    if month:
        parsed_month = parse_flexible_month(month)
        if not parsed_month:
            raise HTTPException(status_code=400, detail="Oy formati noto'g'ri. Namuna: 10.2026 yoki 2026-10")
        selected_month = parsed_month
    else:
        selected_month = today_uz().strftime("%Y-%m")
    start, end = month_bounds(selected_month)
    stats = stats_query(db, start, end)
    return {"month": selected_month, "title": "Oylik hisobot", "message": format_bot_report("Oylik hisobot", selected_month, stats), **stats}


@app.get("/api/bot/yearly-report")
def bot_yearly_report(year: int | None = Query(None), x_bot_api_key: str | None = Header(None), db: Session = Depends(get_db)):
    if x_bot_api_key != (BOT_API_KEY or "change-bot-secret"):
        raise HTTPException(status_code=403, detail="Bot API key xato")
    selected_year = year or today_uz().year
    start, end = year_bounds(selected_year)
    stats = stats_query(db, start, end)
    return {"year": selected_year, "title": "Yillik hisobot", "message": format_bot_report("Yillik hisobot", str(selected_year), stats), **stats}


@app.get("/api/computers", response_model=list[ComputerOut])
def fetch_computers(user: User = Depends(current_user), db: Session = Depends(get_db)):
    return db.query(Computer).order_by(Computer.number).all()


@app.get("/api/computers/{computer_id}/active-session", response_model=SessionActiveOut)
def fetch_active_session(computer_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = db.query(SessionModel).filter(SessionModel.computer_id == computer_id, SessionModel.status == "active").order_by(SessionModel.started_at.desc()).first()
    if not session:
        raise HTTPException(status_code=404, detail="Faol sessiya topilmadi")
    products = [
        SessionProductOut(
            id=item.id,
            product_id=item.product_id,
            product_name=item.product.name if item.product else None,
            quantity=item.quantity,
            price=item.price,
        )
        for item in session.products
    ]
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
        products=products,
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
    session = SessionModel(
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
        product = db.get(Product, item.product_id)
        if not product:
            raise HTTPException(status_code=404, detail=f"Mahsulot {item.product_id} topilmadi")
        if product.quantity is not None and item.quantity > product.quantity:
            raise HTTPException(status_code=400, detail=f"Mahsulot {product.name} yetarli emas")
        db.add(
            SessionProduct(
                session_id=session.id,
                product_id=product.id,
                quantity=item.quantity,
                price=item.price,
            )
        )
        if product.quantity is not None:
            product.quantity = max(0, product.quantity - item.quantity)
    db.commit()
    notify_update('session_started', {'session_id': session.id, 'computer_id': computer.id})
    return {"detail": "Sessiya boshlandi"}


@app.post("/api/sessions/{session_id}/save")
def save_session(session_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = db.get(SessionModel, session_id)
    if not session or session.status != "active":
        raise HTTPException(status_code=404, detail="Faol sessiya topilmadi")
    # allow any authenticated user to remove saved products from an active session
    session.updated_at = now_uz()
    # After saving, if the session has no products and no computer price, delete it and free the computer.
    remaining = db.query(func.count(SessionProduct.id)).filter(SessionProduct.session_id == session.id).scalar() or 0
    computer = db.get(Computer, session.computer_id)
    if remaining == 0 and (not session.computer_price or session.computer_price == Decimal("0")):
        # free computer and delete session
        if computer:
            computer.is_active = False
        db.delete(session)
    db.commit()
    notify_update('session_saved', {'session_id': session.id, 'computer_id': session.computer_id})
    return {"detail": "Sessiya saqlandi"}


@app.post("/api/sessions/{session_id}/products")
def add_products_to_session(session_id: int, payload: list[SessionProductIn], user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = db.get(SessionModel, session_id)
    if not session or session.status != "active":
        raise HTTPException(status_code=404, detail="Faol sessiya topilmadi")
    if session.user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Ruxsat yo'q")
    if not payload:
        raise HTTPException(status_code=400, detail="Hech qanday mahsulot yuborilmadi")

    added_amount = Decimal("0")
    total_qty = 0
    for item in payload:
        product = db.get(Product, item.product_id)
        if not product:
            raise HTTPException(status_code=404, detail=f"Mahsulot {item.product_id} topilmadi")
        if product.quantity is not None and item.quantity > product.quantity:
            raise HTTPException(status_code=400, detail=f"Mahsulot {product.name} yetarli emas")
        sp = SessionProduct(session_id=session.id, product_id=product.id, quantity=item.quantity, price=item.price)
        db.add(sp)
        if product.quantity is not None:
            product.quantity = max(0, product.quantity - item.quantity)
        line_amount = item.price * item.quantity
        added_amount += line_amount
        total_qty += item.quantity

    # update session totals
    session.products_amount = (session.products_amount or Decimal("0")) + added_amount
    session.total_amount = max(Decimal("0"), session.computer_price + session.products_amount - session.discount)
    session.updated_at = now_uz()
    db.commit()
    notify_update('session_product_added', {'session_id': session.id, 'computer_id': session.computer_id, 'added_qty': total_qty})
    notify_update('product_updated', {'product_id': None})
    return {"detail": "Mahsulotlar sessiyaga qo'shildi"}


@app.post("/api/sessions/{session_id}/complete")
def complete_session(session_id: int, payload: SessionComplete, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = db.get(SessionModel, session_id)
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
    session.completed_at = now_uz()
    session.updated_at = now_uz()
    session.computer.is_active = False
    if payload.payment_debt > 0 and payload.debtor_id:
        debtor = db.get(Debtor, payload.debtor_id)
        if not debtor:
            raise HTTPException(status_code=404, detail="Qarzdor topilmadi")
        debtor.total_debt += payload.payment_debt
        debtor.last_payment_at = now_uz()
        # New debt means the debtor is active again (they were likely deactivated at 0 balance)
        debtor.is_active = True
        db.add(
            DebtorTransaction(
                debtor_id=debtor.id,
                session_id=session.id,
                amount=payload.payment_debt,
                payment_cash=Decimal("0"),
                payment_card=Decimal("0"),
                user_id=user.id,
            )
        )
    db.commit()
    notify_update('session_completed', {'session_id': session.id, 'computer_id': session.computer_id})
    return {"detail": "Sessiya yakunlandi"}


@app.delete("/api/sessions/{session_id}/products/{session_product_id}")
def delete_session_product(session_id: int, session_product_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = db.get(SessionModel, session_id)
    if not session or session.status != "active":
        raise HTTPException(status_code=404, detail="Faol sessiya topilmadi")
    if session.user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Ruxsat yo'q")

    sp = db.get(SessionProduct, session_product_id)
    if not sp or sp.session_id != session.id:
        raise HTTPException(status_code=404, detail="Sessiyadagi mahsulot topilmadi")

    product = db.get(Product, sp.product_id)
    removed_qty = sp.quantity
    line_amount = (sp.price or Decimal("0")) * Decimal(removed_qty)

    if product and product.quantity is not None:
        product.quantity = product.quantity + removed_qty

    session.products_amount = max(Decimal("0"), (session.products_amount or Decimal("0")) - line_amount)
    session.total_amount = max(Decimal("0"), (session.computer_price or Decimal("0")) + (session.products_amount or Decimal("0")) - (session.discount or Decimal("0")))

    db.delete(sp)
    db.commit()

    # Do not delete the session here. Session deletion should occur when the session
    # is explicitly saved and found to be empty. This allows users to remove products
    # and still add new products before saving. The save endpoint will decide to
    # remove empty sessions when appropriate.

    notify_update('session_product_removed', {'session_id': session.id, 'computer_id': session.computer_id, 'removed_qty': removed_qty})
    notify_update('product_updated', {'product_id': product.id if product else None})
    return {"detail": "Mahsulot sessiyadan olib tashlandi"}


@app.delete("/api/sessions/{session_id}")
def cancel_session(session_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    session = db.get(SessionModel, session_id)
    if not session or session.status != "active":
        raise HTTPException(status_code=404, detail="Faol sessiya topilmadi")
    if session.user_id != user.id and user.role != "admin":
        raise HTTPException(status_code=403, detail="Ruxsat yo'q")

    # restore product quantities
    for sp in list(session.products):
        product = db.get(Product, sp.product_id)
        if product and product.quantity is not None:
            product.quantity = product.quantity + sp.quantity

    # mark computer as free
    computer = db.get(Computer, session.computer_id)
    if computer:
        computer.is_active = False

    db.delete(session)
    db.commit()
    notify_update('session_cancelled', {'session_id': session_id, 'computer_id': computer.id if computer else None})
    notify_update('product_updated', {'product_id': None})
    return {"detail": "Sessiya bekor qilindi"}


@app.get("/api/products", response_model=list[ProductOut])
def fetch_products(user: User = Depends(current_user), db: Session = Depends(get_db)):
    # Only return products with available stock.
    return db.query(Product).filter(Product.quantity > 0).order_by(Product.name).all()


@app.post("/api/products", response_model=ProductOut)
def create_product(payload: ProductCreate, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    data = payload.model_dump()
    quantity = data.get("quantity", 0) or 0
    purchase_total = data.get("purchase_total", Decimal("0")) or Decimal("0")
    cost_price = data.get("cost_price") or (purchase_total / quantity if quantity else Decimal("0"))
    product = Product(
        name=data["name"],
        price=data["price"],
        quantity=quantity,
        purchase_total=purchase_total,
        cost_price=cost_price,
        created_by_id=_.id if _ else None,
    )
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
    data = payload.model_dump(exclude_unset=True)
    if "purchase_total" in data and "cost_price" not in data:
        quantity = data.get("quantity", product.quantity) or 0
        data["cost_price"] = data["purchase_total"] / quantity if quantity else Decimal("0")
    allowed = {"name", "price", "quantity", "purchase_total", "cost_price"}
    for key, value in data.items():
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
def list_debtors(
    search: str | None = Query(None),
    include_zero_debt: bool = Query(False),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    if include_zero_debt:
        # Session debtor picker: allow selecting every debtor, including archived
        # (is_active=False) ones, so an archived customer can still be picked for credit.
        query = db.query(Debtor)
    else:
        # Debt payments page: show only non-archived (active) debtors.
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
def get_debtor(debtor_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    debtor = db.get(Debtor, debtor_id)
    if not debtor:
        raise HTTPException(status_code=404, detail="Qarzdor topilmadi")
    return debtor


@app.get("/api/debtors/{debtor_id}/history", response_model=list[DebtorTransactionOut])
def debtor_history(debtor_id: int, user: User = Depends(current_user), db: Session = Depends(get_db)):
    debtor = db.get(Debtor, debtor_id)
    if not debtor:
        raise HTTPException(status_code=404, detail="Qarzdor topilmadi")
    # Return transaction history for the debtor
    return db.query(DebtorTransaction).filter(DebtorTransaction.debtor_id == debtor_id).order_by(DebtorTransaction.created_at.desc()).all()


@app.get("/api/admin/debtors", response_model=list[DebtorOut])
def fetch_all_debtors(
    search: str | None = Query(None),
    archived: bool = Query(False),
    _: User = Depends(admin_user),
    db: Session = Depends(get_db),
):
    query = db.query(Debtor)
    if archived:
        query = query.filter(Debtor.is_active == False)
    else:
        query = query.filter(Debtor.is_active == True)
    if search:
        term = f"%{search.lower()}%"
        full_name = func.lower(func.trim(Debtor.first_name + ' ' + func.coalesce(Debtor.last_name, "")))
        query = query.filter(
            or_(
                func.lower(Debtor.first_name).like(term),
                func.lower(func.coalesce(Debtor.last_name, "")).like(term),
                full_name.like(term),
                func.lower(Debtor.phone).like(term),
            )
        )
    return query.order_by(Debtor.created_at.desc()).limit(500).all()


@app.post("/api/debtors", response_model=DebtorOut)
def create_debtor(payload: DebtorCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    phone_digits = normalize_phone(payload.phone)
    if not phone_digits:
        raise HTTPException(status_code=400, detail="Telefon raqami noto'g'ri")
    if phone_exists(db, phone_digits):
        raise HTTPException(status_code=400, detail="This debtor phone already exists")
    data = payload.model_dump()
    data["phone"] = phone_digits
    debtor = Debtor(**data)
    db.add(debtor)
    db.commit()
    db.refresh(debtor)
    notify_update('debtor_updated', {'debtor_id': debtor.id})
    return debtor


@app.post("/api/admin/debtors", response_model=DebtorOut)
def create_admin_debtor(payload: DebtorCreate, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    phone_digits = normalize_phone(payload.phone)
    if not phone_digits:
        raise HTTPException(status_code=400, detail="Telefon raqami noto'g'ri")
    if phone_exists(db, phone_digits):
        raise HTTPException(status_code=400, detail="This debtor phone already exists")
    data = payload.model_dump()
    data["phone"] = phone_digits
    debtor = Debtor(**data)
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
    # Only enforce name uniqueness when the name actually changed. Otherwise any
    # edit to a debtor who shares a name with another debtor would 400 on save.
    eff_first = data.get('first_name', debtor.first_name)
    eff_last = data.get('last_name', debtor.last_name or '')
    if (eff_first, eff_last) != (debtor.first_name, debtor.last_name or ''):
        existing = db.query(Debtor).filter(
            Debtor.id != debtor.id,
            func.lower(Debtor.first_name) == eff_first.lower(),
            func.lower(func.coalesce(Debtor.last_name, "")) == eff_last.lower(),
        ).first()
        if existing:
            raise HTTPException(status_code=400, detail="This debtor name already exists")
    if data.get('phone'):
        phone_digits = normalize_phone(data['phone'])
        if not phone_digits:
            raise HTTPException(status_code=400, detail="Telefon raqami noto'g'ri")
        if phone_exists(db, phone_digits, exclude_id=debtor.id):
            raise HTTPException(status_code=400, detail="This debtor phone already exists")
        data['phone'] = phone_digits
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
    # Archive instead of hard-deleting: the debtor moves to the Arxiv section.
    # Their history and statistics stay intact so records are never destroyed.
    debtor.is_active = False
    db.commit()
    notify_update('debtor_updated', {'debtor_id': debtor.id})
    return {"detail": "Qarzdor arxivga o'tkazildi"}


@app.post("/api/admin/debtors/{debtor_id}/restore", response_model=DebtorOut)
def restore_admin_debtor(debtor_id: int, _: User = Depends(admin_user), db: Session = Depends(get_db)):
    debtor = db.get(Debtor, debtor_id)
    if not debtor:
        raise HTTPException(status_code=404, detail="Qarzdor topilmadi")
    debtor.is_active = True
    db.commit()
    db.refresh(debtor)
    notify_update('debtor_updated', {'debtor_id': debtor.id})
    return debtor


@app.post("/api/debtors/{debtor_id}/pay")
def pay_debtor(debtor_id: int, payload: DebtorPayment, user: User = Depends(current_user), db: Session = Depends(get_db)):
    debtor = db.get(Debtor, debtor_id)
    if not debtor:
        raise HTTPException(status_code=404, detail="Qarzdor topilmadi")
    amount = payload.payment_cash + payload.payment_card
    if amount <= 0:
        raise HTTPException(status_code=400, detail="Toʻlov summasi 0 dan katta boʻlishi kerak")
    debtor.total_debt = max(Decimal("0"), debtor.total_debt - amount)
    debtor.last_payment_at = now_uz()
    if debtor.total_debt <= 0:
        debtor.is_active = False
    db.add(
        DebtorTransaction(
            debtor_id=debtor.id,
            amount=-amount,
            payment_cash=payload.payment_cash,
            payment_card=payload.payment_card,
            user_id=user.id,
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
        debtor.last_payment_at = now_uz()
        # New debt means the debtor is active again (they were likely deactivated at 0 balance)
        debtor.is_active = True
        db.add(
            DebtorTransaction(
                debtor_id=debtor.id,
                amount=payload.payment_debt,
                payment_cash=Decimal("0"),
                payment_card=Decimal("0"),
                user_id=user.id,
            )
        )
    db.commit()
    db.refresh(sale)
    notify_update('product_sold', {'sale_id': sale.id, 'product_id': product.id})
    notify_update('product_updated', {'product_id': product.id})
    return sale


@app.get("/api/productsales")
def list_product_sales(
    limit: int = 100,
    date: str | None = Query(None),
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    month: str | None = Query(None),
    year: int | None = Query(None),
    product_id: int | None = Query(None),
    computer_id: int | None = Query(None),
    _: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    start = end = None
    if start_date and end_date:
        try:
            parsed_start = datetime.strptime(start_date, "%Y-%m-%d").date()
            parsed_end = datetime.strptime(end_date, "%Y-%m-%d").date()
            start, _ = day_bounds(parsed_start)
            _, end = day_bounds(parsed_end)
        except ValueError:
            raise HTTPException(status_code=400, detail="start_date and end_date must be YYYY-MM-DD")
    elif date:
        try:
            selected = datetime.strptime(date, "%Y-%m-%d").date()
            start, end = day_bounds(selected)
        except ValueError:
            raise HTTPException(status_code=400, detail="Sana YYYY-MM-DD formatida bo'lishi kerak")
    elif month:
        try:
            start, end = month_bounds(month)
        except ValueError:
            raise HTTPException(status_code=400, detail="Month must be YYYY-MM")
    elif year:
        start, end = year_bounds(year)

    rows: list[dict] = []
    sale_query = db.query(ProductSale).options(joinedload(ProductSale.product), joinedload(ProductSale.user))
    if start and end:
        sale_query = sale_query.filter(ProductSale.created_at >= start, ProductSale.created_at < end)
    if product_id:
        sale_query = sale_query.filter(ProductSale.product_id == product_id)
    if not computer_id:
        for sale in sale_query.order_by(ProductSale.created_at.desc()).limit(limit).all():
            rows.append({
                "id": sale.id,
                "sale_key": f"sale-{sale.id}",
                "source": "direct",
                "user_id": sale.user_id,
                "operator_name": sale.user.full_name if sale.user else None,
                "product_id": sale.product_id,
                "product_name": sale.product.name if sale.product else None,
                "unit_price": sale.total_amount / sale.quantity if sale.quantity else sale.total_amount,
                "cost_price": sale.product.cost_price if sale.product else Decimal("0"),
                "profit": (sale.total_amount - ((sale.product.cost_price if sale.product else Decimal("0")) * sale.quantity)),
                "quantity": sale.quantity,
                "total_amount": sale.total_amount,
                "payment_cash": sale.payment_cash,
                "payment_card": sale.payment_card,
                "payment_debt": sale.payment_debt,
                "debtor_id": sale.debtor_id,
                "computer_id": None,
                "computer_number": None,
                "created_at": sale.created_at,
                "updated_at": sale.updated_at,
            })

    session_query = (
        db.query(SessionProduct)
        .join(SessionModel, SessionProduct.session_id == SessionModel.id)
        .options(
            joinedload(SessionProduct.product),
            joinedload(SessionProduct.session).joinedload(SessionModel.computer),
            joinedload(SessionProduct.session).joinedload(SessionModel.user),
        )
    )
    if start and end:
        session_query = session_query.filter(SessionProduct.created_at >= start, SessionProduct.created_at < end)
    if product_id:
        session_query = session_query.filter(SessionProduct.product_id == product_id)
    if computer_id:
        session_query = session_query.filter(SessionModel.computer_id == computer_id)
    for item in session_query.order_by(SessionProduct.created_at.desc()).limit(limit).all():
        session = item.session
        rows.append({
            "id": item.id,
            "sale_key": f"session-{item.id}",
            "source": "session",
            "user_id": session.user_id if session else None,
            "operator_name": session.user.full_name if session and session.user else None,
            "product_id": item.product_id,
            "product_name": item.product.name if item.product else None,
            "unit_price": item.price,
            "cost_price": item.product.cost_price if item.product else Decimal("0"),
            "profit": ((item.price - (item.product.cost_price if item.product else Decimal("0"))) * item.quantity),
            "quantity": item.quantity,
            "total_amount": item.price * item.quantity,
            "payment_cash": Decimal("0"),
            "payment_card": Decimal("0"),
            "payment_debt": Decimal("0"),
            "debtor_id": session.debtor_id if session else None,
            "computer_id": session.computer_id if session else None,
            "computer_number": session.computer.number if session and session.computer else None,
            "created_at": item.created_at,
            "updated_at": item.created_at,
        })

    rows.sort(key=lambda row: row["created_at"], reverse=True)
    return rows[:limit]


@app.get("/api/debt-transactions")
def list_debt_transactions(
    limit: int = 100,
    date: str | None = Query(None),
    start_date: str | None = Query(None),
    end_date: str | None = Query(None),
    month: str | None = Query(None),
    year: int | None = Query(None),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    query = db.query(DebtorTransaction).options(joinedload(DebtorTransaction.debtor))
    start = end = None
    if start_date and end_date:
        try:
            parsed_start = datetime.strptime(start_date, "%Y-%m-%d").date()
            parsed_end = datetime.strptime(end_date, "%Y-%m-%d").date()
            start, _ = day_bounds(parsed_start)
            _, end = day_bounds(parsed_end)
        except ValueError:
            raise HTTPException(status_code=400, detail="start_date and end_date must be YYYY-MM-DD")
    elif date:
        try:
            selected = datetime.strptime(date, "%Y-%m-%d").date()
            start, end = day_bounds(selected)
        except ValueError:
            raise HTTPException(status_code=400, detail="Sana YYYY-MM-DD formatida bo'lishi kerak")
    elif month:
        try:
            start, end = month_bounds(month)
        except ValueError:
            raise HTTPException(status_code=400, detail="Month must be YYYY-MM")
    elif year:
        start, end = year_bounds(year)

    if start and end:
        query = query.filter(DebtorTransaction.created_at >= start, DebtorTransaction.created_at < end)
    return [
        {
            "id": item.id,
            "debtor_id": item.debtor_id,
            "debtor_name": item.debtor.full_name if item.debtor else None,
            "debtor_phone": item.debtor.phone if item.debtor else None,
            "amount": item.amount,
            "type": "paid" if item.amount < 0 else "borrowed",
            "created_at": item.created_at,
            "note": item.note,
        }
        for item in query.order_by(DebtorTransaction.created_at.desc()).limit(limit).all()
    ]


@app.post("/api/upload-image", response_model=UploadOut)
def upload_image(image: UploadFile = File(...), user: User = Depends(current_user)):
    if image.content_type not in {"image/jpeg", "image/png", "image/jpg", "image/gif"}:
        raise HTTPException(status_code=400, detail="Faqat jpg, jpeg, png, gif ruxsat")
    content = image.file.read()
    if len(content) > 5 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Rasm 5MB dan katta")
    suffix = Path(image.filename or "image.png").suffix.lower() or ".png"
    filename = f"{uuid4().hex}{suffix}"
    image_url = upload_to_supabase(content, filename, image.content_type or "image/png")
    return UploadOut(image_url=image_url)


@app.post("/api/daily-reports", response_model=DailyReportOut)
def create_daily_report(payload: DailyReportCreate, user: User = Depends(current_user), db: Session = Depends(get_db)):
    report_day = today_uz()
    start, end = day_bounds(report_day)
    stats = stats_query(db, start, end)
    total_discount = db.query(func.coalesce(func.sum(SessionModel.discount), 0)).filter(SessionModel.completed_at >= start, SessionModel.completed_at < end).scalar() or Decimal("0")
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
    send_telegram_report(format_bot_report("Kunlik hisobot yakunlandi", report_day.isoformat(), stats))
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
    start, end = day_bounds(today_uz())
    return stats_query(db, start, end, user_id=user.id)


@app.get("/api/statistics/yearly")
def yearly_statistics(year: int = Query(...), _: User = Depends(admin_user), db: Session = Depends(get_db)):
    start = datetime(year, 1, 1)
    end = datetime(year + 1, 1, 1)
    return {"year": year, **stats_query(db, start, end)}


# ---------------------------------------------------------------------------
# Bot — qarzdorlar ro'yxati
# ---------------------------------------------------------------------------
@app.get("/api/bot/debtors")
def bot_list_debtors(
    x_bot_api_key: str = Header(..., alias="x-bot-api-key"),
    db: Session = Depends(get_db),
):
    if x_bot_api_key != (BOT_API_KEY or "change-bot-secret"):
        raise HTTPException(status_code=403, detail="Bot API key noto'g'ri")
    debtors = db.query(Debtor).filter(Debtor.is_active == True).order_by(Debtor.created_at.desc()).limit(500).all()
    return {
        "count": len(debtors),
        "debtors": [
            {
                "id": d.id,
                "first_name": d.first_name,
                "last_name": d.last_name,
                "full_name": d.full_name,
                "phone": d.phone,
                "total_debt": float(d.total_debt),
                "note": d.note,
                "created_at": d.created_at.isoformat() if d.created_at else None,
            }
            for d in debtors
        ],
    }
