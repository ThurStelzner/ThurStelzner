#!/usr/bin/env python3
"""Mantém a paleta do perfil sincronizada com o avatar do GitHub.

O README do GitHub é estático: não existe CSS/JS capaz de ler a foto do
perfil. Por isso a paleta é calculada aqui e aplicada nos arquivos do repo.

O script só altera README.md, .github/palette.json e o JSON do gráfico 3D
quando o hash do avatar muda. O workflow faz o commit apenas se houver diff.
"""

from __future__ import annotations

import argparse
import colorsys
import hashlib
import io
import json
import math
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

DEFAULT_OWNER = "ThurStelzner"
DEFAULT_PALETTE_PATH = Path(".github/palette.json")
DEFAULT_README_PATH = Path("README.md")
DEFAULT_3D_CONFIG_PATH = Path("conf/github-profile-3d-contrib.json")

WHITE = (255, 255, 255)

# Usada se o avatar não puder ser lido e ainda não existir uma paleta salva.
DEFAULT_PALETTE: dict[str, str] = {
    "bg_dark": "#1A0533",
    "bg_mid": "#3B0764",
    "bg_soft": "#4C1D95",
    "primary": "#7C3AED",
    "secondary": "#8B5CF6",
    "tertiary": "#9333EA",
    "accent_dark": "#C026D3",
    "accent": "#D946EF",
    "highlight": "#C77DFF",
    "text": "#F5D0FE",
    "text_muted": "#A78BFA",
    "border": "#8E2DE2",
}

# Cores que o README usava antes da automação. Serve para a primeira
# migração; depois disso o próprio palette.json passa a ser a referência.
LEGACY_ROLE_MAP: dict[str, str] = {
    "1A0533": "bg_dark",
    "3B0764": "bg_mid",
    "6D28D9": "bg_soft",
    "7C3AED": "secondary",
    "8E2DE2": "primary",
    "8B5CF6": "secondary",
    "9333EA": "border",
    "A855F7": "tertiary",
    "C026D3": "tertiary",
    "D946EF": "accent_dark",
    "FF00C8": "accent",
    "C77DFF": "highlight",
    "F5D0FE": "text_muted",
    "FFFFFF": "text",
}


# ---------------------------------------------------------------------------
# Utilidades de cor
# ---------------------------------------------------------------------------


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))


def _hex_to_rgb(value: str) -> tuple[int, int, int]:
    value = value.lstrip("#")
    return tuple(int(value[i : i + 2], 16) for i in (0, 2, 4))  # type: ignore[return-value]


def _rgb_to_hex(rgb: Iterable[int]) -> str:
    r, g, b = (int(round(c)) for c in rgb)
    return f"#{_clamp(r, 0, 255):02X}{_clamp(g, 0, 255):02X}{_clamp(b, 0, 255):02X}"


def _hex_no_hash(value: str) -> str:
    return value.lstrip("#").upper()


def _rgb_to_hls(rgb: tuple[int, int, int]) -> tuple[float, float, float]:
    r, g, b = (c / 255 for c in rgb)
    return colorsys.rgb_to_hls(r, g, b)


def _hls_to_rgb(h: float, l: float, s: float) -> tuple[int, int, int]:
    r, g, b = colorsys.hls_to_rgb(h % 1, _clamp(l, 0, 1), _clamp(s, 0, 1))
    return (round(r * 255), round(g * 255), round(b * 255))


def _mix(a: str, b: str, amount: float) -> str:
    """Mistura duas cores hex; amount=0 devolve a, amount=1 devolve b."""
    ar, ag, ab = _hex_to_rgb(a)
    br, bg, bb = _hex_to_rgb(b)
    amount = _clamp(amount, 0, 1)
    return _rgb_to_hex(
        (
            ar + (br - ar) * amount,
            ag + (bg - ag) * amount,
            ab + (bb - ab) * amount,
        )
    )


def _relative_luminance(rgb: tuple[int, int, int]) -> float:
    def channel(value: int) -> float:
        c = value / 255
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4

    r, g, b = (channel(c) for c in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def _contrast(a: tuple[int, int, int], b: tuple[int, int, int]) -> float:
    la = _relative_luminance(a)
    lb = _relative_luminance(b)
    lighter, darker = max(la, lb), min(la, lb)
    return (lighter + 0.05) / (darker + 0.05)


def _contrast_white(value: str) -> float:
    return _contrast(_hex_to_rgb(value), WHITE)


def _darken_for_white(value: str, minimum: float = 4.5) -> str:
    """Escurece uma cor até ela ter contraste suficiente com texto branco."""
    if _contrast_white(value) >= minimum:
        return value.upper()
    h, l, s = _rgb_to_hls(_hex_to_rgb(value))
    while l > 0.02 and _contrast_white(_rgb_to_hex(_hls_to_rgb(h, l, s))) < minimum:
        l -= 0.01
    return _rgb_to_hex(_hls_to_rgb(h, l, s))


def _lighten_for_dark(value: str, background: str, minimum: float = 4.5) -> str:
    """Clareia uma cor até ela ter contraste suficiente com um fundo escuro."""
    if _contrast(_hex_to_rgb(value), _hex_to_rgb(background)) >= minimum:
        return value.upper()
    h, l, s = _rgb_to_hls(_hex_to_rgb(value))
    background_rgb = _hex_to_rgb(background)
    while l < 0.98 and _contrast(_hls_to_rgb(h, l, s), background_rgb) < minimum:
        l += 0.01
    return _rgb_to_hex(_hls_to_rgb(h, l, s))


def _unique_palette(colors: dict[str, str]) -> dict[str, str]:
    """Garante que cada papel tenha um hex único.

    A substituição no README usa o hex antigo como chave; valores repetidos
    tornariam a troca ambígua.
    """
    used: set[str] = set()
    result: dict[str, str] = {}
    for role, value in colors.items():
        h, l, s = _rgb_to_hls(_hex_to_rgb(value))
        candidate = _rgb_to_hex(_hex_to_rgb(value))
        step = 0
        while candidate in used and step < 40:
            step += 1
            delta = 0.015 if step % 2 else -0.015
            l = _clamp(l + delta, 0.03, 0.97)
            candidate = _rgb_to_hex(_hls_to_rgb(h, l, s))
        used.add(candidate)
        result[role] = candidate
    return result


def _normalize_colors(colors: dict[str, Any] | None) -> dict[str, str]:
    normalized: dict[str, str] = {}
    for role, default in DEFAULT_PALETTE.items():
        value = colors.get(role) if isinstance(colors, dict) else None
        if isinstance(value, str) and re.fullmatch(r"#?[0-9A-Fa-f]{6}", value.strip()):
            normalized[role] = "#" + value.strip().lstrip("#").upper()
        else:
            normalized[role] = default
    return normalized


# ---------------------------------------------------------------------------
# Extração da paleta
# ---------------------------------------------------------------------------


def _quantized_colors(image: Any, count: int = 20) -> list[dict[str, Any]]:
    from PIL import Image

    img = image.convert("RGB")
    img.thumbnail((240, 240))
    quantized = img.quantize(colors=count, method=Image.Quantize.MEDIANCUT).convert("RGB")
    raw = quantized.getcolors(img.width * img.height) or []
    colors: list[dict[str, Any]] = []
    for occurrences, rgb in raw:
        h, l, s = _rgb_to_hls(rgb)
        colors.append(
            {
                "count": occurrences,
                "rgb": rgb,
                "h": h,
                "l": l,
                "s": s,
                "hex": _rgb_to_hex(rgb),
            }
        )
    return sorted(colors, key=lambda item: item["count"], reverse=True)


def _anchor_hue(colors: list[dict[str, Any]]) -> tuple[float, float]:
    saturated = [c for c in colors if c["s"] >= 0.18 and c["l"] >= 0.06]
    if not saturated:
        saturated = [c for c in colors if c["l"] >= 0.04] or colors
    top = saturated[:12]
    total = sum(c["count"] for c in top) or 1
    x = sum(c["count"] * math.cos(2 * math.pi * c["h"]) for c in top)
    y = sum(c["count"] * math.sin(2 * math.pi * c["h"]) for c in top)
    hue = (math.atan2(y, x) % (2 * math.pi)) / (2 * math.pi)
    saturation = _clamp(sum(c["count"] * c["s"] for c in top) / total, 0.35, 0.75)
    return hue, saturation


def _pick(
    colors: list[dict[str, Any]],
    used: set[str],
    predicate: Any,
    key: Any,
) -> str | None:
    candidates = [c for c in colors if c["hex"] not in used and predicate(c)]
    if not candidates:
        return None
    chosen = max(candidates, key=key)
    used.add(chosen["hex"])
    return chosen["hex"]


def build_palette(image: Any) -> dict[str, str]:
    colors = _quantized_colors(image)
    if not colors:
        return dict(DEFAULT_PALETTE)

    hue, saturation = _anchor_hue(colors)
    used: set[str] = set()

    def fallback(
        *,
        dh: float = 0.0,
        lightness: float = 0.30,
        sat_factor: float = 0.65,
        min_sat: float = 0.25,
    ) -> str:
        rgb = _hls_to_rgb(
            (hue + dh) % 1,
            lightness,
            _clamp(saturation * sat_factor, min_sat, 0.88),
        )
        hex_value = _rgb_to_hex(rgb)
        used.add(hex_value)
        return hex_value

    darkest = min(colors, key=lambda c: c["l"])
    bg_dark = darkest["hex"]
    used.add(bg_dark)

    bg_mid = _pick(colors, used, lambda c: 0.08 <= c["l"] <= 0.28, lambda c: c["count"])
    bg_mid = bg_mid or fallback(lightness=0.13, sat_factor=0.55)

    bg_soft = _pick(colors, used, lambda c: 0.16 <= c["l"] <= 0.40, lambda c: c["count"])
    bg_soft = bg_soft or fallback(lightness=0.20, sat_factor=0.60)

    primary = _pick(
        colors,
        used,
        lambda c: 0.24 <= c["l"] <= 0.52 and _contrast_white(c["hex"]) >= 4.5,
        lambda c: c["count"] * (0.6 + c["s"]),
    )
    primary = primary or fallback(lightness=0.32, sat_factor=0.65)
    primary = _darken_for_white(primary, 4.5)

    secondary = _pick(
        colors,
        used,
        lambda c: 0.16 <= c["l"] <= 0.46 and _contrast_white(c["hex"]) >= 4.5,
        lambda c: c["count"],
    )
    secondary = secondary or fallback(dh=0.035, lightness=0.27, sat_factor=0.70)
    secondary = _darken_for_white(secondary, 4.5)

    tertiary = _pick(
        colors,
        used,
        lambda c: 0.16 <= c["l"] <= 0.50 and _contrast_white(c["hex"]) >= 4.5,
        lambda c: c["count"],
    )
    tertiary = tertiary or fallback(dh=-0.035, lightness=0.38, sat_factor=0.60)
    tertiary = _darken_for_white(tertiary, 4.5)

    accent_dark = _pick(
        colors,
        used,
        lambda c: 0.30 <= c["l"] <= 0.56 and _contrast_white(c["hex"]) >= 4.5,
        lambda c: c["count"] * (0.5 + c["s"]),
    )
    accent_dark = accent_dark or fallback(dh=0.02, lightness=0.38, sat_factor=0.70)
    accent_dark = _darken_for_white(accent_dark, 4.5)

    accent = _pick(
        colors,
        used,
        lambda c: c["l"] >= 0.52 and c["s"] >= 0.22,
        lambda c: c["count"] * (0.5 + c["s"]),
    )
    accent = accent or fallback(dh=0.03, lightness=0.58, sat_factor=0.75)

    accent_h, accent_l, accent_s = _rgb_to_hls(_hex_to_rgb(accent))
    highlight = _rgb_to_hex(
        _hls_to_rgb(
            (accent_h + 0.03) % 1,
            _clamp(accent_l + 0.06, 0.68, 0.80),
            _clamp(accent_s * 1.10, 0.45, 0.85),
        )
    )

    text = _rgb_to_hex(
        _hls_to_rgb(hue, 0.91, _clamp(max(saturation, 0.25) * 1.2, 0.35, 0.55))
    )
    text = _lighten_for_dark(text, bg_dark, 7.0)

    text_muted = _pick(
        colors,
        used,
        lambda c: 0.55 <= c["l"] <= 0.78 and c["s"] <= 0.40,
        lambda c: c["count"],
    )
    text_muted = text_muted or _rgb_to_hex(
        _hls_to_rgb(hue, 0.70, _clamp(saturation * 0.5, 0.15, 0.35))
    )
    text_muted = _lighten_for_dark(text_muted, bg_dark, 4.5)

    border = _mix(primary, accent, 0.25)
    if border.upper() in used:
        border = _mix(primary, accent, 0.40)

    def as_hex(value: str | tuple[int, int, int]) -> str:
        return value.upper() if isinstance(value, str) else _rgb_to_hex(value)

    palette = {
        "bg_dark": as_hex(bg_dark),
        "bg_mid": as_hex(bg_mid),
        "bg_soft": as_hex(bg_soft),
        "primary": as_hex(primary),
        "secondary": as_hex(secondary),
        "tertiary": as_hex(tertiary),
        "accent_dark": as_hex(accent_dark),
        "accent": as_hex(accent),
        "highlight": as_hex(highlight),
        "text": as_hex(text),
        "text_muted": as_hex(text_muted),
        "border": as_hex(border),
    }
    return {role: value.upper() for role, value in _unique_palette(palette).items()}


# ---------------------------------------------------------------------------
# Avatar e arquivos
# ---------------------------------------------------------------------------


def _fetch_avatar(owner: str) -> bytes:
    url = f"https://github.com/{owner}.png?size=460"
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": "profile-palette-bot/1.0 (+https://github.com/ThurStelzner/ThurStelzner)",
            "Accept": "image/*",
        },
    )
    with urllib.request.urlopen(request, timeout=30) as response:
        return response.read()


def _load_palette(path: Path) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None, None
    except (OSError, json.JSONDecodeError) as exc:
        print(f"::warning::Nao foi possivel ler {path}: {exc}", file=sys.stderr)
        return None, None
    colors = data.get("colors") if isinstance(data, dict) else None
    return (data if isinstance(data, dict) else None), _normalize_colors(colors)


def _write_palette(path: Path, owner: str, digest: str, colors: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "version": 1,
        "source": f"https://github.com/{owner}.png?size=460",
        "avatar_sha256": digest,
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "colors": colors,
    }
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _apply_readme(text: str, old_colors: dict[str, str], new_colors: dict[str, str]) -> str:
    mapping: dict[str, str] = {}
    for role, new_value in new_colors.items():
        old_value = old_colors.get(role)
        if old_value:
            mapping[_hex_no_hash(old_value)] = _hex_no_hash(new_value)

    # Migração das cores antigas, mesmo que o palette.json ainda não exista.
    for old_hex, role in LEGACY_ROLE_MAP.items():
        mapping.setdefault(old_hex, _hex_no_hash(new_colors[role]))

    mapping = {old: new for old, new in mapping.items() if old != new}
    if not mapping:
        return text

    # O badge de visitas usava um cinza de 3 dígitos (%23555).
    text = text.replace("%23555", "%23" + _hex_no_hash(new_colors["bg_soft"]))

    alternatives = "|".join(re.escape(value) for value in sorted(mapping, key=len, reverse=True))
    pattern = re.compile(
        rf"(?<![0-9A-Fa-f])(?P<prefix>%23|#)?(?P<hex>{alternatives})(?![0-9A-Fa-f])",
        re.IGNORECASE,
    )

    def replace(match: re.Match[str]) -> str:
        prefix = match.group("prefix") or ""
        old_value = match.group("hex").upper()
        return f"{prefix}{mapping[old_value]}"

    return pattern.sub(replace, text)


ASSET_VERSION_PATHS: tuple[str, ...] = (
    "profile/estatisticas.svg",
    "profile/linguagens.svg",
    "profile/sequencia.svg",
    "output-3d-contrib/day.svg",
    "output-3d-contrib/night.svg",
    "output/github-contribution-grid-snake.svg",
    "output/github-contribution-grid-snake-dark.svg",
)


def _apply_asset_versions(text: str, version: str) -> str:
    """Adiciona ?v=<versão> nas imagens geradas para furar cache do GitHub."""
    for asset in ASSET_VERSION_PATHS:
        pattern = re.compile(re.escape(asset) + r'(\?[^"\s<>)]*)?')

        def replace(match: re.Match[str], asset: str = asset) -> str:
            query = match.group(1) or ""
            params = [
                param
                for param in query.lstrip("?").split("&")
                if param and not param.startswith("v=")
            ]
            params.append(f"v={version}")
            return f"{asset}?{'&'.join(params)}"

        text = pattern.sub(replace, text)
    return text


def _write_3d_config(path: Path, colors: dict[str, str]) -> None:
    c = colors
    light_bg = _mix(c["accent"], "#FFFFFF", 0.90)
    light_weak = _mix(c["accent"], "#FFFFFF", 0.76)
    light_contrib = [
        _mix(c["accent"], "#FFFFFF", 0.88),
        _mix(c["accent"], "#FFFFFF", 0.68),
        _mix(c["accent"], "#FFFFFF", 0.46),
        c["accent_dark"],
        c["primary"],
    ]
    dark_contrib = [
        c["bg_mid"],
        c["primary"],
        c["secondary"],
        c["accent_dark"],
        c["accent"],
    ]
    data = [
        {
            "type": "normal",
            "fileName": "day.svg",
            "backgroundColor": light_bg,
            "foregroundColor": c["bg_mid"],
            "strongColor": c["primary"],
            "weakColor": light_weak,
            "radarColor": c["accent_dark"],
            "growingAnimation": True,
            "contribColors": light_contrib,
        },
        {
            "type": "normal",
            "fileName": "night.svg",
            "backgroundColor": c["bg_dark"],
            "foregroundColor": c["text"],
            "strongColor": c["accent"],
            "weakColor": c["bg_soft"],
            "radarColor": c["highlight"],
            "growingAnimation": True,
            "contribColors": dark_contrib,
        },
    ]
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _emit_outputs(colors: dict[str, str], owner: str, changed: bool) -> None:
    c = colors
    stats_options = (
        f"username={owner}&show_icons=true&hide_border=true"
        f"&title_color={_hex_no_hash(c['accent'])}"
        f"&icon_color={_hex_no_hash(c['highlight'])}"
        f"&text_color={_hex_no_hash(c['text'])}"
        f"&bg_color=45,{_hex_no_hash(c['bg_dark'])},{_hex_no_hash(c['bg_mid'])}"
        f"&border_color={_hex_no_hash(c['border'])}"
    )
    langs_options = (
        f"username={owner}&layout=compact&langs_count=10&hide_border=true"
        f"&title_color={_hex_no_hash(c['accent'])}"
        f"&icon_color={_hex_no_hash(c['highlight'])}"
        f"&text_color={_hex_no_hash(c['text'])}"
        f"&bg_color=45,{_hex_no_hash(c['bg_dark'])},{_hex_no_hash(c['bg_mid'])}"
        f"&border_color={_hex_no_hash(c['border'])}"
    )
    streak_options = (
        f"user={owner}&hide_border=true"
        f"&background=45,{_hex_no_hash(c['bg_dark'])},{_hex_no_hash(c['bg_mid'])}"
        f"&border={_hex_no_hash(c['border'])}"
        f"&stroke={_hex_no_hash(c['primary'])}"
        f"&ring={_hex_no_hash(c['highlight'])}"
        f"&fire={_hex_no_hash(c['accent'])}"
        f"&currStreakNum={_hex_no_hash(c['text'])}"
        f"&currStreakLabel={_hex_no_hash(c['accent'])}"
        f"&sideNums={_hex_no_hash(c['text'])}"
        f"&sideLabels={_hex_no_hash(c['text_muted'])}"
        f"&dates={_hex_no_hash(c['text_muted'])}"
        f"&locale=pt_BR&date_format=j%2Fn%2F%5BY%5D&disable_animations=true"
    )

    light_dots = [
        _mix(c["accent"], "#FFFFFF", 0.88),
        _mix(c["accent"], "#FFFFFF", 0.68),
        _mix(c["accent"], "#FFFFFF", 0.46),
        c["accent_dark"],
        c["primary"],
    ]
    dark_dots = [c["bg_mid"], c["primary"], c["secondary"], c["accent_dark"], c["accent"]]

    outputs = {
        "changed": "true" if changed else "false",
        "stats_options": stats_options,
        "langs_options": langs_options,
        "streak_options": streak_options,
        "snake_light_snake": _hex_no_hash(c["primary"]),
        "snake_light_dots": ",".join(f"%23{_hex_no_hash(value)}" for value in light_dots),
        "snake_dark_snake": _hex_no_hash(c["highlight"]),
        "snake_dark_dots": ",".join(f"%23{_hex_no_hash(value)}" for value in dark_dots),
    }

    output_path = os.environ.get("GITHUB_OUTPUT")
    if output_path:
        with open(output_path, "a", encoding="utf-8") as handle:
            for key, value in outputs.items():
                handle.write(f"{key}={value}\n")
    else:
        print(json.dumps(outputs, ensure_ascii=False, indent=2))


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--owner", default=None, help="Usuário do GitHub (padrão: GITHUB_REPOSITORY_OWNER)")
    parser.add_argument("--palette", type=Path, default=DEFAULT_PALETTE_PATH)
    parser.add_argument("--readme", type=Path, default=DEFAULT_README_PATH)
    parser.add_argument("--config-3d", type=Path, default=DEFAULT_3D_CONFIG_PATH)
    parser.add_argument("--outputs-only", action="store_true", help="Apenas lê a paleta e gera outputs")
    parser.add_argument("--check", action="store_true", help="Apenas verifica se o avatar mudou")
    parser.add_argument("--force", action="store_true", help="Recalcula mesmo se o avatar não mudou")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    owner = args.owner or os.environ.get("GITHUB_REPOSITORY_OWNER") or DEFAULT_OWNER
    force = args.force or os.environ.get("FORCE_PALETTE", "").strip().lower() == "true"

    old_doc, old_colors = _load_palette(args.palette)

    if args.outputs_only:
        colors = old_colors or dict(DEFAULT_PALETTE)
        _emit_outputs(colors, owner, False)
        return 0

    try:
        avatar = _fetch_avatar(owner)
        digest = hashlib.sha256(avatar).hexdigest()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"::warning::Nao foi possivel baixar o avatar de {owner}: {exc}", file=sys.stderr)
        if old_colors:
            _emit_outputs(old_colors, owner, False)
            return 0
        colors = dict(DEFAULT_PALETTE)
        _emit_outputs(colors, owner, False)
        return 0

    changed = force or old_doc is None or old_doc.get("avatar_sha256") != digest

    if args.check:
        colors = old_colors or dict(DEFAULT_PALETTE)
        _emit_outputs(colors, owner, changed)
        return 0

    if not changed and old_colors:
        colors = old_colors
        if not args.config_3d.exists():
            _write_3d_config(args.config_3d, colors)
        _emit_outputs(colors, owner, False)
        return 0

    if changed:
        try:
            from PIL import Image

            with Image.open(io.BytesIO(avatar)) as image:
                colors = build_palette(image)
        except Exception as exc:  # Pillow ausente, imagem inválida etc.
            print(f"::warning::Falha ao extrair a paleta do avatar: {exc}", file=sys.stderr)
            if old_colors:
                _emit_outputs(old_colors, owner, False)
                return 0
            colors = dict(DEFAULT_PALETTE)
            changed = False

    if changed:
        colors = _normalize_colors(colors)
        readme_path: Path = args.readme
        if readme_path.exists():
            original = readme_path.read_text(encoding="utf-8")
            updated = _apply_readme(original, old_colors or {}, colors)
            updated = _apply_asset_versions(updated, digest[:12])
            if updated != original:
                readme_path.write_text(updated, encoding="utf-8")
        _write_palette(args.palette, owner, digest, colors)
        _write_3d_config(args.config_3d, colors)

    _emit_outputs(colors, owner, changed)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
