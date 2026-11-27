# Test migration map

How every test from the baseline (329 passed) maps to the suite after
SPEC_DECISIONS.md. Generated from the collected test ids (baseline:
JUnit report taken before any change) plus the list of hand-written changes;
every changed entry gives the reason. No test was deleted to make the suite
pass; every REPLACED test has a named successor that checks the same intent
under the new rule.

Status: **RETAINED** same intent and assertions (at most an API rename or
a renamed config field); **REWRITTEN** same intent, assertion changed by a
decision; **REPLACED** removed, intent covered by the named test(s);
**NEW** added; **SKIPPED** / **REMOVED** none.

Counts are at the end. 'now:' counts test functions in the current suite
(a parametrized test counts once); 'baseline id gone' counts baseline test
functions whose name no longer exists, each with its successor.

## Changed pytest tests

| status | old test | new test | old behaviour | new behaviour | reason |
|---|---|---|---|---|---|
| REPLACED | `test_provider.py::test_node_ids_look_like_node_dash_12_hex` | test_node_ids_are_sequential_and_zero_padded | id is 'node-' + 12 random hex | ids are node-00001, node-00002, ... | D14: sequential ids are reproducible and restart-safe |
| REPLACED | `test_provider.py::test_different_seed_gives_different_ids` | test_different_seed_gives_different_timings | two seeds give different node ids | same sequential ids, different boot times | D14: ids no longer depend on the seed; the seed still drives timings |
| REPLACED | `test_provider.py::test_launch_errors_are_raised_first_in_order` | test_unavailable_window_is_half_open, test_first_launch_succeeds_then_a_window_refuses_the_second | a list of queued launch errors is raised, one per call | explicit [from, to) outage windows per node type; boundaries 99/100/299/300 | D4: windows are stateless (no in-memory queue) and survive restarts |
| REWRITTEN | `test_provider.py::test_capacity_outages_come_in_bursts` | test_random_outages_come_in_bursts_of_whole_buckets | outage slots all-or-nothing, 2-20 of 200 | buckets all-or-nothing, every burst >= 2 buckets long, 1-25% of 400 buckets | D4: bursts now last randint(2, 6) buckets; the test also checks burst length |
| REWRITTEN | `test_provider.py::test_next_event_time_is_the_earliest_pending_transition` | test_next_transition_time_is_the_earliest_pending_one | provider.next_event_time(conn, now) | provider.next_transition_time; also checks 'strictly after now' | API renamed in the engine rewrite; assertion kept and one added |
| REWRITTEN | `test_provider.py::test_due_transitions_happen_exactly_at_the_planned_time` | (same name) | provider.due() returns and applies (row, outcome) | due_transitions() lists, apply_transition() applies; still exact at 239/240 and applied once | API split so the engine applies each transition inside its group |
| REWRITTEN | `test_provider.py::test_never_ready_node_fails_at_its_planned_time` | (same name) | provider.due() | due_transitions + apply_transition; same failed at 900 | API split (as above) |
| REWRITTEN | `test_provider.py::test_fixed_delay_override` | (same name) | launch_delay_seconds=240 | boot_min_s = boot_max_s = 240; same ready at 340 | Config fields renamed (one Config, D7) |
| RETAINED | `test_scheduler.py::test_launches_the_smallest_node_type_that_fits` | test_launches_the_cheapest_node_type_that_fits | smallest type that fits | same 4 cases, same assertions | renamed: the rule is now 'cheapest' (D13); for one job it is the same type |
| REPLACED | `test_scheduler.py::test_auto_launch_off_never_launches` | test_engine.py::test_scheduler_disabled_never_places_or_launches | EngineConfig(auto_launch=False): place but never launch | Config(scheduler_enabled=False): the engine runs no scheduler at all | D7: one test switch; the flag 'auto_launch' no longer exists |
| REWRITTEN | `test_scheduler.py::test_backoff_doubles_and_is_capped_at_15_minutes` | test_capacity_backoff_doubles_and_is_capped_at_15_minutes | backoff_seconds(k) = 60, 120, 240, 480, 900, 900 | retry_at() after k capacity_unavailable events: the same delays | D2: delay derived from the events table per node type; same numbers |
| REWRITTEN | `test_scheduler.py::test_capacity_unavailable_keeps_job_queued_and_logs_failure` | test_capacity_unavailable_keeps_job_queued_and_logs_it | event 'launch_failed', entity provider | event 'capacity_unavailable', entity provider | D2/D3: 'launch_failed' now means never-ready only; refusals have their own event |
| REWRITTEN | `test_scheduler.py::test_one_failure_stops_further_launches_in_the_same_pass` | test_one_refusal_stops_further_tries_of_that_type_in_the_same_pass | one failure stops all launches in the pass | one refusal stops that node type for the pass (other types may be tried as fallback) | D2/D3: backoff is per node type; the test keeps two jobs of the same type -> one event |
| REPLACED | `test_scheduler.py::test_a_successful_ready_node_resets_the_backoff` | test_capacity_backoff_resets_after_a_successful_launch, test_never_ready_backoff_resets_when_a_node_becomes_ready | any node_ready event clears the (single) backoff | capacity backoff resets on node_launched; never-ready backoff resets on node_ready; per type | D2: two retry policies with their own reset events |
| REWRITTEN | `test_scheduler.py::test_at_cap_the_longest_idle_node_is_evicted_to_make_room` | (same name) | evicted at now=500, end_reason 'idle_reaped' | evicted at now=600, end_reason 'evicted' | D12: only nodes that lived 600 s are evicted; D11: new 'evicted' reason. Younger case: test_a_node_younger_than_the_minimum_lifetime_is_not_evicted |
| RETAINED | `test_scheduler.py::test_at_cap_with_no_idle_node_the_job_waits` | (same name) | now=10 | now=1000 (so the lifetime rule is not what blocks eviction) | keeps the test meaningful under D12 |
| RETAINED | `test_scheduler.py::test_eviction_can_be_turned_off` | (same name) | EngineConfig(evict_idle_at_cap=False), now=10 | Config(evict_at_cap=False), now=1000 | switch kept (rule 14) under one Config; now past the lifetime |
| REWRITTEN | `test_scheduler.py::test_launch_retries_wait_for_the_backoff` | (same name) | 3 queued launch errors; launch_retry_at(conn) | outage window gpu-s [0, 420); next_retry_time; same 60/180/420 schedule | D2/D4: windows replace error queues; same assertions on times and counts |
| RETAINED | `test_scheduler.py::test_queue_order_decides_who_gets_scarce_capacity` | (same name) | auto_launch=False | default config (the losing job may launch a node; it stays queued either way) | auto_launch flag removed (D7); assertions unchanged |
| RETAINED | `test_scheduler.py::test_smaller_job_behind_a_blocked_one_can_still_start` | (same name) | auto_launch=False | default config | as above |
| REWRITTEN | `test_reaper.py::test_idle_node_is_alive_at_exactly_the_timeout` | (same name) | idle since 0, alive at 300 | idle since 600 (past the lifetime), alive at 900 | D1: from 0 the 600 s lifetime would decide, not the timeout |
| REWRITTEN | `test_reaper.py::test_idle_node_is_reaped_one_second_after_the_timeout` | (same name) | reaped at 301, reason 'idle_reaped' | idle since 600, reaped at 901, reason 'idle_timeout' | D1 (lifetime) and D11 (reason name) |
| REWRITTEN | `test_reaper.py::test_reaped_at_its_exact_second_even_in_one_big_advance` | (same name) | idle since 100 -> 401 | idle since 1000 -> 1301 | D1: keep the idle rule the deciding one |
| REWRITTEN | `test_reaper.py::test_reaping_survives_a_restart` | (same name) | idle since 0, restart at 200, reaped at 301 | idle since 600, restart at 700, reaped at 901 | D1 |
| RETAINED | `test_reaper.py::test_idle_timeout_from_environment` | (same name, parametrized) | BROKER_IDLE_TIMEOUT_MINUTES | BROKER_IDLE_MINUTES and the old name both work | new name from the spec; old name kept so README setups still work |
| RETAINED | `test_reaper.py::test_invalid_config_is_rejected` | (same name) | idle_timeout_minutes=-1, max_live_nodes=0 | same two (renamed fields) plus min_node_lifetime_s=-1, policy='random' | one Config; cases added |
| REWRITTEN | `test_engine.py::test_never_ready_launch_fails_at_15_minutes_then_retries_after_backoff` | test_never_ready_launch_fails_at_15_minutes_then_retries_with_backoff | relaunch 60 s after the 1st failure, 120 s after the 2nd: 0, 960, 1980 | relaunch at once after the 1st, 60 s after the 2nd: 0, 900, 1860 | D2: never-ready retry table 0, 60, 120, 240, 480 |
| RETAINED | `test_engine.py::test_job_waits_for_gpus_released_by_a_planned_end` | (same name) | auto_launch=False so the 2nd job can't get its own node | max_nodes=1 for the same effect | auto_launch removed (D7); same assertions |
| RETAINED | `test_engine.py::test_capacity_outage_delays_the_launch_until_the_retry` | (same name) | launch_errors=[CapacityUnavailable] | unavailable_windows gpu-s [0, 60); same start 360 | D4 |
| RETAINED | `test_engine.py::test_planned_job_ends_exactly_actual_minutes_after_it_starts` | (same name) | 2nd job: ended = started + 300 | 2nd job: exact (300, 600) | assertion made stricter |
| REPLACED | `test_schema.py::test_user_version_is_1_after_init_db` | test_user_version_is_2_after_init_db (+ migration tests) | user_version 1 | user_version 2 | schema v2 (D6 created_seq, D11 reasons); migration tested from tests/fixtures/schema_v1.sql |
| REWRITTEN | `test_no_wall_clock.py::test_no_wall_clock_calls_in_python` | (same name) | 'import random' forbidden anywhere in broker/ | seeded random.Random(...) allowed; module-level random.* calls, 'from random import' and unseeded Random() rejected | D10: the provider needs seeded generators; the guard is itself tested (test_the_scan_rejects_..., test_the_scan_allows_...) |
| RETAINED | `test_no_wall_clock.py::test_full_workload_never_reads_the_wall_clock` | (same name) | no random failures/outages | default Config: random never-ready and outages on | now also exercises the random paths |
| RETAINED | `test_integration.py::test_workload_covers_launch_failures_reaping_and_planned_ends` | (same name) | reasons include 'idle_reaped' | reasons include 'idle_timeout' | D11 reason rename |

## Changed YAML cases (tests/cases/gpu_job_broker_api_test_cases.yaml)

| status | case id | old behaviour | new behaviour | reason |
|---|---|---|---|---|
| REWRITTEN | `SCHEDULER-CAPACITY-UNAVAILABLE-DOES-NOT-CRASH` | provider launch_errors [CapacityUnavailable]; 2 events (job_submitted, launch_failed) | gpu-m and gpu-l windows [0, 3600); 3 events in order (job_submitted, capacity_unavailable gpu-m, capacity_unavailable gpu-l); no node; job queued | D3 fallback tries gpu-l too; D4 windows replace error lists |
| REWRITTEN | `SCHEDULER-LAUNCH-FAILURE-AFTER-FIFTEEN-MINUTES` | after 15 min exactly one node, failed at 900 | node-00001 failed at 900 (launch_failed) and node-00002 launching at 900; job queued | D2: first never-ready failure retried with 0 s delay; D14 ids |
| REWRITTEN | `IDLE-REAPER-AFTER-TIMEOUT` | idle since 0, timeout 5 min: terminated at 301 | idle since 600 (past the lifetime): terminated at 901, end_reason idle_timeout | D1: a node is kept 600 s; the old expectation is replaced by IDLE-REAPER-WAITS-FOR-MIN-LIFETIME and IDLE-REAPER-AT-MIN-LIFETIME |
| NEW | `SCHEDULER-CAPACITY-UNAVAILABLE-FALLS-BACK-TO-LARGER-TYPE` | - | see case | D3 example: 3 events, gpu-l launching, k=1 |
| NEW | `SCHEDULER-CAPACITY-UNAVAILABLE-NO-EXPENSIVE-FALLBACK` | - | see case | D3/D13: no gpu-s -> gpu-m fallback (4.5x) |
| NEW | `SCHEDULER-NEVER-READY-RETRY-BACKS-OFF` | - | see case | D2: 2nd never-ready failure waits 60 s (1860) |
| NEW | `IDLE-REAPER-EXACT-TIMEOUT-AFTER-MIN-LIFETIME` | - | see case | D1: alive at exactly N minutes once past the lifetime |
| NEW | `IDLE-REAPER-WAITS-FOR-MIN-LIFETIME` | - | see case | D1: idle past the timeout but younger than 600 s: kept (alive at 599) |
| NEW | `IDLE-REAPER-AT-MIN-LIFETIME` | - | see case | D1 example: reaped at 600, idle_timeout |

Other YAML edits: header comments (queue tie-break is now created_seq,
the provider controls, sequential node ids); runner `ANY_NODE_ID` is now
`^node-\d{5}$` (D14). No other case changed.

## Every pytest test

| file | test | status |
|---|---|---|
| test_api.py | `test_health_returns_ok` | RETAINED |
| test_api.py | `test_submit_then_get_returns_same_queued_job` | RETAINED |
| test_api.py | `test_submit_strips_team_whitespace` | RETAINED |
| test_api.py | `test_submit_writes_an_event` | RETAINED |
| test_api.py | `test_same_key_same_body_twice_returns_one_job` | RETAINED |
| test_api.py | `test_omitted_priority_equals_normal_for_idempotency` | RETAINED |
| test_api.py | `test_same_key_different_body_is_409` | RETAINED |
| test_api.py | `test_same_key_under_another_team_creates_new_job` | RETAINED |
| test_api.py | `test_invalid_submit_is_422` (x10) | RETAINED |
| test_api.py | `test_submit_at_the_limits_is_accepted` (x2) | RETAINED |
| test_api.py | `test_cancel_queued_job_then_cancel_again_is_noop` | RETAINED |
| test_api.py | `test_complete_queued_job_is_409` | RETAINED |
| test_api.py | `test_unknown_job_is_404` (x3) | RETAINED |
| test_api.py | `test_complete_running_job_succeeds_and_node_becomes_idle` | RETAINED |
| test_api.py | `test_cancel_one_of_two_running_jobs_keeps_node_busy` | RETAINED |
| test_api.py | `test_complete_finished_job_again_is_409` | RETAINED |
| test_api.py | `test_wait_seconds_grows_while_queued` | RETAINED |
| test_api.py | `test_wait_seconds_when_cancelled_before_start_is_time_until_cancel` | RETAINED |
| test_api.py | `test_queue_orders_by_priority_then_submit_time` | RETAINED |
| test_api.py | `test_queue_excludes_cancelled_jobs` | RETAINED |
| test_api.py | `test_sim_now_starts_at_epoch_zero` | RETAINED |
| test_api.py | `test_sim_advance_by_minutes` | RETAINED |
| test_api.py | `test_sim_advance_to_ts_in_the_past_is_409` | RETAINED |
| test_api.py | `test_sim_advance_to_now_is_noop` | RETAINED |
| test_api.py | `test_sim_advance_invalid_body_is_422` (x3) | RETAINED |
| test_api.py | `test_sim_advance_zero_minutes_is_noop` | RETAINED |
| test_api.py | `test_new_app_on_same_db_keeps_jobs_and_time` | RETAINED |
| test_api.py | `test_cost_report_spec_example` | RETAINED |
| test_api.py | `test_cost_report_failed_node_is_provider_overhead` | RETAINED |
| test_api.py | `test_cost_report_empty_or_reversed_window_is_422` (x2) | RETAINED |
| test_api.py | `test_nodes_shows_derived_gpu_usage` | RETAINED |
| test_api.py | `test_nodes_hides_ended_nodes_unless_asked` | RETAINED |
| test_api.py | `test_nodes_cost_so_far_equals_cost_report` | RETAINED |
| test_api.py | `test_submit_returns_the_saved_job_when_the_pass_fails` | NEW |
| test_api.py | `test_cancel_returns_the_cancelled_job_when_the_pass_fails` | NEW |
| test_api.py | `test_complete_returns_the_finished_job_when_the_pass_fails` | NEW |
| test_concurrency.py | `test_parallel_same_key_same_body_creates_one_job_every_round` | RETAINED |
| test_concurrency.py | `test_parallel_same_key_different_bodies_one_wins_rest_conflict` | RETAINED |
| test_concurrency.py | `test_parallel_submits_without_key_all_succeed` | RETAINED |
| test_concurrency.py | `test_parallel_launches_get_unique_sequential_node_ids` | NEW |
| test_concurrency.py | `test_parallel_submits_from_many_teams_keep_every_invariant` | NEW |
| test_costs.py | `test_overlap` (x4) | RETAINED |
| test_costs.py | `test_spec_example_one_hour_gpu_m_with_one_2_gpu_job_for_30_min` | RETAINED |
| test_costs.py | `test_terminated_node_alive_120s_is_billed_600s` | RETAINED |
| test_costs.py | `test_minimum_top_up_is_counted_once_across_adjacent_windows` | RETAINED |
| test_costs.py | `test_alive_node_has_no_minimum_yet` | RETAINED |
| test_costs.py | `test_node_spanning_window_edges_is_billed_only_for_overlap` | RETAINED |
| test_costs.py | `test_job_gpu_seconds_are_clipped_to_the_window` | RETAINED |
| test_costs.py | `test_job_still_running_on_ended_node_never_makes_idle_negative` | RETAINED |
| test_costs.py | `test_adjacent_windows_add_up_to_the_micro_dollar` (x3) | RETAINED |
| test_costs.py | `test_window_rounding_never_changes_the_whole_bill` | RETAINED |
| test_costs.py | `test_failed_node_goes_to_provider_overhead_not_team` | RETAINED |
| test_costs.py | `test_report_sums_nodes_per_team` | RETAINED |
| test_costs.py | `test_micros_to_dollars_rounds_half_up` (x6) | RETAINED |
| test_engine.py | `test_queued_job_starts_exactly_when_its_node_becomes_ready` | RETAINED |
| test_engine.py | `test_planned_job_ends_exactly_actual_minutes_after_it_starts` | RETAINED |
| test_engine.py | `test_a_planned_cancel_ends_the_job_cancelled` | NEW |
| test_engine.py | `test_job_waits_for_gpus_released_by_a_planned_end` | RETAINED |
| test_engine.py | `test_never_ready_launch_fails_at_15_minutes_then_retries_with_backoff` | REWRITTEN (successor of `test_never_ready_launch_fails_at_15_minutes_then_retries_after_backoff`) |
| test_engine.py | `test_capacity_outage_delays_the_launch_until_the_retry` | RETAINED |
| test_engine.py | `test_scheduler_disabled_never_places_or_launches` | NEW (successor of `test_auto_launch_off_never_launches`) |
| test_engine.py | `test_one_clock_event_per_advance` | RETAINED |
| test_engine.py | `test_advance_to_the_past_raises_and_changes_nothing` | RETAINED |
| test_engine.py | `test_zero_minute_advance_starts_an_eligible_job` | NEW |
| test_engine.py | `test_zero_minute_advance_with_nothing_to_do_writes_nothing` | NEW |
| test_engine.py | `test_zero_minute_advance_does_not_repeat_a_transition` | NEW |
| test_engine.py | `test_identical_timestamps_are_processed_in_a_fixed_order` | NEW |
| test_engine.py | `test_crash_between_groups_keeps_committed_groups_and_resumes` | NEW |
| test_engine.py | `test_a_group_that_never_settles_raises_instead_of_hanging` | NEW |
| test_engine.py | `test_many_small_advances_equal_one_big_advance` | RETAINED |
| test_engine.py | `test_restart_mid_simulation_gives_the_same_result` | RETAINED |
| test_engine.py | `test_same_seed_gives_the_same_simulation_with_random_failures` | NEW |
| test_engine.py | `test_orphan_provider_node_is_terminated` | NEW |
| test_engine.py | `test_node_the_provider_terminated_is_reconciled_and_its_job_fails` | NEW |
| test_engine.py | `test_node_the_provider_failed_is_marked_failed` | NEW |
| test_engine.py | `test_reconcile_with_nothing_wrong_changes_nothing` | NEW |
| test_integration.py | `test_workload_covers_launch_failures_reaping_and_planned_ends` | RETAINED |
| test_integration.py | `test_restart_mid_run_gives_the_same_result` (x3) | RETAINED |
| test_integration.py | `test_parts_add_up_to_the_totals` | RETAINED |
| test_integration.py | `test_cost_windows_split_exactly` (x7) | RETAINED |
| test_integration.py | `test_nothing_is_billed_after_every_node_has_ended` | RETAINED |
| test_integration.py | `test_cost_report_is_the_same_after_a_restart` | RETAINED |
| test_no_wall_clock.py | `test_the_scan_finds_the_broker_sources` | RETAINED |
| test_no_wall_clock.py | `test_no_wall_clock_calls_in_python` (x12) | REWRITTEN |
| test_no_wall_clock.py | `test_the_scan_rejects_clock_reads_and_global_random` (x7) | NEW |
| test_no_wall_clock.py | `test_the_scan_allows_seeded_generators` | NEW |
| test_no_wall_clock.py | `test_no_wall_clock_in_sql` | RETAINED |
| test_no_wall_clock.py | `test_the_guard_catches_broker_code_reading_the_clock` | RETAINED |
| test_no_wall_clock.py | `test_the_guard_catches_time_calls_from_broker_frames_only` | RETAINED |
| test_no_wall_clock.py | `test_full_workload_never_reads_the_wall_clock` | RETAINED |
| test_provider.py | `test_smallest_node_type_that_fits` (x4) | RETAINED |
| test_provider.py | `test_unknown_node_type_is_rejected` | NEW |
| test_provider.py | `test_same_seed_gives_same_ids_and_timings` | RETAINED |
| test_provider.py | `test_different_seed_gives_different_timings` | NEW (successor of `test_different_seed_gives_different_ids`) |
| test_provider.py | `test_node_ids_are_sequential_and_zero_padded` | NEW (successor of `test_node_ids_look_like_node_dash_12_hex`) |
| test_provider.py | `test_a_new_provider_object_continues_the_same_sequence` | NEW |
| test_provider.py | `test_the_injected_generator_factory_is_used` | NEW |
| test_provider.py | `test_seeded_rng_gives_the_same_draw_for_the_same_key` | NEW |
| test_provider.py | `test_ready_delay_is_between_4_and_8_minutes` | RETAINED |
| test_provider.py | `test_about_3_percent_of_launches_never_become_ready` | RETAINED |
| test_provider.py | `test_fixed_delay_override` | REWRITTEN |
| test_provider.py | `test_failure_probability_1_always_fails` | RETAINED |
| test_provider.py | `test_never_ready_launches_picks_launches_by_number` | NEW |
| test_provider.py | `test_unavailable_window_is_half_open` (x4) | NEW (successor of `test_launch_errors_are_raised_first_in_order`) |
| test_provider.py | `test_unavailable_window_is_per_node_type` | NEW |
| test_provider.py | `test_first_launch_succeeds_then_a_window_refuses_the_second` | NEW (successor of `test_launch_errors_are_raised_first_in_order`) |
| test_provider.py | `test_random_outages_come_in_bursts_of_whole_buckets` | REWRITTEN (successor of `test_capacity_outages_come_in_bursts`) |
| test_provider.py | `test_outages_are_stateless` | NEW |
| test_provider.py | `test_due_transitions_happen_exactly_at_the_planned_time` | REWRITTEN |
| test_provider.py | `test_never_ready_node_fails_at_its_planned_time` | REWRITTEN |
| test_provider.py | `test_next_transition_time_is_the_earliest_pending_one` | REWRITTEN (successor of `test_next_event_time_is_the_earliest_pending_transition`) |
| test_provider.py | `test_terminate_marks_node_terminated` | RETAINED |
| test_provider.py | `test_terminate_an_ended_node_is_a_no_op` | NEW |
| test_provider.py | `test_terminate_unknown_node_raises` | RETAINED |
| test_reaper.py | `test_idle_node_is_alive_at_exactly_the_timeout` | REWRITTEN |
| test_reaper.py | `test_idle_node_is_reaped_one_second_after_the_timeout` | REWRITTEN |
| test_reaper.py | `test_reaped_at_its_exact_second_even_in_one_big_advance` | REWRITTEN |
| test_reaper.py | `test_timeout_counts_from_idle_since_not_from_ready_at` | RETAINED |
| test_reaper.py | `test_timeout_before_lifetime_waits_for_the_lifetime` | NEW |
| test_reaper.py | `test_lifetime_before_timeout_waits_for_the_timeout` | NEW |
| test_reaper.py | `test_both_rules_due_at_the_same_second` | NEW |
| test_reaper.py | `test_lifetime_counts_from_launch_not_from_ready` | NEW |
| test_reaper.py | `test_minimum_lifetime_is_configurable` | NEW |
| test_reaper.py | `test_eligible_at_formula` (x2) | NEW |
| test_reaper.py | `test_busy_node_is_never_reaped` | RETAINED |
| test_reaper.py | `test_launching_node_is_never_reaped` | RETAINED |
| test_reaper.py | `test_not_reaped_while_a_queued_same_team_job_fits_on_it` | RETAINED |
| test_reaper.py | `test_reaped_when_the_queued_job_could_not_use_it` (x2) | RETAINED |
| test_reaper.py | `test_a_protected_node_gets_the_job_in_the_same_group` | NEW |
| test_reaper.py | `test_reaper_off_never_reaps` | RETAINED |
| test_reaper.py | `test_job_start_stops_the_idle_clock_and_its_end_restarts_it` | RETAINED |
| test_reaper.py | `test_node_ready_with_no_job_is_idle_from_ready_at` | RETAINED |
| test_reaper.py | `test_reaping_survives_a_restart` | REWRITTEN |
| test_reaper.py | `test_billing_stops_when_the_node_is_reaped` | RETAINED |
| test_reaper.py | `test_idle_time_before_the_reap_is_billed_to_the_team` | NEW |
| test_reaper.py | `test_reaping_writes_reason_and_event_details` | NEW |
| test_reaper.py | `test_default_idle_timeout_is_10_minutes` | RETAINED |
| test_reaper.py | `test_idle_timeout_from_environment` (x2) | RETAINED |
| test_reaper.py | `test_invalid_config_is_rejected` (x4) | RETAINED |
| test_replay.py | `test_nearest_rank_percentile` (x10) | NEW |
| test_replay.py | `test_peak_counts_half_open_intervals` | NEW |
| test_replay.py | `test_scenario_metrics_per_team_and_overall` | NEW |
| test_replay.py | `test_aggregate_gives_median_and_range_across_seeds` | NEW |
| test_replay.py | `test_default_scenarios_are_unique_and_ordered` | NEW |
| test_replay.py | `test_the_same_seeds_give_byte_identical_reports_and_other_seeds_differ` | NEW |
| test_replay.py | `test_the_json_holds_no_runtime_or_run_timestamp` | NEW |
| test_replay.py | `test_the_markdown_has_every_section_and_leaves_the_analysis_to_the_reader` | NEW |
| test_replay.py | `test_full_trace_smoke` | NEW |
| test_replay_scenario.py | `test_five_rows_all_end_and_every_node_is_gone` | NEW |
| test_replay_scenario.py | `test_every_job_runs_actual_minutes_after_it_starts` | NEW |
| test_replay_scenario.py | `test_duplicate_rows_become_one_job_and_are_counted` | NEW |
| test_replay_scenario.py | `test_keep_duplicates_makes_every_row_a_job` | NEW |
| test_replay_scenario.py | `test_a_failed_row_is_billed_and_counted_in_wait_time` | NEW |
| test_replay_scenario.py | `test_a_zero_minute_job_placed_during_its_own_post_still_ends_at_its_start` | NEW |
| test_replay_scenario.py | `test_invariants_hold_throughout_a_crowded_scenario` | NEW |
| test_replay_scenario.py | `test_the_invariant_check_catches_a_broken_state` (x3) | NEW |
| test_replay_scenario.py | `test_the_invariant_check_catches_too_many_nodes` | NEW |
| test_replay_scenario.py | `test_the_grand_total_equals_every_node_billed_independently` | NEW |
| test_replay_scenario.py | `test_the_cost_window_holds_every_node_and_its_minimum_charge` | NEW |
| test_replay_scenario.py | `test_an_existing_database_is_refused` | NEW |
| test_replay_scenario.py | `test_a_replay_that_never_drains_fails_loudly_with_what_is_stuck` | NEW |
| test_replay_scenario.py | `test_the_hidden_plan_cannot_be_set_over_http` | NEW |
| test_scheduler.py | `test_places_job_on_same_team_ready_node_with_room` | RETAINED |
| test_scheduler.py | `test_never_places_a_job_on_another_teams_node` | RETAINED |
| test_scheduler.py | `test_best_fit_picks_the_node_left_with_fewest_free_gpus` | RETAINED |
| test_scheduler.py | `test_never_overcommits_a_node` | RETAINED |
| test_scheduler.py | `test_does_not_place_on_a_launching_node` | RETAINED |
| test_scheduler.py | `test_queue_order_decides_who_gets_scarce_capacity` | RETAINED |
| test_scheduler.py | `test_smaller_job_behind_a_blocked_one_can_still_start` | RETAINED |
| test_scheduler.py | `test_a_running_job_is_never_preempted_by_priority` | NEW |
| test_scheduler.py | `test_fifo_ties_are_broken_by_creation_order_not_id` | NEW |
| test_scheduler.py | `test_fair_order_puts_the_team_that_used_less_first` | NEW |
| test_scheduler.py | `test_fair_usage_only_counts_the_last_24_hours` | NEW |
| test_scheduler.py | `test_queue_order_survives_a_restart` | NEW |
| test_scheduler.py | `test_launches_the_cheapest_node_type_that_fits` (x4) | RETAINED (successor of `test_launches_the_smallest_node_type_that_fits`) |
| test_scheduler.py | `test_five_one_gpu_jobs_with_free_slots_get_five_small_nodes` | NEW |
| test_scheduler.py | `test_at_the_cap_waiting_jobs_are_packed_onto_one_larger_node` | NEW |
| test_scheduler.py | `test_queued_jobs_share_one_launching_node` | RETAINED |
| test_scheduler.py | `test_no_second_launch_while_a_launching_node_covers_the_job` | RETAINED |
| test_scheduler.py | `test_never_more_than_10_live_nodes` | RETAINED |
| test_scheduler.py | `test_ended_nodes_do_not_count_toward_the_cap` | RETAINED |
| test_scheduler.py | `test_the_scheduler_never_reads_actual_durations` | NEW |
| test_scheduler.py | `test_fair_cap_stops_a_team_launching_while_others_wait` | NEW |
| test_scheduler.py | `test_fair_cap_does_not_apply_when_no_other_team_waits` | NEW |
| test_scheduler.py | `test_fifo_has_no_fair_cap` | NEW |
| test_scheduler.py | `test_a_team_at_its_fair_cap_does_not_evict_another_teams_node` | NEW |
| test_scheduler.py | `test_without_the_fair_cap_the_same_job_would_evict` | NEW |
| test_scheduler.py | `test_at_cap_the_longest_idle_node_is_evicted_to_make_room` | REWRITTEN |
| test_scheduler.py | `test_a_node_younger_than_the_minimum_lifetime_is_not_evicted` | NEW |
| test_scheduler.py | `test_a_protected_node_is_not_evicted` | NEW |
| test_scheduler.py | `test_at_cap_with_no_idle_node_the_job_waits` | RETAINED |
| test_scheduler.py | `test_eviction_can_be_turned_off` | RETAINED |
| test_scheduler.py | `test_capacity_unavailable_keeps_job_queued_and_logs_it` | REWRITTEN (successor of `test_capacity_unavailable_keeps_job_queued_and_logs_failure`) |
| test_scheduler.py | `test_fallback_to_the_next_larger_type_in_the_same_pass` | NEW |
| test_scheduler.py | `test_no_fallback_beyond_twice_the_price` | NEW |
| test_scheduler.py | `test_a_higher_price_ratio_allows_the_expensive_fallback` | NEW |
| test_scheduler.py | `test_all_acceptable_types_unavailable` | NEW |
| test_scheduler.py | `test_one_refusal_stops_further_tries_of_that_type_in_the_same_pass` | REWRITTEN (successor of `test_one_failure_stops_further_launches_in_the_same_pass`) |
| test_scheduler.py | `test_capacity_backoff_doubles_and_is_capped_at_15_minutes` (x6) | REWRITTEN (successor of `test_backoff_doubles_and_is_capped_at_15_minutes`) |
| test_scheduler.py | `test_never_ready_backoff` (x6) | NEW |
| test_scheduler.py | `test_backoff_is_per_node_type` | NEW |
| test_scheduler.py | `test_capacity_backoff_resets_after_a_successful_launch` | NEW (successor of `test_a_successful_ready_node_resets_the_backoff`) |
| test_scheduler.py | `test_never_ready_backoff_resets_when_a_node_becomes_ready` | NEW (successor of `test_a_successful_ready_node_resets_the_backoff`) |
| test_scheduler.py | `test_launch_retries_wait_for_the_backoff` | REWRITTEN |
| test_schema.py | `test_init_db_twice_keeps_exactly_one_clock_row` | RETAINED |
| test_schema.py | `test_init_db_start_ts_is_set_once_and_not_overwritten` | RETAINED |
| test_schema.py | `test_user_version_is_2_after_init_db` | NEW (successor of `test_user_version_is_1_after_init_db`) |
| test_schema.py | `test_init_db_refuses_a_newer_schema` | NEW |
| test_schema.py | `test_created_seq_numbers_jobs_in_insert_order` | NEW |
| test_schema.py | `test_created_seq_is_unique` | NEW |
| test_schema.py | `test_created_seq_continues_after_reopen` | NEW |
| test_schema.py | `test_every_v2_end_reason_is_accepted` (x6) | NEW |
| test_schema.py | `test_v1_and_unknown_end_reasons_are_rejected` (x3) | NEW |
| test_schema.py | `test_migration_from_v1_keeps_data_and_maps_reasons` | NEW |
| test_schema.py | `test_migrated_database_accepts_the_new_reasons` | NEW |
| test_schema.py | `test_migration_runs_once` | NEW |
| test_schema.py | `test_second_sim_clock_row_is_rejected` | RETAINED |
| test_schema.py | `test_job_with_invalid_gpu_count_is_rejected` (x2) | RETAINED |
| test_schema.py | `test_same_team_and_idempotency_key_twice_is_rejected` | RETAINED |
| test_schema.py | `test_two_jobs_with_null_idempotency_key_are_allowed` | RETAINED |
| test_schema.py | `test_same_idempotency_key_under_two_teams_is_allowed` | RETAINED |
| test_schema.py | `test_running_job_without_node_is_rejected` | RETAINED |
| test_schema.py | `test_queued_job_with_node_is_rejected` | RETAINED |
| test_schema.py | `test_job_on_nonexistent_node_is_rejected` | RETAINED |
| test_schema.py | `test_gpu_s_node_with_8_gpus_is_rejected` | RETAINED |
| test_schema.py | `test_terminated_node_without_terminated_at_is_rejected` | RETAINED |
| test_schema.py | `test_provider_node_needs_exactly_one_planned_outcome` (x2) | RETAINED |
| test_schema.py | `test_transaction_rolls_back_all_rows_and_reraises` | RETAINED |
| test_schema.py | `test_data_survives_close_and_reopen` | RETAINED |
| test_trace.py | `test_an_offset_gives_the_same_epoch_as_utc` | NEW |
| test_trace.py | `test_a_timestamp_without_an_offset_is_rejected_with_a_reason` | NEW |
| test_trace.py | `test_non_utc_offsets_are_converted_and_counted` | NEW |
| test_trace.py | `test_gpus_3_and_16_are_rejected_and_the_others_accepted` | NEW |
| test_trace.py | `test_each_invalid_field_is_rejected_with_its_name` (x8) | NEW |
| test_trace.py | `test_actual_minutes_zero_is_accepted` | NEW |
| test_trace.py | `test_a_wrong_header_is_an_error_not_a_rejected_row` | NEW |
| test_trace.py | `test_identical_rows_share_a_key_and_are_both_kept` | NEW |
| test_trace.py | `test_keep_duplicates_makes_the_keys_differ` | NEW |
| test_trace.py | `test_a_row_identical_only_after_utc_conversion_is_a_duplicate` | NEW |
| test_trace.py | `test_rows_differing_in_any_field_are_not_duplicates` | NEW |
| test_trace.py | `test_out_of_order_input_comes_out_sorted_with_the_count` | NEW |
| test_trace.py | `test_equal_timestamps_keep_file_order` | NEW |
| test_trace.py | `test_team_burst_and_overrun_counts` | NEW |
| test_trace.py | `test_the_burst_window_is_configurable` | NEW |
| test_trace.py | `test_the_markdown_report_lists_rejected_lines` | NEW |
| test_provider.py | ~~`test_node_ids_look_like_node_dash_12_hex`~~ | REPLACED -> test_node_ids_are_sequential_and_zero_padded |
| test_provider.py | ~~`test_different_seed_gives_different_ids`~~ | REPLACED -> test_different_seed_gives_different_timings |
| test_provider.py | ~~`test_launch_errors_are_raised_first_in_order`~~ | REPLACED -> test_unavailable_window_is_half_open, test_first_launch_succeeds_then_a_window_refuses_the_second |
| test_provider.py | ~~`test_capacity_outages_come_in_bursts`~~ | REWRITTEN -> test_random_outages_come_in_bursts_of_whole_buckets |
| test_provider.py | ~~`test_next_event_time_is_the_earliest_pending_transition`~~ | REWRITTEN -> test_next_transition_time_is_the_earliest_pending_one |
| test_scheduler.py | ~~`test_launches_the_smallest_node_type_that_fits`~~ | RETAINED -> test_launches_the_cheapest_node_type_that_fits |
| test_scheduler.py | ~~`test_auto_launch_off_never_launches`~~ | REPLACED -> test_engine.py::test_scheduler_disabled_never_places_or_launches |
| test_scheduler.py | ~~`test_backoff_doubles_and_is_capped_at_15_minutes`~~ | REWRITTEN -> test_capacity_backoff_doubles_and_is_capped_at_15_minutes |
| test_scheduler.py | ~~`test_capacity_unavailable_keeps_job_queued_and_logs_failure`~~ | REWRITTEN -> test_capacity_unavailable_keeps_job_queued_and_logs_it |
| test_scheduler.py | ~~`test_one_failure_stops_further_launches_in_the_same_pass`~~ | REWRITTEN -> test_one_refusal_stops_further_tries_of_that_type_in_the_same_pass |
| test_scheduler.py | ~~`test_a_successful_ready_node_resets_the_backoff`~~ | REPLACED -> test_capacity_backoff_resets_after_a_successful_launch, test_never_ready_backoff_resets_when_a_node_becomes_ready |
| test_engine.py | ~~`test_never_ready_launch_fails_at_15_minutes_then_retries_after_backoff`~~ | REWRITTEN -> test_never_ready_launch_fails_at_15_minutes_then_retries_with_backoff |
| test_schema.py | ~~`test_user_version_is_1_after_init_db`~~ | REPLACED -> test_user_version_is_2_after_init_db (+ migration tests) |

## Post-baseline tests replaced

Added after the baseline for the first trace replay (broker/replay.py,
now removed) and replaced when the replay moved to the HTTP API. Each has
a successor that checks the same intent.

| old test | new test(s) | reason |
|---|---|---|
| `test_replay.py::test_parse_ts_accepts_z_and_offsets` | test_trace.py::test_an_offset_gives_the_same_epoch_as_utc | parsing moved to broker/trace.py |
| `test_replay.py::test_parse_ts_rejects_a_timestamp_without_a_timezone` | test_trace.py::test_a_timestamp_without_an_offset_is_rejected_with_a_reason | now a rejected row with a reason, not an exception |
| `test_replay.py::test_load_trace_sorts_by_time_and_keeps_file_order_for_ties` | test_trace.py::test_out_of_order_input_comes_out_sorted_with_the_count, test_trace.py::test_equal_timestamps_keep_file_order | same sort rule; the count is new |
| `test_replay.py::test_invalid_rows_are_rejected_like_the_api_and_listed` | test_trace.py::test_gpus_3_and_16_are_rejected_and_the_others_accepted, test_trace.py::test_each_invalid_field_is_rejected_with_its_name | rejection moved to the loader; more fields checked |
| `test_replay.py::test_a_small_trace_runs_to_completion_with_exact_times` | test_replay_scenario.py::test_every_job_runs_actual_minutes_after_it_starts, test_replay_scenario.py::test_a_failed_row_is_billed_and_counted_in_wait_time | same exact start/end and billing checks, through the API |
| `test_replay.py::test_cancelled_outcome_ends_the_job_cancelled` | test_replay_scenario.py::test_every_job_runs_actual_minutes_after_it_starts | the nlp row there is a cancel after 7 minutes |
| `test_replay.py::test_nearest_rank_percentile` | test_replay.py::test_nearest_rank_percentile | same name and the same 5 cases, now on replay.metrics (+5 cases) |
| `test_replay.py::test_cli_refuses_an_existing_database` | test_replay_scenario.py::test_an_existing_database_is_refused | replay_scenario refuses an existing file |
| `test_replay.py::test_the_trace_replays_within_the_brief_limits` | test_replay.py::test_full_trace_smoke | same limits (<= 10 nodes, every row accounted for, cost adds up) plus invariants and runtime |
| `test_replay.py::test_the_same_seed_replays_identically_and_another_seed_differs` | test_replay.py::test_the_same_seeds_give_byte_identical_reports_and_other_seeds_differ | now byte-identical report files, on a small trace |

## Every YAML case

| case id | status |
|---|---|
| `JOB-SUBMIT-HAPPY-MATRIX` | RETAINED |
| `JOB-SUBMIT-DEFAULT-PRIORITY` | RETAINED |
| `JOB-SUBMIT-UNICODE-TEAM` | RETAINED |
| `JOB-SUBMIT-GPU-VALIDATION-MATRIX` | RETAINED |
| `JOB-SUBMIT-ESTIMATE-VALIDATION-MATRIX` | RETAINED |
| `JOB-SUBMIT-TEAM-VALIDATION-MATRIX` | RETAINED |
| `JOB-SUBMIT-PRIORITY-CASE-VALIDATION` | RETAINED |
| `JOB-SUBMIT-UNKNOWN-FIELD` | RETAINED |
| `JOB-SUBMIT-EMPTY-BODY` | RETAINED |
| `JOB-SUBMIT-NON-JSON-BODY` | RETAINED |
| `JOB-SUBMIT-IDEMPOTENT-REPLAY` | RETAINED |
| `JOB-SUBMIT-IDEMPOTENT-DEFAULT-EQUIVALENCE` | RETAINED |
| `JOB-SUBMIT-IDEMPOTENCY-CONFLICT` | RETAINED |
| `JOB-SUBMIT-IDEMPOTENCY-SCOPED-BY-TEAM` | RETAINED |
| `JOB-SUBMIT-IDEMPOTENCY-AFTER-CANCEL` | RETAINED |
| `JOB-SUBMIT-IDEMPOTENCY-TRIM-KEY` | RETAINED |
| `JOB-SUBMIT-WITHOUT-IDEMPOTENCY-KEY` | RETAINED |
| `JOB-SUBMIT-SIDE-EFFECTS` | RETAINED |
| `JOB-GET-HAPPY` | RETAINED |
| `JOB-GET-NOT-FOUND` | RETAINED |
| `JOB-GET-WAIT-QUEUED` | RETAINED |
| `JOB-GET-WAIT-RUNNING` | RETAINED |
| `JOB-GET-WAIT-FINISHED` | RETAINED |
| `JOB-GET-WAIT-CANCELLED-BEFORE-START` | RETAINED |
| `JOB-CANCEL-QUEUED` | RETAINED |
| `JOB-CANCEL-RUNNING-LAST-ON-NODE` | RETAINED |
| `JOB-CANCEL-RUNNING-NOT-LAST-ON-NODE` | RETAINED |
| `JOB-CANCEL-IDEMPOTENT-NOOP` | RETAINED |
| `JOB-CANCEL-SUCCEEDED-CONFLICT` | RETAINED |
| `JOB-CANCEL-FAILED-CONFLICT` | RETAINED |
| `JOB-CANCEL-NOT-FOUND` | RETAINED |
| `JOB-COMPLETE-SUCCEEDED` | RETAINED |
| `JOB-COMPLETE-FAILED` | RETAINED |
| `JOB-COMPLETE-FROM-QUEUED-CONFLICT` | RETAINED |
| `JOB-COMPLETE-FROM-CANCELLED-CONFLICT` | RETAINED |
| `JOB-COMPLETE-ALREADY-TERMINAL-CONFLICT` | RETAINED |
| `JOB-COMPLETE-NOT-FOUND` | RETAINED |
| `JOB-COMPLETE-OUTCOME-VALIDATION` | RETAINED |
| `QUEUE-PRIORITY-TIME-ID-ORDER` | RETAINED |
| `QUEUE-EMPTY` | RETAINED |
| `QUEUE-EXCLUDES-NONQUEUED` | RETAINED |
| `QUEUE-WAIT-SECONDS` | RETAINED |
| `SIM-GET-NOW-STARTS-AT-ZERO` | RETAINED |
| `SIM-ADVANCE-BY-MINUTES` | RETAINED |
| `SIM-ADVANCE-TO-TIMESTAMP` | RETAINED |
| `SIM-ADVANCE-TO-PAST-CONFLICT` | RETAINED |
| `SIM-ADVANCE-TO-NOW-NOOP` | RETAINED |
| `SIM-ADVANCE-VALIDATION-BOTH-FIELDS` | RETAINED |
| `SIM-ADVANCE-EMPTY-BODY` | RETAINED |
| `SIM-ADVANCE-MINUTES-ZERO` | RETAINED |
| `SIM-ADVANCE-NEGATIVE-MINUTES` | RETAINED |
| `SIM-ADVANCE-FLOAT-MINUTES` | RETAINED |
| `SIM-ADVANCE-STRING-TO-TS` | RETAINED |
| `SIM-ADVANCE-SMALL-STEPS-EQUIVALENCE` | RETAINED |
| `SIM-ADVANCE-LARGE-STEP-EQUIVALENCE` | RETAINED |
| `NODES-EMPTY` | RETAINED |
| `NODES-DERIVED-GPU-COUNTS` | RETAINED |
| `NODES-LAUNCHING-STATE` | RETAINED |
| `NODES-HIDE-TERMINATED-BY-DEFAULT` | RETAINED |
| `NODES-SHOW-TERMINATED` | RETAINED |
| `NODES-COST-SO-FAR` | RETAINED |
| `NODES-IDLE-SINCE-AFTER-LAST-JOB` | RETAINED |
| `COST-SPEC-RATE-EXAMPLE` | RETAINED |
| `COST-MINIMUM-120-SECOND-LIFETIME` | RETAINED |
| `COST-BOUNDARY-599-SECONDS` | RETAINED |
| `COST-BOUNDARY-600-SECONDS` | RETAINED |
| `COST-BOUNDARY-601-SECONDS` | RETAINED |
| `COST-WINDOW-TERMINATED-AT-TO-TS` | RETAINED |
| `COST-WINDOW-TERMINATED-AT-FROM-TS` | RETAINED |
| `COST-JOB-STARTS-AT-TO-TS` | RETAINED |
| `COST-ADJACENT-WINDOW-LEFT` | RETAINED |
| `COST-ADJACENT-WINDOW-RIGHT` | RETAINED |
| `COST-ADJACENT-WINDOW-WHOLE` | RETAINED |
| `COST-TEAM-TOTALS-SUM-TO-GRAND-TOTAL` | RETAINED |
| `COST-FAILED-NODE-OVERHEAD` | RETAINED |
| `COST-TEAM-WITH-IDLE-TIME-ONLY` | RETAINED |
| `COST-WINDOW-WITH-NO-NODES` | RETAINED |
| `COST-VALIDATION-EQUAL-WINDOW` | RETAINED |
| `COST-VALIDATION-REVERSED-WINDOW` | RETAINED |
| `COST-VALIDATION-FLOAT-FROM-TS` | RETAINED |
| `COST-VALIDATION-FLOAT-TO-TS` | RETAINED |
| `COST-VALIDATION-NONNUMERIC-FROM-TS` | RETAINED |
| `COST-GPU-HOURS-ACTUAL-RUNTIME` | RETAINED |
| `NODES-COST-SO-FAR-CONSISTENT-WITH-REPORT` | RETAINED |
| `COST-REPORT-CONSISTENT-WITH-NODE-COST` | RETAINED |
| `QUEUE-JOB-GET-CONSISTENCY` | RETAINED |
| `QUEUE-GET-JOB-CONSISTENCY` | RETAINED |
| `RESTART-JOBS-CLOCK-NODES-SURVIVE` | RETAINED |
| `RESTART-CLOCK-SURVIVES` | RETAINED |
| `RESTART-NODES-SURVIVE` | RETAINED |
| `RESTART-COST-HISTORY-SURVIVES` | RETAINED |
| `TIME-NO-REAL-CLOCK-READS` | RETAINED |
| `OPTIONAL-JOB-SUBMIT-CONCURRENT-IDEMPOTENCY` | RETAINED |
| `SCHEDULER-REUSE-SAME-TEAM-NODE` | RETAINED |
| `SCHEDULER-NEW-NODE-FITS-GPU-REQUEST` | RETAINED |
| `SCHEDULER-DO-NOT-OVERCOMMIT-GPUS` | RETAINED |
| `SCHEDULER-MAX-TEN-LIVE-NODES` | RETAINED |
| `SCHEDULER-CAPACITY-UNAVAILABLE-DOES-NOT-CRASH` | REWRITTEN |
| `SCHEDULER-CAPACITY-UNAVAILABLE-FALLS-BACK-TO-LARGER-TYPE` | NEW |
| `SCHEDULER-CAPACITY-UNAVAILABLE-NO-EXPENSIVE-FALLBACK` | NEW |
| `SCHEDULER-NODE-READY-AT-FOUR-MINUTES` | RETAINED |
| `SCHEDULER-NODE-REMAINS-LAUNCHING-BEFORE-READY` | RETAINED |
| `SCHEDULER-LAUNCH-FAILURE-AFTER-FIFTEEN-MINUTES` | REWRITTEN |
| `SCHEDULER-NEVER-READY-RETRY-BACKS-OFF` | NEW |
| `IDLE-REAPER-BEFORE-TIMEOUT` | RETAINED |
| `IDLE-REAPER-EXACT-TIMEOUT` | RETAINED |
| `IDLE-REAPER-AFTER-TIMEOUT` | REWRITTEN |
| `IDLE-REAPER-EXACT-TIMEOUT-AFTER-MIN-LIFETIME` | NEW |
| `IDLE-REAPER-WAITS-FOR-MIN-LIFETIME` | NEW |
| `IDLE-REAPER-AT-MIN-LIFETIME` | NEW |
| `IDLE-REAPER-NEVER-TERMINATES-BUSY-NODE` | RETAINED |
| `SCHEDULER-TEAM-ISOLATION-POLICY` | RETAINED |

## Summary

| status | count |
|---|---|
| YAML NEW | 6 |
| YAML RETAINED | 103 |
| YAML REWRITTEN | 3 |
| baseline id gone: REPLACED | 6 |
| baseline id gone: RETAINED | 1 |
| baseline id gone: REWRITTEN | 6 |
| now: NEW | 113 |
| now: RETAINED | 116 |
| now: REWRITTEN | 16 |
| post-baseline: REPLACED | 9 |
| post-baseline: RETAINED (moved) | 1 |
