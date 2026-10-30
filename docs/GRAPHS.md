# Agent graphs (generated)

Generated from the compiled LangGraph graphs with `agentic-devops graph`; do not edit by hand.
Dotted edges are conditional. Every step has a dotted edge to `end`: any step can finish the flow
(guardrail blocked, nothing to do, failure, or the final result). Routing is plain Python, never an LLM decision.

## supervisor

Routes CLI commands and Azure DevOps service-hook events to an agent. Deterministic routing.

```mermaid
graph TD
    __start__([start])
    route(route)
    data_validation(data_validation)
    qa_promotion(qa_promotion)
    qa_certification(qa_certification)
    release_preparation(release_preparation)
    release_finalization(release_finalization)
    rollback_planning(rollback_planning)
    __end__([end])
    __start__ --> route
    route -.-> __end__
    route -.-> data_validation
    route -.-> qa_certification
    route -.-> qa_promotion
    route -.-> release_finalization
    route -.-> release_preparation
    route -.-> rollback_planning
    data_validation --> __end__
    qa_certification --> __end__
    qa_promotion --> __end__
    release_finalization --> __end__
    release_preparation --> __end__
    rollback_planning --> __end__
```

## data-validation

PR gate: primary key, fully-NULL columns, column naming, schema contract. `advise` is the only LLM step.

```mermaid
graph TD
    __start__([start])
    load_pr(load_pr)
    resolve_environment(resolve_environment)
    select_tables(select_tables)
    mark_pending(mark_pending)
    run_checks(run_checks)
    advise(advise)
    publish(publish)
    __end__([end])
    __start__ --> load_pr
    advise -.-> __end__
    advise -.-> publish
    load_pr -.-> __end__
    load_pr -.-> resolve_environment
    mark_pending -.-> __end__
    mark_pending -.-> run_checks
    resolve_environment -.-> __end__
    resolve_environment -.-> select_tables
    run_checks -.-> __end__
    run_checks -.-> advise
    select_tables -.-> __end__
    select_tables -.-> mark_pending
    publish --> __end__
```

## qa-promotion

feature/* → main. `find_pr` branches: reuse the open PR, or check commits → draft (LLM) → create.

```mermaid
graph TD
    __start__([start])
    check_branch(check_branch)
    check_pushed(check_pushed)
    find_pr(find_pr)
    check_commits(check_commits)
    analyze_impact(analyze_impact)
    draft_pr(draft_pr)
    create_pr(create_pr)
    check_draft(check_draft)
    check_conflicts(check_conflicts)
    post_status(post_status)
    enable_auto_complete(enable_auto_complete)
    report_blockers(report_blockers)
    __end__([end])
    __start__ --> check_branch
    analyze_impact -.-> __end__
    analyze_impact -.-> draft_pr
    check_branch -.-> __end__
    check_branch -.-> check_pushed
    check_commits -.-> __end__
    check_commits -.-> analyze_impact
    check_conflicts -.-> __end__
    check_conflicts -.-> post_status
    check_draft -.-> __end__
    check_draft -.-> check_conflicts
    check_pushed -.-> __end__
    check_pushed -.-> find_pr
    create_pr -.-> __end__
    create_pr -.-> check_draft
    draft_pr -.-> __end__
    draft_pr -.-> create_pr
    enable_auto_complete -.-> __end__
    enable_auto_complete -.-> report_blockers
    find_pr -.-> __end__
    find_pr -.-> check_commits
    find_pr -.-> check_draft
    post_status -.-> __end__
    post_status -.-> enable_auto_complete
    report_blockers --> __end__
```

## qa-certification

After `databricks bundle deploy -t qa`: validate QA tables, tag `qa-certified/*`.

```mermaid
graph TD
    __start__([start])
    check_source_branch(check_source_branch)
    check_existing_tag(check_existing_tag)
    validate_qa(validate_qa)
    tag_commit(tag_commit)
    __end__([end])
    __start__ --> check_source_branch
    check_existing_tag -.-> __end__
    check_existing_tag -.-> validate_qa
    check_source_branch -.-> __end__
    check_source_branch -.-> check_existing_tag
    validate_qa -.-> __end__
    validate_qa -.-> tag_commit
    tag_commit --> __end__
```

## release-preparation

Certified commit → rc/vX.Y.Z → PR into release. `assess_risk` is the only LLM step and can only raise risk.

```mermaid
graph TD
    __start__([start])
    check_change_window(check_change_window)
    select_candidate(select_candidate)
    collect_changes(collect_changes)
    compute_version(compute_version)
    check_in_flight(check_in_flight)
    revalidate_qa(revalidate_qa)
    ensure_rc_branch(ensure_rc_branch)
    assess_risk(assess_risk)
    open_release_pr(open_release_pr)
    publish_evidence(publish_evidence)
    escalate_reviewers(escalate_reviewers)
    enable_auto_complete(enable_auto_complete)
    notify_approvers(notify_approvers)
    __end__([end])
    __start__ --> check_change_window
    assess_risk -.-> __end__
    assess_risk -.-> open_release_pr
    check_change_window -.-> __end__
    check_change_window -.-> select_candidate
    check_in_flight -.-> __end__
    check_in_flight -.-> revalidate_qa
    collect_changes -.-> __end__
    collect_changes -.-> compute_version
    compute_version -.-> __end__
    compute_version -.-> check_in_flight
    enable_auto_complete -.-> __end__
    enable_auto_complete -.-> notify_approvers
    ensure_rc_branch -.-> __end__
    ensure_rc_branch -.-> assess_risk
    escalate_reviewers -.-> __end__
    escalate_reviewers -.-> enable_auto_complete
    open_release_pr -.-> __end__
    open_release_pr -.-> publish_evidence
    publish_evidence -.-> __end__
    publish_evidence -.-> escalate_reviewers
    revalidate_qa -.-> __end__
    revalidate_qa -.-> ensure_rc_branch
    select_candidate -.-> __end__
    select_candidate -.-> collect_changes
    notify_approvers --> __end__
```

## release-finalization

After `databricks bundle deploy -t prod`: verify, tag vX.Y.Z, hotfix back-merge.

```mermaid
graph TD
    __start__([start])
    identify_release(identify_release)
    check_existing_tag(check_existing_tag)
    verify_production(verify_production)
    tag_release(tag_release)
    open_back_merge(open_back_merge)
    announce(announce)
    __end__([end])
    __start__ --> identify_release
    check_existing_tag -.-> __end__
    check_existing_tag -.-> verify_production
    identify_release -.-> __end__
    identify_release -.-> check_existing_tag
    open_back_merge -.-> __end__
    open_back_merge -.-> announce
    tag_release -.-> __end__
    tag_release -.-> open_back_merge
    verify_production -.-> __end__
    verify_production -.-> tag_release
    announce --> __end__
```
