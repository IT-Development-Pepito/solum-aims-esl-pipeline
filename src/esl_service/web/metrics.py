"""Prometheus exposition for bounded #109 operator metrics."""

from prometheus_client import CollectorRegistry, Gauge, generate_latest
from prometheus_client.exposition import CONTENT_TYPE_LATEST

from esl_service.application.run_evidence import MetricsRead


def render_metrics(metrics: MetricsRead) -> tuple[bytes, str]:
    """Render one request-local registry so stale labels never accumulate."""

    registry = CollectorRegistry()
    issues = Gauge(
        "esl_run_issue_count",
        "Issue rows in the configured recent-run window.",
        ("issue_code", "store", "workflow"),
        registry=registry,
    )
    reconciliation = Gauge(
        "esl_run_reconciliation_count",
        "Reconciliation counts in the configured recent-run window.",
        ("count_name", "store", "workflow"),
        registry=registry,
    )
    duration_total = Gauge(
        "esl_run_step_duration_seconds_sum",
        "Total completed step duration in the configured recent-run window.",
        ("step", "store", "workflow"),
        registry=registry,
    )
    duration_count = Gauge(
        "esl_run_step_duration_seconds_count",
        "Completed step samples in the configured recent-run window.",
        ("step", "store", "workflow"),
        registry=registry,
    )
    window = Gauge(
        "esl_run_metrics_scope_limit",
        "Configured maximum recent runs included per workflow and store.",
        registry=registry,
    )
    window.set(metrics.run_limit_per_scope)
    for issue_metric in metrics.issues:
        issues.labels(
            issue_metric.issue_code,
            issue_metric.store_code,
            issue_metric.workflow_name,
        ).set(issue_metric.count)
    for report_metric in metrics.reconciliation:
        reconciliation.labels(
            report_metric.count_name,
            report_metric.store_code,
            report_metric.workflow_name,
        ).set(
            report_metric.count
        )
    for step_metric in metrics.step_durations:
        labels = (
            step_metric.step_name,
            step_metric.store_code,
            step_metric.workflow_name,
        )
        duration_total.labels(*labels).set(step_metric.total_seconds)
        duration_count.labels(*labels).set(step_metric.sample_count)
    return generate_latest(registry), CONTENT_TYPE_LATEST


__all__ = ["render_metrics"]
