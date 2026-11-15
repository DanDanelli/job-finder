"""BM25 pre-ranking: sorts pending jobs by textual match with the profile.

It only picks which jobs go to the LLM in each run (the top N); the score the user sees
is the LLM's. Hand-rolled so the Pi does not need numpy.

A relevance cutoff keeps jobs that have nothing to do with the profile (a nurse job for
a cloud architect) away from the LLM altogether. BM25 cannot do that: its scores depend
on the set of pending jobs, so there is no fixed threshold.
"""

import math
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass

from job_finder.domain import Job, Profile
from job_finder.text import key

K1 = 1.5
B = 0.75
TITLE_WEIGHT = 3  # the title is repeated so it weighs more than the description


# Function words in the languages of the jobs (pt, en, es). In a corpus that is mostly
# English, "de" looks like a rare, telling term; it is not.
STOPWORDS = frozenset(
    {
        "de",
        "da",
        "do",
        "das",
        "dos",
        "em",
        "na",
        "no",
        "nas",
        "nos",
        "para",
        "por",
        "com",
        "sem",
        "ao",
        "aos",
        "um",
        "uma",
        "ou",
        "and",
        "of",
        "the",
        "for",
        "in",
        "on",
        "at",
        "to",
        "with",
        "by",
        "an",
        "or",
        "from",
        "as",
        "del",
        "el",
        "la",
        "las",
        "los",
        "en",
        "con",
        "y",
    }
)


def tokens(text: str | None) -> list[str]:
    return [t for t in key(text or "").split() if len(t) > 1 and t not in STOPWORDS]


def profile_query(profile: Profile) -> list[str]:
    d = profile.data
    terms: list[str] = []
    for role in d.roles:
        terms += tokens(role) * 2
    for skill in d.skills:
        terms += tokens(skill.name) * skill.weight
    for q in profile.queries:
        if q.active:
            terms += tokens(q.term) * 2
    for extra in (*d.certifications, *d.industries):
        terms += tokens(extra)
    return terms


def job_document(job: Job) -> list[str]:
    return tokens(job.title) * TITLE_WEIGHT + tokens(job.description)


def bm25(query: Sequence[str], documents: Sequence[Sequence[str]]) -> list[float]:
    if not documents:
        return []
    n = len(documents)
    average = sum(len(d) for d in documents) / n or 1.0
    df: Counter[str] = Counter()
    for d in documents:
        df.update(set(d))
    query_weights = Counter(query)
    scores = []
    for d in documents:
        tf = Counter(d)
        total = 0.0
        for term, repeats in query_weights.items():
            f = tf.get(term)
            if not f:
                continue
            idf = math.log((n - df[term] + 0.5) / (df[term] + 0.5) + 1)
            total += repeats * idf * f * (K1 + 1) / (f + K1 * (1 - B + B * len(d) / average))
        scores.append(total)
    return scores


def prerank(profile: Profile, jobs: Sequence[Job]) -> list[float]:
    """BM25 score of each job, in the same order as `jobs`."""
    return bm25(profile_query(profile), [job_document(j) for j in jobs])


@dataclass(frozen=True)
class Relevance:
    """Share (0-1) of the profile's term weight found in the job's title and in the
    whole job. Each term weighs by its rarity (IDF) in a reference corpus, so common
    words ("de", "senior") count little."""

    title: float
    text: float


@dataclass(frozen=True)
class RelevanceCutoff:
    """A job reaches the LLM if its title or its whole text is relevant enough.

    Calibrated on 116 jobs scored by Claude for two very different profiles (cloud and
    fashion marketing): jobs scored 55+ had title >= 0.05 or text >= 0.14, while
    unrelated jobs stayed below title 0.013 and text 0.12.
    """

    min_title: float = 0.02
    min_text: float = 0.14

    def passes(self, r: Relevance) -> bool:
        return r.title >= self.min_title or r.text >= self.min_text


def idf_table(documents: Iterable[Iterable[str]]) -> Callable[[str], float]:
    n = 0
    df: Counter[str] = Counter()
    for d in documents:
        n += 1
        df.update(set(d))

    def idf(term: str) -> float:
        return math.log((n - df[term] + 0.5) / (df[term] + 0.5) + 1)

    return idf


def relevance(
    profile: Profile, jobs: Sequence[Job], idf: Callable[[str], float]
) -> list[Relevance]:
    """Relevance of each job, in the same order as `jobs`. A profile with no terms
    makes every job fully relevant."""
    weights = {t: c * idf(t) for t, c in Counter(profile_query(profile)).items()}
    total = sum(weights.values())
    if total <= 0:
        return [Relevance(1.0, 1.0) for _ in jobs]
    result = []
    for job in jobs:
        title, text = set(tokens(job.title)), set(job_document(job))
        result.append(
            Relevance(
                title=sum(w for t, w in weights.items() if t in title) / total,
                text=sum(w for t, w in weights.items() if t in text) / total,
            )
        )
    return result
