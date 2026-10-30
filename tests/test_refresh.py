import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import app
import elo
import victory
from season_config import CURRENT_SEASON, CURRENT_SEASON_ID, LEAGUE_ID


def team(identifier, name):
    return dict(team_id=identifier, team_name=name, bracket_name='Over 55',
                division_name='Division 2', flight='South', season_id=CURRENT_SEASON_ID, league_id=LEAGUE_ID)


def game(**changes):
    value = dict(id=1, season_id=CURRENT_SEASON_ID, league_id=LEAGUE_ID,
                 season_name=CURRENT_SEASON, game_type='R', home_seasonlineup_id=1,
                 away_seasonlineup_id=2, home_team_score=0, away_team_score=0, game_date='2026-09-06')
    value.update(changes)
    return value


class ImportTests(unittest.TestCase):
    def normalize(self, games):
        return victory.normalize_games(games, [team(1, 'Home'), team(2, 'Away')])

    def test_zero_draw_and_unplayed_are_distinct(self):
        rows = self.normalize([game(), game(id=2, home_team_score=None, away_team_score=None)])
        self.assertEqual((rows[0]['home_goals'], rows[0]['away_goals']), (0, 0))
        self.assertEqual((rows[1]['home_goals'], rows[1]['away_goals']), ('', ''))

    def test_deduplicate_and_exclude_nonleague_fixtures(self):
        rows = self.normalize([game(), game(), game(id=2, game_type='S'), game(id=3, game_type='B'), game(id=4, game_type='C')])
        self.assertEqual(len(rows), 1)

    def test_postponed_score_is_not_counted(self):
        row = self.normalize([game(postponed=True, home_team_score=2, away_team_score=1)])[0]
        self.assertEqual(row['home_goals'], '')
        self.assertIn('postponed', row['notes'])

    def test_forfeit_preserves_awarded_score(self):
        row = self.normalize([game(forfeit='V', home_team_score=2, away_team_score=0)])[0]
        self.assertEqual((row['home_goals'], row['away_goals']), (2, 0))
        self.assertIn('forfeit', row['notes'])

    def test_invalid_or_wrong_season_cannot_publish(self):
        for change in [dict(season_id=253), dict(home_team_score=None), dict(home_seasonlineup_id=999), dict(season_name='Spring 2026')]:
            with self.subTest(change=change), self.assertRaises(ValueError):
                self.normalize([game(**change)])

    def test_pagination(self):
        with patch.object(victory, 'fetch_json', side_effect=[{'results': [{'id': 1}], 'next': '?page=2'}, {'results': [{'id': 2}], 'next': None}]):
            self.assertEqual(victory.fetch_collection('games'), [{'id': 1}, {'id': 2}])

    def test_failed_refresh_leaves_files_untouched(self):
        from download_current import download
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / 'current_results.csv'
            path.write_text('existing data')
            with patch.object(download, 'DATA_DIR', Path(directory)), patch.object(download, 'fetch_collection', side_effect=ValueError('Bad response')):
                with self.assertRaises(ValueError):
                    download.main()
            self.assertEqual(path.read_text(), 'existing data')

    def test_refresh_reports_failure_to_scheduler(self):
        import refresh
        with patch('sys.argv', ['refresh.py']), patch.object(refresh, 'run', return_value=False):
            self.assertEqual(refresh.main(), 1)


class ModelTests(unittest.TestCase):
    def test_week_view_does_not_use_the_full_current_table(self):
        row = next(r for r in app.get_current_season_rows() if app.has_played_score(r))
        table = app.get_standings_for_flight([row], row['age_group'], row['division'], row['geography'])
        self.assertEqual(len(table), 2)
        self.assertEqual(sum(t['gp'] for t in table), 2)

    def test_elo_uses_chronology_and_deduplicates(self):
        base = dict(age_group='Over 55', division='2', geography='South', home_team='Home', away_team='Away', home_goals='1', away_goals='0')
        spring = dict(base, season='Spring 2026', date='2026-04-05')
        fall = dict(base, season='Fall 2025', date='2025-09-07')
        history, _ = elo.rolling_elo([spring, fall, dict(fall)])
        self.assertEqual([r['season'] for r in history], ['Fall 2025', 'Spring 2026'])
        self.assertAlmostEqual(history[1]['elo_home_before'], elo.regress(history[0]['elo_home_after']), places=1)

    def test_probabilities_sum_to_one_even_for_extreme_ratings(self):
        for home, away in [(3000, 500), (500, 3000), (1500, 1500)]:
            p = app.match_outcome_probabilities(home, away)
            self.assertAlmostEqual(sum(p.values()), 1, places=3)
            self.assertTrue(all(0 <= n <= 1 for n in p.values()))
            self.assertEqual(app.expected_result(home, away), elo.expected(home, away))

    def test_completed_import_does_not_invent_more_games(self):
        rows = victory.normalize_games([game()], [team(1, 'Home'), team(2, 'Away')])
        standings = [{'team': t, 'pts': 1, 'gd': 0, 'gf': 0, 'gp': 1, 'w': 0, 'l': 0, 't': 1} for t in ['Home', 'Away']]
        info = dict(team='Home', age_group='Over 55', division='2', geography='South')
        with patch.object(app, 'get_latest_elo_map', return_value={}):
            data = app.get_flight_sim_data(info, standings, rows)
        self.assertEqual(data['remaining_games'], [])
        self.assertFalse(data['schedule_inferred'])

    def test_current_tables_match_official_source_including_deductions(self):
        rows = app.get_current_season_rows()
        snapshot = json.loads((Path(app.DATA_DIR) / 'current_standings.json').read_text())
        self.assertEqual(len(rows), snapshot['row_count'])
        count = 0
        for flight in app.get_flight_catalog(rows):
            table = app.get_standings_for_flight(rows, flight['age_group'], flight['division'], flight['geography'])
            source = sorted([t for t in snapshot['standings'] if t['bracket_name'] == flight['age_group'] and t['division_name'] == 'Division ' + flight['division'] and t['flight'] == flight['geography'] and app.is_real_team_name(t['team_name'])], key=lambda t:t['place'])
            self.assertEqual([(t['team'],t['pts'],t['gp']) for t in table], [(t['team_name'],t['points'],t['games_played']) for t in source])
            count += len(table)
        self.assertGreater(count, 250)


if __name__ == '__main__':
    unittest.main()
