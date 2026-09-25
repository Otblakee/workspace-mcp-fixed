"""Unit tests for the pure signature engine (``gsignatures.engine``).

Covers:

* the shipped ``entities.yaml`` loads and every pinned template resolves;
* every config validation rule fails loudly with a message naming the fault;
* primary entity resolution against real Directory shapes (OU rule beats the
  address domain, the AHWE domain rule beats its OU, exclusions, suspended
  and archived users);
* send-as alias resolution against Oli's real aliases;
* Directory field extraction, including the optional mobile;
* the strict template renderer (escaping, optional blocks, unknown names);
* ``plan_for_user`` isolation: one alias failing does not stop the others;
* ``drift_status`` precedence;
* the plain-text result table;
* the AHWE ring-fence: nothing under its templates or config names the group.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from gsignatures import engine  # noqa: E402
from gsignatures.engine import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    DEFAULT_TEMPLATES_DIR,
    SIGNATURE_PLACEHOLDERS,
    STATUTORY_PLACEHOLDERS,
    Entity,
    LoadedTemplate,
    MissingDirectoryDataError,
    Person,
    PlannedSignature,
    ResultRow,
    SignatureConfigError,
    TemplateError,
    drift_status,
    extract_person,
    format_result_table,
    load_config,
    load_template,
    normalise_html,
    plan_for_user,
    render_signature,
    render_statutory,
    resolve_alias_entity,
    resolve_primary_entity,
    result_rows_as_dicts,
    signature_hash,
)

GROUP_CODES = ("OTB", "JIT", "VALE", "BIR")
ALL_CODES = GROUP_CODES + ("AHWE",)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _user(
    email: str,
    ou: str,
    *,
    given: str = "Test",
    family: str = "Person",
    full: str | None = "Test Person",
    title: str | None = "Director",
    phones: list | None = None,
    suspended: bool = False,
    archived: bool = False,
    organizations: list | None = None,
) -> dict:
    """Build an Admin SDK ``users.get`` resource in the shape the engine reads."""
    name = {"givenName": given, "familyName": family}
    if full is not None:
        name["fullName"] = full
    if organizations is None:
        organizations = []
        if title is not None:
            organizations.append({"title": title, "primary": True})
    return {
        "primaryEmail": email,
        "name": name,
        "organizations": organizations,
        "phones": phones or [],
        "orgUnitPath": ou,
        "suspended": suspended,
        "archived": archived,
    }


def _send_as(email: str, *, primary: bool = False, signature: str = "") -> dict:
    return {
        "sendAsEmail": email,
        "isPrimary": primary,
        "isDefault": primary,
        "displayName": "",
        "signature": signature,
        "treatAsAlias": not primary,
        "verificationStatus": "accepted",
    }


OLI = _user(
    "oliver@otbgroup.co.uk",
    "/01 OTB/Exec",
    given="Oliver",
    family="Blake",
    full="Oliver Blake",
    title="Managing Director",
)
OLI_SEND_AS = [
    _send_as("oliver@otbgroup.co.uk", primary=True),
    _send_as("oliver.blake@jit-logistics.com"),
    _send_as("oliver@bir-d.co.uk"),
    _send_as("otb@otbgroup.co.uk"),
    _send_as("oliver@blakefamily.uk"),
]


def _minimal_entity(code: str = "OTB", **overrides) -> dict:
    entity = {
        "legal_name": "Example Holdings Limited",
        "trading_name": "Example",
        "company_number": "00000001",
        "registered_office": "1 Example Street, Exampletown EX1 1EX",
        "vat_number": None,
        "phone": None,
        "website": "example.test",
        "email": None,
        "domains": [f"{code.lower()}.example.test"],
        "group_branding": True,
        "template_version": "1.0.0",
        "statutory_version": "1.0.0",
        "statutory_verified": False,
    }
    entity.update(overrides)
    return entity


def _minimal_config(entities: dict | None = None, rules: dict | None = None) -> dict:
    if entities is None:
        entities = {"OTB": _minimal_entity("OTB")}
    base_rules = {
        "exclude_ou_prefixes": ["/99 _SYSTEM/BREAKGLASS"],
        "skip_alias_domains": ["personal.example.test"],
        "primary": [
            {"match": {"ou_prefix": f"/{code}"}, "entity": code} for code in entities
        ],
    }
    if rules:
        base_rules.update(rules)
    return {
        "version": 1,
        "defaults": {
            "place_of_registration": "England and Wales",
            "required_fields": ["name", "title"],
            "optional_fields": ["mobile"],
        },
        "rules": base_rules,
        "entities": entities,
    }


def _write_templates(
    templates_dir: Path, code: str, tv: str = "1.0.0", sv: str = "1.0.0"
) -> None:
    d = templates_dir / code
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{code}-signature-v{tv}.html").write_text(
        "<table><tr><td><b>{{name}}</b></td></tr><tr><td>{{title}}</td></tr>"
        "{{#mobile}}<tr><td>{{mobile}}</td></tr>{{/mobile}}"
        "<tr><td>{{email}}</td></tr><tr><td>{{statutory}}</td></tr></table>",
        encoding="utf-8",
    )
    (d / f"{code}-statutory-v{sv}.html").write_text(
        "{{legal_name}}, company number {{company_number}}.", encoding="utf-8"
    )


def _write_config(
    tmp_path: Path, config: dict, *, make_templates: bool = True
) -> tuple[Path, Path]:
    cfg_path = tmp_path / "entities.yaml"
    templates_dir = tmp_path / "templates"
    templates_dir.mkdir(exist_ok=True)
    if make_templates:
        for code, ent in config["entities"].items():
            _write_templates(
                templates_dir,
                code,
                ent.get("template_version", "1.0.0"),
                ent.get("statutory_version", "1.0.0"),
            )
    cfg_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return cfg_path, templates_dir


def _load_tmp(tmp_path: Path, config: dict, **kw):
    cfg_path, templates_dir = _write_config(tmp_path, config, **kw)
    return load_config(cfg_path, templates_dir)


@pytest.fixture
def shipped_config():
    return load_config()


@pytest.fixture
def shipped_copy(tmp_path):
    """A writable copy of the shipped config and templates."""
    cfg_copy = tmp_path / "entities.yaml"
    shutil.copy(DEFAULT_CONFIG_PATH, cfg_copy)
    templates_copy = tmp_path / "templates"
    shutil.copytree(DEFAULT_TEMPLATES_DIR, templates_copy)
    return cfg_copy, templates_copy


# ---------------------------------------------------------------------------
# Constants and dataclasses
# ---------------------------------------------------------------------------


def test_placeholder_sets_match_spec():
    assert SIGNATURE_PLACEHOLDERS == {"name", "title", "mobile", "email", "statutory"}
    assert STATUTORY_PLACEHOLDERS == {
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


def test_error_classes_are_value_errors():
    assert issubclass(SignatureConfigError, ValueError)
    assert issubclass(MissingDirectoryDataError, ValueError)
    assert issubclass(TemplateError, ValueError)
    err = MissingDirectoryDataError(["title"])
    assert err.missing == ["title"]


def test_entity_dataclass_shape():
    fields = set(Entity.__dataclass_fields__)
    assert fields == {
        "code",
        "legal_name",
        "trading_name",
        "legal_form",
        "company_number",
        "place_of_registration",
        "registered_office",
        "vat_number",
        "phone",
        "website",
        "email",
        "domains",
        "group_branding",
        "template_version",
        "statutory_version",
        "statutory_verified",
        "required_fields",
        "optional_fields",
    }
    assert engine.DEFAULT_CONFIG_PATH.name == "entities.yaml"


# ---------------------------------------------------------------------------
# load_config: the shipped file
# ---------------------------------------------------------------------------


def test_shipped_config_loads_and_templates_resolve(shipped_config):
    cfg = shipped_config
    assert cfg.version == 1
    assert set(cfg.entities) == set(ALL_CODES)
    for code in ALL_CODES:
        tpl = load_template(cfg, code)
        assert tpl.signature_path.is_file()
        assert tpl.statutory_path.is_file()
        assert tpl.template_version == "1.0.0"
        assert tpl.statutory_version == "1.0.0"


def test_shipped_config_seed_values(shipped_config):
    cfg = shipped_config
    otb = cfg.entities["OTB"]
    assert otb.legal_name == "OTB Group Limited"
    assert otb.company_number == "15732792"
    assert otb.place_of_registration == "England and Wales"
    assert otb.group_branding is True
    assert otb.statutory_verified is False
    assert otb.required_fields == ["name", "title"]
    assert otb.optional_fields == ["mobile"]
    assert cfg.entities["JIT"].company_number == "03281238"
    assert cfg.entities["VALE"].company_number == "13811748"
    assert cfg.entities["BIR"].company_number == "13838584"
    ahwe = cfg.entities["AHWE"]
    assert ahwe.legal_form == "unknown"
    assert ahwe.group_branding is False
    assert ahwe.company_number is None
    assert cfg.domain_to_entity == {
        "otbgroup.co.uk": "OTB",
        "jit-logistics.com": "JIT",
        "valeautomotive.co.uk": "VALE",
        "bir-d.co.uk": "BIR",
        "arthistorywithemily.co.uk": "AHWE",
    }
    assert cfg.skip_alias_domains == ["blakefamily.uk"]
    assert cfg.exclude_ou_prefixes == [
        "/99 _SYSTEM/BREAKGLASS",
        "/99 _SYSTEM/_Suspended",
        "/Workspace Guests",
    ]
    # AHWE by domain comes before every OU rule.
    assert cfg.primary_rules[0].entity == "AHWE"
    assert cfg.primary_rules[0].primary_domain == "arthistorywithemily.co.uk"
    assert [r.entity for r in cfg.primary_rules[1:]] == ["OTB", "JIT", "VALE", "BIR"]


def test_shipped_config_has_verified_flag_false_everywhere(shipped_config):
    for ent in shipped_config.entities.values():
        assert ent.statutory_verified is False


# ---------------------------------------------------------------------------
# load_config: validation failures
# ---------------------------------------------------------------------------


def test_unknown_entity_in_rule_fails(tmp_path):
    cfg = _minimal_config(
        rules={"primary": [{"match": {"ou_prefix": "/OTB"}, "entity": "NOPE"}]}
    )
    with pytest.raises(SignatureConfigError, match="NOPE"):
        _load_tmp(tmp_path, cfg)


def test_rule_with_both_matchers_fails(tmp_path):
    cfg = _minimal_config(
        rules={
            "primary": [
                {
                    "match": {
                        "ou_prefix": "/OTB",
                        "primary_domain": "otb.example.test",
                    },
                    "entity": "OTB",
                }
            ]
        }
    )
    with pytest.raises(SignatureConfigError, match="exactly one"):
        _load_tmp(tmp_path, cfg)


def test_rule_with_no_matcher_fails(tmp_path):
    cfg = _minimal_config(rules={"primary": [{"match": {}, "entity": "OTB"}]})
    with pytest.raises(SignatureConfigError, match="exactly one"):
        _load_tmp(tmp_path, cfg)


def test_ou_prefix_must_start_with_slash(tmp_path):
    cfg = _minimal_config(
        rules={"primary": [{"match": {"ou_prefix": "OTB"}, "entity": "OTB"}]}
    )
    with pytest.raises(SignatureConfigError, match="must start with '/'"):
        _load_tmp(tmp_path, cfg)


def test_domain_in_two_entities_fails(tmp_path):
    cfg = _minimal_config(
        entities={
            "OTB": _minimal_entity("OTB", domains=["shared.example.test"]),
            "JIT": _minimal_entity("JIT", domains=["Shared.Example.Test"]),
        }
    )
    with pytest.raises(SignatureConfigError, match="shared.example.test"):
        _load_tmp(tmp_path, cfg)


def test_domain_in_skip_list_and_entity_fails(tmp_path):
    cfg = _minimal_config(rules={"skip_alias_domains": ["OTB.example.test"]})
    with pytest.raises(SignatureConfigError, match="skip_alias_domains"):
        _load_tmp(tmp_path, cfg)


def test_legal_name_without_limited_or_ltd_fails(tmp_path):
    cfg = _minimal_config(
        entities={"OTB": _minimal_entity("OTB", legal_name="Example Holdings")}
    )
    with pytest.raises(SignatureConfigError, match="Limited"):
        _load_tmp(tmp_path, cfg)


@pytest.mark.parametrize(
    "legal_name",
    [
        "Example Holdings Limited",
        "Example Holdings Ltd",
        "Example Holdings Ltd.",
        "EXAMPLE LIMITED",
    ],
)
def test_legal_name_suffix_variants_accepted(tmp_path, legal_name):
    cfg = _minimal_config(
        entities={"OTB": _minimal_entity("OTB", legal_name=legal_name)}
    )
    loaded = _load_tmp(tmp_path, cfg)
    assert loaded.entities["OTB"].legal_name == legal_name


@pytest.mark.parametrize("word", ["TODO", "todo", "MISSING", "tbc", "Placeholder"])
def test_placeholder_words_rejected(tmp_path, word):
    cfg = _minimal_config(
        entities={"OTB": _minimal_entity("OTB", registered_office=f"{word} address")}
    )
    with pytest.raises(SignatureConfigError, match="registered_office"):
        _load_tmp(tmp_path, cfg)


def test_limited_company_missing_company_number_fails(tmp_path):
    cfg = _minimal_config(entities={"OTB": _minimal_entity("OTB", company_number=None)})
    with pytest.raises(SignatureConfigError, match="company_number"):
        _load_tmp(tmp_path, cfg)


def test_sole_trader_may_leave_statutory_fields_null(tmp_path):
    cfg = _minimal_config(
        entities={
            "SOLE": _minimal_entity(
                "SOLE",
                legal_form="sole_trader",
                legal_name="Jane Example",
                company_number=None,
                registered_office=None,
                group_branding=False,
            )
        }
    )
    loaded = _load_tmp(tmp_path, cfg)
    assert loaded.entities["SOLE"].legal_form == "sole_trader"
    assert loaded.entities["SOLE"].company_number is None


def test_bad_legal_form_fails(tmp_path):
    cfg = _minimal_config(
        entities={"OTB": _minimal_entity("OTB", legal_form="charity")}
    )
    with pytest.raises(SignatureConfigError, match="legal_form"):
        _load_tmp(tmp_path, cfg)


def test_missing_template_file_names_expected_path(tmp_path):
    cfg = _minimal_config()
    with pytest.raises(SignatureConfigError, match=r"OTB-signature-v1\.0\.0\.html"):
        _load_tmp(tmp_path, cfg, make_templates=False)


@pytest.mark.parametrize("bad", ["1.0", "v1.0.0", "1.0.0-rc1", 1])
def test_bad_semver_fails(tmp_path, bad):
    cfg = _minimal_config(
        entities={"OTB": _minimal_entity("OTB", template_version=bad)}
    )
    with pytest.raises(SignatureConfigError, match="template_version"):
        _load_tmp(tmp_path, cfg)


def test_group_branding_must_be_bool(tmp_path):
    cfg = _minimal_config(
        entities={"OTB": _minimal_entity("OTB", group_branding="yes")}
    )
    with pytest.raises(SignatureConfigError, match="group_branding"):
        _load_tmp(tmp_path, cfg)


def test_field_in_both_required_and_optional_fails(tmp_path):
    cfg = _minimal_config(
        entities={
            "OTB": _minimal_entity("OTB", required_fields=["name", "title", "mobile"])
        }
    )
    with pytest.raises(SignatureConfigError, match="mobile"):
        _load_tmp(tmp_path, cfg)


def test_wrong_version_fails(tmp_path):
    cfg = _minimal_config()
    cfg["version"] = 2
    with pytest.raises(SignatureConfigError, match="version"):
        _load_tmp(tmp_path, cfg)


def test_empty_entities_fails(tmp_path):
    cfg = _minimal_config()
    cfg["entities"] = {}
    cfg["rules"]["primary"] = []
    with pytest.raises(SignatureConfigError, match="entities"):
        _load_tmp(tmp_path, cfg)


def test_all_problems_reported_at_once(tmp_path):
    cfg = _minimal_config(
        entities={
            "OTB": _minimal_entity("OTB", legal_name="No Suffix", template_version="x")
        },
        rules={"primary": [{"match": {"ou_prefix": "OTB"}, "entity": "GHOST"}]},
    )
    with pytest.raises(SignatureConfigError) as info:
        _load_tmp(tmp_path, cfg)
    msg = str(info.value)
    assert "GHOST" in msg
    assert "must start with '/'" in msg
    assert "Limited" in msg
    assert "template_version" in msg


def test_domains_are_lower_cased(tmp_path):
    cfg = _minimal_config(
        entities={"OTB": _minimal_entity("OTB", domains=["OTB.Example.Test"])},
        rules={"skip_alias_domains": ["Personal.Example.Test"]},
    )
    loaded = _load_tmp(tmp_path, cfg)
    assert loaded.entities["OTB"].domains == ["otb.example.test"]
    assert loaded.skip_alias_domains == ["personal.example.test"]
    assert loaded.domain_to_entity == {"otb.example.test": "OTB"}


# ---------------------------------------------------------------------------
# resolve_primary_entity: real Directory cases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "email, ou, expected",
    [
        ("oliver@otbgroup.co.uk", "/01 OTB/Exec", "OTB"),
        ("sean.wallace.vale@otbgroup.co.uk", "/03 VALE/Ops", "VALE"),
        ("peter.wilce@jit-logistics.com", "/02 JIT/Compliance", "JIT"),
        ("ben@bir-d.co.uk", "/04 BIR/Projects", "BIR"),
        ("emily@arthistorywithemily.co.uk", "/99 _SYSTEM/Personal", "AHWE"),
    ],
)
def test_resolve_primary_entity_live_cases(shipped_config, email, ou, expected):
    code, reason = resolve_primary_entity(shipped_config, _user(email, ou))
    assert code == expected
    assert reason.startswith("rule:")


def test_resolve_primary_no_matching_rule(shipped_config):
    code, reason = resolve_primary_entity(
        shipped_config, _user("automated@otbgroup.co.uk", "/")
    )
    assert code is None
    assert reason.startswith("no matching rule")
    assert "/" in reason and "otbgroup.co.uk" in reason


def test_resolve_primary_excluded_ou(shipped_config):
    code, reason = resolve_primary_entity(
        shipped_config,
        _user("breakglass-admin@otbgroup.co.uk", "/99 _SYSTEM/BREAKGLASS"),
    )
    assert code is None
    assert reason == "excluded OU /99 _SYSTEM/BREAKGLASS"


def test_resolve_primary_excluded_ou_child(shipped_config):
    code, reason = resolve_primary_entity(
        shipped_config, _user("x@otbgroup.co.uk", "/99 _SYSTEM/_Suspended/Old")
    )
    assert code is None
    assert reason.startswith("excluded OU")


def test_resolve_primary_prefix_does_not_match_sibling(tmp_path):
    """``/OTB`` must not match ``/OTBX``: the boundary is a path separator."""
    loaded = _load_tmp(tmp_path, _minimal_config())
    code, _ = resolve_primary_entity(loaded, _user("a@otb.example.test", "/OTBX/Team"))
    assert code is None
    code, _ = resolve_primary_entity(loaded, _user("a@otb.example.test", "/OTB"))
    assert code == "OTB"


def test_resolve_primary_suspended(shipped_config):
    code, reason = resolve_primary_entity(
        shipped_config, _user("oliver@otbgroup.co.uk", "/01 OTB/Exec", suspended=True)
    )
    assert (code, reason) == (None, "suspended")


def test_resolve_primary_archived(shipped_config):
    code, reason = resolve_primary_entity(
        shipped_config, _user("oliver@otbgroup.co.uk", "/01 OTB/Exec", archived=True)
    )
    assert (code, reason) == (None, "archived")


def test_resolve_primary_suspended_beats_excluded(shipped_config):
    code, reason = resolve_primary_entity(
        shipped_config,
        _user("x@otbgroup.co.uk", "/99 _SYSTEM/BREAKGLASS", suspended=True),
    )
    assert (code, reason) == (None, "suspended")


# ---------------------------------------------------------------------------
# resolve_alias_entity: Oli's real aliases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "alias, expected_code, reason_start",
    [
        ("oliver.blake@jit-logistics.com", "JIT", "alias domain jit-logistics.com"),
        ("oliver@bir-d.co.uk", "BIR", "alias domain bir-d.co.uk"),
        ("otb@otbgroup.co.uk", "OTB", "same domain as primary"),
        ("oliver@blakefamily.uk", None, "skipped: personal domain"),
        ("someone@example.com", None, "unmapped domain example.com"),
    ],
)
def test_resolve_alias_entity(shipped_config, alias, expected_code, reason_start):
    code, reason = resolve_alias_entity(
        shipped_config, alias, "OTB", "oliver@otbgroup.co.uk"
    )
    assert code == expected_code
    assert reason.startswith(reason_start)


def test_resolve_alias_when_primary_unmanaged(shipped_config):
    code, reason = resolve_alias_entity(
        shipped_config, "oliver.blake@jit-logistics.com", None, "oliver@otbgroup.co.uk"
    )
    assert (code, reason) == (None, "primary address unmanaged")


def test_resolve_alias_is_case_insensitive(shipped_config):
    code, _ = resolve_alias_entity(
        shipped_config, "Oliver.Blake@JIT-Logistics.com", "OTB", "oliver@otbgroup.co.uk"
    )
    assert code == "JIT"


# ---------------------------------------------------------------------------
# extract_person
# ---------------------------------------------------------------------------


def test_extract_person_title_from_primary_organisation(shipped_config):
    user = _user(
        "a@otbgroup.co.uk",
        "/01 OTB",
        organizations=[
            {"title": "Old Role", "primary": False},
            {"title": "Current Role", "primary": True},
        ],
    )
    person = extract_person(user, shipped_config.entities["OTB"])
    assert person.title == "Current Role"
    assert person.name == "Test Person"
    assert person.given_name == "Test"
    assert person.family_name == "Person"
    assert person.email == "a@otbgroup.co.uk"


def test_extract_person_title_falls_back_to_first_with_title(shipped_config):
    user = _user(
        "a@otbgroup.co.uk",
        "/01 OTB",
        organizations=[
            {"name": "Some Dept", "primary": True},
            {"title": "   ", "primary": False},
            {"title": "Fallback Role", "primary": False},
        ],
    )
    person = extract_person(user, shipped_config.entities["OTB"])
    assert person.title == "Fallback Role"


def test_extract_person_missing_title_raises(shipped_config):
    user = _user("a@otbgroup.co.uk", "/01 OTB", title=None)
    with pytest.raises(MissingDirectoryDataError) as info:
        extract_person(user, shipped_config.entities["OTB"])
    assert info.value.missing == ["title"]
    assert "title" in str(info.value)


def test_extract_person_whitespace_title_counts_as_missing(shipped_config):
    user = _user("a@otbgroup.co.uk", "/01 OTB", title="   ")
    with pytest.raises(MissingDirectoryDataError) as info:
        extract_person(user, shipped_config.entities["OTB"])
    assert info.value.missing == ["title"]


def test_extract_person_missing_name_raises(shipped_config):
    user = _user("a@otbgroup.co.uk", "/01 OTB", given="", family="", full=None)
    with pytest.raises(MissingDirectoryDataError) as info:
        extract_person(user, shipped_config.entities["OTB"])
    assert info.value.missing == ["name"]


def test_extract_person_reports_every_missing_field(shipped_config):
    user = _user(
        "a@otbgroup.co.uk", "/01 OTB", given=" ", family="", full=None, title=None
    )
    with pytest.raises(MissingDirectoryDataError) as info:
        extract_person(user, shipped_config.entities["OTB"])
    assert info.value.missing == ["name", "title"]


def test_extract_person_name_from_given_and_family_when_no_full_name(shipped_config):
    user = _user(
        "a@otbgroup.co.uk", "/01 OTB", given="Ada", family="Lovelace", full=None
    )
    person = extract_person(user, shipped_config.entities["OTB"])
    assert person.name == "Ada Lovelace"


def test_extract_person_mobile_none_when_absent(shipped_config):
    user = _user(
        "a@otbgroup.co.uk", "/01 OTB", phones=[{"type": "work", "value": "01949 1"}]
    )
    person = extract_person(user, shipped_config.entities["OTB"])
    assert person.mobile is None


@pytest.mark.parametrize("ptype", ["mobile", "work_mobile"])
def test_extract_person_mobile_types(shipped_config, ptype):
    user = _user(
        "a@otbgroup.co.uk", "/01 OTB", phones=[{"type": ptype, "value": "07700 900000"}]
    )
    person = extract_person(user, shipped_config.entities["OTB"])
    assert person.mobile == "07700 900000"


def test_extract_person_mobile_prefers_primary(shipped_config):
    user = _user(
        "a@otbgroup.co.uk",
        "/01 OTB",
        phones=[
            {"type": "mobile", "value": "07700 000001"},
            {"type": "work_mobile", "value": "07700 000002", "primary": True},
        ],
    )
    person = extract_person(user, shipped_config.entities["OTB"])
    assert person.mobile == "07700 000002"


def test_extract_person_whitespace_mobile_is_none(shipped_config):
    user = _user(
        "a@otbgroup.co.uk", "/01 OTB", phones=[{"type": "mobile", "value": "  "}]
    )
    person = extract_person(user, shipped_config.entities["OTB"])
    assert person.mobile is None


def test_extract_person_mobile_required_when_entity_says_so(tmp_path):
    cfg = _minimal_config(
        entities={
            "OTB": _minimal_entity(
                "OTB", required_fields=["name", "title", "mobile"], optional_fields=[]
            )
        }
    )
    loaded = _load_tmp(tmp_path, cfg)
    with pytest.raises(MissingDirectoryDataError) as info:
        extract_person(_user("a@otb.example.test", "/OTB"), loaded.entities["OTB"])
    assert info.value.missing == ["mobile"]


# ---------------------------------------------------------------------------
# Renderer
# ---------------------------------------------------------------------------


def _tpl(signature_html: str, statutory_html: str = "{{legal_name}}") -> LoadedTemplate:
    return LoadedTemplate(
        entity="OTB",
        signature_html=signature_html,
        statutory_html=statutory_html,
        template_version="1.0.0",
        statutory_version="1.0.0",
        signature_path=Path("/nonexistent/OTB-signature-v1.0.0.html"),
        statutory_path=Path("/nonexistent/OTB-statutory-v1.0.0.html"),
    )


def _person(mobile: str | None = None, name: str = "Ada Lovelace") -> Person:
    return Person(
        email="ada@otbgroup.co.uk",
        name=name,
        given_name="Ada",
        family_name="Lovelace",
        title="Engineer",
        mobile=mobile,
    )


def test_render_escapes_html_in_values(shipped_config):
    tpl = _tpl("<p>{{name}}</p>")
    out = render_signature(
        tpl, _person(name='<b>Ada</b> & "Co"'), shipped_config.entities["OTB"], ""
    )
    assert "<b>Ada</b>" not in out
    assert "&lt;b&gt;Ada&lt;/b&gt; &amp; &quot;Co&quot;" in out


def test_render_optional_block_removed_when_mobile_missing(shipped_config):
    tpl = _tpl("A{{#mobile}}<tr><td>{{mobile}}</td></tr>{{/mobile}}B")
    out = render_signature(tpl, _person(None), shipped_config.entities["OTB"], "")
    assert out == "AB"


def test_render_optional_block_kept_when_mobile_present(shipped_config):
    tpl = _tpl("A{{#mobile}}<tr><td>{{mobile}}</td></tr>{{/mobile}}B")
    out = render_signature(
        tpl, _person("07700 900000"), shipped_config.entities["OTB"], ""
    )
    assert out == "A<tr><td>07700 900000</td></tr>B"


def test_render_bare_mobile_with_no_mobile_raises(shipped_config):
    tpl = _tpl("<p>{{mobile}}</p>")
    with pytest.raises(
        TemplateError,
        match=r"mobile is empty; wrap it in \{\{#mobile\}\}...\{\{/mobile\}\}",
    ):
        render_signature(tpl, _person(None), shipped_config.entities["OTB"], "")


def test_render_unknown_placeholder_raises(shipped_config):
    tpl = _tpl("<p>{{nickname}}</p>")
    with pytest.raises(TemplateError, match="nickname"):
        render_signature(tpl, _person(), shipped_config.entities["OTB"], "")


def test_render_unknown_block_raises(shipped_config):
    tpl = _tpl("{{#nickname}}x{{/nickname}}")
    with pytest.raises(TemplateError, match="nickname"):
        render_signature(tpl, _person(), shipped_config.entities["OTB"], "")


def test_render_unclosed_block_raises(shipped_config):
    tpl = _tpl("{{#mobile}}x")
    with pytest.raises(TemplateError):
        render_signature(tpl, _person("1"), shipped_config.entities["OTB"], "")


def test_render_nested_blocks_raise(shipped_config):
    tpl = _tpl("{{#mobile}}{{#title}}x{{/title}}{{/mobile}}")
    with pytest.raises(TemplateError, match="nest"):
        render_signature(tpl, _person("1"), shipped_config.entities["OTB"], "")


def test_render_statutory_inserted_unescaped(shipped_config):
    tpl = _tpl("<p>{{statutory}}</p>")
    out = render_signature(
        tpl, _person(), shipped_config.entities["OTB"], "<span>Reg &amp; Co</span>"
    )
    assert out == "<p><span>Reg &amp; Co</span></p>"


def test_render_statutory_defaults_to_entity_partial(shipped_config):
    tpl = _tpl("<p>{{statutory}}</p>", "{{legal_name}} no. {{company_number}}")
    out = render_signature(tpl, _person(), shipped_config.entities["OTB"])
    assert out == "<p>OTB Group Limited no. 15732792</p>"


def test_signature_template_with_statutory_only_placeholder_raises(shipped_config):
    tpl = _tpl("<p>{{company_number}}</p>")
    with pytest.raises(TemplateError, match="company_number"):
        render_signature(tpl, _person(), shipped_config.entities["OTB"], "")


def test_statutory_template_with_signature_only_placeholder_raises(shipped_config):
    tpl = _tpl("x", "{{name}}")
    with pytest.raises(TemplateError, match="name"):
        render_statutory(tpl, shipped_config.entities["OTB"])


def test_render_statutory_optional_vat_block(shipped_config):
    tpl = _tpl("x", "{{legal_name}}{{#vat_number}} VAT {{vat_number}}.{{/vat_number}}")
    assert (
        render_statutory(tpl, shipped_config.entities["OTB"])
        == "OTB Group Limited VAT 494870441."
    )
    assert (
        render_statutory(tpl, shipped_config.entities["AHWE"])
        == "Art History with Emily"
    )


def test_render_statutory_bare_null_field_raises(shipped_config):
    tpl = _tpl("x", "{{legal_name}} {{vat_number}}")
    with pytest.raises(TemplateError, match="vat_number is empty"):
        render_statutory(tpl, shipped_config.entities["AHWE"])


def test_render_statutory_escapes_values(tmp_path):
    cfg = _minimal_config(
        entities={
            "OTB": _minimal_entity("OTB", registered_office="1 <High> Street & Co")
        }
    )
    loaded = _load_tmp(tmp_path, cfg)
    tpl = _tpl("x", "{{registered_office}}")
    assert (
        render_statutory(tpl, loaded.entities["OTB"])
        == "1 &lt;High&gt; Street &amp; Co"
    )


def test_render_email_uses_person_email(shipped_config):
    tpl = _tpl("{{email}}")
    out = render_signature(tpl, _person(), shipped_config.entities["OTB"], "")
    assert out == "ada@otbgroup.co.uk"


# ---------------------------------------------------------------------------
# Hashing
# ---------------------------------------------------------------------------


def test_normalise_html():
    assert normalise_html(None) == ""
    assert normalise_html("  <p>\n  a   b </p>\t") == "<p> a b </p>"


def test_signature_hash():
    assert signature_hash(None) == ""
    assert signature_hash("   ") == ""
    expected = hashlib.sha256(b"<p> a b </p>").hexdigest()
    assert signature_hash("<p>\n a   b </p>") == expected
    assert signature_hash("<p> a b </p>") == expected


# ---------------------------------------------------------------------------
# plan_for_user
# ---------------------------------------------------------------------------


def test_plan_for_user_oli(shipped_config):
    rows = plan_for_user(shipped_config, OLI, OLI_SEND_AS)
    assert len(rows) == 5
    assert all(isinstance(r, PlannedSignature) for r in rows)
    assert rows[0].is_primary is True
    assert [r.send_as_email for r in rows] == [
        "oliver@otbgroup.co.uk",
        "oliver.blake@jit-logistics.com",
        "oliver@bir-d.co.uk",
        "otb@otbgroup.co.uk",
        "oliver@blakefamily.uk",
    ]
    assert [r.entity for r in rows] == ["OTB", "JIT", "BIR", "OTB", None]
    assert [r.status for r in rows] == [
        "planned",
        "planned",
        "planned",
        "planned",
        "skipped",
    ]
    assert rows[4].reason.startswith("skipped: personal domain")
    assert rows[4].html is None and rows[4].rendered_hash is None
    for row in rows[:4]:
        assert row.user_email == "oliver@otbgroup.co.uk"
        assert row.template_version == "1.0.0"
        assert row.statutory_version == "1.0.0"
        assert row.html and "Oliver Blake" in row.html
        assert row.rendered_hash == signature_hash(row.html)
    # The JIT alias signature shows the JIT address and the JIT company number.
    assert "oliver.blake@jit-logistics.com" in rows[1].html
    assert "03281238" in rows[1].html
    assert "15732792" in rows[0].html


def test_plan_for_user_primary_first_even_if_listed_last(shipped_config):
    send_as = list(reversed(OLI_SEND_AS))
    rows = plan_for_user(shipped_config, OLI, send_as)
    assert rows[0].is_primary is True
    assert rows[0].send_as_email == "oliver@otbgroup.co.uk"


def test_plan_for_user_missing_template_isolated(shipped_copy):
    cfg_path, templates_dir = shipped_copy
    cfg = load_config(cfg_path, templates_dir)
    (templates_dir / "JIT" / "JIT-signature-v1.0.0.html").unlink()
    rows = plan_for_user(cfg, OLI, OLI_SEND_AS)
    assert [r.status for r in rows] == [
        "planned",
        "error",
        "planned",
        "planned",
        "skipped",
    ]
    assert "JIT-signature-v1.0.0.html" in rows[1].reason
    assert rows[1].html is None


def test_plan_for_user_missing_directory_field_isolated(shipped_config):
    user = copy.deepcopy(OLI)
    user["organizations"] = []
    rows = plan_for_user(shipped_config, user, OLI_SEND_AS)
    assert [r.status for r in rows] == ["error", "error", "error", "error", "skipped"]
    assert "title" in rows[0].reason


def test_plan_for_user_unmanaged_primary_skips_aliases(shipped_config):
    user = _user("automated@otbgroup.co.uk", "/")
    rows = plan_for_user(
        shipped_config,
        user,
        [
            _send_as("automated@otbgroup.co.uk", primary=True),
            _send_as("automated@jit-logistics.com"),
        ],
    )
    assert [r.status for r in rows] == ["skipped", "skipped"]
    assert rows[0].reason.startswith("no matching rule")
    assert rows[1].reason == "primary address unmanaged"


def test_plan_for_user_mobile_optional(shipped_config):
    user = copy.deepcopy(OLI)
    user["phones"] = [{"type": "mobile", "value": "07700 900123"}]
    with_mobile = plan_for_user(shipped_config, user, OLI_SEND_AS[:1])[0]
    without = plan_for_user(shipped_config, OLI, OLI_SEND_AS[:1])[0]
    assert "07700 900123" in with_mobile.html
    assert "07700" not in without.html
    assert without.status == "planned"


# ---------------------------------------------------------------------------
# drift_status
# ---------------------------------------------------------------------------


def _planned(status="planned", html="<p>sig</p>") -> PlannedSignature:
    return PlannedSignature(
        user_email="oliver@otbgroup.co.uk",
        send_as_email="oliver@otbgroup.co.uk",
        is_primary=True,
        entity="OTB",
        template_version="1.0.0",
        statutory_version="1.0.0",
        html=html if status == "planned" else None,
        rendered_hash=signature_hash(html) if status == "planned" else None,
        status=status,
        reason="",
    )


def _ledger(
    readback_html="<p>sig as gmail stored it</p>", tv="1.0.0", sv="1.0.0"
) -> dict:
    return {
        "applied_at": "2026-09-25T10:00:00+00:00",
        "actor": "oliver@otbgroup.co.uk",
        "user_email": "oliver@otbgroup.co.uk",
        "send_as_email": "oliver@otbgroup.co.uk",
        "entity": "OTB",
        "template_version": tv,
        "statutory_version": sv,
        "rendered_hash": signature_hash("<p>sig</p>"),
        "readback_hash": signature_hash(readback_html),
        "previous_hash": "",
        "previous_signature_html": "",
        "run_id": "run-1",
    }


def test_drift_unmanaged_first():
    status, _ = drift_status(_planned("skipped"), "<p>x</p>", _ledger())
    assert status == "unmanaged"


def test_drift_error_second():
    status, _ = drift_status(_planned("error"), "<p>x</p>", None)
    assert status == "error"


def test_drift_never_applied():
    status, reason = drift_status(_planned(), "<p>x</p>", None)
    assert status == "never_applied"
    assert reason


def test_drift_stale_template_version():
    status, reason = drift_status(
        _planned(), "<p>sig as gmail stored it</p>", _ledger(tv="0.9.0")
    )
    assert status == "stale_template"
    assert "0.9.0" in reason and "1.0.0" in reason


def test_drift_stale_statutory_version():
    status, _ = drift_status(
        _planned(), "<p>sig as gmail stored it</p>", _ledger(sv="0.9.0")
    )
    assert status == "stale_template"


def test_drift_stale_beats_changed():
    status, _ = drift_status(_planned(), "<p>edited by user</p>", _ledger(tv="0.9.0"))
    assert status == "stale_template"


def test_drift_changed_since_apply():
    status, _ = drift_status(_planned(), "<p>edited by user</p>", _ledger())
    assert status == "changed_since_apply"


def test_drift_in_sync_ignores_whitespace():
    status, _ = drift_status(_planned(), "<p>sig   as gmail\n stored it</p>", _ledger())
    assert status == "in_sync"


def test_drift_in_sync_does_not_compare_raw_template():
    """Gmail sanitises what it stores; in_sync is judged on the readback hash only."""
    planned = _planned(html="<table><tr><td>rich</td></tr></table>")
    status, _ = drift_status(planned, "<p>sig as gmail stored it</p>", _ledger())
    assert status == "in_sync"


# ---------------------------------------------------------------------------
# Result table
# ---------------------------------------------------------------------------


def _rows() -> list[ResultRow]:
    me = "oliver@otbgroup.co.uk"
    return [
        ResultRow(me, me, "OTB", "1.0.0", "applied", "", "a" * 64, "b" * 64),
        ResultRow(
            me,
            "oliver.blake@jit-logistics.com",
            "JIT",
            "1.0.0",
            "would_apply",
            "",
            None,
            "c" * 64,
        ),
        ResultRow(
            me,
            "oliver@bir-d.co.uk",
            "BIR",
            "1.0.0",
            "unchanged",
            "",
            "d" * 64,
            "d" * 64,
        ),
        ResultRow(
            me,
            "oliver@blakefamily.uk",
            None,
            None,
            "skipped",
            "skipped: personal domain",
            None,
            None,
        ),
        ResultRow(
            me,
            "otb@otbgroup.co.uk",
            "OTB",
            None,
            "error",
            "template missing",
            None,
            None,
        ),
        ResultRow(
            "a@otbgroup.co.uk",
            "a@otbgroup.co.uk",
            "OTB",
            "1.0.0",
            "unchanged",
            "",
            "e" * 64,
            "e" * 64,
        ),
    ]


def test_format_result_table_header_alignment_and_summary():
    text = format_result_table(_rows())
    lines = text.splitlines()
    header = lines[0]
    for col in ("user_email", "send_as_email", "entity", "action", "reason"):
        assert col in header
    body = lines[1:-1]
    assert len(body) == 6
    assert all(len(line) == len(header) for line in body)
    # Columns line up: 'action' values start at the same offset as the header word.
    idx = header.index("action")
    assert body[0][idx:].startswith("applied")
    assert body[1][idx:].startswith("would_apply")
    assert lines[-1] == "applied 1, would_apply 1, unchanged 2, skipped 1, error 1"


def test_format_result_table_empty():
    text = format_result_table([])
    assert (
        text.splitlines()[-1]
        == "applied 0, would_apply 0, unchanged 0, skipped 0, error 0"
    )


def test_format_result_table_does_not_print_full_hashes():
    text = format_result_table(_rows())
    assert "a" * 64 not in text
    assert "aaaaaaaa" in text


def test_result_rows_as_dicts():
    dicts = result_rows_as_dicts(_rows())
    assert len(dicts) == 6
    assert dicts[0]["action"] == "applied"
    assert dicts[3]["entity"] is None
    assert set(dicts[0]) == {
        "user_email",
        "send_as_email",
        "entity",
        "template_version",
        "action",
        "reason",
        "before_hash",
        "after_hash",
    }
    assert json.dumps(dicts)


# ---------------------------------------------------------------------------
# AHWE ring-fence
# ---------------------------------------------------------------------------


FORBIDDEN_IN_AHWE = ("otb", "jit", "vale", "bir", "langar", "harby")


def _assert_ring_fenced(text: str, where: str) -> None:
    lowered = text.lower()
    for word in FORBIDDEN_IN_AHWE:
        assert word not in lowered, f"{where} mentions {word!r}"


def test_ahwe_templates_are_ring_fenced():
    ahwe_dir = DEFAULT_TEMPLATES_DIR / "AHWE"
    files = sorted(p for p in ahwe_dir.rglob("*") if p.is_file())
    assert files, "no AHWE template files found"
    for path in files:
        _assert_ring_fenced(path.read_text(encoding="utf-8"), str(path))
        _assert_ring_fenced(path.name, path.name)


def test_ahwe_config_block_is_ring_fenced():
    raw = yaml.safe_load(DEFAULT_CONFIG_PATH.read_text(encoding="utf-8"))
    block = raw["entities"]["AHWE"]
    _assert_ring_fenced(json.dumps(block), "entities.yaml AHWE block")
    assert block["group_branding"] is False
    assert block["legal_form"] == "unknown"


def test_ahwe_statutory_is_name_and_website_only(shipped_config):
    tpl = load_template(shipped_config, "AHWE")
    out = render_statutory(tpl, shipped_config.entities["AHWE"])
    assert out == "Art History with Emily | arthistorywithemily.co.uk"


# ---------------------------------------------------------------------------
# Shipped templates render for every entity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("code", ALL_CODES)
def test_shipped_templates_render(shipped_config, code):
    entity = shipped_config.entities[code]
    tpl = load_template(shipped_config, code)
    person = _person(mobile="07700 900000")
    statutory = render_statutory(tpl, entity)
    html = render_signature(tpl, person, entity, statutory)
    assert "Ada Lovelace" in html
    assert "Engineer" in html
    assert "07700 900000" in html
    assert "ada@otbgroup.co.uk" in html
    assert statutory in html
    assert "{{" not in html and "}}" not in html
    assert html.count("<table") == 1
    if code in GROUP_CODES:
        assert entity.company_number in html
        assert entity.legal_name in html
        assert "England and Wales" in html
    # Without a mobile the row disappears and nothing is left half-rendered.
    html_no_mobile = render_signature(tpl, _person(mobile=None), entity, statutory)
    assert "07700" not in html_no_mobile
    assert "{{" not in html_no_mobile


@pytest.mark.parametrize("code", ALL_CODES)
def test_shipped_templates_are_inline_only(code):
    for path in (DEFAULT_TEMPLATES_DIR / code).iterdir():
        text = path.read_text(encoding="utf-8").lower()
        for tag in ("<style", "<script", "<link", "<meta", "<form"):
            assert tag not in text, f"{path.name} contains {tag}"


def test_shipped_templates_use_only_known_placeholders():
    pattern = re.compile(r"\{\{[#/]?\s*([a-z_]+)\s*\}\}")
    for code in ALL_CODES:
        for path in (DEFAULT_TEMPLATES_DIR / code).iterdir():
            names = set(pattern.findall(path.read_text(encoding="utf-8")))
            if "-signature-" in path.name:
                allowed = SIGNATURE_PLACEHOLDERS
            else:
                allowed = STATUTORY_PLACEHOLDERS
            assert names <= allowed, f"{path.name} uses {names - allowed}"


def test_no_dashes_in_new_files():
    """House rule: no em dash or en dash anywhere under the package or its tests."""
    root = Path(__file__).resolve().parent.parent.parent
    for base in (root / "gsignatures", root / "tests" / "gsignatures"):
        for path in base.rglob("*"):
            if path.is_file() and path.suffix in {".py", ".yaml", ".html", ".md"}:
                text = path.read_text(encoding="utf-8")
                # Built from code points so this file itself passes the scan.
                assert chr(0x2014) not in text, f"em dash in {path}"
                assert chr(0x2013) not in text, f"en dash in {path}"
