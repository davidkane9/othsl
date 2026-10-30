import copy
import json
import re
import unittest
from unittest.mock import patch

import app


def sample_flight():
    return {
        "teams": ["A", "B"],
        "current_stats": {t: {"pts": 0, "gd": 0, "gf": 0, "gp": 0} for t in ["A", "B"]},
        "current_elos": {"A": 1500, "B": 1500},
        "current_regression": 0.0,
        "remaining_games": [{"id": "game", "home": "A", "away": "B", "date": "2026-11-01", "involves_team": True}],
        "promotion_cut": 1, "relegation_cut": 0,
    }


class ConsistencyTests(unittest.TestCase):
    def test_pooled_sample_is_reproducible_and_near_analytical_probability(self):
        data = sample_flight()
        first = app.simulate_flight_outlook(data)
        second = app.simulate_flight_outlook(data)
        self.assertEqual(first, second)
        self.assertEqual(first["total_runs"], 10000)
        # Equal teams: A wins 39%, draws 22%, and wins the alphabetical tiebreak.
        self.assertAlmostEqual(first["team_stats"]["A"]["promotion_probability"], 61, delta=2)
        for counts in first["counts"].values():
            self.assertEqual(sum(counts), 10000)

    def test_different_team_pages_share_identical_samples(self):
        data = sample_flight()
        other = copy.deepcopy(data)
        other["selected_team"] = "B"
        other["remaining_games"][0]["involves_team"] = False
        self.assertEqual(app.simulation_fingerprint(data), app.simulation_fingerprint(other))
        self.assertEqual(app.simulate_flight_outlook(data), app.simulate_flight_outlook(other))

    def test_cache_changes_when_model_inputs_change(self):
        data = sample_flight()
        before = app.get_cached_flight_outlook(("test",), data, total_runs=100)
        changed = copy.deepcopy(data)
        changed["current_elos"]["A"] = 1900
        after = app.get_cached_flight_outlook(("test",), changed, total_runs=100)
        self.assertIsNot(before, after)

    def test_distribution_uses_the_actual_run_count(self):
        data = sample_flight()
        outlook = app.simulate_flight_outlook(data, total_runs=200)
        summary = app.build_team_simulation_from_outlook("A", data, outlook)
        self.assertAlmostEqual(sum(p["probability"] for p in summary["place_probabilities"]), 100)

    def test_old_conditional_cache_is_rejected(self):
        data = sample_flight()
        with patch.dict(app._flight_importance_cache, {"test": {"impact_runs": 220}}), patch.object(
                app, "calculate_flight_importance", return_value={"fresh": True}) as calculate:
            self.assertEqual(app.get_flight_importance_report("test", data), {"fresh": True})
            calculate.assert_called_once()

    def test_concord_summary_and_embedded_browser_baseline_agree(self):
        with app.app.test_client() as client:
            page = client.get("/team/concord-united-over-30-div-2-north/")
        self.assertEqual(page.status_code, 200)
        text = page.get_data(as_text=True)
        baseline = json.loads(re.search(r"const BASELINE_OUTLOOK = (.*);", text).group(1))
        display = baseline["display_stats"]["Concord United"]
        self.assertIn(f'{display["promo"]}% promotion, {display["stay"]}% stay, and {display["relg"]}% relegation', text)
        self.assertEqual(baseline["total_runs"], 10000)
        self.assertEqual(sum(baseline["counts"]["Concord United"]), 10000)
        self.assertIn("teamStats = BASELINE_OUTLOOK.display_stats", text)
        self.assertIn("summary.hidden = !isBaseline", text)
        for stats in baseline["display_stats"].values():
            self.assertEqual(stats["promo"] + stats["stay"] + stats["relg"], 100)
        with app.app.test_client() as client:
            flight_page = client.get("/flight/over-30-div-2-north/").get_data(as_text=True)
        flight_baseline = json.loads(re.search(r"const BASELINE_OUTLOOK = (.*);", flight_page).group(1))
        self.assertEqual(baseline, flight_baseline)


if __name__ == "__main__":
    unittest.main()
