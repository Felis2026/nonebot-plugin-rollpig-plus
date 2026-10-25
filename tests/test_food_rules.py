from __future__ import annotations

import json
import unittest
from unittest.mock import patch

import test_roast_reservations as fixtures
from nonebot_plugin_rollpig_plus import roast_flow
from nonebot_plugin_rollpig_plus.resource_manager import BUILTIN_RULES_JSON, get_pig_by_id


class FoodRulesTests(unittest.TestCase):
    def test_yiwei_is_preserved_as_normal_pig_and_excluded_from_roast_results(self):
        rules = json.loads(BUILTIN_RULES_JSON.read_text(encoding="utf-8"))
        self.assertNotIn("yiwei-pig", rules["food_pigs"])
        self.assertNotIn("yiwei-pig", fixtures.texts_module.FOOD_PIG_IDS)
        pig = get_pig_by_id("yiwei-pig")
        self.assertIsNotNone(pig)
        self.assertEqual(pig["name"], "猪意味")
        with patch.object(roast_flow.pig_resource_manager, "food_pig_ids", set(rules["food_pigs"])):
            self.assertNotIn("yiwei-pig", roast_flow.get_food_pig_ids())
            self.assertFalse(roast_flow.is_food_pig(pig))
            self.assertTrue(roast_flow.can_backfire_roast(pig))


if __name__ == "__main__":
    unittest.main()
