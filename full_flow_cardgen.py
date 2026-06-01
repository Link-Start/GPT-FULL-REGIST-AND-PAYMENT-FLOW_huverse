#!/usr/bin/env python3
"""Small card generator used by the Web UI resource pool.

The generator mirrors the standalone CardGen package interface for the
countries/brands used by this project, and also supports a custom BIN for
quick test-card pool replenishment.
"""

from __future__ import annotations

import argparse
import json
import random
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Iterable


COUNTRY_BRAND_BINS: dict[str, dict[str, tuple[str, ...]]] = {
    "jp": {
        "visa": ("453450", "454153", "454294", "489784", "490714"),
        "mastercard": ("524806", "524807", "524808", "524809", "525000"),
    },
    "us": {
        "visa": ("411111", "414720", "424242", "426684", "400000"),
        "mastercard": ("510510", "555555", "527675", "520082", "541333"),
    },
}

COUNTRY_ALIASES = {
    "jp": "jp",
    "japan": "jp",
    "日本": "jp",
    "us": "us",
    "usa": "us",
    "unitedstates": "us",
    "united-states": "us",
    "united_states": "us",
    "america": "us",
    "美国": "us",
}

BRAND_ALIASES = {
    "visa": "visa",
    "mastercard": "mastercard",
    "mc": "mastercard",
    "master": "mastercard",
    "master-card": "mastercard",
    "master_card": "mastercard",
}


@dataclass(frozen=True)
class GeneratedCard:
    pan: str
    formatted: str
    expiry: str
    cvv: str
    country: str
    brand: str
    bin_prefix: str
    luhn: bool = True

    def as_line(self) -> str:
        return f"{self.formatted} {self.expiry} {self.cvv}"

    def as_dict(self) -> dict[str, object]:
        return asdict(self)


def normalize_country(value: str) -> str:
    key = "".join(str(value or "").strip().lower().split())
    key = key.replace("_", "-")
    return COUNTRY_ALIASES.get(key, COUNTRY_ALIASES.get(key.replace("-", ""), key))


def normalize_brand(value: str) -> str:
    key = str(value or "visa").strip().lower()
    return BRAND_ALIASES.get(key, key)


def luhn_check_digit(prefix_without_check: str) -> str:
    total = 0
    reverse_digits = list(map(int, reversed(prefix_without_check)))
    for index, digit in enumerate(reverse_digits, start=1):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return str((10 - (total % 10)) % 10)


def luhn_valid(pan: str) -> bool:
    digits = [int(ch) for ch in str(pan) if ch.isdigit()]
    total = 0
    parity = len(digits) % 2
    for index, digit in enumerate(digits):
        if index % 2 == parity:
            digit *= 2
            if digit > 9:
                digit -= 9
        total += digit
    return total % 10 == 0


def format_pan(pan: str) -> str:
    return " ".join(pan[i : i + 4] for i in range(0, len(pan), 4))


def _rng(seed: int | None = None) -> random.Random:
    return random.Random(seed) if seed is not None else random.SystemRandom()


def generate_card(country: str = "jp", brand: str = "visa", *, bin_prefix: str = "", seed: int | None = None) -> GeneratedCard:
    rng = _rng(seed)
    norm_country = normalize_country(country)
    norm_brand = normalize_brand(brand)
    prefix = "".join(ch for ch in str(bin_prefix or "") if ch.isdigit())
    if prefix:
        if not 4 <= len(prefix) <= 15:
            raise ValueError("custom BIN must contain 4-15 digits")
    else:
        try:
            prefix = rng.choice(COUNTRY_BRAND_BINS[norm_country][norm_brand])
        except KeyError as exc:
            raise ValueError(f"unsupported country/brand: {country}/{brand}") from exc
    body_len = 16 - len(prefix) - 1
    if body_len < 0:
        raise ValueError("custom BIN is too long for 16-digit card")
    body = "".join(str(rng.randrange(10)) for _ in range(body_len))
    check = luhn_check_digit(prefix + body)
    pan = prefix + body + check
    month = f"{rng.randrange(1, 13):02d}"
    year = f"{(datetime.now().year + 4) % 100:02d}"
    cvv = f"{rng.randrange(0, 1000):03d}"
    return GeneratedCard(
        pan=pan,
        formatted=format_pan(pan),
        expiry=f"{month}/{year}",
        cvv=cvv,
        country=norm_country,
        brand="MasterCard" if norm_brand == "mastercard" else "Visa",
        bin_prefix=prefix,
        luhn=luhn_valid(pan),
    )


def generate_cards(
    country: str = "jp",
    brand: str = "visa",
    *,
    count: int = 1,
    bin_prefix: str = "",
    seed: int | None = None,
) -> list[GeneratedCard]:
    count = max(1, min(500, int(count)))
    rng = _rng(seed)
    cards: list[GeneratedCard] = []
    for _ in range(count):
        item_seed = rng.randrange(2**32) if seed is not None else None
        cards.append(generate_card(country, brand, bin_prefix=bin_prefix, seed=item_seed))
    return cards


def card_lines(cards: Iterable[GeneratedCard]) -> list[str]:
    return [card.as_line() for card in cards]


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)
    gen = sub.add_parser("generate")
    gen.add_argument("--country", default="jp")
    gen.add_argument("--brand", default="visa")
    gen.add_argument("-n", "--count", type=int, default=1)
    gen.add_argument("--bin", dest="bin_prefix", default="")
    gen.add_argument("--seed", type=int, default=None)
    gen.add_argument("--json", action="store_true")
    args = parser.parse_args()
    if args.cmd == "generate":
        cards = generate_cards(args.country, args.brand, count=args.count, bin_prefix=args.bin_prefix, seed=args.seed)
        if args.json:
            print(json.dumps([card.as_dict() for card in cards], ensure_ascii=False, indent=2))
        else:
            for line in card_lines(cards):
                print(line)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
