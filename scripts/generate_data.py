"""Synthetic data generator for the NBI PoC.  ALL DATA IS INVENTED.

Run from the project root:   python -m scripts.generate_data

Writes three files into data/:
  watchlist.csv     2,000 fake watchlist records
  applicants.csv    10,000 fake applicants (NO labels inside)
  ground_truth.csv  the hidden answer key, used ONLY for training/evaluation
"""
import csv
import random
import re
from datetime import date, timedelta
from pathlib import Path

from faker import Faker

# ---------------------------------------------------------------- settings
SEED = 42                  # fixed seed: same data on every run
N_APPLICANTS = 10_000
N_WATCHLIST = 2_000
SHARE_TRUE = 0.05          # about 5% true matches
SHARE_NAMESAKE = 0.25      # about 25% namesakes; the rest are unrelated
TEST_FRACTION = 0.30       # 30% of each category is held out for testing
DATA_DIR = Path(__file__).resolve().parent.parent / "data"

# CSV column names (applicants.csv deliberately has NO label column)
APPLICANT_FIELDS = [
    "reference_no", "first_name", "middle_name", "last_name", "birthdate",
    "birthplace", "mother_maiden_name", "email",
]
WATCHLIST_FIELDS = [
    "record_no", "first_name", "middle_name", "last_name", "birthdate",
    "birthplace", "mother_maiden_name", "case_reference", "offense_category",
]
TRUTH_FIELDS = [
    "applicant_reference_no", "category", "linked_record_no",
    "is_true_match", "split",
]

# ------------------------------------------------------- name/place lists
# Very common Filipino surnames. Many people share these, which is exactly
# what causes the "HIT" bottleneck. Earlier in the list = more common.
COMMON_SURNAMES = [
    "Santos", "Reyes", "Cruz", "Bautista", "Ocampo", "Garcia", "Mendoza",
    "Torres", "Tomas", "Andrada", "Castillo", "Flores", "Villanueva", "Ramos",
    "Dela Cruz", "Gonzales", "Aquino", "Navarro", "Salazar", "Pascual",
    "Hernandez", "Lopez", "Rivera", "Santiago", "Domingo", "Soriano",
    "Manalo", "Valdez", "Del Rosario", "Aguilar", "Mercado", "Fernandez",
    "Marquez", "Diaz", "Perez", "Sanchez", "Corpuz", "Dizon", "Esteban",
    "Galang",
]
COMMON_FIRST_NAMES = [
    "Jose", "Juan", "Maria", "Ana", "Mark", "John", "Michael", "Joseph",
    "Angelica", "Jennifer", "Christian", "Jerome", "Catherine", "Kristine",
    "Mary", "Ramon", "Carlo", "Paolo", "Patrick", "Joshua", "Daniel",
    "Jessica", "Rica", "Joel", "Rodel", "Rowena", "Noel", "Arnel", "Cherry",
    "Jun",
]
FEMALE_FIRST_NAMES = [   # used for mothers' first names
    "Maria", "Ana", "Rosario", "Teresita", "Luzviminda", "Remedios",
    "Corazon", "Josefina", "Lourdes", "Gloria", "Imelda", "Carmelita",
    "Evelyn", "Mercedes", "Lorna", "Nenita", "Cristina", "Elena", "Rowena",
    "Norma",
]
BIRTHPLACES = [
    "Manila", "Quezon City", "Makati", "Pasig", "Taguig", "Caloocan",
    "Cebu City", "Davao City", "Iloilo City", "Bacolod", "Cagayan de Oro",
    "Zamboanga City", "Baguio", "Angeles City", "San Fernando Pampanga",
    "Batangas City", "Lipa", "Tarlac City", "Naga City", "Legazpi",
    "Tacloban", "Dagupan", "Laoag", "Puerto Princesa", "General Santos",
    "Butuan", "Antipolo", "Cavite City", "Malolos", "Lucena",
]
OFFENSE_CATEGORIES = ["SYNTHETIC_A", "SYNTHETIC_B", "SYNTHETIC_C", "SYNTHETIC_D"]

# Realistic spelling variations of the same first name
NAME_VARIANTS = {
    "Maria": "Ma.", "Mark": "Marc", "Christian": "Cristian",
    "Catherine": "Katherine", "Kristine": "Christine", "Jerome": "Jerom",
    "Jessica": "Jesica", "Joseph": "Josef", "Jennifer": "Jenifer",
    "Michael": "Mikael",
}


def _weights(items, exponent):
    """Earlier items get larger weights (a few names are very common)."""
    return [1 / (i + 1) ** exponent for i in range(len(items))]


SURNAME_WEIGHTS = _weights(COMMON_SURNAMES, 0.7)
FIRST_NAME_WEIGHTS = _weights(COMMON_FIRST_NAMES, 0.7)
PLACE_WEIGHTS = _weights(BIRTHPLACES, 0.5)


# ------------------------------------------------------------ random helpers
def pick_surname(rng, fake):
    """55% a very common surname, 45% any surname from Faker."""
    if rng.random() < 0.55:
        return rng.choices(COMMON_SURNAMES, SURNAME_WEIGHTS)[0]
    return fake.last_name().strip()


def pick_first_name(rng, fake):
    """50% a very common first name, 50% any first name from Faker."""
    if rng.random() < 0.50:
        return rng.choices(COMMON_FIRST_NAMES, FIRST_NAME_WEIGHTS)[0]
    return fake.first_name().strip()


def random_birthdate(rng):
    start, end = date(1960, 1, 1), date(2005, 12, 31)
    return start + timedelta(days=rng.randrange((end - start).days + 1))


def make_mother(rng, fake):
    """Return (mother's maiden name, her surname).
    In the Philippines the middle name is usually the mother's maiden surname."""
    surname = pick_surname(rng, fake)
    first = rng.choice(FEMALE_FIRST_NAMES)
    return f"{first} {surname}", surname


def make_person(rng, fake):
    """A brand-new random person."""
    mother_name, mother_surname = make_mother(rng, fake)
    return {
        "first_name": pick_first_name(rng, fake),
        "middle_name": mother_surname,
        "last_name": pick_surname(rng, fake),
        "birthdate": random_birthdate(rng),
        "birthplace": rng.choices(BIRTHPLACES, PLACE_WEIGHTS)[0],
        "mother_maiden_name": mother_name,
    }


def typo(rng, text):
    """Introduce one small typing mistake (delete, swap, double, or replace a letter)."""
    if len(text) < 4:
        return text
    for _ in range(5):
        i = rng.randrange(1, len(text) - 1)
        op = rng.choice(["delete", "swap", "double", "replace"])
        chars = list(text)
        if op == "delete":
            del chars[i]
        elif op == "swap":
            chars[i], chars[i + 1] = chars[i + 1], chars[i]
        elif op == "double":
            chars.insert(i, chars[i])
        else:
            chars[i] = rng.choice("aeiou")
        new = "".join(chars)
        if new.lower() != text.lower():
            return new
    return text


def vary_name(rng, name):
    """A known spelling variation if we have one, otherwise a typo."""
    return NAME_VARIANTS.get(name) or typo(rng, name)


def corrupt_birthdate(rng, d):
    """A realistic data-entry error: day and month swapped, or year off by one."""
    if d.day <= 12 and d.day != d.month and rng.random() < 0.5:
        return date(d.year, d.day, d.month)
    return date(d.year + rng.choice([-1, 1]), d.month, min(d.day, 28))


# ------------------------------------------------ applicants from watchlist
def make_true_match(rng, w):
    """The SAME person as watchlist record w, but entered with typos/variations."""
    p = {k: w[k] for k in
         ("first_name", "middle_name", "last_name", "birthdate",
          "birthplace", "mother_maiden_name")}
    if rng.random() < 0.55:
        p["first_name"] = vary_name(rng, p["first_name"])
    if rng.random() < 0.25:
        p["last_name"] = typo(rng, p["last_name"])
    r = rng.random()
    if r < 0.15:
        p["middle_name"] = p["middle_name"][:1] + "."   # only an initial
    elif r < 0.25:
        p["middle_name"] = ""                            # left blank

    bad_date = rng.random() < 0.25
    bad_place = rng.random() < 0.15
    bad_mother = rng.random() < 0.20
    # DESIGN CHOICE: at least one identifier stays exact. A true match whose
    # everything is wrong would be impossible for any system to recognise.
    if bad_date and bad_place and bad_mother:
        bad_date = False
    if bad_date:
        p["birthdate"] = corrupt_birthdate(rng, p["birthdate"])
    if bad_place:
        p["birthplace"] = typo(rng, p["birthplace"])
    if bad_mother:
        p["mother_maiden_name"] = typo(rng, p["mother_maiden_name"])
    return p


def make_namesake(rng, fake, w):
    """A DIFFERENT person who shares watchlist record w's surname (and usually
    first name). At least one of birthdate / birthplace / mother's maiden name
    genuinely differs; the others may coincidentally match (that is what makes
    some namesakes hard)."""
    p = {"last_name": w["last_name"]}
    p["first_name"] = (w["first_name"] if rng.random() < 0.75
                       else vary_name(rng, w["first_name"]))

    n_differ = rng.choices([1, 2, 3], [0.20, 0.35, 0.45])[0]
    differ = set(rng.sample(["birthdate", "birthplace", "mother"], n_differ))

    if "birthdate" in differ:
        while True:   # a date more than 30 days away from the watchlist person's
            d = random_birthdate(rng)
            if abs((d - w["birthdate"]).days) > 30:
                break
        p["birthdate"] = d
    else:
        p["birthdate"] = w["birthdate"]

    if "birthplace" in differ:
        p["birthplace"] = rng.choice([b for b in BIRTHPLACES if b != w["birthplace"]])
    else:
        p["birthplace"] = w["birthplace"]

    if "mother" in differ:
        while True:
            mother_name, mother_surname = make_mother(rng, fake)
            if mother_name != w["mother_maiden_name"]:
                break
        p["mother_maiden_name"], p["middle_name"] = mother_name, mother_surname
    else:
        p["mother_maiden_name"], p["middle_name"] = w["mother_maiden_name"], w["middle_name"]
    return p


# ------------------------------------------------------------- main builder
def generate(seed=SEED):
    """Build everything in memory. Returns (watchlist, applicants, truth)."""
    rng = random.Random(seed)
    fake = Faker("en_PH")
    fake.seed_instance(seed)

    # 1) Watchlist records
    watchlist = []
    for n in range(1, N_WATCHLIST + 1):
        p = make_person(rng, fake)
        p["record_no"] = f"WL-{n:05d}"
        p["case_reference"] = f"SYNTH-{rng.randint(2012, 2024)}-{n:06d}"
        p["offense_category"] = rng.choice(OFFENSE_CATEGORIES)
        watchlist.append(p)

    # 2) Decide each applicant's category, then shuffle so that reference
    #    numbers do NOT reveal the category.
    n_true = round(N_APPLICANTS * SHARE_TRUE)
    n_namesake = round(N_APPLICANTS * SHARE_NAMESAKE)
    plan = (["TRUE_MATCH"] * n_true + ["NAMESAKE"] * n_namesake
            + ["UNRELATED"] * (N_APPLICANTS - n_true - n_namesake))
    rng.shuffle(plan)

    true_sources = iter(rng.sample(watchlist, n_true))  # 500 DIFFERENT records

    # 3) Build applicants
    built = []
    for category in plan:
        if category == "TRUE_MATCH":
            w = next(true_sources)
            built.append((category, w["record_no"], make_true_match(rng, w)))
        elif category == "NAMESAKE":
            w = rng.choice(watchlist)
            built.append((category, w["record_no"], make_namesake(rng, fake, w)))
        else:
            built.append((category, "", make_person(rng, fake)))

    # 4) Train/test split, done separately inside each category
    split = ["train"] * len(built)
    for category in ("TRUE_MATCH", "NAMESAKE", "UNRELATED"):
        idx = [i for i, b in enumerate(built) if b[0] == category]
        rng.shuffle(idx)
        for i in idx[: round(len(idx) * TEST_FRACTION)]:
            split[i] = "test"

    # 5) Final rows
    applicants, truth = [], []
    for i, (category, linked, p) in enumerate(built, start=1):
        ref = f"APP-{i:06d}"
        slug = lambda s: re.sub(r"[^a-z0-9]", "", s.lower())
        row = dict(p)
        row["reference_no"] = ref
        row["email"] = f"{slug(p['first_name'])}.{slug(p['last_name'])}{i}@example.com"
        applicants.append(row)
        truth.append({
            "applicant_reference_no": ref,
            "category": category,
            "linked_record_no": linked,
            "is_true_match": 1 if category == "TRUE_MATCH" else 0,
            "split": split[i - 1],
        })
    return watchlist, applicants, truth


def write_csv(path, fieldnames, rows):
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(rows)
    print(f"Wrote {path.relative_to(DATA_DIR.parent)}  ({len(rows)} rows)")


def main():
    watchlist, applicants, truth = generate()
    DATA_DIR.mkdir(exist_ok=True)
    write_csv(DATA_DIR / "watchlist.csv", WATCHLIST_FIELDS, watchlist)
    write_csv(DATA_DIR / "applicants.csv", APPLICANT_FIELDS, applicants)
    write_csv(DATA_DIR / "ground_truth.csv", TRUTH_FIELDS, truth)

    # A quick summary so you can SEE the name-collision problem (pain point #1)
    cats, splits = {}, {}
    for t in truth:
        cats[t["category"]] = cats.get(t["category"], 0) + 1
        splits[t["split"]] = splits.get(t["split"], 0) + 1
    wl_surnames = {w["last_name"].lower() for w in watchlist}
    wl_fullnames = {(w["first_name"].lower(), w["last_name"].lower()) for w in watchlist}
    same_surname = sum(a["last_name"].lower() in wl_surnames for a in applicants)
    same_fullname = sum(
        (a["first_name"].lower(), a["last_name"].lower()) in wl_fullnames
        for a in applicants
    )
    print()
    print("Category mix :", cats)
    print("Split        :", splits)
    print(f"Applicants sharing a surname with at least one watchlist record: {same_surname}")
    print(f"Applicants sharing first+last name with a watchlist record     : {same_fullname}")
    print("(Only 500 of the 10,000 are real matches. That gap is the HIT bottleneck.)")


if __name__ == "__main__":
    main()