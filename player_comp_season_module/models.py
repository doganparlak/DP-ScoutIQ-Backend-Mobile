from typing import Dict, List, Optional
from pydantic import BaseModel, Field

class PlayerCompSeasonSearchIn(BaseModel):
    query: str = Field(min_length=2, max_length=120)
    nationality: Optional[str] = Field(default=None, max_length=120)
    limit: int = Field(default=20, ge=1, le=50)


class PlayerCompSeasonOptionsOut(BaseModel):
    nationalities: List[str] = Field(default_factory=list)


class PlayerCompSeasonCandidateOut(BaseModel):
    playerId: int
    displayName: str
    matchedAlias: str = ""
    nationality: str = ""
    latestTeam: str = ""
    latestPosition: str = ""
    latestSeason: str = ""
    firstSeason: str = ""
    rowCount: int = 0


class PlayerCompSeasonPlayerOut(BaseModel):
    playerId: int
    displayName: str
    imageUrl: Optional[str] = None
    nationality: str = ""
    gender: str = ""
    latestTeam: str = ""
    latestSeason: str = ""


class PlayerCompSeasonRowOut(BaseModel):
    key: str
    playerId: int
    teamId: int
    teamName: str = ""
    leagueId: int
    leagueName: str = ""
    leagueType: str = ""
    leagueSubType: str = ""
    country: str = ""
    leagueShortCode: str = ""
    leagueImagePath: str = ""
    seasonId: int
    seasonName: str = ""
    matchCount: int = 0
    positionName: str = ""
    positionCounts: Dict[str, float] = Field(default_factory=dict)
    age: Optional[int] = None
    height: Optional[float] = None
    weight: Optional[float] = None


class PlayerCompSeasonRowsOut(BaseModel):
    player: PlayerCompSeasonPlayerOut
    rows: List[PlayerCompSeasonRowOut]


class PlayerCompSeasonSourceIn(BaseModel):
    teamId: int
    leagueId: int
    seasonId: int


class PlayerCompSeasonAggregateIn(BaseModel):
    playerId: int
    sources: List[PlayerCompSeasonSourceIn] = Field(min_length=1, max_length=200)


class PlayerCompSeasonAggregateOut(BaseModel):
    playerId: int
    displayName: str
    nationality: str = ""
    gender: str = ""
    age: Optional[int] = None
    height: Optional[float] = None
    weight: Optional[float] = None
    selectedRowCount: int
    matchCount: int
    seasons: List[str] = Field(default_factory=list)
    teams: List[str] = Field(default_factory=list)
    competitions: List[str] = Field(default_factory=list)
    positionCounts: Dict[str, float] = Field(default_factory=dict)
    stats: Dict[str, float] = Field(default_factory=dict)
    selectedRows: List[PlayerCompSeasonRowOut] = Field(default_factory=list)


