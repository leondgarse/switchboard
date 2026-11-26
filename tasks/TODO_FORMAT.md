# TODO.md format

One convention for every `TODO.md` in the workspace (and for `tasks/tasks.md`), so switchboard's Tasks tab can
read them and sessions can keep them tidy. Add this line to a repo's `CLAUDE.md`:

    Keep TODO.md in the format of ~/workspace/switchboard/tasks/TODO_FORMAT.md.

## Shape

```markdown
# <Project>: work board

One or two lines: what this board covers, and where measurements and decisions go (not here).

## In progress
- [~] **Short title.** One or two sentences of detail; file paths in backticks. (since 2026-11-24)

## Next
- [ ] **Short title.** Detail.

## Later
- [ ] **Short title.** Detail.

## Done
- [x] **Short title.** What shipped, and where it's recorded. (2026-11-23)

## Rejected
- [-] **Short title.** The number or reason that killed it. (2026-11-20)
```

## Rules

- **One item per line**, starting `- [ ]`, `- [~]`, `- [x]` or `- [-]` (open, in progress, done, dropped).
  Continuation lines are indented under the item.
- **Bold title first**, ending with a full stop, then the detail. The title alone should say what to do.
- **Move items between sections; don't delete them.** Done and Rejected are the record, so a later session
  doesn't re-open a settled question.
- **Dates** in `(YYYY-MM-DD)` at the end of a line when they matter.
- **Sections** are only these five, in this order; leave one out if it's empty.
- **Not here:** measured results (they belong in the repo's logs), long plans (link a `PLAN.md`), status essays.
