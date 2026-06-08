# Realistic Food Reference Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make uploaded dish reference images render with realistic, moderately beautified food texture instead of obvious AI/3D food.

**Architecture:** Keep the existing Responses API image generation flow. Add reference-image-specific prompt sections and negative terms in `scripts/material_skill.py`, and update the realism reference copy in `references/style_controls.json`. Validate behavior with prompt-structure unit tests.

**Tech Stack:** Python 3, `unittest`, JSON prompt generation, existing `material_skill.py` prompt builder.

---

### Task 1: Failing Prompt Tests

**Files:**
- Create: `tests/test_realistic_food_reference_prompt.py`

- [ ] **Step 1: Write the failing test**

```python
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
        layout = material_skill.select_layout(req, template, material_skill.PANORAMA_API_WIDTH, material_skill.PANORAMA_API_HEIGHT)

        prompt_json = json.loads(material_skill.build_scene_prompt(req, template, layout, 3840, 1280))
        prompt_text = json.dumps(prompt_json, ensure_ascii=False)

        self.assertIn("参考图保真优先", prompt_text)
        self.assertIn("真实美食摄影感", prompt_text)
        self.assertIn("no 3D-rendered dish", prompt_json["negative"])
        self.assertIn("no toy-like dish", prompt_json["negative"])
        self.assertNotIn("不要凭空重做摆盘", prompt_text)


if __name__ == "__main__":
    unittest.main()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python3 -m unittest discover -s tests -p 'test_realistic_food_reference_prompt.py' -v`
Expected: FAIL because prompt currently lacks the new `参考图保真优先` section and new negative terms.

### Task 2: Prompt Implementation

**Files:**
- Modify: `scripts/material_skill.py`
- Modify: `references/style_controls.json`

- [ ] **Step 1: Add helper functions and prompt sections**

Add helpers near the prompt-building helpers:

```python
FOOD_REFERENCE_NEGATIVE_TERMS = [
    "no AI-rendered food",
    "no 3D-rendered dish",
    "no toy-like dish",
    "no plastic food texture",
    "no over-polished sauce",
    "no waxy ingredients",
    "no fake perfect food surface",
    "no miniature diorama food",
]


def _reference_image_realism_constraint() -> str:
    controls = _style_controls()
    value = controls.get("realism_reference_image_constraint", "") if isinstance(controls, dict) else ""
    return normalize_space(value)


def _food_reference_fidelity_section(ref_count: int) -> dict[str, Any]:
    if ref_count <= 0:
        return {}
    return {
        "priority": "参考图保真优先；此约束高于模板中的 3D、微缩、黏土、塑料、Pixar、diorama 等风格词。",
        "reference_count": ref_count,
        "dish_subject_rule": (
            "菜品主体采用真实美食摄影感。保留参考图的主要器皿、菜品结构、食材色泽、真实材质、光线方向和自然瑕疵；"
            "允许适度商业美化，包括轻度补光、提亮、清理杂乱背景、增强食欲色彩和融入海报构图。"
        ),
        "style_boundary": (
            "模板中的 3D、微缩、黏土、塑料、Pixar、diorama 等描述只能用于背景装饰、氛围道具或版式趣味，不能作用到菜品主体。"
        ),
        "constraint": _reference_image_realism_constraint(),
    }
```

- [ ] **Step 2: Wire helper into normal and panorama prompts**

For normal prompts:
- Add `FOOD_REFERENCE` to `prompt_sections` when `has_food`.
- Replace the `food_rule` text for `has_food` with a more concrete fidelity rule.
- Extend `negative` with `FOOD_REFERENCE_NEGATIVE_TERMS` when `has_food`.

For panorama prompts:
- Extend `negative` with `FOOD_REFERENCE_NEGATIVE_TERMS` when reference images exist.
- Add a `food_reference_fidelity` object or equivalent field.
- Append the same fidelity guidance to `photo_reference_instruction`.

- [ ] **Step 3: Update config copy**

Change `references/style_controls.json.realism_reference_image_constraint` to:

```json
"参考图保真优先：保留食材形态、主要器皿、菜品结构、真实材质、自然瑕疵和原始光线方向；允许适度商业美化，如轻度补光、提亮、清理杂乱背景、增强食欲色彩和重构海报背景。不要把菜品主体处理成 3D、塑料、玩具、蜡质或过度光滑的 AI 渲染质感。"
```

- [ ] **Step 4: Run target test to verify it passes**

Run: `python3 -m unittest discover -s tests -p 'test_realistic_food_reference_prompt.py' -v`
Expected: PASS.

### Task 3: Regression Verification

**Files:**
- No source edits unless tests expose a real regression.

- [ ] **Step 1: Run related existing tests**

Run: `python3 -m unittest discover -s tests -p 'test_template_selector.py' -v && python3 -m unittest discover -s tests -p 'test_responses_provider.py' -v`
Expected: PASS.

- [ ] **Step 2: Inspect git diff**

Run: `git diff -- scripts/material_skill.py references/style_controls.json tests/test_realistic_food_reference_prompt.py`
Expected: Diff only includes realistic reference prompt changes and tests.
