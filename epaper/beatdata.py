"""
The vocabulary data behind every beat: one shared policing pack, and the
geography of all 28 states and 8 union territories.

Split out of keywords.py because of what scaling to every state revealed. A
beat used to be one hand-built dict, and "Delhi Police" carried both the
policing words and the Delhi place names in one list. Copying that shape 36
times would have meant 36 copies of the same ~290 policing terms differing
only in their place names -- ten thousand duplicated strings, and a vocabulary
fix that has to be applied 36 times to take effect.

So the data is factored the way the domain actually is:

    POLICING        what a police story is made of. Identical in every state:
                    a constable is a constable in Kohima and in Kochi.
    STATES          what varies. The state's name, its force, its districts
                    and cities, in English and Devanagari.

A beat is then POLICING + one state's places, and `region_out` -- the
geography that marks a story as somebody else's -- is computed as every OTHER
state's places rather than hand-listed. That last part matters: the old
hand-written OTHER_PLACES list had to be maintained by hand and was already
missing most of the country.

Nothing here imports the rest of the package, and nothing here touches a
database. keywords.py builds its default BEATS from this file, so the whole
vocabulary works offline with no MongoDB, no model and no network --
`scripts/topic_check.py` depends on that. epaper/beats.py is the separate
layer that persists these to MongoDB and lets them be edited at runtime.
"""

from __future__ import annotations

# Weights live here because this is where the terms are. keywords.py re-exports
# them, so `keywords.CORE` still resolves for anything that used it.
CORE, RELATED, CONTEXT = 1.0, 0.6, 0.3


# ------------------------------------------------------------- shared pack

# Case-sensitive, matched with optional dots (FIR / F.I.R.). Lowercasing these
# would make "fir" and "sho" fire on ordinary words.
POLICING_ACRONYMS = [
    (CORE, ["DCP", "ACP", "SHO", "SSP", "DSP", "IPS", "PCR", "SIT", "SP",
            "IGP", "DGP", "ADGP", "CID", "ATS", "STF"]),
    (RELATED, ["FIR", "NDPS", "POCSO", "IPC", "CrPC", "MLC", "MCOCA", "UAPA",
               "BNS", "BNSS", "NSA", "PSA"]),
]

POLICING_TERMS = [
    (CORE, [
        # the force
        "police", "policeman", "policemen", "policewoman", "policewomen",
        "policing", "cop", "cops", "constable", "constables",
        "head constable", "sub-inspector", "sub inspector",
        "station house officer", "police station", "police post",
        "police team", "police officer", "police official", "chowki",
        "thana", "crime branch", "special cell", "traffic police",
        "cyber cell", "economic offences wing", "anti-narcotics",
        "police commissioner", "commissioner of police",
        "joint commissioner", "deputy commissioner of police",
        "additional commissioner", "law enforcement",
        "superintendent of police", "inspector general", "director general",
        "police headquarters", "armed police", "home guard",
        # हिन्दी
        "पुलिस", "पुलिसकर्मी", "थाना", "थाने", "थानाध्यक्ष", "थाना प्रभारी",
        "चौकी", "कांस्टेबल", "सिपाही", "हवलदार", "दरोगा", "क्राइम ब्रांच",
        "स्पेशल सेल", "यातायात पुलिस", "ट्रैफिक पुलिस", "पुलिस आयुक्त",
        "पुलिस उपायुक्त", "गश्त", "पुलिसिया", "पुलिस अधीक्षक", "पुलिस महानिदेशक",
        # Ranks as Hindi papers actually spell them. The Latin acronyms are
        # matched case-sensitively and so never catch these.
        "एसएचओ", "डीसीपी", "एसीपी", "एसपी", "डीएसपी", "एसएसपी", "आईपीएस",
        "पीसीआर", "एसआईटी", "डीजीपी", "आईजी",
    ]),
    (RELATED, [
        # what police do
        "arrest", "arrested", "arrests", "arresting", "detain", "detained",
        "detention", "nabbed", "apprehended", "booked", "custody", "remand",
        "bail", "accused", "suspect", "suspects", "absconding", "raid",
        "raided", "raids", "seized", "seizure", "recovered", "chargesheet",
        "charge sheet", "investigation", "investigating", "probe",
        "complaint", "case registered", "encounter", "manhunt",
        "interrogation", "confession", "patrolling", "patrol", "inspector",
        "summons", "warrant", "crackdown", "lookout notice", "lodged",
        # what is done
        "crime", "criminal", "offence", "culprit", "murder", "killed",
        "stabbed", "stabbing", "shot dead", "robbery", "robbed", "loot",
        "snatching", "snatchers", "theft", "stolen", "burglary", "dacoity",
        "extortion", "kidnapping", "kidnapped", "abduction", "rape",
        "molestation", "assault", "firing", "shootout", "gangster", "gang",
        "smuggling", "smuggler", "narcotics", "drugs", "heroin", "ganja",
        "cocaine", "illegal arms", "pistol", "firearm", "cybercrime",
        "cyber fraud", "fraud", "cheating", "forgery", "trafficking",
        "rioting", "vandalism", "hooliganism", "lynching", "honour killing",
        # हिन्दी
        "गिरफ्तार", "हिरासत", "प्राथमिकी", "एफआईआर", "मुकदमा", "जांच",
        "छापा", "छापेमारी", "बरामद", "आरोपपत्र", "चार्जशीट", "रिमांड",
        "जमानत", "आरोपी", "संदिग्ध", "फरार", "मुठभेड़", "पकड़", "दबोच",
        "सलाखों", "इंस्पेक्टर", "अपराध", "अपराधी", "वारदात", "हत्या",
        "कत्ल", "लूट", "चोरी", "डकैती", "झपटमारी", "उगाही", "वसूली",
        "अपहरण", "दुष्कर्म", "बलात्कार", "छेड़छाड़", "मारपीट", "फायरिंग",
        "गोलीबारी", "गैंगस्टर", "गिरोह", "बदमाश", "तस्करी", "नशा", "नशे",
        "मादक पदार्थ", "स्मैक", "गांजा", "अवैध हथियार", "पिस्तौल", "कट्टा",
        "साइबर", "ठगी", "धोखाधड़ी", "जालसाजी", "आईपीसी", "एनडीपीएस",
        "पॉक्सो", "धारा", "लापता", "बलवा", "हिंसा",
    ]),
    (CONTEXT, [
        "court", "judge", "magistrate", "sessions court", "high court",
        "judiciary", "prosecution", "jail", "prison", "victim",
        "witness", "CCTV", "forensic", "postmortem", "post-mortem",
        "law and order", "security", "curfew", "prohibitory orders",
        # हिन्दी
        "अदालत", "कोर्ट", "न्यायालय", "न्यायाधीश", "मजिस्ट्रेट",
        "जेल", "पीड़ित", "गवाह", "फोरेंसिक", "पोस्टमार्टम",
        "कानून व्यवस्था", "सुरक्षा", "अभियोजन", "वारंट", "समन", "कर्फ्यू",
    ]),
]

# Aliases every state beat answers to, on top of its own name. Kept short and
# unambiguous -- an alias that fires on the wrong query costs precision.
POLICING_ALIASES = [
    "police", "policing", "crime", "law and order", "criminal",
    "crime and policing", "police, crime and arrests",
    "पुलिस", "अपराध", "कानून व्यवस्था",
]


# ---------------------------------------------------------------- geography
#
# Places are the state's own name, its capital, and its larger cities and
# districts, in English and -- where Hindi papers actually print them -- in
# Devanagari.
#
# What is deliberately EXCLUDED is as important as what is here. A place name
# that is also an ordinary word or a common surname produces false geography,
# and false geography is expensive: it marks an on-patch story as somebody
# else's and drops it below threshold. So these are all absent on purpose:
#
#   Mandi (HP)      "मंडी" is the Hindi word for a market. Fires constantly.
#   Kota (Raj)      "कोटा" is the Hindi word for quota.
#   Erode (TN)      An English verb.
#   Sagar (MP)      "सागर" is Hindi for ocean, and a common surname.
#   Puri (Odisha)   A very common surname -- Hardeep Singh Puri is a Delhi MP.
#   Anand (Guj)     A given name and a surname.
#   Una (HP)        Three letters, and a word in several languages.
#   Pali (Raj)      A language, and an ordinary word.
#   Nadia (WB)      A given name.
#
# Devanagari needs the same care and gets it wrong more easily, because
# Devanagari terms PREFIX-match. Three names are listed in Latin only for that
# reason, and each was caught firing on real copy:
#
#   गया  (Gaya)     Also the ordinary past tense "went". "किया गया" -- "was
#                   done" -- appears in virtually every Hindi crime report, so
#                   Bihar scored 1.00 on a Mumbai story.
#   दमन  (Daman)    Also "repression", common in political coverage.
#   सूरत (Surat)    Also "face" or "manner", as in "किसी सूरत में".
#
# The English spellings of all three are unambiguous and stay. The same care
# applies to anything added later: prefer a district name that could only ever
# be a place, and be more suspicious of a short Devanagari one.

STATES = [
    # -- union territories that run their own force ----------------------
    {
        "code": "DL", "kind": "ut", "name": "Delhi", "hi": "दिल्ली",
        "force": "Delhi Police",
        "places": [
            "Delhi", "New Delhi", "NCR", "Noida", "Gurugram", "Gurgaon",
            "Ghaziabad", "Faridabad", "Dwarka", "Rohini", "Shahdara",
            "Karol Bagh", "Connaught Place", "Seelampur", "Najafgarh",
            "Okhla", "Saket", "Jamia", "Tis Hazari", "Patiala House",
            "Model Town", "Civil Lines", "Tihar",
            "दिल्ली", "नई दिल्ली", "नोएडा", "गुरुग्राम", "गाजियाबाद",
            "फरीदाबाद", "द्वारका", "रोहिणी", "शाहदरा", "करोल बाग", "ओखला",
            "सीलमपुर", "नजफगढ़", "तिहाड़",
        ],
    },
    {
        "code": "CH", "kind": "ut", "name": "Chandigarh", "hi": "चंडीगढ़",
        "force": "Chandigarh Police",
        "places": ["Chandigarh", "Panchkula", "Mohali", "Zirakpur",
                   "चंडीगढ़", "पंचकूला", "मोहाली"],
    },
    {
        "code": "JK", "kind": "ut", "name": "Jammu and Kashmir",
        "hi": "जम्मू-कश्मीर", "force": "Jammu and Kashmir Police",
        "places": ["Jammu", "Kashmir", "Srinagar", "Anantnag", "Baramulla",
                   "Udhampur", "Pulwama", "Kupwara", "Sopore", "Kathua",
                   "Budgam", "Shopian", "Rajouri", "Poonch",
                   "जम्मू", "कश्मीर", "श्रीनगर", "अनंतनाग", "बारामूला",
                   "पुलवामा", "उधमपुर", "कुपवाड़ा"],
    },
    {
        "code": "LA", "kind": "ut", "name": "Ladakh", "hi": "लद्दाख",
        "force": "Ladakh Police",
        "places": ["Ladakh", "Leh", "Kargil", "Nubra", "लद्दाख", "लेह", "करगिल"],
    },
    {
        "code": "PY", "kind": "ut", "name": "Puducherry", "hi": "पुदुचेरी",
        "force": "Puducherry Police",
        "places": ["Puducherry", "Pondicherry", "Karaikal", "पुदुचेरी"],
    },
    {
        "code": "AN", "kind": "ut", "name": "Andaman and Nicobar Islands",
        "hi": "अंडमान", "force": "Andaman and Nicobar Police",
        "places": ["Andaman", "Nicobar", "Port Blair", "अंडमान", "निकोबार",
                   "पोर्ट ब्लेयर"],
    },
    {
        "code": "DH", "kind": "ut",
        "name": "Dadra and Nagar Haveli and Daman and Diu", "hi": "दमन",
        "force": "Dadra and Nagar Haveli and Daman and Diu Police",
        "places": ["Dadra", "Nagar Haveli", "Silvassa", "Daman", "सिलवासा"],
    },
    {
        "code": "LD", "kind": "ut", "name": "Lakshadweep", "hi": "लक्षद्वीप",
        "force": "Lakshadweep Police",
        "places": ["Lakshadweep", "Kavaratti", "लक्षद्वीप"],
    },

    # -- states ------------------------------------------------------------
    {
        "code": "AP", "kind": "state", "name": "Andhra Pradesh",
        "hi": "आंध्र प्रदेश", "force": "Andhra Pradesh Police",
        "places": ["Andhra Pradesh", "Andhra", "Amaravati", "Vijayawada",
                   "Visakhapatnam", "Vizag", "Guntur", "Nellore", "Tirupati",
                   "Kurnool", "Rajahmundry", "Kakinada", "Anantapur",
                   "आंध्र प्रदेश", "विजयवाड़ा", "विशाखापत्तनम", "तिरुपति"],
    },
    {
        "code": "AR", "kind": "state", "name": "Arunachal Pradesh",
        "hi": "अरुणाचल प्रदेश", "force": "Arunachal Pradesh Police",
        "places": ["Arunachal Pradesh", "Arunachal", "Itanagar", "Naharlagun",
                   "Pasighat", "Tawang", "Ziro",
                   "अरुणाचल प्रदेश", "ईटानगर", "तवांग"],
    },
    {
        "code": "AS", "kind": "state", "name": "Assam", "hi": "असम",
        "force": "Assam Police",
        "places": ["Assam", "Dispur", "Guwahati", "Silchar", "Dibrugarh",
                   "Jorhat", "Nagaon", "Tinsukia", "Tezpur", "Bongaigaon",
                   "असम", "गुवाहाटी", "दिसपुर", "सिलचर", "डिब्रूगढ़"],
    },
    {
        "code": "BR", "kind": "state", "name": "Bihar", "hi": "बिहार",
        "force": "Bihar Police",
        "places": ["Bihar", "Patna", "Gaya", "Bhagalpur", "Muzaffarpur",
                   "Darbhanga", "Purnia", "Begusarai", "Chhapra", "Motihari",
                   "Nalanda", "Samastipur", "Katihar",
                   "बिहार", "पटना", "भागलपुर", "मुजफ्फरपुर", "दरभंगा",
                   "पूर्णिया", "बेगूसराय"],
    },
    {
        "code": "CG", "kind": "state", "name": "Chhattisgarh",
        "hi": "छत्तीसगढ़", "force": "Chhattisgarh Police",
        "places": ["Chhattisgarh", "Raipur", "Bhilai", "Bilaspur", "Korba",
                   "Durg", "Jagdalpur", "Bastar", "Dantewada", "Sukma",
                   "Ambikapur", "Raigarh",
                   "छत्तीसगढ़", "रायपुर", "भिलाई", "बिलासपुर", "बस्तर",
                   "दंतेवाड़ा", "सुकमा"],
    },
    {
        "code": "GA", "kind": "state", "name": "Goa", "hi": "गोवा",
        "force": "Goa Police",
        "places": ["Goa", "Panaji", "Margao", "Vasco", "Mapusa", "Ponda",
                   "गोवा", "पणजी", "मडगांव"],
    },
    {
        "code": "GJ", "kind": "state", "name": "Gujarat", "hi": "गुजरात",
        "force": "Gujarat Police",
        "places": ["Gujarat", "Gandhinagar", "Ahmedabad", "Surat", "Vadodara",
                   "Rajkot", "Bhavnagar", "Jamnagar", "Junagadh", "Bharuch",
                   "Morbi", "Bhuj", "Kutch", "Porbandar", "Navsari",
                   "गुजरात", "गांधीनगर", "अहमदाबाद", "वडोदरा", "राजकोट",
                   "जामनगर", "भुज"],
    },
    {
        "code": "HR", "kind": "state", "name": "Haryana", "hi": "हरियाणा",
        "force": "Haryana Police",
        "places": ["Haryana", "Gurugram", "Faridabad", "Panipat", "Ambala",
                   "Karnal", "Hisar", "Rohtak", "Sonipat", "Yamunanagar",
                   "Sirsa", "Bhiwani", "Jhajjar", "Rewari", "Kaithal",
                   "Kurukshetra", "Palwal", "Jind", "Fatehabad", "Narnaul",
                   "हरियाणा", "गुरुग्राम", "फरीदाबाद", "पानीपत", "अंबाला",
                   "करनाल", "हिसार", "रोहतक", "सोनीपत", "सिरसा", "भिवानी",
                   "कुरुक्षेत्र", "झज्जर", "रेवाड़ी"],
    },
    {
        "code": "HP", "kind": "state", "name": "Himachal Pradesh",
        "hi": "हिमाचल प्रदेश", "force": "Himachal Pradesh Police",
        "places": ["Himachal Pradesh", "Himachal", "Shimla", "Dharamshala",
                   "Solan", "Kullu", "Manali", "Kangra", "Hamirpur",
                   "Chamba", "Kinnaur", "Lahaul", "Spiti", "Nahan", "Palampur",
                   "हिमाचल प्रदेश", "हिमाचल", "शिमला", "धर्मशाला", "सोलन",
                   "कुल्लू", "मनाली", "कांगड़ा", "चंबा"],
    },
    {
        "code": "JH", "kind": "state", "name": "Jharkhand", "hi": "झारखंड",
        "force": "Jharkhand Police",
        "places": ["Jharkhand", "Ranchi", "Jamshedpur", "Dhanbad", "Bokaro",
                   "Hazaribagh", "Deoghar", "Giridih", "Palamu", "Chaibasa",
                   "झारखंड", "रांची", "जमशेदपुर", "धनबाद", "बोकारो",
                   "हजारीबाग", "देवघर"],
    },
    {
        "code": "KA", "kind": "state", "name": "Karnataka", "hi": "कर्नाटक",
        "force": "Karnataka Police",
        "places": ["Karnataka", "Bengaluru", "Bangalore", "Mysuru", "Mysore",
                   "Hubballi", "Dharwad", "Mangaluru", "Mangalore", "Belagavi",
                   "Belgaum", "Kalaburagi", "Davangere", "Shivamogga",
                   "Tumakuru", "Udupi",
                   "कर्नाटक", "बेंगलुरु", "मैसूर", "मंगलुरु", "हुबली"],
    },
    {
        "code": "KL", "kind": "state", "name": "Kerala", "hi": "केरल",
        "force": "Kerala Police",
        "places": ["Kerala", "Thiruvananthapuram", "Trivandrum", "Kochi",
                   "Ernakulam", "Kozhikode", "Calicut", "Thrissur", "Kollam",
                   "Alappuzha", "Kannur", "Malappuram", "Palakkad", "Wayanad",
                   "Kottayam", "Idukki",
                   "केरल", "तिरुवनंतपुरम", "कोच्चि", "कोझिकोड", "त्रिशूर"],
    },
    {
        "code": "MP", "kind": "state", "name": "Madhya Pradesh",
        "hi": "मध्य प्रदेश", "force": "Madhya Pradesh Police",
        "places": ["Madhya Pradesh", "Bhopal", "Indore", "Jabalpur", "Gwalior",
                   "Ujjain", "Satna", "Rewa", "Ratlam", "Dewas", "Chhindwara",
                   "Khandwa", "Shivpuri", "Vidisha",
                   "मध्य प्रदेश", "भोपाल", "इंदौर", "जबलपुर", "ग्वालियर",
                   "उज्जैन", "रीवा", "सतना"],
    },
    {
        "code": "MH", "kind": "state", "name": "Maharashtra",
        "hi": "महाराष्ट्र", "force": "Maharashtra Police",
        "places": ["Maharashtra", "Mumbai", "Bombay", "Pune", "Nagpur",
                   "Nashik", "Thane", "Aurangabad", "Solapur", "Kolhapur",
                   "Navi Mumbai", "Amravati", "Nanded", "Sangli", "Latur",
                   "Jalgaon", "Ahmednagar", "Chandrapur",
                   "महाराष्ट्र", "मुंबई", "पुणे", "नागपुर", "नासिक", "ठाणे",
                   "औरंगाबाद", "कोल्हापुर"],
    },
    {
        "code": "MN", "kind": "state", "name": "Manipur", "hi": "मणिपुर",
        "force": "Manipur Police",
        "places": ["Manipur", "Imphal", "Churachandpur", "Thoubal",
                   "Bishnupur", "Kangpokpi", "Moreh",
                   "मणिपुर", "इंफाल", "चुराचांदपुर"],
    },
    {
        "code": "ML", "kind": "state", "name": "Meghalaya", "hi": "मेघालय",
        "force": "Meghalaya Police",
        "places": ["Meghalaya", "Shillong", "Tura", "Jowai", "Nongstoin",
                   "मेघालय", "शिलांग"],
    },
    {
        "code": "MZ", "kind": "state", "name": "Mizoram", "hi": "मिजोरम",
        "force": "Mizoram Police",
        "places": ["Mizoram", "Aizawl", "Lunglei", "Champhai",
                   "मिजोरम", "आइजोल"],
    },
    {
        "code": "NL", "kind": "state", "name": "Nagaland", "hi": "नगालैंड",
        "force": "Nagaland Police",
        "places": ["Nagaland", "Kohima", "Dimapur", "Mokokchung", "Wokha",
                   "नगालैंड", "कोहिमा", "दीमापुर"],
    },
    {
        "code": "OD", "kind": "state", "name": "Odisha", "hi": "ओडिशा",
        "force": "Odisha Police",
        "places": ["Odisha", "Orissa", "Bhubaneswar", "Cuttack", "Rourkela",
                   "Berhampur", "Sambalpur", "Balasore", "Koraput",
                   "Jharsuguda", "Angul",
                   "ओडिशा", "भुवनेश्वर", "कटक", "राउरकेला", "संबलपुर"],
    },
    {
        "code": "PB", "kind": "state", "name": "Punjab", "hi": "पंजाब",
        "force": "Punjab Police",
        "places": ["Punjab", "Ludhiana", "Amritsar", "Jalandhar", "Patiala",
                   "Bathinda", "Pathankot", "Hoshiarpur", "Moga", "Khanna",
                   "Firozpur", "Barnala", "Sangrur", "Kapurthala",
                   "Faridkot", "Muktsar", "Gurdaspur", "Tarn Taran",
                   "Malerkotla", "Rajpura", "Phagwara", "Batala", "Abohar",
                   "पंजाब", "लुधियाना", "अमृतसर", "जालंधर", "पटियाला",
                   "बठिंडा", "पठानकोट", "होशियारपुर", "मोगा", "खन्ना",
                   "फिरोजपुर", "संगरूर"],
    },
    {
        "code": "RJ", "kind": "state", "name": "Rajasthan", "hi": "राजस्थान",
        "force": "Rajasthan Police",
        "places": ["Rajasthan", "Jaipur", "Jodhpur", "Udaipur", "Ajmer",
                   "Bikaner", "Alwar", "Bharatpur", "Sikar",
                   "Bhilwara", "Jhunjhunu", "Chittorgarh", "Jaisalmer",
                   "राजस्थान", "जयपुर", "जोधपुर", "उदयपुर", "अजमेर",
                   "बीकानेर", "अलवर", "भरतपुर"],
    },
    {
        "code": "SK", "kind": "state", "name": "Sikkim", "hi": "सिक्किम",
        "force": "Sikkim Police",
        "places": ["Sikkim", "Gangtok", "Namchi", "Gyalshing", "Mangan",
                   "सिक्किम", "गंगटोक"],
    },
    {
        "code": "TN", "kind": "state", "name": "Tamil Nadu", "hi": "तमिलनाडु",
        "force": "Tamil Nadu Police",
        "places": ["Tamil Nadu", "Chennai", "Madras", "Coimbatore", "Madurai",
                   "Tiruchirappalli", "Trichy", "Tirunelveli", "Vellore",
                   "Thanjavur", "Tuticorin", "Thoothukudi", "Dindigul",
                   "Kanyakumari", "Cuddalore",
                   "तमिलनाडु", "चेन्नई", "कोयंबटूर", "मदुरै"],
    },
    {
        "code": "TG", "kind": "state", "name": "Telangana", "hi": "तेलंगाना",
        "force": "Telangana Police",
        "places": ["Telangana", "Hyderabad", "Secunderabad", "Warangal",
                   "Nizamabad", "Karimnagar", "Khammam", "Ramagundam",
                   "Mahbubnagar", "Cyberabad",
                   "तेलंगाना", "हैदराबाद", "वारंगल", "निजामाबाद"],
    },
    {
        "code": "TR", "kind": "state", "name": "Tripura", "hi": "त्रिपुरा",
        "force": "Tripura Police",
        "places": ["Tripura", "Agartala", "Dharmanagar", "Belonia",
                   "त्रिपुरा", "अगरतला"],
    },
    {
        "code": "UP", "kind": "state", "name": "Uttar Pradesh",
        "hi": "उत्तर प्रदेश", "force": "Uttar Pradesh Police",
        "places": ["Uttar Pradesh", "Lucknow", "Kanpur", "Varanasi", "Agra",
                   "Prayagraj", "Allahabad", "Meerut", "Bareilly", "Aligarh",
                   "Gorakhpur", "Moradabad", "Mathura", "Jhansi", "Saharanpur",
                   "Ayodhya", "Firozabad", "Muzaffarnagar", "Shahjahanpur",
                   "Rampur", "Etawah", "Ballia", "Azamgarh", "Sultanpur",
                   "उत्तर प्रदेश", "लखनऊ", "कानपुर", "वाराणसी", "आगरा",
                   "प्रयागराज", "मेरठ", "बरेली", "अलीगढ़", "गोरखपुर",
                   "मुरादाबाद", "मथुरा", "झांसी", "अयोध्या", "सहारनपुर"],
    },
    {
        "code": "UK", "kind": "state", "name": "Uttarakhand",
        "hi": "उत्तराखंड", "force": "Uttarakhand Police",
        "places": ["Uttarakhand", "Dehradun", "Haridwar", "Nainital",
                   "Roorkee", "Haldwani", "Rishikesh", "Rudrapur",
                   "Kashipur", "Pithoragarh", "Almora", "Chamoli",
                   "उत्तराखंड", "देहरादून", "हरिद्वार", "नैनीताल", "रुड़की",
                   "हल्द्वानी", "ऋषिकेश"],
    },
    {
        "code": "WB", "kind": "state", "name": "West Bengal",
        "hi": "पश्चिम बंगाल", "force": "West Bengal Police",
        "places": ["West Bengal", "Kolkata", "Calcutta", "Howrah", "Durgapur",
                   "Asansol", "Siliguri", "Darjeeling", "Malda", "Kharagpur",
                   "Barasat", "Murshidabad", "Bardhaman",
                   "पश्चिम बंगाल", "कोलकाता", "हावड़ा", "सिलीगुड़ी",
                   "दुर्गापुर", "आसनसोल", "दार्जिलिंग"],
    },
]


# ------------------------------------------------------------------- build

def slug(name: str) -> str:
    """'Jammu and Kashmir' -> 'jammu-and-kashmir'."""
    out = "".join(c if c.isalnum() else "-" for c in (name or "").lower())
    while "--" in out:
        out = out.replace("--", "-")
    return out.strip("-")


def beat_id(state: dict) -> str:
    return "%s-police" % slug(state["name"])


def _aliases(state: dict) -> list:
    """
    What a user might type to reach this beat.

    The state's own name comes first and is the discriminating one. The shared
    policing aliases come last: they are what makes a bare "police" resolve to
    SOMETHING, and since resolve() returns the first beat whose alias matches,
    the order of STATES decides which. Delhi is first in that list on purpose.
    """
    out = ["%s police" % state["name"].lower(), state["name"].lower(),
           state["force"].lower()]
    if state.get("hi"):
        out += ["%s पुलिस" % state["hi"], state["hi"]]
    for extra in state.get("aliases") or []:
        out.append(extra.lower())
    return out + POLICING_ALIASES


def build_beats(states: list | None = None) -> list:
    """
    Every state beat, ready for keywords.load() or for seeding MongoDB.

    `region_out` is computed, not authored: it is the union of every OTHER
    state's places. That is why a place appearing in two states is harmless --
    score() only counts a competing region when the story names no place of
    this beat's own, so a shared name like Chandigarh (Punjab, Haryana and a UT
    in its own right) satisfies `region` for all three and demotes none.
    """
    states = states if states is not None else STATES
    all_places = {s["code"]: set(s["places"]) for s in states}
    out = []
    for s in states:
        mine = all_places[s["code"]]
        elsewhere = sorted(
            {p for code, ps in all_places.items() if code != s["code"]
             for p in ps} - mine)
        out.append({
            "id": beat_id(s),
            "label": s["force"],
            "kind": "state-police",
            "state": s["name"],
            "state_hi": s.get("hi", ""),
            "state_code": s["code"],
            "region_kind": s.get("kind", "state"),
            "topic": ("the %s -- policing, crime, arrests, investigations, "
                      "law and order, and the conduct and administration of "
                      "the force itself in %s"
                      % (s["force"], s["name"])),
            "aliases": _aliases(s),
            "acronyms": POLICING_ACRONYMS,
            # The shared pack, then this state's places appended as their own
            # CONTEXT group. Places carry weight as well as gating geography:
            # one list, so the two uses cannot drift apart. Kept as a separate
            # group rather than spliced into the pack's CONTEXT terms so that
            # beats.py can rebuild exactly this shape from MongoDB, and so
            # keywords._split_terms can pull the geography back out.
            "terms": POLICING_TERMS + [(CONTEXT, list(s["places"]))],
            "region": list(s["places"]),
            "region_out": elsewhere,
            "pack": "policing",
            "enabled": True,
            "builtin": True,
        })
    return out


def default_beat_id() -> str:
    """Delhi, because that is the edition this was built against."""
    return "delhi-police"
