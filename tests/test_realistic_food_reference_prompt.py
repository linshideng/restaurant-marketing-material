import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import material_skill


class RealisticFoodReferencePromptTests(unittest.TestCase):
    def _normal_request_with_reference(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "3:4"},
                "title": "招牌牛肉饭",
                "store": {"name": "街角小馆", "category": "简餐"},
                "copy": {"selected_text": "招牌牛肉饭"},
                "style": {"name": "美团餐饮团购"},
                "assets": {"qr_code_not_needed": True, "reference_images": ["/tmp/dish.jpg"]},
            }
        )
        return req

    def test_normal_prompt_prioritizes_realistic_food_reference_when_image_uploaded(self):
        req = self._normal_request_with_reference()
        template = material_skill.load_templates("营销海报")[0]
        layout = material_skill.select_layout(req, template, 1024, 1360)

        prompt_json = json.loads(material_skill.build_scene_prompt(req, template, layout, 1024, 1360))
        prompt_text = json.dumps(prompt_json, ensure_ascii=False)

        self.assertEqual(req["style"]["realism"], "realistic")
        self.assertIn("参考图保真优先", prompt_text)
        self.assertIn("适度商业美化", prompt_text)
        self.assertIn("主要器皿", prompt_text)
        self.assertIn("真实材质", prompt_text)
        self.assertIn("no AI-rendered food", prompt_json["negative"])
        self.assertIn("no plastic food texture", prompt_json["negative"])
        self.assertNotIn("不要凭空重做摆盘", prompt_text)

    def test_reference_prompt_suppresses_oily_smearing_and_raw_style_texture_terms(self):
        req = self._normal_request_with_reference()
        template = next(item for item in material_skill.load_templates("营销海报") if item["style_id"] == "bold_split")
        layout = material_skill.select_layout(req, template, 1024, 1360)

        prompt_json = json.loads(material_skill.build_scene_prompt(req, template, layout, 1024, 1360))
        prompt_text = json.dumps(prompt_json, ensure_ascii=False)

        self.assertIn("菜品主体锁定", prompt_text)
        self.assertIn("no painterly smearing", prompt_json["negative"])
        self.assertIn("no over-smoothed food surface", prompt_json["negative"])
        self.assertIn("no mushy ingredient edges", prompt_json["negative"])
        self.assertIn("no heavy greasy sheen", prompt_json["negative"])
        self.assertIn("no global warm amber filter", prompt_json["negative"])
        self.assertIn("no invented extra dishes", prompt_json["negative"])
        self.assertNotIn("油亮食材", prompt_text)
        self.assertNotIn("glossy chili oil", prompt_text)
        self.assertNotIn("锅气蒸汽", prompt_text)

    def test_reference_prompt_requires_bright_clear_appetizing_food_subject(self):
        req = self._normal_request_with_reference()
        template = next(item for item in material_skill.load_templates("营销海报") if item["style_id"] == "ink_oriental")
        layout = material_skill.select_layout(req, template, 1024, 1360)

        prompt_json = json.loads(material_skill.build_scene_prompt(req, template, layout, 1024, 1360))
        prompt_text = json.dumps(prompt_json, ensure_ascii=False)

        self.assertIn("菜品亮度和清晰度下限", prompt_text)
        self.assertIn("自然食物高光", prompt_text)
        self.assertIn("well-lit dish subject", prompt_text)
        self.assertIn("no underexposed food", prompt_json["negative"])
        self.assertIn("no muddy low-contrast dish", prompt_json["negative"])
        self.assertIn("no blurred food details", prompt_json["negative"])
        self.assertIn("no gray ink wash over the dish", prompt_json["negative"])

    def test_panorama_prompt_keeps_food_photographic_when_reference_images_uploaded(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "五连图",
                "title": "招牌菜连连看",
                "store": {"name": "街角小馆", "category": "简餐"},
                "assets": {"reference_images": ["/tmp/dish.jpg"]},
            }
        )
        template = material_skill.load_templates("五连图")[0]
        layout = material_skill.select_layout(
            req,
            template,
            material_skill.PANORAMA_API_WIDTH,
            material_skill.PANORAMA_API_HEIGHT,
        )

        prompt_json = json.loads(material_skill.build_scene_prompt(req, template, layout, 3840, 1280))
        prompt_text = json.dumps(prompt_json, ensure_ascii=False)

        self.assertIn("参考图保真优先", prompt_text)
        self.assertIn("真实美食摄影感", prompt_text)
        self.assertIn("no 3D-rendered dish", prompt_json["negative"])
        self.assertIn("no toy-like dish", prompt_json["negative"])
        self.assertNotIn("不要凭空重做摆盘", prompt_text)

    def test_panorama_prompt_suppresses_oily_smearing_terms_for_reference_food(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "五连图",
                "title": "招牌菜连连看",
                "store": {"name": "街角小馆", "category": "简餐"},
                "assets": {"reference_images": ["/tmp/dish.jpg"]},
            }
        )
        template = next(item for item in material_skill.load_templates("五连图") if item["style_id"] == "bold_split")
        layout = material_skill.select_layout(
            req,
            template,
            material_skill.PANORAMA_API_WIDTH,
            material_skill.PANORAMA_API_HEIGHT,
        )

        prompt_json = json.loads(material_skill.build_scene_prompt(req, template, layout, 3840, 1280))
        prompt_text = json.dumps(prompt_json, ensure_ascii=False)

        self.assertIn("菜品主体锁定", prompt_text)
        self.assertIn("no painterly smearing", prompt_json["negative"])
        self.assertIn("no over-smoothed food surface", prompt_json["negative"])
        self.assertIn("no mushy ingredient edges", prompt_json["negative"])
        self.assertIn("no heavy greasy sheen", prompt_json["negative"])
        self.assertNotIn("油亮食材", prompt_text)
        self.assertNotIn("glossy chili oil", prompt_text)

    def test_panorama_prompt_requires_bright_clear_reference_food_subject(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "五连图",
                "title": "招牌菜连连看",
                "store": {"name": "街角小馆", "category": "简餐"},
                "assets": {"reference_images": ["/tmp/dish.jpg"]},
            }
        )
        template = next(item for item in material_skill.load_templates("五连图") if item["style_id"] == "ink_oriental")
        layout = material_skill.select_layout(
            req,
            template,
            material_skill.PANORAMA_API_WIDTH,
            material_skill.PANORAMA_API_HEIGHT,
        )

        prompt_json = json.loads(material_skill.build_scene_prompt(req, template, layout, 3840, 1280))
        prompt_text = json.dumps(prompt_json, ensure_ascii=False)

        self.assertIn("菜品亮度和清晰度下限", prompt_text)
        self.assertIn("自然食物高光", prompt_text)
        self.assertIn("sharp ingredient details", prompt_text)
        self.assertIn("no underexposed food", prompt_json["negative"])
        self.assertIn("no gray ink wash over the dish", prompt_json["negative"])
        self.assertIn("no waxy food", prompt_json["negative"])
        self.assertIn("no blurred food details", prompt_json["negative"])

    def test_panorama_prompt_uses_medium_shot_and_suppresses_large_tabletop(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "五连图",
                "title": "招牌菜连连看",
                "store": {"name": "街角小馆", "category": "简餐"},
                "assets": {"reference_images": ["/tmp/dish.jpg"]},
            }
        )
        template = next(item for item in material_skill.load_templates("五连图") if item["style_id"] == "clean_hero")
        layout = material_skill.select_layout(
            req,
            template,
            material_skill.PANORAMA_API_WIDTH,
            material_skill.PANORAMA_API_HEIGHT,
        )

        prompt_json = json.loads(material_skill.build_scene_prompt(req, template, layout, 3840, 1280))
        prompt_text = json.dumps(prompt_json, ensure_ascii=False)

        self.assertIn("dish_camera_and_tabletop_policy", prompt_json)
        self.assertIn("medium-shot food product display", prompt_text)
        self.assertIn("菜品垂直居中", prompt_text)
        self.assertIn("complete plate/bowl/pot rim visible", prompt_text)
        self.assertIn("no large horizontal tabletop", prompt_json["negative"])
        self.assertIn("no tabletop horizon line", prompt_json["negative"])
        self.assertIn("no dish pushed to lower edge", prompt_json["negative"])
        self.assertIn("no cropped plate rim", prompt_json["negative"])

    def test_panorama_prompt_includes_global_appetite_color_policy(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "五连图",
                "title": "招牌菜连连看",
                "store": {"name": "街角小馆", "category": "简餐"},
                "assets": {"reference_images": ["/tmp/dish.jpg"]},
            }
        )
        template = next(item for item in material_skill.load_templates("五连图") if item["style_id"] == "clean_hero")
        layout = material_skill.select_layout(
            req,
            template,
            material_skill.PANORAMA_API_WIDTH,
            material_skill.PANORAMA_API_HEIGHT,
        )

        prompt_json = json.loads(material_skill.build_scene_prompt(req, template, layout, 3840, 1280))
        prompt_text = json.dumps(prompt_json, ensure_ascii=False)

        self.assertIn("appetite_color_policy", prompt_text)
        self.assertIn("默认明亮开胃", prompt_text)
        self.assertIn("no black-red dominated palette", prompt_json["negative"])


if __name__ == "__main__":
    unittest.main()
