# Job search in Claude Cowork

A version of Job Finder with no code and no server: four Markdown files in a folder and
Claude Cowork with its connectors and web tools. Claude searches when you ask (or on a
schedule), scores each job against your profile, keeps your decisions and learns your
taste from them.

Download the folder:
[job-search-cowork.zip](https://github.com/DanDanelli/job-finder/releases/latest/download/job-search-cowork.zip).
Em português, com o passo a passo para quem não é de TI: [LEIA-ME.md](LEIA-ME.md).

## Files

- `CLAUDE.md`: what Claude does and how (sources, filters, scoring, history, weekly
  review and cleanup).
- `profile.md`: you. A filled-in example; Claude rewrites it from your CV.
- `jobs.md`: the jobs Claude scored and what you decided.
- `rules.md`: what Claude learned from your decisions, once you approve it.

## Setup

1. Download and unzip the folder (or copy `cowork/`) somewhere of your own (your data lives in it, so keep it out of
   any public repository).
2. In Claude, add the connectors you want. Indeed has an official connector (in beta)
   that covers every kind of job; Cowork's web search and web fetch cover the rest.
3. Create a Cowork project on the folder. Cowork should pick up `CLAUDE.md`; if it does
   not, paste its contents into the project's instructions.
4. Say "create my profile" and attach your CV. Review what Claude writes.
5. Say "search jobs". Tell Claude what you think of the results ("save J3, dismiss J5,
   it's an agency"); the reasons are what it learns from.
6. Optionally schedule "search jobs" in Cowork (every morning, say) and "weekly review"
   on Mondays. The weekly review also keeps `jobs.md` small.

## What to expect

- Filters and deduplication are Claude's judgment, not code: almost always right, not
  guaranteed.
- Coverage depends on the connectors and on what web search finds. LinkedIn and Gupy
  have no official connector; Claude reaches their public pages through web search.
- Each search uses part of your Claude plan's usage, more with more searches and
  sources.
