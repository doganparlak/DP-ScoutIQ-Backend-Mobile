import unittest
import numpy as np
from match_pool_module.forecast_league_offsets import AssociationPrior, LeagueOffsetEstimator, apply_elo_gap


class LeagueOffsetTests(unittest.TestCase):
    def setUp(self):
        self.prior = AssociationPrior({'ENG': 100., 'TUR': 40.}, '2025-06-01', 'test')

    def test_same_association_and_missing_are_neutral(self):
        model = LeagueOffsetEstimator(self.prior)
        model.theta[:] = [.8, .2, -.2]
        self.assertEqual(model.gap('ENG', 'ENG'), 0.)
        self.assertEqual(model.gap('ENG', None), 0.)
        self.assertAlmostEqual(model.gap('ENG', 'TUR'), -model.gap('TUR', 'ENG'))

    def test_pair_mean_and_elo_difference(self):
        raw = np.array([[1500., 1550., 3.], [1550., 1500., 4.]])
        out = apply_elo_gap(raw, ['rating', 'opponent_rating', 'other'], [100.])
        np.testing.assert_array_equal(out, [[1550., 1500., 3.], [1500., 1550., 4.]])
        np.testing.assert_array_equal(apply_elo_gap(raw, ['rating', 'opponent_rating', 'other'], [0.]), raw)
        np.testing.assert_array_equal(raw, [[1500., 1550., 3.], [1550., 1500., 4.]])

    def test_learns_residual_without_repeating_existing_elo_gap(self):
        observations = [{'homeAssociation': 'ENG', 'awayAssociation': 'TUR', 'eloDifference': 0.,
                         'homeAdvantage': 0., 'result': 1. if i % 4 else 0.} for i in range(100)]
        raw = LeagueOffsetEstimator(self.prior).fit(observations)
        already_adjusted = LeagueOffsetEstimator(self.prior).fit([{**r, 'eloDifference': 190.8485} for r in observations])
        self.assertGreater(raw.gap('ENG', 'TUR'), 0.)
        self.assertLess(abs(already_adjusted.gap('ENG', 'TUR')), raw.gap('ENG', 'TUR') * .01)

    def test_empty_fit_and_serialization(self):
        model = LeagueOffsetEstimator(self.prior).fit([])
        self.assertEqual(model.gap('ENG', 'TUR'), 0.)
        self.assertEqual(model.dump()['trainingMatches'], 0)
        self.assertEqual(model.dump()['priorAvailableAt'], '2025-06-01')

    def test_future_results_do_not_change_past_offsets(self):
        from scripts.evaluate_league_offsets import causal_gaps
        matches = [{'id': i, 'at': f'2026-07-{i:02d} 12:00:00', 'league': 2,
                    'home': 1, 'away': 2, 'hg': 2, 'ag': 0, 'stats': {}}
                   for i in range(1, 7)]
        before, audits = causal_gaps(matches, {1: 'ENG', 2: 'TUR'}, self.prior)
        changed = [{**m, 'hg': 0, 'ag': 9} if m['id'] == 6 else m for m in matches]
        after, _ = causal_gaps(changed, {1: 'ENG', 2: 'TUR'}, self.prior)
        self.assertEqual(before, after)
        self.assertEqual(before[1], 0.)
        self.assertGreater(before[6], 0.)
        for cutoff, audit in audits.items():
            if audit['latestLabelAvailableAt']:
                self.assertLessEqual(audit['latestLabelAvailableAt'], cutoff)

    def test_prospective_archive_is_sealed_and_detects_tampering(self):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from scripts.validate_league_offsets_fresh import load_archive, archive_upcoming, digest
        with TemporaryDirectory() as folder:
            output = Path(folder)
            path = output / 'prospective-predictions.json'
            archive = {'predictions': [{'id': 1, 'generatedAt': '2026-11-02T16:00:00Z', 'at': '2026-11-03T17:45:00Z'}]}
            path.write_text(json.dumps(archive))
            manifest = output / 'prospective-manifest.json'
            manifest.write_text(json.dumps({'predictionsSHA256': digest(path)}))
            self.assertEqual(load_archive(output), archive)
            # An existing archive is returned before any API calls or refitting.
            self.assertEqual(archive_upcoming(output, None, None, None, None, None, None, None), archive)
            path.write_text(json.dumps({'predictions': []}))
            with self.assertRaises(ValueError): load_archive(output)

    def test_post_kickoff_prediction_is_rejected(self):
        import json
        from pathlib import Path
        from tempfile import TemporaryDirectory
        from scripts.validate_league_offsets_fresh import load_archive, digest
        with TemporaryDirectory() as folder:
            output = Path(folder)
            path = output / 'prospective-predictions.json'
            path.write_text(json.dumps({'predictions': [{'id': 1, 'generatedAt': '2026-11-03T18:00:00Z', 'at': '2026-11-03T17:45:00Z'}]}))
            (output / 'prospective-manifest.json').write_text(json.dumps({'predictionsSHA256': digest(path)}))
            with self.assertRaises(ValueError): load_archive(output)

    def test_invalid_inputs(self):
        with self.assertRaises(ValueError):
            LeagueOffsetEstimator(AssociationPrior({'ENG': 0., 'TUR': 40.}, '', '')).design('ENG', 'TUR')
        with self.assertRaises(ValueError):
            apply_elo_gap([[1., 2.]], ['rating', 'opponent_rating'], [100.])


if __name__ == '__main__':
    unittest.main()
