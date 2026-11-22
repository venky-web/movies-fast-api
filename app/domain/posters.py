"""Port of `<mfe>/data/posters.ts`. Returns SVG text (the API serves it at /api/posters/{id}.svg)."""


def create_poster(title: str, hue: int) -> str:
    lines = _wrap_title(title.upper(), 13)
    line_height = 34
    first_y = 380 - (len(lines) - 1) * line_height
    text = "".join(
        f'<text x="150" y="{first_y + i * line_height}">{_escape_xml(line)}</text>'
        for i, line in enumerate(lines)
    )
    return (
        '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="450" viewBox="0 0 300 450">'
        '<defs><linearGradient id="g" x1="0" y1="0" x2="0.4" y2="1">'
        f'<stop offset="0" stop-color="hsl({hue} 70% 52%)"/>'
        f'<stop offset="1" stop-color="hsl({(hue + 45) % 360} 65% 14%)"/>'
        "</linearGradient></defs>"
        '<rect width="300" height="450" fill="url(#g)"/>'
        f'<circle cx="230" cy="110" r="90" fill="hsl({(hue + 180) % 360} 80% 70%)" opacity=".25"/>'
        '<circle cx="60" cy="230" r="55" fill="#fff" opacity=".08"/>'
        '<g fill="#fff" font-family="Segoe UI, Helvetica, Arial, sans-serif" font-size="28" '
        f'font-weight="700" text-anchor="middle" letter-spacing="1">{text}</g>'
        '<rect x="110" y="410" width="80" height="3" fill="#fff" opacity=".6"/>'
        "</svg>"
    )


def _wrap_title(title: str, max_chars: int) -> list[str]:
    lines: list[str] = []
    current = ""
    for word in title.split():
        if current and len(f"{current} {word}") > max_chars:
            lines.append(current)
            current = word
        else:
            current = f"{current} {word}" if current else word
    if current:
        lines.append(current)
    return lines


def _escape_xml(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&apos;")
    )
