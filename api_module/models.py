# api_module/models.py
from typing import Optional, Dict, Any, List, Literal
from pydantic import BaseModel, Field, EmailStr

# ---- Favorite Players I/O ----
class FavoritePlayerIn(BaseModel):
    name: str
    nationality: Optional[str] = None
    age: Optional[int] = Field(default=None, ge=0)
    potential: Optional[int] = Field(default=None, ge=0, le=100)
    # Accepts SHORT or LONG; backend will normalize to LONG before storing.
    roles: List[str] = Field(default_factory=list)

class FavoritePlayerOut(BaseModel):
    id: str
    name: str
    nationality: Optional[str] = None
    age: Optional[int] = None
    potential: Optional[int] = None
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
    favorite_players: List[Dict[str, Any]]
    uiLanguage: Optional[Literal["en", "tr"]] = None  # <-- NEW

class ProfilePatch(BaseModel):
    dob: Optional[str] = None
    country: Optional[str] = None
    plan: Optional[str] = None
    favorite_players: Optional[List[Dict[str, Any]]] = None

# email codes
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

# ---- Chat models (existing) ----
class ChatIn(BaseModel):
    message: str
    session_id: Optional[str] = "default"
    strategy: Optional[str] = None

class Query(BaseModel):
    question: str
    strategy: Optional[str] = None
    session_id: str  # required for session tracking
