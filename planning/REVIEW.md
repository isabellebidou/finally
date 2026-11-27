# Review Feedback

Reviewed the changes since the last commit (`.gitignore`, `planning/PLAN.md`, and the new `.claude/` files).

## Findings

1. **[P1] The plan-review agent points to a file path that does not exist on case-sensitive systems.** In `.claude/agents/codex-reviewer.md`, the command asks Codex to review `planning/Plan.md`, but the file is `planning/PLAN.md`. Change the command to use the repository's exact capitalization so the delegated review can open the document reliably.

2. **[P1] The documentation review command targets a missing document.** `.claude/commands/doc-review.md` asks the agent to review a planning document called `#ARGUMENTS`, but there is no such file in `planning/`. Replace this reference with the intended existing document or add the missing document; otherwise the command cannot complete as written.

3. **[P2] The revised plan and README now give conflicting setup instructions.** `planning/PLAN.md` says `OPENROUTER_API_KEY` is optional and chat alone is unavailable without it, and says SQLite initialization occurs in FastAPI lifespan. `README.md` still marks the key as required and describes lazy database initialization. Update the README (or revise the plan) so users receive one consistent set of setup expectations.

4. **[P2] `.env.example` is described as committed, but is absent from the repository.** `planning/PLAN.md` says it documents all variables, and `README.md` instructs users to copy it, but there is no `.env.example` in the tree. Add the example file (including `DB_PATH`) or update the setup instructions; as written, the documented quick start fails at its first copy command.

## Questions

- Should the `codex-reviewer` and `change-reviewer` agents invoke `codex exec` themselves, or should they provide review instructions for the active agent? As written, they require a nested external CLI invocation and prohibit the current agent from reviewing directly, which makes their behavior dependent on that CLI being installed and authenticated.
- In `planning/PLAN.md`, the example still labels `OPENROUTER_API_KEY` as required even though the behavior section says the app works without it and only chat is unavailable. Should the key be optional in the example as well as in the README?
- The plan now documents `DB_PATH`, but the README's environment-variable table does not mention it. Should it be included in the user-facing setup documentation?
