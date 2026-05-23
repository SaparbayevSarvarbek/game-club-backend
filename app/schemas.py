from datetime import datetime
from decimal import Decimal

from pydantic import BaseModel, Field


class LoginIn(BaseModel):
    username: str
    password: str


class UserOut(BaseModel):
    id: int
    full_name: str
    username: str
    role: str
    is_active: bool
    created_at: datetime

    class Config:
        from_attributes = True


class UserCreate(BaseModel):
    full_name: str = Field(min_length=2)
    username: str = Field(min_length=3)
    password: str = Field(min_length=4)
    role: str = "user"
    is_active: bool = True


class UserUpdate(BaseModel):
    full_name: str | None = None
    username: str | None = None
    password: str | None = None
    role: str | None = None
    is_active: bool | None = None


class ProfileUpdate(BaseModel):
    current_password: str
    new_username: str | None = None
    new_password: str | None = None


class IncomeCreate(BaseModel):
    amount: Decimal = Field(gt=0)
    payment_type: str
    category: str
    comment: str | None = None


class IncomeUpdate(BaseModel):
    amount: Decimal | None = Field(default=None, gt=0)
    payment_type: str | None = None
    category: str | None = None
    comment: str | None = None


class IncomeOut(BaseModel):
    id: int
    user_id: int
    amount: Decimal
    payment_type: str
    category: str
    comment: str | None
    created_at: datetime
    updated_at: datetime
    user: UserOut

    class Config:
        from_attributes = True


class ComputerOut(BaseModel):
    id: int
    number: int
    type: str
    is_active: bool

    class Config:
        from_attributes = True


class ProductCreate(BaseModel):
    name: str = Field(min_length=2)
    price: Decimal = Field(gt=0)
    quantity: int = Field(default=0, ge=0)


class ProductUpdate(BaseModel):
    name: str | None = None
    price: Decimal | None = Field(default=None, gt=0)
    quantity: int | None = Field(default=None, ge=0)


class ProductOut(BaseModel):
    id: int
    name: str
    price: Decimal
    quantity: int

    class Config:
        from_attributes = True


class DebtorCreate(BaseModel):
    first_name: str = Field(min_length=2)
    last_name: str | None = None
    phone: str = Field(min_length=5)
    total_debt: Decimal = Field(default=0)


class DebtorUpdate(BaseModel):
    first_name: str | None = None
    last_name: str | None = None
    phone: str | None = None
    total_debt: Decimal | None = None


class DebtorPayment(BaseModel):
    payment_cash: Decimal = Field(default=0)
    payment_card: Decimal = Field(default=0)


class DebtorTransactionOut(BaseModel):
    id: int
    debtor_id: int
    session_id: int | None = None
    amount: Decimal
    payment_cash: Decimal
    payment_card: Decimal
    created_at: datetime
    note: str | None = None

    class Config:
        from_attributes = True


class DebtorOut(BaseModel):
    id: int
    first_name: str
    last_name: str | None = None
    full_name: str
    phone: str
    total_debt: Decimal
    last_payment_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SessionProductIn(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)
    price: Decimal = Field(gt=0)


class ProductSaleCreate(BaseModel):
    product_id: int
    quantity: int = Field(gt=0)
    payment_cash: Decimal = Field(ge=0)
    payment_card: Decimal = Field(ge=0)
    payment_debt: Decimal = Field(default=Decimal("0"), ge=0)
    debtor_id: int | None = None


class ProductSaleOut(BaseModel):
    id: int
    user_id: int
    product_id: int
    quantity: int
    total_amount: Decimal
    payment_cash: Decimal
    payment_card: Decimal
    payment_debt: Decimal
    debtor_id: int | None = None
    created_at: datetime
    updated_at: datetime

    class Config:
        from_attributes = True


class SessionStart(BaseModel):
    computer_id: int
    computer_price: Decimal = Field(ge=0)
    products: list[SessionProductIn] = []
    discount: Decimal = Field(ge=0)


class SessionComplete(BaseModel):
    payment_cash: Decimal = Field(ge=0)
    payment_card: Decimal = Field(ge=0)
    payment_debt: Decimal = Field(ge=0)
    debtor_id: int | None = None
    computer_price: Decimal | None = Field(default=None, ge=0)
    discount: Decimal | None = Field(default=None, ge=0)


class SessionActiveOut(BaseModel):
    session_id: int
    computer_id: int
    started_at: datetime
    computer_price: Decimal
    products_amount: Decimal
    discount: Decimal
    total_amount: Decimal
    status: str
    payment_cash: Decimal
    payment_card: Decimal
    payment_debt: Decimal
    debtor_id: int | None = None

    class Config:
        from_attributes = True


class UploadOut(BaseModel):
    image_url: str


class DailyReportCreate(BaseModel):
    expenses: Decimal = Field(ge=0)
    cash_difference: Decimal = Field(default=0)
    image_url: str | None = None
    comment: str | None = None


class DailyReportOut(BaseModel):
    id: int
    user_id: int
    total_revenue: Decimal
    total_cash: Decimal
    total_card: Decimal
    total_debt: Decimal
    total_expenses: Decimal
    total_discount: Decimal
    cash_difference: Decimal
    image_url: str | None = None
    comment: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class AdminStatisticsOut(BaseModel):
    total_revenue: Decimal
    total_cash: Decimal
    total_card: Decimal
    total_debt: Decimal
    total_expenses: Decimal
    total_discount: Decimal
    sessions_count: int
    products_sold: int
    records_count: int
    users_count: int


class UserStatisticsOut(BaseModel):
    total_revenue: Decimal
    total_cash: Decimal
    total_card: Decimal
    total_debt: Decimal

    class Config:
        from_attributes = True


class DailyClosingOut(BaseModel):
    id: int
    user_id: int
    total_amount: Decimal
    comment: str | None = None
    image_url: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True


class ExpenseCreate(BaseModel):
    title: str
    amount: Decimal
    comment: str | None = None


class ExpenseOut(BaseModel):
    id: int
    admin_id: int
    title: str
    amount: Decimal
    comment: str | None = None
    created_at: datetime

    class Config:
        from_attributes = True