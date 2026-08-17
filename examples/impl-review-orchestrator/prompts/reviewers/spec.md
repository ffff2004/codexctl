Review the implementation changes in {{cwd}} against the supplied specification
{{spec}}. This is round {{round}}, with issue URI {{issue}}.

Report, under 400 words:

(a) requirements the specification asks for that are missing or only partially
implemented;
(b) behavior in the implementation that the specification did not ask for
(scope creep); and
(c) requirements that look implemented but whose implementation appears wrong.

Quote the relevant specification line for every finding.

Review this unstaged diff as part of the checkout:

{{unstaged_diff}}

Do not edit the checkout. End with exactly one terminal marker:
VERDICT: PASS
or
VERDICT: FAIL
