#!/usr/bin/env python3
"""Generate a hand-quality SVG hero figure showing the Nillkin Cube Pocket
keyboard layout before and after JIS/macOS remapping.

Stdlib only. Writes a single deterministic, indented SVG file.
"""

import os
from pathlib import Path

OUT_PATH = Path(__file__).parent / "keymap-before-after.svg"

# ---------------------------------------------------------------------------
# Geometry constants
# ---------------------------------------------------------------------------

U = 62          # unit width in px
GAP = 6         # gap between keys in px
KEY_H = 56      # key height in px
KEY_RX = 7      # key corner radius
PAD = 16        # card inner padding
ROW_STEP = KEY_H + GAP  # 62
ROWS_COUNT = 5

FONT_FAMILY = (
    '-apple-system, "Helvetica Neue", "Hiragino Sans", '
    '"Noto Sans CJK JP", Arial, sans-serif'
)

# Row data: list of rows, each row is a list of (legend, width_in_units).
ROWS = [
    [("Esc", 1), ("1", 1), ("2", 1), ("3", 1), ("4", 1), ("5", 1), ("6", 1),
     ("7", 1), ("8", 1), ("9", 1), ("0", 1), ("-", 1), ("=", 1),
     ("Delete", 1.5)],
    [("tab", 1.5), ("Q", 1), ("W", 1), ("E", 1), ("R", 1), ("T", 1),
     ("Y", 1), ("U", 1), ("I", 1), ("O", 1), ("P", 1), ("[", 1), ("]", 1),
     ("\\", 1)],
    [("caps lock", 1.75), ("A", 1), ("S", 1), ("D", 1), ("F", 1), ("G", 1),
     ("H", 1), ("J", 1), ("K", 1), ("L", 1), (";", 1), ("'", 1),
     ("enter", 1.75)],
    [("shift", 2.25), ("Z", 1), ("X", 1), ("C", 1), ("V", 1), ("B", 1),
     ("N", 1), ("M", 1), (",", 1), (".", 1), ("/", 1), ("shift", 2.25)],
    [("control", 1.5), ("fn", 1), ("option", 1.125), ("command", 1.125),
     ("", 4.25), ("command", 1.25), ("option", 1.25), ("←", 1),
     ("↑↓", 1), ("→", 1)],
]

# Legends that are rendered as words (smaller font) rather than single
# glyphs/symbols.
WORD_LEGENDS = {
    "Esc", "Delete", "tab", "caps lock", "enter", "shift", "control",
    "fn", "option", "command",
}

WORD_FONT_SIZE = 12
GLYPH_FONT_SIZE = 17

# The six physical keys that get remapped, keyed by (row_index, key_index).
# "after" is a tuple of 1 or 2 lines for the AFTER panel's new legend.
# "was" is the small annotation shown in the AFTER panel.
REMAPS = {
    (2, 0): {"after": ("control",), "was": "was caps lock"},
    (4, 0): {"after": ("option",), "was": "was control"},
    (4, 2): {"after": ("command",), "was": "was option"},
    (4, 3): {"after": ("英数", "LANG2"), "was": "was command"},
    (4, 5): {"after": ("かな", "LANG1"), "was": "was command"},
    (4, 6): {"after": ("command",), "was": "was option"},
}

# ---------------------------------------------------------------------------
# Fn-layer strip: small teal annotations drawn under the bottom key row to
# show that fn-combos follow the emitted HID usage (the key's OUTPUT), not
# the physical key, so they move after remapping.
# ---------------------------------------------------------------------------

STRIP_H = 30        # extra card height reserved for the fn-layer strip
TICK_LEN = 5         # connector tick length, key bottom edge -> annotation
FN_COLOR = "#7fd6c8"
FN_FONT_SIZE = 10
ICON_W = 20          # nominal icon bounding-box width  (~20x13 px)
ICON_H = 13
FN_LABEL_W = 14      # approx rendered width of "fn+" at FN_FONT_SIZE
ANNOT_GAP = 3        # gap between the "fn+" label and the icon

# Placement table: which physical (row, key) index gets which icon under
# the strip, per panel. Trivial to change later.
STRIP_PLACEMENTS = {
    "before": [
        {"key": (4, 3), "icon": "toggle", "unverified": False},  # left command
        {"key": (4, 5), "icon": "battery", "unverified": False},  # right command
        {"key": (4, 0), "icon": "osk", "unverified": False},  # control
    ],
    "after": [
        # Fn-combos follow the *output*, so after remapping they sit under
        # the physical keys that now emit command (former left/right option).
        {"key": (4, 2), "icon": "toggle", "unverified": False},  # was option (L)
        {"key": (4, 6), "icon": "battery", "unverified": False},  # was option (R)
    ],
}

# The AFTER-panel osk annotation is not part of the bottom-row strip (caps
# lock is row 2), so it is drawn inside the remapped caps-lock key itself.
CAPSLOCK_FN_COLOR = "#b8f3ea"       # light teal: key already has a teal fill
CAPSLOCK_FN_FONT_SIZE = 8.5
CAPSLOCK_Q_FONT_SIZE = 9
CAPSLOCK_Q_COLOR = "#f0d48a"
CAPSLOCK_FN_LABEL_W = 12
CAPSLOCK_Q_W = 5
CAPSLOCK_GAP = 1

CAPSLOCK_ANNOTATIONS = {
    "after": {"key": (2, 0), "icon": "osk", "unverified": False},
}

# Derived block geometry (identical for both panels).
ROW_WIDTH_UNITS = sum(w for _, w in ROWS[0])  # 14.5u for every row
MAIN_BLOCK_W = ROW_WIDTH_UNITS * U - GAP       # visual right edge of keys
TOUCHPAD_W = 3.4 * U
BLOCK_GAP = 10
ROWS_AREA_H = ROWS_COUNT * ROW_STEP - GAP

CARD_CONTENT_W = MAIN_BLOCK_W + BLOCK_GAP + TOUCHPAD_W
CARD_W = CARD_CONTENT_W + 2 * PAD
CARD_H = ROWS_AREA_H + 2 * PAD + STRIP_H

CANVAS_W = 1240
CARD_X = (CANVAS_W - CARD_W) / 2

TITLE1_Y = 30
CARD1_Y = 46
TITLE2_Y = CARD1_Y + CARD_H + 48
CARD2_Y = TITLE2_Y + 16
LEGEND_Y = CARD2_Y + CARD_H + 42
LEGEND_Y2 = LEGEND_Y + 24  # second legend line (fn-combo note)

CANVAS_H = LEGEND_Y2 + 36

TITLE_BEFORE = "BEFORE — factory / GLOBAL firmware (US layout)"
TITLE_AFTER = "AFTER — configs/jp-lang.toml (JP_LANG firmware)"

DESC_TEXT = (
    "Nillkin Cube Pocket physical key remapping: caps lock becomes control; "
    "left control becomes option; left option becomes command; left "
    "command becomes 英数 (eisu / LANG2); right command becomes "
    "かな (kana / LANG1); right option becomes command. Fn combinations "
    "follow the emitted usage: fn+Command (left) toggles touchpad/numpad "
    "and fn+Command (right) shows battery level, so after remapping they "
    "are on the physical option keys."
)


def esc(text):
    """Escape XML special characters for use in text content/attributes."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )


def fmt(value):
    """Format a number compactly and deterministically."""
    rounded = round(value, 2)
    if rounded == int(rounded):
        return str(int(rounded))
    return ("%.2f" % rounded).rstrip("0").rstrip(".")


def baseline_y(key_y, font_size):
    """Vertical-centering baseline offset for a font size within a key."""
    return key_y + KEY_H / 2 + font_size * 0.35


class SvgBuilder:
    def __init__(self):
        self.lines = []

    def add(self, indent, text):
        self.lines.append(("  " * indent) + text)

    def render(self):
        return "\n".join(self.lines) + "\n"


# ---------------------------------------------------------------------------
# Fn-layer icons. Each function draws its icon inside a nominal
# ICON_W x ICON_H bounding box whose top-left corner is (x, y), using the
# given color for strokes and filled details.
# ---------------------------------------------------------------------------

def icon_toggle(x, y, color):
    """Touchpad / numpad toggle: portrait rect + '/' + rect with a dot grid."""
    lines = []
    r1_y = y + 0.5
    lines.append(
        '<rect x="%s" y="%s" width="7" height="12" rx="1.5" fill="none" '
        'stroke="%s" stroke-width="1.3"/>' % (fmt(x), fmt(r1_y), color)
    )
    lines.append(
        '<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" '
        'stroke-width="1.3"/>'
        % (fmt(x + 8.5), fmt(y + 11.5), fmt(x + 11.5), fmt(y + 1), color)
    )
    r2_x = x + 11
    r2_y = y + 0.5
    lines.append(
        '<rect x="%s" y="%s" width="9" height="12" rx="1.5" fill="none" '
        'stroke="%s" stroke-width="1.3"/>' % (fmt(r2_x), fmt(r2_y), color)
    )
    margin = 1.6
    inner_w = 9 - 2 * margin
    inner_h = 12 - 2 * margin
    row_gap = inner_h / 2
    dot = 1.4
    for j in range(3):
        dy = r2_y + margin + j * row_gap
        for i in range(2):
            dx = r2_x + margin + i * inner_w
            lines.append(
                '<rect x="%s" y="%s" width="%s" height="%s" fill="%s"/>'
                % (fmt(dx - dot / 2), fmt(dy - dot / 2), fmt(dot), fmt(dot), color)
            )
    return lines


def icon_battery(x, y, color):
    """Battery level: rounded body + terminal nub + filled charge bars."""
    lines = []
    body_y = y + 2
    lines.append(
        '<rect x="%s" y="%s" width="16" height="9" rx="2" fill="none" '
        'stroke="%s" stroke-width="1.3"/>' % (fmt(x), fmt(body_y), color)
    )
    lines.append(
        '<rect x="%s" y="%s" width="2" height="4" rx="0.6" fill="%s"/>'
        % (fmt(x + 16), fmt(body_y + 2.5), color)
    )
    bar_w, gap, bar_h = 3.2, 1.2, 5
    bar_y = body_y + 2
    for i in range(3):
        bx = x + 1.5 + i * (bar_w + gap)
        lines.append(
            '<rect x="%s" y="%s" width="%s" height="%s" fill="%s"/>'
            % (fmt(bx), fmt(bar_y), fmt(bar_w), fmt(bar_h), color)
        )
    return lines


def icon_osk(x, y, color):
    """On-screen keyboard: rounded rect, two dot rows, a spacebar row."""
    lines = []
    rect_y = y + 1
    lines.append(
        '<rect x="%s" y="%s" width="18" height="11" rx="2" fill="none" '
        'stroke="%s" stroke-width="1.3"/>' % (fmt(x), fmt(rect_y), color)
    )
    margin = 1.6
    inner_w = 18 - 2 * margin
    col_gap = inner_w / 3
    dot = 1.2
    row1_y = rect_y + 2.6
    row2_y = rect_y + 5.6
    space_y = rect_y + 8.6
    for ry in (row1_y, row2_y):
        for i in range(4):
            dx = x + margin + i * col_gap
            lines.append(
                '<rect x="%s" y="%s" width="%s" height="%s" fill="%s"/>'
                % (fmt(dx - dot / 2), fmt(ry - dot / 2), fmt(dot), fmt(dot), color)
            )
    lines.append(
        '<rect x="%s" y="%s" width="10" height="1.4" rx="0.7" fill="%s"/>'
        % (fmt(x + 4), fmt(space_y - 0.7), color)
    )
    return lines


ICON_FUNCS = {"toggle": icon_toggle, "battery": icon_battery, "osk": icon_osk}


def draw_fn_strip_annotation(svg, indent, cx, row_bottom_y, icon_name):
    """Draw one 'fn+' + icon annotation, centered at cx, below row_bottom_y."""
    tick_y2 = row_bottom_y + TICK_LEN
    svg.add(
        indent,
        '<line x1="%s" y1="%s" x2="%s" y2="%s" stroke="%s" '
        'stroke-width="1" opacity=".6"/>'
        % (fmt(cx), fmt(row_bottom_y), fmt(cx), fmt(tick_y2), FN_COLOR),
    )
    icon_top_y = tick_y2
    row_center_y = icon_top_y + ICON_H / 2
    group_w = FN_LABEL_W + ANNOT_GAP + ICON_W
    start_x = cx - group_w / 2
    text_baseline_y = row_center_y + FN_FONT_SIZE * 0.35
    svg.add(
        indent,
        '<text x="%s" y="%s" text-anchor="start" font-size="%s" '
        'fill="%s">fn+</text>'
        % (fmt(start_x), fmt(text_baseline_y), FN_FONT_SIZE, FN_COLOR),
    )
    icon_x = start_x + FN_LABEL_W + ANNOT_GAP
    for line in ICON_FUNCS[icon_name](icon_x, icon_top_y, FN_COLOR):
        svg.add(indent, line)


def draw_capslock_annotation(svg, indent, key_x, key_y, key_w, entry):
    """Draw the fn+osk(+?) annotation inside the remapped caps-lock key."""
    total_w = (
        CAPSLOCK_FN_LABEL_W + CAPSLOCK_GAP + ICON_W + CAPSLOCK_GAP + CAPSLOCK_Q_W
    )
    start_x = key_x + key_w - 4 - total_w
    icon_top_y = key_y + 14
    row_center_y = icon_top_y + ICON_H / 2
    baseline_y_ = row_center_y + CAPSLOCK_FN_FONT_SIZE * 0.35

    svg.add(
        indent,
        '<text x="%s" y="%s" text-anchor="start" font-size="%s" '
        'fill="%s">fn+</text>'
        % (fmt(start_x), fmt(baseline_y_), CAPSLOCK_FN_FONT_SIZE, CAPSLOCK_FN_COLOR),
    )
    icon_x = start_x + CAPSLOCK_FN_LABEL_W + CAPSLOCK_GAP
    for line in ICON_FUNCS[entry["icon"]](icon_x, icon_top_y, CAPSLOCK_FN_COLOR):
        svg.add(indent, line)

    if entry.get("unverified"):
        q_x = icon_x + ICON_W + CAPSLOCK_GAP
        svg.add(
            indent,
            '<text x="%s" y="%s" text-anchor="start" font-size="%s" '
            'fill="%s">?</text>'
            % (fmt(q_x), fmt(baseline_y_), CAPSLOCK_Q_FONT_SIZE, CAPSLOCK_Q_COLOR),
        )


def build_keyboard_panel(svg, indent, card_x, card_y, mode):
    """Draw one keyboard card (mode is 'before' or 'after')."""

    svg.add(indent, '<g>')

    # Card body.
    svg.add(
        indent + 1,
        '<rect class="card" x="%s" y="%s" width="%s" height="%s" '
        'rx="18"/>' % (fmt(card_x), fmt(card_y), fmt(CARD_W), fmt(CARD_H)),
    )

    main_x0 = card_x + PAD
    main_y0 = card_y + PAD

    # Fold-hinge lines (decorative, subtle).
    # Hinge 1: boundary after the 'V' key in row 3.
    row3 = ROWS[3]
    cum = 0.0
    hinge1_units = None
    for legend, w in row3:
        cum += w
        if legend == "V":
            hinge1_units = cum
            break
    hinge1_x = main_x0 + hinge1_units * U
    svg.add(
        indent + 1,
        '<line class="hinge" x1="%s" y1="%s" x2="%s" y2="%s"/>'
        % (fmt(hinge1_x), fmt(main_y0), fmt(hinge1_x), fmt(main_y0 + ROWS_AREA_H)),
    )

    # Hinge 2: gap between main block and touchpad.
    hinge2_x = main_x0 + MAIN_BLOCK_W + BLOCK_GAP / 2
    svg.add(
        indent + 1,
        '<line class="hinge" x1="%s" y1="%s" x2="%s" y2="%s"/>'
        % (fmt(hinge2_x), fmt(main_y0), fmt(hinge2_x), fmt(main_y0 + ROWS_AREA_H)),
    )

    # Keys.
    key_geom = {}
    for row_idx, row in enumerate(ROWS):
        x_cursor = 0.0
        key_y = main_y0 + row_idx * ROW_STEP
        for key_idx, (legend, w) in enumerate(row):
            key_x = main_x0 + x_cursor
            key_w = w * U - GAP
            x_cursor += w * U
            key_geom[(row_idx, key_idx)] = (key_x, key_y, key_w)

            remap = REMAPS.get((row_idx, key_idx))

            key_class = "key"
            if remap is not None:
                key_class += " key-remap-before" if mode == "before" else " key-remap-after"

            svg.add(
                indent + 1,
                '<rect class="%s" x="%s" y="%s" width="%s" height="%s" '
                'rx="%s"/>'
                % (key_class, fmt(key_x), fmt(key_y), fmt(key_w), fmt(KEY_H), fmt(KEY_RX)),
            )

            if legend == "" and remap is None:
                continue  # spacebar: no legend

            cx = key_x + key_w / 2

            if remap is not None and mode == "after":
                # "was ..." annotation, top-left.
                svg.add(
                    indent + 1,
                    '<text class="legend legend-was" x="%s" y="%s" '
                    'text-anchor="start" font-size="8.5">%s</text>'
                    % (fmt(key_x + 4), fmt(key_y + 11), esc(remap["was"])),
                )

                after_lines = remap["after"]
                if len(after_lines) == 1:
                    fsize = WORD_FONT_SIZE
                    y = key_y + 40
                    svg.add(
                        indent + 1,
                        '<text class="legend legend-remap-after" x="%s" y="%s" '
                        'text-anchor="middle" font-size="%s">%s</text>'
                        % (fmt(cx), fmt(y), fsize, esc(after_lines[0])),
                    )
                else:
                    line1, line2 = after_lines
                    svg.add(
                        indent + 1,
                        '<text class="legend legend-remap-after" x="%s" y="%s" '
                        'text-anchor="middle" font-size="15">%s</text>'
                        % (fmt(cx), fmt(key_y + 34), esc(line1)),
                    )
                    svg.add(
                        indent + 1,
                        '<text class="legend legend-sub" x="%s" y="%s" '
                        'text-anchor="middle" font-size="9">%s</text>'
                        % (fmt(cx), fmt(key_y + 48), esc(line2)),
                    )
                continue

            # Normal legend rendering (BEFORE panel, or non-remapped keys).
            is_word = legend in WORD_LEGENDS
            fsize = WORD_FONT_SIZE if is_word else GLYPH_FONT_SIZE
            y = baseline_y(key_y, fsize)

            if remap is not None and mode == "before":
                text_class = "legend legend-remap-before"
            else:
                text_class = "legend legend-dim"

            svg.add(
                indent + 1,
                '<text class="%s" x="%s" y="%s" text-anchor="middle" '
                'font-size="%s">%s</text>'
                % (text_class, fmt(cx), fmt(y), fsize, esc(legend)),
            )

    # Touchpad / numpad area.
    tp_x = main_x0 + MAIN_BLOCK_W + BLOCK_GAP
    tp_y = main_y0
    svg.add(
        indent + 1,
        '<rect class="touchpad" x="%s" y="%s" width="%s" height="%s" '
        'rx="10"/>' % (fmt(tp_x), fmt(tp_y), fmt(TOUCHPAD_W), fmt(ROWS_AREA_H)),
    )
    svg.add(
        indent + 1,
        '<text class="touchpad-label" x="%s" y="%s" text-anchor="middle">'
        'touchpad / numpad</text>'
        % (fmt(tp_x + TOUCHPAD_W / 2), fmt(tp_y + ROWS_AREA_H / 2 + 4)),
    )

    # Fn-layer strip: small "fn+<icon>" annotations under the bottom row.
    row_bottom_y = main_y0 + ROWS_AREA_H
    for placement in STRIP_PLACEMENTS.get(mode, []):
        key_x, key_y, key_w = key_geom[placement["key"]]
        cx = key_x + key_w / 2
        draw_fn_strip_annotation(svg, indent + 1, cx, row_bottom_y, placement["icon"])

    # AFTER-panel-only annotation drawn inside a key (caps lock -> control).
    capslock_entry = CAPSLOCK_ANNOTATIONS.get(mode)
    if capslock_entry is not None:
        key_x, key_y, key_w = key_geom[capslock_entry["key"]]
        draw_capslock_annotation(svg, indent + 1, key_x, key_y, key_w, capslock_entry)

    svg.add(indent, '</g>')


def build_svg():
    svg = SvgBuilder()

    svg.add(
        0,
        '<svg xmlns="http://www.w3.org/2000/svg" role="img" '
        'viewBox="0 0 %s %s" width="%s" height="%s">'
        % (fmt(CANVAS_W), fmt(CANVAS_H), fmt(CANVAS_W), fmt(CANVAS_H)),
    )

    svg.add(1, "<title>Nillkin Cube Pocket key layout before and after remapping</title>")
    svg.add(1, "<desc>%s</desc>" % esc(DESC_TEXT))

    svg.add(1, "<style>")
    svg.add(
        2,
        'text { font-family: %s; }' % FONT_FAMILY,
    )
    svg.add(2, '.panel-title { fill: #8b949e; font-weight: 600; font-size: 22px; }')
    svg.add(2, '.legend-line-text { fill: #8b949e; font-size: 15px; }')
    svg.add(2, '.card { fill: #17181b; stroke: #3a3c42; stroke-width: 1.5; }')
    svg.add(2, '.key { fill: #26282d; stroke: #34363c; stroke-width: 1; }')
    svg.add(2, '.key-remap-before { stroke: #e3b341; stroke-width: 2.5; }')
    svg.add(2, '.key-remap-after { fill: #1f6f66; stroke: #5eead4; stroke-width: 2; }')
    svg.add(2, '.legend { fill: #d7d9de; }')
    svg.add(2, '.legend-dim { fill: #8d9199; }')
    svg.add(2, '.legend-remap-before { fill: #f0d48a; }')
    svg.add(2, '.legend-remap-after { fill: #ffffff; font-weight: 600; }')
    svg.add(2, '.legend-sub { fill: #b8f3ea; }')
    svg.add(2, '.legend-was { fill: #9adfd5; opacity: .85; }')
    svg.add(2, '.touchpad { fill: #1e2024; stroke: #34363c; stroke-width: 1; }')
    svg.add(2, '.touchpad-label { fill: #6e7681; font-size: 12px; }')
    svg.add(2, '.hinge { stroke: #3a3c42; stroke-width: 1; stroke-dasharray: 4,4; }')
    svg.add(2, '.swatch-before { fill: #26282d; stroke: #e3b341; stroke-width: 2; }')
    svg.add(2, '.swatch-after { fill: #1f6f66; stroke: #5eead4; stroke-width: 2; }')
    svg.add(1, "</style>")

    # --- Panel 1: BEFORE ---
    svg.add(
        1,
        '<text class="panel-title" x="%s" y="%s">%s</text>'
        % (fmt(CARD_X), TITLE1_Y, esc(TITLE_BEFORE)),
    )
    build_keyboard_panel(svg, 1, CARD_X, CARD1_Y, "before")

    # --- Panel 2: AFTER ---
    svg.add(
        1,
        '<text class="panel-title" x="%s" y="%s">%s</text>'
        % (fmt(CARD_X), fmt(TITLE2_Y), esc(TITLE_AFTER)),
    )
    build_keyboard_panel(svg, 1, CARD_X, CARD2_Y, "after")

    # --- Legend ---
    sw1_x = CARD_X
    sw2_x = CARD_X + 300
    sw_y = LEGEND_Y - 12
    sw_size = 14

    svg.add(
        1,
        '<rect class="swatch-before" x="%s" y="%s" width="%s" height="%s" '
        'rx="3"/>' % (fmt(sw1_x), fmt(sw_y), sw_size, sw_size),
    )
    svg.add(
        1,
        '<text class="legend-line-text" x="%s" y="%s">keys that will change</text>'
        % (fmt(sw1_x + sw_size + 8), fmt(LEGEND_Y)),
    )

    svg.add(
        1,
        '<rect class="swatch-after" x="%s" y="%s" width="%s" height="%s" '
        'rx="3"/>' % (fmt(sw2_x), fmt(sw_y), sw_size, sw_size),
    )
    svg.add(
        1,
        '<text class="legend-line-text" x="%s" y="%s">remapped output</text>'
        % (fmt(sw2_x + sw_size + 8), fmt(LEGEND_Y)),
    )

    # Third legend entry (second line): fn-combo note.
    svg.add(
        1,
        '<text x="%s" y="%s" font-size="14" fill="%s">fn+</text>'
        % (fmt(sw1_x), fmt(LEGEND_Y2), FN_COLOR),
    )
    svg.add(
        1,
        '<text class="legend-line-text" x="%s" y="%s">fn combos follow the '
        'key&#39;s output, so they move with Command / Control</text>'
        % (fmt(sw1_x + 27), fmt(LEGEND_Y2)),
    )

    svg.add(0, "</svg>")

    return svg.render()


def main():
    content = build_svg()
    os.makedirs(os.path.dirname(OUT_PATH), exist_ok=True)
    with open(OUT_PATH, "w", encoding="utf-8") as f:
        f.write(content)
    print("Wrote %s (%d bytes)" % (OUT_PATH, len(content.encode("utf-8"))))
    print("Canvas: %s x %s" % (fmt(CANVAS_W), fmt(CANVAS_H)))
    print("Card size: %s x %s" % (fmt(CARD_W), fmt(CARD_H)))


if __name__ == "__main__":
    main()
