#!/usr/bin/env python3
"""Deterministic similarity, clustering and batching over the learned-skill library.

The distiller used to be told, in prose, to "patch the closest existing skill
instead of adding a near-duplicate". With 385 skills on disk that instruction
produced 52 pairs whose names differ only by a suffix or a word order
(`migration-version-ordering` next to `versioned-migration-ordering`,
`live-ui-probe` next to `live-ui-probing` next to `live-ui-state-probing`).
Nothing in the pipeline ever compared a new skill to the ones already there.

This module is that comparison, and it is deliberately dumb:

  * tokens are hyphen/space-split words run through a crude stemmer, so that
    `probe`, `probing` and `probes` all become `prob` — the failure mode this
    catches is siblings that differ by inflection, not synonyms;
  * similarity is set overlap (Jaccard), and relevance of a skill to a
    transcript is containment (what share of the SKILL's tokens the transcript
    mentions) — Jaccard against a 200,000-character transcript is ~0 for every
    skill and ranks nothing;
  * clusters are connected components over "similar" pairs, walked in name
    order with size caps, so the same tree always yields the same clusters and
    the same 8-hex ids. The worker recomputes them from the tree instead of
    carrying members through the queue.

No I/O except `read_inventory`, and no dependency beyond `skill_paths`.
Everything here is cheap: reading 385 frontmatters and scoring every pair
measured at about a tenth of a second, so nothing is cached.
"""

import hashlib
import os
import re
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Iterable, List, Optional, Sequence, Set, Tuple

import skill_paths

# Words that carry no identity: articles, prepositions, the "Use this when"
# frame every description opens with. Two skills that share only these must
# not look alike.
STOPWORDS = frozenset("""
a an and are as at be been before after by for from in into is it its not of
on or over the this that these those to under until use used using via vs when
whenever where which while with within without you your yours we our also any
all some each every than then thus so such if else than about above across
against along around because between both but can could do does did done down
during either few has have having how may might more most much must need needs
no nor now off once only other out own same should still through too up very
was were what who whom why will would yet
""".split())
MIN_TOKEN_LEN = 2
_SPLIT_RE = re.compile(r"[^a-z0-9]+")
_FRONTMATTER_RE = re.compile(r"^---\s*\n(.*?)\n---", re.DOTALL)


def stem(word: str) -> str:
    """Fold inflections into one token. Crude on purpose: `probe`/`probing`/
    `probes` -> `prob`, `migration`/`migrate` -> `migrat`, `verify`/`verifies`
    -> `verifi`. Wrong roots are fine as long as they are wrong consistently."""
    w = word.lower()
    if len(w) <= 3:
        return w
    if w.endswith("ations") and len(w) > 7:
        w = w[:-6] + "at"
    elif w.endswith("ation") and len(w) > 6:
        w = w[:-5] + "at"
    elif w.endswith("ies") and len(w) > 4:
        w = w[:-3] + "i"
    else:
        for suffix in ("ing", "ed", "es", "s"):
            if w.endswith(suffix) and len(w) - len(suffix) >= 3:
                w = w[: -len(suffix)]
                break
    if w.endswith("e") and len(w) > 3:
        w = w[:-1]
    if w.endswith("y") and len(w) > 3:
        w = w[:-1] + "i"
    return w


def tokenize(text: str) -> Set[str]:
    """Identity tokens of a name or description: split, drop stopwords and
    one-character fragments, stem, drop what the stemmer emptied."""
    out: Set[str] = set()
    for raw in _SPLIT_RE.split(str(text or "").lower()):
        if len(raw) < MIN_TOKEN_LEN or raw in STOPWORDS:
            continue
        token = stem(raw)
        if len(token) >= MIN_TOKEN_LEN and token not in STOPWORDS:
            out.add(token)
    return out


def jaccard(a: Iterable[str], b: Iterable[str]) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / float(len(sa | sb))


def containment(small: Iterable[str], large: Iterable[str]) -> float:
    """Share of `small` that appears in `large`. Asymmetric on purpose: this is
    how a few dozen skill tokens are scored against a whole transcript."""
    ss, sl = set(small), set(large)
    if not ss:
        return 0.0
    return len(ss & sl) / float(len(ss))


@dataclass(frozen=True)
class SkillFacts:
    name: str
    description: str
    size: int
    provenance: bool
    pinned: bool = False
    use_count: int = 0
    view_count: int = 0
    last_used_at: Optional[str] = None
    name_tokens: FrozenSet[str] = field(default_factory=frozenset)
    desc_tokens: FrozenSet[str] = field(default_factory=frozenset)

    @property
    def tokens(self) -> FrozenSet[str]:
        return self.name_tokens | self.desc_tokens


def _frontmatter(text: str) -> str:
    m = _FRONTMATTER_RE.match(text or "")
    return m.group(1) if m else ""


def frontmatter_description(text: str) -> str:
    """The `description` scalar of a SKILL.md, folded to one line. Handles the
    bare `description: ...` form and the `>`/`|` block forms the library uses."""
    lines = _frontmatter(text).splitlines()
    for i, line in enumerate(lines):
        m = re.match(r"^description\s*:\s*(.*)$", line)
        if not m:
            continue
        value = m.group(1).strip()
        if value in (">", "|", ">-", "|-", ""):
            block = []
            for cont in lines[i + 1:]:
                if cont and not cont[0].isspace():
                    break
                block.append(cont.strip())
            value = " ".join(part for part in block if part)
        if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
            value = value[1:-1]
        return value
    return ""


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def facts_from_text(name: str, text: str, record: Optional[dict] = None) -> SkillFacts:
    fm = _frontmatter(text)
    description = frontmatter_description(text)
    rec = record if isinstance(record, dict) else {}
    pinned = bool(rec.get("pinned")) or bool(re.search(r"^\s*pinned\s*:\s*true\b", fm, re.I | re.M))
    return SkillFacts(
        name=name,
        description=description,
        size=len(text or ""),
        provenance="provenance: self-improving-skills" in fm,
        pinned=pinned,
        use_count=_int(rec.get("use_count")),
        view_count=_int(rec.get("view_count")),
        last_used_at=rec.get("last_used_at") or None,
        name_tokens=frozenset(tokenize(name)),
        desc_tokens=frozenset(tokenize(description)),
    )


def read_inventory(root: Optional[str] = None, *, records: Optional[dict] = None) -> List[SkillFacts]:
    """Every `<root>/<name>/SKILL.md`, in name order. `.archive/` and other
    dot-directories are skipped, and so is a SKILL.md nested under a support
    directory (references/, templates/): only direct children are skills."""
    base = root or skill_paths.personal_skills_root()
    facts: List[SkillFacts] = []
    try:
        entries = sorted(os.listdir(base))
    except OSError:
        return facts
    recs = records or {}
    for entry in entries:
        if entry.startswith("."):
            continue
        path = os.path.join(base, entry, "SKILL.md")
        if not os.path.isfile(path):
            continue
        try:
            with open(path, encoding="utf-8", errors="ignore") as fh:
                text = fh.read()
        except OSError:
            continue
        facts.append(facts_from_text(entry, text, recs.get(entry)))
    return facts


def learned_count(inventory: Sequence[SkillFacts]) -> int:
    return sum(1 for f in inventory if f.provenance)


def _similar(a: SkillFacts, b: SkillFacts, name_threshold: float, desc_threshold: float) -> Optional[Tuple[float, str]]:
    jn = jaccard(a.name_tokens, b.name_tokens)
    if jn >= name_threshold:
        return jn, "name"
    jd = jaccard(a.desc_tokens, b.desc_tokens)
    if jd >= desc_threshold:
        return jd, "description"
    return None


def duplicate_of(name: str, description: str, inventory: Sequence[SkillFacts], *,
                 exclude: Iterable[str] = (), name_threshold: float = 0.5,
                 desc_threshold: float = 0.4) -> Optional[Tuple[str, float, str]]:
    """The existing skill a (name, description) pair duplicates, if any:
    `(other_name, score, "name"|"description")` for the strongest match. The
    skill itself is never its own duplicate; `exclude` removes further names."""
    probe = SkillFacts(name=name, description=description, size=0, provenance=False,
                       name_tokens=frozenset(tokenize(name)),
                       desc_tokens=frozenset(tokenize(description)))
    skip = {name} | set(exclude)
    best: Optional[Tuple[str, float, str]] = None
    for other in inventory:
        if other.name in skip:
            continue
        hit = _similar(probe, other, name_threshold, desc_threshold)
        if hit and (best is None or hit[0] > best[1]):
            best = (other.name, round(hit[0], 2), hit[1])
    return best


def relevant_to_text(text: str, inventory: Sequence[SkillFacts], *, limit: int = 15) -> List[SkillFacts]:
    """The skills a transcript talks about most, by containment of each
    skill's tokens in the transcript. Ties break toward the skill that has
    actually been used, then by name, so the list is stable."""
    mentioned = tokenize(text)
    scored = []
    for facts in inventory:
        score = containment(facts.tokens, mentioned)
        if score > 0:
            scored.append((score, facts.use_count, facts))
    scored.sort(key=lambda item: (-item[0], -item[1], item[2].name))
    return [item[2] for item in scored[: max(0, int(limit))]]


def _group_id(members: Sequence[str]) -> str:
    return hashlib.sha256(" ".join(sorted(members)).encode("utf-8")).hexdigest()[:8]


def clusters(inventory: Sequence[SkillFacts], *, name_threshold: float = 0.5,
             desc_threshold: float = 0.4, max_chars: int = 90_000,
             max_members: int = 6) -> List[Dict]:
    """Connected components of similar learned skills, each small enough for
    one consolidation job. Pairs are walked in name order and an edge is
    dropped when joining would push the component over `max_chars` of SKILL.md
    or `max_members` skills — first pair wins, which the sort makes
    deterministic. Pinned and user-authored skills are never clustered."""
    eligible = sorted((f for f in inventory if f.provenance and not f.pinned), key=lambda f: f.name)
    parent = {f.name: f.name for f in eligible}
    own_size = {f.name: f.size for f in eligible}
    size = dict(own_size)  # per root: bytes of the whole component
    count = {f.name: 1 for f in eligible}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    for i, a in enumerate(eligible):
        for b in eligible[i + 1:]:
            if _similar(a, b, name_threshold, desc_threshold) is None:
                continue
            ra, rb = find(a.name), find(b.name)
            if ra == rb:
                continue
            if size[ra] + size[rb] > max_chars or count[ra] + count[rb] > max_members:
                continue
            parent[rb] = ra
            size[ra] += size[rb]
            count[ra] += count[rb]

    groups: Dict[str, List[str]] = {}
    for f in eligible:
        groups.setdefault(find(f.name), []).append(f.name)
    result = []
    for members in groups.values():
        if len(members) < 2:
            continue
        members = sorted(members)
        result.append({"id": _group_id(members), "members": members,
                       "size": sum(own_size[m] for m in members)})
    result.sort(key=lambda g: g["members"][0])
    return result


def over_cap(inventory: Sequence[SkillFacts], *, max_desc: int = 300,
             exclude: Iterable[str] = ()) -> List[str]:
    """Learned, unpinned skills whose description exceeds `max_desc`
    characters, minus `exclude` — the compress-job candidates."""
    skip = set(exclude)
    return sorted(f.name for f in inventory
                  if f.provenance and not f.pinned and f.name not in skip
                  and len(f.description) > max_desc)


def batches(names: Sequence[str], *, size: int = 20) -> List[Dict]:
    """Chunk `names` (already sorted) into groups of `size`, each with the same
    kind of stable id a cluster carries."""
    ordered = sorted(names)
    step = max(1, int(size))
    return [{"id": _group_id(ordered[i:i + step]), "members": ordered[i:i + step]}
            for i in range(0, len(ordered), step)]


def find_group(groups: Sequence[Dict], group_id: str) -> Optional[Dict]:
    """The group carrying `group_id`, or the one overlapping it most by
    member names when the tree changed since the id was minted (None if no
    group shares a member). Callers say which happened via `matched`."""
    for group in groups:
        if group.get("id") == group_id:
            return dict(group, matched=True)
    return None


def closest_group(groups: Sequence[Dict], members: Iterable[str]) -> Optional[Dict]:
    wanted = set(members)
    best, best_overlap = None, 0
    for group in groups:
        overlap = len(wanted & set(group.get("members") or ()))
        if overlap > best_overlap:
            best, best_overlap = group, overlap
    return dict(best, matched=False) if best else None
