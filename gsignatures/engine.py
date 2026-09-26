"""
Pure engine for centrally managed Gmail signatures.

This module makes no Google API calls, imports nothing from FastMCP and never
touches the network. It turns configuration, Directory data and templates into
rendered signature HTML and decisions, and the tool layer (a later task) does
the reading and writing. Everything here is unit-testable with plain dicts.

Google API facts this module relies on (shapes only, no calls are made here):

* An Admin SDK ``users.get`` resource with ``projection=full`` carries
  ``primaryEmail``, ``name{givenName, familyName, fullName}``,
  ``organizations[{title, primary}]``, ``phones[{type, value, primary}]``,
  ``orgUnitPath``, ``suspended``, ``archived`` and ``aliases``. Directory
  phone ``type`` values include ``mobile`` and ``work_mobile``; both count as
  a mobile number here.
* A Gmail ``users.settings.sendAs`` resource carries ``sendAsEmail``,
  ``isPrimary``, ``isDefault``, ``displayName``, ``signature``,
  ``treatAsAlias`` and ``verificationStatus``. Every send-as address has its
  own signature, so a user with four aliases has five signatures to manage.
* Gmail sanitises the signature HTML it stores (attributes and tags it does
  not accept are dropped) and returns the sanitised form on read-back. The
  raw template output therefore never equals what Gmail holds, which is why
  drift is judged against the hash Gmail returned right after the last apply
  (kept in the ledger), never against a fresh render.

Design rules carried by this module:

* Configuration is validated in full before anything is used. Every problem
  found is reported in one ``SignatureConfigError`` so a broken file is fixed
  in one pass, not one error at a time.
* Entity resolution is an ordered rule list, first match wins. Suspension,
  archiving and OU exclusion are checked before any rule.
* The template renderer is strict. Unknown placeholders and empty values
  outside an optional block raise ``TemplateError``. Nothing is ever rendered
  blank in silence.
* The optional ``mobile`` field is rendered only when the Directory has one.
  Its absence is never an error and never leaves an empty line, provided the
  template wraps it in ``{{#mobile}} ... {{/mobile}}``.
"""

from __future__ import annotations

import dataclasses
import hashlib
import html as html_lib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

import yaml

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

DEFAULT_CONFIG_PATH = Path(__file__).parent / "config" / "entities.yaml"
DEFAULT_TEMPLATES_DIR = Path(__file__).parent / "templates"

# Names a signature template may use. ``email`` is the send-as address the
# signature belongs to; ``statutory`` is the rendered statutory partial and is
# the one value inserted without HTML escaping.
SIGNATURE_PLACEHOLDERS = {"name", "title", "mobile", "email", "statutory"}

# Names a statutory partial may use. ``email`` here is the entity's contact
# address, not the person's.
STATUTORY_PLACEHOLDERS = {
    "legal_name",
    "trading_name",
    "company_number",
    "place_of_registration",
    "registered_office",
    "vat_number",
    "phone",
    "website",
    "email",
}

LEGAL_FORMS = ("limited_company", "sole_trader", "unknown")

# Fields a limited company must carry for a compliant email footer (Companies
# Act 2006 s.82 and the Company, Limited Liability Partnership and Business
# (Names and Trading Disclosures) Regulations 2015).
_LIMITED_COMPANY_REQUIRED = (
    "legal_name",
    "company_number",
    "place_of_registration",
    "registered_office",
)

# Person fields the Directory can supply. ``required_fields`` and
# ``optional_fields`` in config must draw from this set.
PERSON_FIELDS = ("name", "title", "mobile")

_DEFAULT_REQUIRED_FIELDS = ["name", "title"]
_DEFAULT_OPTIONAL_FIELDS = ["mobile"]

_STATUTORY_STRING_FIELDS = (
    "legal_name",
    "trading_name",
    "company_number",
    "place_of_registration",
    "registered_office",
    "vat_number",
    "phone",
    "website",
    "email",
)

_SEMVER_RE = re.compile(r"^\d+\.\d+\.\d+$")
_LEGAL_SUFFIX_RE = re.compile(r"\b(limited|ltd)\.?$", re.IGNORECASE)
_PLACEHOLDER_WORD_RE = re.compile(r"\b(TODO|MISSING|TBC|PLACEHOLDER)\b", re.IGNORECASE)

# Directory phone types that count as a mobile number.
_MOBILE_PHONE_TYPES = ("mobile", "work_mobile")

# Template syntax. Blocks are ``{{#name}} ... {{/name}}``; placeholders are
# ``{{name}}``. Names are lower-case identifiers.
_BLOCK_RE = re.compile(r"\{\{#\s*([A-Za-z_]+)\s*\}\}(.*?)\{\{/\s*\1\s*\}\}", re.DOTALL)
_BLOCK_MARKER_RE = re.compile(r"\{\{[#/]\s*([A-Za-z_]+)\s*\}\}")
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([^{}]*?)\s*\}\}")

_WHITESPACE_RE = re.compile(r"\s+")

RESULT_ACTIONS = ("applied", "would_apply", "unchanged", "skipped", "error")

# Gmail enforces a 10,000-character limit on a signature. The engine refuses
# a render over that size rather than sending something Gmail will reject or
# cut; the refusal is a TemplateError, which plan_for_user turns into an
# error row for that address.
MAX_SIGNATURE_CHARS = 10000


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class SignatureConfigError(ValueError):
    """The configuration is invalid. The message lists every problem found."""


class MissingDirectoryDataError(ValueError):
    """A required Directory field is empty. ``missing`` names every such field."""

    def __init__(self, missing: Iterable[str], email: Optional[str] = None):
        self.missing = list(missing)
        who = f" for {email}" if email else ""
        super().__init__(
            f"Directory data missing{who}: {', '.join(self.missing)}. "
            "Fill the field in the Admin console (or the Directory sync) and re-run."
        )


class TemplateError(ValueError):
    """A template is missing, malformed, or uses a name it may not."""


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class Entity:
    """One legal entity: the values its statutory footer needs, plus policy."""

    code: str
    legal_name: str
    trading_name: str
    legal_form: str
    company_number: Optional[str]
    place_of_registration: Optional[str]
    registered_office: Optional[str]
    vat_number: Optional[str]
    phone: Optional[str]
    website: Optional[str]
    email: Optional[str]
    domains: List[str]
    group_branding: bool
    template_version: str
    statutory_version: str
    statutory_verified: bool
    required_fields: List[str]
    optional_fields: List[str]


@dataclass
class PrimaryRule:
    """One entry of the ordered primary-address rule list. First match wins."""

    entity: str
    ou_prefix: Optional[str] = None
    primary_domain: Optional[str] = None


@dataclass
class SignatureConfig:
    version: int
    entities: Dict[str, Entity]
    primary_rules: List[PrimaryRule]
    exclude_ou_prefixes: List[str]
    skip_alias_domains: List[str]
    templates_dir: Path
    domain_to_entity: Dict[str, str]


@dataclass
class Person:
    """The Directory fields a signature needs, already cleaned."""

    email: str
    name: str
    given_name: str
    family_name: str
    title: str
    mobile: Optional[str]


@dataclass
class LoadedTemplate:
    entity: str
    signature_html: str
    statutory_html: str
    template_version: str
    statutory_version: str
    signature_path: Path
    statutory_path: Path


@dataclass
class PlannedSignature:
    """What the engine would write for one send-as address, or why not."""

    user_email: str
    send_as_email: str
    is_primary: bool
    entity: Optional[str]
    template_version: Optional[str]
    statutory_version: Optional[str]
    html: Optional[str]
    rendered_hash: Optional[str]
    status: str  # 'planned' | 'skipped' | 'error'
    reason: str


@dataclass
class ResultRow:
    """One line of the apply / dry-run report."""

    user_email: str
    send_as_email: str
    entity: Optional[str]
    template_version: Optional[str]
    action: str  # one of RESULT_ACTIONS
    reason: str
    before_hash: Optional[str]
    after_hash: Optional[str]


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------


def _clean(value: Any) -> Optional[str]:
    """Return the stripped string, or None when empty or whitespace-only."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _is_set(value: Any) -> bool:
    return _clean(value) is not None


def _domain_of(email: Optional[str]) -> str:
    text = (email or "").strip().lower()
    return text.rsplit("@", 1)[-1] if "@" in text else ""


def _ou_under(ou_path: str, prefix: str) -> bool:
    """True when ``ou_path`` is ``prefix`` or sits beneath it.

    The boundary is a path separator, so ``/OTB`` does not match ``/OTBX``.
    """
    if ou_path == prefix:
        return True
    return ou_path.startswith(prefix.rstrip("/") + "/")


def _is_semver(value: Any) -> bool:
    return isinstance(value, str) and bool(_SEMVER_RE.match(value))


def template_paths(
    templates_dir: Path, code: str, template_version: str, statutory_version: str
) -> Tuple[Path, Path]:
    """The two files an entity's pinned versions resolve to."""
    base = Path(templates_dir) / code
    return (
        base / f"{code}-signature-v{template_version}.html",
        base / f"{code}-statutory-v{statutory_version}.html",
    )


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def _parse_field_list(
    raw: Any, name: str, default: List[str], problems: List[str], where: str
) -> List[str]:
    if raw is None:
        return list(default)
    if not isinstance(raw, list) or not all(isinstance(x, str) for x in raw):
        problems.append(f"{where}: {name} must be a list of field names")
        return list(default)
    unknown = [x for x in raw if x not in PERSON_FIELDS]
    if unknown:
        problems.append(
            f"{where}: {name} names unknown fields {unknown}; "
            f"known fields are {list(PERSON_FIELDS)}"
        )
    return list(raw)


def _parse_entity(
    code: str,
    raw: Any,
    defaults: Dict[str, Any],
    templates_dir: Path,
    problems: List[str],
) -> Optional[Entity]:
    where = f"entity {code}"
    if not isinstance(raw, dict):
        problems.append(f"{where}: must be a mapping")
        return None

    legal_form = raw.get("legal_form", "limited_company")
    if legal_form not in LEGAL_FORMS:
        problems.append(
            f"{where}: legal_form must be one of {list(LEGAL_FORMS)}; got {legal_form!r}"
        )

    values: Dict[str, Optional[str]] = {}
    for name in _STATUTORY_STRING_FIELDS:
        value = raw.get(name)
        if value is None and name == "place_of_registration":
            value = defaults.get("place_of_registration")
        if value is not None and not isinstance(value, (str, int)):
            problems.append(f"{where}: {name} must be a string or null")
            value = None
        cleaned = _clean(value)
        if cleaned is not None and _PLACEHOLDER_WORD_RE.search(cleaned):
            problems.append(
                f"{where}: {name} still contains a placeholder word "
                f"(TODO / MISSING / TBC / PLACEHOLDER): {cleaned!r}"
            )
        values[name] = cleaned

    if values["legal_name"] is None:
        problems.append(f"{where}: legal_name is required")
    if values["trading_name"] is None:
        problems.append(f"{where}: trading_name is required")

    if legal_form == "limited_company":
        for name in _LIMITED_COMPANY_REQUIRED:
            if values[name] is None:
                problems.append(f"{where}: {name} is required for a limited_company")
        legal_name = values["legal_name"]
        if legal_name is not None and not _LEGAL_SUFFIX_RE.search(legal_name):
            problems.append(
                f"{where}: legal_name {legal_name!r} must end with 'Limited' or 'Ltd' "
                "for a limited_company"
            )

    raw_domains = raw.get("domains")
    domains: List[str] = []
    if raw_domains is None:
        raw_domains = []
    if not isinstance(raw_domains, list) or not all(
        isinstance(d, str) and d.strip() for d in raw_domains
    ):
        problems.append(f"{where}: domains must be a list of domain names")
    else:
        domains = [d.strip().lower() for d in raw_domains]

    group_branding = raw.get("group_branding", True)
    if not isinstance(group_branding, bool):
        problems.append(f"{where}: group_branding must be true or false")
        group_branding = False

    statutory_verified = raw.get("statutory_verified", False)
    if not isinstance(statutory_verified, bool):
        problems.append(f"{where}: statutory_verified must be true or false")
        statutory_verified = False

    template_version = raw.get("template_version")
    statutory_version = raw.get("statutory_version")
    versions_ok = True
    for name, value in (
        ("template_version", template_version),
        ("statutory_version", statutory_version),
    ):
        if not _is_semver(value):
            problems.append(
                f"{where}: {name} must be a semver string 'X.Y.Z'; got {value!r}"
            )
            versions_ok = False
    if versions_ok:
        for path in template_paths(
            templates_dir, code, template_version, statutory_version
        ):
            if not path.is_file():
                problems.append(f"{where}: template file not found: {path}")

    required_fields = _parse_field_list(
        raw.get("required_fields"),
        "required_fields",
        list(defaults.get("required_fields") or _DEFAULT_REQUIRED_FIELDS),
        problems,
        where,
    )
    optional_fields = _parse_field_list(
        raw.get("optional_fields"),
        "optional_fields",
        list(defaults.get("optional_fields") or _DEFAULT_OPTIONAL_FIELDS),
        problems,
        where,
    )
    both = [f for f in required_fields if f in optional_fields]
    if both:
        problems.append(
            f"{where}: {both} cannot be in both required_fields and optional_fields"
        )

    return Entity(
        code=code,
        legal_name=values["legal_name"] or "",
        trading_name=values["trading_name"] or "",
        legal_form=str(legal_form),
        company_number=values["company_number"],
        place_of_registration=values["place_of_registration"],
        registered_office=values["registered_office"],
        vat_number=values["vat_number"],
        phone=values["phone"],
        website=values["website"],
        email=values["email"],
        domains=domains,
        group_branding=group_branding,
        template_version=str(template_version),
        statutory_version=str(statutory_version),
        statutory_verified=statutory_verified,
        required_fields=required_fields,
        optional_fields=optional_fields,
    )


def _parse_rules(
    raw_rules: Any, entity_codes: Iterable[str], problems: List[str]
) -> Tuple[List[PrimaryRule], List[str], List[str]]:
    codes = set(entity_codes)
    if raw_rules is None:
        raw_rules = {}
    if not isinstance(raw_rules, dict):
        problems.append("rules: must be a mapping")
        raw_rules = {}

    exclude = raw_rules.get("exclude_ou_prefixes") or []
    if not isinstance(exclude, list) or not all(isinstance(x, str) for x in exclude):
        problems.append("rules.exclude_ou_prefixes: must be a list of OU paths")
        exclude = []
    for prefix in exclude:
        if not prefix.startswith("/"):
            problems.append(
                f"rules.exclude_ou_prefixes: {prefix!r} must start with '/'"
            )

    skip = raw_rules.get("skip_alias_domains") or []
    if not isinstance(skip, list) or not all(
        isinstance(x, str) and x.strip() for x in skip
    ):
        problems.append("rules.skip_alias_domains: must be a list of domain names")
        skip = []
    skip = [d.strip().lower() for d in skip]

    primary_raw = raw_rules.get("primary")
    rules: List[PrimaryRule] = []
    if primary_raw is None:
        primary_raw = []
    if not isinstance(primary_raw, list):
        problems.append("rules.primary: must be a list")
        primary_raw = []
    for index, item in enumerate(primary_raw):
        where = f"rules.primary[{index}]"
        if not isinstance(item, dict):
            problems.append(f"{where}: must be a mapping with 'match' and 'entity'")
            continue
        entity = item.get("entity")
        if entity not in codes:
            problems.append(f"{where}: references unknown entity {entity!r}")
        match = item.get("match")
        if not isinstance(match, dict):
            problems.append(f"{where}: 'match' must be a mapping")
            match = {}
        ou_prefix = _clean(match.get("ou_prefix"))
        primary_domain = _clean(match.get("primary_domain"))
        set_count = int(ou_prefix is not None) + int(primary_domain is not None)
        if set_count != 1:
            problems.append(
                f"{where}: exactly one of ou_prefix / primary_domain must be set; "
                f"got {set_count}"
            )
        if ou_prefix is not None and not ou_prefix.startswith("/"):
            problems.append(f"{where}: ou_prefix {ou_prefix!r} must start with '/'")
        unknown_keys = set(match) - {"ou_prefix", "primary_domain"}
        if unknown_keys:
            problems.append(f"{where}: unknown matcher keys {sorted(unknown_keys)}")
        rules.append(
            PrimaryRule(
                entity=str(entity),
                ou_prefix=ou_prefix,
                primary_domain=primary_domain.lower() if primary_domain else None,
            )
        )
    return rules, list(exclude), skip


def load_config(
    path: Optional[Path] = None, templates_dir: Optional[Path] = None
) -> SignatureConfig:
    """Load and fully validate ``entities.yaml``.

    Every problem is collected and raised in one ``SignatureConfigError`` so
    the file can be fixed in a single pass. Nothing is returned from a file
    that fails any check.
    """
    config_path = Path(path) if path is not None else DEFAULT_CONFIG_PATH
    tdir = Path(templates_dir) if templates_dir is not None else DEFAULT_TEMPLATES_DIR

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SignatureConfigError(f"config file not found: {config_path}") from None
    except yaml.YAMLError as exc:
        raise SignatureConfigError(
            f"config is not valid YAML ({config_path}): {exc}"
        ) from exc

    if not isinstance(raw, dict):
        raise SignatureConfigError(
            f"config must be a mapping at top level ({config_path})"
        )

    problems: List[str] = []

    version = raw.get("version")
    if version != 1:
        problems.append(f"version must be 1; got {version!r}")

    defaults = raw.get("defaults") or {}
    if not isinstance(defaults, dict):
        problems.append("defaults: must be a mapping")
        defaults = {}

    raw_entities = raw.get("entities")
    entities: Dict[str, Entity] = {}
    if not isinstance(raw_entities, dict) or not raw_entities:
        problems.append(
            "entities: must be a non-empty mapping of entity code to values"
        )
        raw_entities = {}
    for code, raw_entity in raw_entities.items():
        code_text = str(code)
        if not re.fullmatch(r"[A-Z][A-Z0-9_]*", code_text):
            problems.append(
                f"entity {code_text!r}: code must be upper-case letters, digits or '_'"
            )
        entity = _parse_entity(code_text, raw_entity, defaults, tdir, problems)
        if entity is not None:
            entities[code_text] = entity

    rules, exclude, skip = _parse_rules(raw.get("rules"), entities.keys(), problems)

    domain_to_entity: Dict[str, str] = {}
    for code, entity in entities.items():
        for domain in entity.domains:
            owner = domain_to_entity.get(domain)
            if owner is not None and owner != code:
                problems.append(
                    f"domain {domain!r} belongs to both {owner} and {code}; "
                    "a domain may belong to at most one entity"
                )
                continue
            domain_to_entity[domain] = code
    for domain in skip:
        if domain in domain_to_entity:
            problems.append(
                f"domain {domain!r} is in skip_alias_domains and also belongs to "
                f"entity {domain_to_entity[domain]}"
            )

    if problems:
        raise SignatureConfigError(
            f"{config_path} has {len(problems)} problem(s):\n  - "
            + "\n  - ".join(problems)
        )

    return SignatureConfig(
        version=1,
        entities=entities,
        primary_rules=rules,
        exclude_ou_prefixes=exclude,
        skip_alias_domains=skip,
        templates_dir=tdir,
        domain_to_entity=domain_to_entity,
    )


# ---------------------------------------------------------------------------
# Directory data
# ---------------------------------------------------------------------------


def _pick_title(organizations: Any) -> Optional[str]:
    if not isinstance(organizations, list):
        return None
    for org in organizations:
        if isinstance(org, dict) and org.get("primary") and _is_set(org.get("title")):
            return _clean(org.get("title"))
    for org in organizations:
        if isinstance(org, dict) and _is_set(org.get("title")):
            return _clean(org.get("title"))
    return None


def _pick_mobile(phones: Any) -> Optional[str]:
    if not isinstance(phones, list):
        return None
    candidates = [
        p
        for p in phones
        if isinstance(p, dict)
        and str(p.get("type") or "").lower() in _MOBILE_PHONE_TYPES
        and _is_set(p.get("value"))
    ]
    # Primary first, then Directory order.
    candidates.sort(key=lambda p: 0 if p.get("primary") else 1)
    return _clean(candidates[0]["value"]) if candidates else None


def extract_person(user: Dict[str, Any], entity: Entity) -> Person:
    """Pull the signature fields out of a ``users.get`` resource.

    Raises ``MissingDirectoryDataError`` naming every field in
    ``entity.required_fields`` that is empty or whitespace-only. Optional
    fields come back as ``None`` when absent.
    """
    email = _clean(user.get("primaryEmail"))
    name_block = user.get("name") or {}
    given = _clean(name_block.get("givenName")) or ""
    family = _clean(name_block.get("familyName")) or ""
    full = _clean(name_block.get("fullName"))
    name = full or (" ".join(p for p in (given, family) if p) or None)

    found: Dict[str, Optional[str]] = {
        "name": name,
        "title": _pick_title(user.get("organizations")),
        "mobile": _pick_mobile(user.get("phones")),
    }

    missing = [f for f in entity.required_fields if not found.get(f)]
    if email is None:
        missing.insert(0, "primaryEmail")
    if missing:
        raise MissingDirectoryDataError(missing, email)

    return Person(
        email=email or "",
        name=found["name"] or "",
        given_name=given,
        family_name=family,
        title=found["title"] or "",
        mobile=found["mobile"],
    )


# ---------------------------------------------------------------------------
# Entity resolution
# ---------------------------------------------------------------------------


def resolve_primary_entity(
    config: SignatureConfig, user: Dict[str, Any]
) -> Tuple[Optional[str], str]:
    """Decide which entity owns a user's primary address.

    Order: suspended, archived, excluded OU, then the ordered rule list.
    Returns ``(entity_code, reason)``; the code is ``None`` when unmanaged.
    """
    if user.get("suspended"):
        return None, "suspended"
    if user.get("archived"):
        return None, "archived"

    ou_path = str(user.get("orgUnitPath") or "/")
    domain = _domain_of(user.get("primaryEmail"))

    for prefix in config.exclude_ou_prefixes:
        if _ou_under(ou_path, prefix):
            return None, f"excluded OU {ou_path}"

    for rule in config.primary_rules:
        if rule.primary_domain is not None and domain == rule.primary_domain:
            return rule.entity, f"rule: primary_domain {rule.primary_domain}"
        if rule.ou_prefix is not None and _ou_under(ou_path, rule.ou_prefix):
            return rule.entity, f"rule: ou_prefix {rule.ou_prefix}"

    return None, f"no matching rule for OU {ou_path} and domain {domain or '(none)'}"


def resolve_alias_entity(
    config: SignatureConfig,
    send_as_email: str,
    primary_entity: Optional[str],
    primary_email: str,
) -> Tuple[Optional[str], str]:
    """Decide which entity a send-as alias belongs to. The alias domain decides."""
    if primary_entity is None:
        return None, "primary address unmanaged"
    domain = _domain_of(send_as_email)
    if domain and domain == _domain_of(primary_email):
        return primary_entity, "same domain as primary"
    if domain in config.skip_alias_domains:
        return None, f"skipped: personal domain {domain}"
    code = config.domain_to_entity.get(domain)
    if code is not None:
        return code, f"alias domain {domain}"
    return None, f"unmapped domain {domain or '(none)'}"


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


def load_template(config: SignatureConfig, entity_code: str) -> LoadedTemplate:
    """Read the two files pinned by the entity's versions.

    Leading and trailing whitespace is stripped from both files so a trailing
    newline in the editor never becomes part of the signature.
    """
    entity = config.entities.get(entity_code)
    if entity is None:
        raise TemplateError(f"unknown entity {entity_code!r}")
    signature_path, statutory_path = template_paths(
        config.templates_dir,
        entity_code,
        entity.template_version,
        entity.statutory_version,
    )
    for path in (signature_path, statutory_path):
        if not path.is_file():
            raise TemplateError(f"template file not found: {path}")
    return LoadedTemplate(
        entity=entity_code,
        signature_html=signature_path.read_text(encoding="utf-8").strip(),
        statutory_html=statutory_path.read_text(encoding="utf-8").strip(),
        template_version=entity.template_version,
        statutory_version=entity.statutory_version,
        signature_path=signature_path,
        statutory_path=statutory_path,
    )


def _render(
    text: str,
    values: Dict[str, Any],
    allowed: Iterable[str],
    raw_names: Iterable[str],
    label: str,
) -> str:
    """Strict renderer shared by signatures and statutory partials.

    1. Optional blocks ``{{#x}} ... {{/x}}`` are kept when ``x`` is set and
       removed entirely otherwise. Blocks may not nest.
    2. Placeholders ``{{x}}`` are substituted, HTML-escaped unless ``x`` is in
       ``raw_names``. An empty value outside a block is an error.
    """
    allowed_set = set(allowed)
    raw_set = set(raw_names)

    def block_sub(match: re.Match) -> str:
        name, body = match.group(1), match.group(2)
        if name not in allowed_set:
            raise TemplateError(
                f"{label}: unknown block '{name}'; allowed names are {sorted(allowed_set)}"
            )
        if _BLOCK_MARKER_RE.search(body):
            raise TemplateError(
                f"{label}: block '{name}' contains another block; blocks may not nest"
            )
        return body if _is_set(values.get(name)) else ""

    text = _BLOCK_RE.sub(block_sub, text)

    stray = _BLOCK_MARKER_RE.search(text)
    if stray:
        raise TemplateError(
            f"{label}: unbalanced block marker {stray.group(0)}; every {{{{#x}}}} needs "
            "a matching {{/x}} and blocks may not nest"
        )

    def placeholder_sub(match: re.Match) -> str:
        name = match.group(1)
        if name not in allowed_set:
            raise TemplateError(
                f"{label}: unknown placeholder '{name}'; allowed names are {sorted(allowed_set)}"
            )
        value = values.get(name)
        if not _is_set(value):
            raise TemplateError(
                f"{name} is empty; wrap it in {{{{#{name}}}}}...{{{{/{name}}}}}"
            )
        text_value = str(value).strip()
        if name in raw_set:
            return text_value
        return html_lib.escape(text_value, quote=True)

    return _PLACEHOLDER_RE.sub(placeholder_sub, text)


def render_statutory(template: LoadedTemplate, entity: Entity) -> str:
    """Render the entity's statutory partial. Every value is HTML-escaped."""
    values = {name: getattr(entity, name) for name in STATUTORY_PLACEHOLDERS}
    return _render(
        template.statutory_html,
        values,
        STATUTORY_PLACEHOLDERS,
        raw_names=(),
        label=f"{template.statutory_path.name}",
    )


def render_signature(
    template: LoadedTemplate,
    person: Person,
    entity: Entity,
    statutory_html: Optional[str] = None,
) -> str:
    """Render a person's signature for one entity.

    ``statutory_html`` is inserted as-is for ``{{statutory}}``. When it is
    ``None`` the entity's statutory partial is rendered first. Every other
    value is HTML-escaped.
    """
    if statutory_html is None:
        statutory_html = render_statutory(template, entity)
    values = {
        "name": person.name,
        "title": person.title,
        "mobile": person.mobile,
        "email": person.email,
        "statutory": statutory_html,
    }
    rendered = _render(
        template.signature_html,
        values,
        SIGNATURE_PLACEHOLDERS,
        raw_names=("statutory",),
        label=f"{template.signature_path.name}",
    )
    if len(rendered) > MAX_SIGNATURE_CHARS:
        raise TemplateError(
            f"{template.signature_path.name}: rendered signature is "
            f"{len(rendered)} characters, over Gmail's limit of "
            f"{MAX_SIGNATURE_CHARS}; shorten the template"
        )
    return rendered


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def normalise_html(html: Optional[str]) -> str:
    """Collapse whitespace runs to one space and strip. ``None`` becomes ''."""
    if not html:
        return ""
    return _WHITESPACE_RE.sub(" ", html).strip()


def signature_hash(html: Optional[str]) -> str:
    """sha256 hex of the normalised HTML; '' when there is nothing to hash."""
    normalised = normalise_html(html)
    if not normalised:
        return ""
    return hashlib.sha256(normalised.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


def plan_for_user(
    config: SignatureConfig,
    user: Dict[str, Any],
    send_as_list: List[Dict[str, Any]],
) -> List[PlannedSignature]:
    """Work out the signature for every send-as address of one user.

    One row per send-as entry, primary first. A skip reason gives status
    ``skipped``; a Directory or template fault gives ``error`` naming the
    fields or file. Each address is handled on its own, so one failing alias
    never touches the others.
    """
    user_email = str(user.get("primaryEmail") or "")
    primary_entity, primary_reason = resolve_primary_entity(config, user)

    ordered = sorted(send_as_list, key=lambda s: 0 if s.get("isPrimary") else 1)
    plans: List[PlannedSignature] = []

    for send_as in ordered:
        send_as_email = str(send_as.get("sendAsEmail") or "")
        is_primary = bool(send_as.get("isPrimary"))

        if is_primary:
            code, reason = primary_entity, primary_reason
        else:
            code, reason = resolve_alias_entity(
                config, send_as_email, primary_entity, user_email
            )

        row = PlannedSignature(
            user_email=user_email,
            send_as_email=send_as_email,
            is_primary=is_primary,
            entity=code,
            template_version=None,
            statutory_version=None,
            html=None,
            rendered_hash=None,
            status="skipped",
            reason=reason,
        )

        if code is None:
            plans.append(row)
            continue

        entity = config.entities[code]
        row.template_version = entity.template_version
        row.statutory_version = entity.statutory_version
        try:
            template = load_template(config, code)
            person = extract_person(user, entity)
            # The signature shows the address it is attached to, not always
            # the primary one.
            person = dataclasses.replace(person, email=send_as_email)
            statutory = render_statutory(template, entity)
            rendered = render_signature(template, person, entity, statutory)
        except MissingDirectoryDataError as exc:
            row.status = "error"
            row.reason = f"missing Directory fields: {', '.join(exc.missing)}"
        except TemplateError as exc:
            row.status = "error"
            row.reason = f"template error: {exc}"
        else:
            row.html = rendered
            row.rendered_hash = signature_hash(rendered)
            row.status = "planned"
        plans.append(row)

    return plans


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------


# ``readback_hash`` of the ledger row an apply writes BEFORE it patches
# Gmail. The completed row that follows the patch carries the real read-back
# hash. A pending row that is never followed by a completed row for the same
# run means the process died (or the patch failed) between the two writes;
# its ``previous_signature_html`` is still a valid rollback record.
PENDING_READBACK = "pending"

# Drift status for an address whose latest ledger row is still pending.
APPLY_INTERRUPTED = "apply_interrupted"


def is_pending_ledger_row(ledger_row: Optional[Dict[str, Any]]) -> bool:
    """Whether a ledger row is the pre-patch row of an apply that never
    recorded its completion."""
    if not ledger_row:
        return False
    return str(ledger_row.get("readback_hash") or "").strip() == PENDING_READBACK


def drift_status(
    planned: PlannedSignature,
    current_signature_html: Optional[str],
    ledger_row: Optional[Dict[str, Any]],
) -> Tuple[str, str]:
    """Compare what Gmail holds now with what the ledger says was applied.

    Checked in this order: unmanaged, error, never_applied,
    apply_interrupted, stale_template, stale_directory, changed_since_apply,
    in_sync. ``apply_interrupted`` means the latest ledger row is the
    pre-patch ``pending`` row and no completed row followed it: the apply
    was cut off (or its patch failed) after the rollback record was written,
    so nothing is known about what Gmail holds. ``stale_directory`` means
    the fresh render no longer hashes to the ledger's ``rendered_hash`` (the
    person's Directory data changed since the apply), which is still a
    judgement against the ledger, not against Gmail. The comparison for
    ``changed_since_apply`` is against the ledger's ``readback_hash`` (what
    Gmail returned right after the apply), never against a fresh render,
    because Gmail sanitises what it stores.
    """
    if planned.status == "skipped":
        return "unmanaged", planned.reason or "not managed"
    if planned.status == "error":
        return "error", planned.reason or "plan error"
    if not ledger_row:
        return "never_applied", "no ledger row for this address"
    if is_pending_ledger_row(ledger_row):
        return (
            APPLY_INTERRUPTED,
            f"ledger holds a pending row (run_id {ledger_row.get('run_id') or '?'}, "
            f"{ledger_row.get('applied_at') or '?'}) with no completed row: the "
            "apply was interrupted between the ledger write and the Gmail "
            "read-back; re-apply, or restore from that row",
        )

    ledger_tv = str(ledger_row.get("template_version") or "")
    ledger_sv = str(ledger_row.get("statutory_version") or "")
    if ledger_tv != (planned.template_version or "") or ledger_sv != (
        planned.statutory_version or ""
    ):
        return (
            "stale_template",
            f"ledger has template {ledger_tv or '?'} / statutory {ledger_sv or '?'}, "
            f"config pins {planned.template_version} / {planned.statutory_version}",
        )

    if str(ledger_row.get("rendered_hash") or "") != (planned.rendered_hash or ""):
        return (
            "stale_directory",
            "rendered output differs from the ledger rendered_hash (Directory "
            "data changed since apply)",
        )

    current_hash = signature_hash(current_signature_html)
    readback_hash = str(ledger_row.get("readback_hash") or "")
    if current_hash != readback_hash:
        return (
            "changed_since_apply",
            f"current hash {current_hash[:12] or '(empty)'} differs from ledger "
            f"readback {readback_hash[:12] or '(empty)'}",
        )
    return "in_sync", f"matches ledger readback {readback_hash[:12]}"


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def _short_hash(value: Optional[str]) -> str:
    return (value or "")[:12]


_TABLE_COLUMNS = (
    ("user_email", lambda r: r.user_email),
    ("send_as_email", lambda r: r.send_as_email),
    ("entity", lambda r: r.entity or ""),
    ("template", lambda r: r.template_version or ""),
    ("action", lambda r: r.action),
    ("before", lambda r: _short_hash(r.before_hash)),
    ("after", lambda r: _short_hash(r.after_hash)),
    ("reason", lambda r: r.reason or ""),
)


def format_result_table(rows: List[ResultRow]) -> str:
    """Aligned plain-text table with a header row and a closing summary line.

    Hashes are shortened to their first 12 characters for readability; the
    full values are in ``result_rows_as_dicts``.
    """
    cells = [[getter(row) for _, getter in _TABLE_COLUMNS] for row in rows]
    headers = [name for name, _ in _TABLE_COLUMNS]
    widths = [len(h) for h in headers]
    for line in cells:
        for i, value in enumerate(line):
            widths[i] = max(widths[i], len(value))

    def fmt(values: List[str]) -> str:
        return "  ".join(v.ljust(widths[i]) for i, v in enumerate(values))

    lines = [fmt(headers)]
    lines.extend(fmt(line) for line in cells)
    counts = {action: 0 for action in RESULT_ACTIONS}
    for row in rows:
        counts[row.action] = counts.get(row.action, 0) + 1
    lines.append(", ".join(f"{action} {counts[action]}" for action in RESULT_ACTIONS))
    return "\n".join(lines)


def result_rows_as_dicts(rows: List[ResultRow]) -> List[Dict[str, Any]]:
    return [dataclasses.asdict(row) for row in rows]


__all__ = [
    "DEFAULT_CONFIG_PATH",
    "DEFAULT_TEMPLATES_DIR",
    "SIGNATURE_PLACEHOLDERS",
    "STATUTORY_PLACEHOLDERS",
    "LEGAL_FORMS",
    "MAX_SIGNATURE_CHARS",
    "RESULT_ACTIONS",
    "SignatureConfigError",
    "MissingDirectoryDataError",
    "TemplateError",
    "Entity",
    "PrimaryRule",
    "SignatureConfig",
    "Person",
    "LoadedTemplate",
    "PlannedSignature",
    "ResultRow",
    "load_config",
    "extract_person",
    "resolve_primary_entity",
    "resolve_alias_entity",
    "load_template",
    "template_paths",
    "render_statutory",
    "render_signature",
    "normalise_html",
    "signature_hash",
    "plan_for_user",
    "drift_status",
    "format_result_table",
    "result_rows_as_dicts",
]
