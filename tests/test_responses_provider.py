import base64
import json
import os
import sys
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

import material_skill
import qr_enhance

try:
    from PIL import Image, ImageChops, ImageDraw
except ImportError:  # pragma: no cover - optional local dependency
    Image = None
    ImageChops = None
    ImageDraw = None


PNG_BYTES = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAIAAACQd1PeAAAADUlEQVR42mP8z8BQDwAFgwJ/lw9W7wAAAABJRU5ErkJggg=="
)
PNG_B64 = base64.b64encode(PNG_BYTES).decode("ascii")


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


class ResponsesProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out_path = Path(self.tmp.name) / "out.png"
        self.raw_path = Path(self.tmp.name) / "raw.txt"
        self.proxy_base = mock.patch.object(material_skill, "PROXY_BASE_URL", "http://proxy.example/v1")
        self.proxy_token = mock.patch.object(material_skill, "PROXY_TOKEN", "proxy-test-token")
        self.proxy_base.start()
        self.proxy_token.start()
        self.env = mock.patch.dict(
            os.environ,
            {
                "RESTAURANT_MATERIAL_API_KEY": "test-key",
                "RESTAURANT_MATERIAL_BASE_URL": "http://new.example/v1",
                "RESTAURANT_MATERIAL_RESPONSES_MODEL": "gpt-5.5",
            },
            clear=True,
        )
        self.env.start()

    def tearDown(self):
        self.env.stop()
        self.proxy_token.stop()
        self.proxy_base.stop()
        self.tmp.cleanup()

    def test_http_image_api_uses_responses_image_generation_only(self):
        requests = []

        def fake_urlopen(request, timeout):
            requests.append(request)
            return FakeHTTPResponse(
                {
                    "output": [
                        {"type": "message", "content": [{"type": "output_text", "text": "placement text"}]},
                        {"type": "image_generation_call", "result": PNG_B64},
                    ]
                }
            )

        with (
            mock.patch.object(material_skill.urllib.request, "urlopen", side_effect=fake_urlopen),
            mock.patch.object(material_skill.time, "sleep", return_value=None),
        ):
            model_text = material_skill.call_image_api(
                "make poster",
                [],
                self.out_path,
                self.raw_path,
            )

        self.assertEqual(model_text, "placement text")
        self.assertEqual(self.out_path.read_bytes(), PNG_BYTES)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].full_url, material_skill.PROXY_BASE_URL + "/responses")
        self.assertEqual(requests[0].headers["Authorization"], f"Bearer {material_skill.PROXY_TOKEN}")
        payload = captured_request_payload(requests[0])
        self.assertEqual(payload["model"], "gpt-5.5")
        self.assertEqual(payload["reasoning"], {"effort": "high"})
        self.assertEqual(payload["input"][0]["content"][0]["type"], "input_text")
        self.assertEqual(payload["input"][0]["content"][0]["text"], "make poster")
        self.assertEqual(
            payload["tools"],
            [{"type": "image_generation", "action": "generate", "quality": "high", "size": "1024x1024"}],
        )

    def test_http_image_api_inserts_role_text_before_each_reference_image(self):
        ref_1 = Path(self.tmp.name) / "style_ref.png"
        ref_2 = Path(self.tmp.name) / "dish_ref.png"
        ref_1.write_bytes(PNG_BYTES)
        ref_2.write_bytes(PNG_BYTES)
        requests = []

        def fake_urlopen(request, timeout):
            requests.append(request)
            return FakeHTTPResponse({"output": [{"type": "image_generation_call", "result": PNG_B64}]})

        with mock.patch.object(material_skill.urllib.request, "urlopen", side_effect=fake_urlopen):
            material_skill.call_image_api(
                "make poster",
                [str(ref_1), str(ref_2)],
                self.out_path,
                self.raw_path,
                reference_image_roles=[
                    "STYLE_REFERENCE_HIGHEST_PRIORITY: controls visual style; MUST override templates; MUST NOT be used as food.",
                    "DISH_REFERENCE: preserves food subject; MUST NOT control global visual style.",
                ],
            )

        content = captured_request_payload(requests[0])["input"][0]["content"]
        self.assertEqual([item["type"] for item in content], ["input_text", "input_text", "input_image", "input_text", "input_image"])
        self.assertEqual(content[0]["text"], "make poster")
        self.assertIn("IMAGE_1 ROLE", content[1]["text"])
        self.assertIn("STYLE_REFERENCE_HIGHEST_PRIORITY", content[1]["text"])
        self.assertIn("MUST override templates", content[1]["text"])
        self.assertIn("IMAGE_2 ROLE", content[3]["text"])
        self.assertIn("DISH_REFERENCE", content[3]["text"])
        self.assertIn("MUST NOT control global visual style", content[3]["text"])

    def test_http_image_api_uses_generic_role_text_when_roles_are_missing(self):
        ref = Path(self.tmp.name) / "ref.png"
        ref.write_bytes(PNG_BYTES)
        requests = []

        def fake_urlopen(request, timeout):
            requests.append(request)
            return FakeHTTPResponse({"output": [{"type": "image_generation_call", "result": PNG_B64}]})

        with mock.patch.object(material_skill.urllib.request, "urlopen", side_effect=fake_urlopen):
            material_skill.call_image_api("make poster", [str(ref)], self.out_path, self.raw_path)

        content = captured_request_payload(requests[0])["input"][0]["content"]
        self.assertEqual([item["type"] for item in content], ["input_text", "input_text", "input_image"])
        self.assertIn("IMAGE_1 ROLE", content[1]["text"])
        self.assertIn("reference image", content[1]["text"])

    def test_runtime_env_does_not_change_proxy_configuration(self):
        self.env.stop()
        self.env = mock.patch.dict(
            os.environ,
            {
                "RESTAURANT_MATERIAL_API_KEY": "env-key-should-not-be-used",
                "RESTAURANT_MATERIAL_BASE_URL": "https://aihub" + "mix.com/v1",
                "RESTAURANT_MATERIAL_RESPONSES_MODEL": "wrong-model",
                "RESTAURANT_SKILL_PROXY_BASE_URL": "https://wrong-proxy.example/v1",
                "RESTAURANT_SKILL_PROXY_TOKEN": "wrong-token",
                "OPENAI_API_KEY": "openai-env-key-should-not-be-used",
            },
            clear=True,
        )
        self.env.start()
        requests = []

        def fake_urlopen(request, timeout):
            requests.append(request)
            return FakeHTTPResponse({"output": [{"type": "image_generation_call", "result": PNG_B64}]})

        with mock.patch.object(material_skill.urllib.request, "urlopen", side_effect=fake_urlopen):
            material_skill.call_image_api("make poster", [], self.out_path, self.raw_path)

        self.assertEqual(requests[0].full_url, material_skill.PROXY_BASE_URL + "/responses")
        self.assertEqual(requests[0].headers["Authorization"], f"Bearer {material_skill.PROXY_TOKEN}")
        self.assertEqual(captured_request_payload(requests[0])["model"], material_skill.RESPONSES_API_MODEL)

    def test_responses_api_configuration_mismatch_raises_without_http_call(self):
        with (
            mock.patch.object(material_skill, "RESPONSES_API_MODEL", "wrong-model"),
            mock.patch.object(material_skill.urllib.request, "urlopen") as urlopen,
        ):
            with self.assertRaises(material_skill.SkillError) as context:
                material_skill.call_image_api("make poster", [], self.out_path, self.raw_path)

        urlopen.assert_not_called()
        self.assertIn("只允许调用 Responses API", str(context.exception))

    def test_placeholder_proxy_configuration_raises_before_http_call(self):
        with (
            mock.patch.object(material_skill, "PROXY_BASE_URL", "http://restaurant-skill-proxy.placeholder.sankuai.com/v1"),
            mock.patch.object(material_skill, "PROXY_TOKEN", "PLACEHOLDER-DEPLOY-THEN-REPLACE"),
            mock.patch.object(material_skill.urllib.request, "urlopen") as urlopen,
        ):
            with self.assertRaises(material_skill.SkillError) as context:
                material_skill.call_image_api("make poster", [], self.out_path, self.raw_path)

        urlopen.assert_not_called()
        self.assertIn("代理服务尚未配置", str(context.exception))

    def test_retryable_http_error_retries_before_success(self):
        requests = []

        def fake_urlopen(request, timeout):
            requests.append(request)
            if len(requests) == 1:
                raise urllib.error.HTTPError(
                    request.full_url,
                    503,
                    "Unavailable",
                    hdrs=None,
                    fp=FakeHTTPResponse({"error": {"message": "temporary upstream failure"}}),
                )
            return FakeHTTPResponse({"output": [{"type": "image_generation_call", "result": PNG_B64}]})

        with (
            mock.patch.object(material_skill.urllib.request, "urlopen", side_effect=fake_urlopen),
            mock.patch.object(material_skill.time, "sleep", return_value=None) as sleep,
        ):
            material_skill.call_image_api("make poster", [], self.out_path, self.raw_path)

        self.assertEqual(len(requests), 2)
        sleep.assert_called_once()
        wait = sleep.call_args.args[0]
        self.assertGreaterEqual(wait, 2)
        self.assertLessEqual(wait, 3.5)
        self.assertEqual(self.out_path.read_bytes(), PNG_BYTES)

    def test_auto_provider_uses_responses_api_directly(self):
        call_order = []

        def fake_api(prompt, reference_images, out_path, raw_response_path, **kwargs):
            call_order.append("responses")
            out_path.write_bytes(PNG_BYTES)
            return "api text"

        with (
            mock.patch.object(material_skill, "call_image_api", side_effect=fake_api),
            mock.patch.object(material_skill.subprocess, "run", side_effect=AssertionError("auto provider must not run local image generation")),
        ):
            warnings, model_text = material_skill.generate_scene_image(
                prompt="make poster",
                reference_images=[],
                out_path=self.out_path,
                raw_response_path=self.raw_path,
                provider="auto",
                dry_run=False,
                width=10,
                height=10,
                variant="big_type_impact",
            )

        self.assertEqual(call_order, ["responses"])
        self.assertEqual(model_text, "api text")
        self.assertTrue(any("Responses API" in warning for warning in warnings))
        self.assertFalse(any("local" in warning.lower() for warning in warnings))

    def test_copy_runtime_assets_only_requires_existing_meituan_fonts(self):
        raw_req = {
            "type": "营销海报",
            "size": {"preset": "3:4"},
            "title": "春日尝鲜",
            "store": {"category": "火锅"},
            "campaign": {"offer": "扫码下单 更优惠"},
            "products": [],
            "assets": {"qr_code_not_needed": True, "food_image_not_needed": True},
        }
        req, _ = material_skill.normalize_request(raw_req)

        req, runtime = material_skill.copy_runtime_assets(req, Path(self.tmp.name))

        self.assertEqual(set(runtime.fonts), {"regular", "bold"})
        for font_path in runtime.fonts.values():
            self.assertTrue(font_path.exists())
        self.assertTrue(runtime.selected_logo_path and runtime.selected_logo_path.exists())

    def test_group_buying_context_selects_meituan_group_buying_logo(self):
        raw_req = {
            "type": "营销海报",
            "size": {"preset": "3:4"},
            "title": "双人团购套餐",
            "store": {"category": "火锅"},
            "campaign": {"offer": "双人套餐 68 元", "cta": "到店享优惠"},
            "products": [],
            "assets": {"qr_code_not_needed": True, "food_image_not_needed": True},
        }
        req, _ = material_skill.normalize_request(raw_req)

        req, runtime = material_skill.copy_runtime_assets(req, Path(self.tmp.name) / "group")

        self.assertEqual(runtime.selected_logo_path.name, "meituan-group-buying-logo.png")
        self.assertEqual(runtime.selected_white_logo_path.name, "meituan-group-white-buying-logo.png")

    def test_dianping_platform_logo_is_copied_and_selected(self):
        raw_req = {
            "type": "营销海报",
            "size": {"preset": "3:4"},
            "title": "大众点评必吃榜同款",
            "store": {"category": "川湘菜"},
            "campaign": {"offer": "到店尝鲜"},
            "products": [],
            "assets": {
                "qr_code_not_needed": True,
                "food_image_not_needed": True,
                "platform_logo": "dianping",
            },
        }
        req, _ = material_skill.normalize_request(raw_req)

        req, runtime = material_skill.copy_runtime_assets(req, Path(self.tmp.name) / "dianping")

        self.assertEqual(req["assets"]["platform_logo"], "dianping")
        self.assertEqual(runtime.selected_logo_path.name, "dianping-logo.png")
        self.assertEqual(runtime.selected_white_logo_path.name, "dianping-white-logo.png")
        self.assertTrue(runtime.selected_logo_path.exists())
        self.assertTrue(runtime.selected_white_logo_path.exists())

    def test_platform_logo_none_skips_runtime_overlay_assets(self):
        raw_req = {
            "type": "营销海报",
            "size": {"preset": "3:4"},
            "title": "春日尝鲜",
            "store": {"category": "火锅"},
            "campaign": {"offer": "到店享优惠"},
            "products": [],
            "assets": {
                "qr_code_not_needed": True,
                "food_image_not_needed": True,
                "platform_logo": "none",
            },
        }
        req, _ = material_skill.normalize_request(raw_req)

        req, runtime = material_skill.copy_runtime_assets(req, Path(self.tmp.name) / "no_logo")

        self.assertEqual(req["assets"]["platform_logo"], "none")
        self.assertFalse(req["assets"]["use_meituan_logo"])
        self.assertIsNone(runtime.selected_logo_path)
        self.assertIsNone(runtime.selected_white_logo_path)

    def test_mascot_auto_skips_reference_image_by_default_but_explicit_reference_uploads_it(self):
        raw_req = {
            "type": "营销海报",
            "size": {"preset": "3:4"},
            "title": "春日尝鲜",
            "store": {"category": "火锅"},
            "campaign": {"offer": "到店享优惠"},
            "products": [],
            "assets": {"qr_code_not_needed": True, "food_image_not_needed": True, "mascot_mode": "auto"},
        }
        req, _ = material_skill.normalize_request(raw_req)

        _req, runtime = material_skill.copy_runtime_assets(req, Path(self.tmp.name) / "auto")

        self.assertEqual(runtime.generation_reference_images, [])
        self.assertTrue(any("mascot_mode=auto" in warning for warning in runtime.warnings))

        explicit_req = json.loads(json.dumps(raw_req, ensure_ascii=False))
        explicit_req["assets"]["mascot_mode"] = "official_reference"
        req, _ = material_skill.normalize_request(explicit_req)

        _req, runtime = material_skill.copy_runtime_assets(req, Path(self.tmp.name) / "explicit")

        self.assertEqual(len(runtime.generation_reference_images), 1)
        self.assertEqual(len(runtime.generation_reference_image_roles), 1)
        self.assertIn("MASCOT_REFERENCE", runtime.generation_reference_image_roles[0])

    def test_panorama_runtime_assets_pass_real_shots_and_logo_as_model_references(self):
        ref_1 = Path(self.tmp.name) / "ref_1.png"
        ref_2 = Path(self.tmp.name) / "ref_2.png"
        logo = Path(self.tmp.name) / "brand_logo.png"
        material_skill.write_mock_png(ref_1, 8, 8, "clean_hero")
        material_skill.write_mock_png(ref_2, 8, 8, "street_warm")
        material_skill.write_mock_png(logo, 8, 8, "natural_earth")
        raw_req = {
            "type": "五连图",
            "title": "田园绿意一口尝鲜",
            "store": {"category": "融合菜"},
            "assets": {
                "reference_images": [str(ref_1), str(ref_2)],
                "brand_logo_path": str(logo),
            },
        }
        req, _ = material_skill.normalize_request(raw_req)

        req, runtime = material_skill.copy_runtime_assets(req, Path(self.tmp.name) / "panorama")

        self.assertIsNone(runtime.selected_logo_path)
        self.assertIsNone(runtime.selected_white_logo_path)
        self.assertEqual(len(runtime.food_images), 2)
        self.assertEqual(len(runtime.generation_reference_images), 3)
        self.assertEqual(len(runtime.generation_reference_image_roles), 3)
        self.assertIn("DISH_REFERENCE", runtime.generation_reference_image_roles[0])
        self.assertIn("AMBIENT_REFERENCE", runtime.generation_reference_image_roles[1])
        self.assertIn("LOGO_REFERENCE", runtime.generation_reference_image_roles[2])
        self.assertTrue(req["assets"]["brand_logo_path"].endswith("brand_logo.png"))

    def test_runtime_assets_roles_track_style_reference_and_dedupe_order(self):
        style_ref = Path(self.tmp.name) / "style_ref.png"
        duplicate_dish = Path(self.tmp.name) / "duplicate_dish.png"
        material_skill.write_mock_png(style_ref, 8, 8, "same")
        material_skill.write_mock_png(duplicate_dish, 8, 8, "same")
        raw_req = {
            "type": "营销海报",
            "size": {"preset": "3:4"},
            "title": "春日尝鲜",
            "store": {"category": "融合菜"},
            "assets": {
                "qr_code_not_needed": True,
                "style_reference_images": [str(style_ref)],
                "style_reference_note": "参考图是高对比红黑背景和大标题排版",
                "reference_images": [str(duplicate_dish)],
            },
        }
        req, _ = material_skill.normalize_request(raw_req)

        _req, runtime = material_skill.copy_runtime_assets(req, Path(self.tmp.name) / "role_dedupe")

        self.assertEqual(len(runtime.generation_reference_images), 1)
        self.assertEqual(len(runtime.generation_reference_image_roles), 1)
        self.assertIn("STYLE_REFERENCE_HIGHEST_PRIORITY", runtime.generation_reference_image_roles[0])
        self.assertNotIn("DISH_REFERENCE", runtime.generation_reference_image_roles[0])
        self.assertTrue(any("参考图去重" in warning for warning in runtime.warnings))

    def test_regular_variant_passes_runtime_reference_roles_to_generator(self):
        style_ref = Path(self.tmp.name) / "style_ref_regular.png"
        dish_ref = Path(self.tmp.name) / "dish_ref_regular.png"
        Image.new("RGB", (12, 12), (210, 20, 30)).save(style_ref)
        Image.new("RGB", (12, 12), (30, 140, 90)).save(dish_ref)
        raw_req = {
            "type": "营销海报",
            "size": {"preset": "3:4"},
            "title": "春日尝鲜",
            "store": {"name": "青禾小馆", "category": "融合菜"},
            "assets": {
                "qr_code_not_needed": True,
                "platform_logo": "none",
                "style_reference_images": [str(style_ref)],
                "style_reference_note": "参考图是红黑高对比背景和居中大标题",
                "reference_images": [str(dish_ref)],
            },
        }
        req, _ = material_skill.normalize_request(raw_req)
        req, runtime = material_skill.copy_runtime_assets(req, Path(self.tmp.name) / "regular_roles")
        template = material_skill.load_templates("营销海报")[0]
        captured_roles = []

        def fake_generate(prompt, reference_images, out_path, raw_response_path, provider,
                          dry_run, width, height, variant, **kwargs):
            captured_roles.append(list(kwargs.get("reference_image_roles") or []))
            material_skill.write_mock_png(out_path, width, height, "regular-roles")
            return [], ""

        with mock.patch.object(material_skill, "generate_scene_image", side_effect=fake_generate):
            material_skill._process_single_variant(
                index=1,
                template=template,
                req=req,
                runtime=runtime,
                width=300,
                height=400,
                out_dir=Path(self.tmp.name) / "regular_roles",
                provider="api",
                dry_run=False,
                qr_shared={},
                verify_qr_scan=False,
            )

        self.assertEqual(captured_roles, [runtime.generation_reference_image_roles])
        self.assertIn("STYLE_REFERENCE_HIGHEST_PRIORITY", captured_roles[0][0])
        self.assertIn("DISH_REFERENCE", captured_roles[0][1])

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_large_reference_images_are_compressed_and_deduped_after_preprocessing(self):
        ref_1 = Path(self.tmp.name) / "large_ref_1.png"
        ref_2 = Path(self.tmp.name) / "large_ref_2.png"
        Image.new("RGB", (2000, 2600), (180, 30, 20)).save(ref_1)
        Image.new("RGB", (2000, 2600), (180, 30, 20)).save(ref_2)
        raw_req = {
            "type": "营销海报",
            "size": {"preset": "3:4"},
            "title": "春日尝鲜",
            "store": {"category": "火锅"},
            "assets": {
                "qr_code_not_needed": True,
                "reference_images": [str(ref_1), str(ref_2)],
            },
        }
        req, _ = material_skill.normalize_request(raw_req)

        _req, runtime = material_skill.copy_runtime_assets(req, Path(self.tmp.name) / "compressed_refs")

        self.assertEqual(len(runtime.generation_reference_images), 1)
        self.assertEqual(len(runtime.generation_reference_image_roles), 1)
        compressed = Path(runtime.generation_reference_images[0])
        with Image.open(compressed) as image:
            self.assertEqual(image.format, "JPEG")
            self.assertLessEqual(max(image.size), 1024)
        self.assertTrue(any("参考图去重" in warning for warning in runtime.warnings))
        self.assertTrue(any("压缩" in warning for warning in runtime.warnings))

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_small_jpeg_reference_skips_recompression(self):
        ref = Path(self.tmp.name) / "small_ref.jpg"
        Image.new("RGB", (640, 480), (80, 160, 90)).save(ref, "JPEG", quality=90)
        raw_req = {
            "type": "营销海报",
            "size": {"preset": "3:4"},
            "title": "春日尝鲜",
            "store": {"category": "火锅"},
            "assets": {
                "qr_code_not_needed": True,
                "reference_images": [str(ref)],
            },
        }
        req, _ = material_skill.normalize_request(raw_req)

        _req, runtime = material_skill.copy_runtime_assets(req, Path(self.tmp.name) / "small_jpeg")

        self.assertEqual(len(runtime.generation_reference_images), 1)
        model_ref = Path(runtime.generation_reference_images[0])
        self.assertEqual(model_ref.suffix.lower(), ".jpg")
        with Image.open(model_ref) as image:
            self.assertEqual(image.size, (640, 480))
        self.assertTrue(any("跳过重压缩" in warning for warning in runtime.warnings))

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_alpha_logo_reference_preserves_png_transparency(self):
        ref = Path(self.tmp.name) / "dish.png"
        logo = Path(self.tmp.name) / "alpha_logo.png"
        Image.new("RGB", (600, 500), (180, 30, 20)).save(ref)
        logo_img = Image.new("RGBA", (1600, 1200), (250, 210, 20, 0))
        logo_img.putpixel((20, 20), (250, 210, 20, 255))
        logo_img.save(logo)
        raw_req = {
            "type": "五连图",
            "title": "招牌菜连连看",
            "store": {"category": "融合菜"},
            "assets": {
                "reference_images": [str(ref)],
                "brand_logo_path": str(logo),
            },
        }
        req, _ = material_skill.normalize_request(raw_req)

        req, runtime = material_skill.copy_runtime_assets(req, Path(self.tmp.name) / "alpha_logo")

        logo_ref = Path(req["assets"]["brand_logo_path"])
        self.assertIn(str(logo_ref), runtime.generation_reference_images)
        with Image.open(logo_ref) as image:
            self.assertEqual(image.format, "PNG")
            self.assertIn(image.mode, {"RGBA", "LA", "P"})
            self.assertLessEqual(max(image.size), 1024)

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_panorama_postprocess_upscales_and_optionally_slices(self):
        source = Path(self.tmp.name) / "panorama_source.png"
        target = Path(self.tmp.name) / "option_01.png"
        Image.new("RGB", (3840, 576), (34, 120, 78)).save(source)

        material_skill.normalize_panorama_image(source, target)
        with Image.open(target) as image:
            self.assertEqual(image.size, (10240, 1536))

        slice_dir = Path(self.tmp.name) / "option_01_slices"
        slices = material_skill.slice_panorama_image(target, slice_dir)

        self.assertEqual([path.name for path in slices], [f"slice_{idx:02d}.png" for idx in range(1, 6)])
        sizes = []
        for path in slices:
            with Image.open(path) as image:
                sizes.append(image.size)
        self.assertEqual(sizes, [(2048, 1536)] * 5)

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_panorama_publish_requires_five_valid_delivery_slices(self):
        source = Path(self.tmp.name) / "panorama_source.png"
        Image.new("RGB", (10240, 1536), (34, 120, 78)).save(source)
        variant = material_skill.GeneratedVariant(
            index=1,
            template={"style_id": "natural_earth"},
            layout={"id": "layout_panorama", "name": "五连图长画布连贯型"},
            prompt="{}",
            prompt_path=Path(self.tmp.name) / "prompt.json",
            scene_image_path=source,
            final_no_qr_path=None,
            final_path=source,
            png_path=source,
            raw_response_path=None,
        )
        invalid_slices = []
        invalid_dir = Path(self.tmp.name) / "invalid_slices"
        invalid_dir.mkdir()
        for index in range(4):
            path = invalid_dir / f"slice_{index + 1:02d}.png"
            Image.new("RGB", (2048, 1536), (20, 20, 20)).save(path)
            invalid_slices.append(path)

        with (
            mock.patch.object(material_skill, "slice_panorama_image", return_value=invalid_slices),
            self.assertRaises(material_skill.SkillError) as context,
        ):
            material_skill.publish_delivery_material(
                variant,
                Path(self.tmp.name) / "deliverables" / "run_001",
                1,
                material_type="五连图",
            )

        self.assertIn("五连图切片交付失败", str(context.exception))

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_collect_deliverable_pngs_includes_panorama_slices_for_dx_push(self):
        out_dir = Path(self.tmp.name) / "delivery_collect"
        run_dir = out_dir / "deliverables" / "run_001"
        slice_dir = run_dir / "option_01_slices"
        slice_dir.mkdir(parents=True)
        long_image = run_dir / "option_01.png"
        Image.new("RGB", (10240, 1536), (34, 120, 78)).save(long_image)
        for index in range(5):
            Image.new("RGB", (2048, 1536), (34, 120, 78)).save(slice_dir / f"slice_{index + 1:02d}.png")

        files = material_skill._collect_deliverable_pngs(out_dir)

        self.assertEqual(len(files), 6)
        self.assertEqual(Path(files[0]).name, "option_01.png")
        self.assertEqual([Path(path).name for path in files[1:]], [f"slice_{idx:02d}.png" for idx in range(1, 6)])
        with mock.patch.dict(os.environ, {"RESTAURANT_DX_PUSH_DISABLED": "1"}):
            dx_result = material_skill._send_dx_push_notification(out_dir, files, proxy_api_used=True)
        self.assertEqual(dx_result["expected_images"], 6)

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_collect_deliverable_pngs_can_limit_to_current_run(self):
        out_dir = Path(self.tmp.name) / "delivery_collect_current"
        old_run = out_dir / "deliverables" / "run_old"
        new_run = out_dir / "deliverables" / "run_new"
        (old_run / "option_01_slices").mkdir(parents=True)
        (new_run / "option_01_slices").mkdir(parents=True)
        Image.new("RGB", (32, 32), (20, 20, 20)).save(old_run / "option_01.png")
        Image.new("RGB", (32, 32), (30, 30, 30)).save(new_run / "option_01.png")
        Image.new("RGB", (32, 32), (30, 30, 30)).save(new_run / "option_01_slices" / "slice_01.png")

        files = material_skill._collect_deliverable_pngs(out_dir, run_id="run_new")

        self.assertEqual([Path(path).relative_to(new_run) for path in files], [
            Path("option_01.png"),
            Path("option_01_slices/slice_01.png"),
        ])

    @unittest.skipIf(Image is None or ImageDraw is None, "Pillow is not installed")
    def test_panorama_normalize_force_resizes_without_cropping(self):
        source = Path(self.tmp.name) / "panorama_tall_source.png"
        target = Path(self.tmp.name) / "panorama_tall_normalized.png"
        image = Image.new("RGB", (1200, 600), (40, 120, 80))
        draw = ImageDraw.Draw(image)
        draw.rectangle((0, 0, 1199, 59), fill=(230, 20, 20))
        draw.rectangle((0, 540, 1199, 599), fill=(20, 20, 230))
        image.save(source)

        material_skill.normalize_panorama_image(source, target)

        with Image.open(target).convert("RGB") as normalized:
            self.assertEqual(normalized.size, (10240, 1536))
            center_x = normalized.width // 2
            top_pixel = normalized.getpixel((center_x, 10))
            mid_pixel = normalized.getpixel((center_x, normalized.height // 2))
            bottom_pixel = normalized.getpixel((center_x, normalized.height - 10))

        self.assertGreater(top_pixel[0], 180)
        self.assertLess(top_pixel[1], 80)
        self.assertLess(top_pixel[2], 80)
        self.assertLess(mid_pixel[0], 80)
        self.assertGreater(mid_pixel[1], 100)
        self.assertLess(mid_pixel[2], 120)
        self.assertLess(bottom_pixel[0], 80)
        self.assertLess(bottom_pixel[1], 80)
        self.assertGreater(bottom_pixel[2], 180)

    @unittest.skipIf(Image is None or ImageDraw is None, "Pillow is not installed")
    def test_detect_panorama_content_band_measures_top_band_and_deviation(self):
        # Carrier 3840x1280: content band 0..640 (≈6:1, +11% vs 576), pure white below.
        carrier = Path(self.tmp.name) / "carrier.png"
        img = Image.new("RGB", (3840, 1280), (255, 255, 255))
        ImageDraw.Draw(img).rectangle((0, 0, 3839, 639), fill=(200, 40, 30))
        img.save(carrier)

        band = material_skill.detect_panorama_content_band(carrier)
        self.assertEqual(band["top"], 0)
        self.assertGreaterEqual(band["band_height"], 636)
        self.assertLessEqual(band["band_height"], 644)
        # target 576 -> ~+11%
        self.assertAlmostEqual(band["deviation"], (band["band_height"] - 576) / 576, places=5)

    @unittest.skipIf(Image is None or ImageDraw is None, "Pillow is not installed")
    def test_detect_panorama_content_band_trims_sparse_light_tail(self):
        carrier = Path(self.tmp.name) / "carrier_sparse_tail.png"
        img = Image.new("RGB", (3840, 1280), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, 3839, 573), fill=(45, 30, 24))
        # Sparse near-white tail: above the loose 5% content threshold, below the strict 30% effective-content threshold.
        for y in range(574, 713):
            draw.rectangle((0, y, 383, y), fill=(230, 226, 220))
        img.save(carrier)

        band = material_skill.detect_panorama_content_band(carrier)

        self.assertGreaterEqual(band["raw_bottom"], 700)
        self.assertGreaterEqual(band["raw_band_height"], 700)
        self.assertLessEqual(band["bottom"], 590)
        self.assertGreaterEqual(band["band_height"], 560)
        self.assertLessEqual(band["band_height"], 590)
        self.assertTrue(band["tail_trimmed"])
        self.assertEqual(band["trim_params"]["effective_row_content_frac"], 0.30)

    @unittest.skipIf(Image is None or ImageDraw is None, "Pillow is not installed")
    def test_detect_panorama_content_band_keeps_dense_tall_content_untrimmed(self):
        carrier = Path(self.tmp.name) / "carrier_dense_tall.png"
        img = Image.new("RGB", (3840, 1280), (255, 255, 255))
        ImageDraw.Draw(img).rectangle((0, 0, 3839, 704), fill=(45, 30, 24))
        img.save(carrier)

        band = material_skill.detect_panorama_content_band(carrier)

        self.assertEqual(band["raw_band_height"], 705)
        self.assertEqual(band["band_height"], 705)
        self.assertFalse(band["tail_trimmed"])
        self.assertGreater(band["abs_deviation"], material_skill.PANORAMA_BAND_TOLERANCE)

    @unittest.skipIf(Image is None or ImageDraw is None, "Pillow is not installed")
    def test_detect_panorama_content_band_marks_one_pixel_content_as_failed(self):
        carrier = Path(self.tmp.name) / "carrier_one_pixel_band.png"
        img = Image.new("RGB", (3840, 1280), (255, 255, 255))
        ImageDraw.Draw(img).rectangle((0, 0, 3839, 0), fill=(45, 30, 24))
        img.save(carrier)

        band = material_skill.detect_panorama_content_band(carrier)

        self.assertEqual(band["band_height"], 1)
        self.assertGreater(band["abs_deviation"], material_skill.PANORAMA_BAND_TOLERANCE)

    @unittest.skipIf(Image is None or ImageDraw is None, "Pillow is not installed")
    def test_detect_panorama_content_band_marks_blank_carrier_as_content_missing(self):
        carrier = Path(self.tmp.name) / "carrier_blank.png"
        Image.new("RGB", (3840, 1280), (255, 255, 255)).save(carrier)

        band = material_skill.detect_panorama_content_band(carrier)

        self.assertEqual(band["band_height"], 0)
        self.assertEqual(band["raw_band_height"], 0)
        self.assertTrue(band["content_missing"])
        self.assertLess(band["deviation"], 0)
        self.assertGreater(band["abs_deviation"], material_skill.PANORAMA_BAND_TOLERANCE)
        self.assertIn("过矮或内容不足", material_skill._panorama_height_feedback(band))

    def test_panorama_band_tolerance_is_fifteen_percent(self):
        self.assertEqual(material_skill.PANORAMA_BAND_TOLERANCE, 0.15)

    @unittest.skipIf(Image is None or ImageDraw is None, "Pillow is not installed")
    def test_crop_panorama_content_band_crops_full_detected_band(self):
        carrier = Path(self.tmp.name) / "carrier_crop.png"
        img = Image.new("RGB", (3840, 1280), (255, 255, 255))
        ImageDraw.Draw(img).rectangle((0, 0, 3839, 620), fill=(220, 30, 30))
        ImageDraw.Draw(img).rectangle((0, 700, 3839, 760), fill=(20, 20, 230))
        img.save(carrier)

        cropped = Path(self.tmp.name) / "cropped.png"
        band = material_skill.detect_panorama_content_band(carrier)
        material_skill.crop_panorama_content_band(carrier, cropped, band)

        with Image.open(cropped).convert("RGB") as out:
            self.assertEqual(out.size, (3840, 621))
            colors = {out.getpixel((1920, y)) for y in range(0, out.height, 32)}
            self.assertTrue(all(r > 180 and g < 80 and b < 80 for (r, g, b) in colors))

    @unittest.skipIf(Image is None or ImageDraw is None, "Pillow is not installed")
    def test_crop_panorama_content_band_uses_trimmed_band_without_redetecting(self):
        carrier = Path(self.tmp.name) / "carrier_crop_trimmed.png"
        img = Image.new("RGB", (3840, 1280), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        draw.rectangle((0, 0, 3839, 573), fill=(45, 30, 24))
        for y in range(574, 713):
            draw.rectangle((0, y, 383, y), fill=(230, 226, 220))
        img.save(carrier)

        cropped = Path(self.tmp.name) / "cropped_trimmed.png"
        band = material_skill.detect_panorama_content_band(carrier)
        material_skill.crop_panorama_content_band(carrier, cropped, band)

        with Image.open(cropped).convert("RGB") as out:
            self.assertEqual(out.height, band["band_height"])
            self.assertEqual(out.height, band["trimmed_band_height"])
            self.assertLess(out.height, band["raw_band_height"])

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_panorama_carrier_retries_until_band_within_tolerance(self):
        # Stub generate_scene_image to write carriers with controlled band heights:
        # attempt1 = +16% (over 15% tolerance), attempt2 = within 15% -> should stop at attempt2.
        heights = iter([670, 588])  # 670≈+16%, 588≈+2%

        def fake_generate(prompt, reference_images, out_path, raw_response_path, provider,
                          dry_run, width, height, variant, **kwargs):
            band_h = next(heights)
            img = Image.new("RGB", (width, height), (255, 255, 255))
            ImageDraw.Draw(img).rectangle((0, 0, width - 1, band_h - 1), fill=(180, 60, 40))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(out_path)
            return [], "ok"

        req, _ = material_skill.normalize_request({
            "type": "五连图", "title": "麻辣鲜香", "store": {"name": "洪掌柜", "category": "火锅"},
            "assets": {"reference_images": [str(Path(self.tmp.name) / "ref.png")]},
        })
        (Path(self.tmp.name) / "ref.png").write_bytes(PNG_BYTES)
        req, runtime = material_skill.copy_runtime_assets(req, Path(self.tmp.name) / "carrier_run")
        out_dir = Path(self.tmp.name) / "carrier_run"
        (out_dir / "variants" / "variant_01").mkdir(parents=True, exist_ok=True)
        template = material_skill.load_templates("五连图")[0]
        layout = material_skill.select_layout(req, template, material_skill.PANORAMA_API_WIDTH, material_skill.PANORAMA_API_HEIGHT)

        with mock.patch.object(material_skill, "generate_scene_image", side_effect=fake_generate):
            final_path, warnings, _ = material_skill._generate_panorama_carrier(
                index=1, template=template, req=req, runtime=runtime, layout=layout,
                out_dir=out_dir, provider="api", dry_run=False,
            )

        with Image.open(final_path) as im:
            self.assertEqual(im.size, (material_skill.PANORAMA_FINAL_WIDTH, material_skill.PANORAMA_FINAL_HEIGHT))
        # exactly 2 attempts (stopped once within tolerance), and second attempt was accepted
        attempt_files = sorted((out_dir / "materials" / "panorama_01").glob("carrier_attempt*.png"))
        self.assertEqual([p.name for p in attempt_files], ["carrier_attempt1.png", "carrier_attempt2.png"])
        self.assertTrue(any("≤" in w for w in warnings))

    @unittest.skipIf(Image is None or ImageDraw is None, "Pillow is not installed")
    def test_panorama_carrier_runs_quality_check_on_raw_carrier_and_skips_without_blocking(self):
        quality_inputs = []

        def fake_generate(prompt, reference_images, out_path, raw_response_path, provider,
                          dry_run, width, height, variant, **kwargs):
            img = Image.new("RGB", (width, height), (255, 255, 255))
            ImageDraw.Draw(img).rectangle((0, 0, width - 1, 599), fill=(180, 60, 40))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(out_path)
            return [], "ok"

        def fake_quality_check(carrier_path, req, attempt):
            quality_inputs.append((Path(carrier_path), attempt))
            return {"status": "skipped", "passed": True, "issues": [], "summary": "host visual tool unavailable"}

        req, _ = material_skill.normalize_request({
            "type": "五连图", "title": "麻辣鲜香", "store": {"name": "洪掌柜", "category": "火锅"},
            "assets": {"reference_images": [str(Path(self.tmp.name) / "ref.png")]},
        })
        (Path(self.tmp.name) / "ref.png").write_bytes(PNG_BYTES)
        req, runtime = material_skill.copy_runtime_assets(req, Path(self.tmp.name) / "carrier_quality_skip")
        out_dir = Path(self.tmp.name) / "carrier_quality_skip"
        (out_dir / "variants" / "variant_01").mkdir(parents=True, exist_ok=True)
        template = material_skill.load_templates("五连图")[0]
        layout = material_skill.select_layout(req, template, material_skill.PANORAMA_API_WIDTH, material_skill.PANORAMA_API_HEIGHT)

        with (
            mock.patch.object(material_skill, "generate_scene_image", side_effect=fake_generate),
            mock.patch.object(material_skill, "run_panorama_host_quality_check", side_effect=fake_quality_check),
        ):
            final_path, warnings, _ = material_skill._generate_panorama_carrier(
                index=1, template=template, req=req, runtime=runtime, layout=layout,
                out_dir=out_dir, provider="api", dry_run=False,
            )

        self.assertTrue(final_path.exists())
        self.assertEqual(len(quality_inputs), 1)
        self.assertEqual(quality_inputs[0][1], 1)
        self.assertEqual(quality_inputs[0][0].name, "carrier_attempt1.png")
        self.assertTrue(any("quality_check: skipped" in warning for warning in warnings))

    @unittest.skipIf(Image is None or ImageDraw is None, "Pillow is not installed")
    def test_panorama_carrier_does_not_retry_based_on_final_preview_brightness(self):
        generated_prompts = []

        def fake_generate(prompt, reference_images, out_path, raw_response_path, provider,
                          dry_run, width, height, variant, **kwargs):
            generated_prompts.append(prompt)
            img = Image.new("RGB", (width, height), (255, 255, 255))
            draw = ImageDraw.Draw(img)
            draw.rectangle((0, 0, width - 1, 517), fill=(45, 30, 24))
            draw.rectangle((0, 518, width - 1, 575), fill=(243, 243, 243))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(out_path)
            return [], "ok"

        def fake_quality_check(carrier_path, req, attempt):
            return {"status": "skipped", "passed": True, "issues": [], "text_error_count": 0, "dish_error_count": 0}

        req, _ = material_skill.normalize_request({
            "type": "五连图", "title": "麻辣鲜香", "store": {"name": "洪掌柜", "category": "火锅"},
            "assets": {"reference_images": [str(Path(self.tmp.name) / "ref.png")]},
        })
        (Path(self.tmp.name) / "ref.png").write_bytes(PNG_BYTES)
        req, runtime = material_skill.copy_runtime_assets(req, Path(self.tmp.name) / "carrier_brightness_gate")
        out_dir = Path(self.tmp.name) / "carrier_brightness_gate"
        (out_dir / "variants" / "variant_01").mkdir(parents=True, exist_ok=True)
        template = material_skill.load_templates("五连图")[0]
        layout = material_skill.select_layout(req, template, material_skill.PANORAMA_API_WIDTH, material_skill.PANORAMA_API_HEIGHT)

        with (
            mock.patch.object(material_skill, "generate_scene_image", side_effect=fake_generate),
            mock.patch.object(material_skill, "run_panorama_host_quality_check", side_effect=fake_quality_check),
        ):
            final_path, warnings, _ = material_skill._generate_panorama_carrier(
                index=1, template=template, req=req, runtime=runtime, layout=layout,
                out_dir=out_dir, provider="api", dry_run=False,
            )

        self.assertTrue(final_path.exists())
        attempt_files = sorted((out_dir / "materials" / "panorama_01").glob("carrier_attempt*.png"))
        self.assertEqual([p.name for p in attempt_files], ["carrier_attempt1.png"])
        self.assertEqual(len(generated_prompts), 1)
        self.assertFalse(any("白条" in warning for warning in warnings))
        prompt_record = material_skill.read_json(out_dir / "variants" / "variant_01" / "prompt.json")
        self.assertNotIn("white_strip_check", prompt_record["carrier_attempts"][0])

    def test_panorama_host_quality_check_reads_agent_file_result(self):
        out_dir = Path(self.tmp.name) / "agent_file_quality"
        carrier_dir = out_dir / "materials" / "panorama_01"
        carrier_dir.mkdir(parents=True, exist_ok=True)
        carrier_path = carrier_dir / "carrier_attempt1.png"
        carrier_path.write_bytes(PNG_BYTES)
        result_path = carrier_dir / "quality_check_attempt1.result.json"
        material_skill.write_json(result_path, {
            "status": "failed",
            "passed": False,
            "issues": ["菜名必须写作：葱油鲈鱼"],
            "text_error_count": 1,
            "dish_error_count": 0,
        })
        material_skill._write_status(out_dir, "awaiting_quality_check")

        with mock.patch.dict(os.environ, {
            "RESTAURANT_PANORAMA_HOST_QUALITY_MODE": "agent_file",
            "RESTAURANT_PANORAMA_HOST_QUALITY_TIMEOUT": "1",
        }):
            result = material_skill.run_panorama_host_quality_check(
                carrier_path,
                {"products": [{"name": "葱油鲈鱼"}]},
                1,
            )

        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["passed"])
        self.assertEqual(result["text_error_count"], 1)
        self.assertTrue((carrier_dir / "quality_check_attempt1.request.json").exists())
        status = material_skill.read_json(out_dir / "status.json")
        self.assertEqual(status["status"], "generating")
        self.assertEqual(status["quality_check_request"]["status"], "completed")

    def test_panorama_quality_request_requires_content_presence_before_text_check(self):
        out_dir = Path(self.tmp.name) / "agent_file_quality_request"
        carrier_dir = out_dir / "materials" / "panorama_01"
        carrier_dir.mkdir(parents=True, exist_ok=True)
        carrier_path = carrier_dir / "carrier_attempt1.png"
        carrier_path.write_bytes(PNG_BYTES)

        with mock.patch.dict(os.environ, {
            "RESTAURANT_PANORAMA_HOST_QUALITY_MODE": "agent_file",
            "RESTAURANT_PANORAMA_HOST_QUALITY_TIMEOUT": "0.01",
        }):
            result = material_skill.run_panorama_host_quality_check(
                carrier_path,
                {"products": [{"name": "葱油鲈鱼"}, {"name": "青椒鳝丝"}]},
                1,
            )

        request = material_skill.read_json(carrier_dir / "quality_check_attempt1.request.json")
        self.assertEqual(result["status"], "skipped")
        self.assertIn("先确认能清晰看到预期菜品和菜名标签", request["instructions"])
        self.assertIn("空白图", request["instructions"])
        self.assertIn("菜品缺失", request["instructions"])
        self.assertIn("菜名缺失", request["instructions"])
        self.assertIn("status=failed", request["instructions"])
        self.assertIn("所有可见中文菜名、角标、短标签、标题和营销文字", request["instructions"])
        self.assertIn("单字错", request["instructions"])
        self.assertIn("形近字替换", request["instructions"])
        self.assertIn("推荐", request["instructions"])
        self.assertIn("香辣焖鱼", request["instructions"])

    def test_panorama_quality_result_fails_when_text_error_count_is_positive(self):
        result = material_skill._normalize_panorama_quality_result({
            "status": "passed",
            "passed": True,
            "issues": ["推荐的荐写成形近错字", "香辣焖鱼的焖写错"],
            "text_error_count": 2,
            "dish_error_count": 0,
            "label_position_error_count": 1,
        })

        self.assertEqual(result["status"], "failed")
        self.assertFalse(result["passed"])
        self.assertEqual(result["text_error_count"], 2)
        self.assertEqual(result["label_position_error_count"], 1)

    def test_panorama_best_candidate_counts_label_position_errors_after_text_errors(self):
        candidates = [
            {
                "attempt": 1,
                "deviation": 0.001,
                "height_passed": True,
                "quality_passed": True,
                "quality_check": {"status": "passed", "text_error_count": 0, "dish_error_count": 0, "label_position_error_count": 1},
            },
            {
                "attempt": 2,
                "deviation": 0.02,
                "height_passed": True,
                "quality_passed": True,
                "quality_check": {"status": "passed", "text_error_count": 0, "dish_error_count": 0, "label_position_error_count": 0},
            },
        ]

        best = material_skill._panorama_best_candidate(candidates)

        self.assertEqual(best["attempt"], 2)

    def test_panorama_quality_request_does_not_require_dish_labels_without_expected_names(self):
        out_dir = Path(self.tmp.name) / "agent_file_quality_request_no_names"
        carrier_dir = out_dir / "materials" / "panorama_01"
        carrier_dir.mkdir(parents=True, exist_ok=True)
        carrier_path = carrier_dir / "carrier_attempt1.png"
        carrier_path.write_bytes(PNG_BYTES)

        with mock.patch.dict(os.environ, {
            "RESTAURANT_PANORAMA_HOST_QUALITY_MODE": "agent_file",
            "RESTAURANT_PANORAMA_HOST_QUALITY_TIMEOUT": "0.01",
        }):
            result = material_skill.run_panorama_host_quality_check(
                carrier_path,
                {"products": [{"description": "招牌菜参考图"}]},
                1,
            )

        request = material_skill.read_json(carrier_dir / "quality_check_attempt1.request.json")
        self.assertEqual(result["status"], "skipped")
        self.assertEqual(request["expected_dish_names"], [])
        self.assertNotIn("先确认能清晰看到预期菜品和菜名标签", request["instructions"])
        self.assertIn("expected_dish_names 为空时，不检查菜名标签缺失", request["instructions"])

    def test_panorama_height_feedback_is_directional_without_exact_percent(self):
        high_band = {
            "band_height": 705,
            "deviation": (705 - material_skill.PANORAMA_TARGET_BAND_HEIGHT) / material_skill.PANORAMA_TARGET_BAND_HEIGHT,
        }
        low_band = {
            "band_height": 1,
            "deviation": (1 - material_skill.PANORAMA_TARGET_BAND_HEIGHT) / material_skill.PANORAMA_TARGET_BAND_HEIGHT,
        }

        high_feedback = material_skill._panorama_height_feedback(high_band)
        low_feedback = material_skill._panorama_height_feedback(low_band)

        self.assertNotIn("%", high_feedback)
        self.assertNotIn("490-662px", high_feedback)
        self.assertNotIn("目标约 576px", high_feedback)
        self.assertIn("顶部长条标签画得太高", high_feedback)
        self.assertIn("超扁横向贴纸", high_feedback)
        self.assertIn("纯白空白", high_feedback)
        self.assertNotIn("%", low_feedback)
        self.assertNotIn("490-662px", low_feedback)
        self.assertNotIn("目标约 576px", low_feedback)
        self.assertIn("过矮或内容不足", low_feedback)
        self.assertIn("菜品和文字清晰可见", low_feedback)

    def test_panorama_post_check_buffer_expands_with_host_quality_timeout(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertEqual(material_skill._panorama_post_check_buffer_seconds(), 10.0)
        with mock.patch.dict(os.environ, {"RESTAURANT_PANORAMA_HOST_QUALITY_TIMEOUT": "30"}, clear=True):
            self.assertEqual(material_skill._panorama_host_quality_timeout_seconds(), 30.0)
            self.assertEqual(material_skill._panorama_host_quality_poll_interval_seconds(), 5.0)
            self.assertEqual(material_skill._panorama_post_check_buffer_seconds(), 35.0)
        with mock.patch.dict(os.environ, {"RESTAURANT_PANORAMA_HOST_QUALITY_TIMEOUT": "120"}, clear=True):
            self.assertEqual(material_skill._panorama_host_quality_timeout_seconds(), 90.0)
            self.assertEqual(material_skill._panorama_post_check_buffer_seconds(), 95.0)

    def test_panorama_api_timeout_defaults_to_660_seconds(self):
        self.assertEqual(material_skill.DEFAULT_IMAGE_TIMEOUT, 420)
        self.assertEqual(material_skill.DEFAULT_IMAGE_TIMEOUT_PANORAMA, 660)
        self.assertEqual(material_skill.FALLBACK_IMAGE_TIMEOUT_PANORAMA, 660)

    @unittest.skipIf(Image is None or ImageDraw is None, "Pillow is not installed")
    def test_panorama_wall_time_budget_stops_starting_new_candidate(self):
        def fake_generate(prompt, reference_images, out_path, raw_response_path, provider,
                          dry_run, width, height, variant, **kwargs):
            img = Image.new("RGB", (width, height), (255, 255, 255))
            ImageDraw.Draw(img).rectangle((0, 0, width - 1, 799), fill=(180, 60, 40))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(out_path)
            return [], "ok"

        req, _ = material_skill.normalize_request({
            "type": "五连图", "title": "麻辣鲜香", "store": {"name": "洪掌柜", "category": "火锅"},
            "assets": {"reference_images": [str(Path(self.tmp.name) / "ref.png")]},
        })
        (Path(self.tmp.name) / "ref.png").write_bytes(PNG_BYTES)
        req, runtime = material_skill.copy_runtime_assets(req, Path(self.tmp.name) / "carrier_budget")
        out_dir = Path(self.tmp.name) / "carrier_budget"
        (out_dir / "variants" / "variant_01").mkdir(parents=True, exist_ok=True)
        template = material_skill.load_templates("五连图")[0]
        layout = material_skill.select_layout(req, template, material_skill.PANORAMA_API_WIDTH, material_skill.PANORAMA_API_HEIGHT)

        with (
            mock.patch.object(material_skill, "generate_scene_image", side_effect=fake_generate),
            mock.patch.object(material_skill.time, "monotonic", side_effect=[0.0, 1190.0]),
        ):
            final_path, warnings, _ = material_skill._generate_panorama_carrier(
                index=1, template=template, req=req, runtime=runtime, layout=layout,
                out_dir=out_dir, provider="api", dry_run=False,
                image_timeout=10, image_max_attempts=2,
            )

        self.assertTrue(final_path.exists())
        attempt_files = sorted((out_dir / "materials" / "panorama_01").glob("carrier_attempt*.png"))
        self.assertEqual([p.name for p in attempt_files], ["carrier_attempt1.png"])
        self.assertTrue(any("总耗时预算" in warning for warning in warnings))

    def test_panorama_best_candidate_prioritizes_quality_over_height_passed(self):
        candidates = [
            {
                "attempt": 1,
                "deviation": 0.003,
                "height_passed": True,
                "quality_passed": False,
                "quality_check": {"status": "failed", "text_error_count": 1, "dish_error_count": 0},
            },
            {
                "attempt": 3,
                "deviation": 0.238,
                "height_passed": False,
                "quality_passed": True,
                "quality_check": {"status": "skipped", "text_error_count": 0, "dish_error_count": 0},
            },
        ]

        best = material_skill._panorama_best_candidate(candidates)

        self.assertEqual(best["attempt"], 3)

    def test_panorama_best_candidate_uses_quality_passed_tall_candidate_before_quality_failed(self):
        candidates = [
            {
                "attempt": 1,
                "deviation": 0.28,
                "height_passed": False,
                "quality_passed": False,
                "quality_check": {"status": "failed", "text_error_count": 1, "dish_error_count": 1},
            },
            {
                "attempt": 2,
                "deviation": 0.35,
                "height_passed": False,
                "quality_passed": True,
                "quality_check": {"status": "skipped", "text_error_count": 0, "dish_error_count": 0},
            },
        ]

        best = material_skill._panorama_best_candidate(candidates)

        self.assertEqual(best["attempt"], 2)

    @unittest.skipIf(Image is None or ImageDraw is None, "Pillow is not installed")
    def test_panorama_carrier_merges_height_and_quality_feedback_without_exceeding_three_candidates(self):
        generated_prompts = []
        heights = iter([806, 640, 600])

        def fake_generate(prompt, reference_images, out_path, raw_response_path, provider,
                          dry_run, width, height, variant, **kwargs):
            generated_prompts.append(prompt)
            band_h = next(heights)
            img = Image.new("RGB", (width, height), (255, 255, 255))
            ImageDraw.Draw(img).rectangle((0, 0, width - 1, band_h - 1), fill=(180, 60, 40))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(out_path)
            return [], "ok"

        quality_results = iter([
            {"status": "failed", "passed": False, "issues": ["菜名必须写作：葱油鲈鱼"], "text_error_count": 1, "dish_error_count": 0},
            {"status": "failed", "passed": False, "issues": ["第 3 道看着像绿色豆角，实际应是青椒鳝丝"], "text_error_count": 0, "dish_error_count": 1},
            {"status": "passed", "passed": True, "issues": [], "text_error_count": 0, "dish_error_count": 0},
        ])

        def fake_quality_check(carrier_path, req, attempt):
            return next(quality_results)

        req, _ = material_skill.normalize_request({
            "type": "五连图",
            "title": "川韵家常菜",
            "store": {"name": "川韵小馆", "category": "川湘菜"},
            "products": [
                {"name": "葱油鲈鱼", "reference_image_index": 1},
                {"name": "青椒鳝丝", "reference_image_index": 2},
            ],
            "assets": {
                "reference_images": [str(Path(self.tmp.name) / "ref1.png"), str(Path(self.tmp.name) / "ref2.png")]
            },
        })
        (Path(self.tmp.name) / "ref1.png").write_bytes(PNG_BYTES)
        (Path(self.tmp.name) / "ref2.png").write_bytes(PNG_BYTES)
        req, runtime = material_skill.copy_runtime_assets(req, Path(self.tmp.name) / "carrier_feedback")
        out_dir = Path(self.tmp.name) / "carrier_feedback"
        (out_dir / "variants" / "variant_01").mkdir(parents=True, exist_ok=True)
        template = material_skill.load_templates("五连图")[0]
        layout = material_skill.select_layout(req, template, material_skill.PANORAMA_API_WIDTH, material_skill.PANORAMA_API_HEIGHT)

        with (
            mock.patch.object(material_skill, "generate_scene_image", side_effect=fake_generate),
            mock.patch.object(material_skill, "run_panorama_host_quality_check", side_effect=fake_quality_check),
        ):
            final_path, warnings, _ = material_skill._generate_panorama_carrier(
                index=1, template=template, req=req, runtime=runtime, layout=layout,
                out_dir=out_dir, provider="api", dry_run=False,
            )

        self.assertTrue(final_path.exists())
        attempt_files = sorted((out_dir / "materials" / "panorama_01").glob("carrier_attempt*.png"))
        self.assertEqual([p.name for p in attempt_files], ["carrier_attempt1.png", "carrier_attempt2.png", "carrier_attempt3.png"])
        self.assertEqual(len(generated_prompts), 3)
        self.assertNotIn("quality_feedback", generated_prompts[0])
        self.assertIn("顶部长条标签画得太高", generated_prompts[1])
        self.assertIn("超扁横向贴纸", generated_prompts[1])
        self.assertIn("纯白空白", generated_prompts[1])
        self.assertNotIn("490-662px", generated_prompts[1])
        self.assertNotIn("目标约 576px", generated_prompts[1])
        self.assertNotIn("39.9%", generated_prompts[1])
        self.assertIn("菜名必须写作：葱油鲈鱼", generated_prompts[1])
        self.assertIn("第 3 道看着像绿色豆角，实际应是青椒鳝丝", generated_prompts[2])
        self.assertFalse(any("葱油鲈鱼" in line and "绿色豆角" in line for line in generated_prompts[2].splitlines()))
        self.assertTrue(any("第 3 次生成通过" in warning for warning in warnings))

    @unittest.skipIf(Image is None or ImageDraw is None, "Pillow is not installed")
    def test_panorama_carrier_passes_calibrated_layout_mask_first(self):
        captured_refs = []
        captured_roles = []

        def fake_generate(prompt, reference_images, out_path, raw_response_path, provider,
                          dry_run, width, height, variant, **kwargs):
            captured_refs.append(list(reference_images))
            captured_roles.append(list(kwargs.get("reference_image_roles") or []))
            img = Image.new("RGB", (width, height), (255, 255, 255))
            ImageDraw.Draw(img).rectangle((0, 0, width - 1, 599), fill=(180, 60, 40))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(out_path)
            return [], "ok"

        req, _ = material_skill.normalize_request({
            "type": "五连图", "title": "麻辣鲜香", "store": {"name": "洪掌柜", "category": "火锅"},
            "assets": {"reference_images": [str(Path(self.tmp.name) / "ref.png")]},
        })
        (Path(self.tmp.name) / "ref.png").write_bytes(PNG_BYTES)
        req, runtime = material_skill.copy_runtime_assets(req, Path(self.tmp.name) / "carrier_mask_run")
        out_dir = Path(self.tmp.name) / "carrier_mask_run"
        (out_dir / "variants" / "variant_01").mkdir(parents=True, exist_ok=True)
        template = material_skill.load_templates("五连图")[0]
        layout = material_skill.select_layout(req, template, material_skill.PANORAMA_API_WIDTH, material_skill.PANORAMA_API_HEIGHT)

        with mock.patch.object(material_skill, "generate_scene_image", side_effect=fake_generate):
            material_skill._generate_panorama_carrier(
                index=1, template=template, req=req, runtime=runtime, layout=layout,
                out_dir=out_dir, provider="api", dry_run=False,
            )

        self.assertTrue(captured_refs)
        mask_path = Path(captured_refs[0][0])
        self.assertEqual(mask_path.name, "layout_mask_396px.png")
        self.assertTrue(captured_roles)
        self.assertIn("INTERNAL_GEOMETRY_MASK_ONLY", captured_roles[0][0])
        self.assertIn("DISH_REFERENCE", captured_roles[0][1])
        self.assertNotIn("STYLE_REFERENCE", captured_roles[0][1])
        self.assertTrue(mask_path.exists())
        with Image.open(mask_path).convert("RGB") as mask:
            self.assertEqual(mask.size, (material_skill.PANORAMA_CARRIER_WIDTH, material_skill.PANORAMA_CARRIER_HEIGHT))
            self.assertEqual(mask.getpixel((100, material_skill.PANORAMA_LAYOUT_MASK_CONTENT_HEIGHT - 2)), (220, 220, 220))
            self.assertEqual(mask.getpixel((100, material_skill.PANORAMA_LAYOUT_MASK_CONTENT_HEIGHT + 2)), (255, 255, 255))

    @unittest.skipIf(Image is None or ImageDraw is None, "Pillow is not installed")
    def test_panorama_carrier_inserts_layout_mask_role_before_style_reference_role(self):
        captured_roles = []

        def fake_generate(prompt, reference_images, out_path, raw_response_path, provider,
                          dry_run, width, height, variant, **kwargs):
            captured_roles.append(list(kwargs.get("reference_image_roles") or []))
            img = Image.new("RGB", (width, height), (255, 255, 255))
            ImageDraw.Draw(img).rectangle((0, 0, width - 1, 599), fill=(180, 60, 40))
            out_path.parent.mkdir(parents=True, exist_ok=True)
            img.save(out_path)
            return [], "ok"

        style_ref = Path(self.tmp.name) / "style_ref.png"
        dish_ref = Path(self.tmp.name) / "dish_ref.png"
        Image.new("RGB", (8, 8), (220, 30, 30)).save(style_ref)
        Image.new("RGB", (8, 8), (30, 160, 80)).save(dish_ref)
        req, _ = material_skill.normalize_request({
            "type": "五连图",
            "title": "麻辣鲜香",
            "store": {"name": "洪掌柜", "category": "火锅"},
            "assets": {
                "style_reference_images": [str(style_ref)],
                "style_reference_note": "参考图是红黑强对比背景，大标题居中，菜品围绕标题排列",
                "reference_images": [str(dish_ref)],
            },
        })
        req, runtime = material_skill.copy_runtime_assets(req, Path(self.tmp.name) / "carrier_style_ref_roles")
        out_dir = Path(self.tmp.name) / "carrier_style_ref_roles"
        (out_dir / "variants" / "variant_01").mkdir(parents=True, exist_ok=True)
        template = material_skill.load_templates("五连图")[0]
        layout = material_skill.select_layout(req, template, material_skill.PANORAMA_API_WIDTH, material_skill.PANORAMA_API_HEIGHT)

        with mock.patch.object(material_skill, "generate_scene_image", side_effect=fake_generate):
            material_skill._generate_panorama_carrier(
                index=1, template=template, req=req, runtime=runtime, layout=layout,
                out_dir=out_dir, provider="api", dry_run=False,
            )

        self.assertTrue(captured_roles)
        self.assertIn("INTERNAL_GEOMETRY_MASK_ONLY", captured_roles[0][0])
        self.assertIn("STYLE_REFERENCE_HIGHEST_PRIORITY", captured_roles[0][1])
        self.assertIn("DISH_REFERENCE", captured_roles[0][2])
        prompt_payload = json.loads((out_dir / "materials" / "panorama_01" / "prompt_attempt1.json").read_text(encoding="utf-8"))
        self.assertEqual(prompt_payload["generation_reference_image_roles"], captured_roles[0])

    def test_panorama_prompt_uses_v3a_top_band_carrier_rules(self):
        req, _ = material_skill.normalize_request({
            "type": "五连图", "title": "来一碗麻辣鲜香全都有",
            "store": {"name": "洪掌柜", "category": "火锅"},
            "assets": {"reference_images": ["/tmp/dish.jpg"]},
        })
        template = material_skill.load_templates("五连图")[0]
        layout = material_skill.select_layout(req, template, material_skill.PANORAMA_API_WIDTH, material_skill.PANORAMA_API_HEIGHT)
        prompt = material_skill.build_scene_prompt(req, template, layout, 3840, 1280)
        prompt_json = json.loads(prompt)
        text = json.dumps(prompt_json, ensure_ascii=False)

        self.assertIn("carrier_layout_rule", prompt_json)
        self.assertIn("layout_mask_reference", prompt_json)
        self.assertIn("20:3", text)
        self.assertIn("纯白", text)
        self.assertIn("396", text)
        self.assertIn("超扁横向长条标签", text)
        self.assertIn("窄幅腰封", text)
        self.assertIn("高于任何风格参考图", text)
        self.assertIn("不得模仿参考图的满幅海报结构", text)
        self.assertNotIn("目标约 576px", text)
        self.assertNotIn("可接受 490-662px", text)
        self.assertNotIn("绝不超过 662px", text)
        self.assertNotIn("461-691px", text)
        self.assertNotIn("690px", text)
        self.assertNotIn("书签", text)
        self.assertNotIn("segmented_5", text)
        self.assertIn("no content in the bottom white margin", prompt_json["negative"])

    def test_scene_prompt_is_compact_and_requires_local_brand_font_and_qr_slot_rules(self):
        qr_path = Path(self.tmp.name) / "qr.png"
        qr_path.write_bytes(PNG_BYTES)
        raw_req = {
            "type": "营销海报",
            "size": {"preset": "3:4"},
            "title": "春日尝鲜",
            "store": {"name": "山城热辣火锅", "category": "火锅"},
            "campaign": {"offer": "扫码下单 更优惠", "cta": "立即扫码"},
            "products": [],
            "style": {"name": "美团餐饮团购"},
            "assets": {
                "qr_code_path": str(qr_path),
                "food_image_not_needed": True,
                "mascot_mode": "auto",
            },
        }
        req, _ = material_skill.normalize_request(raw_req)
        req, _runtime = material_skill.copy_runtime_assets(req, Path(self.tmp.name))
        template = {
            "template_id": "unit",
            "variant": "big_type_impact",
            "scene_prompt": "dense 3D restaurant scene",
            "negative": "no QR code, no fake QR",
        }
        layout = {"id": "layout_a", "name": "Unit Layout"}

        prompt = material_skill.build_scene_prompt(req, template, layout, 1080, 1440)
        prompt_json = json.loads(prompt)
        prompt_text = json.dumps(prompt_json, ensure_ascii=False)

        self.assertLess(len(prompt), 11000)
        self.assertIn("本地品牌素材", prompt_text)
        self.assertNotIn("Meituan Type", prompt_text)
        self.assertIn("不得指定或模仿具体商业/版权字体", prompt_text)
        self.assertIn("通用字形", prompt_text)
        self.assertIn("冲击力", prompt_text)
        self.assertIn("除二维码预留位外不要留空白", prompt_text)
        self.assertIn("不要自行生成或改造 logo", prompt_text)

    def test_style_controls_typography_profiles_are_valid(self):
        controls = material_skill._style_controls()
        profiles = controls.get("typography_profiles", {})

        self.assertNotIn("material_typography_defaults", controls)
        self.assertNotIn("typography_contrast_pairs", controls)
        self.assertNotIn("variant_typography_defaults", controls)
        self.assertNotIn("visual_contrast_pairs", controls)
        self.assertIsInstance(profiles, dict)
        self.assertGreaterEqual(len(profiles), 8)

        for profile_id, profile in profiles.items():
            self.assertEqual(profile["id"], profile_id)
            self.assertTrue(profile.get("prompt_guidance"))
            self.assertIn("open_font_examples", profile)
            self.assertIn("avoid_for", profile)
            self.assertTrue(profile["open_font_examples"])
            for font in profile["open_font_examples"]:
                self.assertTrue(font.get("name"))
                self.assertTrue(font.get("license"))
                self.assertTrue(font.get("source_url"))
                self.assertEqual(font.get("license_status"), "verified")

        for style in material_skill.load_styles("营销海报"):
            self.assertIn(style["typography_profile"], profiles, f"{style['style_id']} references unknown typography profile")

    def test_scene_prompt_uses_style_typography_profile_without_font_example_names(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "3:4"},
                "title": "春日尝鲜",
                "store": {"name": "城市西餐厅", "category": "西餐"},
                "style": {"name": "美团餐饮团购"},
                "assets": {"qr_code_not_needed": True, "food_image_not_needed": True},
            }
        )
        style = next(item for item in material_skill.load_styles("营销海报") if item["style_id"] == "clean_hero")
        prompt = material_skill.build_scene_prompt(
            req,
            style,
            {"id": "layout_a", "name": "Unit Layout"},
            1080,
            1440,
        )
        prompt_json = json.loads(prompt)
        prompt_text = json.dumps(prompt_json, ensure_ascii=False)
        profile = prompt_json["style"]["typography_profile"]

        self.assertEqual(profile["id"], "modern_minimal_sans")
        self.assertIn(profile["prompt_guidance"], prompt_text)
        for forbidden in (
            "Source Han Sans",
            "Source Han Serif",
            "JetBrains Mono",
            "Bebas Neue",
            "霞鹜文楷",
            "Meituan Type",
            "微软雅黑",
            "苹方",
            "方正",
            "汉仪",
            "迪士尼字体",
        ):
            self.assertNotIn(forbidden, prompt_text)

    def test_explicit_typography_profile_overrides_variant_default(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "3:4"},
                "title": "老店新味",
                "store": {"name": "巷口小馆", "category": "川湘菜"},
                "style": {"name": "美团餐饮团购", "typography_profile": "warm_handwritten_kai"},
                "assets": {"qr_code_not_needed": True, "food_image_not_needed": True},
            }
        )
        prompt = material_skill.build_scene_prompt(
            req,
            {"template_id": "unit", "variant": "urban_chic", "scene_prompt": "sleek room"},
            {"id": "layout_a", "name": "Unit Layout"},
            1080,
            1440,
        )
        prompt_json = json.loads(prompt)

        self.assertEqual(prompt_json["style"]["typography_profile"]["id"], "warm_handwritten_kai")

    def test_scene_prompt_uses_design_style_without_legacy_variant_terms(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "3:4"},
                "title": "七夕套餐",
                "store": {"name": "城市西餐厅", "category": "西餐"},
                "campaign": {"theme": "七夕", "offer": "双人套餐"},
                "scene": {"business_intent": "festival"},
                "style": {"name": "美团餐饮团购"},
                "assets": {"qr_code_not_needed": True, "food_image_not_needed": True},
            }
        )
        style = next(item for item in material_skill.load_styles("营销海报") if item["style_id"] == "illustration_flat")
        prompt = material_skill.build_scene_prompt(
            req,
            style,
            {"id": "layout", "name": "Layout"},
            1080,
            1440,
        )
        prompt_json = json.loads(prompt)
        prompt_text = json.dumps(prompt_json, ensure_ascii=False)

        self.assertEqual(prompt_json["style"]["style_id"], "illustration_flat")
        self.assertEqual(prompt_json["business_intent"]["primary"], "festival")
        self.assertIn("flat vector", prompt_text)
        self.assertIn("solid color", prompt_text)
        self.assertIn("no photography", prompt_text)
        self.assertNotIn("variant", prompt_json["style"])
        self.assertNotIn("mood_group", prompt_text)
        self.assertNotIn("visual_atmospheres", prompt_text)
        for brand_name in ("海底捞", "凑凑", "丹江渔村", "麻六记", "峨眉酒家", "潇湘府", "天宝兄弟"):
            self.assertNotIn(brand_name, prompt_text)

    def test_collage_pop_prompt_contains_specific_graphic_anchors(self):
        req, _ = material_skill.normalize_request(
            {
                "type": "营销海报",
                "size": {"preset": "3:4"},
                "title": "周末团购优惠",
                "store": {"name": "活力小馆", "category": "快餐"},
                "scene": {"business_intent": "social_spread"},
                "style": {"name": "美团餐饮团购"},
                "assets": {"qr_code_not_needed": True, "food_image_not_needed": True},
            }
        )
        style = next(item for item in material_skill.load_styles("营销海报") if item["style_id"] == "collage_pop")
        prompt = material_skill.build_scene_prompt(
            req,
            style,
            {"id": "layout", "name": "Layout"},
            1080,
            1440,
        )
        prompt_text = json.dumps(json.loads(prompt), ensure_ascii=False)

        for anchor in ("paper cut edges", "stickers", "tape strips", "halftone dots"):
            self.assertIn(anchor, prompt_text)

    def test_scene_prompt_requires_qr_host_to_be_front_facing_and_axis_aligned(self):
        qr_path = Path(self.tmp.name) / "qr.png"
        qr_path.write_bytes(PNG_BYTES)
        raw_req = {
            "type": "台卡",
            "size": {"preset": "3:4"},
            "title": "扫码点餐",
            "store": {"name": "山城热辣火锅", "category": "火锅"},
            "campaign": {"offer": "扫码下单 更优惠", "cta": "立即扫码"},
            "products": [],
            "style": {"name": "美团餐饮团购"},
            "assets": {
                "qr_code_path": str(qr_path),
                "food_image_not_needed": True,
                "mascot_mode": "auto",
            },
        }
        req, _ = material_skill.normalize_request(raw_req)
        template = {
            "template_id": "unit",
            "variant": "qr_conversion",
            "scene_prompt": "conversion scene",
            "negative": "no QR code, no fake QR",
        }
        layout = {"id": "layout_c", "name": "Unit Layout"}

        prompt = material_skill.build_scene_prompt(req, template, layout, 1080, 1440)
        prompt_json = json.loads(prompt)
        prompt_text = json.dumps(prompt_json, ensure_ascii=False)

        self.assertIn("正对镜头", prompt_text)
        self.assertIn("轴对齐", prompt_text)
        self.assertIn("0°", prompt_text)
        self.assertIn("无倾斜", prompt_text)
        self.assertIn("无透视", prompt_text)
        self.assertIn("no tilted QR hosting area", prompt_json["negative"])
        self.assertIn("no rotated QR placeholder", prompt_json["negative"])
        self.assertIn("no perspective-skewed QR container", prompt_json["negative"])

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_brand_logo_is_overlaid_from_local_asset(self):
        poster_path = Path(self.tmp.name) / "poster.png"
        out_path = Path(self.tmp.name) / "poster_with_logo.png"
        Image.new("RGBA", (640, 900), (20, 40, 70, 255)).save(poster_path)

        result = material_skill.overlay_brand_logo(
            poster_path,
            material_skill.MEITUAN_LOGO,
            out_path,
        )

        self.assertEqual(result, out_path)
        self.assertTrue(out_path.exists())
        before = Image.open(poster_path).convert("RGBA")
        after = Image.open(out_path).convert("RGBA")
        diff_bbox = ImageChops.difference(before, after).getbbox()
        self.assertIsNotNone(diff_bbox)

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_artistic_logo_mid_luminance_gets_outline_protection(self):
        poster_path = Path(self.tmp.name) / "flat_artistic.png"
        out_path = Path(self.tmp.name) / "flat_artistic_logo.png"
        Image.new("RGBA", (640, 900), (145, 145, 145, 255)).save(poster_path)

        result = material_skill.overlay_brand_logo(
            poster_path,
            material_skill.MEITUAN_LOGO,
            out_path,
            white_logo_path=material_skill.MEITUAN_WHITE_LOGO,
            realism="artistic",
        )

        self.assertEqual(result, out_path)
        self.assertTrue(out_path.exists())
        with Image.open(out_path).convert("RGBA") as image:
            bright_pixels = 0
            for pixel in image.crop((20, 20, 260, 140)).getdata():
                r, g, b, a = pixel
                if a and r > 245 and g > 245 and b > 245:
                    bright_pixels += 1
        self.assertGreater(bright_pixels, 20)

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_brand_visual_lock_controls_logo_position_and_size(self):
        poster_path = Path(self.tmp.name) / "locked_brand.png"
        out_path = Path(self.tmp.name) / "locked_brand_logo.png"
        Image.new("RGBA", (640, 900), (245, 245, 245, 255)).save(poster_path)

        material_skill.overlay_brand_logo(
            poster_path,
            material_skill.MEITUAN_LOGO,
            out_path,
            logo_position="bottom-right",
            logo_size_ratio=0.16,
        )

        before = Image.open(poster_path).convert("RGBA")
        after = Image.open(out_path).convert("RGBA")
        diff_bbox = ImageChops.difference(before, after).getbbox()
        self.assertIsNotNone(diff_bbox)
        assert diff_bbox is not None
        self.assertGreater(diff_bbox[0], 360)
        self.assertGreater(diff_bbox[1], 760)

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_qr_scoring_keeps_exact_model_white_slot_as_candidate(self):
        poster_path = Path(self.tmp.name) / "poster_slot.png"
        image = Image.new("RGB", (420, 420), (30, 40, 55))
        for y in range(173, 273):
            for x in range(251, 351):
                image.putpixel((x, y), (255, 255, 255))
        image.save(poster_path)

        candidates = material_skill.score_qr_candidates(
            poster_path,
            420,
            420,
            100,
            model_hint={
                "x_px": 251,
                "y_px": 173,
                "size_px": 100,
                "canvas_width": 420,
                "canvas_height": 420,
            },
            grid_step=90,
            safety_margin=10,
        )

        self.assertEqual(candidates[0]["x_px"], 251)
        self.assertEqual(candidates[0]["y_px"], 173)
        self.assertEqual(candidates[0]["_source"], "model_hint_scored")

    @unittest.skipIf(Image is None or ImageDraw is None, "Pillow is not installed")
    def test_artistic_small_regular_framed_slot_is_detected_with_relaxed_area(self):
        poster_path = Path(self.tmp.name) / "artistic_regular_slot.png"
        image = Image.new("RGB", (1086, 1448), (10, 14, 24))
        draw = ImageDraw.Draw(image)
        draw.rectangle((768, 998, 998, 1228), fill=(232, 62, 140))
        draw.rectangle((776, 1006, 990, 1221), fill=(255, 255, 255))
        image.save(poster_path)

        default_result = qr_enhance.detect_qr_slot(poster_path, min_slot_size=172)
        artistic_result = qr_enhance.detect_qr_slot(
            poster_path,
            min_slot_size=172,
            min_area_ratio=0.015,
            prefer_framed_slot=True,
        )

        self.assertEqual(default_result["status"], "no_slot")
        self.assertEqual(artistic_result["status"], "detected_slot")
        self.assertEqual(artistic_result["border_kind"], "regular_frame")
        self.assertGreaterEqual(artistic_result["candidate_area_ratio"], 0.02)
        self.assertLessEqual(abs(artistic_result["host_rect"]["x"] - 776), 2)
        self.assertLessEqual(abs(artistic_result["host_rect"]["y"] - 1006), 2)

    @unittest.skipIf(Image is None or ImageDraw is None, "Pillow is not installed")
    def test_irregular_decorative_border_does_not_receive_regular_frame_bonus(self):
        poster_path = Path(self.tmp.name) / "decorative_slot.png"
        image = Image.new("RGB", (1086, 1448), (10, 14, 24))
        draw = ImageDraw.Draw(image)
        draw.rectangle((760, 998, 995, 1014), fill=(232, 62, 140))
        draw.rectangle((760, 998, 776, 1230), fill=(232, 62, 140))
        draw.rectangle((776, 1006, 990, 1221), fill=(255, 255, 255))
        for offset in range(0, 210, 24):
            draw.rectangle((990, 1010 + offset, 1018, 1020 + offset), fill=(29, 183, 201))
        image.save(poster_path)

        result = qr_enhance.detect_qr_slot(
            poster_path,
            min_slot_size=172,
            min_area_ratio=0.015,
            prefer_framed_slot=True,
        )

        self.assertEqual(result["status"], "soft_slot")
        self.assertEqual(result["border_kind"], "decorative_frame")
        self.assertLess(result["slot_score"], 0.75)

    def test_artistic_regular_frame_uses_lower_high_confidence_threshold(self):
        slot_info = {
            "status": "detected_slot",
            "slot_score": 0.78,
            "border_kind": "regular_frame",
            "inner_rect": {"x": 776, "y": 1006, "w": 215, "h": 216},
            "aspect_ratio": 1.0,
            "rotation_deg": 0.0,
            "slot_luminance": 255.0,
        }

        fit_mode, decision_path = material_skill._decide_qr_slot_fit_mode(
            slot_info,
            model_hint=None,
            width=1086,
            height=1448,
            realism="artistic",
        )

        self.assertEqual(fit_mode, "detected_slot")
        self.assertEqual(decision_path, "artistic_slot_high_confidence")

    def test_artistic_non_regular_frame_still_requires_cross_validation(self):
        slot_info = {
            "status": "detected_slot",
            "slot_score": 0.78,
            "border_kind": "none",
            "inner_rect": {"x": 776, "y": 1006, "w": 215, "h": 216},
            "aspect_ratio": 1.0,
            "rotation_deg": 0.0,
            "slot_luminance": 255.0,
        }

        fit_mode, decision_path = material_skill._decide_qr_slot_fit_mode(
            slot_info,
            model_hint=None,
            width=1086,
            height=1448,
            realism="artistic",
        )

        self.assertEqual(fit_mode, "fallback_card")
        self.assertEqual(decision_path, "artistic_fallback")

    @unittest.skipIf(Image is None, "Pillow is not installed")
    def test_qr_alignment_check_reports_warning_when_qr_misses_slot(self):
        poster_path = Path(self.tmp.name) / "alignment.png"
        image = Image.new("RGB", (500, 500), (20, 20, 20))
        for y in range(150, 350):
            for x in range(150, 350):
                image.putpixel((x, y), (255, 255, 255))
        image.save(poster_path)
        inner_rect = {"x": 150, "y": 150, "w": 200, "h": 200}

        ok = material_skill._qr_alignment_check(
            poster_path,
            inner_rect,
            {"x": 180, "y": 180, "w": 140, "h": 140},
        )
        warning = material_skill._qr_alignment_check(
            poster_path,
            inner_rect,
            {"x": 20, "y": 20, "w": 140, "h": 140},
        )

        self.assertEqual(ok["alignment_status"], "ok")
        self.assertEqual(warning["alignment_status"], "warning")
        self.assertLess(warning["alignment_check"]["slot_coverage_ratio"], 0.5)

    def test_http_error_body_redacts_proxy_token_before_writing_raw_response(self):
        def fake_urlopen(request, timeout):
            raise urllib.error.HTTPError(
                request.full_url,
                401,
                "Unauthorized",
                hdrs=None,
                fp=FakeHTTPResponse({"error": {"message": f"invalid token: {material_skill.PROXY_TOKEN}"}}),
            )

        with mock.patch.object(material_skill.urllib.request, "urlopen", side_effect=fake_urlopen):
            with self.assertRaises(material_skill.SkillError) as context:
                material_skill.call_image_api("make poster", [], self.out_path, self.raw_path)

        self.assertNotIn(material_skill.PROXY_TOKEN, self.raw_path.read_text(encoding="utf-8"))
        self.assertNotIn(material_skill.PROXY_TOKEN, str(context.exception))
        self.assertIn("<redacted>", self.raw_path.read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
