# Domain Docs

Agent-facing guidance for using qpi's domain documentation.

## Read Order

1. Read root `CONTEXT.md` for the domain vocabulary before naming concepts.
2. Read `docs/product/requirements.md` for current product behavior in the feature area.
3. Read ADRs in `docs/adr/` that touch the area being changed.

## Vocabulary

Use the terms from `CONTEXT.md` in explanations, issues, PRDs, and new docs.
Do not drift to avoided synonyms such as `task` for Purchase or `ticket` for
Support Topic unless you are explicitly referring to code identifiers or old
historical artifacts.

If the concept you need is not in `CONTEXT.md`, add a small follow-up note or
update the glossary in the same change that introduces the term.

## ADR Conflicts

If a proposed change contradicts an existing ADR, surface the conflict and either
update the ADR or record a new superseding decision. Do not silently override a
recorded decision in code or product docs.
