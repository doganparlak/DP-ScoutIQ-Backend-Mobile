"""Shared completed-match evidence for mobile reports."""
from report_module.snapshot_cache import SnapshotCache
from match_pool_module.fixtures import with_fixture_coaches
from .report import MATCH_REPORT_VERSION, MatchReportError, _build_summary, generate_match_report

_FIXTURES = SnapshotCache(ttl=300, limit=32)


def _completed_with_evidence(report):
    fixture = report.get('fixture') or {}
    return (fixture.get('state_id') in {5, 7, 8}
            and bool(report.get('lineups')) and bool((report.get('coverage') or {}).get('unique_team_metrics')))


def load_fixture_report(fixture_id, language='en'):
    def load():
        report = generate_match_report(fixture_id, 'en', False)
        if (report.get('fixture') or {}).get('id') != fixture_id:
            raise MatchReportError('Fixture identity does not match the requested report')
        return with_fixture_coaches(report)

    report = _FIXTURES.get_or_load((MATCH_REPORT_VERSION, fixture_id), load, cache_if=_completed_with_evidence)
    if language != report['language']:
        report = {**report, 'language': language, 'summary': _build_summary(report['teams'], report['events'], language)}
    return report
