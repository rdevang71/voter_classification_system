import argparse
import os
import re
import sys
from pathlib import Path

import pandas as pd
import pytesseract
from pdf2image import convert_from_path
from pytesseract import TesseractNotFoundError


DEFAULT_POPPLER_PATH = r"C:\Release-26.02.0-0\poppler-26.02.0\Library\bin"
LOCAL_TESSDATA_DIR = Path(__file__).resolve().parent / "tessdata"
DEFAULT_TESSERACT_PATHS = (
    r"C:\Program Files\Tesseract-OCR\tesseract.exe",
    r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
)
DEFAULT_OCR_LANG = "hin+eng"
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


def clean_text(value):
    return re.sub(r"\s+", " ", value or "").strip()


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
            return clean_text(match.group(1))
    return ""


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
            r"(?:Mother'?s?\s+Name)\s*[:\-]?\s*([^\d|,;:]+)",
        ),
        text,
    )


def extract_house_number(text):
    return first_match(
        (
            r"(?:\u092e\u0915\u093e\u0928\s+\u0938\u0902\u0916\u094d\u092f\u093e|House\s+Number|House\s+No)\s*[:\-]?\s*([A-Za-z0-9\/\-]+)",
        ),
        text,
    )


def extract_age(text):
    return first_match((r"(?:\u0906\u092f\u0941|Age)\s*[:\-]?\s*(\d{1,3})",), text)


def extract_gender(text):
    return first_match(
        (r"(?:\u0932\u093f\u0902\u0917|Gender|Sex)\s*[:\-]?\s*([A-Za-z\u0900-\u097F]+)",),
        text,
    )


def extract_serial_number(text):
    return first_match((r"^\s*(\d{1,5})\b",), text, flags=0)


# ==================== RELIGION & CASTE CLASSIFICATION (Expanded) ====================

RELIGION_KEYWORDS = {
   "MUSLIM": [
    "khan", "ahmed", "ahmad", "ali", "mohammad", "muhammad", "mohd", "hussain", "husain",
    "shaikh", "sheikh", "ansari", "qureshi", "malik", "begum", "fatima", "rehman", "rahman",
    "siddiqui", "zaidi", "rizvi", "mirza", "pathan", "syed", "hashmi", "abbas", "javed",
    "farooq", "iqbal", "rashid", "akhtar", "parveen", "bano", "nadeem", "naeem", "salman",
    "imran", "zaman", "alam", "sultan", "ismail", "yakub", "yaqub", "idris", "aslam", "adil",
    "firoz", "nawaz", "shah", "dar", "wani", "lone", "bhat", "rather", "mir", "baig",
    "khanam", "khatoon", "bi", "jaan", "uddin", "rabbani", "qasmi", "faridi", "chishti",
    "naqvi", "jilani", "gilani", "kazmi", "mehdi", "husaini", "abidi", "tonki", "zubair",
    "zuberi", "qazi", "pirzada", "deobandi", "barelvi", "sufi", "fakir", "saifi", "siddiqi",
    "kureshi", "kuraishi", "kureshy", "momin", "momin", "khan", "khoja", "bohra", "ismaili",
    "dawoodi", "sulaimani", "meman", "vohra", "ghanchi", "chhipa", "teli", "pinjara",
    "mansuri", "ghumara", "kumbhar", "dhobi", "kasai", "quraishi", "meo", "mev", "rangrez",
    "saifi", "shershahi", "lodhi", "shah", "pir", "dargah", "makhdoom", "bukhari", "gilani",
    "jafari", "zaidi", "rizvi", "naqvi", "usmani", "farooqi", "siddiqi", "qadri", "chishti",
    "suhrawardi", "nakshbandi"
],

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
        "kandu","barai","bari","bind",
        "यादव","अहीर","कुर्मी","पटेल","मौर्य","सैनी","लोधी","पाल",
        "निषाद","केवट","कहार","राजभर","गुर्जर","कम्बोज","शाक्य",
        "कटियार","तोमर","सोलंकी","जायसवाल","कोरी","प्रजापति",
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
    "सिंह",
    "कुमार",
    "देवी",
    "रानी",
    "लाल",
    "राम",
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
    return set(re.findall(r"[a-z']+|[\u0900-\u097F]+", clean_text(name).lower()))


def has_keyword_token(name, keywords, ignore_common=False):
    tokens = name_tokens(name)
    for keyword in keywords:
        keyword = clean_text(keyword).lower()
        if ignore_common and keyword in COMMON_NAME_TOKENS:
            continue
        if keyword in tokens:
            return True
    return False


def classify_religion(name: str) -> str:
    if not name:
        return "Unknown"
    for religion, keywords in RELIGION_KEYWORDS.items():
        if has_keyword_token(name, keywords, ignore_common=True):
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
    if len(raw_text) > 220:
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
    record["religion_label"] = classify_religion(record["name"])
    record["caste_label"] = classify_caste(record["name"])
    record["review_status"] = classify_review_status(record["name"], record["raw_text"])
    return record if is_valid_voter_record(record) else None


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
        value = clean_text(text[match.end() : end])
        value = re.sub(r"\[?\s*\u092b\u094b\u091f\u094b\s+\u0909\u092a\u0932\u092c\u094d\u0927.*$", "", value)
        value = re.sub(r"\s*\|.*$", "", value)
        values.append(clean_text(value))

    return values


def extract_repeated_names(text):
    text = clean_text(text)
    if not re.match(r"^(?:\u0928\u093e\u092e|Name)\s*[:\-\uff1a]?", text, re.IGNORECASE):
        return []
    return repeated_values(text, r"(?:^|\s)(?:\u0928\u093e\u092e|Name)\s*[:\-\uff1a]?")


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
    pairs = []
    for match in re.finditer(
        r"(?:\u0906\u092f\u0941|Age)\s*[:\-]?\s*([0-9\u0966-\u096f]{1,3})\s*(?:\u0932\u093f\u0902\u0917|Gender|Sex)\s*[:\-]?\s*([A-Za-z\u0900-\u097F]+)",
        text,
        re.IGNORECASE,
    ):
        pairs.append((clean_text(match.group(1)), clean_text(match.group(2))))
    return pairs


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
    expected_count = min(len(names), len(guardians), len(houses), len(ages_genders))

    if expected_count < 2:
        return [], start_index

    voters = []
    raw_text = clean_text(" ".join([lines[start_index], *group_lines]))
    for offset in range(expected_count):
        record = blank_voter(page_number)
        record["name"] = names[offset]
        record["guardian_name"] = guardians[offset]
        record["house_number"] = houses[offset]
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


def extract_voters_from_pdf(
    pdf_path,
    first_page=None,
    last_page=None,
    dpi=300,
    lang=DEFAULT_OCR_LANG,
    progress_callback=None,
):
    pdf_path = Path(pdf_path)
    if not pdf_path.is_file():
        raise FileNotFoundError(f"PDF not found: {pdf_path}")

    setup = validate_setup()
    images = convert_from_path(
        str(pdf_path),
        dpi=dpi,
        first_page=first_page,
        last_page=last_page,
        poppler_path=setup["poppler_path"] if setup["poppler_path"] != "system PATH" else None,
    )

    voters = []
    start_page = first_page or 1
    total_pages = len(images)
    for offset, image in enumerate(images):
        page_number = start_page + offset
        print(f"Processing page {page_number}")
        if progress_callback:
            progress_callback(
                offset,
                total_pages,
                page_number,
                f"Reading page {page_number} ({offset + 1} of {total_pages})...",
            )
        text = pytesseract.image_to_string(image, lang=lang)
        voters.extend(parse_voter_rows(text, page_number))
        if progress_callback:
            progress_callback(offset + 1, total_pages, page_number)

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
    return {
        "total_voters": int(len(df)),
        "pages_processed": int(df["page"].nunique()) if not df.empty else 0,
        "extracted": int((df["review_status"] == "extracted").sum()) if not df.empty else 0,
        "needs_review": int((df["review_status"] == "needs_review").sum()) if not df.empty else 0,
        "religion_labeled": int((df["religion_label"] != "").sum()),
        "caste_labeled": int((df["caste_label"] != "").sum()),
        "religion_counts": religion_counts,
        "caste_counts": caste_counts,
    }


def write_reports(df, output_dir):
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

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
    dpi=300,
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
    details_path, summary_path, summary = write_reports(df, output_dir)
    return {
        "data": df,
        "summary": summary,
        "dashboard": build_dashboard(df),
        "details_path": details_path,
        "summary_path": summary_path,
    }


def main():
    parser = argparse.ArgumentParser(description="Extract one voter per row from an electoral-roll PDF.")
    parser.add_argument("pdf_path", help="Path to the PDF file to process")
    parser.add_argument("--output-dir", default="outputs", help="Folder for Excel reports")
    parser.add_argument("--first-page", type=int, default=None)
    parser.add_argument("--last-page", type=int, default=None)
    parser.add_argument("--dpi", type=int, default=300)
    parser.add_argument("--lang", default=DEFAULT_OCR_LANG)
    args = parser.parse_args()

    try:
        result = process_pdf(
            args.pdf_path,
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
