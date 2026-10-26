"""Livscykel och förvaltning – en egen, sluten regeluppsättning.

Kolumner som "Tillagd", "Borttagen datum", "Status", "Inaktiv", "Ersätts av"
eller "Senast ändrad" beskriver inte begreppet utan dess *förvaltning*. Om
varje kodverk fick göra egna egenskaper av dem skulle samma sak heta
``tillagdDatum`` i ett kodverk och ``inford`` i ett annat – homonyma och
synonyma egenskaper som ingen mottagare kan tolka.

Därför är de inga egenskaper i registret, utan en egen roll
(``ColumnRole.LIFECYCLE``) med ett fåtal fasta mål. Varje mål har en regel för
eget kodverk (CodeSystem) och en för urval (ValueSet):

==================  =========================================  =========================================
mål                 eget kodverk (CodeSystem)                  urval (ValueSet)
==================  =========================================  =========================================
status              egenskap ``status`` (concept-properties)    styr om raden ingår
inactive / active   egenskap ``inactive`` (active inverteras)   styr om raden ingår
effectiveDate       egenskap ``effectiveDate``                  raden ingår först från datumet
deprecationDate     ``deprecated`` (R4) / ``deprecationDate``   raden ingår, men flaggas
retirementDate      egenskap ``retirementDate``                 raden ingår inte efter datumet
replacedBy          tillägget ``codesystem-replacedby``         flaggas om ersättaren saknas i urvalet
changed, changeNote förvaltningsuppgift i arbetskopian –        samma
versionAdded        exporteras inte (historiken finns i
versionRemoved      releaserna, diffarna och journalen)
==================  =========================================  =========================================

Värdena härleds konsekvent: ett passerat borttagsdatum ger status
``retired`` och ``inactive = true``, "Aktiv = nej" ger ``retired`` osv.
Motsägelser (status aktiv men borttagsdatum passerat) blir varningar.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable

from .registry import CONCEPT_PROPERTIES, normalize
from .replacements import REPLACED_BY_EXT

STATUS_VALUES = ["active", "experimental", "deprecated", "retired"]
STATUS_LABELS = {"active": "aktiv", "experimental": "experimentell", "deprecated": "avråds (utfasad)",
                 "retired": "avvecklad"}


@dataclass(frozen=True)
class LifecycleField:
    key: str
    label: str           # etikett i vyn och vallistan
    kind: str            # status | flag | date | code | text | version
    codesystem: str      # vad det blir i ett eget kodverk
    valueset: str        # vad det betyder i ett urval
    aliases: tuple[str, ...]
    exported: bool = True


FIELDS: dict[str, LifecycleField] = {f.key: f for f in [
    LifecycleField("status", "Status", "status", "egenskapen status (concept-properties#status)",
                   "styr om raden ingår i urvalet",
                   ("status", "begreppsstatus", "kodstatus", "tillstand", "livscykel", "livscykelstatus",
                    "giltighetsstatus", "state", "concept status")),
    LifecycleField("inactive", "Inaktiv", "flag", "egenskapen inactive (concept-properties#inactive)",
                   "inaktiva rader ingår inte",
                   ("inaktiv", "inactive", "ej aktiv", "utgangen", "avregistrerad", "avford", "makulerad",
                    "upphord", "utgatt", "borttagen kod")),
    LifecycleField("active", "Aktiv (ja/nej)", "flag", "egenskapen inactive, inverterad",
                   "rader med 'nej' ingår inte",
                   ("aktiv", "active", "aktiv kod", "i bruk", "gallande", "giltig kod", "anvands")),
    LifecycleField("effectiveDate", "Tillagd / giltig från", "date",
                   "egenskapen effectiveDate (concept-properties#effectiveDate)",
                   "raden ingår först från datumet",
                   ("tillagd", "tillagd datum", "tillagddatum", "datum tillagd", "tillagt", "inford", "inford datum",
                    "infordes", "skapad", "skapad datum", "skapaddatum", "upprattad", "giltig from", "giltig fr o m",
                    "giltig fran", "galler fran", "fran och med", "from datum", "startdatum", "effective date",
                    "ikrafttradande", "giltig fran och med")),
    LifecycleField("deprecationDate", "Utfasad / avråds från", "date",
                   "egenskapen deprecated (R4) eller deprecationDate (R5)",
                   "raden ingår men flaggas som avrådd",
                   ("utfasad", "utfasningsdatum", "utfasad datum", "avrads", "avrads fran", "avradd", "deprecated",
                    "deprecation date", "utfasas")),
    LifecycleField("retirementDate", "Borttagen / giltig till", "date",
                   "egenskapen retirementDate (concept-properties#retirementDate)",
                   "raden ingår inte efter datumet",
                   ("borttagen", "borttagen datum", "borttagetdatum", "borttaget datum", "datum borttagen",
                    "borttagningsdatum", "avregistrerad datum", "avregistreringsdatum", "avford datum", "upphord datum",
                    "upphor", "upphorde", "utgar", "utgick", "avvecklad", "avvecklingsdatum", "giltig tom",
                    "giltig t o m", "giltig till", "galler till", "till och med", "tom datum", "slutdatum",
                    "retirement date", "end date", "makulerad datum", "giltig till och med")),
    LifecycleField("replacedBy", "Ersätts av", "code", "tillägget codesystem-replacedby",
                   "flaggas om ersättaren inte finns i urvalet",
                   ("ersatts av", "ersatt av", "ersattare", "ersattande kod", "ny kod", "replaced by",
                    "se i stallet", "anvand i stallet", "hanvisas till", "ersattningskod")),
    LifecycleField("changed", "Senast ändrad", "date", "förvaltningsuppgift (exporteras inte)",
                   "förvaltningsuppgift (exporteras inte)",
                   ("andrad", "andrad datum", "andringsdatum", "senast andrad", "uppdaterad", "senast uppdaterad",
                    "revisionsdatum", "last modified", "modified", "datum andrad"), exported=False),
    LifecycleField("changeNote", "Ändringsnotering", "text", "förvaltningsuppgift (exporteras inte)",
                   "förvaltningsuppgift (exporteras inte)",
                   ("andring", "andringar", "andringsorsak", "andringskommentar", "andringsbeskrivning",
                    "andringstyp", "andringslogg", "orsak till andring", "change note", "change reason"),
                   exported=False),
    LifecycleField("versionAdded", "Tillagd i version", "version", "förvaltningsuppgift (exporteras inte)",
                   "förvaltningsuppgift (exporteras inte)",
                   ("tillagd i version", "inford i version", "fran version", "version tillagd", "ny i version",
                    "version inford", "since version"), exported=False),
    LifecycleField("versionRemoved", "Borttagen i version", "version",
                   "ger status retired (versionen i sig exporteras inte)", "raden ingår inte",
                   ("borttagen i version", "till version", "version borttagen", "utgar i version",
                    "utgick i version", "avregistrerad i version"), exported=False),
]}

# Egenskapskoder som tidigare (eller på andra servrar) använts för samma sak -> livscykelmål.
PROPERTY_CODES = {"status": "status", "inactive": "inactive", "effectiveDate": "effectiveDate",
                  "deprecated": "deprecationDate", "deprecationDate": "deprecationDate",
                  "retirementDate": "retirementDate", "replacedBy": "replacedBy"}

_STATUS_WORDS = {
    "active": {"active", "aktiv", "gallande", "giltig", "i bruk", "godkand", "faststalld", "publicerad", "current",
               "ja", "j", "yes", "a"},
    "experimental": {"experimental", "experimentell", "test", "prov", "preliminar", "utkast", "draft", "forslag",
                     "under utveckling", "pilot"},
    "deprecated": {"deprecated", "utfasad", "avrads", "avradd", "ska ej anvandas", "bor ej anvandas", "utfases",
                   "pa vag ut"},
    "retired": {"retired", "inaktiv", "inactive", "avford", "avregistrerad", "borttagen", "makulerad", "upphord",
                "utgangen", "utgatt", "avvecklad", "ersatt", "ogiltig", "nej", "n", "no", "i"},
}
_YES = {"ja", "j", "yes", "y", "true", "sant", "1", "x", "✓", "✔", "*"}
_NO = {"nej", "n", "no", "false", "falskt", "0"}


# -- tolkning av värden ---------------------------------------------------------------------------


def normalize_status(value: Any) -> str | None:
    """'Gällande' -> 'active', 'Avregistrerad' -> 'retired'. None om okänt."""
    if value is None or str(value).strip() == "":
        return None
    v = normalize(str(value))
    return next((s for s, words in _STATUS_WORDS.items() if v in words), None)


def parse_flag(value: Any) -> bool | None:
    if value is True or value is False:
        return value
    if value is None or str(value).strip() == "":
        return None
    v = str(value).strip().lower()
    if v in _YES or normalize(v) in _YES:
        return True
    if v in _NO or normalize(v) in _NO:
        return False
    return None


YES_WITHOUT_DATE = "*"


def parse_date(value: Any) -> str | None:
    """Datum som ÅÅÅÅ-MM-DD (eller ÅÅÅÅ-MM / ÅÅÅÅ). Ett kryss ("x", "ja") ger ``YES_WITHOUT_DATE``.
    Kastar ValueError för värden som varken är datum eller kryss."""
    if value is None or str(value).strip() == "":
        return None
    if isinstance(value, datetime):
        return value.date().isoformat()
    if isinstance(value, date):
        return value.isoformat()
    s = str(value).strip()
    if m := re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})(?:[T ].*)?", s):
        return "-".join(m.groups())
    if m := re.fullmatch(r"(\d{4})(\d{2})(\d{2})", s):
        return "-".join(m.groups())
    if m := re.fullmatch(r"(\d{1,2})[./](\d{1,2})[./](\d{4})", s):
        return f"{m[3]}-{int(m[2]):02d}-{int(m[1]):02d}"
    if re.fullmatch(r"\d{4}(-\d{2})?", s):
        return s
    if parse_flag(s) is True:
        return YES_WITHOUT_DATE
    raise ValueError(f"'{value}' är inte ett datum (ÅÅÅÅ-MM-DD)")


def check_value(key: str, value: Any) -> str | None:
    """Felmeddelande om värdet inte går att tolka för livscykelmålet, annars None."""
    if value is None or str(value).strip() == "":
        return None
    kind = FIELDS[key].kind
    if kind == "status" and normalize_status(value) is None:
        return f"'{value}' är ingen känd status ({', '.join(STATUS_VALUES)} eller svenska motsvarigheter)"
    if kind == "flag" and parse_flag(value) is None and _date_or_none(value) is None:
        return f"'{value}' är inte ja/nej"
    if kind == "date":
        try:
            parse_date(value)
        except ValueError as exc:
            return str(exc)
    return None


def _date_or_none(value: Any) -> str | None:
    try:
        d = parse_date(value)
    except ValueError:
        return None
    return d if d and d != YES_WITHOUT_DATE else None


def values_fit(key: str, values: Iterable[Any], threshold: float = 0.8) -> bool:
    """Passar kolumnens värden målet? Används för att godta (eller avvisa) en rubrikträff."""
    vals = [v for v in values if v is not None and str(v).strip()]
    if not vals:
        return True
    kind = FIELDS[key].kind
    if kind in ("text", "code", "version"):
        return True
    ok = sum(check_value(key, v) is None for v in vals)
    return ok / len(vals) >= threshold


def refine_key(key: str, values: Iterable[Any]) -> str:
    """Byt till ett syskonmål när värdena visar vad som avses: 'Avregistrerad' med datum är ett
    borttagsdatum, 'Borttagen' med bara kryss är en inaktivmarkering."""
    vals = [v for v in values if v is not None and str(v).strip()]
    if not vals:
        return key
    dates = sum(_date_or_none(v) is not None for v in vals) / len(vals)
    flags = sum(parse_flag(v) is not None for v in vals) / len(vals)
    if key == "inactive" and dates >= 0.8:
        return "retirementDate"
    if key == "retirementDate" and flags >= 0.9 and dates == 0:
        return "inactive"
    if key == "status" and dates >= 0.8:
        return "retirementDate"
    return key


# -- tillstånd per rad ----------------------------------------------------------------------------


@dataclass
class State:
    status: str = "active"
    explicit_status: bool = False
    inactive: bool | None = None          # uttryckligt värde från en inaktiv-/aktivkolumn
    effective: str | None = None
    deprecation: str | None = None
    retirement: str | None = None
    replaced_by: str | None = None
    problems: list[str] = field(default_factory=list)

    @property
    def retired(self) -> bool:
        return self.status == "retired"


def lifecycle_columns(columns) -> dict[str, str]:
    """{livscykelnyckel: källkolumn} för en malls kolumner."""
    from .mapping import ColumnRole
    return {c.lifecycle: c.source_column for c in columns if c.role == ColumnRole.LIFECYCLE and c.lifecycle}


def row_state(cols: dict[str, str], row: dict, on: date | None = None) -> State:
    """Härled ett konsekvent livscykeltillstånd för en rad."""
    today = (on or date.today()).isoformat()
    st = State()

    def get(key):
        col = cols.get(key)
        v = row.get(col) if col else None
        return None if v is None or str(v).strip() == "" else v

    def as_date(key):
        v = get(key)
        if v is None:
            return None
        try:
            return parse_date(v)
        except ValueError as exc:
            st.problems.append(f"{FIELDS[key].label}: {exc}")
            return None

    st.effective, st.deprecation, st.retirement = as_date("effectiveDate"), as_date("deprecationDate"), \
        as_date("retirementDate")
    for key in ("inactive", "active"):
        v = get(key)
        if v is None:
            continue
        flag = parse_flag(v)
        if flag is None and _date_or_none(v):  # "Inaktiv: 2024-05-01"
            flag, st.retirement = True, st.retirement or _date_or_none(v)
        if flag is None:
            st.problems.append(f"{FIELDS[key].label}: '{v}' är inte ja/nej")
            continue
        st.inactive = flag if key == "inactive" else not flag
    replaced = get("replacedBy")
    st.replaced_by = str(replaced).strip() if replaced is not None else None

    raw_status = get("status")
    explicit = normalize_status(raw_status)
    if raw_status is not None and explicit is None:
        st.problems.append(f"Status: '{raw_status}' känns inte igen")

    retired_on = st.retirement if st.retirement and (st.retirement == YES_WITHOUT_DATE or st.retirement <= today) \
        else None
    deprecated_on = st.deprecation if st.deprecation and (st.deprecation == YES_WITHOUT_DATE
                                                         or st.deprecation <= today) else None
    if explicit:
        st.status, st.explicit_status = explicit, True
    elif st.inactive or retired_on or get("versionRemoved") is not None:
        st.status = "retired"
    elif deprecated_on:
        st.status = "deprecated"

    if explicit and explicit != "retired" and (st.inactive or retired_on):
        st.problems.append(f"Status '{raw_status}' motsäger att begreppet är avvecklat"
                           + (f" ({st.retirement})" if retired_on and retired_on != YES_WITHOUT_DATE else ""))
    if explicit == "retired" and st.inactive is False:
        st.problems.append(f"Status '{raw_status}' motsäger att begreppet är markerat som aktivt")
    if st.effective and st.retirement and YES_WITHOUT_DATE not in (st.effective, st.retirement) \
            and st.retirement < st.effective:
        st.problems.append(f"Borttagen ({st.retirement}) före tillagd ({st.effective})")
    if st.replaced_by and not st.retired and st.status != "deprecated":
        st.problems.append(f"Har en ersättare ({st.replaced_by}) men är inte avvecklad eller utfasad")
    return st


# -- export ---------------------------------------------------------------------------------------


def _deprecation_code(fhir_version: str) -> str:
    return "deprecated" if fhir_version.upper().startswith("R4") else "deprecationDate"


def concept_elements(cols: dict[str, str], row: dict, cs_url: str, fhir_version: str = "R4",
                     on: date | None = None) -> tuple[list[dict], list[dict], list[str]]:
    """(concept.property, concept.extension, varningar) för en rad i ett eget kodverk."""
    st = row_state(cols, row, on)
    props: list[dict] = []
    if st.explicit_status or st.status != "active":
        props.append({"code": "status", "valueCode": st.status})
    if st.retired:
        props.append({"code": "inactive", "valueBoolean": True})
    elif st.inactive is False:
        props.append({"code": "inactive", "valueBoolean": False})
    for code, value in (("effectiveDate", st.effective), (_deprecation_code(fhir_version), st.deprecation),
                        ("retirementDate", st.retirement)):
        if value and value != YES_WITHOUT_DATE:
            props.append({"code": code, "valueDateTime": value})
    exts = [{"url": REPLACED_BY_EXT, "valueCoding": {"system": cs_url, "code": st.replaced_by}}] \
        if st.replaced_by else []
    return props, exts, st.problems


_DECLARATIONS = {
    "status": ("code", "Livscykelstatus: active | experimental | deprecated | retired"),
    "inactive": ("boolean", "Begreppet är inaktivt (avvecklat)"),
    "effectiveDate": ("dateTime", "Datum då begreppet började gälla"),
    "deprecated": ("dateTime", "Datum då begreppet fasades ut (avråds)"),
    "deprecationDate": ("dateTime", "Datum då begreppet fasades ut (avråds)"),
    "retirementDate": ("dateTime", "Datum då begreppet avvecklades"),
}


def declarations(used: Iterable[str]) -> list[dict]:
    """CodeSystem.property för de livscykelegenskaper som används, med standard-URI."""
    used = set(used)
    return [{"code": code, "uri": f"{CONCEPT_PROPERTIES}#{code}", "description": desc, "type": t}
            for code, (t, desc) in _DECLARATIONS.items() if code in used]


def membership(cols: dict[str, str], row: dict, on: date | None = None) -> tuple[bool, str | None, State]:
    """Ingår raden i urvalet vid datumet? (ingår, anledning om inte / anmärkning, tillstånd)."""
    st = row_state(cols, row, on)
    today = (on or date.today()).isoformat()
    if st.retired:
        when = st.retirement if st.retirement and st.retirement != YES_WITHOUT_DATE else None
        return False, "borttagen" + (f" {when}" if when else ""), st
    if st.effective and st.effective != YES_WITHOUT_DATE and st.effective > today:
        return False, f"ingår först från {st.effective}", st
    if st.status == "deprecated":
        return True, "avråds", st
    return True, None, st


# -- skydd mot homonyma egenskaper ----------------------------------------------------------------


def lifecycle_target_for(header: str, registry=None) -> LifecycleField | None:
    """Livscykelmålet som en rubrik (eller egenskapskod) troligen avser, annars None."""
    if header in PROPERTY_CODES:
        return FIELDS[PROPERTY_CODES[header]]
    if registry is not None:
        hit = next((c for c in registry.match(header, limit=5) if c.entry.kind == "lifecycle" and c.score >= 0.85),
                   None)
        if hit:
            return FIELDS[hit.entry.key.split(":", 1)[1]]
        return None
    n = normalize(header)
    return next((f for f in FIELDS.values() if n in f.aliases or n == normalize(f.key)), None)


def homonym_message(header: str, target: LifecycleField) -> str:
    return (f"'{header}' ser ut att vara förvaltningsinformation ({target.label}). Välj den under "
            "\"Livscykel och förvaltning\" i stället för en egen egenskap – då uttrycks den likadant i alla "
            "kodverk och urval.")
