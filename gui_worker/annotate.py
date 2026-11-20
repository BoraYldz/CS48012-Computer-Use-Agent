# Draw numbered boxes ("set-of-marks") on a copy of the screenshot so the model can match each
# element ID to what it sees instead of correlating JSON rectangles with pixels by itself.
"""Screenshot annotation for model grounding. The raw screenshot stays untouched as evidence."""
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

# Distinct, high-contrast outline colors; neighbouring boxes rarely share a color.
COLORS = ["#e6194b", "#3cb44b", "#4363d8", "#f58231", "#911eb4", "#008080",
          "#f032e6", "#9a6324", "#800000", "#000075"]
# Boxes covering this much of the screen (documents, panes) would hide content; they stay in the JSON.
MAX_AREA_FRACTION = 0.35


# uia_12 is drawn as "12" and vision_3 as "v3"; the prompt explains this short form.
def mark_label(element_id):
    if element_id.startswith("uia_"):
        return element_id[4:]
    if element_id.startswith("vision_"):
        return "v" + element_id[7:]
    return element_id


def _font():
    # Arial ships with Windows; Pillow's built-in font is the portable fallback.
    try:
        return ImageFont.truetype("arial.ttf", 13)
    except OSError:
        return ImageFont.load_default()


# Return the path of an annotated copy; the caller sends it to the model in place of the raw image.
def draw_marks(screenshot_path, elements):
    source = Path(screenshot_path)
    with Image.open(source) as opened:
        image = opened.convert("RGB")
    draw = ImageDraw.Draw(image)
    font = _font()
    screen_area = image.width * image.height
    for index, element in enumerate(elements):
        box = element.get("bbox")
        if not box or len(box) != 4:
            continue
        left, top, right, bottom = (int(round(v)) for v in box)
        if right <= left or bottom <= top or (right - left) * (bottom - top) > MAX_AREA_FRACTION * screen_area:
            continue
        color = COLORS[index % len(COLORS)]
        draw.rectangle((left, top, right, bottom), outline=color, width=2)
        text = mark_label(str(element.get("id", "")))
        text_box = draw.textbbox((0, 0), text, font=font)
        width, height = text_box[2] - text_box[0] + 6, text_box[3] - text_box[1] + 4
        # Put the tag just above the box; inside it when the box touches the top edge.
        tag_top = top - height if top - height >= 0 else top
        tag_left = min(max(left, 0), image.width - width)
        draw.rectangle((tag_left, tag_top, tag_left + width, tag_top + height), fill=color)
        draw.text((tag_left + 3, tag_top + 1 - text_box[1]), text, fill="white", font=font)
    target = source.with_name(source.stem + "_marks.png")
    image.save(target)
    return str(target)
