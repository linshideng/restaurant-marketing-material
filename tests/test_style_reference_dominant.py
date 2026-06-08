import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import material_skill


def panorama_request(style_reference_images=None, style_reference_note=""):
    req, _ = material_skill.normalize_request(
        {
            "type": "五连图",
            "title": "川韵家常菜",
            "store": {"name": "测试门店", "category": "川菜"},
            "campaign": {"theme": "川韵家常菜"},
            "copy": {"selected_text": "川韵家常菜", "dimensions": ["餐品特色"]},
            "products": [{"name": "回锅肉", "reference_image_index": 1}],
            "style": {"name": "水墨东方版", "cuisine_tag": "川菜"},
            "assets": {
                "reference_images": ["/tmp/test_food_01.png"],
                "style_reference_images": style_reference_images or [],
                "style_reference_note": style_reference_note,
            },
        }
    )
    return req


def poster_request(style_reference_images=None, style_reference_note=""):
    req, _ = material_skill.normalize_request(
        {
            "type": "营销海报",
            "size": {"preset": "3:4"},
            "title": "春日尝鲜",
            "store": {"name": "测试门店", "category": "川菜"},
            "campaign": {"theme": "春日尝鲜", "offer": "到店有礼", "cta": "立即扫码"},
            "copy": {"selected_text": "春日尝鲜 到店有礼", "dimensions": ["餐品特色"]},
            "products": [{"name": "回锅肉", "reference_image_index": 1}],
            "style": {"name": "水墨东方版", "cuisine_tag": "川菜"},
            "assets": {
                "qr_code_not_needed": True,
                "reference_images": ["/tmp/test_food_01.png"],
                "style_reference_images": style_reference_images or [],
                "style_reference_note": style_reference_note,
            },
        }
    )
    return req


def template(style_id="ink_oriental"):
    return json.loads((Path(__file__).resolve().parents[1] / "templates" / "styles" / f"{style_id}.json").read_text(encoding="utf-8"))


class StyleReferenceDominantTests(unittest.TestCase):
    def test_panorama_without_style_reference_preserves_template_family(self):
        prompt = json.loads(
            material_skill.build_scene_prompt(
                panorama_request(),
                template("ink_oriental"),
                {"id": "panorama_default", "name": "五连图默认布局"},
                3840,
                1280,
            )
        )

        self.assertNotIn("style_reference_guidance", prompt)
        self.assertIn("山水意境", prompt["panorama_style_family"]["family"])
        self.assertIn("宣纸纤维", prompt["panorama_style_family"]["background"])

    def test_panorama_with_style_reference_suppresses_template_family(self):
        prompt = json.loads(
            material_skill.build_scene_prompt(
                panorama_request(
                    style_reference_images=["/tmp/style_ref_01.png"],
                    style_reference_note="深墨绿色纯色背景，金色花卉暗纹，中式克制风格",
                ),
                template("ink_oriental"),
                {"id": "panorama_default", "name": "五连图默认布局"},
                3840,
                1280,
            )
        )

        self.assertEqual(next(iter(prompt)), "style_reference_guidance")
        self.assertNotIn("山水意境", prompt["panorama_style_family"]["family"])
        self.assertNotIn("宣纸纤维", prompt["panorama_style_family"]["background"])
        self.assertIn("深墨绿色", prompt["style_reference_guidance"]["user_visual_requirements"])

    def test_panorama_style_reference_note_is_wrapped_as_constraints_not_plain_background(self):
        note = "红黑强对比背景，大标题居中压住画面核心，菜品围绕标题形成冲击式层叠"
        prompt = json.loads(
            material_skill.build_scene_prompt(
                panorama_request(
                    style_reference_images=["/tmp/style_ref_01.png"],
                    style_reference_note=note,
                ),
                template("ink_oriental"),
                {"id": "panorama_default", "name": "五连图默认布局"},
                3840,
                1280,
            )
        )

        guidance = prompt["style_reference_guidance"]
        guidance_text = json.dumps(guidance, ensure_ascii=False)
        self.assertIn("MUST", guidance_text)
        self.assertIn("MUST_NOT", guidance_text)
        self.assertIn("conflict_priority", guidance)
        self.assertIn("模板风格", guidance_text)
        self.assertIn(note, guidance_text)
        self.assertNotEqual(prompt["panorama_style_family"]["background"], note)
        self.assertNotEqual(prompt["panorama_style_family"]["composition"], note)
        self.assertNotEqual(prompt["style"]["design_guidance"], note)
        self.assertNotEqual(prompt["panorama_background_design"]["style_specific_direction"], note)
        self.assertIn("MUST", prompt["style"]["design_guidance"])
        self.assertIn("MUST", prompt["panorama_style_family"]["background"])

    def test_short_style_reference_note_adds_structured_observation_fallback(self):
        prompt = json.loads(
            material_skill.build_scene_prompt(
                panorama_request(
                    style_reference_images=["/tmp/style_ref_01.png"],
                    style_reference_note="参考这张",
                ),
                template("ink_oriental"),
                {"id": "panorama_default", "name": "五连图默认布局"},
                3840,
                1280,
            )
        )

        guidance_text = json.dumps(prompt["style_reference_guidance"], ensure_ascii=False)
        self.assertIn("先从参考图识别设计要素再生成", guidance_text)
        self.assertIn("色调", guidance_text)
        self.assertIn("构图比例", guidance_text)
        self.assertIn("文字风格", guidance_text)
        self.assertIn("菜品/主体呈现方式", guidance_text)
        self.assertIn("背景元素", guidance_text)
        self.assertIn("分隔/留白/节奏方式", guidance_text)
        self.assertIn("无明显特征", guidance_text)

    def test_regular_length_style_reference_note_is_preserved_without_short_note_fallback(self):
        note = (
            "色调：深墨绿色底色搭配金色细线；构图比例：左侧大标题占三成，右侧菜品占六成；"
            "文字风格：粗宋体标题配小号副标题；菜品/主体呈现方式：主体菜品右侧层叠；"
            "背景元素：暗纹花卉；分隔/留白/节奏方式：左密右疏，竖向分隔。"
        )
        prompt = json.loads(
            material_skill.build_scene_prompt(
                panorama_request(
                    style_reference_images=["/tmp/style_ref_01.png"],
                    style_reference_note=note,
                ),
                template("ink_oriental"),
                {"id": "panorama_default", "name": "五连图默认布局"},
                3840,
                1280,
            )
        )

        guidance = prompt["style_reference_guidance"]
        self.assertEqual(guidance["user_visual_requirements"], note)
        guidance_text = json.dumps(guidance, ensure_ascii=False)
        self.assertNotIn("note 过短", guidance_text)

    def test_low_resolution_style_reference_with_short_note_adds_compensation_guidance(self):
        with tempfile.TemporaryDirectory() as tmp:
            low_ref = Path(tmp) / "low_style_ref.png"
            material_skill.write_mock_png(low_ref, 1324, 200, "style-ref")
            prompt = json.loads(
                material_skill.build_scene_prompt(
                    panorama_request(
                        style_reference_images=[str(low_ref)],
                        style_reference_note="参考这张",
                    ),
                    template("ink_oriental"),
                    {"id": "panorama_default", "name": "五连图默认布局"},
                    3840,
                    1280,
                )
            )

        guidance_text = json.dumps(prompt["style_reference_guidance"], ensure_ascii=False)
        self.assertIn("低分辨率风格参考图", guidance_text)
        self.assertIn("短边 <400px", guidance_text)
        self.assertIn("至少 200 个中文字符", guidance_text)

    def test_style_reference_guidance_imitates_visuals_but_not_business_facts(self):
        prompt = json.loads(
            material_skill.build_scene_prompt(
                panorama_request(
                    style_reference_images=["/tmp/style_ref_01.png"],
                    style_reference_note="参考图是奶油黄底、左侧粗体大字、右侧菜品层叠、圆角色块分隔，店名写着老王饭铺，价格19.9元",
                ),
                template("ink_oriental"),
                {"id": "panorama_default", "name": "五连图默认布局"},
                3840,
                1280,
            )
        )

        guidance_text = json.dumps(prompt["style_reference_guidance"], ensure_ascii=False)
        style_text = json.dumps(prompt["style"], ensure_ascii=False)

        self.assertIn("non-factual visual", guidance_text)
        self.assertIn("palette, composition, layout, typography", guidance_text)
        self.assertIn("dish names, prices, store names, campaign rules", guidance_text)
        self.assertIn("菜名、价格、店名、活动规则", guidance_text)
        self.assertIn("尽量复现", style_text)

    def test_regular_poster_with_style_reference_suppresses_template_design_style(self):
        prompt = json.loads(
            material_skill.build_scene_prompt(
                poster_request(
                    style_reference_images=["/tmp/style_ref_01.png"],
                    style_reference_note="参考这张的配色和排版风格",
                ),
                template("ink_oriental"),
                {"id": "layout", "name": "Layout"},
                1242,
                1660,
            )
        )

        self.assertEqual(next(iter(prompt)), "style_reference_guidance")
        design_style = prompt["prompt_sections"]["DESIGN_STYLE"]
        self.assertEqual(design_style["style_id"], "style_reference_dominant")
        self.assertNotIn("宣纸纤维", design_style["texture_guidance"])
        self.assertIn("风格参考图", prompt["prompt_sections"]["BASE_STYLE"])


if __name__ == "__main__":
    unittest.main()
