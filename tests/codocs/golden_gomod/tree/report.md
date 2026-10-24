# CoDoc report

## Analysis

- **module**: example.com/fixture
- **go_version**: go version <GO>
- **packages**: 2
- **packages_with_tests**: 1
- **files**: 3
- **lines**: 114
- **test_files**: 2
- **statuses**: ok: 3
- **docgen_targets**: 3
- **testgen_targets**: 3

## Run

- **model**: gpt-5.4
- **tokens** (cumulative over every run into this OUT): 0 in / 0 out
- **analyze**: <t>
- **baseline**: <t>
- **testgen**: <t>
- **docgen**: <t>

## Module docs (SpecForge-go)

- **files**: 3
- **ran**: 3
- **statuses**: ok: 3
- **errors**: 0
- **mean_best_phi**: 1.00
- **early_stop_rate**: 1.00
- **reached_threshold**: 3
- **rounds_histogram**: 1: 3
- **total_rounds**: 3
- **seconds**: <t>
- **signals**: generated-tests: 1, tests: 2

| file | status | signal | rounds | φ trajectory | best φ | early stop |
|---|---|---|---|---|---|---|
| calc/calc.go | ok | tests | 1/3 | 1.00 | 1.00 | yes |
| calc/stats.go | ok | tests | 1/3 | 1.00 | 1.00 | yes |
| shapes/shapes.go | ok | generated-tests | 1/3 | 1.00 | 1.00 | yes |

## Unit tests (TeSearch-go)

- **files**: 3
- **ran**: 3
- **statuses**: ok: 3
- **errors**: 0
- **mean_best_score**: 0.58
- **early_stop_rate**: 0.33
- **reached_threshold**: 1
- **rounds_histogram**: 1: 1, 2: 2
- **total_rounds**: 5
- **seconds**: <t>
- **mean_best_cov**: 1.00
- **mean_best_mut**: 0.58
- **delivered_suites**: 3
- **delivered_tests**: 7

| file | status | rounds | score trajectory | best | cov | mut | size | early stop |
|---|---|---|---|---|---|---|---|---|
| calc/calc.go | ok | 2/2 | 0.50 → 0.50 | 0.50 | 1.00 | 0.50 | 3 | no |
| calc/stats.go | ok | 1/2 | 1.00 | 1.00 | 1.00 | 1.00 | 1 | yes |
| shapes/shapes.go | ok | 2/2 | 0.25 → 0.25 | 0.25 | 1.00 | 0.25 | 3 | no |
