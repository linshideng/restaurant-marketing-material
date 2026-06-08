import contextlib
import io
import json
import os
import sys
import tempfile
import time
import unittest
import urllib.error
from argparse import Namespace
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import material_skill


class FakeHTTPResponse:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self.payload

    def close(self):
        pass


def captured_request_payload(request):
    return json.loads(request.data.decode("utf-8"))


def base_request(**overrides):
    req = {
        "type": "营销海报",
        "size": {"preset": "3:4"},
        "title": "七夕烛光晚餐",
        "store": {"name": "城市西餐厅", "category": "西餐"},
        "campaign": {"theme": "七夕约会", "offer": "双人套餐", "cta": "立即预约"},
        "products": [],
        "style": {"name": "美团餐饮团购"},
        "assets": {"qr_code_not_needed": True, "food_image_not_needed": True},
    }
    req.update(overrides)
    normalized, _ = material_skill.normalize_request(req)
    return normalized


class TemplateSelectorTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out_dir = Path(self.tmp.name)
        self.raw_path = self.out_dir / "raw_selector.txt"
        self.dx_push_disabled = mock.patch.dict(os.environ, {"RESTAURANT_DX_PUSH_DISABLED": "1"})
        self.dx_push_disabled.start()
        self.addCleanup(self.dx_push_disabled.stop)

    def tearDown(self):
        self.tmp.cleanup()

    def test_business_intent_detection_uses_new_field_and_legacy_scene_intent_mapping(self):
        explicit = base_request(
            title="春季新品上新",
            scene={"business_intent": "new_product", "intent": "堂食引流"},
        )
        self.assertEqual(explicit["scene"]["business_intent"], "new_product")
        self.assertEqual(explicit["scene"]["business_intents"], ["new_product"])

        legacy = base_request(
            title="午市套餐",
            campaign={"theme": "午市套餐", "offer": "工作日套餐", "cta": "到店品尝"},
            scene={"intent": "堂食引流"},
        )
        self.assertEqual(legacy["scene"]["business_intent"], "daily_attract")

        mixed = base_request(title="七夕套餐促销海报", campaign={"theme": "七夕活动", "offer": "双人优惠"})
        self.assertEqual(mixed["scene"]["business_intent"], "festival")
        self.assertEqual(mixed["scene"]["business_intents"][:2], ["festival", "promotion"])

    def test_load_styles_requires_concrete_design_styles(self):
        styles = material_skill.load_styles("营销海报")
        style_ids = {item["style_id"] for item in styles}
        core_style_ids = {
            "clean_hero",
            "dark_premium",
            "natural_earth",
            "bold_split",
            "collage_pop",
            "street_warm",
            "neon_night",
            "festive_red",
            "illustration_flat",
            "ink_oriental",
            "retro_poster",
            "latin_fiesta",
        }

        self.assertGreater(len(styles), 12)
        self.assertTrue(core_style_ids.issubset(style_ids))
        controls = material_skill._style_controls()
        typography_profiles = controls["typography_profiles"]
        for style in styles:
            self.assertGreaterEqual(len(style["scene_prompt"]), 520)
            self.assertIn(style["typography_profile"], typography_profiles)
            for key in ("light", "material", "color", "composition", "avoid"):
                self.assertIn(key, style["scene_prompt"].lower(), style["style_id"])
            self.assertTrue(style["color_strategy"])
            self.assertTrue(style["composition_strategy"])
            self.assertTrue(style["texture_guidance"])

    def test_recommend_styles_uses_intent_and_visual_distance(self):
        req = base_request(
            title="春季新品首发",
            store={"name": "城市西餐厅", "category": "西餐"},
            scene={"business_intent": "new_product"},
        )
        recommendations = material_skill.recommend_styles(req, material_skill.load_styles(req["type"]), top_n=2)

        self.assertEqual(recommendations[0]["id"], "clean_hero")
        self.assertEqual(len(recommendations), 2)
        self.assertNotEqual(recommendations[0]["color_strategy"], recommendations[1]["color_strategy"])
        self.assertNotEqual(recommendations[0]["composition_strategy"], recommendations[1]["composition_strategy"])

    def test_general_restaurant_intents_prefer_bright_appetite_defaults(self):
        cases = [
            (
                "membership",
                {"name": "城市会员餐厅", "category": "会员套餐"},
                {"theme": "会员日", "offer": "会员专享", "cta": "立即领取"},
                {"dark_premium"},
            ),
            (
                "promotion",
                {"name": "街角简餐", "category": "简餐"},
                {"theme": "午市优惠", "offer": "套餐立减", "cta": "立即购买"},
                {"dark_premium", "neon_night"},
            ),
        ]

        for intent, store, campaign, forbidden_top3 in cases:
            with self.subTest(intent=intent):
                req = base_request(
                    title="今日到店更划算",
                    store=store,
                    campaign=campaign,
                    scene={"business_intent": intent},
                )

                recommended_ids = [
                    item["id"]
                    for item in material_skill.recommend_styles(req, material_skill.load_styles(req["type"]), top_n=5)
                ]

                self.assertNotEqual(recommended_ids[0], "dark_premium")
                self.assertTrue(forbidden_top3.isdisjoint(recommended_ids[:3]), recommended_ids)

    def test_explicit_dark_context_can_still_recommend_dark_styles(self):
        cases = [
            (
                "夜宵烧烤 霓虹开吃",
                {"name": "深夜烧烤酒馆", "category": "夜宵烧烤酒馆"},
                {"theme": "深夜酒馆", "offer": "夜宵套餐", "cta": "立即下单"},
                {"neon_night"},
            ),
            (
                "私宴新品 暗调高级",
                {"name": "私宴融合菜", "category": "高端中餐私宴"},
                {"theme": "黑金暗调高级", "offer": "会员私宴", "cta": "预约品鉴"},
                {"dark_premium"},
            ),
        ]

        for title, store, campaign, expected_top3 in cases:
            with self.subTest(title=title):
                req = base_request(
                    title=title,
                    store=store,
                    campaign=campaign,
                    scene={"business_intent": "promotion"},
                )

                recommended_ids = [
                    item["id"]
                    for item in material_skill.recommend_styles(req, material_skill.load_styles(req["type"]), top_n=5)
                ]

                self.assertTrue(expected_top3.intersection(recommended_ids[:3]), recommended_ids)

    def test_broad_western_japanese_and_membership_categories_do_not_boost_dark_premium(self):
        cases = [
            ("城市西餐厅", "西餐", "new_product"),
            ("清爽日料店", "日料", "new_product"),
            ("会员套餐", "会员", "membership"),
        ]

        for store_name, category, intent in cases:
            with self.subTest(category=category):
                req = base_request(
                    title="春季新品首发",
                    store={"name": store_name, "category": category},
                    campaign={"theme": "新品尝鲜", "offer": "到店有礼", "cta": "立即预约"},
                    scene={"business_intent": intent},
                )

                recommended_ids = [
                    item["id"]
                    for item in material_skill.recommend_styles(req, material_skill.load_styles(req["type"]), top_n=5)
                ]

                self.assertNotIn("dark_premium", recommended_ids[:3], recommended_ids)

    def test_recommend_styles_applies_cultural_filter_and_light_category_affinity(self):
        western = base_request(
            title="圣诞节双人套餐",
            store={"name": "城市西餐厅", "category": "西餐"},
            scene={"business_intent": "festival"},
        )
        western_ids = [item["id"] for item in material_skill.recommend_styles(western, material_skill.load_styles(western["type"]), top_n=5)]
        self.assertNotIn("festive_red", western_ids)
        self.assertNotIn("ink_oriental", western_ids)

        chinese = base_request(
            title="午市到店引流",
            store={"name": "山城火锅", "category": "四川火锅"},
            scene={"business_intent": "daily_attract"},
        )
        chinese_ids = [item["id"] for item in material_skill.recommend_styles(chinese, material_skill.load_styles(chinese["type"]), top_n=5)]
        self.assertNotIn("latin_fiesta", chinese_ids)
        self.assertIn("street_warm", chinese_ids[:2])

    def test_sichuan_hunan_summer_recommendations_prefer_bright_appetite_styles(self):
        req = base_request(
            title="夏日酸菜鱼 解暑清甜",
            store={"name": "麻六记", "category": "川湘菜"},
            campaign={"theme": "夏季上新", "offer": "酸菜鱼尝鲜", "cta": "到店品尝", "season": "夏季"},
            style={"name": "美团餐饮团购", "cuisine_tag": "川湘菜", "season_atmosphere": "夏日清爽"},
            scene={"business_intent": "daily_attract"},
        )

        recommendations = material_skill.recommend_styles(req, material_skill.load_styles(req["type"]), top_n=5)
        recommended_ids = [item["id"] for item in recommendations]

        self.assertIn(recommended_ids[0], {"natural_earth", "ink_oriental", "clean_hero"})
        self.assertIn("natural_earth", recommended_ids[:2])
        self.assertIn("ink_oriental", recommended_ids)
        self.assertNotIn("dark_premium", recommended_ids[:3])

    def test_sichuan_hunan_classic_dish_prefers_light_calligraphy_style(self):
        req = base_request(
            title="一城巴蜀味 一味宫保香",
            store={"name": "峨嵋酒家", "category": "川湘菜"},
            campaign={"theme": "经典招牌", "offer": "宫保鸡丁尝鲜", "cta": "到店品尝"},
            style={"name": "美团餐饮团购", "cuisine_tag": "川湘菜"},
            scene={"business_intent": "daily_attract"},
        )

        recommendations = material_skill.recommend_styles(req, material_skill.load_styles(req["type"]), top_n=3)
        recommended_ids = [item["id"] for item in recommendations]

        self.assertEqual(recommended_ids[0], "ink_oriental")
        self.assertIn("retro_poster", recommended_ids)

    def test_sichuan_hunan_wok_dish_prefers_bright_impact_styles_over_natural_green(self):
        req = base_request(
            title="现炒小炒肉 锅气真下饭",
            store={"name": "川湘小馆", "category": "川湘菜"},
            campaign={"theme": "日常引流", "offer": "小炒肉尝鲜", "cta": "到店品尝"},
            style={"name": "美团餐饮团购", "cuisine_tag": "川湘菜"},
            scene={"business_intent": "daily_attract"},
        )

        recommendations = material_skill.recommend_styles(req, material_skill.load_styles(req["type"]), top_n=3)
        recommended_ids = [item["id"] for item in recommendations]

        self.assertIn(recommended_ids[0], {"dynamic_angle", "bold_split"})
        self.assertNotIn("natural_earth", recommended_ids[:2])

    def test_neighboring_regional_cuisines_do_not_default_to_street_warm(self):
        cases = [
            ("云贵川酸汤鱼 夏日清爽", "云贵川菜", {"natural_earth", "ink_oriental", "clean_hero"}),
            ("广西本地味 现炒现上桌", "广西菜", {"dynamic_angle", "bold_split", "oversize_type", "natural_earth", "ink_oriental"}),
            ("江西小炒 锅气下饭", "江西菜", {"dynamic_angle", "bold_split", "oversize_type"}),
            ("湖北藕汤 鲜香上桌", "湖北菜", {"natural_earth", "clean_hero", "retro_poster", "ink_oriental"}),
        ]

        for title, category, expected_first_ids in cases:
            with self.subTest(category=category):
                req = base_request(
                    title=title,
                    store={"name": "地域小馆", "category": category},
                    campaign={"theme": "日常引流", "offer": "到店尝鲜", "cta": "到店品尝"},
                    style={"name": "美团餐饮团购", "cuisine_tag": category},
                    scene={"business_intent": "daily_attract"},
                )

                recommendations = material_skill.recommend_styles(req, material_skill.load_styles(req["type"]), top_n=3)
                recommended_ids = [item["id"] for item in recommendations]

                self.assertIn(recommended_ids[0], expected_first_ids)
                self.assertNotEqual(recommended_ids[0], "street_warm")

    def test_sichuan_hunan_prompt_adds_season_and_dish_specific_bright_guidance(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "3:4"},
                "title": "夏日酸菜鱼 解暑清甜",
                "store": {"name": "麻六记", "category": "川湘菜"},
                "campaign": {"theme": "夏季上新", "offer": "酸菜鱼尝鲜", "cta": "到店品尝", "season": "夏季"},
                "style": {"name": "美团餐饮团购", "cuisine_tag": "川湘菜", "season_atmosphere": "夏日清爽"},
                "assets": {"qr_code_not_needed": True, "food_image_not_needed": True},
            }
        )
        template = next(item for item in material_skill.load_styles(req["type"]) if item["style_id"] == "natural_earth")
        prompt_json = json.loads(
            material_skill.build_scene_prompt(
                req,
                template,
                {"id": "layout", "name": "Layout"},
                1080,
                1440,
            )
        )
        cuisine_city = json.dumps(prompt_json["prompt_sections"]["CUISINE_CITY"], ensure_ascii=False)

        self.assertIn("地域菜系季节菜品差异化", cuisine_city)
        self.assertIn("普通地域菜系优先明亮开胃", cuisine_city)
        self.assertIn("酸菜鱼", cuisine_city)
        self.assertIn("夏季", cuisine_city)
        self.assertIn("字体策略", cuisine_city)
        self.assertEqual(prompt_json["style"]["typography_profile"]["id"], "warm_handwritten_kai")

    def test_neighboring_regional_prompt_uses_shared_bright_appetite_guidance(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "3:4"},
                "title": "江西小炒 锅气下饭",
                "store": {"name": "赣味小馆", "category": "江西菜"},
                "campaign": {"theme": "日常引流", "offer": "到店尝鲜", "cta": "到店品尝"},
                "style": {"name": "美团餐饮团购", "cuisine_tag": "江西菜"},
                "assets": {"qr_code_not_needed": True, "food_image_not_needed": True},
            }
        )
        template = next(item for item in material_skill.load_styles(req["type"]) if item["style_id"] == "dynamic_angle")
        prompt_json = json.loads(
            material_skill.build_scene_prompt(
                req,
                template,
                {"id": "layout", "name": "Layout"},
                1080,
                1440,
            )
        )
        cuisine_city = json.dumps(prompt_json["prompt_sections"]["CUISINE_CITY"], ensure_ascii=False)

        self.assertIn("地域菜系季节菜品差异化", cuisine_city)
        self.assertIn("云贵川/广西/江西/湖北", cuisine_city)
        self.assertIn("爆炒", cuisine_city)
        self.assertIn("字体策略", cuisine_city)

    def test_prompt_includes_appetite_color_policy_without_reference_images(self):
        req = base_request(
            title="招牌牛肉饭 午市上新",
            store={"name": "街角简餐", "category": "简餐"},
            campaign={"theme": "午市上新", "offer": "招牌套餐", "cta": "立即下单"},
            assets={"qr_code_not_needed": True, "food_image_not_needed": True},
        )
        template = next(item for item in material_skill.load_styles(req["type"]) if item["style_id"] == "clean_hero")

        prompt_json = json.loads(
            material_skill.build_scene_prompt(
                req,
                template,
                {"id": "layout", "name": "Layout"},
                1080,
                1440,
            )
        )
        prompt_text = json.dumps(prompt_json, ensure_ascii=False)

        self.assertIn("appetite_color_policy", prompt_text)
        self.assertIn("默认明亮开胃", prompt_text)
        self.assertIn("no black-red dominated palette", prompt_json["negative"])

    def test_dark_style_without_reference_images_still_protects_food_brightness(self):
        req = base_request(
            title="私宴新品 暗调高级",
            store={"name": "私宴融合菜", "category": "高端中餐私宴"},
            campaign={"theme": "黑金暗调高级", "offer": "会员私宴", "cta": "预约品鉴"},
            scene={"business_intent": "brand_image"},
            assets={"qr_code_not_needed": True, "food_image_not_needed": True},
        )
        template = next(item for item in material_skill.load_styles(req["type"]) if item["style_id"] == "dark_premium")

        prompt_json = json.loads(
            material_skill.build_scene_prompt(
                req,
                template,
                {"id": "layout", "name": "Layout"},
                1080,
                1440,
            )
        )
        prompt_text = json.dumps(prompt_json, ensure_ascii=False)

        self.assertIn("显式深色风格菜品保护", prompt_text)
        self.assertIn("well-lit dish subject", prompt_text)
        self.assertIn("no underexposed food", prompt_json["negative"])

    def test_style_reference_note_is_sanitized_for_protected_ip_and_triggers_guard(self):
        req, warnings = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "3:4"},
                "title": "春日尝鲜",
                "store": {"name": "青禾小馆", "category": "融合菜"},
                "assets": {
                    "qr_code_not_needed": True,
                    "food_image_not_needed": True,
                    "style_reference_images": ["/tmp/style-ref.png"],
                    "style_reference_note": "参考漫威英雄海报和迪士尼城堡配色",
                },
            }
        )
        prompt_json = json.loads(
            material_skill.build_scene_prompt(
                req,
                {"template_id": "unit", "variant": "clean_hero", "scene_prompt": "clean poster"},
                {"id": "layout", "name": "Layout"},
                1080,
                1440,
            )
        )
        prompt_text = json.dumps(prompt_json["style_reference_guidance"], ensure_ascii=False)

        self.assertTrue(any("受保护 IP" in warning for warning in warnings))
        for forbidden in ("漫威", "迪士尼", "Disney", "Marvel"):
            self.assertNotIn(forbidden, req["assets"]["style_reference_note"])
            self.assertNotIn(forbidden, prompt_text)
        self.assertIn("超级英雄", prompt_text)
        self.assertIn("童趣", prompt_text)
        self.assertIn("no protected IP logos or characters", prompt_json["negative"])

    def test_regular_style_reference_dominant_base_style_does_not_force_3d_miniature(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "3:4"},
                "title": "春日尝鲜",
                "store": {"name": "青禾小馆", "category": "融合菜"},
                "assets": {
                    "qr_code_not_needed": True,
                    "food_image_not_needed": True,
                    "style_reference_images": ["/tmp/flat-ref.png"],
                    "style_reference_note": "参考扁平招贴排版",
                },
            }
        )
        prompt_json = json.loads(
            material_skill.build_scene_prompt(
                req,
                {"template_id": "unit", "variant": "clean_hero", "scene_prompt": "clean poster"},
                {"id": "layout", "name": "Layout"},
                1080,
                1440,
            )
        )
        base_style = prompt_json["prompt_sections"]["BASE_STYLE"]
        visual_requirements = json.dumps(prompt_json["visual_requirements"], ensure_ascii=False)

        self.assertIn("风格参考图", base_style)
        self.assertNotIn("3D rendered miniature", base_style)
        self.assertNotIn("diorama", base_style.lower())
        self.assertNotIn("3D rendered miniature", visual_requirements)

    def test_recommend_styles_cli_skips_style_choices_when_style_reference_provided(self):
        request_path = self.out_dir / "request.json"
        req = base_request(
            assets={
                "qr_code_not_needed": True,
                "food_image_not_needed": True,
                "style_reference_images": ["/tmp/style-ref.png"],
                "style_reference_note": "完全参考这张图的构图、配色和字体层级",
            },
        )
        request_path.write_text(json.dumps(req, ensure_ascii=False), encoding="utf-8")
        args = Namespace(request=str(request_path), recommend_styles=5)

        with contextlib.redirect_stdout(io.StringIO()) as stdout:
            material_skill._run_recommend_styles(args)

        self.assertEqual(json.loads(stdout.getvalue()), [])

    def test_style_reference_selection_uses_virtual_style_and_skips_selector_api(self):
        req = base_request(
            assets={
                "qr_code_not_needed": True,
                "food_image_not_needed": True,
                "style_reference_images": ["/tmp/style-ref.png"],
            },
        )
        styles = material_skill.load_styles(req["type"])

        with mock.patch.object(material_skill, "call_template_selector_api") as selector_api:
            selector_api.side_effect = AssertionError("style reference must skip selector API")
            selection = material_skill.select_styles(
                req=req,
                styles=styles,
                count=2,
                provider="api",
                model="gpt-5.5",
                timeout=30,
                out_dir=self.out_dir,
            )

        self.assertEqual(len(selection.templates), 1)
        self.assertEqual(material_skill.style_id_of(selection.templates[0]), "style_reference_dominant")
        self.assertEqual(selection.audit["provider"], "style_reference")
        self.assertEqual(selection.audit["fallback_reason"], "style_reference_images_provided")
        self.assertEqual(
            [item["style_id"] for item in selection.audit["final_styles"]],
            ["style_reference_dominant"],
        )

    def test_recommend_atmospheres_alias_matches_recommend_styles(self):
        request_path = self.out_dir / "request.json"
        request_path.write_text(json.dumps(base_request(scene={"business_intent": "promotion"}), ensure_ascii=False), encoding="utf-8")
        styles_args = Namespace(request=str(request_path), recommend_styles=3)
        atmospheres_args = Namespace(request=str(request_path), recommend_atmospheres=3)

        with contextlib.redirect_stdout(io.StringIO()) as styles_stdout:
            material_skill._run_recommend_styles(styles_args)
        with contextlib.redirect_stdout(io.StringIO()) as atmospheres_stdout:
            material_skill._run_recommend_atmospheres(atmospheres_args)

        self.assertEqual(json.loads(styles_stdout.getvalue()), json.loads(atmospheres_stdout.getvalue()))

    def test_legacy_pre_selected_variants_raise_migration_error(self):
        request_path = self.out_dir / "request.json"
        request_path.write_text(json.dumps(base_request(), ensure_ascii=False), encoding="utf-8")
        args = Namespace(
            request=str(request_path),
            out=str(self.out_dir / "out"),
            variants=1,
            dpi=150,
            dry_run=True,
            image_provider="none",
            template_selector_provider="none",
            template_selector_model="gpt-5.5",
            template_selector_timeout=30,
            pre_selected_variants="bold_fiery",
        )

        with self.assertRaises(material_skill.SkillError) as context:
            material_skill.run(args)

        self.assertIn("--pre-selected-styles", str(context.exception))
        self.assertIn("bold_fiery", str(context.exception))

    def test_selector_api_uses_proxy_responses_without_tools(self):
        requests = []

        def fake_urlopen(request, timeout):
            requests.append((request, timeout))
            return FakeHTTPResponse({"output_text": "{\"ranked_variants\": []}"})

        with (
            mock.patch.object(material_skill.urllib.request, "urlopen", side_effect=fake_urlopen),
            mock.patch.object(material_skill, "RESPONSES_API_MODEL", "wrong-image-model"),
            mock.patch.object(material_skill, "PROXY_BASE_URL", "http://proxy.example/v1"),
            mock.patch.object(material_skill, "PROXY_TOKEN", "proxy-test-token"),
        ):
            response_text = material_skill.call_template_selector_api(
                system_prompt="Return JSON only.",
                user_prompt="Pick templates.",
                raw_response_path=self.raw_path,
                model="selector-model",
                timeout=30,
            )

        self.assertIn("ranked_variants", response_text)
        self.assertEqual(len(requests), 1)
        request, timeout = requests[0]
        self.assertEqual(timeout, 30)
        self.assertEqual(request.full_url, "http://proxy.example/v1/responses")
        self.assertEqual(request.headers["Authorization"], "Bearer proxy-test-token")
        payload = captured_request_payload(request)
        self.assertEqual(payload["model"], "selector-model")
        self.assertNotIn("tools", payload)
        self.assertEqual(payload["input"][0]["role"], "system")
        self.assertEqual(payload["input"][1]["role"], "user")

    def test_parse_selector_response_extracts_json_from_output_text(self):
        parsed = material_skill.parse_template_selection(
            json.dumps({"output_text": "{\"ranked_variants\": [{\"variant\": \"urban_chic\", \"score\": 0.91}]}"})
        )

        self.assertEqual(parsed["ranked_variants"][0]["variant"], "urban_chic")
        self.assertEqual(parsed["ranked_variants"][0]["score"], 0.91)

    def test_validate_and_rerank_keeps_top_then_diversifies_by_mood(self):
        templates = [
            {"variant": "a", "template_id": "a", "mood_group": "warm"},
            {"variant": "b", "template_id": "b", "mood_group": "warm"},
            {"variant": "c", "template_id": "c", "mood_group": "cool"},
            {"variant": "d", "template_id": "d", "mood_group": "hot"},
        ]
        parsed = {
            "ranked_variants": [
                {"variant": "a", "score": 0.95, "reason": "best"},
                {"variant": "missing", "score": 0.99, "reason": "invalid"},
                {"variant": "b", "score": 0.90, "reason": "same mood"},
                {"variant": "c", "score": 0.70, "reason": "explore"},
                {"variant": "d", "score": 2.0, "reason": "bad score"},
            ],
            "fallback_variant": "b",
        }

        selected, audit = material_skill.validate_and_rerank_selection(base_request(), templates, parsed, 3, "api")

        self.assertEqual([item["variant"] for item in selected], ["a", "c", "b"])
        self.assertTrue(any("unknown variant" in item for item in audit["validation_warnings"]))
        self.assertTrue(any("invalid score" in item for item in audit["validation_warnings"]))

    def test_validate_and_rerank_does_not_add_low_score_candidates_in_second_pass(self):
        templates = [
            {"variant": "urban_chic", "template_id": "urban", "mood_group": "cool"},
            {"variant": "low_same_mood", "template_id": "low", "mood_group": "cool"},
            {"variant": "bustling_warmth", "template_id": "fallback", "mood_group": "warm"},
        ]
        parsed = {
            "ranked_variants": [
                {"variant": "urban_chic", "score": 0.95, "reason": "best"},
                {"variant": "low_same_mood", "score": 0.10, "reason": "bad fit"},
            ],
            "fallback_variant": "bustling_warmth",
        }

        selected, _audit = material_skill.validate_and_rerank_selection(base_request(), templates, parsed, 2, "api")

        self.assertEqual([item["variant"] for item in selected], ["urban_chic", "bustling_warmth"])

    def test_provider_none_writes_selection_audit_and_uses_fallback(self):
        request_path = self.out_dir / "request.json"
        request_path.write_text(json.dumps(base_request(), ensure_ascii=False), encoding="utf-8")
        out_dir = self.out_dir / "out"
        args = Namespace(
            request=str(request_path),
            out=str(out_dir),
            variants=1,
            dpi=150,
            dry_run=True,
            image_provider="none",
            template_selector_provider="none",
            template_selector_model="gpt-5.5",
            template_selector_timeout=30,
        )

        rc = material_skill.run(args)

        self.assertEqual(rc, 0)
        audit = json.loads((out_dir / "template_selection.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["provider"], "none")
        self.assertEqual(audit["fallback_reason"], "provider=none")
        self.assertEqual(audit["raw_selector_text"], "")
        self.assertEqual(len(audit["final_styles"]), 1)

    def test_auto_provider_falls_back_when_selector_api_fails(self):
        def fake_urlopen(request, timeout):
            raise urllib.error.URLError("offline")

        req = base_request(title="冬至饺子馆", store={"name": "社区老店", "category": "饺子"})
        styles = material_skill.load_styles("营销海报")

        with mock.patch.object(material_skill.urllib.request, "urlopen", side_effect=fake_urlopen):
            selection = material_skill.select_styles(
                req=req,
                styles=styles,
                count=1,
                provider="auto",
                model="gpt-5.5",
                timeout=1,
                out_dir=self.out_dir,
            )

        self.assertEqual(selection.audit["fallback_reason"], "selector_api_failed")
        self.assertEqual(len(selection.templates), 1)

    def test_api_provider_raises_when_selector_api_fails(self):
        req = base_request()
        styles = [material_skill.load_styles("营销海报")[0]]

        with mock.patch.object(material_skill.urllib.request, "urlopen", side_effect=urllib.error.URLError("offline")):
            with self.assertRaises(material_skill.SkillError):
                material_skill.select_styles(
                    req=req,
                    styles=styles,
                    count=1,
                    provider="api",
                    model="gpt-5.5",
                    timeout=1,
                    out_dir=self.out_dir,
                )

    def test_load_styles_requires_core_fields(self):
        style_dir = self.out_dir / "styles"
        style_dir.mkdir()
        (style_dir / "bad.json").write_text(
            json.dumps({"template_id": "bad", "material_type": ["营销海报"], "scene_prompt": "x"}),
            encoding="utf-8",
        )

        with mock.patch.object(material_skill, "STYLE_DIR", style_dir):
            with self.assertRaises(material_skill.SkillError) as context:
                material_skill.load_styles("营销海报")

        self.assertIn("缺少必填字段", str(context.exception))
        self.assertIn("style_id", str(context.exception))

    def test_qr_asset_detection_covers_attachment_before_runtime_copy(self):
        req = base_request(assets={"qr_code_attachment": "/tmp/uploaded-qr.png", "food_image_not_needed": True})

        self.assertTrue(material_skill._has_qr_asset(req["assets"]))
        self.assertTrue(material_skill.request_summary_for_selector(req)["has_qr"])
        prompt = material_skill.build_scene_prompt(
            req,
            {"template_id": "unit", "variant": "urban_chic", "scene_prompt": "sleek room"},
            {"id": "layout", "name": "Layout"},
            1080,
            1440,
        )
        self.assertIn("qr_placement_output", json.loads(prompt))

    def test_minimal_layer_one_request_gets_safe_defaults(self):
        normalized, warnings = material_skill.normalize_request(
            {
                "type": "营销海报",
                "store": {"name": "山城火锅"},
            }
        )

        self.assertEqual(normalized["size"]["preset"], "3:4")
        self.assertEqual(normalized["title"], "山城火锅到店尝鲜")
        self.assertTrue(normalized["assets"]["qr_code_not_needed"])
        self.assertTrue(normalized["assets"]["food_image_not_needed"])
        self.assertTrue(any("未提及二维码" in warning for warning in warnings))

    def test_custom_pixel_size_is_preserved_as_canvas_size(self):
        normalized, _warnings = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"custom_px": {"width": "1242", "height": "1660"}},
                "store": {"name": "山城火锅"},
            }
        )

        self.assertEqual(normalized["size"]["custom_px"], {"width": 1242, "height": 1660})
        self.assertEqual(material_skill.canvas_size(normalized["size"], 150), (1242, 1660))

    def test_preset_canvas_sizes_use_gpt_image_two_friendly_defaults(self):
        expected_sizes = {
            "1:1": (1024, 1024),
            "2:3": (1024, 1536),
            "2:5": (1024, 2560),
            "3:4": (1024, 1360),
            "4:5": (1024, 1280),
            "9:16": (1024, 1824),
            "3:2": (1536, 1024),
            "4:3": (1360, 1024),
            "5:4": (1280, 1024),
            "16:9": (1824, 1024),
            "20:3": (10240, 1536),
        }

        for preset, expected in expected_sizes.items():
            with self.subTest(preset=preset):
                width, height = material_skill.canvas_size({"preset": preset}, 150)
                self.assertEqual((width, height), expected)
                self.assertEqual(width % 16, 0)
                self.assertEqual(height % 16, 0)

    def test_group_buying_context_without_qr_still_requires_qr_decision(self):
        with self.assertRaises(material_skill.SkillError) as context:
            material_skill.normalize_request(
                {
                    "type": "营销海报",
                    "size": {"preset": "3:4"},
                    "title": "团购优惠",
                    "store": {"name": "山城火锅", "category": "火锅"},
                    "campaign": {"offer": "双人套餐 68 元", "cta": "立即购买"},
                    "assets": {"food_image_not_needed": True},
                }
            )

        self.assertIn("二维码", str(context.exception))

    def test_no_qr_with_scan_cta_warns_instead_of_failing(self):
        normalized, warnings = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "3:4"},
                "title": "扫码领券",
                "store": {"name": "山城火锅", "category": "火锅"},
                "campaign": {"cta": "立即扫码"},
                "assets": {"qr_code_not_needed": True, "food_image_not_needed": True},
            }
        )

        self.assertTrue(normalized["assets"]["qr_code_not_needed"])
        self.assertTrue(any("qr_code_not_needed=true" in warning for warning in warnings))

    def test_use_meituan_logo_false_maps_to_no_platform_logo_without_warning(self):
        normalized, warnings = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "3:4"},
                "title": "春日尝鲜",
                "store": {"name": "山城火锅", "category": "火锅"},
                "campaign": {"offer": "到店享优惠"},
                "assets": {
                    "qr_code_not_needed": True,
                    "food_image_not_needed": True,
                    "use_meituan_logo": False,
                },
            }
        )

        self.assertEqual(normalized["assets"]["platform_logo"], "none")
        self.assertFalse(normalized["assets"]["use_meituan_logo"])
        self.assertFalse(any("必须带美团品牌 logo" in warning for warning in warnings))

    def test_auto_platform_logo_uses_dianping_for_explicit_dianping_context(self):
        normalized, _warnings = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "3:4"},
                "title": "大众点评必吃榜同款",
                "store": {"name": "山城火锅", "category": "火锅"},
                "campaign": {"offer": "到店尝鲜"},
                "assets": {"qr_code_not_needed": True, "food_image_not_needed": True},
            }
        )

        logo_path, white_logo_path, logo_label, logo_reason = material_skill.select_logo_asset(normalized)

        self.assertEqual(normalized["assets"]["platform_logo"], "auto")
        self.assertEqual(logo_path.name, "dianping-logo.png")
        self.assertEqual(white_logo_path.name, "dianping-white-logo.png")
        self.assertEqual(logo_label, "大众点评标识")
        self.assertIn("明确提到大众点评", logo_reason)

    def test_auto_platform_logo_does_not_treat_generic_review_as_dianping(self):
        normalized, _warnings = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "3:4"},
                "title": "顾客点评都说好",
                "store": {"name": "山城火锅", "category": "火锅"},
                "campaign": {"offer": "到店尝鲜"},
                "assets": {"qr_code_not_needed": True, "food_image_not_needed": True},
            }
        )

        logo_path, white_logo_path, logo_label, _logo_reason = material_skill.select_logo_asset(normalized)

        self.assertEqual(logo_path.name, "meituan-logo.png")
        self.assertEqual(white_logo_path.name, "meituan-white-logo.png")
        self.assertEqual(logo_label, "美团标识")

    def test_disabled_platform_logo_removes_prompt_safe_zone(self):
        req = base_request(
            title="春日尝鲜",
            campaign={"theme": "春日尝鲜", "offer": "到店享优惠"},
            assets={
                "qr_code_not_needed": True,
                "food_image_not_needed": True,
                "platform_logo": "none",
            },
        )

        prompt = material_skill.build_scene_prompt(
            req,
            {"template_id": "unit", "variant": "clean_hero", "scene_prompt": "clean poster"},
            {"id": "layout", "name": "Layout"},
            1080,
            1440,
        )
        prompt_json = json.loads(prompt)
        prompt_text = json.dumps(prompt_json, ensure_ascii=False)

        self.assertEqual(req["assets"]["platform_logo"], "none")
        self.assertNotIn("BRAND LOGO SAFE ZONE", prompt_text)
        self.assertNotIn("最终海报必须带", prompt_text)
        self.assertIn("不要自行生成、改造或替换美团/大众点评平台 logo", prompt_text)

    def test_copy_and_style_controls_are_preserved_in_prompt_sections(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "3:4"},
                "store": {"name": "町屋寿司", "category": "日料"},
                "copy": {
                    "dimensions": ["餐品特色", "品牌门店故事"],
                    "selected_text": "匠心寿司新鲜现做",
                },
                "style": {
                    "name": "美团餐饮团购",
                    "cuisine_tag": "日料",
                    "city_tag": "成都烟火气",
                    "tone": "热闹喜庆",
                    "realism": "artistic",
                    "style_factors": {
                        "lighting_accent": "neon_tint",
                        "texture_emphasis": "paper_cut",
                        "color_shift": "saturated_15",
                        "composition_accent": "diagonal_flow",
                    },
                },
                "assets": {"qr_code_not_needed": True, "food_image_not_needed": True},
            }
        )
        prompt = material_skill.build_scene_prompt(
            req,
            {"template_id": "unit", "variant": "urban_chic", "scene_prompt": "sleek sushi room"},
            {"id": "layout", "name": "Layout"},
            1080,
            1440,
        )
        prompt_json = json.loads(prompt)

        self.assertIn("prompt_sections", prompt_json)
        self.assertEqual(prompt_json["style"]["realism"], "artistic")
        self.assertEqual(prompt_json["copy"]["selected_text"], "匠心寿司新鲜现做")
        self.assertNotIn("dimensions", prompt_json["copy"])
        self.assertEqual(
            prompt_json["prompt_sections"]["COPY_ATMOSPHERE"]["visual_guidance"],
            [
                "ingredient craft, freshness, signature taste cues",
                "heritage, trust, time, craft and founder story atmosphere",
            ],
        )
        self.assertIn("热闹喜庆", prompt_json["prompt_sections"]["TONE_MODIFIER"])
        self.assertIn("neon_tint", json.dumps(prompt_json["prompt_sections"]["STYLE_FACTORS"], ensure_ascii=False))
        self.assertIn("only", prompt_json["prompt_sections"]["CUISINE_CITY"]["conflict_policy"])

    def test_user_selected_long_slogan_is_not_truncated_in_allowed_text(self):
        slogan = "金果山庄 柴火慢炖 鲜到骨子里"
        req, _ = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "3:4"},
                "title": slogan,
                "store": {"name": "金果山庄", "category": "柴火鸡"},
                "campaign": {"cta": "欢迎到店品尝"},
                "copy": {"selected_text": slogan},
                "assets": {"qr_code_not_needed": True, "food_image_not_needed": True},
            }
        )
        prompt = material_skill.build_scene_prompt(
            req,
            {"template_id": "unit", "variant": "bustling_warmth", "scene_prompt": "warm restaurant"},
            {"id": "layout", "name": "Layout"},
            1080,
            1440,
        )
        prompt_json = json.loads(prompt)

        self.assertIn(slogan, prompt_json["allowed_text"])
        self.assertNotIn("金果山庄 柴火慢炖 鲜到", prompt_json["allowed_text"])
        self.assertEqual(
            prompt_json["display_text_plan"][0]["suggested_lines"],
            ["金果山庄", "柴火慢炖", "鲜到骨子里"],
        )
        self.assertIn("不得截断", json.dumps(prompt_json["visual_requirements"], ensure_ascii=False))

    def test_product_reference_image_index_is_normalized_for_dish_labels(self):
        req, warnings = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "3:4"},
                "title": "招牌菜推荐",
                "store": {"name": "青禾小馆", "category": "融合菜"},
                "products": [
                    {"name": "姜母鸭", "reference_image_index": "1"},
                    {"name": "越界菜", "reference_image_index": 3},
                    {"name": "无效菜", "reference_image_index": "abc"},
                    {"name": "环境图说明"},
                ],
                "assets": {
                    "qr_code_not_needed": True,
                    "reference_images": ["/tmp/dish.jpg", "/tmp/env.jpg"],
                },
            }
        )

        self.assertEqual(req["products"][0]["reference_image_index"], 1)
        self.assertEqual(material_skill.dish_label_items(req)[0]["name"], "姜母鸭")
        self.assertEqual(material_skill.dish_label_items(req)[0]["reference_image_index"], 1)
        self.assertEqual(len(material_skill.dish_label_items(req)), 1)
        self.assertTrue(any("reference_image_index" in warning for warning in warnings))

    def test_normal_material_allows_dish_label_text_without_changing_display_plan(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "3:4"},
                "title": "招牌上新",
                "store": {"name": "青禾小馆", "category": "融合菜"},
                "copy": {"selected_text": "招牌上新"},
                "products": [{"name": "姜母鸭", "reference_image_index": 1}],
                "assets": {
                    "qr_code_not_needed": True,
                    "reference_images": ["/tmp/dish.jpg", "/tmp/env.jpg"],
                },
            }
        )
        prompt = material_skill.build_scene_prompt(
            req,
            {"template_id": "unit", "variant": "street_warm", "scene_prompt": "warm restaurant"},
            {"id": "layout", "name": "Layout"},
            1080,
            1440,
        )
        prompt_json = json.loads(prompt)
        requirements = json.dumps(prompt_json["visual_requirements"], ensure_ascii=False)

        self.assertIn("姜母鸭", prompt_json["allowed_text"])
        self.assertEqual(prompt_json["display_text_plan"][0]["text"], "招牌上新")
        self.assertNotIn("姜母鸭", [item["text"] for item in prompt_json["display_text_plan"]])
        self.assertEqual(prompt_json["dish_labels"][0]["name"], "姜母鸭")
        self.assertEqual(prompt_json["dish_labels"][0]["reference_image_index"], 1)
        self.assertIn("辅助菜名标签", requirements)
        self.assertIn("headline/copy/cta", requirements)

    def test_pure_atmosphere_blocks_dish_label_text(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "3:4"},
                "title": "招牌上新",
                "store": {"name": "青禾小馆", "category": "融合菜"},
                "copy": {"selected_text": "招牌上新"},
                "products": [{"name": "姜母鸭", "reference_image_index": 1}],
                "style": {"name": "美团餐饮团购", "ai_text_role": "pure_atmosphere"},
                "assets": {
                    "qr_code_not_needed": True,
                    "reference_images": ["/tmp/dish.jpg", "/tmp/env.jpg"],
                },
            }
        )
        prompt = material_skill.build_scene_prompt(
            req,
            {"template_id": "unit", "variant": "street_warm", "scene_prompt": "warm restaurant"},
            {"id": "layout", "name": "Layout"},
            1080,
            1440,
        )
        prompt_json = json.loads(prompt)
        requirements = json.dumps(prompt_json["visual_requirements"], ensure_ascii=False)

        self.assertEqual(prompt_json["allowed_text"], [])
        self.assertEqual(prompt_json["display_text_plan"], [])
        self.assertEqual(prompt_json["dish_labels"][0]["name"], "姜母鸭")
        self.assertIn("不要出现任何可读文字", requirements)
        self.assertIn("no text", prompt_json["negative"])
        self.assertIn("no words", prompt_json["negative"])
        self.assertIn("no letters", prompt_json["negative"])

    def test_exact_title_prompt_limits_visible_text_to_user_selected_copy(self):
        title = "大屏看球 冰啤畅饮"
        req, _ = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "4:3"},
                "title": title,
                "store": {"name": "太史令", "category": "餐厅"},
                "campaign": {"theme": "看球季", "cta": "看球季"},
                "copy": {"selected_text": title},
                "assets": {"qr_code_not_needed": True, "food_image_not_needed": True},
            }
        )
        prompt = material_skill.build_scene_prompt(
            req,
            {"template_id": "unit", "variant": "street_warm", "scene_prompt": "warm restaurant"},
            {"id": "layout", "name": "Layout"},
            1360,
            1024,
        )
        prompt_json = json.loads(prompt)
        requirements = json.dumps(prompt_json["visual_requirements"], ensure_ascii=False)

        self.assertEqual(prompt_json["allowed_text"], [title])
        self.assertEqual(prompt_json["display_text_plan"][0]["suggested_lines"], ["大屏看球", "冰啤畅饮"])
        self.assertIn("逐字复制", requirements)
        self.assertIn("禁止同音字", requirements)
        self.assertIn("冰啤畅饮", requirements)

    def test_world_cup_ip_terms_are_sanitized_and_blocked_in_prompt(self):
        req, warnings = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "3:4"},
                "title": "世界杯看球套餐",
                "store": {"name": "山城火锅", "category": "四川火锅"},
                "campaign": {
                    "theme": "FIFA World Cup 决赛夜",
                    "offer": "大力神杯同款氛围",
                    "cta": "世界杯期间到店看球",
                },
                "copy": {
                    "selected_text": "世界杯火锅之夜",
                    "generated_candidates": ["世界杯开涮", "大力神杯举杯"],
                },
                "assets": {"qr_code_not_needed": True, "food_image_not_needed": True},
            }
        )
        prompt = material_skill.build_scene_prompt(
            req,
            {"template_id": "unit", "variant": "bold_fiery", "scene_prompt": "hotpot match night"},
            {"id": "layout", "name": "Layout"},
            1080,
            1440,
        )
        prompt_json = json.loads(prompt)
        sanitized_payload = json.dumps(
            {
                "allowed_text": prompt_json["allowed_text"],
                "copy": prompt_json["copy"],
                "campaign": prompt_json["campaign"],
            },
            ensure_ascii=False,
        )
        negative_payload = prompt_json["negative"]
        requirements_payload = json.dumps(prompt_json["visual_requirements"], ensure_ascii=False)

        self.assertTrue(any("受保护 IP" in warning for warning in warnings))
        self.assertNotIn("世界杯", sanitized_payload)
        self.assertNotIn("World Cup", sanitized_payload)
        self.assertNotIn("FIFA", sanitized_payload)
        self.assertNotIn("大力神杯", sanitized_payload)
        self.assertIn("球赛", sanitized_payload)
        self.assertIn("看球", sanitized_payload)
        self.assertIn("no protected IP logos or characters", negative_payload)
        self.assertIn("no FIFA/World Cup/Olympic/NBA wording", negative_payload)
        self.assertIn("no World Cup trophy or Olympic rings", negative_payload)
        self.assertIn("严禁出现", requirements_payload)
        self.assertIn("世界杯", requirements_payload)

    def test_league_and_entertainment_ip_terms_are_sanitized_and_blocked_in_prompt(self):
        req, warnings = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "3:4"},
                "title": "NBA总决赛迪士尼主题套餐",
                "store": {"name": "欢聚餐厅", "category": "西餐"},
                "campaign": {
                    "theme": "Disney Marvel 观赛派对",
                    "offer": "米奇同款童趣布置",
                    "cta": "来店看NBA",
                },
                "copy": {
                    "selected_text": "NBA看球夜 迪士尼同款快乐",
                    "generated_candidates": ["漫威英雄开餐", "米奇陪你看球"],
                },
                "assets": {"qr_code_not_needed": True, "food_image_not_needed": True},
            }
        )
        prompt = material_skill.build_scene_prompt(
            req,
            {"template_id": "unit", "variant": "youthful_buzz", "scene_prompt": "party restaurant"},
            {"id": "layout", "name": "Layout"},
            1080,
            1440,
        )
        prompt_json = json.loads(prompt)
        sanitized_payload = json.dumps(
            {
                "allowed_text": prompt_json["allowed_text"],
                "copy": prompt_json["copy"],
                "campaign": prompt_json["campaign"],
            },
            ensure_ascii=False,
        )
        negative_payload = prompt_json["negative"]
        requirements_payload = json.dumps(prompt_json["visual_requirements"], ensure_ascii=False)

        self.assertTrue(any("受保护 IP" in warning for warning in warnings))
        for forbidden in ("NBA", "Disney", "迪士尼", "Marvel", "漫威", "米奇"):
            self.assertNotIn(forbidden, sanitized_payload)
        self.assertIn("篮球赛", sanitized_payload)
        self.assertIn("童趣", sanitized_payload)
        self.assertIn("超级英雄", sanitized_payload)
        self.assertIn("no Disney characters/castle", negative_payload)
        self.assertIn("no NBA logo/team jerseys", negative_payload)
        self.assertIn("受保护 IP", requirements_payload)

    def test_generic_hotpot_style_does_not_default_to_copper_pot(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "3:4"},
                "title": "沸腾红锅 鲜辣入魂",
                "store": {"name": "山城火锅", "category": "火锅"},
                "style": {"name": "美团餐饮团购", "cuisine_tag": "火锅"},
                "assets": {"qr_code_not_needed": True, "food_image_not_needed": True},
            }
        )
        prompt = material_skill.build_scene_prompt(
            req,
            {"template_id": "unit", "variant": "bold_fiery", "scene_prompt": "hotpot scene"},
            {"id": "layout", "name": "Layout"},
            1080,
            1440,
        )
        prompt_text = json.dumps(json.loads(prompt)["prompt_sections"]["CUISINE_CITY"], ensure_ascii=False)

        self.assertNotIn("copper pot", prompt_text)
        self.assertIn("hot pot steam", prompt_text)

    def test_sichuan_hotpot_style_explicitly_uses_red_oil_visuals(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "3:4"},
                "title": "沸腾红锅 鲜辣入魂",
                "store": {"name": "山城火锅", "category": "四川火锅"},
                "style": {"name": "美团餐饮团购", "cuisine_tag": "四川火锅"},
                "assets": {"qr_code_not_needed": True, "food_image_not_needed": True},
            }
        )
        prompt = material_skill.build_scene_prompt(
            req,
            {"template_id": "unit", "variant": "bold_fiery", "scene_prompt": "hotpot scene"},
            {"id": "layout", "name": "Layout"},
            1080,
            1440,
        )
        prompt_text = json.dumps(json.loads(prompt)["prompt_sections"]["CUISINE_CITY"], ensure_ascii=False)

        self.assertIn("Sichuan red oil", prompt_text)
        self.assertIn("nine-grid hotpot", prompt_text)

    def test_iteration_modify_scope_is_validated_as_enum(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "3:4"},
                "title": "春日尝鲜",
                "store": {"name": "山城火锅"},
                "assets": {"qr_code_not_needed": True, "food_image_not_needed": True},
                "iteration": {"modify_scope": "color_tone", "base_prompt_path": "/tmp/old/prompt.json"},
            }
        )

        self.assertEqual(req["iteration"]["modify_scope"], "color_tone")

        with self.assertRaises(material_skill.SkillError):
            material_skill.normalize_request(
                {
                    "type": "营销海报",
                    "size": {"preset": "3:4"},
                    "title": "春日尝鲜",
                    "store": {"name": "山城火锅"},
                    "assets": {"qr_code_not_needed": True, "food_image_not_needed": True},
                    "iteration": {"modify_scope": "freeform"},
                }
            )

    def test_digital_materials_use_dedicated_defaults_and_layouts(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "短视频封面",
                "store": {"name": "山城火锅"},
            }
        )
        width, height = material_skill.canvas_size(req["size"], 150)
        templates = material_skill.load_templates(req["type"])
        layout = material_skill.select_layout(req, templates[0], width, height)
        prompt = material_skill.build_scene_prompt(req, templates[0], layout, width, height)
        prompt_json = json.loads(prompt)

        self.assertEqual(req["size"]["preset"], "9:16")
        self.assertEqual(layout["id"], "layout_h")
        self.assertIn("标题安全区", prompt_json["prompt_sections"]["SCENE_ELEMENTS"]["composition_rule"])

    def test_panorama_material_uses_dedicated_defaults_layout_and_prompt(self):
        req, warnings = material_skill.normalize_request(
            {
                "type": "五连图",
                "title": "田园绿意一口尝鲜",
                "store": {"name": "青禾小馆", "category": "融合菜"},
                "assets": {
                    "qr_code_path": "/tmp/should_ignore_qr.png",
                    "reference_images": ["/tmp/dish.jpg"],
                    "brand_logo_path": "/tmp/logo.png",
                },
            }
        )
        width, height = material_skill.canvas_size(req["size"], 150)
        self.assertEqual(req["size"]["preset"], "20:3")
        self.assertEqual((width, height), (10240, 1536))
        self.assertTrue(req["assets"]["qr_code_not_needed"])
        self.assertFalse(material_skill._has_qr_asset(req["assets"]))
        self.assertFalse(req["assets"]["disclaimer_overlay"])
        self.assertFalse(req["assets"]["use_meituan_logo"])
        self.assertEqual(req["assets"]["panorama_slice_requested"], False)
        self.assertFalse(any("必须带美团品牌 logo" in warning for warning in warnings))

        templates = material_skill.load_templates(req["type"])
        layout = material_skill.select_layout(req, templates[0], width, height)
        prompt = material_skill.build_scene_prompt(req, templates[0], layout, width, height)
        prompt_json = json.loads(prompt)
        prompt_text = json.dumps(prompt_json, ensure_ascii=False)

        self.assertEqual(layout["id"], "layout_panorama")
        self.assertEqual(material_skill.ratio_kind(width, height), "ultra_wide")
        self.assertEqual(prompt_json["material_type"], "五连图")
        self.assertNotIn("title_overlay", prompt_json)
        self.assertEqual(prompt_json["title_instruction"]["mode"], "model_integrated_text")
        self.assertIn("田园绿意一口尝鲜", prompt_json["title_instruction"]["text"])
        self.assertIn("与菜品和背景一起绘制", prompt_text)
        self.assertIn("safe_text_box_ratio", prompt_json["title_instruction"])
        self.assertNotIn("no readable text", prompt_json["negative"])
        self.assertNotIn("no title text", prompt_json["negative"])
        self.assertNotIn("no Chinese characters", prompt_json["negative"])
        self.assertIn("visual_density", prompt_json)
        self.assertIn("text_placement", prompt_json)
        self.assertIn("hero_visual_anchor", prompt_json)
        self.assertIn("第一张实拍图", prompt_text)
        self.assertIn("最重要", prompt_text)
        self.assertIn("自然融入", prompt_text)
        self.assertIn("Logo", prompt_text)
        self.assertNotIn("qr_placement_output", prompt_json)
        self.assertNotIn("BRAND LOGO SAFE ZONE", prompt_text)
        self.assertNotIn("AI辅助生成", prompt_text)

    def test_panorama_prompt_includes_first_screen_slice_and_density_aware_background_contract(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "五连图",
                "title": "现配现炒味道真好",
                "store": {"name": "油菜花香", "category": "江西菜"},
                "style": {"cuisine_tag": "川湘菜", "city_tag": "江西乡野"},
                "assets": {"reference_images": ["/tmp/dish.jpg"]},
            }
        )
        style = next(item for item in material_skill.load_styles("五连图") if item["style_id"] == "natural_earth")
        layout = material_skill.select_layout(req, style, 10240, 1536)
        prompt = material_skill.build_scene_prompt(req, style, layout, 3840, 1280)
        prompt_json = json.loads(prompt)
        prompt_text = json.dumps(prompt_json, ensure_ascii=False)

        self.assertIn("homepage_first_screen_rule", prompt_json)
        self.assertIn("slice_storyboard", prompt_json)
        self.assertIn("slice_seam_safety_policy", prompt_json)
        self.assertNotIn("slice_boundary_safety", prompt_json)
        self.assertIn("panorama_style_family", prompt_json)
        self.assertIn("panorama_background_design", prompt_json)
        self.assertIn("visual_density", prompt_json)
        self.assertEqual(prompt_json["visual_density"], "rich")
        self.assertIn("density_direction", prompt_json["panorama_style_family"])
        self.assertIn("首屏约 2.5 张", prompt_text)
        self.assertIn("slice_01", prompt_text)
        self.assertIn("slice_02", prompt_text)
        self.assertIn("slice_03", prompt_text)
        self.assertIn("20%", prompt_text)
        self.assertIn("40%", prompt_text)
        self.assertIn("60%", prompt_text)
        self.assertIn("80%", prompt_text)
        self.assertIn("底色材质层", prompt_text)
        self.assertIn("空间/氛围暗示", prompt_text)
        self.assertNotIn("视频/相册遮挡避让", prompt_text)
        self.assertNotIn("视频", json.dumps(prompt_json["slice_seam_safety_policy"], ensure_ascii=False))
        self.assertNotIn("相册", json.dumps(prompt_json["slice_seam_safety_policy"], ensure_ascii=False))

    def test_panorama_prompt_uses_density_permissions_instead_of_global_complete_dish_ban(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "五连图",
                "title": "广西本地味现炒现上桌",
                "store": {"name": "桂味小馆", "category": "广西菜"},
                "style": {"cuisine_tag": "广西菜", "city_tag": "广西街巷", "visual_density": "explosive"},
                "assets": {"reference_images": ["/tmp/main-dish.jpg", "/tmp/side-dish.jpg"]},
            }
        )
        style = next(item for item in material_skill.load_styles("五连图") if item["style_id"] == "street_warm")
        layout = material_skill.select_layout(req, style, 10240, 1536)
        prompt_json = json.loads(material_skill.build_scene_prompt(req, style, layout, 3840, 1280))
        prompt_text = json.dumps(prompt_json, ensure_ascii=False)
        negative = prompt_json["negative"]

        self.assertIn("dish_showcase_integrity", prompt_json)
        self.assertIn("crop_and_layering_permissions", prompt_json)
        self.assertIn("hero_visual_anchor", prompt_json)
        self.assertIn("dish_scale_rule", prompt_json)
        self.assertIn("recognizable and appetizing", prompt_text)
        self.assertIn("controlled partial bleed on side or top edges only", prompt_text)
        self.assertIn("never into the bottom white margin", prompt_text)
        self.assertIn("no severe crop", prompt_text)
        self.assertIn("non-uniform food scale", prompt_text)
        self.assertNotIn("所有锅、盘、碗、蒸笼必须完整露出完整外轮廓", prompt_text)
        self.assertNotIn("主菜高度建议控制在内容带高度 36%-48%", prompt_text)
        self.assertNotIn("cropped dishes", negative)
        self.assertNotIn("no dish cropped for dramatic close-up", negative)
        self.assertIn("no severely cropped hero dish", negative)
        self.assertIn("no main plate/pot/bowl/serving vessel cut off at bottom edge", negative)

    def test_panorama_prompt_uses_visual_anchoring_instead_of_floating_table_ban(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "五连图",
                "title": "川韵家常菜热辣上桌",
                "store": {"name": "川藤小馆", "category": "川菜"},
                "style": {"cuisine_tag": "川菜", "city_tag": "成都街巷"},
                "assets": {"reference_images": ["/tmp/fish.jpg", "/tmp/chicken.jpg", "/tmp/noodle.jpg"]},
            }
        )
        style = next(item for item in material_skill.load_styles("五连图") if item["style_id"] == "street_warm")
        layout = material_skill.select_layout(req, style, 10240, 1536)
        prompt_json = json.loads(material_skill.build_scene_prompt(req, style, layout, 3840, 1280))
        prompt_text = json.dumps(prompt_json, ensure_ascii=False)
        negative = prompt_json["negative"]

        self.assertIn("visual_anchoring_policy", prompt_json)
        self.assertNotIn("dish_vertical_centering", prompt_json)
        self.assertIn("visually anchored", prompt_text)
        self.assertIn("not weightless", prompt_text)
        self.assertIn("plate rim", prompt_text)
        self.assertIn("flame/steam base", prompt_text)
        self.assertIn("small partial counter edge", prompt_text)
        self.assertIn("not as the main background or stage", prompt_text)
        self.assertNotIn("菜品悬浮在画面正中央", prompt_text)
        self.assertNotIn("像漂浮在空中", prompt_text)
        self.assertNotIn("no visible dining table surface", negative)
        self.assertNotIn("no food sitting on a table", negative)
        self.assertNotIn("no horizontal tabletop line behind dishes", negative)
        self.assertIn("avoid large visible dining tables", negative)

    def test_panorama_negative_allows_scene_backgrounds_instead_of_banning_physical_context(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "五连图",
                "title": "烟火老店热气腾腾",
                "store": {"name": "半重山老火锅", "category": "重庆火锅"},
                "assets": {"reference_images": ["/tmp/hotpot.jpg"]},
            }
        )
        style = next(item for item in material_skill.load_styles("五连图") if item["style_id"] == "street_warm")
        layout = material_skill.select_layout(req, style, 10240, 1536)
        prompt_json = json.loads(material_skill.build_scene_prompt(req, style, layout, 3840, 1280))
        negative = prompt_json["negative"]

        for forbidden in ("no restaurant interior", "no wall", "no physical environment", "no visible dining table surface"):
            self.assertNotIn(forbidden, negative)
        self.assertIn("avoid large visible dining tables", negative)

    def test_panorama_helper_infers_density_cuisine_and_text_placement_with_fallbacks(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "五连图",
                "title": "热辣火锅夜宵局",
                "store": {"name": "半重山老火锅", "category": "重庆火锅"},
                "style": {"visual_density": "heavy", "text_placement": "diagonal", "cuisine_tag": "火锅"},
                "assets": {"reference_images": ["/tmp/hotpot.jpg"]},
            }
        )

        self.assertEqual(material_skill._infer_visual_density(req, "clean_hero"), "clean")
        self.assertEqual(material_skill._infer_cuisine_family(req), "火锅专门")
        self.assertEqual(material_skill._resolve_text_placement(req, "clean_hero", "火锅专门", "clean"), "left_block")

    def test_panorama_negative_contains_only_prohibitions_not_permissions(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "五连图",
                "title": "爆款烧烤热辣上桌",
                "store": {"name": "阿强烧烤", "category": "烧烤"},
                "style": {"visual_density": "explosive"},
                "assets": {"reference_images": ["/tmp/skewer.jpg"]},
            }
        )
        style = next(item for item in material_skill.load_styles("五连图") if item["style_id"] == "bold_split")
        layout = material_skill.select_layout(req, style, 10240, 1536)
        prompt_json = json.loads(material_skill.build_scene_prompt(req, style, layout, 3840, 1280))
        phrases = [phrase.strip().lower() for phrase in prompt_json["negative"].split(",")]
        forbidden_permission_phrases = ("may crop", "may be", "allowed", "encouraged", "can be", "should ")

        self.assertTrue(phrases)
        for phrase in phrases:
            self.assertFalse(
                any(forbidden in phrase for forbidden in forbidden_permission_phrases),
                f"negative phrase contains positive permission: {phrase}",
            )
        self.assertIn("controlled partial bleed", prompt_json["crop_and_layering_permissions"])
        self.assertNotIn("controlled partial bleed", prompt_json["negative"])

    def test_panorama_text_placement_modes_keep_structure_ratios_and_hero_position_aligned(self):
        cases = [
            ("left_block", {"x": 0.035, "y": 0.167, "w": 0.345, "h": 0.667}, "slice_02-03 center-right"),
            ("center_overlay", {"x": 0.25, "y": 0.15, "w": 0.30, "h": 0.70}, "slice_01-02 center"),
            ("top_banner", {"x": 0.05, "y": 0.10, "w": 0.55, "h": 0.25}, "slice_02-03 lower-center"),
        ]
        style = next(item for item in material_skill.load_styles("五连图") if item["style_id"] == "bold_split")
        for mode, ratio, hero_position in cases:
            with self.subTest(mode=mode):
                req, _ = material_skill.normalize_request(
                    {
                        "type": "五连图",
                        "title": "招牌菜连连看",
                        "store": {"name": "青禾小馆", "category": "融合菜"},
                        "style": {"text_placement": mode},
                        "assets": {"reference_images": ["/tmp/dish.jpg"]},
                    }
                )
                layout = material_skill.select_layout(req, style, 10240, 1536)
                prompt_json = json.loads(material_skill.build_scene_prompt(req, style, layout, 3840, 1280))
                prompt_text = json.dumps(prompt_json, ensure_ascii=False)

                self.assertEqual(prompt_json["text_placement"], mode)
                self.assertEqual(prompt_json["title_instruction"]["safe_text_box_ratio"], ratio)
                self.assertIn(hero_position, prompt_json["hero_visual_anchor"]["position"])
                self.assertIn(mode, prompt_text)

    def test_panorama_style_reference_note_can_drive_left_block_instead_of_forced_center(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "五连图",
                "title": "招牌菜连连看",
                "store": {"name": "青禾小馆", "category": "融合菜"},
                "assets": {
                    "reference_images": ["/tmp/dish.jpg"],
                    "style_reference_images": ["/tmp/left-title-ref.png"],
                    "style_reference_note": "参考图标题在左侧窄栏，菜品在右侧大面积展示",
                },
            }
        )
        style = next(item for item in material_skill.load_styles("五连图") if item["style_id"] == "natural_earth")
        layout = material_skill.select_layout(req, style, 10240, 1536)
        prompt_json = json.loads(material_skill.build_scene_prompt(req, style, layout, 3840, 1280))

        self.assertEqual(prompt_json["text_placement"], "left_block")
        self.assertNotEqual(prompt_json["text_placement"], "center_overlay")
        self.assertIn("左侧窄栏", prompt_json["composition_structure"])

    def test_panorama_prompt_enumerates_only_mapped_dish_references(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "五连图",
                "title": "招牌菜连连看",
                "store": {"name": "青禾小馆", "category": "融合菜"},
                "products": [
                    {"name": "姜母鸭", "reference_image_index": 1},
                    {"name": "", "reference_image_index": 3, "description": "招牌热菜"},
                ],
                "assets": {
                    "reference_images": ["/tmp/dish-1.jpg", "/tmp/env.jpg", "/tmp/dish-3.jpg"],
                },
            }
        )
        style = next(item for item in material_skill.load_styles("五连图") if item["style_id"] == "street_warm")
        layout = material_skill.select_layout(req, style, 10240, 1536)
        prompt_json = json.loads(material_skill.build_scene_prompt(req, style, layout, 3840, 1280))
        prompt_text = json.dumps(prompt_json, ensure_ascii=False)

        self.assertIn("exactly 2 dishes", prompt_text)
        self.assertIn("姜母鸭", prompt_text)
        self.assertIn("dish from reference photo #3", prompt_text)
        self.assertIn("ambient/environment reference photo #2", prompt_text)
        self.assertIn("dish_showcase_references", prompt_json)
        self.assertNotIn("其余菜品依次向右", prompt_json["photo_reference_instruction"])
        self.assertNotIn("no dish-name labels or captions next to food items", prompt_json["negative"])

    def test_panorama_prompt_keeps_dish_label_ban_without_valid_labels(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "五连图",
                "title": "招牌菜连连看",
                "store": {"name": "青禾小馆", "category": "融合菜"},
                "assets": {"reference_images": ["/tmp/dish.jpg"]},
            }
        )
        style = next(item for item in material_skill.load_styles("五连图") if item["style_id"] == "street_warm")
        layout = material_skill.select_layout(req, style, 10240, 1536)
        prompt_json = json.loads(material_skill.build_scene_prompt(req, style, layout, 3840, 1280))

        self.assertIn("no dish-name labels or captions next to food items", prompt_json["negative"])

    def test_panorama_material_requires_one_to_six_reference_images(self):
        for images in ([], [f"/tmp/ref_{idx}.jpg" for idx in range(7)]):
            with self.assertRaises(material_skill.SkillError):
                material_skill.normalize_request(
                    {
                        "type": "五连图",
                        "title": "夏日麻辣季",
                        "assets": {"reference_images": images},
                    }
                )

        req, _ = material_skill.normalize_request(
            {
                "type": "五连图",
                "title": "夏日麻辣季",
                "assets": {"reference_images": ["/tmp/one.jpg"]},
            }
        )
        self.assertEqual(req["assets"]["reference_images"], ["/tmp/one.jpg"])

    def test_skill_doc_tells_agent_not_to_ask_panorama_disclaimer_option(self):
        skill_doc = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")
        self.assertIn("五连图首轮不询问二维码、AI 标注或平台品牌标识", skill_doc)
        self.assertIn("五连图不提供 AI 标注/平台标识选项", skill_doc)

    def test_skill_doc_mentions_optional_dish_names_for_reference_images(self):
        skill_doc = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("可一并提供菜品名称", skill_doc)
        self.assertIn("第1张：姜母鸭；第3张：Salted Abalone；第4张：环境图不标名", skill_doc)
        self.assertIn("AI 对复杂文字和生僻字可能写错", skill_doc)
        self.assertIn("菜名尽量用常见、简短写法", skill_doc)
        self.assertIn("避免复杂字、生僻字和长串文字", skill_doc)
        self.assertIn("跳过菜名", skill_doc)
        self.assertIn("不标菜名", skill_doc)
        self.assertIn("第1张到第N张", skill_doc)
        self.assertIn("不得只询问部分图片", skill_doc)
        self.assertIn("缺失序号", skill_doc)
        self.assertIn("当用户未提供有效菜品名映射或明确选择跳过菜名/不标菜名时", skill_doc)
        self.assertIn("当用户提供有效菜品名映射时", skill_doc)
        self.assertIn("id: real_photos", skill_doc)

    def test_skill_doc_requires_final_full_review_reminder_for_all_materials(self):
        skill_doc = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("适用于所有物料类型，包括五连图", skill_doc)
        self.assertIn("对最终物料做全面检查", skill_doc)
        self.assertIn("尤其重点检查文字和菜品", skill_doc)
        self.assertIn("文字准确性", skill_doc)
        self.assertIn("菜品准确性", skill_doc)
        self.assertIn("大象/catclaw 消息场景", skill_doc)
        self.assertIn("必须发送人工审核提醒", skill_doc)
        self.assertIn("同一条大象/CatClaw 对话", skill_doc)
        self.assertIn("不得因为图片已通过文件工具发送、或 dx-push 已自动通知管理员，就省略面向用户的提醒", skill_doc)

    def test_skill_doc_uses_panorama_wait_time_and_postprocess_contract(self):
        skill_doc = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("AI 绘图通常需要 5～15 分钟，请稍等", skill_doc)
        self.assertIn("裁出完整内容带", skill_doc)
        self.assertIn("强制 resize 到 `10240×1536`（允许变形）", skill_doc)

    def test_appetite_brightening_style_templates_and_options_avoid_dark_defaults(self):
        styles = {item["style_id"]: item for item in material_skill.load_styles("营销海报")}
        bold_prompt = styles["bold_split"]["scene_prompt"]
        festive_prompt = styles["festive_red"]["scene_prompt"]
        oversize_prompt = styles["oversize_type"]["scene_prompt"]
        controls = material_skill._style_controls()
        options_text = (Path(__file__).resolve().parents[1] / "references" / "options.json").read_text(encoding="utf-8")
        skill_doc = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")

        self.assertNotIn("红黑撞色", bold_prompt)
        self.assertNotIn("charcoal black #15100E", bold_prompt)
        self.assertIn("奶油白", bold_prompt)
        self.assertNotIn("deep red #8F1118", festive_prompt)
        self.assertIn("亮朱红", festive_prompt)
        self.assertNotIn("charcoal", oversize_prompt.lower())
        self.assertNotIn("forest black", oversize_prompt.lower())
        for scheme in ("(a)", "(b)", "(c)", "(d)"):
            self.assertIn(scheme, oversize_prompt)
        self.assertNotIn("红黑撞色", controls["panorama_style_families"]["bold_split"]["background"])
        self.assertNotIn("红黑撞色", options_text)
        self.assertIn("默认优先明亮开胃", skill_doc)

    def test_negative_string_and_negative_style_are_in_scene_prompt(self):
        req = base_request()
        template = {
            "template_id": "unit",
            "variant": "urban_chic",
            "scene_prompt": "sleek room",
            "negative": "no old string artifact, no legacy issue",
            "negative_style": ["no wedding poster", "no cheap gold texture"],
        }

        prompt = material_skill.build_scene_prompt(req, template, {"id": "layout", "name": "Layout"}, 1080, 1440)
        prompt_json = json.loads(prompt)

        self.assertIn("no old string artifact", prompt_json["negative"])
        self.assertIn("no wedding poster", prompt_json["negative"])
        self.assertIn("no cheap gold texture", prompt_json["negative"])

    def test_all_core_styles_load_for_poster(self):
        styles = material_skill.load_styles("营销海报")
        style_ids = {item["style_id"] for item in styles}

        self.assertGreater(len(styles), 12)
        self.assertTrue(
            {
                "clean_hero",
                "dark_premium",
                "natural_earth",
                "bold_split",
                "collage_pop",
                "street_warm",
                "neon_night",
                "festive_red",
                "illustration_flat",
                "ink_oriental",
                "retro_poster",
                "latin_fiesta",
            }.issubset(style_ids)
        )

    def test_parallel_variant_failure_is_recorded_in_manifest(self):
        request_path = self.out_dir / "request.json"
        request_path.write_text(json.dumps(base_request(), ensure_ascii=False), encoding="utf-8")
        out_dir = self.out_dir / "partial"
        args = Namespace(
            request=str(request_path),
            out=str(out_dir),
            variants=2,
            dpi=150,
            dry_run=False,
            image_provider="api",
            template_selector_provider="none",
            template_selector_model="gpt-5.5",
            template_selector_timeout=30,
        )

        def fake_process(index, template, req, runtime, width, height, out_path, provider, dry_run, qr_shared, *extra_args):
            if index == 2:
                raise material_skill.SkillError("variant boom")
            png_path = out_path / "materials" / "material_01.png"
            material_skill.write_mock_png(png_path, width, height, template.get("style_id", "clean_hero"))
            return material_skill.GeneratedVariant(
                index=index,
                template=template,
                layout={"id": "layout", "name": "Layout"},
                prompt="{}",
                prompt_path=out_path / "variants" / "variant_01" / "prompt.json",
                scene_image_path=out_path / "materials" / "material_01_ai.png",
                final_no_qr_path=None,
                final_path=png_path,
                png_path=png_path,
                raw_response_path=None,
            )

        with mock.patch.object(material_skill, "_process_single_variant", side_effect=fake_process):
            rc = material_skill.run(args)

        self.assertEqual(rc, 0)
        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["variants"]), 1)
        self.assertEqual(manifest["failed_variants"], [{"index": 2, "error": "variant boom"}])

    def test_fastest_completed_variant_becomes_option_one_with_stable_delivery_path(self):
        request_path = self.out_dir / "request.json"
        request_path.write_text(json.dumps(base_request(), ensure_ascii=False), encoding="utf-8")
        out_dir = self.out_dir / "completion_order"
        args = Namespace(
            request=str(request_path),
            out=str(out_dir),
            variants=2,
            dpi=150,
            dry_run=False,
            image_provider="api",
            template_selector_provider="none",
            template_selector_model="gpt-5.5",
            template_selector_timeout=30,
        )
        seen_fast_path_options = []

        def fake_process(*call_args, **_kwargs):
            index = call_args[0]
            template = call_args[1]
            width = call_args[4]
            height = call_args[5]
            out_path = call_args[6]
            seen_fast_path_options.append(
                {
                    "timeout": call_args[10],
                    "max_attempts": call_args[11],
                    "retryable_codes": call_args[12],
                    "verify_qr_scan": call_args[13],
                }
            )
            time.sleep(0.20 if index == 1 else 0.01)
            png_path = out_path / "materials" / f"material_{index:02d}.png"
            material_skill.write_mock_png(png_path, width, height, template.get("style_id", "clean_hero"))
            return material_skill.GeneratedVariant(
                index=index,
                template=template,
                layout={"id": "layout", "name": "Layout"},
                prompt="{}",
                prompt_path=out_path / "variants" / f"variant_{index:02d}" / "prompt.json",
                scene_image_path=out_path / "materials" / f"material_{index:02d}_ai.png",
                final_no_qr_path=None,
                final_path=png_path,
                png_path=png_path,
                raw_response_path=None,
            )

        stdout = io.StringIO()
        with (
            mock.patch.object(material_skill, "_process_single_variant", side_effect=fake_process),
            contextlib.redirect_stdout(stdout),
        ):
            rc = material_skill.run(args)

        self.assertEqual(rc, 0)
        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
        first = manifest["variants"][0]
        second = manifest["variants"][1]
        self.assertEqual(first["display_index"], 1)
        self.assertEqual(first["source_index"], 2)
        self.assertTrue(first["delivery_material"].endswith("/option_01.png"))
        self.assertTrue(Path(first["delivery_material"]).exists())
        self.assertEqual(second["display_index"], 2)
        self.assertEqual(second["source_index"], 1)
        self.assertIn("[option 01 ready]", stdout.getvalue())
        self.assertIn("source_style=02", stdout.getvalue())
        self.assertTrue(seen_fast_path_options)
        self.assertTrue(all(item["timeout"] == material_skill.DEFAULT_IMAGE_TIMEOUT for item in seen_fast_path_options))
        self.assertTrue(all(item["max_attempts"] == 2 for item in seen_fast_path_options))
        self.assertTrue(all(item["retryable_codes"] == {408, 429} for item in seen_fast_path_options))
        self.assertTrue(all(item["verify_qr_scan"] is False for item in seen_fast_path_options))

    def test_pre_selected_styles_skips_template_selector_and_uses_all_requested_styles(self):
        request_path = self.out_dir / "request.json"
        request_path.write_text(json.dumps(base_request(), ensure_ascii=False), encoding="utf-8")
        out_dir = self.out_dir / "pre_selected_many"
        args = Namespace(
            request=str(request_path),
            out=str(out_dir),
            variants=2,
            dpi=150,
            dry_run=True,
            image_provider="none",
            template_selector_provider="api",
            template_selector_model="gpt-5.5",
            template_selector_timeout=30,
            pre_selected_styles="clean_hero,natural_earth",
        )

        with mock.patch.object(material_skill, "call_template_selector_api") as selector_api:
            selector_api.side_effect = AssertionError("pre-selected styles must skip selector API")
            rc = material_skill.run(args)

        self.assertEqual(rc, 0)
        audit = json.loads((out_dir / "template_selection.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["provider"], "pre_selected")
        self.assertEqual(
            [item["style_id"] for item in audit["final_styles"]],
            ["clean_hero", "natural_earth"],
        )

    def _write_panorama_request(self, request_path: Path) -> Path:
        ref = self.out_dir / f"{request_path.stem}_dish.png"
        material_skill.write_mock_png(ref, 96, 96, "dish")
        request_path.write_text(
            json.dumps(
                base_request(
                    type="五连图",
                    title="招牌菜连连看",
                    store={"name": "街角小馆", "category": "简餐"},
                    assets={"reference_images": [str(ref)]},
                    products=[{"reference_image_index": 1, "name": "招牌牛肉饭"}],
                ),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        return ref

    def _panorama_args(self, request_path: Path, out_dir: Path, **overrides):
        values = {
            "request": str(request_path),
            "out": str(out_dir),
            "variants": 2,
            "dpi": 150,
            "dry_run": True,
            "image_provider": "none",
            "template_selector_provider": "none",
            "template_selector_model": "gpt-5.5",
            "template_selector_timeout": 30,
            "pre_selected_styles": "",
        }
        values.update(overrides)
        return Namespace(**values)

    def test_panorama_variants_two_default_branch_generates_single_delivery_set(self):
        request_path = self.out_dir / "panorama_default_request.json"
        self._write_panorama_request(request_path)
        out_dir = self.out_dir / "panorama_default"

        rc = material_skill.run(self._panorama_args(request_path, out_dir, variants=2))

        self.assertEqual(rc, 0)
        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["variants"]), 1)
        self.assertIn("五连图固定只生成 1 套方案", "\n".join(manifest["warnings"]))
        delivery_dir = Path(manifest["delivery_dir"])
        self.assertTrue((delivery_dir / "option_01.png").exists())
        self.assertEqual(len(list((delivery_dir / "option_01_slices").glob("slice_*.png"))), 5)
        self.assertFalse((delivery_dir / "option_02.png").exists())
        self.assertFalse((delivery_dir / "option_02_slices").exists())

    def test_panorama_pre_selected_styles_uses_first_and_audits_ignored_styles(self):
        request_path = self.out_dir / "panorama_preselected_request.json"
        self._write_panorama_request(request_path)
        out_dir = self.out_dir / "panorama_preselected"

        with mock.patch.object(material_skill, "call_template_selector_api") as selector_api:
            selector_api.side_effect = AssertionError("pre-selected styles must skip selector API")
            rc = material_skill.run(
                self._panorama_args(
                    request_path,
                    out_dir,
                    variants=2,
                    pre_selected_styles="clean_hero,natural_earth",
                )
            )

        self.assertEqual(rc, 0)
        audit = json.loads((out_dir / "template_selection.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["provider"], "pre_selected")
        self.assertEqual(audit["pre_selected_styles"], ["clean_hero"])
        self.assertEqual(audit["ignored_pre_selected_styles"], ["natural_earth"])
        self.assertEqual([item["style_id"] for item in audit["final_styles"]], ["clean_hero"])
        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["variants"]), 1)
        self.assertEqual(manifest["variants"][0]["style_id"], "clean_hero")

    def test_panorama_single_pre_selected_style_stays_single_without_ignored_audit(self):
        request_path = self.out_dir / "panorama_single_request.json"
        self._write_panorama_request(request_path)
        out_dir = self.out_dir / "panorama_single"

        rc = material_skill.run(
            self._panorama_args(
                request_path,
                out_dir,
                variants=1,
                pre_selected_styles="clean_hero",
            )
        )

        self.assertEqual(rc, 0)
        audit = json.loads((out_dir / "template_selection.json").read_text(encoding="utf-8"))
        self.assertNotIn("ignored_pre_selected_styles", audit)
        self.assertEqual([item["style_id"] for item in audit["final_styles"]], ["clean_hero"])
        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["variants"]), 1)
        self.assertEqual(manifest["variants"][0]["style_id"], "clean_hero")

    def test_panorama_run_uses_limited_retries_and_dx_push_only_long_image_after_done(self):
        request_path = self.out_dir / "panorama_dx_request.json"
        self._write_panorama_request(request_path)
        out_dir = self.out_dir / "panorama_dx"
        seen_options = []
        seen_dx_status = []
        seen_dx_files = []

        def fake_process(*call_args, **_kwargs):
            index = call_args[0]
            template = call_args[1]
            out_path = call_args[6]
            seen_options.append({
                "timeout": call_args[10],
                "max_attempts": call_args[11],
                "retryable_codes": call_args[12],
                "verify_qr_scan": call_args[13],
            })
            png_path = out_path / "materials" / f"material_{index:02d}.png"
            material_skill.write_mock_png(png_path, 32, 32, template.get("style_id", "clean_hero"))
            return material_skill.GeneratedVariant(
                index=index,
                template=template,
                layout={"id": "layout_panorama", "name": "五连图长画布连贯型"},
                prompt="{}",
                prompt_path=out_path / "variants" / f"variant_{index:02d}" / "prompt.json",
                scene_image_path=png_path,
                final_no_qr_path=None,
                final_path=png_path,
                png_path=png_path,
                raw_response_path=None,
                provider_warnings=["AI 海报来源: Responses API (gpt-5.5 + image_generation)"],
            )

        def fake_publish(variant, delivery_dir, display_index, **_kwargs):
            delivery_dir.mkdir(parents=True, exist_ok=True)
            long_path = delivery_dir / f"option_{display_index:02d}.png"
            material_skill.write_mock_png(long_path, 32, 32, "clean_hero")
            slice_dir = delivery_dir / f"option_{display_index:02d}_slices"
            slice_dir.mkdir(parents=True, exist_ok=True)
            variant.delivery_slices = []
            for idx in range(1, 6):
                path = slice_dir / f"slice_{idx:02d}.png"
                material_skill.write_mock_png(path, 16, 16, "street_warm")
                variant.delivery_slices.append(path)
            variant.delivery_material_path = long_path
            variant.display_index = display_index
            variant.source_index = variant.index
            variant.completed_at = "now"
            return long_path

        def fake_dx_push(push_out_dir, files, proxy_api_used):
            seen_dx_status.append(json.loads((push_out_dir / "status.json").read_text(encoding="utf-8"))["dx_push"])
            seen_dx_files.append([Path(path).relative_to(Path(path).parents[1]) for path in files])
            return {"status": "sent", "expected_images": len(files), "image_success": len(files)}

        with (
            mock.patch.object(material_skill, "_process_single_variant", side_effect=fake_process),
            mock.patch.object(material_skill, "publish_delivery_material", side_effect=fake_publish),
            mock.patch.object(material_skill, "_send_dx_push_notification", side_effect=fake_dx_push),
            mock.patch.object(material_skill, "delivery_run_id", return_value="run_001"),
        ):
            rc = material_skill.run(self._panorama_args(
                request_path,
                out_dir,
                variants=2,
                dry_run=False,
                image_provider="api",
            ))

        self.assertEqual(rc, 0)
        self.assertEqual(seen_options[0]["max_attempts"], 2)
        self.assertEqual(seen_options[0]["retryable_codes"], {408, 429, 502})
        self.assertTrue(seen_options[0]["verify_qr_scan"])
        self.assertEqual(seen_dx_status[0]["status"], "pending")
        self.assertEqual(seen_dx_files, [[Path("run_001/option_01.png")]])
        status = json.loads((out_dir / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "done")
        self.assertEqual(len(status["files"]), 6)
        self.assertEqual(status["dx_push"]["status"], "sent")
        self.assertEqual(status["dx_push"]["expected_images"], 1)

    def test_run_with_style_reference_ignores_pre_selected_styles(self):
        style_ref = self.out_dir / "style_ref.png"
        material_skill.write_mock_png(style_ref, 96, 96, "style-ref")
        request_path = self.out_dir / "request.json"
        request_path.write_text(
            json.dumps(
                base_request(
                    assets={
                        "qr_code_not_needed": True,
                        "food_image_not_needed": True,
                        "style_reference_images": [str(style_ref)],
                        "style_reference_note": "完全对标参考图的配色、排版、字体和装饰语言",
                    },
                ),
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        out_dir = self.out_dir / "style_ref_run"
        args = Namespace(
            request=str(request_path),
            out=str(out_dir),
            variants=2,
            dpi=150,
            dry_run=True,
            image_provider="none",
            template_selector_provider="api",
            template_selector_model="gpt-5.5",
            template_selector_timeout=30,
            pre_selected_styles="clean_hero,natural_earth",
        )

        with mock.patch.object(material_skill, "call_template_selector_api") as selector_api:
            selector_api.side_effect = AssertionError("style reference must not call selector API")
            rc = material_skill.run(args)

        self.assertEqual(rc, 0)
        audit = json.loads((out_dir / "template_selection.json").read_text(encoding="utf-8"))
        self.assertEqual(audit["provider"], "style_reference")
        self.assertEqual(
            [item["style_id"] for item in audit["final_styles"]],
            ["style_reference_dominant"],
        )
        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["variants"]), 1)
        self.assertEqual(manifest["variants"][0]["style_id"], "style_reference_dominant")

    def test_parallel_all_variant_failures_raise_after_manifest_written(self):
        request_path = self.out_dir / "request.json"
        request_path.write_text(json.dumps(base_request(), ensure_ascii=False), encoding="utf-8")
        out_dir = self.out_dir / "all_failed"
        args = Namespace(
            request=str(request_path),
            out=str(out_dir),
            variants=2,
            dpi=150,
            dry_run=False,
            image_provider="api",
            template_selector_provider="none",
            template_selector_model="gpt-5.5",
            template_selector_timeout=30,
        )

        with mock.patch.object(material_skill, "_process_single_variant", side_effect=material_skill.SkillError("all boom")):
            with self.assertRaises(material_skill.SkillError) as context:
                material_skill.run(args)

        self.assertIn("均生成失败", str(context.exception))
        manifest = json.loads((out_dir / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(len(manifest["variants"]), 0)
        self.assertEqual(len(manifest["failed_variants"]), 2)

    def test_skill_doc_distinguishes_delivery_files_from_dx_push_files(self):
        skill_doc = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("status.json.files 是用户交付清单", skill_doc)
        self.assertIn("dx-push 是附属推送状态", skill_doc)
        self.assertIn("五连图自动 dx-push 只推送完整长图", skill_doc)
        self.assertIn("1500 秒", skill_doc)

    def test_skill_doc_marks_dx_push_as_builtin_code_logic(self):
        skill_doc = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")

        # dx-push 已固化为代码逻辑，Agent 无需手动执行
        self.assertIn("已内置为代码逻辑", skill_doc)
        self.assertIn("最终回复前门禁", skill_doc)
        self.assertIn("静默执行", skill_doc)

    def test_dx_user_prompt_summary_priority_and_formatting(self):
        # 主路径：request.json 的 user_original_prompt（字符串）
        self.assertEqual(
            material_skill._dx_user_prompt_summary({"user_original_prompt": "生成一个韩式炸鸡店的世界杯营销海报"}),
            "1. 生成一个韩式炸鸡店的世界杯营销海报",
        )
        # 列表形式：第 1 条原始输入 + 各轮澄清回答，逐条编号
        self.assertEqual(
            material_skill._dx_user_prompt_summary(
                {"user_original_prompt": ["生成韩式炸鸡海报", "改成竖版", "加第二杯半价"]}
            ),
            "1. 生成韩式炸鸡海报\n2. 改成竖版\n3. 加第二杯半价",
        )
        # 已带序号的字符串不重复加序号
        self.assertEqual(
            material_skill._dx_user_prompt_summary({"user_original_prompt": "1. 已编号"}),
            "1. 已编号",
        )
        # 多行字符串（Agent 预拼好）保留换行，不被折叠成一行
        self.assertEqual(
            material_skill._dx_user_prompt_summary(
                {"user_original_prompt": "1. 生成海报\n2. 改竖版"}
            ),
            "1. 生成海报\n2. 改竖版",
        )
        # 未编号多行字符串也逐行编号，避免第二行以后在通知里变成散落文本
        self.assertEqual(
            material_skill._dx_user_prompt_summary(
                {"user_original_prompt": "生成海报\n改竖版"}
            ),
            "1. 生成海报\n2. 改竖版",
        )
        # user_original_prompt 优先级高于环境变量 override
        with mock.patch.dict(os.environ, {"RESTAURANT_DX_USER_PROMPTS": "ENV覆盖值"}):
            self.assertEqual(
                material_skill._dx_user_prompt_summary({"user_original_prompt": "原始输入", "title": "X"}),
                "1. 原始输入",
            )
            # 无 user_original_prompt 时回退到环境变量
            self.assertEqual(
                material_skill._dx_user_prompt_summary({"title": "X"}),
                "ENV覆盖值",
            )
        # 两者都没有时兜底拼接结构化字段
        self.assertEqual(
            material_skill._dx_user_prompt_summary({"title": "看球必备", "store": {"category": "韩式炸鸡"}}),
            "1. 看球必备；韩式炸鸡",
        )

    def test_dx_caller_mis_priority_and_numeric_uid_preserved(self):
        for k in ("RESTAURANT_DX_CALLER_MIS", "SANDBOX_MIS", "CATPAW_CONFIG_CONTENT"):
            os.environ.pop(k, None)
        # 主路径：request.json 的 caller_mis
        self.assertEqual(material_skill._dx_caller_mis({"caller_mis": "wanghao55"}), "wanghao55")
        # 缺失时为 unknown
        self.assertEqual(material_skill._dx_caller_mis({}), "unknown")
        # 平台配置中的 misId 可作为兜底来源
        with mock.patch.dict(os.environ, {"CATPAW_CONFIG_CONTENT": json.dumps({"misId": "wangwu09"})}):
            self.assertEqual(material_skill._dx_caller_mis({}), "wangwu09")
        # 纯数字 uid 不丢弃，保留为 uid:xxx
        with mock.patch.dict(os.environ, {"SANDBOX_MIS": "1303903"}):
            self.assertEqual(material_skill._dx_caller_mis({}), "uid:1303903")
            # 环境变量 RESTAURANT_DX_CALLER_MIS 优先于平台注入
            with mock.patch.dict(os.environ, {"RESTAURANT_DX_CALLER_MIS": "zhangsan06"}):
                self.assertEqual(material_skill._dx_caller_mis({}), "zhangsan06")
                # request.json 的 caller_mis 优先级最高
                self.assertEqual(material_skill._dx_caller_mis({"caller_mis": "lisi08"}), "lisi08")

    def test_unit_test_harness_disables_real_dx_push(self):
        self.assertEqual(os.environ.get("RESTAURANT_DX_PUSH_DISABLED"), "1")

    def test_async_worker_delegates_dx_push_to_run_and_keeps_status(self):
        # dx-push 已内置到 run() 内部；worker 成功路径不再自己发通知，
        # 只要 main() 返回 0 就保留 run() 写好的 status.json（含 dx_push 结果）。
        request_path = self.out_dir / "request.json"
        request_path.write_text(json.dumps(base_request(), ensure_ascii=False), encoding="utf-8")
        normalized_path = self.out_dir / "request.normalized.json"
        normalized_path.write_text(json.dumps(base_request(), ensure_ascii=False), encoding="utf-8")
        run_dir = self.out_dir / "deliverables" / "run_001"
        run_dir.mkdir(parents=True)
        first = run_dir / "option_01.png"
        second = run_dir / "option_02.png"
        material_skill.write_mock_png(first, 32, 32, "clean_hero")
        material_skill.write_mock_png(second, 32, 32, "street_warm")

        argv = [
            "--request", str(request_path),
            "--out", str(self.out_dir),
            "--variants", "2",
            "--image-provider", "api",
        ]

        def fake_main(sync_argv):
            # 模拟 run() 在成功路径写好的 status.json
            material_skill._write_status(
                self.out_dir, "done",
                files=[str(first), str(second)], exit_code=0,
                dx_push={"status": "sent", "expected_images": 2},
            )
            return 0

        with (
            mock.patch.object(material_skill, "main", side_effect=fake_main),
            mock.patch.object(material_skill, "_send_dx_push_notification", create=True) as dx_push,
        ):
            material_skill._async_worker(argv, self.out_dir)

        # worker 成功路径不再自己调用 dx-push（已由 run() 负责）
        dx_push.assert_not_called()
        status = json.loads((self.out_dir / "status.json").read_text(encoding="utf-8"))
        self.assertEqual(status["status"], "done")
        self.assertEqual(status["files"], [str(first), str(second)])
        self.assertEqual(status["dx_push"]["status"], "sent")
        self.assertEqual(status["dx_push"]["expected_images"], 2)


if __name__ == "__main__":
    unittest.main()
