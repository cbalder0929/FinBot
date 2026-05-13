"""Transaction categorizer.

Three layers, applied in order of precedence per row:

  1. **User overrides** — substring → category/type from
     ``category_overrides.json`` (if it exists). Highest priority so a
     household can teach the classifier its specific merchants without
     editing code.

  2. **Regex rules** — ordered, merchant-aware patterns in ``_RULES``.
     Earlier rules win, so list more specific patterns first.

  3. **Bank-supplied category normalization** — if the parser already
     captured a category from the source CSV (Discover's "Restaurants",
     Amex's "Fees & Adjustments-Fees & Adjustments"), map it onto our
     canonical vocabulary via ``_BANK_CATEGORY_MAP``. Anything we don't
     recognize falls back to ``Uncategorized``.

Public API:
  - ``categorize(item, is_debit) -> (category, type)``
  - ``apply_categorization(df) -> df`` — adds/overwrites ``category`` and
    ``type`` columns on the canonical-schema DataFrame.

``type`` is one of ``expense``, ``income``, or ``transfer``. Reporting
code excludes ``transfer`` rows from cashflow math so card payments and
Zelle moves don't double-count.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

import pandas as pd


# ---------------------------------------------------------------------------
# Canonical vocabulary
# ---------------------------------------------------------------------------
# Keep this list short and dashboard-friendly. New merchants should map
# onto one of these rather than introduce a new category.
CATEGORIES = (
    "Card Payment",       # transfer
    "Bank Transfer",      # transfer
    "Transfer",           # transfer (peer-to-peer)
    "Payroll",            # income
    "Interest Income",    # income
    "Refund",             # income
    "Groceries",
    "Dining",
    "Alcohol",
    "Gas",
    "Transit",
    "Travel",
    "Subscriptions",
    "Rent & Housing",
    "Utilities",
    "Insurance",
    "Shopping",
    "Clothing",
    "Health & Pharmacy",
    "Healthcare",
    "Fitness",
    "Personal Care",
    "Pet",
    "Entertainment",
    "Education",
    "Taxes & Government",
    "Charity",
    "Auto",
    "Cash",
    "Vending & Snacks",
    "Fees & Interest",
    "Uncategorized",
)


# ---------------------------------------------------------------------------
# Regex rules (ordered; first match wins)
#
# Conventions:
#   - Patterns run against the upper-cased item string.
#   - Prefer ``\bWORD\b`` (word boundaries) to avoid partial matches
#     like "BP " catching "BURGER" — but BP statements also print
#     "BP#16361905616", so we explicitly accept BP followed by # or
#     a digit.
# ---------------------------------------------------------------------------
_RULES: list[tuple[re.Pattern, str, str]] = [
    # ---- Transfers (most specific — must come before generic "PAYMENT") ----
    (re.compile(
        r"PAYMENT\s*-\s*THANK YOU|PAYMENT\s+THANK YOU|MOBILE PAYMENT|"
        r"AUTOPAY|ONLINE PAYMENT|MOBILE PMT|"
        r"DISCOVER DES:E[- ]?PAYMENT|DISCOVER\s+E[- ]?PAYMENT|"
        r"AMEX.*PAYMENT|AMERICAN EXPRESS.*PAYMENT|"
        r"CREDIT CARD PAYMENT|ONLINE BANKING PAYMENT TO CRD"
    ), "Card Payment", "transfer"),

    (re.compile(r"ZELLE|VENMO|CASH ?APP|PAYPAL|APPLE CASH"), "Transfer", "transfer"),

    (re.compile(r"\bWIRE\b|WIRE TRANSFER|ACH TRANSFER|TRANSFER TO|TRANSFER FROM"),
     "Bank Transfer", "transfer"),

    # ---- Income ----
    (re.compile(r"PAYROLL|\bDIRECT DEP\b|\bDIR DEP\b|ACH CREDIT.*PAYROLL|SALARY|WAGES"),
     "Payroll", "income"),

    (re.compile(r"INTEREST PAID|DIVIDEND|SAVINGS INTEREST|INT EARNED|INTEREST EARNED"),
     "Interest Income", "income"),

    (re.compile(r"REFUND|CREDIT VOUCHER|\bRETURN\b|TAX REFUND|RBT\s*\*"),
     "Refund", "income"),

    # ---- Groceries (most-specific food before general "Dining") ----
    (re.compile(
        r"WALMART|WAL[- ]?MART|TARGET\b|COSTCO|HEB\b|\bH\.E\.B\.|"
        r"KROGER|TRADER JOE|WHOLE FOODS|ALDI|PUBLIX|SAFEWAY|"
        r"JEWEL[- ]?OSCO|WEGMAN|MARIANO|SHOP[- ]?RITE|"
        r"FOOD LION|HARRIS TEETER|STOP\s*&?\s*SHOP|GIANT FOOD|"
        r"MEIJER|FRESH MARKET|SPROUTS|BURNHAM GROCERS|GROCERY|GROCERS"
    ), "Groceries", "expense"),

    # ---- Alcohol / Bars / Liquor ----
    (re.compile(
        r"LIQUORAMA|LIQUOR\b|WINE SHOP|TOTAL WINE|BINNY|"
        r"BREWERY|TAVERN|\bPUB\b|BAR\s*&\s*GRILL|COCKTAIL"
    ), "Alcohol", "expense"),

    # ---- Dining (restaurants, fast food, coffee, food delivery) ----
    (re.compile(
        r"MCDONALD|CHIPOTLE|STARBUCKS|DOORDASH|UBER ?EATS|GRUBHUB|"
        r"CHICK[- ]?FIL|TACO BELL|SUBWAY|PIZZA|DOMINO|PANERA|WENDY|"
        r"BURGER KING|CHILI'?S|APPLEBEE|OLIVE GARDEN|IHOP|DENNY|"
        r"POPEYES|KFC|PANDA EXPRESS|FIVE GUYS|JIMMY JOHN|CULVERS|"
        r"WHATABURGER|JACK IN THE BOX|SHAKE SHACK|NYC BAGEL|"
        r"DUNKIN|DD/BR|TIM HORTONS|CARIBOU|PEET'S|SCHLOTZSKY|"
        r"RESTAURANT|\bCAFE\b|COFFEE|BAGEL|\bDELI\b|BISTRO|DINER|"
        r"\bGRILL\b|EATERY|KITCHEN|\bTST\s*\*|\bSQ\s*\*|TASTE OF"
    ), "Dining", "expense"),

    # ---- Gas / Fuel ----
    (re.compile(
        r"SHELL\b|EXXON|CHEVRON|VALERO|\bBP\b|BP[#\d]|"
        r"\bMOBIL\b|CITGO|MARATHON|PILOT\b|CASEY|SUNOCO|"
        r"PHILLIPS 66|CONOCO|FUEL INC|GAS STATION"
    ), "Gas", "expense"),

    # ---- Transit (ground transport, parking, tolls) ----
    (re.compile(
        r"\bUBER\b(?! ?EATS)|LYFT|MTA[ /]|\bMETRO[- ]?(TRANSIT)?\b|"
        r"TRANSIT\b|\bPARKING\b|\bTOLL\b|IPASS|EZ[ -]?PASS|FASTRACK|"
        r"VENTRA|\bCTA\b|WMATA|\bBART\b|AMTRAK|GREYHOUND|"
        r"\bTAXI\b|CAB CO|BLINK BRIDGEPORT"
    ), "Transit", "expense"),

    # ---- Travel (air + hotels + booking sites) ----
    (re.compile(
        r"DELTA AIR|UNITED AIR|AMERICAN AIRL|SOUTHWEST AIR|JETBLUE|"
        r"ALASKA AIR|SPIRIT AIRL|FRONTIER AIR|HAWAIIAN AIR|AIRLINE|"
        r"AIRBNB|VRBO|MARRIOTT|HILTON|HYATT|\bIHG\b|SHERATON|RAMADA|"
        r"HOLIDAY INN|BEST WESTERN|EXPEDIA|HOTELS\.COM|BOOKING\.COM|"
        r"\bKAYAK\b|PRICELINE|TRIVAGO"
    ), "Travel", "expense"),

    # ---- Subscriptions / Streaming / Software ----
    (re.compile(
        r"NETFLIX|SPOTIFY|HULU|DISNEY\+|DISNEY PLUS|APPLE\.COM/BILL|"
        r"APPLE ONE|\bHBO\b|PEACOCK|PARAMOUNT|YOUTUBE PREMIUM|"
        r"YOUTUBE TV|AMAZON PRIME|PRIME VIDEO|NYTIMES|NEW YORK TIMES|"
        r"WSJ\.COM|WASHINGTON POST|NOTION|DROPBOX|ICLOUD|"
        r"GOOGLE STORAGE|ADOBE|MICROSOFT 365|MSFT\*|MS BILL|"
        r"OPENAI|ANTHROPIC|CHATGPT|CLAUDE\.AI|TWITCH|PATREON|"
        r"SUBSTACK|MEDIUM\.COM|TIDAL|PANDORA|AUDIBLE|SIRIUS ?XM"
    ), "Subscriptions", "expense"),

    # ---- Rent / Housing ----
    (re.compile(r"\bRENT\b|APARTMENT|PROPERTY MGMT|PROPERTY MANAGEMENT|\bLEASE\b|\bHOA\b|MORTGAGE|MORTG\b"),
     "Rent & Housing", "expense"),

    # ---- Utilities (telecom + power/gas/water) ----
    (re.compile(
        r"AT&T|VERIZON|T[- ]?MOBILE|TMOBILE|COMCAST|XFINITY|SPECTRUM|"
        r"\bCOX\b|CENTURY ?LINK|INTERNET|ELECTRIC\b|\bGAS CO\b|"
        r"WATER BILL|UTILIT(Y|IES)|CON ED|CONEDISON|PG&E|\bPGE\b|"
        r"DUKE ENERGY|NATIONAL GRID|ENBRIDGE|GOOGLE FIBER|\bFIOS\b"
    ), "Utilities", "expense"),

    # ---- Insurance ----
    (re.compile(
        r"GEICO|STATE FARM|PROGRESSIVE|ALLSTATE|LIBERTY MUTUAL|"
        r"FARMERS INS|TRAVELERS|\bUSAA\b|NATIONWIDE|MET[- ]?LIFE|"
        r"GUARDIAN|HUMANA|UNITED ?HEALTH(CARE)?|\bUHC\b|"
        r"BLUE\s*CROSS|AETNA|CIGNA|INSURANCE"
    ), "Insurance", "expense"),

    # ---- Vending / Workplace snacks (before general Shopping) ----
    (re.compile(r"CTLP\*|MARK VEND|MARKET@WORK|VENDING"),
     "Vending & Snacks", "expense"),

    # ---- Clothing / Apparel ----
    (re.compile(
        r"\bGAP\b|OLD NAVY|BANANA REPUBLIC|H&M|\bZARA\b|UNIQLO|"
        r"LULULEMON|NIKE|ADIDAS|UNDER ARMOUR|COACH OUTLET|COACH STORE|"
        r"MICHAEL KORS|KATE SPADE|\bUGG\b|FOOT LOCKER"
    ), "Clothing", "expense"),

    # ---- Shopping (online + big box) ----
    (re.compile(
        r"AMAZON|AMZN MKTP|AMZN\.COM|AMZN\*|EBAY|ETSY|WAYFAIR|"
        r"ZAPPOS|NORDSTROM|MACY|KOHL|DILLARD|BLOOMINGDALE|\bSAKS\b|"
        r"TJ ?MAXX|MARSHALLS|\bROSS\b|HOMEGOODS|HOME GOODS|"
        r"BED BATH|IKEA|HOME DEPOT|LOWES|LOWE'S|BEST BUY|BESTBUY|"
        r"MICROCENTER|NEWEGG|GAMESTOP|APPLE STORE|MICROSOFT STORE|"
        r"BARNES\s*&?\s*NOBLE|MERCHANDISE"
    ), "Shopping", "expense"),

    # ---- Health & Pharmacy ----
    (re.compile(r"\bCVS\b|WALGREEN|PHARMACY|RITE AID|DUANE READE|GOODRX"),
     "Health & Pharmacy", "expense"),

    # ---- Healthcare (providers) ----
    (re.compile(r"DOCTOR|CLINIC|HOSPITAL|URGENT CARE|DENTAL|DENTIST|"
                r"VISION CENTER|OPTOMETRIST|HEALTH INS|MEDICAL"),
     "Healthcare", "expense"),

    # ---- Fitness ----
    (re.compile(r"\bGYM\b|PLANET FITNESS|24 HOUR FITNESS|CROSSFIT|"
                r"YMCA|YWCA|FITNESS|ORANGETHEORY|EQUINOX|PURE BARRE|"
                r"SOULCYCLE|PELOTON"), "Fitness", "expense"),

    # ---- Personal Care ----
    (re.compile(r"SUPERCUTS|GREAT CLIPS|\bSALON\b|BARBER|\bSPA\b|"
                r"MASSAGE|NAIL\b"), "Personal Care", "expense"),

    # ---- Pet ----
    (re.compile(r"PETSMART|\bPETCO\b|\bCHEWY\b|VETERINAR|VET CLINIC|"
                r"\bVCA\b|BANFIELD"), "Pet", "expense"),

    # ---- Entertainment ----
    (re.compile(r"AMC THEATR|REGAL CINEMA|\bCINEMA\b|MOVIE THEAT|"
                r"TICKETMASTER|STUBHUB|LIVE NATION|EVENTBRITE|"
                r"STEAM(POWERED)?|PLAYSTATION|XBOX|NINTENDO|CLYBOURN CLUB"),
     "Entertainment", "expense"),

    # ---- Education ----
    (re.compile(r"COURSERA|UDEMY|UDACITY|\bEDX\b|KHAN ACADEMY|"
                r"\bSCHOOL\b|UNIVERSITY|COLLEGE|TUITION|BOOKSTORE"),
     "Education", "expense"),

    # ---- Taxes & Government ----
    (re.compile(r"\bIRS\b|US TREAS(URY)?|TAX PAYMENT|\bDMV\b|"
                r"DEPT OF MOTOR|CITY OF\b|STATE OF .* TAX"),
     "Taxes & Government", "expense"),

    # ---- Charity ----
    (re.compile(r"DONAT|CHARITY|GOFUNDME|RED CROSS|UNICEF|"
                r"SALVATION ARMY|UNITED WAY"), "Charity", "expense"),

    # ---- Auto (service / parts, not fuel) ----
    (re.compile(r"AUTOZONE|O[' ]?REILLY AUTO|ADVANCE AUTO|PEP BOYS|"
                r"JIFFY LUBE|MIDAS|MEINEKE|\bNTB\b|GOODYEAR|"
                r"FIRESTONE|CARMAX|CAR WASH|MECHANIC"),
     "Auto", "expense"),

    # ---- ATM / Cash ----
    (re.compile(r"ATM WITHDRAWAL|ATM CASH|CASH WITHDRAWAL|ATM\s*-"),
     "Cash", "expense"),

    # ---- Fees & Interest (general — checked late so merchant rules win) ----
    (re.compile(
        r"INTEREST CHARGE|FINANCE CHARGE|LATE FEE|"
        r"FOREIGN TRANSACTION FEE|ANNUAL FEE|RETURNED ITEM FEE|"
        r"OVERDRAFT|\bNSF\b FEE|MAINTENANCE FEE|SERVICE FEE|"
        r"TRANSACTION FEE|MEMBERSHIP FEE|\bMEMBER FEE\b|"
        r"FEES\s*&\s*ADJUSTMENTS"
    ), "Fees & Interest", "expense"),
]


# ---------------------------------------------------------------------------
# Bank-supplied category normalization
# ---------------------------------------------------------------------------
# When a parser pulls a category straight out of a bank's CSV export
# (Discover's "Restaurants", Amex's "Travel/ Entertainment", etc.), map
# it onto our canonical vocabulary so reports aren't fragmented across
# three spellings of "food".
#
# Keys are matched case-insensitively after stripping whitespace.
# Values are (canonical_category, type).
_BANK_CATEGORY_MAP: dict[str, tuple[str, str]] = {
    # Discover
    "restaurants": ("Dining", "expense"),
    "gasoline": ("Gas", "expense"),
    "supermarkets": ("Groceries", "expense"),
    "merchandise": ("Shopping", "expense"),
    "travel/ entertainment": ("Travel", "expense"),
    "travel / entertainment": ("Travel", "expense"),
    "services": ("Shopping", "expense"),
    "government services": ("Taxes & Government", "expense"),
    "payments and credits": ("Card Payment", "transfer"),
    "awards and rebate credits": ("Refund", "income"),
    "home improvement": ("Shopping", "expense"),
    "education": ("Education", "expense"),
    "automotive": ("Auto", "expense"),
    "medical services": ("Healthcare", "expense"),
    "fees": ("Fees & Interest", "expense"),
    "other/ miscellaneous": ("Uncategorized", "expense"),
    "other / miscellaneous": ("Uncategorized", "expense"),
    "miscellaneous": ("Uncategorized", "expense"),

    # Amex (uses "Category-Subcategory" form)
    "fees & adjustments-fees & adjustments": ("Fees & Interest", "expense"),
    "transportation-fuel": ("Gas", "expense"),
    "restaurant-bar & café": ("Dining", "expense"),
    "restaurant-restaurant": ("Dining", "expense"),
    "merchandise & supplies-groceries": ("Groceries", "expense"),
    "merchandise & supplies-general retail": ("Shopping", "expense"),
    "merchandise & supplies-pharmacies": ("Health & Pharmacy", "expense"),
    "communications-cable & internet": ("Utilities", "expense"),
    "communications-phone services": ("Utilities", "expense"),
    "entertainment-general attractions": ("Entertainment", "expense"),

    # Our own — passthrough so the map can be the single source of truth
    **{c.lower(): (c, "expense") for c in CATEGORIES if c not in (
        "Card Payment", "Bank Transfer", "Transfer",
        "Payroll", "Interest Income", "Refund", "Uncategorized",
    )},
    "card payment": ("Card Payment", "transfer"),
    "bank transfer": ("Bank Transfer", "transfer"),
    "transfer": ("Transfer", "transfer"),
    "payroll": ("Payroll", "income"),
    "interest income": ("Interest Income", "income"),
    "refund": ("Refund", "income"),
}


def _normalize_bank_category(raw: str, is_debit: bool) -> tuple[str, str] | None:
    """Return (canonical_category, type) for a bank-supplied label, or None."""
    if not raw:
        return None
    key = raw.strip().lower()
    if not key or key == "nan":
        return None
    if key in _BANK_CATEGORY_MAP:
        return _BANK_CATEGORY_MAP[key]
    # Unknown bank label — keep it but classify by sign so downstream
    # cashflow math still makes sense.
    return (raw.strip(), "expense" if is_debit else "income")


# ---------------------------------------------------------------------------
# User overrides — loaded fresh on each call so a session editing
# category_overrides.json sees changes without restarting the server.
# ---------------------------------------------------------------------------
_OVERRIDES_PATH = Path(__file__).resolve().parent / "category_overrides.json"


def _load_overrides() -> dict[str, tuple[str, str]]:
    if not _OVERRIDES_PATH.exists():
        return {}
    try:
        raw = json.loads(_OVERRIDES_PATH.read_text(encoding="utf-8"))
        return {k: (v["category"], v["type"]) for k, v in raw.items()}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def categorize(item: str, is_debit: bool) -> tuple[str, str]:
    """Return (category, type) for a single transaction description.

    Resolution order: user overrides → regex rules → ``Uncategorized``.
    ``is_debit`` only influences the default ``type`` for unmatched rows.
    """
    upper = (item or "").upper()
    overrides = _load_overrides()
    for substr, (cat, typ) in overrides.items():
        if substr.upper() in upper:
            return cat, typ
    for pattern, cat, typ in _RULES:
        if pattern.search(upper):
            return cat, typ
    return ("Uncategorized", "expense" if is_debit else "income")


def apply_categorization(df: pd.DataFrame) -> pd.DataFrame:
    """Fill ``category`` / ``type`` columns on every row.

    Precedence per row:
      1. ``categorize(item)`` — our rules + user overrides.
      2. If the rules return ``Uncategorized`` *and* the row carries a
         bank-supplied category, normalize that label and use it.
      3. Otherwise leave it as ``Uncategorized``.

    Step 2 means: a known merchant (e.g. STARBUCKS) is always classified
    by *our* rules, even if Discover's export disagrees — because cross-
    bank consistency matters more than the bank's hand-labeled
    granularity. Step 2 also means: when we have no rule (e.g. a niche
    local merchant), we still benefit from the bank's labeling.
    """
    df = df.copy()
    cats, types = [], []
    for _, row in df.iterrows():
        item = str(row.get("item", "") or "")
        existing_cat = str(row.get("category", "") or "").strip()
        debits = float(row.get("debits", 0) or 0)
        is_debit = debits > 0

        inferred_cat, inferred_type = categorize(item, is_debit)

        if inferred_cat != "Uncategorized":
            cats.append(inferred_cat)
            types.append(inferred_type)
            continue

        bank = _normalize_bank_category(existing_cat, is_debit)
        if bank is not None:
            cats.append(bank[0])
            types.append(bank[1])
            continue

        cats.append(inferred_cat)   # "Uncategorized"
        types.append(inferred_type)

    df["category"] = cats
    df["type"] = types
    return df
