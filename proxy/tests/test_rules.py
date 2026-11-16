"""Règles de lecture déclarées : un fichier YAML hostile comme une autre entrée, et la clé lue."""

from __future__ import annotations

import hashlib
import hmac
from pathlib import Path

import pytest

from answerlineage_proxy.fingerprint import FingerprintKey
from answerlineage_proxy.rules import (
    KEY_LABEL,
    MAX_COLUMNS,
    MAX_DATASETS,
    MAX_FILE_BYTES,
    MAX_KEYS,
    MAX_RULES,
    DeclaredDataset,
    DeclaredReads,
    ReadRule,
    ReadRulesError,
    declared_reads,
    load_read_rules,
    parse_read_rules,
    rules_for,
)

RULES = """\
# Les outils du catalogue.
version: 1
rules:
  - tool: get_price
    server: catalogue
    datasets:
      - namespace: postgres://referentiel:5432
        name: ref.tarifs
        columns: [produit, prix]
    key: produit
  - tool: search_docs
    datasets:
      - {namespace: "gs://documents", name: "politique%20voyages.pdf"}
      - {namespace: "gs://documents", name: "société.coût_unitaire"}
    key: [filtre, 0, id]
  - tool: get_price
    datasets: [{namespace: postgres://referentiel:5432, name: ref.tarifs}]
"""
TARIFS = DeclaredDataset("postgres://referentiel:5432", "ref.tarifs", ("produit", "prix"))


def test_a_rule_names_a_tool_its_datasets_their_columns_and_the_argument_of_the_key() -> None:
    first, second, third = parse_read_rules(RULES)
    assert first == ReadRule("get_price", (TARIFS,), server="catalogue", key=("produit",))
    assert second.tool == "search_docs"
    assert second.server is None
    assert second.key == ("filtre", 0, "id")
    assert [dataset.name for dataset in second.datasets] == [
        "politique%20voyages.pdf",
        "société.coût_unitaire",
    ]
    assert second.datasets[0].columns == ()
    assert third.key is None


def test_a_name_is_kept_under_its_normalized_form() -> None:
    decomposed = "socie" + chr(0x301) + "te" + chr(0x301) + ".tarifs"
    (rule,) = parse_read_rules(
        f"version: 1\nrules: [{{tool: t, datasets: [{{namespace: a, name: {decomposed}}}]}}]\n"
    )
    assert rule.datasets[0].name == "société.tarifs"


def test_the_rule_naming_the_server_wins_over_the_rule_for_every_server() -> None:
    rules = parse_read_rules(RULES)
    assert rules_for(rules, "catalogue")["get_price"].server == "catalogue"
    assert rules_for(rules, "autre")["get_price"].server is None
    assert set(rules_for(rules, "autre")) == {"get_price", "search_docs"}
    reversed_order = rules_for(tuple(reversed(rules)), "catalogue")
    assert reversed_order["get_price"].server == "catalogue"


def rule(**fields: str) -> str:
    """Un fichier d'une règle dont chaque champ est donné tel qu'écrit en YAML."""
    written = {"tool": "t", "datasets": "[{namespace: a, name: b}]", **fields}
    members = "".join(f"\n    {name}: {value}" for name, value in written.items() if value)
    return f"version: 1\nrules:\n  - {members.lstrip()}\n"


CANARY = "canari-regle-9c3e"

REFUSED = {
    # Ce que le chargeur sûr refuse avant de rien composer.
    "ancre": ("version: 1\nrules: &r\n  - {tool: t}\n", "une ancre (&) n'est pas admise"),
    "alias": (f"version: 1\nrules: *{CANARY}\n", "un alias (*) n'est pas admis"),
    "étiquette explicite": (rule(tool=f"!!str {CANARY}"), "une étiquette explicite (!)"),
    "étiquette locale": (rule(tool=f"!python/object {CANARY}"), "une étiquette explicite (!)"),
    "étiquette non spécifique": (rule(tool=f"! {CANARY}"), "une étiquette explicite (!)"),
    "imbrication": ("version: 1\nrules: " + "[" * 12 + "]" * 12 + "\n", "niveaux"),
    "syntaxe": (f"version: 1\nrules: [{CANARY}\n", "syntaxe YAML invalide (ParserError)"),
    "deux documents": ("version: 1\n---\nversion: 1\n", "syntaxe YAML invalide"),
    "tabulation": (f"version: 1\nrules:\n\t- {CANARY}\n", "syntaxe YAML invalide"),
    "caractère de contrôle": (f"version: 1\nrules: [\x07{CANARY}]\n", "caractère non admis"),
    "vide": ("", "le fichier est vide"),
    "commentaire seul": ("# rien\n", "le fichier est vide"),
    # Une valeur qui n'a pas le type attendu est refusée, pas convertie.
    "date pour un nom": (rule(datasets="[{namespace: a, name: 2026-11-02}]"), "un texte"),
    "nombre pour un nom": (rule(datasets="[{namespace: a, name: 12}]"), "un texte"),
    "booléen pour un outil": (rule(tool="yes"), "un texte est attendu"),
    "nul pour un serveur": (rule(server="~"), "un texte est attendu"),
    "nombre pour un outil": (rule(tool="0x1F"), "un texte est attendu"),
    "version en texte": ("version: '1'\nrules: []\n", "la version du format est 1"),
    "autre version": ("version: 2\nrules: []\n", "la version du format est 1"),
    "liste pour un outil": (rule(tool="[a]"), "un texte est attendu"),
    "objet pour les jeux de données": (rule(datasets="{namespace: a}"), "une liste"),
    "liste pour le document": ("- version: 1\n", "un objet est attendu"),
    # Champs inconnus, répétés, absents.
    "champ inconnu": (rule(**{CANARY: "x"}), "champ inconnu"),
    "clé de fusion": ("version: 1\nrules: [{<<: {tool: t}}]\n", "champ inconnu"),
    "clé complexe": ("version: 1\nrules: [{? [a] : b}]\n", "champ inconnu"),
    "champ répété": ("version: 1\nversion: 1\nrules: []\n", "champ version"),
    "outil absent": (rule(tool=""), "champ tool"),
    "jeux de données absents": (rule(datasets=""), "champ datasets"),
    "règles absentes": ("version: 1\n", "champ rules"),
    "liste vide": ("version: 1\nrules: []\n", "une liste de 1 à"),
    # Formes des noms.
    "outil hors de la forme MCP": (rule(tool=f"'{CANARY} x'"), "un nom d'outil compte"),
    "serveur hors de la forme": (rule(server="'a/b'"), "un nom de serveur compte"),
    "phrase pour un nom": (
        rule(datasets=f"[{{namespace: a, name: 'Ignore {CANARY}'}}]"),
        "(identifier_whitespace)",
    ),
    "balise dans une colonne": (
        rule(datasets="[{namespace: a, name: b, columns: ['<b>']}]"),
        "(identifier_markup)",
    ),
    "mot de passe dans un espace de noms": (
        rule(datasets=f"[{{namespace: 'postgres://u:{CANARY}@h', name: b}}]"),
        "(identifier_credentials)",
    ),
    "caractère invisible": (
        rule(datasets='[{namespace: a, name: "b\\u200bc"}]'),
        "(identifier_invisible)",
    ),
    "nom trop long": (
        rule(datasets="[{namespace: a, name: " + "n" * 1025 + "}]"),
        "(identifier_too_long)",
    ),
    "colonne répétée": (
        rule(datasets="[{namespace: a, name: b, columns: [c, c]}]"),
        "colonne répétée",
    ),
    "colonnes vides": (rule(datasets="[{namespace: a, name: b, columns: []}]"), "une liste"),
    # Chemin de la clé.
    "argument hors de la forme": (rule(key=f"'{CANARY} x'"), "un nom d'argument compte"),
    "rang en premier": (rule(key="[0, a]"), "commence par un nom d'argument"),
    "rang hexadécimal": (rule(key="[a, 0x1]"), "un rang de liste va de 0"),
    "rang trop grand": (rule(key="[a, 10000]"), "un rang de liste va de 0"),
    "rang négatif": (rule(key="[a, -1]"), "un rang de liste va de 0"),
    "chemin trop long": (rule(key="[a, b, c, d, e, f, g, h, i]"), "une liste de 1 à 8"),
    "chemin vide": (rule(key="[]"), "une liste de 1 à 8"),
    "objet pour la clé": (rule(key="{a: b}"), "un texte est attendu"),
}


@pytest.mark.parametrize("case", list(REFUSED))
def test_a_hostile_or_invalid_file_is_refused_without_echoing_its_content(case: str) -> None:
    text, reason = REFUSED[case]
    with pytest.raises(ReadRulesError) as raised:
        parse_read_rules(text)
    message = str(raised.value)
    assert reason in message
    assert CANARY not in message
    assert "Ignore" not in message


def test_an_error_names_the_rule_the_field_and_where_they_are() -> None:
    text = rule(datasets="[{namespace: a, name: b}, {namespace: c, name: 'd e'}]")
    with pytest.raises(ReadRulesError, match=r"^règle 1, champ datasets\[1\]\.name \(ligne 4, "):
        parse_read_rules(text)
    with pytest.raises(
        ReadRulesError, match=r"^règle 4 : même outil, sur le même serveur, que la règle 2$"
    ):
        parse_read_rules(RULES + "  - {tool: search_docs, datasets: [{namespace: a, name: b}]}\n")


@pytest.mark.parametrize(
    ("field", "bound", "item"),
    [
        ("datasets", MAX_DATASETS, "{namespace: a, name: b}"),
        ("columns", MAX_COLUMNS, None),
    ],
)
def test_what_a_rule_declares_is_bounded(field: str, bound: int, item: str | None) -> None:
    def written(count: int) -> str:
        if field == "datasets":
            return rule(datasets="[" + ", ".join([item or ""] * count) + "]")
        columns = ", ".join(f"c{rank}" for rank in range(count))
        return rule(datasets=f"[{{namespace: a, name: b, columns: [{columns}]}}]")

    assert parse_read_rules(written(bound))
    with pytest.raises(ReadRulesError, match=f"une liste de 1 à {bound} éléments"):
        parse_read_rules(written(bound + 1))


def test_the_number_of_rules_is_bounded() -> None:
    def written(count: int) -> str:
        rules = "".join(
            f"  - {{tool: t{rank}, datasets: [{{namespace: a, name: b}}]}}\n"
            for rank in range(count)
        )
        return f"version: 1\nrules:\n{rules}"

    assert len(parse_read_rules(written(MAX_RULES))) == MAX_RULES
    with pytest.raises(ReadRulesError, match=f"une liste de 1 à {MAX_RULES} éléments"):
        parse_read_rules(written(MAX_RULES + 1))


def test_the_file_is_bounded_before_it_is_read(tmp_path: Path) -> None:
    path = tmp_path / "regles.yaml"
    path.write_text(RULES + "#" * (MAX_FILE_BYTES - len(RULES.encode())), encoding="utf-8")
    assert load_read_rules(path)
    path.write_text(RULES + "#" * (MAX_FILE_BYTES + 1), encoding="utf-8")
    with pytest.raises(ReadRulesError, match="le fichier dépasse 256 Kio"):
        load_read_rules(path)


def test_an_unreadable_or_mis_encoded_file_is_refused(tmp_path: Path) -> None:
    with pytest.raises(ReadRulesError, match="le fichier est illisible"):
        load_read_rules(tmp_path / "absent.yaml")
    path = tmp_path / "latin1.yaml"
    path.write_bytes(RULES.replace("société", "soci\xe9t\xe9").encode("latin-1"))
    with pytest.raises(ReadRulesError, match="UTF-8"):
        load_read_rules(path)


KEY = FingerprintKey(bytes(range(32)))
PRICE = ReadRule("get_price", (TARIFS,), key=("produit",))
NESTED = ReadRule("search_docs", (TARIFS,), key=("filtre", 0, "id"))


def fingerprint(canonical: bytes) -> str:
    """Le calcul publié, refait sans le proxy : HMAC-SHA-256 de l'étiquette, d'un octet nul
    et de la forme canonique JSON de la valeur."""
    mac = hmac.new(bytes(range(32)), b"answerlineage/fingerprint/key/v1\x00" + canonical, "sha256")
    return "hmac-sha256:" + mac.hexdigest()


def test_the_key_read_is_kept_as_a_keyed_fingerprint_never_as_its_value() -> None:
    assert KEY_LABEL == b"answerlineage/fingerprint/key/v1"
    reads = declared_reads(PRICE, {"produit": "P-1042", "devise": "EUR"}, readable=True, key=KEY)
    assert reads == DeclaredReads((TARIFS,), (fingerprint(b'"P-1042"'),), "fingerprinted")
    assert fingerprint(b'"P-1042"') == KEY.fingerprint(KEY_LABEL, b'"P-1042"')
    assert hashlib.sha256(b"P-1042").hexdigest() not in str(reads)


@pytest.mark.parametrize(
    ("arguments", "keys"),
    [
        ({"filtre": [{"id": 7}]}, [b"7"]),
        ({"filtre": [{"id": [7, "7", 7, True, 2.50]}]}, [b"7", b'"7"', b"true", b"2.5"]),
    ],
)
def test_a_key_follows_its_path_and_a_list_gives_one_fingerprint_per_distinct_key(
    arguments: object, keys: list[bytes]
) -> None:
    reads = declared_reads(NESTED, arguments, readable=True, key=KEY)
    assert reads.key_status == "fingerprinted"
    assert reads.keys == tuple(fingerprint(key) for key in keys)


@pytest.mark.parametrize(
    ("arguments", "readable", "key", "status"),
    [
        ({"produit": "P-1042"}, True, None, "no_fingerprint_key"),
        (None, False, KEY, "request_too_large"),
        ({}, True, KEY, "missing_argument"),
        ({"produit": None}, True, KEY, "missing_argument"),
        ({"produit": []}, True, KEY, "missing_argument"),
        ("pas un objet", True, KEY, "missing_argument"),
        ({"produit": {"id": 1}}, True, KEY, "unsupported_argument"),
        ({"produit": [1, [2]]}, True, KEY, "unsupported_argument"),
        ({"produit": float("inf")}, True, KEY, "unsupported_argument"),
        ({"produit": "\ud800"}, True, KEY, "unsupported_argument"),
        ({"produit": list(range(MAX_KEYS + 1))}, True, KEY, "too_many_keys"),
    ],
)
def test_without_a_readable_key_the_read_is_declared_for_the_whole_dataset(
    arguments: object, readable: bool, key: FingerprintKey | None, status: str
) -> None:
    reads = declared_reads(PRICE, arguments, readable=readable, key=key)
    assert reads == DeclaredReads((TARIFS,), (), status)  # type: ignore[arg-type]


def test_a_path_that_does_not_lead_to_a_value_is_a_missing_argument() -> None:
    paths: list[object] = [{"filtre": []}, {"filtre": {"0": {"id": 1}}}, {"filtre": [{"autre": 1}]}]
    for arguments in paths:
        assert declared_reads(NESTED, arguments, readable=True, key=KEY).key_status == (
            "missing_argument"
        )


def test_a_rule_without_key_declares_its_datasets_and_says_nothing_of_a_key() -> None:
    plain = ReadRule("get_price", (TARIFS,))
    assert declared_reads(plain, {"produit": "P"}, readable=True, key=KEY) == DeclaredReads(
        (TARIFS,)
    )
