"""Enterprise similarity evidence and eligibility, without discovery AI."""
from __future__ import annotations
from datetime import date
import math
from collections import defaultdict
from pydantic import BaseModel, Field, ConfigDict, model_validator, field_validator
from constants_module.constants import ROLE_SHORT_TO_LONG
ROLE_NAMES = {
    'goalkeeper': 'GK', 'center midfield': 'CM', 'central midfield': 'CM',
    'center defensive midfield': 'CDM', 'defensive midfield': 'CDM',
    'center attacking midfield': 'CAM', 'attacking midfield': 'CAM',
    'center back': 'CB', 'centre back': 'CB', 'left back': 'LB', 'right back': 'RB',
    'left wing back': 'LWB', 'right wing back': 'RWB', 'left midfield': 'LM',
    'right midfield': 'RM', 'center forward': 'CF', 'left wing': 'LW', 'right wing': 'RW',
}
ROLE_CODES = {'GK', 'CM', 'CDM', 'CAM', 'CB', 'LCB', 'RCB', 'LB', 'RB', 'LWB', 'RWB', 'LM', 'RM', 'LCM', 'RCM', 'LDM', 'RDM', 'LAM', 'RAM', 'CF', 'LCF', 'RCF', 'LW', 'RW'}


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def eligible_roles(metadata):
    counts = metadata.get('position_counts') or {}
    normalized = defaultdict(float)
    for name, value in counts.items():
        code = ROLE_NAMES.get(str(name).lower(), str(name).upper())
        count = number(value)
        if code in ROLE_CODES and count is not None and count > 0:
            normalized[code] += count
    if not normalized:
        code = str(metadata.get('primary_position_code') or '').upper()
        return {code: 100.0} if code in ROLE_CODES else {}
    total = sum(normalized.values())
    shares = sorted(((key, value / total * 100) for key, value in normalized.items()), key=lambda item: (-item[1], item[0]))
    # Only the primary and secondary role; threshold is inclusive, in percentage points.
    return {key: round(value, 2) for key, value in shares[:2] if shares[0][1] - value <= 20 + 1e-8}


# Explicit, reviewable feature bundles. Minus-prefixed metrics are lower-is-better.
# Aliases are resolved below and never counted twice.
CATEGORY_DEFINITIONS = [
 ('impact','Hücum katkısı','Attacking contribution',[
  ('creation','Pozisyon yaratma','Chance creation',['Chances Created','Big Chances Created']),
  ('dribbling','Dripling katkısı','Dribbling',['Dribble Attempts','Successful Dribbles','Dribble Accuracy (%)']),
  ('involvement','Oyuna katılım','Involvement',['Touches','Fouls Drawn','Penalties Won'])]),
 ('shooting','Şut','Shooting',[
  ('threat','Şut üretimi ve isabeti','Shot production and accuracy',['Shots Total','Shots On Target','Shots On Target (%)','-Shots Off Target']),
  ('quality','Şut kalitesi','Shot quality',['Expected Goals','Expected Goals On Target','Shot Quality (%)','On-Target Shot Quality (%)']),
  ('finishing','Bitiricilik','Finishing',['Goals','Goal Conversion (%)','On-Target to Goal Conversion (%)','Shooting Performance','-Big Chances Missed']),
  ('penalties','Penaltı bitiriciliği','Penalty finishing',['Penalties Scored','-Penalties Missed'])]),
 ('passing','Pas','Passing',[
  ('connection','Pas bağlantısı ve isabeti','Passing involvement and accuracy',['Passes','Accurate Passes','Accurate Passes (%)']),
  ('creative','Yaratıcı pas ve son bölge bağlantısı','Creative passing and final-third links',['Key Passes','Passes In Final Third','Through Balls','Through Balls Won','Assists','Assist Efficiency (%)']),
  ('long','Uzun pas','Long passing',['Long Balls','Long Balls Won','Long Balls Won (%)']),
  ('crossing','Orta üretimi','Crossing',['Total Crosses','Accurate Crosses','Successful Crosses (%)'])]),
 ('defending','Savunma','Defending',[
  ('recovery','Top kazanma','Ball winning',['Tackles','Tackles Won','Tackles Won (%)','Interceptions','Ball Recovery']),
  ('blocking','Şut engelleme ve uzaklaştırma','Shot blocking and clearances',['Blocked Shots','Clearances','Last Man Tackle','Clearance Offline']),
  ('duels','İkili mücadele','Duels',['Total Duels','Duels Won','Duels Won (%)','-Duels Lost','-Dribbled Past']),
  ('aerial','Hava mücadelesi','Aerial duels',['Aerials','Aerials Won','Aerials Won (%)','-Aerials Lost']),
  ('offsides','Rakibi ofsayta düşürme','Catching opponents offside',['Offsides Provoked'])]),
 ('security','Top güvenliği ve disiplin','Ball security and discipline',[
  ('retention','Topu koruma','Ball retention',['-Possession Lost','-Dispossessed','-Turn Over']),
  ('errors','Kritik hatalardan kaçınma','Avoiding critical errors',['-Error Lead To Shot','-Error Lead To Goal','-Own Goals']),
  ('discipline','Disiplin','Discipline',['-Fouls','-Yellow Cards','-Yellow & Red Cards','-Red Cards','-Penalties Committed','-Offsides'])]),
 ('goalkeeping','Kalecilik','Goalkeeping',[
  ('saves','Şut kurtarma','Shot stopping',['Saves','Saves Insidebox']),
  ('claims','Hava topu müdahalesi','Aerial interventions',['Good High Claim','Punches']),
  ('penalties','Penaltı kurtarma','Penalty saves',['Penalties Saved'])]),
]
BUNDLES = {f'{cat}.{key}': metrics for cat, _, _, groups in CATEGORY_DEFINITIONS for key, _, _, metrics in groups}
CATEGORIES = {cat: [f'{cat}.{key}' for key, _, _, _ in groups] for cat, _, _, groups in CATEGORY_DEFINITIONS}
# These metrics inform final tactical interpretation, without assuming higher/lower is better.
CONTEXT_BUNDLES = {'passing.connection':['Backward Passes'], 'shooting.threat':['Hit Woodwork'], 'goalkeeping.saves':['Goals Conceded']}
CONTEXT_METRICS = {metric for names in CONTEXT_BUNDLES.values() for metric in names}
ALIASES = {'Goals Conceded':['Goalkeeper Goals Conceded','Goals Conceded.1'], 'Blocked Shots':['Shots Blocked'], 'On-Target to Goal Conversion (%)':['On Target Goal Conversion (%)'], 'Successful Crosses (%)':['Accurate Crosses (%)','Successful Crosses Percentage'], 'Accurate Passes (%)':['Accurate Passes Percentage'], 'Tackles Won (%)':['Tacles Won Percentage']}


# Include every ranking, duplicate-selection, diversity and AI evidence input.
# Photos and unused profile fields are fetched only for the final selections.
DISCOVERY_METADATA_FIELDS = sorted({
    'player_id', 'player_name', 'name', 'team_name', 'age',
    'position_counts', 'primary_position_code', 'Minutes Played', 'match_count',
    *(metric.lstrip('-') for metrics in BUNDLES.values() for metric in metrics),
    *CONTEXT_METRICS,
    *(alias for aliases in ALIASES.values() for alias in aliases),
})


def discovery_config():
    return {'categories':[{'key':cat,'tr':tr,'en':en,'groups':[{'key':f'{cat}.{key}','tr':gtr,'en':gen,'metrics':[metric.lstrip('-') for metric in metrics], 'contextMetrics':CONTEXT_BUNDLES.get(f'{cat}.{key}',[])} for key,gtr,gen,metrics in groups]} for cat,tr,en,groups in CATEGORY_DEFINITIONS]}


class DiscoveryFilters(BaseModel):
    model_config = ConfigDict(extra='forbid', str_strip_whitespace=True)
    nationality: list[str] = Field(default_factory=list, max_length=50)
    team: list[str] = Field(default_factory=list, max_length=50)
    league: list[str] = Field(default_factory=list, max_length=50)
    minAge: int | None = Field(default=None, ge=14, le=60)
    maxAge: int | None = Field(default=None, ge=14, le=60)
    contractStatus: str = ''
    loanEndDate: date | None = None
    contractEndDate: date | None = None

    @field_validator('nationality','team','league',mode='before')
    @classmethod
    def selection_list(cls,value):
        if isinstance(value,str): value=[value] if value.strip() else []
        if not isinstance(value,list): raise ValueError('Expected selections')
        result=[]
        for item in value:
            if not isinstance(item,str) or len(item)>150: raise ValueError('Invalid selection')
            item=item.strip()
            if item and item not in result: result.append(item)
        return result

    @model_validator(mode='after')
    def validate_filters(self):
        if self.minAge is not None and self.maxAge is not None and self.minAge > self.maxAge:
            raise ValueError('Invalid age range')
        if self.contractStatus not in ('','loan','permanent'):
            raise ValueError('Invalid contract status')
        if self.contractStatus == 'permanent' and self.loanEndDate:
            raise ValueError('Permanent contract cannot have a loan end filter')
        return self


def player_metrics(metadata, include_context=False):
    minutes = number(metadata.get('Minutes Played'))
    matches = number(metadata.get('match_count'))
    # Stored player_data action metrics and minutes are per-match averages.
    if not minutes or minutes <= 0 or not matches or minutes * matches < 90:
        return {}
    result={}
    names={item.lstrip('-') for bundle in BUNDLES.values() for item in bundle}
    if include_context: names |= CONTEXT_METRICS
    for metric in names:
        value=number(metadata.get(metric))
        if value is None:
            value=next((number(metadata.get(alias)) for alias in ALIASES.get(metric,[]) if number(metadata.get(alias)) is not None),None)
        if value is not None:
            result[metric]=value if '%' in metric else value*90/minutes
    return result


def discovery_roles(metadata):
    names={value.lower():key for key,value in ROLE_SHORT_TO_LONG.items()}
    counts={}
    for name,value in (metadata.get('position_counts') or {}).items():
        code=names.get(str(name).strip().lower(),str(name).strip().upper())
        count=number(value)
        if count is not None: counts[code]=counts.get(code,0)+count
    return eligible_roles({**metadata,'position_counts':counts})


