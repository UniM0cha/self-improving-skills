"""Unit tests for skill_similarity.py — the deterministic duplicate/cluster logic."""

import skill_similarity as sim

PROV = ("---\nname: {0}\ndescription: {1}\nmetadata:\n"
        "  provenance: self-improving-skills\n---\n{2}\n")


def _facts(name, description, size=100, provenance=True, pinned=False, use_count=0):
    text = PROV.format(name, description, "x" * max(0, size - 60))
    if pinned:
        text = text.replace("metadata:", "pinned: true\nmetadata:")
    return sim.facts_from_text(name, text, {"use_count": use_count}) if provenance else \
        sim.facts_from_text(name, "---\nname: {0}\ndescription: {1}\n---\nbody\n".format(name, description))


def test_hyphen_and_stem_variants_collapse_to_one_token():
    assert sim.tokenize("live-ui-probe") == sim.tokenize("live ui probing") == sim.tokenize("Live UI probes")
    assert sim.tokenize("migration-version-ordering") == sim.tokenize("versioned-migration-ordering")


def test_stopwords_do_not_make_two_unrelated_skills_look_alike():
    a = sim.tokenize("Use this when you need to rotate a credential")
    b = sim.tokenize("Use this when you need to resize a window")
    assert sim.jaccard(a, b) < 0.4
    assert "use" not in a and "when" not in a and "this" not in a


def test_a_near_duplicate_name_is_reported_with_the_skill_it_duplicates():
    inventory = [_facts("verify-the-fix-actually-ran", "Prove the fix ran"),
                 _facts("zsh-argument-splitting", "zsh word splitting")]
    hit = sim.duplicate_of("verify-the-fix-ran", "Prove the fix ran", inventory)
    assert hit is not None
    other, score, kind = hit
    assert other == "verify-the-fix-actually-ran" and score >= 0.5 and kind == "name"


def test_a_description_match_counts_when_names_differ():
    inventory = [_facts("absent-element-diagnosis",
                        "Use this when UI automation reports a row or value missing and you are about to treat that absence as a real state")]
    hit = sim.duplicate_of("missing-cell-handling",
                           "Use this when automation reports a cell or value missing and you are about to treat the absence as real state",
                           inventory)
    assert hit is not None and hit[2] == "description"


def test_a_skill_is_never_its_own_duplicate():
    inventory = [_facts("only-skill", "Use this when the only skill applies")]
    assert sim.duplicate_of("only-skill", "Use this when the only skill applies", inventory) is None
    assert sim.duplicate_of("only-skill-two", "Use this when the only skill applies", inventory,
                            exclude={"only-skill"}) is None


def test_transcript_relevance_uses_containment_not_jaccard():
    inventory = [_facts("captcha-retry-budget", "Use this when a captcha keeps failing and retries are counted"),
                 _facts("window-resize", "Use this when the window is too narrow")]
    # A transcript far larger than any description: Jaccard would be ~0 for both,
    # containment still ranks the skill whose words the transcript mentions.
    transcript = ("captcha failing retries counted budget "
                  + " ".join("word{0}".format(i) for i in range(40_000)))
    ranked = sim.relevant_to_text(transcript, inventory, limit=5)
    assert [f.name for f in ranked][0] == "captcha-retry-budget"
    assert sim.jaccard(sim.tokenize(transcript), inventory[0].tokens) < 0.01


def test_clusters_split_when_the_size_sum_exceeds_the_cap():
    big = [_facts("live-ui-probe", "probe", size=60_000),
           _facts("live-ui-probing", "probing", size=60_000),
           _facts("live-ui-state-probing", "state probing", size=1_000)]
    groups = sim.clusters(big, max_chars=90_000)
    sizes = sorted(len(g["members"]) for g in groups)
    # 60k + 60k breaks the cap, so the two big ones cannot share a cluster
    assert all(not ({"live-ui-probe", "live-ui-probing"} <= set(g["members"])) for g in groups)
    assert sizes and max(sizes) == 2


def test_clusters_never_include_pinned_or_user_skills():
    inventory = [_facts("live-ui-probe", "probe"),
                 _facts("live-ui-probing", "probing", pinned=True),
                 _facts("live-ui-probes", "probes", provenance=False)]
    assert sim.clusters(inventory) == []


def test_cluster_ids_are_stable_across_calls():
    inventory = [_facts("alpha-beta-gamma", "one"), _facts("alpha-beta-delta", "two"),
                 _facts("xray-yankee-zulu", "three")]
    first = sim.clusters(inventory)
    second = sim.clusters(list(reversed(inventory)))
    assert first == second and len(first) == 1 and len(first[0]["id"]) == 8
    assert sim.find_group(first, first[0]["id"])["matched"] is True
    assert sim.find_group(first, "deadbeef") is None
    assert sim.closest_group(first, ["alpha-beta-gamma", "gone"])["matched"] is False


def test_over_cap_and_batches_follow_the_caps():
    inventory = [_facts("long-one", "L" * 301), _facts("long-two", "L" * 400, pinned=True),
                 _facts("short", "s"), _facts("long-three", "L" * 301), _facts("long-user", "L" * 500, provenance=False)]
    assert sim.over_cap(inventory) == ["long-one", "long-three"]
    assert sim.over_cap(inventory, exclude={"long-one"}) == ["long-three"]
    chunks = sim.batches(["b", "a", "c"], size=2)
    assert [c["members"] for c in chunks] == [["a", "b"], ["c"]]
    assert chunks[0]["id"] == sim.batches(["a", "b"], size=2)[0]["id"]


def test_inventory_ignores_the_archive_and_support_dirs(sandbox):
    sandbox.make_skill("learned-one", PROV.format("learned-one", "d one", "body"))
    ref = sandbox.skills / "learned-one" / "references"
    ref.mkdir()
    (ref / "SKILL.md").write_text(PROV.format("copy", "d", "body"), encoding="utf-8")
    archived = sandbox.skills / ".archive" / "old-one"
    archived.mkdir(parents=True)
    (archived / "SKILL.md").write_text(PROV.format("old-one", "d", "body"), encoding="utf-8")
    sandbox.make_skill("hand-made")
    inventory = sim.read_inventory(str(sandbox.skills), records={"learned-one": {"use_count": 2}})
    assert [f.name for f in inventory] == ["hand-made", "learned-one"]
    assert sim.learned_count(inventory) == 1
    learned = inventory[1]
    assert learned.use_count == 2 and learned.description == "d one" and learned.provenance
