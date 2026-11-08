"""Prometheus metrics, computed at scrape time from Postgres + Redis (roadmap §30).

Computing on scrape keeps values correct across multiple API workers (in-process counters would
not aggregate). Counters are derived from durable records so they survive restarts. Add rollup
tables if scrape cost ever matters. GPU and unified-memory metrics come from the DGX Spark's own
exporter (e.g. DCGM / node_exporter), scraped separately.
"""

from datetime import timedelta

from prometheus_client import CollectorRegistry, Gauge, generate_latest
from prometheus_client.core import CounterMetricFamily, GaugeMetricFamily
from sqlalchemy import func, select

from app import ops
from app.inference import dbcall
from app.models import Alert, InferenceRequest, Model, ModelDeployment, utcnow
from app.scheduler import Scheduler


class NexoraCollector:
    def __init__(self, inp: ops.OpsInputs, db_stats: dict):
        self.inp, self.db = inp, db_stats

    def collect(self):
        lbl = ["model", "deployment", "node"]
        q = GaugeMetricFamily("nexora_queue_depth", "Requests waiting per deployment", labels=lbl)
        a = GaugeMetricFamily("nexora_active_requests", "Requests running per deployment", labels=lbl)
        h = GaugeMetricFamily("nexora_model_healthy", "1 if the deployment's last health check passed",
                              labels=lbl)
        for d in self.inp.deployments:
            q.add_metric([d.model, d.deployment, d.node], d.depth)
            a.add_metric([d.model, d.deployment, d.node], d.active)
            h.add_metric([d.model, d.deployment, d.node], 1 if d.health == "healthy" else 0)
        yield from (q, a, h)

        w = GaugeMetricFamily("nexora_queue_wait_seconds_avg_5m", "Average queue wait over 5 minutes")
        if self.inp.avg_queue_wait_ms_5m is not None:
            w.add_metric([], self.inp.avg_queue_wait_ms_5m / 1000)
        yield w

        rq = CounterMetricFamily("nexora_requests", "Inference requests by model and status",
                                 labels=["model", "status"])
        for model, status, n in self.db["requests"]:
            rq.add_metric([model, status], n)
        yield rq
        tk = CounterMetricFamily("nexora_tokens", "Tokens processed", labels=["model", "direction"])
        for model, tin, tout in self.db["tokens"]:
            tk.add_metric([model, "input"], tin or 0)
            tk.add_metric([model, "output"], tout or 0)
        yield tk
        ttft = GaugeMetricFamily("nexora_time_to_first_token_seconds_avg_5m", "Average TTFT over 5 minutes")
        if self.db["ttft_ms_5m"] is not None:
            ttft.add_metric([], self.db["ttft_ms_5m"] / 1000)
        yield ttft
        al = GaugeMetricFamily("nexora_alerts_open", "Open alerts by severity", labels=["severity"])
        for sev, n in self.db["alerts"].items():
            al.add_metric([sev], n)
        yield al


def _db_stats(db) -> dict:
    five = utcnow() - timedelta(minutes=5)
    return {
        "requests": [(m, s, n) for m, s, n in db.execute(
            select(Model.model_alias, InferenceRequest.status, func.count())
            .join(Model, Model.id == InferenceRequest.model_id)
            .group_by(Model.model_alias, InferenceRequest.status))],
        "tokens": [(m, i, o) for m, i, o in db.execute(
            select(Model.model_alias, func.sum(InferenceRequest.input_tokens),
                   func.sum(InferenceRequest.output_tokens))
            .join(Model, Model.id == InferenceRequest.model_id).group_by(Model.model_alias))],
        "ttft_ms_5m": db.scalar(select(func.avg(InferenceRequest.time_to_first_token_ms)).where(
            InferenceRequest.started_at >= five)),
        "alerts": {sev: n for sev, n in db.execute(
            select(Alert.severity, func.count()).where(Alert.status == "open").group_by(Alert.severity))},
    }


async def render(sched: Scheduler) -> bytes:
    inp = await ops.gather_inputs(sched)
    stats = await dbcall(_db_stats)
    reg = CollectorRegistry()
    reg.register(NexoraCollector(inp, stats))
    info = Gauge("nexora_up", "Nexora API is serving", registry=reg)
    info.set(1)
    return generate_latest(reg)


_ = ModelDeployment  # (imported for type clarity in future per-deployment series)
