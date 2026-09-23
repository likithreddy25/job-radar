from __future__ import annotations

import re
import unicodedata


_FORTUNE_500_ALIASES = {
    "alphabet": "Google",
    "alphabet inc": "Google",
    "alphabet, inc": "Google",
    "bank of america corporation": "Bank of America",
    "capital one financial": "Capital One",
    "capital one financial corporation": "Capital One",
    "charter communications": "Spectrum",
    "costco wholesale": "Costco",
    "dell technologies inc": "Dell Technologies",
    "eli lilly and company": "Eli Lilly",
    "exxon mobil": "ExxonMobil",
    "ford motor": "Ford",
    "general motors company": "General Motors",
    "goldman sachs group": "Goldman Sachs",
    "goldman sachs group inc": "Goldman Sachs",
    "hca healthcare inc": "HCA Healthcare",
    "international business machines": "IBM",
    "jones lang lasalle": "JLL",
    "jones lang lasalle incorporated": "JLL",
    "jpmorgan chase": "JPMorgan Chase",
    "jpmorgan chase and co": "JPMorgan Chase",
    "jpmorgan chase & co": "JPMorgan Chase",
    "loews": "Loews Corporation",
    "marathon petroleum corporation": "Marathon Petroleum",
    "meta": "Meta",
    "meta platforms": "Meta",
    "meta platforms inc": "Meta",
    "meta platforms, inc": "Meta",
    "phillips 66 company": "Phillips 66",
    "t mobile": "T-Mobile",
    "t mobile us": "T-Mobile",
    "t-mobile us inc": "T-Mobile",
    "t mobile us inc": "T-Mobile",
    "united parcel service": "UPS",
    "ups inc": "UPS",
    "verizon communications": "Verizon",
    "walt disney": "Disney",
    "walt disney company": "Disney",
}

_CORPORATE_SUFFIXES = {
    "co",
    "company",
    "corp",
    "corporation",
    "group",
    "holding",
    "holdings",
    "inc",
    "incorporated",
    "limited",
    "ltd",
    "llc",
    "plc",
}


def _fold_text(value: str) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.lower().replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _strip_suffix_tokens(folded: str) -> str:
    tokens = folded.split()
    while tokens and tokens[-1] in _CORPORATE_SUFFIXES:
        tokens.pop()
    while tokens and tokens[0] == "the":
        tokens.pop(0)
    return " ".join(tokens)


def normalize_company_name(name: str) -> str:
    folded = _fold_text(name)
    if not folded:
        return ""
    alias = _FORTUNE_500_ALIASES.get(folded)
    if alias:
        return alias
    stripped = _strip_suffix_tokens(folded)
    alias = _FORTUNE_500_ALIASES.get(stripped)
    if alias:
        return alias
    return name.strip()


def company_key(name: str) -> str:
    canonical = normalize_company_name(name)
    return _strip_suffix_tokens(_fold_text(canonical))
