# 背景配图提示词

Responses API `image_generation` 生成完整 AI 海报，包含 3D 微缩场景、布局、展示文字和 QR 宿主区域。本地 Meituan logo 与 QR 由后合成管线填充。

核心风格约束：

```text
MANDATORY: 3D rendered miniature diorama scene with volumetric lighting, realistic material textures (clay, felt, plastic, wood), depth of field, cinematic warm color grading. Must feel like a photograph of a Pixar-quality handcrafted miniature set, NOT a flat 2D graphic illustration. QR hosting area interior must be a clean flat white square (1:1 aspect, 30-35% of short side), front-facing and axis-aligned with the canvas (0° rotation, no tilt, no perspective skew).
```

通用否定约束：

```text
no QR code, no fake QR, no checkerboard scan pattern, no watermark, no flat 2D illustration, no plain yellow background, no generic clip art, no yellow clipboard QR holder, no tilted QR hosting area, no rotated QR placeholder, no perspective-skewed QR container, no more than 3 mascot figures, no repeated mascot crowd.
```

## 餐饮氛围场景

```text
3D miniature restaurant diorama scene: tiny detailed shop front with awning and warm interior glow, miniature street elements (lanterns, potted plants, cobblestone), floating appetizing food props (steam wisps, sauce drizzle, spice particles), warm volumetric lighting with golden hour ambiance, depth of field blur on background layer. Rich material textures — clay mascot, wooden signage, felt tablecloth. Clean negative space reserved for headline typography and QR hosting area.
```

## 节日/季节氛围

```text
3D miniature seasonal diorama for [season/campaign]: build a themed miniature world with region-specific 3D props (traditional architecture, cultural decorations, seasonal flora). For spring: cherry blossoms, green meadow, butterflies. For summer/Songkran: water splashes, elephants, tropical foliage. For autumn/Mid-Autumn: golden ginkgo leaves, moon, traditional Chinese courtyard. For winter: snow globe effect, warm lanterns, pine trees. Warm cinematic lighting, depth of field, clean QR hosting area integrated as a natural scene prop.
```

## 吉祥物场景

```text
Use the attached Meituan mascot sheet only as character reference. Create ONE main 3D mascot figure actively participating in a themed miniature scene — e.g. wearing a chef hat, riding a vehicle, holding themed props, interacting with cultural elements. The mascot should have clay/plastic material texture with 3D volume and shadow. Up to 2 smaller supporting mascots may appear in the background. Mascots are secondary to headline and QR — they support the story, not dominate the layout. Leave clean space for title text and QR hosting area.
```

## 抽象美食线索

```text
3D miniature food diorama elements: realistic miniature food props with appetizing material texture (glistening sauce, melted cheese pulls, steam wisps, crispy golden crust), arranged as floating scene decorations around the composition. Mix with miniature restaurant props (tiny plates, mini utensils, condiment bottles). Warm volumetric backlighting creates appetite appeal. Do not draw a specific full plated dish unless a real food reference image is provided — use floating ingredient props and abstract food textures instead.
```

## QR 宿主区域造型参考

```text
Design the QR hosting area as a creative 3D prop that matches the campaign theme. Choose from:
- Inflatable balloon / soft pillow shape with gentle shadow (spring, youth, fun themes)
- Wooden signpost / road sign on a decorated pole with 3D wood grain (outdoor, street, adventure themes)
- Crystal ball / snow globe with miniature scene inside the border (magical, winter, premium themes)
- Ornate picture frame / decorative frame held by mascot or on easel (classic, elegant themes)
- Chalkboard menu board on wooden easel with chalk dust texture (food, cafe, restaurant themes)
- Floating translucent bubble with iridescent rim (dreamy, modern themes)
- Gift tag / luggage tag with string/ribbon attachment (festival, travel themes)
The exterior frame must have realistic 3D material texture and shadow. Interior must be pure flat white square. The face that contains the QR interior must be parallel to the image plane: horizontal/vertical edges align with the canvas, 0° rotation, no tilted signboard, no diagonal easel plane, no perspective-skewed container.
```
