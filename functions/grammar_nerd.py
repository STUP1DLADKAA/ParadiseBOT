"""English-only grammar and spelling mistake detector with nerd replies.

Design goals:
- Trigger ONLY on genuine English-language sentences (never Hinglish,
  Devanagari, Spanish/French/other languages, code blocks, URLs, mentions,
  single-word fragments, or valid UK/US spelling variants).
- Catch unmistakable English spelling mistakes and classic contextual
  grammar slips ("its" vs "it's", "your" vs "you're", "could of", "alot",
  "didn't knew", "better then", etc.).
- Format corrections in peak insufferable nerd voice ("🤓👆 err its akchewly ...").
"""

from __future__ import annotations

import random
import re
from collections import deque
from dataclasses import dataclass

_CODE_BLOCK_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`]*`")
_URL_RE = re.compile(r"https?://\S+|www\.\S+", re.IGNORECASE)
_DISCORD_TOKEN_RE = re.compile(r"<(?:@!?|@&|#|a?:[A-Za-z0-9_]+:)\d+>")
_QUOTE_LINE_RE = re.compile(r"^\s*>.*$", re.MULTILINE)
_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")
_SERIOUS_TOPIC_RE = re.compile(
    r"\b(passed away|died|funeral|hospital|cancer|suicide|self[- ]harm|depressed|grief|condolences|rest in peace)\b",
    re.IGNORECASE,
)

# Any non-ASCII letter outside basic Latin punctuation/emojis (e.g. Devanagari,
# Arabic, Cyrillic, CJK, accented European scripts) signals non-English text.
_NON_ENGLISH_LETTER_RE = re.compile(
    r"[\u00C0-\u024F\u0370-\u03FF\u0400-\u052F\u0590-\u06FF\u0900-\u0DFF\u0E00-\u0FFF\u3040-\u9FFF\uAC00-\uD7AF]"
)

# Common Romanized Hindi / Hinglish / Urdu & non-English conversational words.
# If ANY word in the message lands here, we treat the sentence as non-English
# (or code-switched Hinglish) and stay completely quiet.
_NON_ENGLISH_MARKERS: frozenset[str] = frozenset(
    {
        # Hinglish / Hindi / Urdu
        "aaj", "aaja", "aao", "aap", "aapka", "aapki", "aaya", "aayi", "abey",
        "abhi", "accha", "acha", "agar", "apna", "apne", "apni", "arey", "arre",
        "aur", "baad", "baat", "bahut", "bakwas", "bas", "bata", "batao", "bc",
        "bhai", "bhaai", "bhag", "bhi", "bht", "bilkul", "bkl", "bol", "bola",
        "boli", "bolo", "bohot", "chal", "chala", "chali", "chalo", "chahiye",
        "chup", "dekh", "dekha", "dekho", "dimag", "dost", "ek", "ekdum", "fir",
        "galat", "ghar", "gaya", "gayi", "gaye", "haan", "hai", "hain", "hamara",
        "hanji", "hota", "hoti", "hote", "hua", "hui", "hum", "idhar", "inka",
        "iska", "iski", "iske", "isliye", "itna", "itni", "jaise", "jaldi",
        "jao", "jhooth", "ji", "jyada", "kaam", "kab", "kabhi", "kahan", "kaisa",
        "kaise", "kaisi", "kal", "kar", "kara", "kare", "karenge", "karke",
        "karna", "karo", "karta", "karti", "karte", "kaun", "khana", "kidhar",
        "kis", "kisi", "kiska", "kiya", "koi", "krha", "krna", "kro", "kuch",
        "kuchh", "kya", "kyu", "kyuki", "kyun", "kyunki", "laga", "lagta",
        "lagti", "lekin", "log", "magar", "maloom", "mast", "mat", "matlab",
        "mc", "mein", "mera", "mere", "meri", "mila", "mili", "milte", "mtlb",
        "mujhe", "mujhko", "naa", "nahi", "nhi", "nikal", "paani", "pagal",
        "paisa", "pakka", "parso", "pata", "pehle", "phir", "raat", "raha",
        "rahe", "rahi", "rha", "rhe", "rhi", "roz", "ruk", "ruko", "saala",
        "sab", "sach", "sahi", "sala", "samajh", "shaam", "shayad", "subah",
        "sun", "suna", "suno", "tab", "tabhi", "tera", "tere", "teri", "tha",
        "theek", "thi", "thik", "thoda", "tum", "tumhara", "tumhari", "udhar",
        "unka", "unki", "uske", "uski", "uska", "waise", "wala", "wale", "wali",
        "woh", "yaar", "yahan", "yeh", "yr", "zyada",
        # Common Spanish / French / German / Portuguese / Indonesian markers
        "hola", "como", "estas", "pero", "para", "porque", "esto", "esta",
        "estoy", "tengo", "puedo", "muy", "bueno", "gracias", "amigo", "hacer",
        "avec", "dans", "pour", "mais", "cest", "suis", "tres", "merci",
        "nicht", "aber", "oder", "mein", "dein", "sehr", "warum",
        "tidak", "yang", "bisa", "sama", "kalau", "banget",
    }
)

# High-frequency English vocabulary used to confirm that the surrounding
# sentence is genuinely written in English.
_CORE_ENGLISH_WORDS: frozenset[str] = frozenset(
    {
        "a", "about", "above", "actually", "after", "again", "all", "almost",
        "alone", "along", "already", "also", "always", "am", "an", "and",
        "another", "any", "anyone", "anything", "anyway", "are", "aren't",
        "arent", "around", "as", "ask", "asked", "at", "away", "back", "bad",
        "be", "because", "been", "before", "being", "best", "better", "between",
        "big", "bit", "both", "bot", "bro", "broke", "broken", "bruh", "but",
        "by", "call", "called", "came", "can", "can't", "cannot", "cant", "care",
        "change", "changed", "channel", "chat", "check", "close", "come",
        "coming", "cool", "could", "couldn't", "couldnt", "crazy", "damn", "day",
        "days", "dead", "did", "didn't", "didnt", "different", "do", "does",
        "doesn't", "doesnt", "doing", "don't", "done", "dont", "down", "dude",
        "dumb", "each", "early", "easy", "eat", "else", "end", "enough", "even",
        "ever", "every", "everyone", "everything", "examen", "excited", "extra",
        "fact", "far", "fast", "feel", "feels", "felt", "few", "file", "find",
        "fine", "first", "fix", "fixed", "for", "forgot", "found", "free",
        "friend", "friends", "from", "full", "fun", "funny", "game", "games",
        "gave", "get", "gets", "getting", "give", "go", "god", "goes", "going",
        "gonna", "gone", "good", "got", "great", "guess", "guy", "guys", "had",
        "half", "hand", "happen", "happened", "happening", "happy", "hard",
        "has", "hasn't", "hate", "have", "haven't", "having", "he", "he's",
        "head", "hear", "heard", "hell", "help", "her", "here", "hey", "high",
        "him", "his", "hit", "hold", "home", "honestly", "hope", "hour", "hours",
        "house", "how", "however", "huge", "i", "i'd", "i'll", "i'm", "i've",
        "id", "idea", "idk", "if", "ill", "im", "imo", "in", "insane", "instead",
        "into", "is", "isn't", "isnt", "issue", "it", "it's", "its", "ive",
        "job", "join", "joined", "joke", "joking", "just", "keep", "kept",
        "kidding", "kind", "kinda", "knew", "know", "knows", "last", "late",
        "later", "least", "leave", "left", "less", "let", "let's", "lets",
        "life", "like", "line", "literally", "little", "live", "lol", "lmao",
        "long", "look", "looking", "looks", "lose", "losing", "lost", "lot",
        "love", "lowkey", "made", "make", "makes", "making", "man", "many",
        "match", "matter", "may", "maybe", "me", "mean", "means", "meant",
        "message", "might", "mind", "minute", "minutes", "mine", "miss",
        "mistake", "moment", "money", "more", "morning", "most", "move", "much",
        "must", "my", "myself", "name", "near", "need", "needs", "never", "new",
        "next", "ngl", "nice", "night", "no", "nobody", "none", "normal", "not",
        "nothing", "now", "number", "of", "off", "often", "oh", "ok", "okay",
        "old", "on", "once", "one", "online", "only", "open", "or", "other",
        "our", "out", "over", "own", "part", "people", "person", "phone", "pick",
        "place", "plan", "play", "playing", "please", "point", "pretty",
        "probably", "problem", "put", "question", "quick", "quite", "random",
        "rather", "read", "reading", "ready", "real", "really", "reason",
        "right", "room", "round", "run", "running", "said", "same", "saw",
        "say", "saying", "says", "school", "second", "see", "seem", "seems",
        "seen", "send", "sent", "sense", "seriously", "server", "set", "she",
        "she's", "should", "shouldn't", "shouldnt", "show", "side", "since",
        "sleep", "sleeping", "slow", "small", "smart", "so", "some", "someone",
        "something", "sometimes", "soon", "sorry", "sound", "sounds", "start",
        "started", "stay", "still", "stop", "story", "stuff", "stupid", "such",
        "sure", "system", "take", "taken", "takes", "taking", "talk", "talking",
        "tbh", "team", "tell", "telling", "test", "than", "thank", "thanks",
        "that", "that's", "thats", "the", "their", "them", "then", "there",
        "there's", "theres", "these", "they", "they're", "theyre", "thing",
        "things", "think", "thinking", "this", "those", "though", "thought",
        "three", "through", "time", "times", "tired", "to", "today", "together",
        "told", "tomorrow", "tonight", "too", "took", "top", "totally", "tried",
        "true", "try", "trying", "turn", "two", "type", "typing", "under",
        "understand", "until", "up", "update", "us", "use", "used", "using",
        "usually", "very", "voice", "wait", "waiting", "wanna", "want", "wanted",
        "was", "wasn't", "wasnt", "watch", "watching", "water", "way", "we",
        "we're", "week", "weird", "welcome", "well", "went", "were", "what",
        "what's", "whatever", "whats", "when", "where", "which", "while", "who",
        "whole", "why", "wild", "will", "win", "winning", "with", "without",
        "won", "won't", "wont", "word", "words", "work", "working", "works",
        "world", "worse", "worst", "would", "wouldn't", "wouldnt", "wrong",
        "yeah", "year", "years", "yes", "yesterday", "yet", "yo", "you",
        "you'd", "you'll", "you're", "you've", "your", "youre", "yourself",
    }
)

_ENGLISH_SUFFIXES: tuple[str, ...] = (
    "ing", "ed", "ly", "tion", "sion", "ness", "ment", "able", "ible", "ful", "less"
)

# Unmistakable English spelling mistakes (excludes UK/US variants & slang).
SPELLING_MISTAKES: dict[str, str] = {
    "accomodate": "accommodate",
    "achievment": "achievement",
    "actully": "actually",
    "adress": "address",
    "agressive": "aggressive",
    "aparent": "apparent",
    "appearence": "appearance",
    "arguement": "argument",
    "assassination": "assassination",
    "basicly": "basically",
    "becuase": "because",
    "beacuse": "because",
    "begining": "beginning",
    "beleive": "believe",
    "belive": "believe",
    "buisness": "business",
    "calender": "calendar",
    "catagory": "category",
    "cemetary": "cemetery",
    "collegue": "colleague",
    "comming": "coming",
    "commited": "committed",
    "comparisson": "comparison",
    "completly": "completely",
    "concious": "conscious",
    "curiousity": "curiosity",
    "decieve": "deceive",
    "definately": "definitely",
    "definatly": "definitely",
    "definetly": "definitely",
    "definitly": "definitely",
    "desparate": "desperate",
    "diffrent": "different",
    "dilema": "dilemma",
    "dissapear": "disappear",
    "dissapoint": "disappoint",
    "dissapointed": "disappointed",
    "embarass": "embarrass",
    "embarassing": "embarrassing",
    "enviroment": "environment",
    "especally": "especially",
    "exagerate": "exaggerate",
    "existance": "existence",
    "experiance": "experience",
    "familar": "familiar",
    "finaly": "finally",
    "foriegn": "foreign",
    "fourty": "forty",
    "freind": "friend",
    "freinds": "friends",
    "garantee": "guarantee",
    "gaurd": "guard",
    "goverment": "government",
    "grammer": "grammar",
    "greatful": "grateful",
    "happend": "happened",
    "harrass": "harass",
    "heighth": "height",
    "hygeine": "hygiene",
    "ignorence": "ignorance",
    "immediatly": "immediately",
    "independant": "independent",
    "intresting": "interesting",
    "interupt": "interrupt",
    "irresistable": "irresistible",
    "jelous": "jealous",
    "knowlege": "knowledge",
    "liason": "liaison",
    "liesure": "leisure",
    "litrally": "literally",
    "lonley": "lonely",
    "maintainance": "maintenance",
    "millenium": "millennium",
    "mischievious": "mischievous",
    "mispell": "misspell",
    "mispelled": "misspelled",
    "naturaly": "naturally",
    "neccessary": "necessary",
    "necesary": "necessary",
    "nieghbor": "neighbor",
    "ninty": "ninety",
    "noticable": "noticeable",
    "obvios": "obvious",
    "occassion": "occasion",
    "occured": "occurred",
    "ocurred": "occurred",
    "oppinion": "opinion",
    "oppurtunity": "opportunity",
    "origional": "original",
    "outragous": "outrageous",
    "parliment": "parliament",
    "peice": "piece",
    "perseverence": "perseverance",
    "persistant": "persistent",
    "politican": "politician",
    "posession": "possession",
    "practicle": "practical",
    "prefered": "preferred",
    "presense": "presence",
    "priviledge": "privilege",
    "probabaly": "probably",
    "probaly": "probably",
    "questionaire": "questionnaire",
    "realy": "really",
    "reciept": "receipt",
    "recieve": "receive",
    "recieved": "received",
    "recogize": "recognize",
    "recomend": "recommend",
    "rediculous": "ridiculous",
    "referance": "reference",
    "refered": "referred",
    "relavant": "relevant",
    "religous": "religious",
    "remeber": "remember",
    "repitition": "repetition",
    "resistence": "resistance",
    "restaraunt": "restaurant",
    "rythm": "rhythm",
    "scedule": "schedule",
    "sentance": "sentence",
    "seperate": "separate",
    "seperated": "separated",
    "sieze": "seize",
    "similiar": "similar",
    "sincerly": "sincerely",
    "speach": "speech",
    "speling": "spelling",
    "strenght": "strength",
    "studing": "studying",
    "subconcious": "subconscious",
    "succesful": "successful",
    "supercede": "supersede",
    "supposably": "supposedly",
    "supress": "suppress",
    "suprise": "surprise",
    "suprised": "surprised",
    "surprize": "surprise",
    "tatoo": "tattoo",
    "temperture": "temperature",
    "tendancy": "tendency",
    "therefor": "therefore",
    "threshhold": "threshold",
    "thier": "their",
    "tommorow": "tomorrow",
    "tommorrow": "tomorrow",
    "tounge": "tongue",
    "truely": "truly",
    "unforseen": "unforeseen",
    "unfortunatly": "unfortunately",
    "untill": "until",
    "usally": "usually",
    "wierd": "weird",
    "wich": "which",
    "writting": "writing",
}
del SPELLING_MISTAKES["assassination"]  # keep only genuine misspellings

_PAST_TO_BASE: dict[str, str] = {
    "ate": "eat",
    "bought": "buy",
    "broke": "break",
    "brought": "bring",
    "came": "come",
    "caught": "catch",
    "chose": "choose",
    "drove": "drive",
    "fell": "fall",
    "felt": "feel",
    "forgot": "forget",
    "fought": "fight",
    "found": "find",
    "gave": "give",
    "got": "get",
    "heard": "hear",
    "kept": "keep",
    "knew": "know",
    "left": "leave",
    "lost": "lose",
    "made": "make",
    "meant": "mean",
    "met": "meet",
    "paid": "pay",
    "ran": "run",
    "said": "say",
    "saw": "see",
    "sent": "send",
    "slept": "sleep",
    "spoke": "speak",
    "spent": "spend",
    "stood": "stand",
    "taught": "teach",
    "thought": "think",
    "told": "tell",
    "took": "take",
    "understood": "understand",
    "went": "go",
    "won": "win",
    "wore": "wear",
    "wrote": "write",
}

# Contextual grammar patterns: (compiled_regex, resolver(match) -> (wrong, right))
_GRAMMAR_RULES: tuple[tuple[re.Pattern[str], object], ...] = (
    # "alot" -> "a lot"
    (
        re.compile(r"\balot\b", re.IGNORECASE),
        lambda m: (m.group(0), "a lot"),
    ),
    # "could of / would of / should of / must of / might of" -> "could've" etc.
    (
        re.compile(
            r"\b(could|would|should|must|might)\s+of\b(?!\s+(?:course|all|them|us|you|it\b))",
            re.IGNORECASE,
        ),
        lambda m: (m.group(0), f"{m.group(1).lower()}'ve"),
    ),
    # "its" when contraction "it's" is unambiguously meant
    (
        re.compile(
            r"\bits\s+(?:a|an|the|so|too|very|really|just|not|been|going|gonna|getting|"
            r"still|always|never|already|almost|pretty|quite|such|only|actually|"
            r"literally|completely|totally|over|fine|okay|good|bad|hard|easy|crazy|"
            r"wild|funny|weird|broken|working|about|like|time)\b",
            re.IGNORECASE,
        ),
        lambda m: ("its", "it's"),
    ),
    # Possessive "its" mistakenly written as "it's" ("on it's own")
    (
        re.compile(r"\b(?:on|in|for|of|by|with|into)\s+(it's)\s+own\b", re.IGNORECASE),
        lambda m: (m.group(1), "its"),
    ),
    # "your" when "you're" is unambiguously meant
    (
        re.compile(
            r"\byour\s+(?:welcome|right|wrong|kidding|joking|lying|going|gonna|doing|"
            r"being|making|getting|coming|looking|talking|saying|trying|playing|"
            r"watching|acting|losing|winning|cooked|done|late|early|crazy|insane|"
            r"stupid|smart|funny|weird|awesome|amazing|literally|actually|probably|"
            r"definitely|not\s+(?:going|even|ready|allowed|supposed|wrong|right)|"
            r"so\s+(?:good|bad|right|wrong|funny|slow|fast|late|annoying|dumb|smart)|"
            r"too\s+(?:slow|fast|late|good|bad)|"
            r"a\s+(?:legend|clown|nerd|bot|god|goat|genius|loser|winner))\b",
            re.IGNORECASE,
        ),
        lambda m: ("your", "you're"),
    ),
    # "their is / their are / their was / their were" -> "there ..."
    (
        re.compile(r"\b(their)\s+(is|are|was|were)\b", re.IGNORECASE),
        lambda m: (
            f"{m.group(1)} {m.group(2)}",
            f"there {m.group(2).lower()}",
        ),
    ),
    # "their/there going/gonna/doing/trying/playing" -> "they're ..."
    (
        re.compile(
            r"\b(their|there)\s+(going|gonna|doing|trying|coming|making|playing|winning|losing)\b",
            re.IGNORECASE,
        ),
        lambda m: (
            f"{m.group(1)} {m.group(2)}",
            f"they're {m.group(2).lower()}",
        ),
    ),
    # Comparative + "then" -> "than"
    (
        re.compile(
            r"\b(better|worse|faster|slower|bigger|smaller|harder|easier|older|younger|"
            r"higher|lower|smarter|dumber|stronger|weaker|rather)\s+(then)\b",
            re.IGNORECASE,
        ),
        lambda m: (
            f"{m.group(1)} {m.group(2)}",
            f"{m.group(1).lower()} than",
        ),
    ),
    # Double comparative "more better / more worse / more faster ..."
    (
        re.compile(
            r"\bmore\s+(better|worse|faster|slower|bigger|smaller|smarter|dumber|easier|harder|stronger)\b",
            re.IGNORECASE,
        ),
        lambda m: (m.group(0), m.group(1).lower()),
    ),
    # "didn't knew / didn't went / did not saw ..." -> base verb
    (
        re.compile(
            r"\b(did(?:n'?t|\s+not))\s+("
            + "|".join(sorted(_PAST_TO_BASE))
            + r")\b",
            re.IGNORECASE,
        ),
        lambda m: (
            m.group(0),
            (
                "didn't"
                if m.group(1).lower() in {"didnt", "didn't"}
                else "did not"
            )
            + f" {_PAST_TO_BASE[m.group(2).lower()]}",
        ),
    ),
    # "a" before clear vowel-sound nouns/adjectives -> "an"
    (
        re.compile(
            r"\b(a)\s+(apple|orange|idiot|issue|idea|example|error|hour|honest|"
            r"actual|amazing|awesome|easy|obvious|interesting|important|entire|"
            r"umbrella|elephant|accident|excuse|argument|answer|attempt|option|opinion)\b",
            re.IGNORECASE,
        ),
        lambda m: (
            f"{m.group(1)} {m.group(2)}",
            f"an {m.group(2).lower()}",
        ),
    ),
    # "loose" where verb "lose" is clearly intended
    (
        re.compile(
            r"\b(gonna|going\s+to|will|would|don'?t|did(?:n'?t)?|can'?t|never|about\s+to)\s+(loose)\b",
            re.IGNORECASE,
        ),
        lambda m: (
            f"{m.group(1)} {m.group(2)}",
            {"dont": "don't", "didnt": "didn't", "cant": "can't"}.get(
                m.group(1).lower(), m.group(1).lower()
            )
            + " lose",
        ),
    ),
)

NERD_TEMPLATES: tuple[str, ...] = (
    '🤓👆 err its akchewly **"{right}"**',
    '🤓☝️ erm, akchewly it\'s **"{right}"**, not "{wrong}"',
    '🤓👆 *pushes up glasses* umm akshually you meant **"{right}"**, not "{wrong}"',
    '🤓☝️ err hold on, according to the Oxford dictionary it\'s akchewly **"{right}"** *snort*',
    '🤓👆 erm excuse me, "{wrong}"?? it\'s akchewly **"{right}"** pal',
    '🤓☝️ *adjusts bowtie* ahem, technically speaking the correct English is **"{right}"**, not "{wrong}"',
    '🤓👆 umm akchewly "{wrong}" is incorrect, please write **"{right}"** before my glasses fog up',
    '🤓☝️ err my grammar sensors are beeping — "{wrong}" should akchewly be **"{right}"**',
    '🤓👆 *taps chalkboard aggressively* erm no, it\'s akchewly **"{right}"**',
    '🤓☝️ um akshually *inhales* it\'s **"{right}"**. minus 5 points from your report card',
    '🤓👆 ermm technically "{wrong}" isn\'t right here, it\'s akchewly **"{right}"** *snorts in nerd*',
    '🤓☝️ hold everything, did you just type "{wrong}" instead of **"{right}"**?? akchewly tragic',
)


@dataclass(frozen=True, slots=True)
class GrammarMistake:
    wrong: str
    right: str
    kind: str  # "grammar" or "spelling"


def _strip_noise(text: str) -> str:
    cleaned = _CODE_BLOCK_RE.sub(" ", text or "")
    cleaned = _INLINE_CODE_RE.sub(" ", cleaned)
    cleaned = _QUOTE_LINE_RE.sub(" ", cleaned)
    cleaned = _URL_RE.sub(" ", cleaned)
    cleaned = _DISCORD_TOKEN_RE.sub(" ", cleaned)
    return " ".join(cleaned.split())


def _looks_english_word(word_lower: str) -> bool:
    if word_lower in _CORE_ENGLISH_WORDS or word_lower in SPELLING_MISTAKES:
        return True
    if word_lower in _PAST_TO_BASE:
        return True
    if word_lower.endswith("s") and len(word_lower) > 3 and word_lower[:-1] in _CORE_ENGLISH_WORDS:
        return True
    for suffix in _ENGLISH_SUFFIXES:
        if len(word_lower) > len(suffix) + 2 and word_lower.endswith(suffix):
            return True
    return False


def is_english_sentence(text: str) -> bool:
    """Return True only when ``text`` is a multi-word English-language sentence."""
    cleaned = _strip_noise(text)
    if len(cleaned) < 10:
        return False
    if _NON_ENGLISH_LETTER_RE.search(cleaned):
        return False

    words = _WORD_RE.findall(cleaned)
    if len(words) < 3:
        return False

    lowered = [w.lower() for w in words]
    if any(w in _NON_ENGLISH_MARKERS for w in lowered):
        return False

    english_hits = sum(1 for w in lowered if _looks_english_word(w))
    # Require at least 2 confirmed English words AND >= 60% of tokens to look English.
    return english_hits >= 2 and (english_hits / len(lowered)) >= 0.60


def detect_english_mistake(text: str) -> GrammarMistake | None:
    """Detect a clear spelling or grammar mistake in an English-only sentence."""
    if not text or _SERIOUS_TOPIC_RE.search(text) or not is_english_sentence(text):
        return None

    cleaned = _strip_noise(text)

    # 1. Contextual grammar mistakes ("its" vs "it's", "your" vs "you're", etc.)
    for regex, resolver in _GRAMMAR_RULES:
        match = regex.search(cleaned)
        if match is not None:
            wrong, right = resolver(match)
            if wrong.lower() != right.lower():
                return GrammarMistake(wrong=wrong, right=right, kind="grammar")

    # 2. Unmistakable English spelling mistakes
    for match in _WORD_RE.finditer(cleaned):
        token = match.group(0)
        # Skip ALL-CAPS acronyms or shouted tokens only if <= 4 chars
        lower = token.lower()
        correction = SPELLING_MISTAKES.get(lower)
        if correction is not None:
            return GrammarMistake(wrong=token, right=correction, kind="spelling")

    return None


def format_nerd_reply(
    mistake: GrammarMistake,
    *,
    recent: deque[str] | None = None,
) -> str:
    """Format a playful nerd correction avoiding back-to-back template repeats."""
    recent_set = set(recent) if recent else set()
    candidates = [t for t in NERD_TEMPLATES if t not in recent_set] or list(NERD_TEMPLATES)
    template = random.choice(candidates)
    if recent is not None:
        recent.append(template)
    return template.format(wrong=mistake.wrong, right=mistake.right)
