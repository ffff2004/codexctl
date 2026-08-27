Review the implementation changes in {{cwd}} on the Standards axis. Read the
repository's documented coding standards and check the implementation against
them.

This is round {{round}}. If round > 1, reuse your previous context and see if
previous problems are fixed.

Also apply this Fowler smell baseline. These are labelled heuristics, not hard
violations; documented repository standards override them, and tooling-enforced
issues should be skipped:

- Mysterious Name: a name does not reveal what it holds or does; rename it.
- Duplicated Code: the same logic shape appears more than once; extract it.
- Feature Envy: a method reaches into another object's data; move it there.
- Data Clumps: the same fields travel together; bundle them into a type.
- Primitive Obsession: a primitive stands in for a domain concept; model it.
- Repeated Switches: the same conditional cascade recurs; centralize or use
  polymorphism.
- Shotgun Surgery: one change scatters across files; gather it in one module.
- Divergent Change: one module changes for unrelated reasons; split it.
- Speculative Generality: unused flexibility was added; remove it.
- Message Chains: long navigation leaks object structure; hide the walk.
- Middle Man: a function mostly delegates; call the real target directly.
- Refused Bequest: an implementer ignores most inherited behavior; compose it.

Report, under 400 words, every documented-standard violation with the standard
file and rule, plus any baseline smell with the relevant file/hunk. Distinguish
hard violations from heuristic judgements.

Review this unstaged diff as part of the checkout:

{{unstaged_diff}}

Do not edit the checkout. You are the standards reviewer, review yourself, do not spawn sub-agents.
