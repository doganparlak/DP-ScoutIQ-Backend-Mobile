import unittest
from match_pool_module.forecast import half_scenarios, _half_share, _half_prior, forecast_for_tier

class HalfProfileTests(unittest.TestCase):
    def test_halves_use_learned_team_shares_and_preserve_full_match_goals(self):
        totals={8:[20,30,12,24,8]}
        model={'leagueTotals':{'8':totals[8]},'priorStrength':15.}
        own=[{'gf':2,'ga':1,'hgf':0,'hga':1} for _ in range(5)]
        opponent=[{'gf':3,'ga':2,'hgf':2,'hga':0} for _ in range(5)]
        for rho in (0.,-.05):
            result=half_scenarios(2.4,1.6,own,opponent,8,model,rho)
            goals=result['projectedGoals']
            self.assertAlmostEqual(goals['first']['home']+goals['second']['home'],2.4)
            self.assertAlmostEqual(goals['first']['away']+goals['second']['away'],1.6)
            self.assertAlmostEqual(goals['first']['home'],2.4*_half_share(own,opponent,_half_prior(totals,8,True),15.))
            self.assertAlmostEqual(sum(result['moreGoalsHalf'].values()),1.)
            self.assertTrue(all(0<=v<=1 for v in result['moreGoalsHalf'].values()))
            self.assertNotAlmostEqual(goals['first']['home'],1.2)

    def test_no_half_model_does_not_fabricate_half_goals(self):
        self.assertIsNone(half_scenarios(2.,1.,[],[],8,None))

    def test_paid_half_data_is_not_in_free_response(self):
        result=forecast_for_tier({'status':'ready','forecast':{'halfScenarios':{'projectedGoals':{}}}},'free')
        self.assertNotIn('halfScenarios',result['forecast'])
