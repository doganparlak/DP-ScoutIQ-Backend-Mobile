# api_module/models.py
from typing import Optional, Dict, Any, List, Literal
from pydantic import BaseModel, Field, EmailStr
from api_module.utilities import PlanLiteral
from datetime import datetime



# ---- Favorite Players I/O ----
class FavoritePlayerIn(BaseModel):
    name: str
    nationality: Optional[str] = None
    age: Optional[int] = Field(default=None, ge=0)
    potential: Optional[int] = Field(default=None, ge=0, le=100)
    gender: Optional[str] = None
    height: Optional[float] = Field(default=None, ge=0)
    weight: Optional[float] = Field(default=None, ge=0)
    team: Optional[str] = None
    # Accepts SHORT or LONG; backend will normalize to LONG before storing.
    roles: List[str] = Field(default_factory=list)

class FavoritePlayerOut(BaseModel):
    id: str
    name: str
    nationality: Optional[str] = None
    age: Optional[int] = None
    potential: Optional[int] = None
    gender: Optional[str] = None
    height: Optional[float] = None
    weight: Optional[float] = None
    team: Optional[str] = None
    # LONG strings (e.g., "Center Back")
    roles: List[str]

# ---- Auth & Profile models ----
class SignUpIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8)
    dob: str                    # YYYY-MM-DD
    country: str
    favorite_players: List[Dict[str, Any]] = []
    plan: Optional[str] = None  # default 'Free' if omitted
    newsletter: Optional[bool] = False

class LoginIn(BaseModel):
    email: EmailStr
    password: str
    uiLanguage: Optional[Literal["en", "tr"]] = None

class LoginOut(BaseModel):
    token: str
    user: Dict[str, Any]

class ProfileOut(BaseModel):
    id: int
    email: EmailStr
    dob: Optional[str] = None
    country: Optional[str] = None
    plan: str
    favorite_players: List[Any] = []
    uiLanguage: Optional[Literal["en", "tr"]] = None  

    subscriptionEndAt: Optional[datetime] = None
    subscriptionPlatform: Optional[str] = None
    subscriptionAutoRenew: Optional[bool] = None

class ProfilePatch(BaseModel):
    dob: Optional[str] = None
    country: Optional[str] = None
    plan: Optional[str] = None
    favorite_players: Optional[List[Dict[str, Any]]] = None

class PlanUpdateIn(BaseModel):
    plan: PlanLiteral

# Email
class ReachOutIn(BaseModel):
    message: str
class PasswordResetRequestIn(BaseModel):
  email: EmailStr

class VerifyResetIn(BaseModel):
  email: EmailStr
  code: str

class SignupCodeRequestIn(BaseModel):
  email: EmailStr

class VerifySignupIn(BaseModel):
  email: EmailStr
  code: str

class SetNewPasswordIn(BaseModel):
    email: EmailStr
    new_password: str
class IAPActivateIn(BaseModel):
    platform: Literal["ios", "android"]
    product_id: str
    external_id: str      # originalTransactionId (iOS) or purchaseToken (Android)
    receipt: Optional[str] = None

# ---- Chat models (existing) ----
class ChatIn(BaseModel):
    message: str
    session_id: Optional[str] = "default"
    strategy: Optional[str] = None

class Query(BaseModel):
    question: str
    strategy: Optional[str] = None
    session_id: str  # required for session tracking
