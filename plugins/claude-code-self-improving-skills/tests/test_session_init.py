"""Unit tests for session_init.py counting rules."""

import importlib

PROV = ("---\nname: {0}\ndescription: d\nmetadata:\n"
        "  provenance: self-improving-skills\n---\nbody\n")


def test_count_excludes_support_dir_skillmd(sandbox):
    import session_init
    importlib.reload(session_init)  # rebind SKILLS_DIR to the sandbox HOME
    sandbox.make_skill("learned-one", PROV.format("learned-one"))
    # a SKILL.md copy inside a support dir must NOT inflate the count
    ref = sandbox.skills / "learned-one" / "references"
    ref.mkdir()
    (ref / "SKILL.md").write_text(PROV.format("copy"), encoding="utf-8")
    # nor a non-provenance (user-authored) skill
    sandbox.make_skill("hand-made")
    assert session_init._count_learned_skills() == 1
