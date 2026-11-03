import unittest
from match_pool_module.forecast import score_distribution, forecast_for_tier

class ScoreMarginTests(unittest.TestCase):
    def test_margins_partition_full_outcomes_including_high_scores(self):
        for home,away,rho in [(2.14,1.65,0.),(1.22,1.99,-.05),(8.,7.,0.)]:
            with self.subTest(home=home,away=away,rho=rho):
                result=score_distribution(home,away,rho);m=result['scoreMargins'];o=result['outcomes']
                self.assertAlmostEqual(sum(m.values()),1.,places=10)
                self.assertAlmostEqual(m['homeOne']+m['homeTwoPlus'],o['home'],places=12)
                self.assertAlmostEqual(m['awayOne']+m['awayTwoPlus'],o['away'],places=12)
                self.assertEqual(m['draw'],o['draw'])
                visible=sum(c['probability'] for c in result['grid'] if c['home']-c['away']>=2)
                self.assertGreater(m['homeTwoPlus'],visible)

    def test_free_response_includes_margin_distribution(self):
        dist=score_distribution(1.2,1.8)
        result=forecast_for_tier({'status':'ready','forecast':dist},'free')
        self.assertEqual(result['forecast']['scoreMargins'],dist['scoreMargins'])
        self.assertNotIn('goalDistribution',result['forecast'])
