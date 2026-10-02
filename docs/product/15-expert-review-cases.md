# Экспертные review cases

[Индекс](README.md) · [Архитектура](04-architecture.md) · [Центральный Live-агент](12-central-live-agent.md).

## Зачем это отдельный объект

Projects Hub уже умеет быть голосовой рабочей поверхностью для задач, решений и
проектов. Экспертная проверка внешнего предметного объекта похожа на задачу, но
имеет другой инвариант:

- canonical case и его предметные данные принадлежат owning service;
- эксперт не “закрывает задачу”, а выносит типизированное предметное решение;
- evidence нельзя переписывать в Projects Hub;
- решение должно вернуться в owning service и получить durable receipt/readback.

Поэтому вводится общий тип **external expert review case**, а не ещё одна memory
note или копия доменной БД.

Первый consumer — Street Story contradiction journal для POI.

## Владение

~~~text
Projects Hub
  = люди/команды + expertise profiles + UX + routing + notifications

Street Story
  = POI + claims + evidence + conflicts + review case + assignment snapshot
    + final domain decision

Regional Knowledge
  = book/page/region evidence + author/source verification score
~~~

Projects Hub не становится владельцем “истины” о POI.

## Expert identity

Canonical identity:

~~~text
issuer + sub
~~~

Expert profile is a Projects Hub/platform object:

~~~text
expert_profile
  subject
  display_name
  geography[]
  historical_periods[]
  subjects[]
  languages[]
  institutional_roles[]
  evidence_refs[]
  verification_state
  verified_by
  updated_at
~~~

Expertise is scoped. “Historian” is not a universal permission to resolve every
historical dispute.

Unknown/self-declared expertise may be displayed but does not automatically grant
resolution rights. Workspace/admin policy determines which verified profiles are
eligible for assignments.

## Incoming case

Owning service exposes a versioned read projection. For Street Story:

~~~text
contract_version = poi.review_case.v1

review_case_id
poi_id
status
relation
claims[]
  claim_id
  text
  verification_score
  evidence_refs[]
required_expertise
required_reviews
scope
detector_suggestion?
~~~

Projects Hub stores only:
- remote service/resource reference;
- assignment/workflow metadata;
- notification state;
- local UI projection/cache with freshness/version;
- command/receipt history.

It does not persist a second canonical copy of claims/evidence.

## Assignment

Routing is deterministic:

1. fetch accessible open cases;
2. compare required expertise with verified expert profiles;
3. apply workspace membership/availability policy;
4. create an assignment in the owning service;
5. notify the selected expert.

The central Live model may explain a case or suggest who appears relevant, but the
backend does not let the model invent qualifications or grant permissions.

Street Story keeps the durable assignment snapshot:

~~~text
review_case_id
expert_sub
assigned_by
expertise_snapshot
assignment_revision
assigned_at
status
~~~

This is important because a profile can change after the decision.

## Multi-expert policy

Case declares required_reviews.

Default:
- ordinary conflict: 1 verified expert;
- high-impact / low-confidence / policy-marked sensitive conflict: 2 independent experts;
- configurable hard maximum for MVP: 3.

One expert must not see another expert's unpublished decision before submitting
their own when the case requires independent review.

If decisions disagree, case remains open and can:
- request another reviewer;
- be escalated to an explicitly authorized senior expert;
- remain unresolved.

No majority rule is invented unless a domain policy explicitly defines it.

## Expert UX

The case appears as a compact floating-island work card, not a long form.

Visible:
- POI name/identity;
- why review is needed;
- claim A / claim B;
- verification score **components**, not only one number;
- source/author provenance;
- exact evidence links/pages when authorized;
- required expertise and review count;
- prior published/canonical state if relevant.

Quick choices:
- prefer A;
- prefer B;
- both valid: scope differs;
- both valid: different time;
- unresolved;
- need more sources;
- wrong POI link.

Voice is the main rationale input.

The central Live agent can answer questions using authorized evidence and then
call the typed resolution tool. It must not silently resolve the case from its own
analysis.

## Tools

Projects Hub-facing adapter remains narrow:

~~~text
expert_reviews.list_assigned(status?)
expert_reviews.get(review_case_id)
expert_reviews.accept(review_case_id)
expert_reviews.resolve(
  review_case_id,
  expected_revision,
  resolution,
  rationale,
  confidence?
)
expert_reviews.request_research(review_case_id, rationale)
~~~

These are adapters to the owning service. They do not contain another LLM.

Every mutation requires:
- actor binding;
- resource-bound OAuth/delegation for owning service;
- assignment/role check;
- expected revision;
- idempotency/command ID;
- readback receipt.

## Private evidence

Assignment is allowed only if the expert can read every evidence item required by
the case.

A public POI can have private evidence. That private evidence must not appear in a
review card for an unauthorized expert.

If the current actor lacks evidence access:
- case is not assigned to them, or
- a deliberately redacted review variant is created by domain policy;
- backend never fetches private evidence and filters it after retrieval.

## “Need more sources”

This is a first-class resolution state, not a failure.

For Street Story it returns a typed command to the case:

~~~text
needs_more_sources
rationale
requested_scope:
  claim ids / period / source type / language
~~~

Street Story/Regional Knowledge can later attach new evidence. The same case
revision advances and returns to review.

Projects Hub itself does not launch hidden research LLMs inside the tool.

## Notifications

Review cases use normal Projects Hub notification policy:
- assigned case can produce a compact notification;
- deadline/priority decides immediate vs digest;
- quiet hours apply;
- deep link opens the review card;
- opening a card does not start microphone automatically.

## Cross-service OAuth

Projects Hub is an OAuth client of the owning review service.

For a Street Story private case:

~~~text
expert -> Projects Hub
             |
             | user-approved Street Story resource grant
             v
         Street Story
             |
             +-- case/read evidence authorization
             +-- assignment authorization
             +-- resolve/readback
~~~

Never forward the Projects Hub bearer token to Street Story.

## Durable decision history

Projects Hub records command/receipt, but Street Story owns the append-only
review decision.

A decision record contains:
- expert sub;
- assignment revision;
- expertise snapshot reference;
- resolution;
- rationale;
- optional confidence;
- evidence/case revision observed;
- submitted_at;
- durable owning-service receipt.

Changing a decision creates a new revision/history entry; it does not rewrite the
earlier expert opinion invisibly.

## Release gates

1. Same user sub resolves across Projects Hub and Street Story.
2. Unassigned expert cannot open private review evidence.
3. Revoked grant blocks subsequent reads/resolution immediately.
4. Required expertise is checked deterministically.
5. Two-review case hides reviewer A's pending decision from reviewer B.
6. Stale expected revision is rejected.
7. Duplicate resolve command returns same receipt.
8. “Need more sources” leaves the case open.
9. Expert decision changes Street Story state only after durable readback.
10. Projects Hub restart does not lose assignment/command receipt.
