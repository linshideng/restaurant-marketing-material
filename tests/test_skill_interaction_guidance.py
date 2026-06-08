from pathlib import Path
import unittest


SKILL_TEXT = (Path(__file__).resolve().parents[1] / "SKILL.md").read_text(encoding="utf-8")


class SkillInteractionGuidanceTests(unittest.TestCase):
    def test_second_round_guidance_separates_reply_modes(self):
        self.assertIn("你可以这样回复：", SKILL_TEXT)
        self.assertIn("直接生成：回复\"OK\"", SKILL_TEXT)
        self.assertIn("指定候选：回复\"OK 3\"或\"文案用 3\"", SKILL_TEXT)
        self.assertIn("换风格：回复\"换成 A+C，文案用 3\"", SKILL_TEXT)
        self.assertIn("自定义文案：直接发完整文案", SKILL_TEXT)

    def test_direct_custom_copy_reply_is_treated_as_confirmation(self):
        self.assertIn("第 2 轮用户回复解析", SKILL_TEXT)
        self.assertIn("视为自定义展示文案", SKILL_TEXT)
        self.assertIn("九年本土老店、每日新鲜食材、地道家常菜、口碑老店", SKILL_TEXT)
        self.assertIn("不得终止对话", SKILL_TEXT)
        self.assertIn("不得等待用户补充", SKILL_TEXT)

    def test_real_photo_confirmation_template_includes_dish_name_warning(self):
        self.assertIn("实拍图确认模板", SKILL_TEXT)
        self.assertIn("你上传了 N 张图片，我识别如下", SKILL_TEXT)
        self.assertIn("AI 对复杂文字和生僻字可能写错", SKILL_TEXT)
        self.assertIn("菜名尽量用常见、简短写法", SKILL_TEXT)
        self.assertIn("发布前仍需逐字核对", SKILL_TEXT)
        self.assertIn("跳过菜名/不标菜名", SKILL_TEXT)

    def test_style_reference_note_requires_structured_visual_observations(self):
        self.assertIn("风格参考图结构化分析", SKILL_TEXT)
        for dimension in [
            "色调",
            "构图比例",
            "文字风格",
            "菜品/主体呈现方式",
            "背景元素",
            "分隔/留白/节奏方式",
        ]:
            self.assertIn(dimension, SKILL_TEXT)
        self.assertIn("无明显特征", SKILL_TEXT)
        self.assertIn("不得为了凑满 6 类强行描述不存在的元素", SKILL_TEXT)


if __name__ == "__main__":
    unittest.main()
