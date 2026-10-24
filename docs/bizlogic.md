# Business logic

`codocs bizlogic` infers the business logic of a repository from its function dependency
graph and writes it as JSON plus a section of wiki pages with Mermaid diagrams. It is a
separate command, not a stage of `codocs run`.

```bash
codocs bizlogic REPO -o OUT --plan-only        # graph, clusters, roots, walks; no LLM
codocs bizlogic REPO -o OUT -k 20              # one granularity
codocs bizlogic REPO -o OUT -k 10 -k 40        # several granularities, one run
codocs bizlogic REPO -o OUT --domain-profile 3gpp-5gc
codocs bizlogic-render OUT                     # rebuild pages and HTML from the JSON; no LLM
```

## Pipeline

1. **Function graph.** Top-level functions and methods become nodes. The edges are calls,
   function references and handler tables. Docs come from `OUT/docs` (written by `docgen`
   or `run`), from `--docs` roots and from in-tree `markdown_docs/`. Utilities are skipped by
   walks and clustering: functions with at least `--utility-fanin` callers, tiny accessors,
   plumbing names (logging, locking, printing, error text) and every function of a package
   called from at least `--infra-callers` packages. Test-support directories are left out.
   A Go module gives a Go-only graph. Any other `--source-lang` set adds
   Python/Java/C/C++/COBOL functions and cross-language edges.
2. **Hierarchical clusters.** Louvain gives the base communities. Each community is embedded
   by call structure, documentation and identifier text, and the package tree (`--alpha`,
   `--pkg-weight`). Bisecting 2-means then splits them, so every K is a refinement of every
   smaller K.
3. **Roots.** A root is an uncalled (or only dispatched) business function that reaches at
   least `--min-reach` others. When there are more than `--max-roots`, each subsystem (first
   `--group-depth` path segments) gets a share of the budget.
4. **Random walks.** `--walks` walks per root, at most `--walk-length` functions each. A
   walk skips utilities, never revisits a function and backtracks at a leaf, so sibling
   steps of one procedure stay together.
5. **Deduplication per K.** Each walk becomes its sequence of clusters at granularity K. A
   rare sequence (fewer than `--min-share` × walks / K walks) is folded into the closest
   kept one as a variant. A sequence that is a prefix of exactly one longer sequence is
   merged into it (`--no-subsume` keeps it). Fewer clusters give fewer, broader
   trajectories.
6. **LLM steps.** The LLM names each cluster and each parent cluster (√K groups), then
   infers one business logic per trajectory: title, trigger, outside parties, steps mapped
   to the cluster sequence, alternatives, state changes, data objects and confidence.
   The confidence is capped by the evidence (number of walks, share of documented
   functions).

The diagrams are deterministic Mermaid built from the stored JSON, with no LLM call. Each
drawn fact carries an evidence level: bold means code evidence, solid means the LLM shows
an exchange, and dashed means the party is only mentioned. The caption under each diagram
explains it.

## Options

| Option | Default | Effect |
|---|---|---|
| `-o, --output OUT` | required | results go to `OUT/bizlogic/` |
| `-k, --clusters K` | `20` | granularity, repeatable; K is clamped to the number of base communities |
| `--subtree DIR` | | restrict to one subdirectory |
| `--wiki-dir DIR` | | existing wiki (e.g. `OUT/wiki`) to extend with a Business Logic section |
| `--docs DIR` | | extra root holding `markdown_docs/` trees, repeatable |
| `--domain-profile NAME\|PATH` | none | domain vocabulary, see [Domain profiles](#domain-profiles) |
| `--resolution` | `1.0` | Louvain resolution of the base communities |
| `--alpha` | `0.6` | weight of call structure against text in the hierarchy |
| `--pkg-weight` | `0.5` | weight of the package tree in the hierarchy |
| `--utility-fanin` | `40` | functions with at least this many callers are utilities |
| `--infra-callers` | `40` | packages called from this many packages are infrastructure (`0` = off) |
| `--keep-accessors` | off | do not fold tiny Get/Set/Is/Has methods |
| `--max-roots` | `300` | roots to walk from (`0` = all) |
| `--min-reach` | `3` | business functions a root must reach |
| `--group-depth` | `1` | path segments that define a subsystem for the root quotas |
| `--exclude GLOB` | test-support dirs | replaces the default excludes (`*/ut/*`, `*/stub/*`, `*/mock/*`, `*/test/*`, ...); `--exclude ''` keeps everything |
| `--exclude-extra GLOB` | | excluded in addition to the default |
| `--walks` | `20` | random walks per root |
| `--walk-length` | `12` | maximum functions per walk |
| `--min-share` | `0.02` | share that keeps a cluster sequence as its own trajectory |
| `--no-subsume` | off | keep trajectories whose sequence is a prefix of another |
| `--max-trajectories` | all | cap on trajectories sent to the LLM per K (`0` = all) |
| `--seed` | `0` | random seed for clustering and walks |
| `-l, --lang` | `en` | output language (`en`, `ja`, `ko`) |
| `--plan-only` | off | steps 1-5 only, no LLM |
| `-m`, `-j`, `--source-lang`, ... | | shared options ([Usage](usage.md)) |

`codocs bizlogic-render OUT` takes `-k K` (repeatable; default every `OUT/bizlogic/k<K>`),
`--repo DIR` (rebuilds function locations when `nodes.json` is missing), `--system-name`
and `--domain-profile`.

## Outputs

```text
OUT/bizlogic/
├── graph.json              # graph stats, parameters, roots, coverage
├── summary.json            # time, tokens, per-K counts (and the profile name, if any)
├── domain_profile.json     # the profile of the run (only with --domain-profile)
└── k<K>/
    ├── clusters.json       # clusters and parent groups with LLM names, run stats
    ├── nodes.json          # file, line and doc summary of the central functions
    ├── trajectories.json   # trajectories, dedup stats, coverage per subsystem
    ├── business_logic.json # trajectory + inferred logic + function locations
    └── wiki/               # Markdown pages and <project>-business-logic-k<K>.html
```

The pages are:

- **Overview**: system context (D1), procedures (D2, only when a profile lists procedures
  and the LLM assigned some), parties per group, components (D3), component call map (D4),
  all business logic, coverage, and run stats.
- **Group** pages: use cases (D7) and call map (D8).
- **Business logic** pages: participants (D9), flow (D10: a sequence, activity or family
  view), steps, variants, alternatives, state changes (D11), related logic, and where in
  the code.
- **Component** pages: an ego map (D13) and the central functions.

## Domain profiles

Without a profile, `bizlogic` is domain-neutral:

- Deployables are the top directories.
- Outside parties are the LLM's free text, normalised by a small generic set: operator, monitoring, Kubernetes operator and platform, key-value store, DNS server, load balancer, other services.
- The prompts carry no actor, procedure or state lists.

A domain profile adds vocabulary for one kind of system. It is a JSON file, or the name of
a built-in profile.

```bash
codocs bizlogic REPO -o OUT --domain-profile 3gpp-5gc        # built-in
codocs bizlogic REPO -o OUT --domain-profile ./shop.json     # your own
```

The run stores the profile as `OUT/bizlogic/domain_profile.json`. `bizlogic-render` uses
this stored profile unless `--domain-profile` names another one, or `none`. Procedures that
the LLM assigned are kept in `business_logic.json` as written. They are shown only while a
profile lists them.

### Fields

Every top-level field is optional; inside list entries, the keys marked below are required.
Unknown fields, missing required keys, wrong types, invalid regexes and names that refer to
nothing are rejected with a message that names the field:

- the party names in the `interfaces` rows, `wildcard_parties`, the `actor` of each
  `prompt_actors` entry and `pkg_evidence[].actors` must be actor names: a profile
  `actors[].name` or a built-in one (`Operator / OAM`, `Monitoring / observability`,
  `Kubernetes operator`, `Kubernetes platform`, `Key-value store`, `DNS server`,
  `Load balancer`, `Other services`);
- every `self` value must be a `deployables` value (its keys are not checked);
- every `state_entities[].state` must be a key of `states`.

The deployable keys of `interfaces` are not checked against `deployables`.

| Field | Type | Meaning |
|---|---|---|
| `name` | string | profile name (default: the file name) |
| `description` | string | free text |
| `system_description` | string | inserted into the prompts: "a large codebase (*system_description*)" |
| `deployables` | {prefix: name} | package prefix (relative to the module root) → deployable name; the longest prefix wins, other packages fall back to their top directory |
| `self` | {actor: deployable} | the deployable that *is* that party: never drawn as its own outside actor, and shown inside the system boundary when present in the run |
| `interfaces` | {deployable: {actor: label}} | the interface a party uses towards a deployable, e.g. `"N11 (Nsmf)"`; the part before ` (` is the reference point; key `"*"` applies to every party in `wildcard_parties` |
| `wildcard_parties` | [actor] | parties the `"*"` interface rows apply to |
| `actors` | [rule] | outside parties, tried in order after the built-in operator rules and before the other built-in rules (see below) |
| `prompt_actors` | [name \| {name, actor}] (both keys required) | the `[ALLOWED ACTORS]` list in the trajectory prompt, with interface hints; a plain string is its own actor |
| `procedures` | [{name, family, reference}] (`name` and `family` required) | the `[KNOWN PROCEDURES]` list; the LLM may only name these in `implements` / `supports`; families are the D2 use cases |
| `ops_family` | string | D2 family for operations & maintenance logic without a procedure |
| `data_objects` | [string] | examples for the `data_objects` field of the prompt |
| `states` | {entity: [state]} | state vocabulary per entity; D11 draws only transitions within it |
| `state_aliases` | {state: state} | e.g. `"IDLE": "CM-IDLE"`; applied when the target starts with the entity key and `-` |
| `state_entities` | [{state, pattern, case_sensitive}] (`state` and `pattern` required) | regexes that map the LLM's entity text to a `states` key, first match wins |
| `pkg_evidence` | [{prefix, interface, actors}] (all three required) | packages whose code implements an interface to these parties (bold edges, `via` fallback); test and driver packages never count |
| `controller_roots` | [prefix] | roots under these prefixes are controllers: never started by the operator role |
| `plumbing` | regex | extra utility names, added to the default plumbing pattern |
| `party_tokens` | [word] | party names that do not count as message evidence on their own |
| `token_aliases` | {word: word} | word rewrites before message verification, e.g. `"namf": "amf"` |
| `short_tokens` | [word] | words shorter than three letters that still count |
| `adjacent_lifelines` | [{first, second, note}] (`first` and `second` required) | sequence lifelines kept side by side, with an optional note over both |
| `message_prefixes` | {actor: text} | prefix of every message to or from that party in sequences |

An `actors` rule:

| Key | Default | Meaning |
|---|---|---|
| `name` | required | canonical name shown in diagrams |
| `pattern` | required | regex searched in the LLM's actor strings |
| `kind` | `system` | `nf` / `system` («system» box), `role` (stick figure), `infra` (infrastructure the code calls, a dashed cylinder in D1), `generic` (catch-all, used only when nothing specific matched) |
| `case_sensitive` | `false` | match `pattern` case-sensitively |
| `unless` | | skip the rule when this actor already matched |
| `label` | name | display label |
| `aliases` | the name up to ` (` or ` /` | phrases searched in the trigger text of older results |
| `case_sensitive_aliases` | | the same, matched case-sensitively |
| `primary` | `false` | drawn left of the system (as a starter) for results without trigger sources |
| `device` | `false` | drawn as a «device» box |

### Example

```json
{
  "name": "shop",
  "system_description": "an online shop",
  "deployables": {"checkout": "Checkout service", "billing": "Billing service"},
  "self": {"Billing": "Billing service"},
  "actors": [
    {"name": "Customer", "kind": "role", "pattern": "\\bcustomers?\\b|\\bbuyers?\\b",
     "primary": true},
    {"name": "Billing", "pattern": "\\bbilling\\b"},
    {"name": "Payment provider", "pattern": "payment (provider|gateway)|\\bpsp\\b"}
  ],
  "interfaces": {"Checkout service": {"Customer": "HTTPS (REST)",
                                      "Payment provider": "PSP API (REST)"}},
  "prompt_actors": ["Customer", {"name": "payment gateway", "actor": "Payment provider"},
                    {"name": "operator", "actor": "Operator / OAM"}],
  "procedures": [{"name": "Place order", "reference": "ordering spec 2.1",
                  "family": "Ordering"}],
  "states": {"Order": ["NEW", "PAID", "SHIPPED"]},
  "state_entities": [{"state": "Order", "pattern": "\\border\\b"}]
}
```

### The built-in `3gpp-5gc` profile

`src/codocs/bizlogic/profiles/3gpp-5gc.json` is an example for a 3GPP 5G core network. It
contains only public 3GPP vocabulary:

- network functions as actors: UE, NG-RAN, AMF, SMF, UPF, AUSF, UDM, UDR, PCF, NRF, NSSF,
  SCP, CHF, NEF/AF, SMSF, LMF, GMLC, 5G-EIR, CBC, MME;
- their reference points and service-based interfaces (TS 23.501);
- registration, connection, mobility, PDU session, policy, security, SMS, location, public
  warning and NF management procedures (TS 23.502, TS 33.501);
- RM/CM, NF registration and PDU session states.

Its deployables use the conventional lower-case network-function directory names (`amf/`,
`smf/`, `smsf/`, `ausf/`, `udm/`, `udr/`, `pcf/`, `nrf/`, `nssf/`, `chf/`, `upf/`), as in
open-source 5G cores. To adapt it to another layout:

1. Copy the file.
2. Change `deployables` and `self`.
3. Optionally add `pkg_evidence` for the packages that implement each interface.
4. Pass the copy with `--domain-profile PATH`.
