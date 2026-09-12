from typing import Any, Dict, List, Optional, Literal
from datetime import datetime
from pydantic import BaseModel, Field
class MatchAnalysisSearchIn(BaseModel):
    country: Optional[str] = None
    league: Optional[str] = None
    leagueId: Optional[int] = None
    homeTeam: Optional[str] = None
    awayTeam: Optional[str] = None
    startDate: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    endDate: Optional[str] = Field(default=None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    page: int = Field(default=1, ge=1)
    limit: int = Field(default=50, ge=1, le=50)


class MatchAnalysisSearchOut(BaseModel):
    fixtures: List[Dict[str, Any]]
    pagination: Dict[str, Any]


class EnterpriseFavoriteMatchIn(BaseModel):
    fixture: Dict[str, Any]
    reportType: Literal["pre_match", "post_match"] = "post_match"


class EnterpriseFavoriteMatchOut(BaseModel):
    favoriteId: str
    fixture: Dict[str, Any]
    reportType: Literal["pre_match", "post_match"] = "post_match"
    reportStatus: str = ""
    createdAt: datetime



class SaveMatchIn(BaseModel):
    fixtureId: int = Field(gt=0)
