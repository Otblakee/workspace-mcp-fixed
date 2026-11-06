"""Signature template 2.0.0: brand partials, DDI, per-send-as overrides (v1.19.0).

* Every entity partial renders the 2.0.0 template for a fixture person (OTB
  and BIR with the shipped verified partials; JIT and VALE with their shipped
  partial filled in and marked verified in a temporary copy, since the
  shipped files hold nulls on purpose).
* The DDI and mobile rows appear only when the Directory has the value.
* An override in entities.yaml applies to the named send-as only.
* Snapshot: the OTB render equals the live OTB signature read back from
  Gmail on 2026-11-06 (tests/gsignatures/fixtures), once the Google image
  proxy prefix and the artefacts Gmail's sanitiser adds or drops are
  normalised on both sides. Those artefacts are listed in
  ``_gmail_normalise`` and nowhere else.
* The pin rule: an entity cannot pin 2.0.0 while its partial is unverified.
* The ledger row and the audit row carry the overrides; the dry-run row
  names them; the preview prints the template version actually used.
"""

from __future__ import annotations

import dataclasses
import json
import re
import shutil
import sys
from pathlib import Path

import pytest
import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from gsignatures import engine, operations  # noqa: E402
from gsignatures.engine import (  # noqa: E402
    DEFAULT_CONFIG_PATH,
    DEFAULT_PARTIALS_DIR,
    DEFAULT_TEMPLATES_DIR,
    Person,
    SignatureConfigError,
    TemplateError,
    load_config,
    load_template,
    plan_for_user,
    render_signature,
)
from gsignatures.ledger import AUDIT_HEADER, LEDGER_HEADER  # noqa: E402
from tests.gsignatures.fakes import (  # noqa: E402
    FakeDirectory,
    FakeGmailPool,
    FakeSheets,
    send_as,
    user,
)

FIXTURES = Path(__file__).parent / "fixtures"
LIVE_OTB_HTML = (
    (FIXTURES / "otb_live_signature_2026-11-06.html")
    .read_text(encoding="utf-8")
    .strip()
)

OLI = "oliver@otbgroup.co.uk"
OLI_BIR = "oliver@bir-d.co.uk"
OLI_JIT = "oliver.blake@jit-logistics.com"
BEN = "ben@bir-d.co.uk"
DDI = "+44 (0)1949 898 203"
MOBILE = "07700 900123"


def _oli(*, ddi: str | None = DDI, mobile: str | None = None) -> dict:
    phones = []
    if ddi:
        phones.append({"type": "work", "value": ddi, "primary": True})
    if mobile:
        phones.append({"type": "mobile", "value": mobile})
    return user(
        OLI,
        "/01 OTB/Exec",
        given="Oli",
        family="Blake",
        full="Oli Blake",
        title="Group Managing Director",
        phones=phones,
    )


def _person(**overrides) -> Person:
    base = Person(
        email="ada@otbgroup.co.uk",
        name="Ada Lovelace",
        given_name="Ada",
        family_name="Lovelace",
        title="Engineer",
        mobile=None,
        ddi=None,
    )
    return dataclasses.replace(base, **overrides)


@pytest.fixture(scope="module")
def cfg():
    return load_config()


@pytest.fixture
def shipped_copy(tmp_path):
    """Writable copy of the shipped config, templates and partials."""
    cfg_path = tmp_path / "entities.yaml"
    shutil.copy(DEFAULT_CONFIG_PATH, cfg_path)
    shutil.copytree(DEFAULT_TEMPLATES_DIR, tmp_path / "templates")
    shutil.copytree(DEFAULT_PARTIALS_DIR, tmp_path / "partials")
    return cfg_path, tmp_path / "templates", tmp_path / "partials"


def _set_yaml(path: Path, **changes) -> None:
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    data.update(changes)
    path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


def _pin(cfg_path: Path, code: str, version: str) -> None:
    data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    data["entities"][code]["template_version"] = version
    cfg_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Gmail normalisation for the snapshot
# ---------------------------------------------------------------------------

_GOOGLE_PROXY_RE = re.compile(r"https://ci\d+\.googleusercontent\.com/[^\"#]*#")


def _gmail_normalise(html: str) -> str:
    """What Gmail's sanitiser changed on the live signature, undone on both
    sides so the comparison is about content:

    * the Google image proxy prefix on ``<img src>``;
    * ``<span> </span>`` fillers where the template has a plain space;
    * ``target="_blank"`` added to every anchor;
    * ``alt`` and ``style="display:block"`` dropped from the image, and a
      ``<br>`` added after it;
    * ``font-family`` dropped from the table style;
    * newlines between elements.
    """
    text = _GOOGLE_PROXY_RE.sub("", html)
    text = text.replace("<span> </span>", " ")
    text = text.replace(' target="_blank"', "")
    text = re.sub(r' alt="[^"]*"', "", text)
    text = text.replace(' style="display:block"', "")
    text = text.replace("<br></td>", "</td>")
    text = text.replace(";font-family:Arial,Helvetica,sans-serif", "")
    text = text.replace("\n", "")
    return text


# ---------------------------------------------------------------------------
# Shipped pins and partials
# ---------------------------------------------------------------------------


class TestShippedPins:
    def test_otb_and_bir_pin_2_0_0_with_verified_partials(self, cfg):
        assert cfg.entities["OTB"].template_version == "2.0.0"
        assert cfg.entities["BIR"].template_version == "2.0.0"
        assert cfg.partials["OTB"].verified is True
        assert cfg.partials["BIR"].verified is True

    def test_jit_and_vale_stay_on_1_0_0_with_unverified_partials(self, cfg):
        for code in ("JIT", "VALE"):
            assert cfg.entities[code].template_version == "1.0.0", code
            partial = cfg.partials[code]
            assert partial.verified is False
            # Nothing invented: phone, logo and colours are null.
            for name in ("office_tel", "logo_url", "rule_rgb", "name_rgb", "label_rgb"):
                assert partial.values[name] is None, (code, name)

    def test_ahwe_has_no_partial_and_is_untouched(self, cfg):
        assert "AHWE" not in cfg.partials
        assert cfg.entities["AHWE"].template_version == "1.0.0"
        assert not (DEFAULT_PARTIALS_DIR / "AHWE.yaml").exists()

    def test_bir_partial_values(self, cfg):
        values = cfg.partials["BIR"].values
        assert values["entity_name"] == "BIR Developments Ltd"
        assert (
            values["logo_url"] == "https://cdn.otbgroup.co.uk/bir/bir-logo-black-v1.png"
        )
        assert values["logo_w"] == "170"
        assert values["logo_h"] is None
        assert (
            values["rule_rgb"]
            == values["name_rgb"]
            == values["label_rgb"]
            == "rgb(50,71,116)"
        )
        assert values["office_tel"] == "+44 (0)1949 861 701"
        assert values["website"] == "bir-d.co.uk"
        assert values["address"] == (
            "Building 83, Langar Industrial Estate, Harby Road, Langar, "
            "Nottinghamshire, NG13 9HY"
        )

    def test_otb_partial_values(self, cfg):
        values = cfg.partials["OTB"].values
        assert (
            values["logo_url"] == "https://cdn.otbgroup.co.uk/otb/otb-logo-shimmer.png"
        )
        assert values["logo_w"] == "200" and values["logo_h"] == "120"
        assert values["rule_rgb"] == "rgb(201,162,39)"
        assert values["name_rgb"] == "rgb(31,56,100)"
        assert values["label_rgb"] == "rgb(201,162,39)"
        assert values["office_tel"] == "+44 (0)1949 861 514"

    def test_statutory_numbers_come_from_entities_yaml_not_the_partial(self, cfg):
        for code in ("OTB", "BIR"):
            raw = yaml.safe_load(
                (DEFAULT_PARTIALS_DIR / f"{code}.yaml").read_text(encoding="utf-8")
            )
            assert "company_no" not in raw and "vat_no" not in raw
            values = engine.partial_values(cfg.partials[code], cfg.entities[code])
            assert values["company_no"] == cfg.entities[code].company_number
            assert values["vat_no"] == cfg.entities[code].vat_number

    def test_statutory_verified_flag_still_governs_the_warning(self, cfg):
        # The statutory values and their verified flag stayed in entities.yaml.
        assert cfg.entities["OTB"].statutory_verified is True
        assert cfg.entities["AHWE"].statutory_verified is False

    def test_overrides_shipped(self, cfg):
        assert cfg.overrides == {OLI_BIR: {"title": "Managing Director"}}

    def test_every_group_entity_has_a_2_0_0_template_file(self):
        for code in ("OTB", "JIT", "VALE", "BIR"):
            path = DEFAULT_TEMPLATES_DIR / code / f"{code}-signature-v2.0.0.html"
            assert path.is_file(), code
            assert engine.template_uses_partial(path.read_text(encoding="utf-8"))
        assert not (
            DEFAULT_TEMPLATES_DIR / "AHWE" / "AHWE-signature-v2.0.0.html"
        ).exists()


# ---------------------------------------------------------------------------
# Rendering every entity partial
# ---------------------------------------------------------------------------


def _verified_copy(cfg_path: Path, partials_dir: Path, code: str) -> None:
    """Fill the shipped JIT / VALE partial with example values and verify it."""
    _set_yaml(
        partials_dir / f"{code}.yaml",
        verified=True,
        logo_url=f"https://cdn.otbgroup.co.uk/{code.lower()}/{code.lower()}-logo-v1.png",
        logo_w=180,
        rule_rgb="rgb(1,2,3)",
        name_rgb="rgb(4,5,6)",
        label_rgb="rgb(7,8,9)",
        office_tel="+44 (0)1949 000 000",
    )
    _pin(cfg_path, code, "2.0.0")


class TestRenderEveryEntity:
    @pytest.mark.parametrize("code", ["OTB", "BIR"])
    def test_shipped_verified_partials_render(self, cfg, code):
        entity = cfg.entities[code]
        tpl = load_template(cfg, code)
        html = render_signature(
            tpl, _person(ddi=DDI, mobile=MOBILE), entity, partial=cfg.partials[code]
        )
        values = cfg.partials[code].values
        assert values["logo_url"] in html
        assert f'width="{values["logo_w"]}"' in html
        if values["logo_h"]:
            assert f'height="{values["logo_h"]}"' in html
        else:
            assert "height=" not in html
        assert f"Engineer, {values['entity_name']}" in html
        assert values["office_tel"] in html
        assert f'href="https://{values["website"]}/"' in html
        assert values["address"] in html
        assert f"No. {entity.company_number} | VAT {entity.vat_number}" in html
        assert "{{" not in html and "}}" not in html
        assert html.count("<table") == 1

    @pytest.mark.parametrize("code", ["JIT", "VALE"])
    def test_jit_and_vale_render_once_their_partial_is_verified(
        self, shipped_copy, code
    ):
        cfg_path, templates_dir, partials_dir = shipped_copy
        _verified_copy(cfg_path, partials_dir, code)
        cfg = load_config(cfg_path, templates_dir)
        assert cfg.entities[code].template_version == "2.0.0"
        tpl = load_template(cfg, code)
        html = render_signature(
            tpl, _person(ddi=DDI), cfg.entities[code], partial=cfg.partials[code]
        )
        assert f"{code.lower()}-logo-v1.png" in html
        assert cfg.entities[code].legal_name in html
        assert cfg.entities[code].company_number in html
        assert "{{" not in html


class TestOptionalRows:
    def test_ddi_and_mobile_rows_only_when_set(self, cfg):
        tpl = load_template(cfg, "OTB")
        entity, partial = cfg.entities["OTB"], cfg.partials["OTB"]
        neither = render_signature(tpl, _person(), entity, partial=partial)
        assert "DDI" not in neither and ">M</span>" not in neither
        assert "+44 (0)1949 861 514<br>" in neither  # office number then line break
        ddi_only = render_signature(tpl, _person(ddi=DDI), entity, partial=partial)
        assert f">DDI</span> {DDI}<br>" in ddi_only and ">M</span>" not in ddi_only
        mobile_only = render_signature(
            tpl, _person(mobile=MOBILE), entity, partial=partial
        )
        assert f">M</span> {MOBILE}<br>" in mobile_only and "DDI" not in mobile_only
        both = render_signature(
            tpl, _person(ddi=DDI, mobile=MOBILE), entity, partial=partial
        )
        assert f">DDI</span> {DDI}" in both and f">M</span> {MOBILE}<br>" in both
        assert both.index("DDI") < both.index(">M</span>")

    def test_ddi_comes_from_the_directory_work_phone(self, cfg):
        person = engine.extract_person(
            _oli(ddi=DDI, mobile=MOBILE), cfg.entities["OTB"]
        )
        assert person.ddi == DDI
        assert person.mobile == MOBILE
        person = engine.extract_person(_oli(ddi=None), cfg.entities["OTB"])
        assert person.ddi is None

    def test_primary_work_phone_wins(self, cfg):
        u = _oli(ddi=None)
        u["phones"] = [
            {"type": "work", "value": "+44 1"},
            {"type": "work", "value": "+44 2", "primary": True},
        ]
        assert engine.extract_person(u, cfg.entities["OTB"]).ddi == "+44 2"


# ---------------------------------------------------------------------------
# Overrides
# ---------------------------------------------------------------------------


class TestOverrides:
    def test_override_applies_to_the_named_send_as_only(self, cfg):
        plans = plan_for_user(
            cfg,
            _oli(),
            [
                send_as(OLI, primary=True),
                send_as(OLI_BIR),
                send_as(OLI_JIT),
            ],
        )
        by = {p.send_as_email: p for p in plans}
        assert by[OLI_BIR].overrides == {"title": "Managing Director"}
        assert "Managing Director, BIR Developments Ltd" in by[OLI_BIR].html
        assert "Group Managing Director" not in by[OLI_BIR].html
        assert by[OLI].overrides == {}
        assert "Group Managing Director, OTB Group Limited" in by[OLI].html
        assert by[OLI_JIT].overrides == {}
        assert "Group Managing Director" in by[OLI_JIT].html

    def test_other_users_on_the_same_domain_keep_their_directory_title(self, cfg):
        ben = user(BEN, "/04 BIR", full="Ben Example", title="Site Manager")
        plans = plan_for_user(cfg, ben, [send_as(BEN, primary=True)])
        assert plans[0].overrides == {}
        assert "Site Manager, BIR Developments Ltd" in plans[0].html

    def test_override_key_is_case_insensitive(self, cfg):
        plans = plan_for_user(cfg, _oli(), [send_as(OLI_BIR.upper(), primary=True)])
        assert plans[0].overrides == {"title": "Managing Director"}

    def test_override_can_set_every_allowed_field(self, shipped_copy):
        cfg_path, templates_dir, _ = shipped_copy
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        data["overrides"] = {
            OLI: {
                "name": "O. Blake",
                "title": "Chair",
                "mobile": "07700 1",
                "ddi": "+44 9",
            }
        }
        cfg_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        cfg = load_config(cfg_path, templates_dir)
        plan = plan_for_user(cfg, _oli(), [send_as(OLI, primary=True)])[0]
        assert "O. Blake" in plan.html and "Chair, OTB" in plan.html
        assert ">M</span> 07700 1" in plan.html and ">DDI</span> +44 9" in plan.html

    @pytest.mark.parametrize(
        "bad",
        [
            {"not-an-address": {"title": "x"}},
            {OLI: {"email": "x@y"}},
            {OLI: {"title": ""}},
            {OLI: {}},
            {OLI: "Chair"},
            ["list"],
        ],
    )
    def test_invalid_overrides_fail_config_load(self, shipped_copy, bad):
        cfg_path, templates_dir, _ = shipped_copy
        data = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
        data["overrides"] = bad
        cfg_path.write_text(yaml.safe_dump(data, sort_keys=False), encoding="utf-8")
        with pytest.raises(SignatureConfigError, match="overrides"):
            load_config(cfg_path, templates_dir)


# ---------------------------------------------------------------------------
# Snapshot against the live OTB signature
# ---------------------------------------------------------------------------


class TestLiveSnapshot:
    def test_otb_render_matches_the_live_signature(self, cfg):
        plan = plan_for_user(cfg, _oli(), [send_as(OLI, primary=True)])[0]
        assert plan.status == "planned", plan.reason
        assert plan.template_version == "2.0.0"
        assert _gmail_normalise(plan.html) == _gmail_normalise(LIVE_OTB_HTML)

    def test_only_the_proxy_prefix_differs_on_the_image_url(self, cfg):
        plan = plan_for_user(cfg, _oli(), [send_as(OLI, primary=True)])[0]
        live_src = re.search(r'<img src="([^"]+)"', LIVE_OTB_HTML).group(1)
        ours = re.search(r'<img src="([^"]+)"', plan.html).group(1)
        assert live_src.endswith("#" + ours)
        assert live_src.startswith("https://ci3.googleusercontent.com/")

    def test_normaliser_is_a_no_op_on_clean_html(self):
        clean = "<table><tr><td>x</td></tr></table>"
        assert _gmail_normalise(clean) == clean


# ---------------------------------------------------------------------------
# Pin rule and partial validation
# ---------------------------------------------------------------------------


class TestPinRule:
    @pytest.mark.parametrize("code", ["JIT", "VALE"])
    def test_cannot_pin_2_0_0_while_partial_is_unverified(self, shipped_copy, code):
        cfg_path, templates_dir, _ = shipped_copy
        _pin(cfg_path, code, "2.0.0")
        with pytest.raises(SignatureConfigError) as excinfo:
            load_config(cfg_path, templates_dir)
        message = str(excinfo.value)
        assert f"entity {code}" in message and "verified: false" in message

    def test_cannot_pin_2_0_0_without_a_partial_file(self, shipped_copy):
        cfg_path, templates_dir, partials_dir = shipped_copy
        (partials_dir / "OTB.yaml").unlink()
        with pytest.raises(SignatureConfigError, match="does not exist"):
            load_config(cfg_path, templates_dir)

    def test_unverifying_a_partial_refuses_the_2_0_0_pin(self, shipped_copy):
        cfg_path, templates_dir, partials_dir = shipped_copy
        _set_yaml(partials_dir / "OTB.yaml", verified=False)
        with pytest.raises(SignatureConfigError, match="verified: false"):
            load_config(cfg_path, templates_dir)
        # Back on 1.0.0 the unverified partial is simply unused.
        _pin(cfg_path, "OTB", "1.0.0")
        cfg = load_config(cfg_path, templates_dir)
        assert cfg.partials["OTB"].verified is False
        plan = plan_for_user(cfg, _oli(), [send_as(OLI, primary=True)])[0]
        assert plan.status == "planned" and plan.template_version == "1.0.0"

    def test_render_without_partial_names_the_file(self, cfg):
        tpl = load_template(cfg, "OTB")
        with pytest.raises(TemplateError, match="config/partials/OTB.yaml"):
            render_signature(tpl, _person(), cfg.entities["OTB"])

    @pytest.mark.parametrize(
        "changes, needle",
        [
            ({"office_tel": None}, "office_tel is required"),
            ({"logo_url": "http://cdn.otbgroup.co.uk/x.png"}, "https://"),
            ({"rule_rgb": "#c9a227"}, "rgb(r,g,b)"),
            ({"logo_w": 0}, "positive integer"),
            ({"logo_w": "wide"}, "positive integer"),
            ({"address": "TBC"}, "placeholder word"),
            ({"colour": "x"}, "unknown keys"),
            ({"entity": "BIR"}, "expected 'OTB'"),
            ({"verified": "yes"}, "verified must be true or false"),
        ],
    )
    def test_verified_partial_validation(self, shipped_copy, changes, needle):
        cfg_path, templates_dir, partials_dir = shipped_copy
        _set_yaml(partials_dir / "OTB.yaml", **changes)
        with pytest.raises(SignatureConfigError, match=re.escape(needle)):
            load_config(cfg_path, templates_dir)

    def test_unverified_partial_may_hold_nulls_and_placeholder_words(
        self, shipped_copy
    ):
        cfg_path, templates_dir, partials_dir = shipped_copy
        _set_yaml(partials_dir / "JIT.yaml", office_tel="TBC", logo_url=None)
        cfg = load_config(cfg_path, templates_dir)
        assert cfg.partials["JIT"].values["office_tel"] == "TBC"

    def test_logo_h_is_optional_even_when_verified(self, cfg):
        assert cfg.partials["BIR"].values["logo_h"] is None


class TestIfBlockAlias:
    def _tpl(self, cfg, signature: str):
        tpl = load_template(cfg, "OTB")
        return dataclasses.replace(tpl, signature_html=signature)

    def test_if_block_is_kept_and_removed(self, cfg):
        tpl = self._tpl(cfg, "<p>{{name}}{{#if ddi}} ddi={{ddi}}{{/if}}</p>")
        entity = cfg.entities["OTB"]
        assert (
            render_signature(tpl, _person(ddi="1"), entity, "")
            == "<p>Ada Lovelace ddi=1</p>"
        )
        assert render_signature(tpl, _person(), entity, "") == "<p>Ada Lovelace</p>"

    def test_unbalanced_if_is_refused(self, cfg):
        entity = cfg.entities["OTB"]
        with pytest.raises(TemplateError, match="unbalanced"):
            render_signature(
                self._tpl(cfg, "{{name}}{{#if ddi}}x"), _person(), entity, ""
            )
        with pytest.raises(TemplateError, match="unbalanced"):
            render_signature(self._tpl(cfg, "{{name}}x{{/if}}"), _person(), entity, "")

    def test_nested_if_is_refused(self, cfg):
        entity = cfg.entities["OTB"]
        tpl = self._tpl(cfg, "{{#if ddi}}{{#if mobile}}x{{/if}}{{/if}}")
        with pytest.raises(TemplateError):
            render_signature(tpl, _person(ddi="1", mobile="2"), entity, "")

    def test_old_block_syntax_still_works(self, cfg):
        tpl = self._tpl(cfg, "{{#mobile}}m={{mobile}}{{/mobile}}{{name}}")
        assert (
            render_signature(tpl, _person(mobile="7"), cfg.entities["OTB"], "")
            == "m=7Ada Lovelace"
        )


# ---------------------------------------------------------------------------
# Ledger, dry run, audit and preview carry the overrides and the version
# ---------------------------------------------------------------------------


@pytest.fixture
def world():
    directory = FakeDirectory(users=[_oli()])
    pool = FakeGmailPool()
    pool.add(OLI, [send_as(OLI, primary=True), send_as(OLI_BIR)])
    sheets = FakeSheets({"Ledger": [LEDGER_HEADER]})
    return directory, pool, sheets


class TestOverridesOnRecord:
    @pytest.mark.asyncio
    async def test_dry_run_row_names_the_override_and_the_version(self, cfg, world):
        directory, pool, sheets = world
        rows = await operations.apply_user(
            cfg,
            directory,
            OLI,
            actor=OLI,
            run_id="r1",
            dry_run=True,
            sheets=sheets,
            sheet_id="sheet",
            gmail_factory=pool.factory,
        )
        by = {r.send_as_email: r for r in rows}
        assert by[OLI_BIR].action == "would_apply"
        assert by[OLI_BIR].template_version == "2.0.0"
        assert "overrides title='Managing Director'" in by[OLI_BIR].reason
        assert "overrides" not in by[OLI].reason
        assert sheets.ledger_rows() == []

    @pytest.mark.asyncio
    async def test_live_ledger_rows_record_the_override(self, cfg, world):
        directory, pool, sheets = world
        rows = await operations.apply_user(
            cfg,
            directory,
            OLI,
            actor=OLI,
            run_id="r2",
            dry_run=False,
            confirm=True,
            sheets=sheets,
            sheet_id="sheet",
            gmail_factory=pool.factory,
        )
        assert {r.action for r in rows} == {"applied"}
        ledger = sheets.ledger_rows()
        bir_rows = [r for r in ledger if r["send_as_email"] == OLI_BIR]
        assert len(bir_rows) == 2  # pending + completed
        for row in bir_rows:
            assert json.loads(row["overrides"]) == {"title": "Managing Director"}
            assert row["template_version"] == "2.0.0"
        primary_rows = [r for r in ledger if r["send_as_email"] == OLI]
        assert all(r["overrides"] == "" for r in primary_rows)
        assert (
            "Managing Director, BIR Developments Ltd"
            in pool.mailboxes[OLI].send_as[OLI_BIR]["signature"]
        )

    @pytest.mark.asyncio
    async def test_audit_row_carries_the_override(self, cfg, world):
        directory, pool, sheets = world
        rows = await operations.audit_scope(
            cfg,
            directory,
            ou_path="/01 OTB",
            ledger_latest={},
            gmail_factory=pool.factory,
            audited_at="2026-11-06T09:00:00+00:00",
        )
        assert all(set(r) == set(AUDIT_HEADER) for r in rows)
        by = {r["send_as_email"]: r for r in rows}
        assert json.loads(by[OLI_BIR]["overrides"]) == {"title": "Managing Director"}
        assert by[OLI]["overrides"] == ""
        assert by[OLI_BIR]["expected_template_version"] == "2.0.0"

    @pytest.mark.asyncio
    async def test_preview_prints_version_used_ddi_and_override(
        self, cfg, world, monkeypatch
    ):
        from unittest.mock import AsyncMock

        from gsignatures import signature_tools

        directory, pool, sheets = world
        rt = operations.Runtime(
            config=cfg,
            directory=directory,
            sheets=None,
            sheet_id=None,
            gmail_factory=pool.factory,
        )
        monkeypatch.setattr(signature_tools, "build_runtime", lambda **kw: rt)
        monkeypatch.setattr(
            signature_tools, "_resolve_caller_email", AsyncMock(return_value=OLI)
        )
        monkeypatch.setattr(
            signature_tools,
            "_resolve_caller_auth_path",
            AsyncMock(return_value="fastmcp_oauth"),
        )
        out = await signature_tools.preview_email_signature(OLI, send_as_email=OLI_BIR)
        assert "Entity: BIR" in out
        assert "Template version used: 2.0.0 (brand partial BIR.yaml, verified)" in out
        assert f"DDI: {DDI}" in out
        assert "Overrides for this send-as: title='Managing Director'" in out
        assert "Managing Director, BIR Developments Ltd" in out
        assert "https://cdn.otbgroup.co.uk/bir/bir-logo-black-v1.png" in out

        out = await signature_tools.preview_email_signature(OLI)
        assert "Template version used: 2.0.0 (brand partial OTB.yaml, verified)" in out
        assert "Overrides for this send-as: (none)" in out
        assert "Group Managing Director, OTB Group Limited" in out

    @pytest.mark.asyncio
    async def test_preview_on_1_0_0_entity_says_so(self, cfg, monkeypatch):
        from unittest.mock import AsyncMock

        from gsignatures import signature_tools

        bob = user(
            "bob@jit-logistics.com", "/02 JIT", full="Bob Baker", title="Manager"
        )
        directory = FakeDirectory(users=[bob])
        pool = FakeGmailPool()
        pool.add(
            "bob@jit-logistics.com", [send_as("bob@jit-logistics.com", primary=True)]
        )
        rt = operations.Runtime(
            config=cfg,
            directory=directory,
            sheets=None,
            sheet_id=None,
            gmail_factory=pool.factory,
        )
        monkeypatch.setattr(signature_tools, "build_runtime", lambda **kw: rt)
        monkeypatch.setattr(
            signature_tools, "_resolve_caller_email", AsyncMock(return_value=OLI)
        )
        monkeypatch.setattr(
            signature_tools,
            "_resolve_caller_auth_path",
            AsyncMock(return_value="fastmcp_oauth"),
        )
        out = await signature_tools.preview_email_signature("bob@jit-logistics.com")
        assert (
            "Template version used: 1.0.0 (brand partial JIT.yaml NOT verified, unused)"
            in out
        )
