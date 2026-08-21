def test_ontology_constants_are_versioned_and_ranked():
    from src.skill_ontology import ONTOLOGY_VERSION, PROFICIENCY_RANK

    assert ONTOLOGY_VERSION == "skill-ontology-v1"
    assert PROFICIENCY_RANK == {
        "aware": 1,
        "working": 2,
        "advanced": 3,
        "expert": 4,
    }


def test_aliases_normalize_to_canonical_skill():
    from src.skill_ontology import normalize_skill

    assert normalize_skill(" K8s ") == {
        "name": "Kubernetes",
        "category": "cloud_native",
        "alias": "K8s",
    }
    assert normalize_skill("SpringBoot")["name"] == "Spring Boot"
    assert normalize_skill("Postgres")["name"] == "PostgreSQL"


def test_canonical_skill_normalizes_to_itself():
    from src.skill_ontology import normalize_skill

    assert normalize_skill("PostgreSQL") == {
        "name": "PostgreSQL",
        "category": "database",
        "alias": "PostgreSQL",
    }


def test_unknown_and_empty_skills_remain_safe_to_use():
    from src.skill_ontology import normalize_skill

    assert normalize_skill("  Rust  ") == {
        "name": "Rust",
        "category": "general",
        "alias": "Rust",
    }
    assert normalize_skill("")["name"] is None
    assert normalize_skill("   ")["name"] is None
    assert normalize_skill(None)["name"] is None


def test_relationships_cover_exact_related_prerequisite_and_none():
    from src.skill_ontology import skill_relationship

    assert skill_relationship("Java", "Java") == "exact"
    assert skill_relationship("K8s", "Kubernetes") == "exact"
    assert skill_relationship("MySQL", "PostgreSQL") == "related"
    assert skill_relationship("PostgreSQL", "MySQL") == "related"
    assert skill_relationship("Docker", "Kubernetes") == "prerequisite"
    assert skill_relationship("Kubernetes", "Docker") == "none"
    assert skill_relationship("Java", "PostgreSQL") == "none"
    assert skill_relationship("", "Kubernetes") == "none"


def test_prerequisite_chain_is_transitive_deduplicated_and_deterministic():
    from src.skill_ontology import prerequisite_chain

    expected = ["Linux", "Docker"]
    assert prerequisite_chain("Kubernetes") == expected
    assert prerequisite_chain("K8s") == expected
    assert prerequisite_chain("Kubernetes") == expected


def test_prerequisite_chain_does_not_loop(monkeypatch):
    from src import skill_ontology

    monkeypatch.setitem(skill_ontology.PREREQUISITES, "Linux", ("Kubernetes",))
    assert skill_ontology.prerequisite_chain("Kubernetes") == ["Linux", "Docker"]


def test_job_data_service_reexports_the_shared_catalog():
    from src.job_data_service import SKILL_CATALOG as job_catalog
    from src.skill_ontology import SKILL_CATALOG as shared_catalog

    assert job_catalog is shared_catalog
