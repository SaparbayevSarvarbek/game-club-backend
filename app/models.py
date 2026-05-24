from datetime import datetime
from decimal import Decimal
from enum import Enum

from sqlalchemy import Boolean, DateTime, ForeignKey, Numeric, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base
from app.timezone import now_uz


class UserRole(str, Enum):
    admin = "admin"
    user = "user"


class PaymentType(str, Enum):
    cash = "cash"
    card = "card"
    debt = "debt"


class IncomeCategory(str, Enum):
    computer = "computer"
    playstation = "playstation"
    products = "products"


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    full_name: Mapped[str] = mapped_column(String(255))
    username: Mapped[str] = mapped_column(String(120), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    role: Mapped[str] = mapped_column(String(20), default=UserRole.user.value)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_uz)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_uz, onupdate=now_uz)

    incomes = relationship("Income", back_populates="user", cascade="all, delete-orphan")
    closings = relationship("DailyClosing", back_populates="user", cascade="all, delete-orphan")
    sessions = relationship("Session", back_populates="user", cascade="all, delete-orphan")
    product_sales = relationship("ProductSale", back_populates="user", cascade="all, delete-orphan")


class Income(Base):
    __tablename__ = "incomes"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    payment_type: Mapped[str] = mapped_column(String(20), index=True)
    category: Mapped[str] = mapped_column(String(30), index=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_uz, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_uz, onupdate=now_uz)

    user = relationship("User", back_populates="incomes")


class Computer(Base):
    __tablename__ = "computers"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    number: Mapped[int] = mapped_column(index=True, unique=True)
    type: Mapped[str] = mapped_column(String(30))
    is_active: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_uz)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_uz, onupdate=now_uz)

    sessions = relationship("Session", back_populates="computer")


class Product(Base):
    __tablename__ = "products"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    name: Mapped[str] = mapped_column(String(255), index=True)
    price: Mapped[Decimal] = mapped_column(Numeric(14, 2))
    quantity: Mapped[int] = mapped_column(default=0)
    created_by_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_uz)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_uz, onupdate=now_uz)

    created_by = relationship("User")
    sales = relationship("ProductSale", back_populates="product")
    session_products = relationship("SessionProduct", back_populates="product")


class Debtor(Base):
    __tablename__ = "debtors"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    first_name: Mapped[str] = mapped_column(String(120))
    last_name: Mapped[str | None] = mapped_column(String(120), nullable=True)
    phone: Mapped[str] = mapped_column(String(50), unique=True, index=True)
    total_debt: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    last_payment_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_uz)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_uz, onupdate=now_uz)

    transactions = relationship("DebtorTransaction", back_populates="debtor")

    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name or ''}".strip()


class Session(Base):
    __tablename__ = "sessions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    computer_id: Mapped[int] = mapped_column(ForeignKey("computers.id"), index=True)
    debtor_id: Mapped[int | None] = mapped_column(ForeignKey("debtors.id"), index=True, nullable=True)
    computer_price: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    computer_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    products_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    discount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    payment_cash: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    payment_card: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    payment_debt: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    status: Mapped[str] = mapped_column(String(20), default="active", index=True)
    category: Mapped[str] = mapped_column(String(30), index=True)
    started_at: Mapped[datetime] = mapped_column(DateTime, default=now_uz, index=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_uz)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_uz, onupdate=now_uz)

    user = relationship("User", back_populates="sessions")
    computer = relationship("Computer", back_populates="sessions")
    debtor = relationship("Debtor")
    # When a Session is deleted, delete its SessionProduct rows
    products = relationship(
        "SessionProduct",
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )
    debtor_transactions = relationship(
        "DebtorTransaction",
        back_populates="session",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )


class SessionProduct(Base):
    __tablename__ = "session_products"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    # Ensure deletion of a Session cascades to SessionProduct rows
    session_id: Mapped[int] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    quantity: Mapped[int] = mapped_column(default=1)
    price: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_uz)

    session = relationship("Session", back_populates="products")
    product = relationship("Product", back_populates="session_products")


class ProductSale(Base):
    __tablename__ = "product_sales"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    quantity: Mapped[int] = mapped_column(default=1)
    total_amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    payment_cash: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    payment_card: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    payment_debt: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    debtor_id: Mapped[int | None] = mapped_column(ForeignKey("debtors.id"), index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_uz)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_uz, onupdate=now_uz)

    user = relationship("User", back_populates="product_sales")
    product = relationship("Product", back_populates="sales")
    debtor = relationship("Debtor")


class DebtorTransaction(Base):
    __tablename__ = "debtor_transactions"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    debtor_id: Mapped[int] = mapped_column(ForeignKey("debtors.id"), index=True)
    session_id: Mapped[int | None] = mapped_column(ForeignKey("sessions.id", ondelete="CASCADE"), index=True, nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    payment_cash: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    payment_card: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_uz)
    note: Mapped[str | None] = mapped_column(Text, nullable=True)

    debtor = relationship("Debtor", back_populates="transactions")
    session = relationship("Session", back_populates="debtor_transactions")


class DailyReport(Base):
    __tablename__ = "daily_reports"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    total_revenue: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    total_cash: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    total_card: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    total_debt: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    total_expenses: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    total_discount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    cash_difference: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))
    image_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    comment: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime, default=now_uz, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=now_uz, onupdate=now_uz)

    user = relationship("User")
class DailyClosing(Base):
    __tablename__ = "daily_closings"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)

    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(14, 2),
        default=Decimal("0.00")
    )

    comment: Mapped[str | None] = mapped_column(Text, nullable=True)

    image_url: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_uz
    )

    user = relationship("User", back_populates="closings")


class Expense(Base):
    __tablename__ = "expenses"

    id: Mapped[int] = mapped_column(primary_key=True, index=True)

    admin_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        index=True
    )

    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), default=Decimal("0.00"))

    title: Mapped[str] = mapped_column(String(255))

    comment: Mapped[str | None] = mapped_column(
        Text,
        nullable=True
    )

    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        default=now_uz
    )

    admin = relationship("User")
