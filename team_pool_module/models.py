from typing import Optional, List
from pydantic import BaseModel, Field

class MatchAnalysisOptionsIn(BaseModel):
    country: Optional[str] = None
    league: Optional[str] = None


class MatchAnalysisOptionsOut(BaseModel):
    countries: List[str]
    leagues: List[str]
    teams: List[str]


class TeamPoolSearchIn(BaseModel):
    team: Optional[str] = None
    country: Optional[str] = None
    league: Optional[str] = None
    limit: int = Field(default=50, ge=1, le=50)


class TeamPoolSearchRow(BaseModel):
    id: str
    name: str
    country: str
    league: str
    logoUrl: Optional[str] = None
    city: str
    coachName: str
    playerCount: int
    stadiumName: str
    stadiumImageUrl: Optional[str] = None
    leagueId: Optional[int] = None


