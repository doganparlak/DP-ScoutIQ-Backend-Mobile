
-- Scouting/Oyuncu Incele benchmarks: keep league peer data inside PostgreSQL.
-- No source-table rebuild, extensions, or background jobs required.
BEGIN;
CREATE OR REPLACE FUNCTION public.scoutwise_report_number_v1(v jsonb, signed_value boolean DEFAULT false)
RETURNS double precision LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE SET search_path=pg_catalog AS $$
DECLARE n double precision;
BEGIN
 IF v IS NULL OR jsonb_typeof(v) NOT IN ('number','string') THEN RETURN NULL; END IF;
 BEGIN n := (v #>> '{}')::double precision; EXCEPTION WHEN invalid_text_representation OR numeric_value_out_of_range THEN RETURN NULL; END;
 IF n::text IN ('NaN','Infinity','-Infinity') OR (NOT signed_value AND n<0) THEN RETURN NULL; END IF;
 RETURN n;
END $$;

-- Match Python round(float, digits), including halfway-to-even and binary ties.
CREATE OR REPLACE FUNCTION public.scoutwise_report_round_v1(v double precision,digits integer)
RETURNS double precision LANGUAGE plpgsql IMMUTABLE STRICT PARALLEL SAFE SET search_path=pg_catalog AS $$
DECLARE bytes bytea:=float8send(v); exponent integer; mantissa numeric; numerator numeric; denominator numeric;
 quotient numeric; remainder numeric; factor numeric; i integer; negative boolean; scaled float8;
BEGIN
 IF digits<0 OR digits>12 OR v::text IN ('NaN','Infinity','-Infinity') THEN RAISE EXCEPTION 'Invalid benchmark rounding input'; END IF;
 scaled:=abs(v)*power(10::float8,digits);
 IF abs((scaled-trunc(scaled))-0.5)>GREATEST(0.000001,scaled*0.000000000000001) THEN RETURN round(v::numeric,digits)::float8; END IF;
 negative:=(get_byte(bytes,0)&128)<>0;
 exponent:=((get_byte(bytes,0)&127)<<4)+(get_byte(bytes,1)>>4);
 mantissa:=(get_byte(bytes,1)&15);
 FOR i IN 2..7 LOOP mantissa:=mantissa*256+get_byte(bytes,i); END LOOP;
 IF exponent=0 THEN exponent:=-1074; ELSE mantissa:=mantissa+4503599627370496;exponent:=exponent-1075; END IF;
 factor:=power(10::numeric,digits);numerator:=mantissa*factor;denominator:=1;
 IF exponent>=0 THEN numerator:=numerator*power(2::numeric,exponent); ELSE denominator:=power(2::numeric,-exponent); END IF;
 quotient:=div(numerator,denominator);remainder:=mod(numerator,denominator);
 IF remainder*2>denominator OR (remainder*2=denominator AND mod(quotient,2)=1) THEN quotient:=quotient+1; END IF;
 IF negative THEN quotient:=-quotient; END IF;
 RETURN (quotient/factor)::float8;
END $$;

CREATE OR REPLACE FUNCTION public.scoutwise_report_peer_v1(stints jsonb)
RETURNS jsonb LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE SET search_path=pg_catalog,public AS $$
DECLARE
 counts_map CONSTANT jsonb := '{"Goals": "goals", "Assists": "assists", "Penalties Won": "penalties_won", "Touches": "touches", "Big Chances Created": "big_chances_created", "Dribble Attempts": "dribble_attempts", "Successful Dribbles": "successful_dribbles", "Man Of Match": "man_of_match", "Fouls Drawn": "fouls_drawn", "Offsides Provoked": "offsides_provoked", "Shots Total": "shots_total", "Shots On Target": "shots_on_target", "Expected Goals": "expected_goals", "Expected Goals On Target": "expected_goals_on_target", "Hit Woodwork": "hit_woodwork", "Penalties Scored": "penalties_scored", "Long Balls": "long_balls", "Long Balls Won": "long_balls_won", "Total Crosses": "total_crosses", "Accurate Crosses": "accurate_crosses", "Passes": "passes", "Accurate Passes": "accurate_passes", "Backward Passes": "backward_passes", "Key Passes": "key_passes", "Passes In Final Third": "passes_in_final_third", "Through Balls": "through_balls", "Through Balls Won": "through_balls_won", "Interceptions": "interceptions", "Tackles": "tackles", "Tackles Won": "tackles_won", "Last Man Tackle": "last_man_tackle", "Blocked Shots": "blocked_shots", "Clearances": "clearances", "Clearance Offline": "clearance_offline", "Ball Recovery": "ball_recovery", "Aerials": "aerials", "Aerials Won": "aerials_won", "Total Duels": "total_duels", "Duels Won": "duels_won", "Goals Conceded": "goals_conceded", "Penalties Committed": "penalties_committed", "Penalties Missed": "penalties_missed", "Shots Off Target": "shots_off_target", "Big Chances Missed": "big_chances_missed", "Aerials Lost": "aerials_lost", "Duels Lost": "duels_lost", "Fouls": "fouls", "Dispossessed": "dispossessed", "Dribbled Past": "dribbled_past", "Turn Over": "turn_over", "Possession Lost": "possession_lost", "Offsides": "offsides", "Own Goals": "own_goals", "Error Lead To Goal": "error_lead_to_goal", "Error Lead To Shot": "error_lead_to_shot", "Yellow Cards": "yellow_cards", "Yellow & Red Cards": "yellow_red_cards", "Red Cards": "red_cards", "Saves": "saves", "Saves Insidebox": "saves_insidebox", "Penalties Saved": "penalties_saved", "Punches": "punches", "Good High Claim": "good_high_claim"}'; ratios CONSTANT jsonb := '{"Accurate Passes (%)": ["accurate_passes", ["passes"], 20], "Long Balls Won (%)": ["long_balls_won", ["long_balls"], 10], "Successful Crosses (%)": ["accurate_crosses", ["total_crosses"], 10], "Dribble Accuracy (%)": ["successful_dribbles", ["dribble_attempts"], 10], "Tackles Won (%)": ["tackles_won", ["tackles"], 10], "Duels Won (%)": ["duels_won", ["total_duels"], 10], "Aerials Won (%)": ["aerials_won", ["aerials_won", "aerials_lost"], 10], "Shots On Target (%)": ["shots_on_target", ["shots_total"], 5], "Goal Conversion (%)": ["goals", ["shots_total"], 5], "On-Target to Goal Conversion (%)": ["goals", ["shots_on_target"], 5], "Shot Quality (%)": ["expected_goals", ["shots_total"], 5], "On-Target Shot Quality (%)": ["expected_goals_on_target", ["shots_on_target"], 5], "Assist Efficiency (%)": ["assists", ["key_passes"], 20]}';
 rates_map CONSTANT jsonb := '{"accurate_passes_percentage": ["accurate_passes", "passes"], "long_balls_won_percentage": ["long_balls_won", "long_balls"], "successful_crosses_percentage": ["accurate_crosses", "total_crosses"], "dribble_accuracy_percentage": ["successful_dribbles", "dribble_attempts"], "tackles_won_percentage": ["tackles_won", "tackles"], "duels_won_percentage": ["duels_won", "total_duels"], "aerials_won_percentage": ["aerials_won", "aerials"], "shots_on_target_percentage": ["shots_on_target", "shots_total"], "goal_conversion_percentage": ["goals", "shots_total"], "on_target_to_goal_conversion_percentage": ["goals", "shots_on_target"], "assist_efficiency_percentage": ["assists", "key_passes"], "shot_quality_percentage": ["expected_goals", "shots_total"], "on_target_shot_quality_percentage": ["expected_goals_on_target", "shots_on_target"]}'; aliases CONSTANT jsonb := '{"goalkeeper": "GK", "center midfield": "CM", "central midfield": "CM", "center defensive midfield": "CDM", "defensive midfield": "CDM", "center attacking midfield": "CAM", "attacking midfield": "CAM", "center back": "CB", "centre back": "CB", "left back": "LB", "right back": "RB", "left wing back": "LWB", "right wing back": "RWB", "left midfield": "LM", "right midfield": "RM", "center forward": "CF", "left wing": "LW", "right wing": "RW"}';
 role_codes CONSTANT jsonb := '["CAM", "CB", "CDM", "CF", "CM", "GK", "LAM", "LB", "LCB", "LCF", "LCM", "LDM", "LM", "LW", "LWB", "RAM", "RB", "RCB", "RCF", "RCM", "RDM", "RM", "RW", "RWB"]'; families CONSTANT jsonb := '{"central_midfield": ["CDM", "CM", "CAM"], "full_back": ["LB", "RB"], "centre_back": ["CB"], "goalkeeper": ["GK"], "centre_forward": ["CF"], "wide": ["LM", "RM", "LW", "RW"]}';
 role_group CONSTANT jsonb := '{"CDM": "central_midfield", "CM": "central_midfield", "CAM": "central_midfield", "LB": "full_back", "RB": "full_back", "CB": "centre_back", "GK": "goalkeeper", "CF": "centre_forward", "LM": "wide", "RM": "wide", "LW": "wide", "RW": "wide"}'; custom_rates CONSTANT jsonb := '["assist_efficiency_percentage", "on_target_shot_quality_percentage", "shot_quality_percentage"]'; matched_rates CONSTANT jsonb := '["on_target_shot_quality_percentage", "shot_quality_percentage"]';
 r jsonb; stats jsonb; state jsonb; prior jsonb; piece jsonb; evidence jsonb;
 roles jsonb:='{}'; groups jsonb:='{}'; metrics jsonb:='{}'; merged_rates jsonb:='{}'; positions jsonb:='{}';
 normalized jsonb; spec record; kv record; role record; roles_total double precision:=0;
 m double precision; t double precision; total_m double precision:=0; total_t double precision:=0;
 total double precision; value double precision; a double precision; b double precision;
 count_stints integer:=jsonb_array_length(stints); valid boolean; legacy boolean;
 k text; denom text; code text; family text; main_share double precision:=NULL; share double precision;
 missing boolean; invalid boolean;
BEGIN
 IF count_stints=0 THEN RETURN NULL; END IF;
 FOR r IN SELECT x FROM jsonb_array_elements(stints) x LOOP
  stats:=r->'stats'; m:=public.scoutwise_report_number_v1(r->'match_count'); t:=public.scoutwise_report_number_v1(stats->'minutes_played');
  IF m IS NULL OR m<=0 OR t IS NULL OR t<=0 THEN RETURN NULL; END IF;
  total_m:=total_m+m; total_t:=total_t+m*t;
  FOR kv IN SELECT * FROM jsonb_each(COALESCE(r->'position_counts','{}')) LOOP
   value:=public.scoutwise_report_number_v1(kv.value);
   IF value IS NOT NULL THEN positions:=jsonb_set(positions,ARRAY[kv.key],to_jsonb(COALESCE((positions->>kv.key)::float8,0)+value)); END IF;
  END LOOP;
  evidence:=stats->'_rate_inputs'; legacy:=COALESCE(evidence->>'version','')<>'1'; piece:='{}';
  IF NOT legacy THEN piece:=COALESCE(evidence->'rates','{}');
  ELSE
   -- Legacy evidence is reconstructed exactly as apply_merged_rates/add_sample.
   normalized:='{}';
   FOR kv IN SELECT * FROM jsonb_each(stats) LOOP
    k:=regexp_replace(lower(regexp_replace(kv.key,'^stat_','')), '%',' percentage ','g');
    k:=trim(both '_' FROM regexp_replace(k,'[^a-z0-9]+','_','g'));
    k:=CASE k WHEN 'aeriels_won' THEN 'aerials_won' WHEN 'aeriels_lost' THEN 'aerials_lost'
       WHEN 'accurate_crosses_percentage' THEN 'successful_crosses_percentage'
       WHEN 'on_target_conversion_percentage' THEN 'on_target_to_goal_conversion_percentage'
       WHEN 'on_target_goal_conversion_percentage' THEN 'on_target_to_goal_conversion_percentage'
       WHEN 'expected_goals_xg' THEN 'expected_goals' WHEN 'expected_goals_on_target_xgot' THEN 'expected_goals_on_target' ELSE k END;
    value:=public.scoutwise_report_number_v1(kv.value);
    IF value IS NOT NULL AND NOT rates_map ? k THEN normalized:=jsonb_set(normalized,ARRAY[k],to_jsonb(value*m)); END IF;
   END LOOP;
   FOR spec IN SELECT * FROM jsonb_each(rates_map) LOOP
    IF matched_rates ? spec.key THEN CONTINUE; END IF;
    a:=public.scoutwise_report_number_v1(normalized->(spec.value->>0)); b:=public.scoutwise_report_number_v1(normalized->(spec.value->>1));
    missing:=false; invalid:=false;
    IF spec.key='aerials_won_percentage' AND b IS NULL AND a IS NOT NULL AND normalized ? 'aerials_lost' THEN b:=a+(normalized->>'aerials_lost')::float8; END IF;
    IF b IS NULL THEN missing:=a IS NOT NULL AND a>0; a:=0; b:=0;
    ELSIF a IS NULL THEN
     IF normalized ? (spec.value->>0) THEN invalid:=true; ELSE a:=0; END IF;
    END IF;
    IF a IS NULL THEN a:=0; b:=0; END IF;
    IF NOT custom_rates ? spec.key AND a>b THEN invalid:=true; END IF;
    piece:=jsonb_set(piece,ARRAY[spec.key],jsonb_build_object('numerator',a,'denominator',b,'missing_attempts',missing,'invalid',invalid));
   END LOOP;
  END IF;
  FOR kv IN SELECT * FROM jsonb_each(piece) LOOP
   prior:=merged_rates->kv.key; state:=kv.value;
   merged_rates:=jsonb_set(merged_rates,ARRAY[kv.key],jsonb_build_object(
    'numerator',COALESCE((prior->>'numerator')::float8,0)+COALESCE((state->>'numerator')::float8,0),
    'denominator',COALESCE((prior->>'denominator')::float8,0)+COALESCE((state->>'denominator')::float8,0),
    'missing_attempts',COALESCE((prior->>'missing_attempts')::boolean,false) OR COALESCE((state->>'missing_attempts')::boolean,false),
    'invalid',COALESCE((prior->>'invalid')::boolean,false) OR COALESCE((state->>'invalid')::boolean,false)));
  END LOOP;
 END LOOP;
 total_t:=public.scoutwise_report_round_v1(total_t,8);
 FOR spec IN SELECT * FROM jsonb_each(counts_map) LOOP
  k:=spec.value #>> '{}'; total:=0; valid:=true;
  FOR r IN SELECT x FROM jsonb_array_elements(stints) x LOOP
   value:=public.scoutwise_report_number_v1(r->'stats'->k);
   IF value IS NULL THEN valid:=false; EXIT; END IF;
   total:=total+value*(r->>'match_count')::float8;
  END LOOP;
  IF valid AND k NOT IN ('expected_goals','expected_goals_on_target') THEN
   FOR kv IN SELECT * FROM jsonb_each(merged_rates) LOOP
    IF COALESCE((kv.value->>'missing_attempts')::boolean,false) AND rates_map->kv.key->>1=k THEN valid:=false; EXIT; END IF;
   END LOOP;
  END IF;
  IF valid THEN metrics:=jsonb_set(metrics,ARRAY[spec.key],to_jsonb(public.scoutwise_report_round_v1(total,8)*90/total_t)); END IF;
 END LOOP;
 FOR spec IN SELECT * FROM jsonb_each(ratios) LOOP
  k:=lower(regexp_replace(spec.key,'%',' percentage ','g')); k:=trim(both '_' FROM regexp_replace(k,'[^a-z0-9]+','_','g'));
  state:=merged_rates->k;
  a:=(state->>'numerator')::float8; b:=(state->>'denominator')::float8;
  IF state IS NOT NULL AND NOT COALESCE((state->>'missing_attempts')::boolean,false)
   AND NOT COALESCE((state->>'invalid')::boolean,false) AND a>=0 AND b>0 AND b>=(spec.value->>2)::float8 THEN
   value:=a/b*100;
   IF custom_rates ? k THEN value:=LEAST(100,value); END IF;
   metrics:=jsonb_set(metrics,ARRAY[spec.key],to_jsonb(value));
  END IF;
 END LOOP;
 IF count_stints=1 THEN
  value:=public.scoutwise_report_number_v1(stints->0->'stats'->'rating');
  IF value BETWEEN 0 AND 10 THEN metrics:=jsonb_set(metrics,ARRAY['Rating'],to_jsonb(public.scoutwise_report_round_v1(value*total_m,8)/total_m)); END IF;
 END IF;
 total:=0;valid:=true;
 FOR r IN SELECT x FROM jsonb_array_elements(stints) x LOOP
  value:=public.scoutwise_report_number_v1(r->'stats'->'shooting_performance',true);
  IF value IS NULL THEN valid:=false; EXIT; END IF;
  total:=total+value*(r->>'match_count')::float8;
 END LOOP;
 IF valid THEN metrics:=jsonb_set(metrics,ARRAY['Shooting Performance'],to_jsonb(total/total_m)); END IF;
 normalized:='{}';
 FOR kv IN SELECT * FROM jsonb_each(positions) LOOP
  code:=COALESCE(aliases->>lower(kv.key),upper(kv.key)); value:=public.scoutwise_report_number_v1(kv.value);
  IF role_codes ? code AND value>0 THEN normalized:=jsonb_set(normalized,ARRAY[code],to_jsonb(COALESCE((normalized->>code)::float8,0)+value));roles_total:=roles_total+value; END IF;
 END LOOP;
 IF roles_total>0 THEN
  FOR role IN SELECT e.key,e.value::text::float8 AS n FROM jsonb_each(normalized) e ORDER BY e.value::text::float8 DESC,e.key LIMIT 2 LOOP
   share:=role.n/roles_total*100;
   IF main_share IS NULL THEN main_share:=share; END IF;
   IF main_share-share<=20+1e-8 THEN
    family:=COALESCE(role_group->>role.key,role.key); groups:=jsonb_set(groups,ARRAY[family],'true');
   END IF;
  END LOOP;
 END IF;
 RETURN jsonb_build_object('minutes',total_t,'groups',groups,'metrics',metrics);
END $$;

-- Compact database-side peer profiles; a statement-level source revision invalidates changed data.
CREATE TABLE IF NOT EXISTS public.scoutwise_report_peer_cache_v1 (
 league_id bigint NOT NULL,player_id bigint NOT NULL,gender text NOT NULL,
 source_hash text NOT NULL,algorithm_version integer NOT NULL,profile jsonb,
 PRIMARY KEY(league_id,player_id,gender)
);
ALTER TABLE public.scoutwise_report_peer_cache_v1 ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.scoutwise_report_peer_cache_v1 FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE,DELETE ON public.scoutwise_report_peer_cache_v1 TO service_role,CURRENT_USER;

-- Statement-level invalidation is constant-time per import batch, including TRUNCATE.
CREATE TABLE IF NOT EXISTS public.scoutwise_report_source_revision_v1 (
 singleton boolean PRIMARY KEY DEFAULT true CHECK(singleton),revision bigint NOT NULL DEFAULT 1
);
INSERT INTO public.scoutwise_report_source_revision_v1(singleton) VALUES(true) ON CONFLICT DO NOTHING;
CREATE TABLE IF NOT EXISTS public.scoutwise_report_peer_leagues_v1 (
 league_id bigint PRIMARY KEY,source_revision bigint NOT NULL,algorithm_version integer NOT NULL
);
ALTER TABLE public.scoutwise_report_source_revision_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.scoutwise_report_peer_leagues_v1 ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.scoutwise_report_source_revision_v1,public.scoutwise_report_peer_leagues_v1 FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE,DELETE ON public.scoutwise_report_source_revision_v1,public.scoutwise_report_peer_leagues_v1 TO service_role,CURRENT_USER;

CREATE OR REPLACE FUNCTION public.scoutwise_report_source_changed_v1()
RETURNS trigger LANGUAGE plpgsql SECURITY DEFINER SET search_path=pg_catalog,public AS $$
BEGIN
 UPDATE public.scoutwise_report_source_revision_v1 SET revision=revision+1 WHERE singleton;
 RETURN NULL;
END $$;
DROP TRIGGER IF EXISTS scoutwise_report_source_changed_v1 ON public.player_comp_data;
CREATE TRIGGER scoutwise_report_source_changed_v1 AFTER INSERT OR UPDATE OR DELETE OR TRUNCATE
 ON public.player_comp_data FOR EACH STATEMENT EXECUTE FUNCTION public.scoutwise_report_source_changed_v1();
REVOKE ALL ON FUNCTION public.scoutwise_report_source_changed_v1() FROM PUBLIC,anon,authenticated;

CREATE TABLE IF NOT EXISTS public.scoutwise_report_metric_distributions_v1 (
 league_id bigint NOT NULL,gender text NOT NULL,group_key text NOT NULL,label text NOT NULL,
 metric_values double precision[] NOT NULL,PRIMARY KEY(league_id,gender,group_key,label)
);
CREATE TABLE IF NOT EXISTS public.scoutwise_report_peer_groups_v1 (
 league_id bigint NOT NULL,gender text NOT NULL,group_key text NOT NULL,peer_count bigint NOT NULL,
 PRIMARY KEY(league_id,gender,group_key)
);
ALTER TABLE public.scoutwise_report_metric_distributions_v1 ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.scoutwise_report_peer_groups_v1 ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.scoutwise_report_metric_distributions_v1,public.scoutwise_report_peer_groups_v1 FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE,DELETE ON public.scoutwise_report_metric_distributions_v1,public.scoutwise_report_peer_groups_v1 TO service_role,CURRENT_USER;

CREATE OR REPLACE FUNCTION public.scoutwise_report_bound_v1(a double precision[],v double precision,inclusive boolean)
RETURNS integer LANGUAGE plpgsql IMMUTABLE PARALLEL SAFE SET search_path=pg_catalog AS $$
DECLARE lo integer:=1;hi integer:=COALESCE(cardinality(a),0)+1;mid integer;
BEGIN
 WHILE lo<hi LOOP
  mid:=lo+(hi-lo)/2;
  IF a[mid]<v OR (inclusive AND a[mid]=v) THEN lo:=mid+1; ELSE hi:=mid; END IF;
 END LOOP;
 RETURN lo-1;
END $$;

CREATE OR REPLACE FUNCTION public.scoutwise_refresh_report_peers_v1(league_ids bigint[])
RETURNS void LANGUAGE plpgsql VOLATILE SET search_path=pg_catalog,public AS $$
DECLARE league bigint; current_revision bigint; cached_revision bigint; candidate record;
 stints jsonb; source_hash text; old_hash text; old_version integer; profile jsonb; cohort record;
BEGIN
 SELECT revision INTO STRICT current_revision FROM public.scoutwise_report_source_revision_v1 WHERE singleton;
 FOR league IN SELECT DISTINCT id FROM unnest(league_ids) id ORDER BY id LOOP
  SELECT l.source_revision INTO cached_revision FROM public.scoutwise_report_peer_leagues_v1 l
   WHERE l.league_id=league AND l.algorithm_version=2;
  IF cached_revision=current_revision THEN CONTINUE; END IF;
  PERFORM pg_advisory_xact_lock(hashtextextended('scoutwise-report-peers-v1:'||league::text,0));
  -- A concurrent request may already have completed this revision while we waited.
  SELECT l.source_revision INTO cached_revision FROM public.scoutwise_report_peer_leagues_v1 l
   WHERE l.league_id=league AND l.algorithm_version=2;
  IF cached_revision=current_revision THEN CONTINUE; END IF;
  -- Stream player identities, then read at most one player's stints at a time.
  FOR candidate IN SELECT d.player_id,d.gender FROM public.player_comp_data d
   WHERE d.league_id=league AND d.league_type='league' AND d.league_sub_type='domestic'
    AND d.gender IS NOT NULL AND d.gender<>'' GROUP BY d.player_id,d.gender ORDER BY d.player_id,d.gender LOOP
   SELECT jsonb_agg(jsonb_build_object('match_count',d.match_count,'position_counts',d.position_counts,'stats',d.stats) ORDER BY d.team_id)
    INTO stints FROM public.player_comp_data d WHERE d.league_id=league AND d.player_id=candidate.player_id
    AND d.gender=candidate.gender AND d.league_type='league' AND d.league_sub_type='domestic';
   source_hash:=md5(stints::text);
   SELECT c.source_hash,c.algorithm_version INTO old_hash,old_version FROM public.scoutwise_report_peer_cache_v1 c
    WHERE c.league_id=league AND c.player_id=candidate.player_id AND c.gender=candidate.gender;
   IF old_hash IS NOT DISTINCT FROM source_hash AND old_version=1 THEN CONTINUE; END IF;
   profile:=public.scoutwise_report_peer_v1(stints);
   IF profile IS NOT NULL THEN
    profile:=jsonb_set(profile,'{metrics}',COALESCE((SELECT jsonb_object_agg(m.key,to_jsonb(public.scoutwise_report_round_v1((m.value #>> '{}')::float8,6)))
     FROM jsonb_each(profile->'metrics') m),'{}'));
   END IF;
   INSERT INTO public.scoutwise_report_peer_cache_v1(league_id,player_id,gender,source_hash,algorithm_version,profile)
    VALUES(league,candidate.player_id,candidate.gender,source_hash,1,profile)
    ON CONFLICT(league_id,player_id,gender) DO UPDATE SET source_hash=EXCLUDED.source_hash,
     algorithm_version=EXCLUDED.algorithm_version,profile=EXCLUDED.profile;
  END LOOP;
  DELETE FROM public.scoutwise_report_peer_cache_v1 c WHERE c.league_id=league AND NOT EXISTS(
   SELECT 1 FROM public.player_comp_data d WHERE d.league_id=c.league_id AND d.player_id=c.player_id
    AND d.gender=c.gender AND d.league_type='league' AND d.league_sub_type='domestic');
  DELETE FROM public.scoutwise_report_metric_distributions_v1 d WHERE d.league_id=league;
  DELETE FROM public.scoutwise_report_peer_groups_v1 g WHERE g.league_id=league;
  FOR cohort IN SELECT c.gender,g.key AS group_key,count(*) AS peer_count
   FROM public.scoutwise_report_peer_cache_v1 c CROSS JOIN LATERAL jsonb_object_keys(c.profile->'groups') g(key)
   WHERE c.league_id=league AND (c.profile->>'minutes')::float8>=300 GROUP BY c.gender,g.key LOOP
   INSERT INTO public.scoutwise_report_peer_groups_v1 VALUES(league,cohort.gender,cohort.group_key,cohort.peer_count);
   INSERT INTO public.scoutwise_report_metric_distributions_v1(league_id,gender,group_key,label,metric_values)
   SELECT league,cohort.gender,cohort.group_key,m.key,array_agg((m.value #>> '{}')::float8 ORDER BY (m.value #>> '{}')::float8)
   FROM public.scoutwise_report_peer_cache_v1 c CROSS JOIN LATERAL jsonb_each(c.profile->'metrics') m
   WHERE c.league_id=league AND c.gender=cohort.gender AND (c.profile->>'minutes')::float8>=300
    AND c.profile->'groups' ? cohort.group_key GROUP BY m.key;
  END LOOP;
  INSERT INTO public.scoutwise_report_peer_leagues_v1(league_id,source_revision,algorithm_version) VALUES(league,current_revision,2)
   ON CONFLICT(league_id) DO UPDATE SET source_revision=EXCLUDED.source_revision,algorithm_version=EXCLUDED.algorithm_version;
 END LOOP;
END $$;

CREATE OR REPLACE FUNCTION public.scoutwise_report_benchmarks_v1(target_player_id bigint,sources jsonb)
RETURNS jsonb LANGUAGE plpgsql STABLE PARALLEL SAFE SET search_path=pg_catalog,public AS $$
DECLARE scope jsonb; source jsonb; own_profile jsonb; group_row record; metric_row record;
 output jsonb:='{}'; scope_result jsonb; group_result jsonb; metric_result jsonb; memo jsonb:='{}'; memo_key text;
 source_league bigint; source_gender text; source_minutes float8; selected_group text;
 eligible boolean; own_eligible boolean; remove_own boolean; add_source boolean;
 cohort_size bigint; n bigint; less bigint; equal bigint; size integer;
 v float8; own_value float8; low_value float8; high_value float8; percentile float8; a float8[];
BEGIN
 FOR scope IN SELECT x FROM jsonb_array_elements(sources) x LOOP
  source:=scope->'profile';source_league:=(scope->>'leagueId')::bigint;
  source_gender:=source->>'gender';source_minutes:=(source->>'minutes')::float8;
  eligible:=COALESCE(source_minutes>=300 AND source_gender<>'',false);scope_result:='{}';
  SELECT c.profile INTO own_profile FROM public.scoutwise_report_peer_cache_v1 c
   WHERE c.league_id=source_league AND c.player_id=target_player_id AND c.gender=source_gender;
  FOR group_row IN SELECT g.key,g.value FROM jsonb_each(source->'comparisonGroups') g LOOP
   selected_group:=group_row.key;
   memo_key:=md5(jsonb_build_array(source_league,selected_group,source-'comparisonGroups')::text);
   IF memo ? memo_key THEN scope_result:=jsonb_set(scope_result,ARRAY[selected_group],memo->memo_key);CONTINUE; END IF;
   own_eligible:=COALESCE((own_profile->>'minutes')::float8>=300 AND own_profile->'groups' ? selected_group,false);
   SELECT g.peer_count INTO cohort_size FROM public.scoutwise_report_peer_groups_v1 g
    WHERE g.league_id=source_league AND g.gender=source_gender AND g.group_key=selected_group;
   cohort_size:=COALESCE(cohort_size,0)-(own_eligible::integer)+(eligible::integer);metric_result:='{}';
   FOR metric_row IN SELECT m.key,m.value,d.metric_values FROM jsonb_each(source->'metrics') m
    LEFT JOIN public.scoutwise_report_metric_distributions_v1 d ON d.league_id=source_league
     AND d.gender=source_gender AND d.group_key=selected_group AND d.label=m.key LOOP
    a:=metric_row.metric_values;size:=COALESCE(cardinality(a),0);
    v:=public.scoutwise_report_round_v1((metric_row.value->>'value')::float8,6);
    own_value:=public.scoutwise_report_number_v1(own_profile->'metrics'->metric_row.key,true);
    remove_own:=own_eligible AND own_profile->'metrics' ? metric_row.key;
    add_source:=eligible AND (metric_row.value->>'rankable')::boolean;
    n:=size-(remove_own::integer)+(add_source::integer);
    less:=public.scoutwise_report_bound_v1(a,v,false);
    equal:=public.scoutwise_report_bound_v1(a,v,true)-less;
    IF remove_own THEN
     IF own_value<v THEN less:=less-1; ELSIF own_value=v THEN equal:=equal-1; END IF;
    END IF;
    IF add_source THEN equal:=equal+1; END IF;
    low_value:=a[1];high_value:=a[size];
    IF remove_own THEN
     IF size=1 THEN low_value:=NULL;high_value:=NULL;
     ELSE
      IF low_value=own_value THEN low_value:=a[2]; END IF;
      IF high_value=own_value THEN high_value:=a[size-1]; END IF;
     END IF;
    END IF;
    IF add_source THEN low_value:=LEAST(low_value,v);high_value:=GREATEST(high_value,v); END IF;
    percentile:=NULL;
    IF add_source AND n>=5 AND low_value<>high_value THEN
     percentile:=100.0::float8*(2*less+equal)::float8/(2*n)::float8;
     IF (metric_row.value->>'lowerIsBetter')::boolean THEN percentile:=100-percentile; END IF;
     percentile:=public.scoutwise_report_round_v1(percentile,1);
    END IF;
    metric_result:=jsonb_set(metric_result,ARRAY[metric_row.key],metric_row.value||jsonb_build_object('peerCount',n,'percentile',percentile));
   END LOOP;
   group_result:=group_row.value||jsonb_build_object('peerCount',cohort_size,'metrics',metric_result);
   scope_result:=jsonb_set(scope_result,ARRAY[selected_group],group_result);memo:=jsonb_set(memo,ARRAY[memo_key],group_result);
  END LOOP;
  output:=jsonb_set(output,ARRAY[scope->>'key'],scope_result);
 END LOOP;
 RETURN output;
END $$;
REVOKE ALL ON FUNCTION public.scoutwise_report_bound_v1(double precision[],double precision,boolean), public.scoutwise_refresh_report_peers_v1(bigint[]), public.scoutwise_report_number_v1(jsonb,boolean), public.scoutwise_report_round_v1(double precision,integer), public.scoutwise_report_peer_v1(jsonb), public.scoutwise_report_benchmarks_v1(bigint,jsonb) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.scoutwise_report_bound_v1(double precision[],double precision,boolean), public.scoutwise_refresh_report_peers_v1(bigint[]), public.scoutwise_report_number_v1(jsonb,boolean), public.scoutwise_report_round_v1(double precision,integer), public.scoutwise_report_peer_v1(jsonb), public.scoutwise_report_benchmarks_v1(bigint,jsonb) TO service_role,CURRENT_USER;
COMMIT;
