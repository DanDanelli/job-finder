"""BM25 pre-ranking: sorts pending jobs by textual match with the profile.

It only picks which jobs go to the LLM in each run (the top N); the score the user sees
is the LLM's. Hand-rolled so the Pi does not need numpy.
"""

import math
from collections import Counter
from collections.abc import Sequence

from job_finder.domain import Job, Profile
from job_finder.text import key

K1 = 1.5
B = 0.75
TITLE_WEIGHT = 3  # the title is repeated so it weighs more than the description


def tokens(text: str | None) -> list[str]:
    return [t for t in key(text or "").split() if len(t) > 1]


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
