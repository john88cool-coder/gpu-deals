"""Нормализация названий: извлечение чипа, объёма памяти, партномера, бренда.

Двухуровневый матчинг: партномер, если он есть в названии, иначе откат на
класс «чип + объём памяти». Нечёткое сравнение строк не используется — у GPU
одна цифра меняет модель, и ложные склейки дороже пропусков.
"""

from __future__ import annotations

import re

# «RTX 5070 Ti», «RTX5070Ti», «RX 9070 XT», «RX 7800 XT».
_NVIDIA = re.compile(r"\bRTX\s?(\d{4})\s?(Ti\s?Super|Ti|Super)?\b", re.I)
_AMD = re.compile(r"\bRX\s?(\d{4})\s?(XTX|XT|GRE)?\b", re.I)

# «16 Гб», «16GB», «16 GB», «12Gb», «/16G ».
_MEMORY = re.compile(r"\b(\d{1,2})\s?(?:GB|G[бb]|Гб|G)\b", re.I)

# Оперативная память и накопители в названиях сборок: «DDR5 16GB», «SSD 512GB».
# Без этого «RTX5050/DDR5 16GB» дало бы видеокарте 16 ГБ видеопамяти.
_NOT_VRAM = re.compile(r"(?:DDR\d?|SSD|HDD|NVMe|M\.2)\s*$", re.I)

# Идентификатор модели в квадратных скобках — так его подаёт Technodom.
_BRACKETED = re.compile(r"\[([^\]]{6,60})\]")

# Партномера производителей. Используются, когда скобок в названии нет.
_PART_NUMBER_PATTERNS = (
    # Gigabyte: GV-N5070WF3OC-12GD, GV-N506TEAGLEOC ICE-16G.
    # Хвост из одних цифр не забираем: «GV-N507TWF3OC-16GD 16 Гб» — 16 это объём.
    re.compile(r"\bGV-N[A-Z0-9]+(?:[- ][A-Z0-9]*[A-Z][A-Z0-9]*)*\b", re.I),
    # Palit/Gainward: NE75070019K9-GB2050S, NE7506TU19T1-GB2061M
    re.compile(r"\bNE\d[A-Z0-9]{6,}-[A-Z0-9]+\b", re.I),
    # ASUS: DUAL-RTX5060TI-O16G, PRIME-RTX5060TI-O16G, TUF-RTX5070-O12G-GAMING
    re.compile(r"\b(?:DUAL|PRIME|TUF|ROG|PROART|STRIX)-RTX\d{4}[A-Z]*-[A-Z0-9-]+\b", re.I),
    # MSI: RTX 5060 Ti 16G VENTUS 3X OC — партномера как такового нет, пропускаем.
    # INNO3D: N506T2-08D7-193075N, N50702-12D7X-195064N (длина групп различается).
    re.compile(r"\bN[0-9][0-9A-Z]{4}-[0-9A-Z]{4,5}-[0-9A-Z]+\b", re.I),
)

_BRANDS = (
    "ASUS", "Gigabyte", "MSI", "Palit", "Gainward", "INNO3D", "ZOTAC",
    "Sapphire", "PowerColor", "XFX", "AFOX", "Colorful", "PNY", "KFA2",
)

# Признаки того, что позиция — готовая сборка, а не отдельная карта.
# `\bкомпьютер\b` не задевает «компьютерные аксессуары»: граница слова требует
# после «компьютер» несловесного символа.
_BUILD_MARKERS = re.compile(
    r"систем\w*\s+блок|\bкомпьютер\b|\bПК\b|\bITBRO\b|\bGARANT\b|\bIT-MR\b"
    r"|\bSSD\s*\d{3,4}\s*Г?[бb]|/\s*\d{2}\s*Гб\s*/",
    re.I,
)


def looks_like_build(title: str) -> bool:
    """Отличает готовую сборку от отдельной видеокарты по названию."""
    return bool(_BUILD_MARKERS.search(title))


# Допустимые объёмы видеопамяти по чипам. Служат двум целям: подставить штатный
# объём, когда в названии сборки он опущен («RTX5060/DDR4 16GB»), и отбросить
# заведомо чужое число («RTX 5060 TI/32GB/SSD 1TB» — 32 ГБ это оперативная).
# У чипов с двумя конфигурациями объём не угадывается: позиция останется без
# класса и в сравнение аналогов не попадёт.
_VRAM_CONFIGS: dict[str, frozenset[int]] = {
    "rtx5050": frozenset({8}),
    "rtx5060": frozenset({8}),
    "rtx5060ti": frozenset({8, 16}),
    "rtx5070": frozenset({12}),
    "rtx5070ti": frozenset({16}),
    "rtx5080": frozenset({16}),
    "rtx5090": frozenset({24, 32}),
    "rtx4060": frozenset({8}),
    "rtx4060ti": frozenset({8, 16}),
    "rtx4070": frozenset({12}),
    "rtx4070super": frozenset({12}),
    "rtx4070ti": frozenset({12}),
    "rtx4070tisuper": frozenset({16}),
    "rtx4080": frozenset({16}),
    "rtx4080super": frozenset({16}),
    "rtx4090": frozenset({24}),
    "rx9060": frozenset({8}),
    "rx9060xt": frozenset({8, 16}),
    "rx9070": frozenset({16}),
    "rx9070gre": frozenset({12}),
    "rx9070xt": frozenset({16}),
    "rx7600": frozenset({8}),
    "rx7600xt": frozenset({16}),
    "rx7700xt": frozenset({12}),
    "rx7800xt": frozenset({16}),
    "rx7900gre": frozenset({16}),
    "rx7900xt": frozenset({20}),
    "rx7900xtx": frozenset({24}),
}


def canonical_vram(chip: str | None) -> int | None:
    """Штатный объём видеопамяти чипа, если конфигурация единственная."""
    if not chip:
        return None
    sizes = _VRAM_CONFIGS.get(chip)
    return next(iter(sizes)) if sizes and len(sizes) == 1 else None


def _vram_plausible(chip: str | None, value: int) -> bool:
    """Похоже ли число на объём видеопамяти этого чипа."""
    if not 4 <= value <= 32:
        return False
    sizes = _VRAM_CONFIGS.get(chip) if chip else None
    return value in sizes if sizes else True


def extract_chip(title: str) -> str | None:
    """Нормализованный чип: 'rtx5070ti', 'rx9070xt'."""
    if m := _NVIDIA.search(title):
        suffix = (m.group(2) or "").replace(" ", "").lower()
        return f"rtx{m.group(1)}{suffix}"
    if m := _AMD.search(title):
        return f"rx{m.group(1)}{(m.group(2) or '').lower()}"
    return None


def extract_memory_gb(title: str, chip: str | None = None) -> int | None:
    """Объём видеопамяти в ГБ.

    В названиях сборок объёмов несколько ('RTX 5060 Ti 16 Гб / 32 Гб / SSD
    1000 Гб'), поэтому берём то число, что стоит сразу после чипа, и отбрасываем
    те, перед которыми стоит DDR5 или SSD.
    """
    if chip:
        pattern = _NVIDIA if chip.startswith("rtx") else _AMD
        if m := pattern.search(title):
            tail = title[m.end() : m.end() + 24]
            for mem in _MEMORY.finditer(tail):
                if _NOT_VRAM.search(tail[: mem.start()]):
                    continue
                if _vram_plausible(chip, int(mem.group(1))):
                    return int(mem.group(1))
    for mem in _MEMORY.finditer(title):
        if _NOT_VRAM.search(title[: mem.start()]):
            continue
        if _vram_plausible(chip, int(mem.group(1))):
            return int(mem.group(1))
    # В сборках объём видеопамяти часто опущен: «RTX5060/DDR4 16GB». Берём
    # штатный, но только для чипов с единственной конфигурацией.
    return canonical_vram(chip)


def extract_part_number(title: str) -> str | None:
    """Партномер модели.

    Сначала шаблоны вендоров: у части магазинов партномер стоит в круглых
    скобках рядом со спецификацией, и шаблон точнее её отбрасывает. Затем
    квадратные скобки — так идентификатор подаёт Technodom, включая MSI, у
    которого партномера как отдельной строки нет.
    """
    for pattern in _PART_NUMBER_PATTERNS:
        if m := pattern.search(title):
            return m.group(0).upper().replace(" ", "")
    return _from_brackets(title)


def _from_brackets(title: str) -> str | None:
    """Содержимое квадратных скобок, если похоже на идентификатор модели."""
    m = _BRACKETED.search(title)
    if not m:
        return None
    value = m.group(1).strip()
    # Часть названий заканчивается брендом: [DUAL-RTX5060-O8G-WHITE ASUS].
    for brand in _BRANDS:
        if value.upper().endswith(" " + brand.upper()):
            value = value[: -len(brand) - 1].strip()
            break
    # Спецификация в скобках («16 ГБ, GDDR7, 128БИТ») идентификатором не является.
    if "," in value or "ГБ" in value.upper() or "БИТ" in value.upper() or "GDDR" in value.upper():
        return None
    # Идентификатор всегда содержит цифру и не выглядит как фраза из слов.
    if not any(ch.isdigit() for ch in value) or len(value) < 6:
        return None
    # Пробелы мешают сопоставлению между магазинами: один пишет
    # «GV-N5070EAGLE OC-12GD», другой — «GV-N5070EAGLEOC-12GD».
    return value.upper().replace(" ", "")


def extract_brand(title: str) -> str | None:
    lowered = title.lower()
    for brand in _BRANDS:
        if brand.lower() in lowered:
            return brand
    return None


def class_key(chip: str | None, memory_gb: int | None) -> str | None:
    """Ключ класса для сравнения аналогов: 'rtx5070ti-16'."""
    if not chip or not memory_gb:
        return None
    return f"{chip}-{memory_gb}"
