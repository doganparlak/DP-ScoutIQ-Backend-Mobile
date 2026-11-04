import copy
from datetime import datetime,timezone
import unittest
from unittest.mock import patch,MagicMock
from standings_module import comparison_ranks as ranks
from standings_module.metrics import Aggregate

class ComparisonRankTests(unittest.TestCase):
    def setUp(self):
        self.now=datetime(2026,11,4,12,tzinfo=timezone.utc)
        self.fixture={'startingAt':'2026-11-05T20:00:00Z','league':{'id':2,'name':'Champions League'},'season':{'id':28155,'name':'2026/2027'}}
        self.usage={'teams':[{'team_id':1,'sample_size':10,'team_comparison':{'shooting':[{'name':'Shots Total','value':4.2,'total':42,'matches_covered':10}]}}]}

    def test_ranks_use_whole_season_not_last_ten_and_preserve_recent_values(self):
        agg=Aggregate()
        for i in range(15):
            agg.add({'included_in_aggregation':True,'kickoff_at':self.now,'player_contributions':[],
                     'team_contributions':{'1':{'name':'One','metrics':{'Shots Total':30 if i<5 else 1}},'2':{'name':'Two','metrics':{'Shots Total':5}}}})
        result=ranks.rank_metrics(agg.finish()['team_metrics'])
        self.assertEqual(result['1']['Shots Total']['position'],1)
        self.assertEqual(result['1']['Shots Total']['matches_played'],15)
        before=copy.deepcopy(self.usage)
        with patch.object(ranks,'competition_ranks',return_value=result)as load:
            enriched=ranks.enrich_comparison_ranks(self.usage,self.fixture,now=self.now)
        load.assert_called_once_with(2,28155,self.now,'overall')
        metric=enriched['teams'][0]['team_comparison']['shooting'][0]
        self.assertEqual(metric['value'],4.2);self.assertEqual(metric['total'],42);self.assertEqual(metric['matches_covered'],10)
        self.assertEqual(metric['competition_rank']['matches_played'],15)
        self.assertEqual(self.usage,before)

    def test_zero_is_valid_missing_is_not_and_ties_share_rank(self):
        teams={str(i):{'matches':3,'metrics':{'Fouls':{'perMatch':v,'matchesCovered':3}}}for i,v in enumerate([0,0,2,None],1)}
        result=ranks.rank_metrics(teams)
        self.assertEqual(result['1']['Fouls']['position'],1);self.assertEqual(result['2']['Fouls']['position'],1)
        self.assertEqual(result['3']['Fouls']['position'],3);self.assertNotIn('4',result)
        self.assertEqual(result['1']['Fouls']['total_teams'],3)

    def test_no_competition_appearance_does_not_borrow_domestic_rank(self):
        with patch.object(ranks,'competition_ranks',return_value={}):
            enriched=ranks.enrich_comparison_ranks(self.usage,self.fixture,now=self.now)
        self.assertNotIn('competition_rank',enriched['teams'][0]['team_comparison']['shooting'][0])
        self.assertNotIn('competition_ranking',enriched)

    def test_domestic_fixture_selects_its_exact_season(self):
        fixture={**self.fixture,'league':{'id':600,'name':'Super Lig'},'season':{'id':28203,'name':'2026/2027'}}
        with patch.object(ranks,'competition_ranks',return_value={})as load:
            ranks.enrich_comparison_ranks(self.usage,fixture,now=self.now)
        load.assert_called_once_with(600,28203,self.now,'overall')

    def test_started_report_is_not_recomputed(self):
        with patch.object(ranks,'competition_ranks')as load:
            result=ranks.enrich_comparison_ranks(self.usage,{**self.fixture,'startingAt':'2026-11-03T20:00:00Z'},now=self.now)
        load.assert_not_called();self.assertIs(result,self.usage)

    def test_query_scopes_completed_season_and_excludes_future_matches(self):
        engine=MagicMock();db=engine.connect.return_value.__enter__.return_value
        db.execute.return_value.mappings.return_value.all.return_value=[]
        with patch.object(ranks,'engine',engine),patch.object(ranks,'_provider_rows',return_value=[]):
            ranks._CACHE.clear();ranks.competition_ranks(2,28155,self.now)
        query,params=db.execute.call_args.args
        self.assertIn('included_in_aggregation=TRUE',str(query));self.assertIn('kickoff_at<:cutoff',str(query));self.assertIn('season_id=:season',str(query))
        self.assertEqual(params['league'],2);self.assertEqual(params['season'],28155)

    def test_derived_rates_use_aggregate_counts_across_season(self):
        agg=Aggregate()
        for made,total in [(1,1),(1,9)]:
            agg.add({'included_in_aggregation':True,'kickoff_at':self.now,'player_contributions':[],
                     'team_contributions':{'1':{'name':'One','metrics':{'Shots On Target':made,'Shots Total':total}}}})
        result=ranks.rank_metrics(agg.finish()['team_metrics'])
        self.assertEqual(result['1']['Shots On Target (%)']['season_value'],20)

    def test_unavailable_rank_source_does_not_remove_comparison(self):
        with patch.object(ranks,'competition_ranks',side_effect=RuntimeError('unavailable')):
            self.assertIs(ranks.enrich_comparison_ranks(self.usage,self.fixture,now=self.now),self.usage)
