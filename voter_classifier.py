import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import os
import re
import sys
import tempfile
from pathlib import Path

import pandas as pd
import pytesseract
from pdf2image import convert_from_path, pdfinfo_from_path
from PIL import Image
from pytesseract import TesseractNotFoundError


DEFAULT_POPPLER_PATH = r"C:\Release-26.02.0-0\poppler-26.02.0\Library\bin"
LOCAL_TESSDATA_DIR = Path(__file__).resolve().parent / "tessdata"
DEFAULT_TESSERACT_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)
DEFAULT_OCR_LANG = "hin"
DEFAULT_DPI = 300
DEFAULT_TESSERACT_CONFIG = "--oem 1 --psm 6"
DEFAULT_MAX_OCR_WORKERS = 20
DEFAULT_MAX_PAGE_WORKERS = 8
REPORT_COLUMNS = [
    "page",
    "serial_number",
    "name",
    "guardian_name",
    "house_number",
    "age",
    "gender",
    "religion_label",
    "caste_label",
    "review_status",
    "raw_text",
]


class SetupError(RuntimeError):
    pass


def get_poppler_path():
    if os.name != "nt":
        return None

    poppler_path = os.environ.get("POPPLER_PATH", DEFAULT_POPPLER_PATH).strip()
    if not os.path.isdir(poppler_path):
        raise SetupError(
            "Poppler was not found. Set POPPLER_PATH to the Poppler bin folder. "
            f"Expected folder: {DEFAULT_POPPLER_PATH}"
        )

    missing_tools = [
        tool
        for tool in ("pdftoppm.exe", "pdfinfo.exe")
        if not os.path.isfile(os.path.join(poppler_path, tool))
    ]
    if missing_tools:
        raise SetupError(
            f"Poppler folder is missing {', '.join(missing_tools)}: {poppler_path}"
        )

    os.environ["PATH"] = poppler_path + os.pathsep + os.environ.get("PATH", "")
    return poppler_path


def configure_tesseract():
    configured_path = os.environ.get("TESSERACT_CMD", "").strip()
    candidate_paths = (configured_path, *DEFAULT_TESSERACT_PATHS)

    for path in candidate_paths:
        if path and os.path.isfile(path):
            pytesseract.pytesseract.tesseract_cmd = path
            return path

    return None


def validate_setup():
    poppler_path = get_poppler_path()
    tesseract_path = configure_tesseract()
    os.environ.setdefault("OMP_THREAD_LIMIT", "1")
    if LOCAL_TESSDATA_DIR.is_dir():
        os.environ["TESSDATA_PREFIX"] = str(LOCAL_TESSDATA_DIR)

    try:
        pytesseract.get_tesseract_version()
    except TesseractNotFoundError as exc:
        raise SetupError(
            "Tesseract OCR is not installed or is not on PATH. Install it, then "
            "set TESSERACT_CMD to tesseract.exe if it is installed in a custom folder."
        ) from exc

    return {
        "poppler_path": poppler_path or "system PATH",
        "tesseract_path": tesseract_path or "system PATH",
    }


def worker_count(total_items, env_name="VOTER_OCR_WORKERS", default_limit=DEFAULT_MAX_OCR_WORKERS):
    if total_items <= 1:
        return 1

    configured = os.environ.get(env_name, "").strip()
    if configured:
        try:
            return max(1, min(total_items, int(configured)))
        except ValueError:
            pass

    return max(1, min(total_items, default_limit))


def default_page_worker_limit():
    cpu_count = os.cpu_count() or 4
    return max(2, min(DEFAULT_MAX_PAGE_WORKERS, cpu_count))


def pdf_page_count(pdf_path, poppler_path):
    info = pdfinfo_from_path(
        str(pdf_path),
        poppler_path=poppler_path if poppler_path != "system PATH" else None,
    )
    return int(info["Pages"])


def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


DEVANAGARI_NORMALIZE_MAP = str.maketrans(
    {
        "क़": "क",
        "ख़": "ख",
        "ग़": "ग",
        "ज़": "ज",
        "ड़": "ड",
        "ढ़": "ढ",
        "फ़": "फ",
        "य़": "य",
        "़": "",
    }
)


def normalize_name_text(value):
    return clean_text(value).lower().translate(DEVANAGARI_NORMALIZE_MAP)


DIGIT_NORMALIZE_MAP = str.maketrans(
    {
        "०": "0",
        "१": "1",
        "२": "2",
        "३": "3",
        "४": "4",
        "५": "5",
        "६": "6",
        "७": "7",
        "८": "8",
        "९": "9",
    }
)


def normalize_digits(value):
    value = clean_text(value).translate(DIGIT_NORMALIZE_MAP)
    return re.sub(r"(?<=\d)[|Il\u0964]|[|Il\u0964](?=\d|$)", "1", value)


def normalize_age(value):
    digits = re.sub(r"\D", "", normalize_digits(value))
    if not digits:
        return ""
    age = int(digits)
    if 18 <= age <= 120:
        return str(age)
    return ""


def has_voter_field(text):
    text = clean_text(text)
    return any(
        field in text
        for field in (
            "\u0928\u093e\u092e",
            "\u092a\u093f\u0924\u093e",
            "\u092a\u0924\u093f",
            "\u092e\u0915\u093e\u0928",
            "\u0906\u092f\u0941",
            "\u0932\u093f\u0902\u0917",
            "Name",
            "House",
            "Age",
            "Gender",
        )
    )


def first_match(patterns, text, flags=re.IGNORECASE):
    for pattern in patterns:
        match = re.search(pattern, text, flags)
        if match:
            return clean_field_value(match.group(1))
    return ""


FIELD_LABEL_PATTERN = (
    r"(?:"
    r"\u092e\u0924\u0926\u093e\u0924\u093e\s+\u0915\u093e\s+\u0928\u093e\u092e|"
    r"\u092a\u093f\u0924\u093e\s+\u0915\u093e\s+\u0928\u093e\u092e|"
    r"\u092a\u0924\u093f\s+\u0915\u093e\s+\u0928\u093e\u092e|"
    r"\u092e\u0915\u093e\u0928\s+\u0938\u0902\u0916\u094d\u092f\u093e|"
    r"\u0928\u093e\u092e|"
    r"\u0906\u092f\u0941|"
    r"\u0909\u092e\u094d\u0930|"
    r"\u0932\u093f\u0902\u0917|"
    r"Elector'?s?\s+Name|Father'?s?\s+Name|Husband'?s?\s+Name|Mother'?s?\s+Name|"
    r"House\s+Number|House\s+No|Name|Age|Years?|Gender|Sex"
    r")"
)


def clean_field_value(value):
    value = clean_text(value)
    value = re.sub(r"\[?\s*\u092b\u094b\u091f\u094b\s+\u0909\u092a\u0932\u092c\u094d\u0927.*$", "", value)
    value = re.sub(r"\s*\|.*$", "", value)
    value = re.split(rf"\s+(?={FIELD_LABEL_PATTERN}\s*[:\-\uff1a]?)", value, maxsplit=1, flags=re.IGNORECASE)[0]
    value = re.sub(rf"\s+{FIELD_LABEL_PATTERN}\s*$", "", value, flags=re.IGNORECASE)
    value = re.sub(r"^[\s:：\-\|,;]+|[\s:：\-\|,;]+$", "", value)
    return clean_text(value)


def extract_name(text):
    if re.search(
        r"\b(?:Father|Husband|Mother)'?s?\s+Name\b|(?:\u092a\u093f\u0924\u093e|\u092a\u0924\u093f)\s+\u0915\u093e\s+\u0928\u093e\u092e",
        text,
        re.IGNORECASE,
    ):
        return ""
    return first_match(
        (
            r"(?:\u092e\u0924\u0926\u093e\u0924\u093e\s+\u0915\u093e\s+\u0928\u093e\u092e|Elector'?s?\s+Name)\s*[:\-]?\s*([^\d|,;:]+)",
            r"(?:^|\s)(?:\u0928\u093e\u092e|Name)\s*[:：]\s*([^\d|,;:]+)",
        ),
        text,
    )


def extract_guardian(text):
    return first_match(
        (
            r"(?:\u092a\u093f\u0924\u093e\s+\u0915\u093e\s+\u0928\u093e\u092e|Father'?s?\s+Name)\s*[:\-]?\s*([^\d|,;:]+)",
            r"(?:\u092a\u0924\u093f\s+\u0915\u093e\s+\u0928\u093e\u092e|Husband'?s?\s+Name)\s*[:\-]?\s*([^\d|,;:]+)",
            r"(?:\u092e\u093e\u0924\u093e\s+\u0915\u093e\s+\u0928\u093e\u092e)\s*[:\-]?\s*([^\d|,;:]+)",
            r"(?:Mother'?s?\s+Name)\s*[:\-]?\s*([^\d|,;:]+)",
        ),
        text,
    )


def extract_house_number(text):
    house_number = first_match(
        (
            r"(?:\u092e\u0915\u093e\u0928\s+\u0938\u0902\u0916\u094d\u092f\u093e|House\s+Number|House\s+No)\s*[:;\-\uff1a]?\s*([A-Za-z0-9\u0966-\u096f|Il\u0964\/\-.]+)",
        ),
        text,
    )
    house_number = normalize_digits(house_number)
    house_number = re.sub(r"[^A-Za-z0-9\/\-.]", "", house_number)
    return house_number


def extract_age(text):
    text = normalize_digits(text)
    patterns = (
        r"(?:\u0906\u092f\u0941|\u0909\u092e\u094d\u0930|Age|Years?)\s*[:\-\uff1a]?\s*([0-9]{1,3})(?=\s*(?:\u0935\u0930\u094d\u0937|\u0932\u093f\u0902\u0917|Gender|Sex|\b))",
        r"(?:\u0906\u092f\u0941|\u0909\u092e\u094d\u0930|Age|Years?)\s*[:\-\uff1a]?\s*([0-9]{1,3})",
    )
    for pattern in patterns:
        for match in re.finditer(pattern, text, re.IGNORECASE):
            age = normalize_age(match.group(1))
            if age:
                return age
    return ""


def extract_gender(text):
    return first_match(
        (r"(?:\u0932\u093f\u0902\u0917|Gender|Sex)\s*[:\-]?\s*([A-Za-z\u0900-\u097F]+)",),
        text,
    )


def extract_serial_number(text):
    return first_match((r"^\s*(\d{1,5})\b",), text, flags=0)


# ==================== RELIGION & CASTE CLASSIFICATION (Expanded) ====================

RELIGION_KEYWORDS = {
   "MUSLIM":[

    # Common Devanagari spellings/transliterations seen in Hindi electoral rolls.
    "अब्दुल", "अब्दुल्ला", "अब्दुल्लाह", "अबरार", "अदील", "अदनान", "अफजल", "अफसर",
    "अफरीन", "अफशां", "अहमद", "अहसान", "अजहर", "अजीज", "अजरा", "अकरम", "अकबर",
    "अख्तर", "अली", "अलिया", "अल्ताफ", "अमान", "अमीना", "अमीन", "आमिर", "आमिरा",
    "अमर", "अम्मार", "अनस", "अनीस", "अनीसा", "अंजुम", "अंसारी", "अनवर", "आफताब",
    "आसिफ", "आसमा", "आयशा", "आयेशा", "अयूब", "आजम", "बानो", "बेग", "बेगम",
    "बिलाल", "बिलकिस", "बुशरा", "दाऊद", "दावूद", "एहसान", "एजाज", "इलियास",
    "फईम", "फहीम", "फहद", "फैजान", "फैसल", "फैजल", "फखरी", "फराज", "फरहान",
    "फरीद", "फरीदा", "फरीहा", "फारिया", "फारूक", "फारूकी", "फरजाना", "फरजाना",
    "फातिमा", "फातिमाह", "फिरोज", "फिरदौस", "फौजिया", "फुरकान", "गनी", "गजाला",
    "गुलाम", "गुलनाज", "गुलजार", "गुलशन", "हबीब", "हबीबा", "हफीज", "हाफिज",
    "हैदर", "हलीमा", "हमीद", "हमजा", "हनीफ", "हारून", "हाशमी", "हसन", "हिना",
    "हुसैन", "इब्राहिम", "इदरीस", "इफ्तिखार", "इकराम", "इलियास", "इमदाद", "इमरान",
    "इनायत", "इकबाल", "इकरा", "इरफान", "इसहाक", "इशरत", "इस्लाम", "इस्माइल",
    "जब्बार", "जाफरी", "जलाल", "जमाल", "जमील", "जमीला", "जन्नत", "जावेद", "जुनैद",
    "करीम", "कलीम", "कमाल", "कामरान", "काशिफ", "काजमी", "खदीजा", "खालिद", "खान",
    "खुर्शीद", "कुलसुम", "लतीफ", "लुबना", "लुकमान", "मदीना", "माहिरा", "महमूद",
    "महबूब", "मेहर", "मेहबूब", "महमूद", "मरियम", "मरयम", "मसूद", "मकसूदन",
    "मेमन", "मीर", "मिर्जा", "मिस्बाह", "मोहसिन", "मोमिन", "मुबारक", "मुदस्सिर",
    "मुगल", "मुजाहिद", "मुख्तार", "मुनीर", "मुनव्वर", "मुनवर", "मुनव्वरी", "मुमताज",
    "मुराद", "मुर्तजा", "मुश्ताक", "मुस्तफा", "नदीम",
    "नईम", "नफीसा", "नरगिस", "नसीम", "नसीर", "नसरीन", "नवाज", "नाज", "नाजिया",
    "नाजमा", "नाजनीन", "निलोफर", "नूर", "नूरानी", "नुसरत", "ओबैद", "ओमर", "उस्मान",
    "परवीन", "परवेज", "पठान", "कादिर", "कमर", "कासिम", "कुरैशी", "राबिया", "रफिया",
    "रहीला", "रईस", "रईसा", "रहीम", "रहमान", "राशिद", "राशिदा", "रजा", "रजवी",
    "रेहाना", "रेहमान", "रियाज", "रिजवी", "रिजवान", "रूबी", "रुबी", "रूबीना",
    "रुबिना", "रुकसाना", "रुखसाना", "रुमाना", "सबा", "सबीना", "साबिर", "सादिया",
    "सईद", "सफिया", "सकीना", "सलीम", "सलीमा", "सलमा", "सलमान", "समीर", "सना",
    "सानिया", "सरफराज", "सैयद", "शबाना", "शब्बीर", "शबनम", "शाहिद", "शेख", "शेख़",
    "शाइस्ता", "शाकिर", "शम्स", "शरीफ", "शरीफ़", "शाजिया", "शहनाज", "शोएब", "सोहेल",
    "सुफी", "सुल्तान", "सुमैया", "तबस्सुम", "ताहिर", "तनवीर", "तारिक", "तसनीम",
    "उमैर", "उमर", "उस्मान", "उजैर", "उजमा", "वाहिद", "वसीम", "याह्या", "याकूब",
    "यासमीन", "यासीन", "यासिर", "युसुफ", "जफर", "जहीर", "जाहिद", "जाहिदा", "जहरा", "जैनब",
    "जाकिर", "जमीर", "जरीना", "जीनत", "जिया", "जुबैर", "जुल्फिकार", "जुम्मा", "सवाना",
    "सवनम", "निसार", "नूरजहां", "नूरजहाँ", "रज्जाक", "रजाक", "रज़्ज़ाक", "रुस्तम",
    "शकील", "शमीना", "इसरार", "अलीजान", "तसवीरन",
    "ज़रीना", "फ़ईम", "फ़हीम", "फ़ैजान", "फ़ारूक", "फ़िरोज", "शरीफ़", "शेख़", "रज़्ज़ाक",
]
,

  "Sikh": [
        # Common Sikh Surnames
        "singh", "kaur", "grewal", "sidhu", "bains", "gill", "dhillon", "sandhu", "brar", 
        "randhawa", "cheema", "mann", "aujla", "bhullar", "chahal", "dosanjh", "johal", 
        "kang", "pawar", "rahal", "sahi", "sekhon", "sohal", "thind", "virk", "walia", 
        "bajwa", "bedi", "sodhi", "chopra", "kapoor", "khurana", "ahuja", "arora", 
        "bhatia", "chawla", "duggal", "grover", "handa", "jaggi", "kalra", "khanna", 
        "luthra", "malhotra", "mehta", "nanda", "oberoi", "puri", "sachdeva", "sethi", 
        "tandon", "vohra", "aggarwal", "bansal", "gupta",
        
        # Common Sikh First Names (to improve detection)
        "gurpreet", "harpreet", "manpreet", "jaspreet", "amritpal", "balwinder", "davinder",
        "inderjit", "jaspinder", "karamjit", "kuldeep", "lovepreet", "mandeep", "navdeep",
        "parminder", "rajinder", "ranjit", "sandeep", "sukhdeep", "sukhwinder", "tarandeep",
        "gurbir", "harbir", "jagbir", "kulbir", "ranbir", "sarabjit", "simran", "harman",
        "gurman", "jasman", "kirandeep", "prabhdeep"
    ],
"Christian": [
        # Common Christian Surnames
        "masih", "toppo", "minz", "lakra", "kerketta", "xess", "barla", "ekka", "kujur",
        "tirkey", "beck", "soreng", "kindo", "bodra", "herenz", "horo", "murmu", "soren",
        "hembrom", "marandi", "tudu", "majhi", "baski", "hansda",
        
        # Common Christian First Names & Full Names
        "thomas", "john", "mary", "joseph", "peter", "paul", "david", "michael", "anthony",
        "james", "andrew", "philip", "stephen", "george", "francis", "robert", "richard",
        "patrick", "vincent", "sebastian", "gabriel", "raphael", "christopher", "daniel",
        "samuel", "benjamin", "isaac", "abraham", "jacob", "luke", "mark", "matthew",
        "simon", "timothy", "jerome", "augustine", "agnes", "catherine", "theresa", "margaret",
        "elizabeth", "ann", "rose", "victoria", "grace", "mercy", "hope", "faith", "charity",
        "lourdes", "fatima", "rosario", "anita", "sunita", "rani", "sister", "father"
    ],
}

RELIGION_KEYWORDS["MUSLIM"].extend(
    [
        "आलम",
        "असफाक",
        "अशफाक",
        "नाजिश",
        "शमीम",
        "शम्सुद्दीन",
        "शमसुद्दीन",
        "शामसुद्दीन",
    ]
)

MUSLIM_STRONG_DEVANAGARI_EXTENSIONS = {
    # Surnames, titles, and community names that are strong evidence in Hindi OCR.
    "अंसारी", "खान", "खां", "बेग", "बेगम", "सैयद", "सय्यद", "शेख", "शेख़",
    "पठान", "कुरैशी", "कुरेशी", "काजमी", "काज़मी", "रिजवी", "रज़वी", "जाफरी",
    "जाफ़री", "हाशमी", "उस्मानी", "मंसूरी", "नकवी", "नक़वी", "मुगल", "मेमन",
    "मुसलमान", "मौलाना", "मौलवी", "हाजी", "मियाँ", "मियां",

    # Common male names and compound-name parts.
    "मोहम्मद", "मोहमद", "मुहम्मद", "मुहम्मद", "मोहम्मद", "मो", "मौ",
    "अब्दुल", "अब्दुल्ला", "अब्दुल्लाह", "अहमद", "अहमद", "महमूद", "मेहमूद",
    "अली", "हसन", "हुसैन", "हुसैन", "शरीफ", "शरीफ़", "शरीफुद्दीन", "दीन",
    "फईम", "फहीम", "फैजान", "फैसल", "फारूक", "फारूकी", "फिरोज", "फुरकान",
    "इकबाल", "इरफान", "इस्लाम", "इस्माइल", "इमरान", "इलियास", "इब्राहिम",
    "इदरीस", "इसहाक", "इसरार", "इश्तियाक", "इफ्तिखार", "इकराम", "इनायत",
    "जावेद", "जुबैर", "जुनैद", "जाकिर", "जमील", "जमाल", "जलाल", "जब्बार",
    "करीम", "कलीम", "कामरान", "काशिफ", "खालिद", "लतीफ", "लुकमान", "मसूद",
    "मकसूद", "मकसूदन", "मोहसिन", "मोमिन", "मुबारक", "मुदस्सिर", "मुजाहिद",
    "मुख्तार", "मुनीर", "मुनव्वर", "मुनवर", "मुराद", "मुर्तजा", "मुश्ताक",
    "मुस्तफा", "नदीम", "नईम", "नसीम", "नसीर", "नवाज", "निसार", "परवेज",
    "कादिर", "कासिम", "रईस", "रफीक", "रहीम", "रहमान", "राशिद", "रियाज",
    "रिजवान", "साबिर", "सईद", "सलीम", "सलमान", "सरफराज", "शब्बीर", "शकील",
    "शाकिर", "शम्स", "शम्सुद्दीन", "शमसुद्दीन", "शामसुद्दीन", "शामशुद्दीन",
    "शमशुद्दीन", "शमीम", "शाहिद", "शोएब", "सोहेल", "ताहिर", "तनवीर",
    "तारिक", "उमैर", "उमर", "उस्मान", "उजैर", "वाहिद", "वसीम", "याकूब",
    "यासीन", "यासिर", "युसुफ", "जफर", "जहीर", "जाहिद", "जिया", "रज्जाक",
    "रजाक", "रुस्तम", "अलीजान", "जुम्मा", "आलम", "असफाक", "अशफाक",

    # Common female names that are strong evidence unless listed as ambiguous below.
    "आयशा", "आयेशा", "अमीना", "अनीसा", "अफरीन", "अफशां", "फातिमा", "फातिमाह",
    "फरीदा", "फरीहा", "फरजाना", "फौजिया", "गुलनाज", "हलीमा", "खदीजा",
    "कुलसुम", "लुबना", "माहिरा", "मरियम", "मरयम", "नफीसा", "नरगिस",
    "नसरीन", "नाजिया", "नाजमा", "नाजनीन", "निलोफर", "परवीन", "राबिया",
    "रफिया", "रहीला", "रईसा", "राशिदा", "रेहाना", "रूबीना", "रुकसाना",
    "रुखसाना", "सबीना", "सादिया", "सफिया", "सकीना", "सलीमा", "सलमा",
    "शबाना", "शबनम", "शमीना", "शाइस्ता", "शाजिया", "शहनाज", "सुमैया",
    "तबस्सुम", "तसनीम", "उजमा", "यासमीन", "जाहिदा", "जहरा", "जैनब",
    "जरीना", "जीनत", "तसवीरन", "नाजिश",

    # OCR/roll variants often seen in Hindi PDFs.
    "अहमद", "अहमद", "हनीफ", "हनीफ़", "हफीज", "हाफिज", "हाफ़िज", "रहमत",
    "नूरजहां", "नूरजहाँ", "नूरजंहा", "नूरजहाँ", "नूर आलम", "शमीम जहां",
    "शमीम जहा", "रज़्ज़ाक", "रज़्ज़ाक", "रज्‍जाक",
}

RELIGION_KEYWORDS["MUSLIM"].extend(sorted(MUSLIM_STRONG_DEVANAGARI_EXTENSIONS))

CASTE_KEYWORDS = {

    "General": [
        "sharma","mishra","tiwari","tripathi","dwivedi","rajput","chauhan",
        "trivedi","chaturvedi","upadhyay","pandey","pathak",
        "joshi","bhatt","purohit","vaidya","dikshit",
        "agrawal","agarwal","bansal","mittal","goel",
        "garg","jain","maheshwari","somani","birla",
        "khandelwal","pareek","vyas","kushwaha",
        "शर्मा","मिश्रा","तिवारी","त्रिपाठी","द्विवेदी","राजपूत","चौहान",
        "पांडे","पाण्डे","पाठक","जोशी","भट्ट","पुरोहित","दीक्षित",
        "अग्रवाल","बंसल","मित्तल","गोयल","गर्ग","जैन","व्यास"
    ],

    "OBC": [
        "yadav","ahir","kurmi","patel","maurya",
        "saini","lodhi","pal","nishad","kewat","kahar",
        "rajbhar","gujjar","gurjar","kamboj","shakya",
        "katiyar","tomar","solanki","jaiswal","kori",
        "gadariya","prajapati","kumhar","nai","teli",
        "kandu","barai","bari","bind","chaudhary","choudhary",
        "यादव","अहीर","कुर्मी","पटेल","मौर्य","सैनी","लोधी","पाल",
        "निषाद","केवट","कहार","राजभर","गुर्जर","कम्बोज","शाक्य",
        "कटियार","तोमर","सोलंकी","जायसवाल","कोरी","चौधरी",
        "प्रजापति",
        "कुम्हार","नाई","तेली","बिंद"
    ],

    "SC": [
        "jatav","chamar","pasi","paswan","valmiki",
        "bairwa","dhobi","khatik","raidas","ravidas",
        "mehtar","dom","dhanuk","kureel","kori",
        "baudh","balmiki",
        "जाटव","चमार","पासी","पासवान","वाल्मीकि","बाल्मीकि",
        "बैरवा","धोबी","खटीक","रैदास","रविदास","मेहतर","डोम",
        "धानुक","कुरील","कोरी","बौद्ध"
    ],

    "ST": [
        "gond","bhil","bhilala","meena","mina",
        "munda","oraon","lakra","toppo","minz",
        "kerketta","ekka","tirkey","xalxo","kindo",
        "ho","santhal","murmu","hansda","soren",
        "tudu","kisku","hembram","besra","marandi",
        "गोंड","भील","भीलाला","मीणा","मीना","मुंडा","उरांव",
        "लाकड़ा","टोप्पो","मिंज","केरकेट्टा","एक्का","तिर्की",
        "संथाल","मुर्मू","हांसदा","सोरेन","मरांडी"
    ]
}

COMMON_NAME_TOKENS = {
    "singh",
    "kumar",
    "devi",
    "rani",
    "lal",
    "ram",
    "name",
    "father",
    "mother",
    "husband",
    "wife",
    "सिंह",
    "कुमार",
    "देवी",
    "रानी",
    "लाल",
    "राम",
    "नाम",
    "का",
    "की",
    "के",
    "पति",
    "पिता",
    "माता",
    "पत्नी",
}


AMBIGUOUS_RELIGION_TOKENS = {
    # Common across communities; do not use a single token here as religion evidence.
    "amar",
    "aaryan",
    "aryan",
    "arman",
    "arsh",
    "azad",
    "chaudhry",
    "choudhary",
    "dar",
    "gulshan",
    "hana",
    "hina",
    "kamal",
    "malik",
    "mannat",
    "mehak",
    "muskaan",
    "noor",
    "raj",
    "rani",
    "reshma",
    "rubi",
    "ruby",
    "sana",
    "sandeep",
    "sara",
    "sameer",
    "simran",
    "suhana",
    "tania",
    "अमर",
    "आर्यन",
    "अरमान",
    "अर्श",
    "आजाद",
    "चौधरी",
    "गुलशन",
    "हिना",
    "कमल",
    "कमाल",
    "मलिक",
    "मन्नत",
    "मेहक",
    "मुस्कान",
    "नूर",
    "राज",
    "रानी",
    "रेशमा",
    "रुबी",
    "रूबी",
    "सना",
    "संदीप",
    "सारा",
    "समीर",
    "सिमरन",
    "सुहाना",
    "तानिया",
    # Christian list contains several broadly used Indian names.
    "anita",
    "sunita",
    "mary",
    "rani",
    "अनीता",
    "सुनीता",
}


CASTE_KEYWORDS["General"].extend(
    [
        "\u0936\u0930\u094d\u092e\u093e",
        "\u092e\u093f\u0936\u094d\u0930\u093e",
        "\u0924\u093f\u0935\u093e\u0930\u0940",
        "\u0924\u094d\u0930\u093f\u092a\u093e\u0920\u0940",
        "\u0926\u094d\u0935\u093f\u0935\u0947\u0926\u0940",
        "\u0930\u093e\u091c\u092a\u0942\u0924",
        "\u091a\u094c\u0939\u093e\u0928",
        "\u092a\u093e\u0902\u0921\u0947",
        "\u092a\u093e\u0923\u094d\u0921\u0947",
        "\u092a\u093e\u0920\u0915",
        "\u091c\u094b\u0936\u0940",
        "\u092d\u091f\u094d\u091f",
        "\u092a\u0941\u0930\u094b\u0939\u093f\u0924",
        "\u0926\u0940\u0915\u094d\u0937\u093f\u0924",
        "\u0905\u0917\u094d\u0930\u0935\u093e\u0932",
        "\u092c\u0902\u0938\u0932",
        "\u092e\u093f\u0924\u094d\u0924\u0932",
        "\u0917\u094b\u092f\u0932",
        "\u0917\u0930\u094d\u0917",
        "\u091c\u0948\u0928",
        "\u0935\u094d\u092f\u093e\u0938",
    ]
)

CASTE_KEYWORDS["OBC"].extend(
    [
        "\u092f\u093e\u0926\u0935",
        "\u0905\u0939\u0940\u0930",
        "\u0915\u0941\u0930\u094d\u092e\u0940",
        "\u092a\u091f\u0947\u0932",
        "\u092e\u094c\u0930\u094d\u092f",
        "\u0938\u0948\u0928\u0940",
        "\u0932\u094b\u0927\u0940",
        "\u092a\u093e\u0932",
        "\u0928\u093f\u0937\u093e\u0926",
        "\u0915\u0947\u0935\u091f",
        "\u0915\u0939\u093e\u0930",
        "\u0930\u093e\u091c\u092d\u0930",
        "\u0917\u0941\u0930\u094d\u091c\u0930",
        "\u0936\u093e\u0915\u094d\u092f",
        "\u0924\u094b\u092e\u0930",
        "\u0915\u094b\u0930\u0940",
        "\u092a\u094d\u0930\u091c\u093e\u092a\u0924\u093f",
        "\u0915\u0941\u092e\u094d\u0939\u093e\u0930",
        "\u0928\u093e\u0908",
        "\u0924\u0947\u0932\u0940",
        "\u092c\u093f\u0902\u0926",
    ]
)

CASTE_KEYWORDS["SC"].extend(
    [
        "\u091c\u093e\u091f\u0935",
        "\u091a\u092e\u093e\u0930",
        "\u092a\u093e\u0938\u0940",
        "\u092a\u093e\u0938\u0935\u093e\u0928",
        "\u0935\u093e\u0932\u094d\u092e\u0940\u0915\u093f",
        "\u092c\u093e\u0932\u094d\u092e\u0940\u0915\u093f",
        "\u0927\u094b\u092c\u0940",
        "\u0916\u091f\u0940\u0915",
        "\u0930\u0948\u0926\u093e\u0938",
        "\u0930\u0935\u093f\u0926\u093e\u0938",
        "\u092e\u0947\u0939\u0924\u0930",
        "\u0921\u094b\u092e",
        "\u0927\u093e\u0928\u0941\u0915",
        "\u0915\u0941\u0930\u0940\u0932",
        "\u092c\u094c\u0926\u094d\u0927",
    ]
)

CASTE_KEYWORDS["ST"].extend(
    [
        "\u0917\u094b\u0902\u0921",
        "\u092d\u0940\u0932",
        "\u092d\u0940\u0932\u093e\u0932\u093e",
        "\u092e\u0940\u0923\u093e",
        "\u092e\u0940\u0928\u093e",
        "\u092e\u0941\u0902\u0921\u093e",
        "\u0909\u0930\u093e\u0902\u0935",
        "\u0932\u093e\u0915\u0921\u093c\u093e",
        "\u091f\u094b\u092a\u094d\u092a\u094b",
        "\u092e\u093f\u0902\u091c",
        "\u090f\u0915\u094d\u0915\u093e",
        "\u0924\u093f\u0930\u094d\u0915\u0940",
        "\u0938\u0902\u0925\u093e\u0932",
        "\u092e\u0941\u0930\u094d\u092e\u0942",
        "\u0939\u093e\u0902\u0938\u0926\u093e",
        "\u0938\u094b\u0930\u0947\u0928",
        "\u092e\u0930\u093e\u0902\u0921\u0940",
    ]
)


def name_tokens(name):
    return set(re.findall(r"[a-z']+|[\u0900-\u097F]+", normalize_name_text(name)))


def has_keyword_token(name, keywords, ignore_common=False, skip_tokens=None):
    tokens = name_tokens(name)
    skip_tokens = {normalize_name_text(token) for token in (skip_tokens or set())}
    for keyword in keywords:
        keyword = normalize_name_text(keyword)
        if ignore_common and keyword in COMMON_NAME_TOKENS:
            continue
        if keyword in skip_tokens:
            continue
        if keyword in tokens:
            return True
    return False


def classify_religion(*values: str) -> str:
    name = clean_text(" ".join(clean_text(value) for value in values if value))
    if not name:
        return "Unknown"
    for religion, keywords in RELIGION_KEYWORDS.items():
        if has_keyword_token(
            name,
            keywords,
            ignore_common=True,
            skip_tokens=AMBIGUOUS_RELIGION_TOKENS,
        ):
            return religion
    return "Hindu / Other"


def classify_caste(name: str) -> str:
    if not name:
        return "Unclassified"
    for caste, keywords in CASTE_KEYWORDS.items():
        if has_keyword_token(name, keywords, ignore_common=True):
            return caste
    return "Unclassified"


def classify_review_status(name, raw_text):
    name = clean_text(name)
    if not name:
        return "needs_review"
    if len(name) < 3 or any(char.isdigit() for char in name):
        return "needs_review"
    return "extracted"


def contains_non_voter_text(record):
    text = clean_text(
        " ".join(
            str(record.get(field) or "")
            for field in ("name", "guardian_name", "raw_text")
        )
    )
    non_voter_patterns = (
        r"\u0928\u093f\u0930\u094d\u0935\u093e\u091a\u0915\s+\u0928\u093e\u092e\u093e\u0935\u0932\u0940",
        r"\u092e\u0924\u0926\u093e\u0928\s+\u0938\u094d\u0925\u0932",
        r"\u0905\u0928\u0941\u092d\u093e\u0917\s+\u0938\u0902\u0916\u094d\u092f\u093e",
        r"\u092d\u093e\u0917\s+\u0938\u0902\u0916\u094d\u092f\u093e",
        r"\u0915\u0941\u0932\s+\u092a\u0943\u0937\u094d\u0920",
        r"\b(?:polling|section|electoral|revision|assembly)\b",
    )
    return any(re.search(pattern, text, re.IGNORECASE) for pattern in non_voter_patterns)


def is_page_furniture_line(text):
    text = clean_text(text)
    if not text:
        return True

    patterns = (
        r"\u0928\u093f\u0930\u094d\u0935\u093e\u091a\u0915\s+\u0928\u093e\u092e\u093e\u0935\u0932\u0940",
        r"\u0905\u0928\u0941\u092d\u093e\u0917\s+\u0938\u0902\u0916\u094d\u092f\u093e",
        r"\u092d\u093e\u0917\s+\u0938\u0902\u0916\u094d\u092f\u093e",
        r"\u0905\u0930\u094d\u0939\u0924\u093e\s+\u0926\u093f\u0928\u093e\u0902\u0915",
        r"\u092a\u094d\u0930\u0915\u093e\u0936\u0928\s+\u0915\u0940\s+\u0924\u093f\u0925\u093f",
        r"\u0915\u0941\u0932\s+\u092a\u0943\u0937\u094d\u0920",
        r"\b(?:electoral|revision|assembly)\b",
    )
    if any(re.search(pattern, text, re.IGNORECASE) for pattern in patterns):
        return True

    if not has_voter_field(text) and re.search(r"\u092b\u094b\u091f\u094b\s+\u0909\u092a\u0932\u092c\u094d\u0927", text):
        return True

    return False


def is_valid_voter_record(record):
    if contains_non_voter_text(record):
        return False
    if not clean_text(record.get("name")):
        return False

    has_age_or_gender = bool(clean_text(record.get("age")) or clean_text(record.get("gender")))
    has_guardian_or_house = bool(
        clean_text(record.get("guardian_name")) or clean_text(record.get("house_number"))
    )
    return has_age_or_gender and has_guardian_or_house


def finalize_voter(record):
    if not record or not record.get("name"):
        return None
    record["religion_label"] = classify_religion(record["name"], record.get("guardian_name"))
    record["caste_label"] = classify_caste(record["name"])
    if not is_valid_voter_record(record):
        return None
    required_fields = ("name", "guardian_name", "house_number", "age", "gender")
    if all(clean_text(record.get(field)) for field in required_fields):
        record["review_status"] = classify_review_status(record["name"], record["raw_text"])
    else:
        record["review_status"] = "needs_review"
    return record


def blank_voter(page_number):
    return {
        "page": page_number,
        "serial_number": "",
        "name": "",
        "guardian_name": "",
        "house_number": "",
        "age": "",
        "gender": "",
        "religion_label": "",
        "caste_label": "",
        "review_status": "needs_review",
        "raw_text": "",
    }


def repeated_values(text, marker_pattern):
    text = clean_text(text)
    matches = list(re.finditer(marker_pattern, text, re.IGNORECASE))
    values = []

    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        value = clean_field_value(text[match.end() : end])
        if value:
            values.append(value)

    return values


def extract_repeated_names(text):
    text = clean_text(text)
    name_marker = r"(?<!\u0915\u093e\s)(?:^|\s)(?:\u0928\u093e\u092e|Name)\s*[:\-\uff1a]?"
    if len(list(re.finditer(name_marker, text, re.IGNORECASE))) < 2:
        return []
    return repeated_values(text, name_marker)


def extract_repeated_guardians(text):
    return repeated_values(
        text,
        r"(?:^|\s)(?:\u092a\u093f\u0924\u093e\s+\u0915\u093e\s+\u0928\u093e\u092e|\u092a\u0924\u093f\s+\u0915\u093e\s+\u0928\u093e\u092e|Father'?s?\s+Name|Husband'?s?\s+Name|Mother'?s?\s+Name)\s*[:\-]?",
    )


def extract_repeated_house_numbers(text):
    return [
        clean_text(match.group(1))
        for match in re.finditer(
            r"(?:\u092e\u0915\u093e\u0928\s+\u0938\u0902\u0916\u094d\u092f\u093e|House\s+Number|House\s+No)\s*[:\-]?\s*([A-Za-z0-9\u0966-\u096f\/\-]+)",
            text,
            re.IGNORECASE,
        )
    ]


def extract_repeated_age_gender(text):
    text = normalize_digits(text)
    pairs = []
    for match in re.finditer(
        r"(?:\u0906\u092f\u0941|\u0909\u092e\u094d\u0930|Age|Years?)\s*[:\-\uff1a]?\s*([0-9]{1,3})\s*(?:\u0935\u0930\u094d\u0937|yrs?|years?)?\s*(?:\u0932\u093f\u0902\u0917|Gender|Sex)\s*[:\-\uff1a]?\s*([A-Za-z\u0900-\u097F]+)",
        text,
        re.IGNORECASE,
    ):
        pairs.append((normalize_age(match.group(1)), clean_text(match.group(2))))
    return pairs


def line_text_from_ocr_group(group):
    return clean_text(" ".join(str(text) for text in group.sort_values("left")["text"] if str(text) != "nan"))


def card_grid_bounds(width, height):
    x_starts = [0.02 * width, 0.34 * width, 0.658 * width]
    cell_width = 0.318 * width
    y_start = 0.035 * height
    row_pitch = 0.0933 * height
    cell_height = 0.088 * height
    return x_starts, cell_width, y_start, row_pitch, cell_height


def fallback_serial_for_card(page_number, row_index, column_index):
    if page_number < 3:
        return ""
    return str((page_number - 3) * 30 + row_index * 3 + column_index + 1)


def extract_card_serial(card_text, page_number, row_index, column_index):
    first_line = normalize_digits(card_text.splitlines()[0] if card_text.splitlines() else "")
    match = re.search(r"\b(\d{1,5})\b", first_line)
    if match:
        serial = int(match.group(1))
        if 1 <= serial <= 99999:
            return str(serial)
    return fallback_serial_for_card(page_number, row_index, column_index)


def parse_voter_card_text(card_text, page_number, row_index, column_index):
    if not has_voter_field(card_text):
        return None

    record = blank_voter(page_number)
    record["raw_text"] = clean_text(card_text)
    record["serial_number"] = extract_card_serial(card_text, page_number, row_index, column_index)

    for line in card_text.splitlines():
        record["name"] = record["name"] or extract_name(line)
        record["guardian_name"] = record["guardian_name"] or extract_guardian(line)
        record["house_number"] = record["house_number"] or extract_house_number(line)
        record["age"] = record["age"] or extract_age(line)
        record["gender"] = record["gender"] or extract_gender(line)

    completed = finalize_voter(record)
    if completed and not completed.get("serial_number"):
        completed["serial_number"] = extract_card_serial(card_text, page_number, row_index, column_index)
    return completed


def parse_voter_card_grid(image_path, page_number, lang):
    with Image.open(image_path) as image:
        width, height = image.size
        ocr_data = pytesseract.image_to_data(
            image,
            lang=lang,
            config=DEFAULT_TESSERACT_CONFIG,
            output_type=pytesseract.Output.DATAFRAME,
        )

    if ocr_data.empty:
        return []

    ocr_data = ocr_data.copy()
    ocr_data["text"] = ocr_data["text"].fillna("").astype(str)
    ocr_data = ocr_data[ocr_data["text"].str.strip() != ""]
    if ocr_data.empty:
        return []

    ocr_data["center_x"] = ocr_data["left"] + (ocr_data["width"] / 2)
    ocr_data["center_y"] = ocr_data["top"] + (ocr_data["height"] / 2)
    x_starts, cell_width, y_start, row_pitch, cell_height = card_grid_bounds(width, height)

    voters = []
    for row_index in range(10):
        top = y_start + row_index * row_pitch
        bottom = top + cell_height
        for column_index, left in enumerate(x_starts):
            right = left + cell_width
            cell_words = ocr_data[
                (ocr_data["center_x"] >= left)
                & (ocr_data["center_x"] < right)
                & (ocr_data["center_y"] >= top)
                & (ocr_data["center_y"] < bottom)
            ].copy()
            if cell_words.empty:
                continue

            line_groups = []
            for _, group in cell_words.groupby(["block_num", "par_num", "line_num"], sort=False):
                text = line_text_from_ocr_group(group)
                if text:
                    line_groups.append((int(group["top"].min()), text))

            card_text = "\n".join(text for _, text in sorted(line_groups))
            record = parse_voter_card_text(card_text, page_number, row_index, column_index)
            if record:
                voters.append(record)

    return voters


def parse_repeated_voter_group(lines, start_index, page_number):
    names = extract_repeated_names(lines[start_index])
    if len(names) < 2:
        return [], start_index

    group_lines = []
    index = start_index + 1
    while index < len(lines) and len(group_lines) < 3:
        line = lines[index]
        if has_voter_field(line):
            group_lines.append(line)
        index += 1

    if len(group_lines) < 3:
        return [], start_index

    guardians = extract_repeated_guardians(group_lines[0])
    houses = extract_repeated_house_numbers(group_lines[1])
    ages_genders = extract_repeated_age_gender(group_lines[2])

    if max(len(guardians), len(houses), len(ages_genders)) < 2:
        return [], start_index

    voters = []
    raw_text = clean_text(" ".join([lines[start_index], *group_lines]))
    for offset, name in enumerate(names):
        record = blank_voter(page_number)
        record["name"] = name
        record["guardian_name"] = guardians[offset] if offset < len(guardians) else ""
        record["house_number"] = houses[offset] if offset < len(houses) else ""
        if offset < len(ages_genders):
            record["age"], record["gender"] = ages_genders[offset]
        record["raw_text"] = raw_text
        completed = finalize_voter(record)
        if completed:
            voters.append(completed)

    return voters, index - 1


def parse_voter_rows(text, page_number):
    voters = []
    current = None
    lines = [clean_text(line) for line in text.splitlines()]
    index = 0

    while index < len(lines):
        line = lines[index]
        if len(line) < 4:
            index += 1
            continue
        if is_page_furniture_line(line):
            index += 1
            continue

        grouped_voters, consumed_index = parse_repeated_voter_group(lines, index, page_number)
        if grouped_voters:
            completed = finalize_voter(current)
            if completed:
                voters.append(completed)
            voters.extend(grouped_voters)
            current = None
            index = consumed_index + 1
            continue

        name = extract_name(line)
        if name:
            completed = finalize_voter(current)
            if completed:
                voters.append(completed)
            current = blank_voter(page_number)
            current["serial_number"] = extract_serial_number(line)
            current["name"] = name
            current["raw_text"] = line
        elif current:
            current["raw_text"] = clean_text(f"{current['raw_text']} {line}")

        if not current:
            index += 1
            continue

        current["guardian_name"] = current["guardian_name"] or extract_guardian(line)
        current["house_number"] = current["house_number"] or extract_house_number(line)
        current["age"] = current["age"] or extract_age(line)
        current["gender"] = current["gender"] or extract_gender(line)
        index += 1

    completed = finalize_voter(current)
    if completed:
        voters.append(completed)

    return voters


def ocr_page(image, page_number, lang):
    text = pytesseract.image_to_string(image, lang=lang, config=DEFAULT_TESSERACT_CONFIG)
    return page_number, parse_voter_rows(text, page_number)


def ocr_image_file(image_path, page_number, lang):
    grid_voters = parse_voter_card_grid(image_path, page_number, lang)
    if grid_voters:
        return page_number, grid_voters

    text = pytesseract.image_to_string(str(image_path), lang=lang, config=DEFAULT_TESSERACT_CONFIG)
    return page_number, parse_voter_rows(text, page_number)


def render_and_ocr_page(pdf_path, page_number, dpi, lang, poppler_path, image_dir):
    image_paths = convert_from_path(
        str(pdf_path),
        dpi=dpi,
        first_page=page_number,
        last_page=page_number,
        poppler_path=poppler_path if poppler_path != "system PATH" else None,
        grayscale=True,
        fmt="jpeg",
        jpegopt={"quality": 80, "progressive": False, "optimize": False},
        output_folder=image_dir,
        output_file=f"{abs(hash(str(pdf_path.resolve())))}_{page_number}",
        paths_only=True,
        thread_count=1,
    )
    if not image_paths:
        return page_number, []
    return ocr_image_file(image_paths[0], page_number, lang)


def pdf_page_range(pdf_path, setup, first_page=None, last_page=None):
    pdf_pages = pdf_page_count(pdf_path, setup["poppler_path"])
    start_page = first_page or 1
    end_page = min(last_page or pdf_pages, pdf_pages)
    if start_page > end_page:
        return []
    return list(range(start_page, end_page + 1))


def extract_voters_from_pdf(
    pdf_path,
    first_page=None,
    last_page=None,
    dpi=DEFAULT_DPI,
    lang=DEFAULT_OCR_LANG,
    progress_callback=None,
):
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    setup = validate_setup()
    page_numbers = pdf_page_range(pdf_path, setup, first_page=first_page, last_page=last_page)
    if not page_numbers:
        return pd.DataFrame([], columns=REPORT_COLUMNS)

    total_pages = len(page_numbers)
    page_workers = worker_count(
        total_pages,
        env_name="VOTER_PAGE_WORKERS",
        default_limit=default_page_worker_limit(),
    )

    if progress_callback:
        progress_callback(
            0,
            total_pages,
            page_numbers[0],
            f"Running OCR with {page_workers} page worker{'s' if page_workers != 1 else ''}...",
        )

    with tempfile.TemporaryDirectory(prefix="voter_pages_") as image_dir:
        page_results = {}
        completed_pages = 0
        with ThreadPoolExecutor(max_workers=page_workers) as executor:
            futures = {
                executor.submit(
                    render_and_ocr_page,
                    pdf_path,
                    page_number,
                    dpi,
                    lang,
                    setup["poppler_path"],
                    image_dir,
                ): page_number
                for page_number in page_numbers
            }
            for future in as_completed(futures):
                page_number, page_voters = future.result()
                print(f"{pdf_path.name}: processed page {page_number}")
                page_results[page_number] = page_voters
                completed_pages += 1
                if progress_callback:
                    progress_callback(
                        completed_pages,
                        total_pages,
                        page_number,
                        f"{pdf_path.name}: processed page {page_number}",
                    )

    voters = []
    for page_number in sorted(page_results):
        voters.extend(page_results[page_number])

    return pd.DataFrame(voters, columns=REPORT_COLUMNS)


def labeled_counts(df, column_name):
    if df.empty or column_name not in df:
        return pd.DataFrame(columns=["label", "count"])
    labels = df[column_name].fillna("").astype(str).str.strip()
    labels = labels[labels != ""]
    if labels.empty:
        return pd.DataFrame(columns=["label", "count"])
    return labels.value_counts().rename_axis("label").reset_index(name="count")


def build_dashboard(df):
    religion_counts = labeled_counts(df, "religion_label")
    caste_counts = labeled_counts(df, "caste_label")
    pages_processed = 0
    pdfs_processed = 0
    if not df.empty:
        if "pdf_file" in df:
            pdfs_processed = int(df["pdf_file"].nunique())
            pages_processed = int(df[["pdf_file", "page"]].drop_duplicates().shape[0])
        else:
            pages_processed = int(df["page"].nunique())
    return {
        "total_voters": int(len(df)),
        "pdfs_processed": pdfs_processed,
        "pages_processed": pages_processed,
        "extracted": int((df["review_status"] == "extracted").sum()) if not df.empty else 0,
        "needs_review": int((df["review_status"] == "needs_review").sum()) if not df.empty else 0,
        "religion_labeled": int((df["religion_label"] != "").sum()),
        "caste_labeled": int((df["caste_label"] != "").sum()),
        "religion_counts": religion_counts,
        "caste_counts": caste_counts,
    }


def assign_sequential_serials(df):
    if df.empty or "serial_number" not in df:
        return df

    df = df.copy()
    sort_columns = [column for column in ("pdf_file", "page") if column in df.columns]
    ordered_index = df.sort_values(sort_columns, kind="stable").index if sort_columns else df.index

    if "pdf_file" in df.columns:
        for _, group in df.loc[ordered_index].groupby("pdf_file", sort=False):
            for serial, index in enumerate(group.index, start=1):
                df.at[index, "serial_number"] = str(serial)
    else:
        for serial, index in enumerate(ordered_index, start=1):
            df.at[index, "serial_number"] = str(serial)

    return df


def write_reports(df, output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    df = assign_sequential_serials(df)

    details_path = output_dir / "voter_extraction.xlsx"
    summary_path = output_dir / "dashboard_summary.xlsx"

    summary = (
        df["review_status"].value_counts().rename_axis("review_status").reset_index(name="count")
        if not df.empty
        else pd.DataFrame(columns=["review_status", "count"])
    )
    dashboard = build_dashboard(df)

    df.to_excel(details_path, index=False)
    with pd.ExcelWriter(summary_path) as writer:
        summary.to_excel(writer, sheet_name="review_status", index=False)
        dashboard["religion_counts"].to_excel(writer, sheet_name="religion_labels", index=False)
        dashboard["caste_counts"].to_excel(writer, sheet_name="caste_labels", index=False)

    return details_path, summary_path, summary


def process_pdf(
    pdf_path,
    output_dir="outputs",
    first_page=None,
    last_page=None,
    dpi=DEFAULT_DPI,
    lang=DEFAULT_OCR_LANG,
    progress_callback=None,
):
    df = extract_voters_from_pdf(
        pdf_path,
        first_page=first_page,
        last_page=last_page,
        dpi=dpi,
        lang=lang,
        progress_callback=progress_callback,
    )
    df = assign_sequential_serials(df)
    details_path, summary_path, summary = write_reports(df, output_dir)
    return {
        "data": df,
        "summary": summary,
        "dashboard": build_dashboard(df),
        "details_path": details_path,
        "summary_path": summary_path,
    }


def process_pdfs(
    pdf_paths,
    output_dir="outputs",
    first_page=None,
    last_page=None,
    dpi=DEFAULT_DPI,
    lang=DEFAULT_OCR_LANG,
    progress_callback=None,
):
    pdf_paths = [Path(path) for path in pdf_paths]
    if not pdf_paths:
        raise ValueError("Choose at least one PDF file to process.")

    missing_paths = [str(path) for path in pdf_paths if not path.is_file()]
    if missing_paths:
        raise FileNotFoundError(f"PDF not found: {', '.join(missing_paths)}")

    setup = validate_setup()
    page_tasks = []
    for pdf_path in pdf_paths:
        display_name = re.sub(r"^\d{3}_", "", pdf_path.name)
        for page_number in pdf_page_range(
            pdf_path,
            setup,
            first_page=first_page,
            last_page=last_page,
        ):
            page_tasks.append((pdf_path, display_name, page_number))

    total_pages = len(page_tasks)
    if not page_tasks:
        empty_df = pd.DataFrame(columns=["pdf_file", *REPORT_COLUMNS])
        details_path, summary_path, summary = write_reports(empty_df, output_dir)
        return {
            "data": empty_df,
            "summary": summary,
            "dashboard": build_dashboard(empty_df),
            "details_path": details_path,
            "summary_path": summary_path,
        }

    page_workers = worker_count(
        total_pages,
        env_name="VOTER_PAGE_WORKERS",
        default_limit=default_page_worker_limit(),
    )

    if progress_callback:
        progress_callback(
            0,
            total_pages,
            0,
            f"Processing {len(pdf_paths)} PDFs with {page_workers} shared page workers...",
        )

    page_results = {}
    completed_pages = 0
    with tempfile.TemporaryDirectory(prefix="voter_pages_") as image_dir:
        with ThreadPoolExecutor(max_workers=page_workers) as executor:
            futures = {
                executor.submit(
                    render_and_ocr_page,
                    pdf_path,
                    page_number,
                    dpi,
                    lang,
                    setup["poppler_path"],
                    image_dir,
                ): (pdf_path, display_name, page_number)
                for pdf_path, display_name, page_number in page_tasks
            }
            for future in as_completed(futures):
                pdf_path, display_name, page_number = futures[future]
                _, page_voters = future.result()
                print(f"{display_name}: processed page {page_number}")
                page_results[(pdf_path, page_number)] = (display_name, page_voters)
                completed_pages += 1
                if progress_callback:
                    progress_callback(
                        completed_pages,
                        total_pages,
                        page_number,
                        f"{display_name}: processed page {page_number} ({completed_pages} of {total_pages})",
                    )

    voters = []
    for pdf_path, display_name, page_number in page_tasks:
        _, page_voters = page_results.get((pdf_path, page_number), (display_name, []))
        for voter in page_voters:
            voters.append({"pdf_file": display_name, **voter})

    combined_df = pd.DataFrame(voters, columns=["pdf_file", *REPORT_COLUMNS])
    combined_df = assign_sequential_serials(combined_df)
    details_path, summary_path, summary = write_reports(combined_df, output_dir)
    return {
        "data": combined_df,
        "summary": summary,
        "dashboard": build_dashboard(combined_df),
        "details_path": details_path,
        "summary_path": summary_path,
    }


def main():
    parser = argparse.ArgumentParser(description="Extract one voter per row from electoral-roll PDFs.")
    parser.add_argument("pdf_paths", nargs="+", help="Path to one or more PDF files to process")
    parser.add_argument("--output-dir", default="outputs", help="Folder for Excel reports")
    parser.add_argument("--first-page", type=int, default=None)
    parser.add_argument("--last-page", type=int, default=None)
    parser.add_argument("--dpi", type=int, default=DEFAULT_DPI)
    parser.add_argument("--lang", default=DEFAULT_OCR_LANG)
    args = parser.parse_args()

    try:
        if len(args.pdf_paths) == 1:
            result = process_pdf(
                args.pdf_paths[0],
                output_dir=args.output_dir,
                first_page=args.first_page,
                last_page=args.last_page,
                dpi=args.dpi,
                lang=args.lang,
            )
        else:
            result = process_pdfs(
                args.pdf_paths,
                output_dir=args.output_dir,
                first_page=args.first_page,
                last_page=args.last_page,
                dpi=args.dpi,
                lang=args.lang,
            )
    except Exception as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1

    dashboard = result["dashboard"]
    print("\nDashboard:")
    if dashboard["pdfs_processed"]:
        print(f"PDFs processed: {dashboard['pdfs_processed']}")
    print(f"Total voters: {dashboard['total_voters']}")
    print(f"Pages processed: {dashboard['pages_processed']}")
    print(f"Extracted: {dashboard['extracted']}")
    print(f"Needs review: {dashboard['needs_review']}")
    print("\nReligion Breakdown:")
    print(dashboard["religion_counts"])
    print("\nCaste Breakdown:")
    print(dashboard["caste_counts"])
    print(f"\nSaved details: {result['details_path']}")
    print(f"Saved summary: {result['summary_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
