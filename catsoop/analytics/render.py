"""
Rendering helpers for the analytics dashboard.

Charts are hand-built inline SVG rather than a charting library, for three
reasons: a CAT-SOOP page should not depend on a CDN being reachable, inline
SVG prints and scales without JavaScript, and the charts needed here are
simple enough that a library would be the larger dependency.

Colour is carried by CSS custom properties so the palette is defined once, in
`STYLE`, and adapts to dark mode in one place.  Series colours were checked for
adequate separation under the common colour-vision deficiencies, and every
chart also distinguishes its series by position or label, never by hue alone.
"""

import html
import time

#: Chart geometry
_W = 720


def esc(text):
    return html.escape(str(text if text is not None else ""))


def pct(value, digits=0):
    if value is None:
        return "—"
    return ("%%.%df%%%%" % digits) % (value * 100)


def num(value, digits=0):
    if value is None:
        return "—"
    return ("%%.%df" % digits) % value


def duration(seconds):
    """Human-readable duration.  Returns '—' for nothing measured."""
    if not seconds:
        return "—"
    seconds = int(seconds)
    if seconds < 60:
        return "%ds" % seconds
    if seconds < 3600:
        return "%dm %02ds" % (seconds // 60, seconds % 60)
    return "%dh %02dm" % (seconds // 3600, (seconds % 3600) // 60)


def ago(ts):
    if not ts:
        return "never"
    delta = time.time() - ts
    if delta < 90:
        return "just now"
    if delta < 5400:
        return "%d min ago" % (delta // 60)
    if delta < 172800:
        return "%d hours ago" % (delta // 3600)
    return "%d days ago" % (delta // 86400)


def short_path(path):
    """`ml101/assignments/hw01` -> `assignments / hw01`."""
    parts = [p for p in str(path).split("/")]
    if len(parts) > 1:
        parts = parts[1:]
    return " / ".join(parts) if parts else "(course root)"


# --------------------------------------------------------------------- style

STYLE = """
<style>
.csa {
  color-scheme: light;
  /* Chart surface and ink.  Categorical slots 1-3 and the status palette are
     the data-viz reference values; the three categorical slots clear the
     all-pairs CVD and normal-vision floors in both modes. */
  --surface-1:  #fcfcfb;
  --panel:      #f4f4f1;
  --border:     #dcdcd6;
  --ink:        #0b0b0b;
  --ink-soft:   #52514e;
  --series-1:   #2a78d6;
  --series-2:   #eb6834;
  --series-3:   #1baf7a;
  --track:      #e4e4de;
  /* Status is fixed, never themed, and never reused as a series colour. */
  --good:       #0ca30c;
  --warning:    #fab219;
  --serious:    #ec835a;
  --critical:   #d03b3b;
  color: var(--ink);
  font-size: 15px;
  line-height: 1.5;
}
@media (prefers-color-scheme: dark) {
  :root:where(:not([data-theme="light"])) .csa {
    color-scheme: dark;
    --surface-1: #1a1a19;
    --panel:     #232321;
    --border:    #3a3a37;
    --ink:       #ffffff;
    --ink-soft:  #c3c2b7;
    --series-1:  #3987e5;
    --series-2:  #d95926;
    --series-3:  #199e70;
    --track:     #333330;
  }
}
:root[data-theme="dark"] .csa {
  color-scheme: dark;
  --surface-1: #1a1a19;
  --panel:     #232321;
  --border:    #3a3a37;
  --ink:       #ffffff;
  --ink-soft:  #c3c2b7;
  --series-1:  #3987e5;
  --series-2:  #d95926;
  --series-3:  #199e70;
  --track:     #333330;
}
.csa .csa-tiles {
  display: grid; gap: 12px; margin: 18px 0 26px 0;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
}
.csa .csa-tile {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 10px; padding: 14px 16px;
}
.csa .csa-tile .v { font-size: 27px; font-weight: 650; letter-spacing: -0.02em; }
.csa .csa-tile .k {
  font-size: 12px; color: var(--ink-soft); text-transform: uppercase;
  letter-spacing: 0.06em; margin-bottom: 4px;
}
.csa .csa-tile .s { font-size: 12.5px; color: var(--ink-soft); margin-top: 3px; }
.csa .csa-panel {
  background: var(--panel); border: 1px solid var(--border);
  border-radius: 10px; padding: 16px 18px; margin: 0 0 22px 0;
}
.csa h3 { margin: 0 0 4px 0; font-size: 17px; font-weight: 640; }
.csa .csa-note { color: var(--ink-soft); font-size: 13px; margin: 0 0 14px 0; }
.csa table { width: 100%; border-collapse: collapse; font-size: 14px; }
.csa th {
  text-align: left; font-size: 11.5px; text-transform: uppercase;
  letter-spacing: 0.06em; color: var(--ink-soft); font-weight: 600;
  padding: 7px 10px; border-bottom: 1px solid var(--border); white-space: nowrap;
}
.csa td { padding: 7px 10px; border-bottom: 1px solid var(--border); }
.csa td.n, .csa th.n { text-align: right; font-variant-numeric: tabular-nums; }
.csa tr:last-child td { border-bottom: none; }
.csa .csa-chip {
  display: inline-block; font-size: 11.5px; padding: 1px 7px; border-radius: 999px;
  border: 1px solid var(--border); color: var(--ink-soft); margin: 1px 3px 1px 0;
  white-space: nowrap;
}
/* Status chips carry an icon and a word; the colour is on the border, and the
   text keeps an ink token, so meaning never rests on hue alone. */
.csa .csa-chip.warn {
  border-color: var(--critical); border-left-width: 3px; color: var(--ink);
}
.csa .csa-status { display: flex; align-items: center; gap: 8px; }
.csa .csa-status .lab {
  font-size: 11.5px; color: var(--ink-soft); min-width: 62px;
}
.csa .csa-bar-track {
  background: var(--track); border-radius: 4px; height: 8px; width: 100%;
  overflow: hidden; min-width: 60px;
}
.csa .csa-bar-fill { height: 100%; border-radius: 4px; }
.csa .csa-banner {
  border: 1px solid var(--border); border-left: 3px solid var(--series-2);
  background: var(--panel); border-radius: 6px; padding: 10px 14px;
  font-size: 13.5px; margin: 0 0 18px 0; color: var(--ink-soft);
}
.csa .csa-banner b { color: var(--ink); }
.csa a.csa-link { color: var(--series-1); }
.csa .csa-scroll { overflow-x: auto; }
.csa svg { display: block; max-width: 100%; height: auto; }
.csa .csa-axis { fill: var(--ink-soft); font-size: 11px; }
.csa .csa-val { fill: var(--ink); font-size: 11px; font-variant-numeric: tabular-nums; }
.csa .csa-grid { stroke: var(--border); stroke-width: 1; }
.csa svg .mark { transition: opacity .12s ease; }
.csa svg .mark:hover { opacity: .78; }
</style>
"""


# -------------------------------------------------------------------- pieces


def tiles(items):
    """`items` is a list of (label, value, sublabel)."""
    out = ['<div class="csa-tiles">']
    for label, value, sub in items:
        out.append(
            '<div class="csa-tile"><div class="k">%s</div>'
            '<div class="v">%s</div><div class="s">%s</div></div>'
            % (esc(label), esc(value), esc(sub))
        )
    out.append("</div>")
    return "".join(out)


def panel(title, note, body):
    return (
        '<div class="csa-panel"><h3>%s</h3><p class="csa-note">%s</p>%s</div>'
        % (esc(title), esc(note), body)
    )


def inline_bar(fraction, color="var(--series-1)"):
    width = max(0.0, min(1.0, fraction or 0.0)) * 100
    return (
        '<div class="csa-bar-track"><div class="csa-bar-fill" '
        'style="width:%.1f%%;background:%s"></div></div>' % (width, color)
    )


#: Difficulty bands.  The colour is the status palette; the word beside it is
#: what actually carries the meaning, so the band survives greyscale, printing
#: and colour-vision deficiency.
_DIFFICULTY_BANDS = (
    (0.50, "critical", "var(--critical)", "\u25b2 hard"),
    (0.25, "serious", "var(--serious)", "\u25b3 moderate"),
    (0.00, "good", "var(--good)", "\u25cb routine"),
)


def status_bar(value, title=None):
    """
    A bar whose fill is a status colour, paired with the band's name.

    Status colour never travels alone here: the label is always rendered, so
    the reader is never asked to decode a hue.
    """
    value = max(0.0, min(1.0, value or 0.0))
    for threshold, _role, color, label in _DIFFICULTY_BANDS:
        if value >= threshold:
            break
    return (
        '<div class="csa-status" title="%s"><span class="lab">%s</span>%s</div>'
        % (esc(title or label), esc(label), inline_bar(value, color))
    )


def grouped_bars(rows, series, value_max=None, row_height=30, label_width=210):
    """
    Horizontal grouped bars.

    `rows` is a list of dicts; `series` is a list of
    `(key, display_name, css_color)`.  Each bar is labelled with its own value
    at the end, so the chart is readable without a legend lookup -- the series
    name is printed inside the first row's bars instead.
    """
    if not rows:
        return '<p class="csa-note">No data yet.</p>'
    n = len(series)
    bar_h = max(7, (row_height - 10) // n)
    height = len(rows) * row_height + 26
    chart_x = label_width
    chart_w = _W - label_width - 58
    vmax = value_max or max(
        (r.get(k) or 0) for r in rows for k, _l, _c in series
    ) or 1

    parts = [
        '<svg viewBox="0 0 %d %d" role="img" aria-label="grouped bar chart">' % (_W, height)
    ]
    # legend
    lx = chart_x
    for _k, label, color in series:
        parts.append(
            '<rect x="%d" y="4" width="9" height="9" rx="2" fill="%s"/>'
            '<text class="csa-axis" x="%d" y="12">%s</text>'
            % (lx, color, lx + 13, esc(label))
        )
        lx += 16 + len(label) * 6.6
    for i, r in enumerate(rows):
        top = 26 + i * row_height
        parts.append(
            '<text class="csa-axis" x="0" y="%d">%s</text>'
            % (top + row_height / 2 + 3, esc(r.get("label", "")))
        )
        for j, (key, _label, color) in enumerate(series):
            v = r.get(key) or 0
            w = (v / vmax) * chart_w if vmax else 0
            y = top + (row_height - bar_h * n - 2 * (n - 1)) / 2 + j * (bar_h + 2)
            parts.append(
                '<rect class="mark" x="%d" y="%.1f" width="%.1f" height="%d" rx="2"'
                ' fill="%s"><title>%s: %s %s</title></rect>'
                % (chart_x, y, max(w, 1.0), bar_h, color,
                   esc(r.get("label", "")), esc(v), esc(_label))
            )
            parts.append(
                '<text class="csa-val" x="%.1f" y="%.1f">%s</text>'
                % (chart_x + max(w, 1.0) + 6, y + bar_h - 1, esc(r.get(key + "_label", v)))
            )
    parts.append("</svg>")
    return "".join(parts)


def column_chart(labels, values, color="var(--series-1)", value_labels=None, height=190):
    """Vertical bars, for distributions where the x axis is ordered."""
    if not values or not any(values):
        return '<p class="csa-note">No data yet.</p>'
    n = len(values)
    left, bottom, top_pad = 34, 30, 16
    chart_w = _W - left - 14
    chart_h = height - bottom - top_pad
    vmax = max(values) or 1
    slot = chart_w / n
    bar_w = min(slot * 0.68, 60)

    parts = ['<svg viewBox="0 0 %d %d" role="img" aria-label="bar chart">' % (_W, height)]
    for frac in (0, 0.5, 1.0):
        y = top_pad + chart_h - frac * chart_h
        parts.append(
            '<line class="csa-grid" x1="%d" y1="%.1f" x2="%d" y2="%.1f"/>'
            % (left, y, left + chart_w, y)
        )
        parts.append(
            '<text class="csa-axis" x="0" y="%.1f">%d</text>'
            % (y + 4, round(vmax * frac))
        )
    for i, v in enumerate(values):
        h = (v / vmax) * chart_h
        x = left + i * slot + (slot - bar_w) / 2
        y = top_pad + chart_h - h
        parts.append(
            '<rect class="mark" x="%.1f" y="%.1f" width="%.1f" height="%.1f" rx="2"'
            ' fill="%s"><title>%s: %s</title></rect>'
            % (x, y, bar_w, max(h, 1.0), color, esc(labels[i]), esc(v))
        )
        if v:
            parts.append(
                '<text class="csa-val" x="%.1f" y="%.1f" text-anchor="middle">%s</text>'
                % (x + bar_w / 2, y - 4,
                   esc(value_labels[i] if value_labels else v))
            )
        parts.append(
            '<text class="csa-axis" x="%.1f" y="%d" text-anchor="middle">%s</text>'
            % (x + bar_w / 2, height - 10, esc(labels[i]))
        )
    parts.append("</svg>")
    return "".join(parts)


def sparkline_area(points, labels, color="var(--series-1)", height=170):
    """Filled line chart for a daily series."""
    if not points or not any(points):
        return '<p class="csa-note">No activity recorded in this window.</p>'
    left, bottom, top_pad = 34, 28, 16
    chart_w = _W - left - 14
    chart_h = height - bottom - top_pad
    vmax = max(points) or 1
    n = len(points)
    step = chart_w / max(1, n - 1)

    coords = [
        (left + i * step, top_pad + chart_h - (v / vmax) * chart_h)
        for i, v in enumerate(points)
    ]
    line = " ".join("%.1f,%.1f" % c for c in coords)
    area = "%d,%.1f " % (left, top_pad + chart_h) + line + " %.1f,%.1f" % (
        left + (n - 1) * step, top_pad + chart_h
    )

    parts = ['<svg viewBox="0 0 %d %d" role="img" aria-label="activity over time">' % (_W, height)]
    for frac in (0, 0.5, 1.0):
        y = top_pad + chart_h - frac * chart_h
        parts.append(
            '<line class="csa-grid" x1="%d" y1="%.1f" x2="%d" y2="%.1f"/>'
            % (left, y, left + chart_w, y)
        )
        parts.append(
            '<text class="csa-axis" x="0" y="%.1f">%d</text>' % (y + 4, round(vmax * frac))
        )
    parts.append(
        '<polygon points="%s" fill="%s" fill-opacity="0.16"/>' % (area, color)
    )
    parts.append(
        '<polyline points="%s" fill="none" stroke="%s" stroke-width="2" '
        'stroke-linejoin="round"/>' % (line, color)
    )
    # Invisible full-height hit targets give every day a tooltip without
    # requiring JavaScript.
    for i, (cx, _cy) in enumerate(coords):
        parts.append(
            '<rect class="mark" x="%.1f" y="%d" width="%.1f" height="%.1f"'
            ' fill="transparent"><title>%s: %s attempts</title></rect>'
            % (cx - step / 2, top_pad, max(step, 1.0), chart_h,
               esc(labels[i]), esc(points[i]))
        )

    peak = max(range(n), key=lambda i: points[i])
    parts.append(
        '<circle cx="%.1f" cy="%.1f" r="3" fill="%s"/>' % (*coords[peak], color)
    )
    parts.append(
        '<text class="csa-val" x="%.1f" y="%.1f" text-anchor="middle">%d</text>'
        % (coords[peak][0], coords[peak][1] - 8, points[peak])
    )
    for i in (0, n // 2, n - 1):
        parts.append(
            '<text class="csa-axis" x="%.1f" y="%d" text-anchor="middle">%s</text>'
            % (coords[i][0], height - 8, esc(labels[i][5:]))
        )
    parts.append("</svg>")
    return "".join(parts)
