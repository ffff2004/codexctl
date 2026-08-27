Review the implementation changes in {{cwd}} against the supplied specification
{{spec}} with issue URI {{issue}}.

This is round {{round}}. If round > 1, reuse your previous context and see if
previous problems are fixed.

Report, under 400 words:

(a) requirements the specification asks for that are missing or only partially
implemented;
(b) behavior in the implementation that the specification did not ask for
(scope creep); and
(c) requirements that look implemented but whose implementation appears wrong.

Quote the relevant specification line for every finding.

Review this unstaged diff as part of the checkout:

{{unstaged_diff}}

Do not edit the checkout. You are the spec reviewer, review yourself, do not spawn sub-agents.
