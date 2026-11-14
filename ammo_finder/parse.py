"""Pull ammo attributes (caliber, grain, case, shot weight, ...) out of free text.

Product titles are messy, so everything here is a best-effort regex read.
When a value cannot be found it is left as None, and the rules decide whether
an unknown value disqualifies a listing or just adds a "verify" note.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Optional

_FRACTIONS = {"¼": " 1/4", "½": " 1/2", "¾": " 3/4", "⅛": " 1/8", "⅜": " 3/8", "″": '"', "”": '"', "“": '"'}


def normalize(text: str) -> str:
    for k, v in _FRACTIONS.items():
        text = text.replace(k, v)
    text = text.replace(" ", " ")
    return re.sub(r"\s+", " ", text).strip()


@dataclass
class Attrs:
    caliber: Optional[str] = None  # "9mm" | "12ga" | "762x54r"
    grain: Optional[int] = None
    case: Optional[str] = None  # brass | steel | aluminum | polymer
    fmj: bool = False
    expanding: bool = False  # JHP/SP/etc - not range ammo
    rounds: Optional[int] = None
    shot_oz: Optional[float] = None
    shot_sizes: set = field(default_factory=set)
    shell_len: Optional[float] = None
    velocity: Optional[int] = None
    shot_material: Optional[str] = None  # lead | steel | bismuth | tungsten
    not_birdshot: bool = False  # buckshot / slug
    corrosive: Optional[bool] = None
    steel_core: Optional[bool] = None
    berdan: Optional[bool] = None
    surplus: bool = False
    sealed_can: bool = False
    reloaded: bool = False
    not_ammo: bool = False  # brass, bullets, dummies, empty cans, ...
    used: bool = False


# --- caliber ---------------------------------------------------------------
_RE_9MM = re.compile(r"\b9\s?mm\b|\b9\s?x\s?19\b|\b9mm\s?luger|parabellum", re.I)
_RE_9MM_NOT = re.compile(r"makarov|9\s?x\s?18|\bkurz\b|\.?380\b|9mm\s?short|9mm\s?major|9x21|9x23|\bpak\b", re.I)
_RE_12GA = re.compile(r"\b12\s?-?\s?(?:ga\.?|gauge|g)\b|\b12ga\b", re.I)
_RE_54R = re.compile(r"7\.62\s?x\s?54\s?(?:mm)?\s?r\b|7\.62x54|\b762x54|7\.62\s?russian|54r\b", re.I)


def detect_caliber(text: str) -> Optional[str]:
    if _RE_54R.search(text):
        return "762x54r"
    if _RE_12GA.search(text):
        return "12ga"
    if _RE_9MM.search(text) and not _RE_9MM_NOT.search(text):
        return "9mm"
    return None


# --- rounds ----------------------------------------------------------------
_RE_BOXES_OF = re.compile(r"(\d+)\s*(?:boxes|bx|packs?|cartons?)\s*(?:of|x)\s*(\d+)", re.I)
_RE_COUNT = re.compile(
    r"(?<![\d.$/])(\d{1,2},\d{3}|\d{1,5})\s*-?\s*(?:rounds?|rnds?|rds?|ct|count|shells?|cartridges?|pcs?)\b"
    r"(?!\s*(?:/|per)\s*(?:box|bx|case|cs|carton|pack))",
    re.I,
)
_RE_OF_COUNT = re.compile(r"\b(?:case|box|can|tin|pack|crate|carton|spam can)\s+of\s+(\d{1,2},\d{3}|\d{1,5})\b", re.I)


def detect_rounds(text: str) -> Optional[int]:
    cands: list[int] = []
    for a, b in _RE_BOXES_OF.findall(text):
        cands.append(int(a) * int(b))
    for m in _RE_COUNT.findall(text):
        cands.append(int(m.replace(",", "")))
    for m in _RE_OF_COUNT.findall(text):
        cands.append(int(m.replace(",", "")))
    cands = [c for c in cands if 5 <= c <= 20000]
    return max(cands) if cands else None


# --- other attributes ------------------------------------------------------
_STEEL_CASE_BRANDS = re.compile(
    r"\b(wolf|tula|tulammo|barnaul|brown bear|silver bear|golden bear|golden tiger|red army standard|"
    r"bear claw|maxxtech steel|silver state armory)\b",
    re.I,
)
_RE_STEEL_CASE = re.compile(
    r"steel[\s-]*(case|cased|casing)|lacquer(ed)?[\s-]*steel|polymer[\s-]*(coated)?[\s-]*steel|"
    r"brass[\s-]*(washed|coated|plated|lacquered)|zinc[\s-]*plated[\s-]*steel|bi-?metal case",
    re.I,
)


def detect_case(text: str) -> Optional[str]:
    if _RE_STEEL_CASE.search(text) or _STEEL_CASE_BRANDS.search(text):
        return "steel"
    if re.search(r"blazer\s+brass", text, re.I):
        return "brass"
    if re.search(r"alumin(um|ium)|\bblazer\b(?!\s+brass)", text, re.I):
        return "aluminum"
    if re.search(r"polymer[\s-]*case|polycase", text, re.I):
        return "polymer"
    if re.search(r"\bbrass\b|nickel[\s-]*plated|\bnickel\b|\bboxer\b|reloadable", text, re.I):
        return "brass"
    return None


_RE_SHOT_OZ = re.compile(r"(?<![\d/.])(\d)(?:\s*[- ]\s*(\d)\s*/\s*(\d)|\.(\d{1,3}))?\s*(?:oz\b|ounces?\b)", re.I)


def detect_shot_oz(text: str) -> Optional[float]:
    m = _RE_SHOT_OZ.search(text)
    if not m:
        return None
    whole, num, den, dec = m.groups()
    val = float(whole)
    if num and den:
        val += int(num) / int(den)
    elif dec:
        val = float(f"{whole}.{dec}")
    return round(val, 4) if 0.5 <= val <= 2.5 else None


def detect_shell_length(text: str) -> Optional[float]:
    # "3-1/4 Dram" is a powder charge, not a shell length
    text = re.sub(r"(max\s*)?\d(\s*[- ]\s*\d/\d|\.\d+)?\s*(dram|dr\b|dr\.)", " ", text, flags=re.I)
    if re.search(r"3\s*[- ]\s*1/2|3\.5\s*(\"|in\b|inch)|89\s?mm", text, re.I):
        return 3.5
    if re.search(r"2\s*[- ]\s*3/4|2\.75|\b70\s?mm|2-3/4", text, re.I):
        return 2.75
    if re.search(r"(?<![\d/.\-])3\s*(\"|''|in\b|inch)|\b3\.0\s*(\"|in)|\b76\s?mm|3\" magnum", text, re.I):
        return 3.0
    if re.search(r"2\s*[- ]\s*1/2|\b65\s?mm|2\.5\s*(\"|in)", text, re.I):
        return 2.5
    return None


def detect_shot_sizes(text: str) -> set:
    t = re.sub(r"(\d)\s*[- ]\s*1/2", r"\1.5", text)
    sizes = set()
    for m in re.findall(r"#\s?(\d{1,2}(?:\.\d)?)", t):
        sizes.add(m)
    for m in re.findall(r"\b(?:no\.?|size|shot size)\s?(\d{1,2}(?:\.\d)?)\b", t, re.I):
        sizes.add(m)
    for m in re.findall(r"\b(\d{1,2}(?:\.\d)?)\s*(?:shot|lead shot)\b", t, re.I):
        sizes.add(m)
    return {s[:-2] if s.endswith(".0") else s for s in sizes}


def detect_velocity(text: str) -> Optional[int]:
    vals = [int(v.replace(",", "")) for v in re.findall(r"(\d,?\d{3}|\d{3})\s*(?:fps|ft\.?/s(?:ec)?|feet per second)", text, re.I)]
    vals = [v for v in vals if 600 <= v <= 4000]
    return max(vals) if vals else None


def detect_shot_material(text: str) -> Optional[str]:
    if re.search(r"tungsten|\btss\b|hevi-?shot|heavi-?shot|hevi-?13|hevi-?x", text, re.I):
        return "tungsten"
    if re.search(r"bismuth", text, re.I):
        return "bismuth"
    if re.search(r"\bsteel\b|fasteel|blind side|blindside|xpert|waterfowl", text, re.I):
        return "steel"
    if re.search(r"\blead\b", text, re.I):
        return "lead"
    return None


def parse(text: str, caliber_hint: Optional[str] = None) -> Attrs:
    t = normalize(text)
    a = Attrs()
    a.caliber = detect_caliber(t) or caliber_hint
    g = re.search(r"(?<![\d.])(\d{2,3})\s*-?\s*(?:gr\b|grain|grs?\b)", t, re.I)
    a.grain = int(g.group(1)) if g else None
    a.case = detect_case(t)
    a.fmj = bool(re.search(r"\bfmj\b|\bfmc\b|full metal (jacket|case)|\btmj\b|\bball\b|\blps\b|\bmc\b|\bfmj-?bt\b", t, re.I))
    a.expanding = bool(
        re.search(r"\bjhp\b|hollow ?point|\bhp\b|\bsp\b|soft ?point|\bpsp\b|\bv-?max\b|frangible|\bsjhp\b|\bxtp\b|defense|self-?defense|\bhst\b|gold dot|critical", t, re.I)
    )
    a.rounds = detect_rounds(t)
    a.shot_oz = detect_shot_oz(t)
    a.shot_sizes = detect_shot_sizes(t)
    a.shell_len = detect_shell_length(t)
    a.velocity = detect_velocity(t)
    a.shot_material = detect_shot_material(t)
    a.not_birdshot = bool(re.search(r"buck(shot)?\b|\bslugs?\b|sabot|\b00\s?b\b|\b000\b|defense", t, re.I))

    if re.search(r"non[\s-]?corrosive|\bnoncorrosive\b", t, re.I):
        a.corrosive = False
    elif re.search(r"corrosive", t, re.I):
        a.corrosive = True
    if re.search(r"steel[\s-]*core|\blps\b|steel penetrator|\bspp\b|7n1\b|57-n-323", t, re.I):
        a.steel_core = True
    elif re.search(r"lead[\s-]*core", t, re.I):
        a.steel_core = False
    if re.search(r"berdan", t, re.I):
        a.berdan = True
    elif re.search(r"\bboxer\b|reloadable", t, re.I):
        a.berdan = False

    a.surplus = bool(
        re.search(r"surplus|mil-?surp|spam ?can|sealed can|military issue|\b(bulgarian|russian|indian|polish|czech|yugo|yugoslavian|hungarian|romanian|albanian|chinese|soviet|ussr)\b", t, re.I)
    )
    a.sealed_can = bool(re.search(r"spam ?can|sealed can|\bcan\b|\btin\b|crate", t, re.I))
    a.reloaded = bool(re.search(r"\breload(ed|s)?\b|reman(ufactured)?\b|re-?manufactured|refurbished|remanned", t, re.I))
    a.not_ammo = bool(
        re.search(
            r"\bblanks?\b|dummy|snap[\s-]?caps?|inert|\bbrass only\b|once[\s-]?fired|unprimed|\bprimers?\b|projectiles?|"
            r"bullets? only|\bcomponents?\b|\bempty\b|magazine|speed ?loader|stripper clips?|\bholster\b|gift card|hulls?\b|wads?\b|"
            r"training cartridge|marking",
            t,
            re.I,
        )
    )
    a.used = bool(re.search(r"\bused\b|pre-?owned", t, re.I))
    return a
