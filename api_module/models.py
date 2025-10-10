# api_module/models.py
from typing import Optional, Dict, Any, List
from pydantic import BaseModel, Field, EmailStr

# ---- Auth & Profile models ----
class SignUpIn(BaseModel):
    email: EmailStr
    password: str = Field(min_length=6)
    dob: str                    # YYYY-MM-DD
    country: str
    favorite_players: List[Dict[str, Any]] = []
    plan: Optional[str] = None  # default 'Free' if omitted
    newsletter: Optional[bool] = False

class LoginIn(BaseModel):
    email: EmailStr
    password: str

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

# ---- Chat models (existing) ----
class ChatIn(BaseModel):
    message: str
    session_id: Optional[str] = "default"
    strategy: Optional[str] = None

class Query(BaseModel):
    question: str
    strategy: Optional[str] = None
    session_id: str  # required for session tracking
